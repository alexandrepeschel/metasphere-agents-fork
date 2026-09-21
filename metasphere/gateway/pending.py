"""Durable retry queue for inbound messages that could not reach a REPL."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from ..io import atomic_write_text, file_lock
from ..paths import Paths, resolve


def _queue_dir(paths: Paths) -> Path:
    return paths.state / "pending_inbound"


def enqueue_inbound(
    *,
    delivery_id: str,
    from_user: str,
    text: str,
    session: str,
    target_agent_id: str,
    chat_id: str | int,
    surface_id: str = "telegram",
    thread_id: int | None = None,
    reply_command: str | None = None,
    paths: Paths | None = None,
) -> Path:
    """Persist one failed inbound delivery, idempotently by delivery id."""
    paths = paths or resolve()
    queue = _queue_dir(paths)
    queue.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    with file_lock(queue / ".lock"):
        existing = sorted(queue.glob(f"*-{digest}.json"))
        if existing:
            marker = existing[0]
        else:
            sequence_file = queue / ".sequence"
            try:
                sequence = int(sequence_file.read_text(encoding="utf-8")) + 1
            except (OSError, ValueError):
                sequence = 1
            atomic_write_text(sequence_file, f"{sequence}\n")
            marker = queue / f"{sequence:020d}-{digest}.json"
        atomic_write_text(
            marker,
            json.dumps({
                "schema_version": 2,
                "delivery_id": delivery_id,
                "from_user": from_user,
                "text": text,
                "session": session,
                "target_agent_id": target_agent_id,
                "chat_id": str(chat_id),
                "surface_id": surface_id,
                "thread_id": thread_id,
                "reply_command": reply_command,
            }) + "\n",
        )
    return marker


def _marker_sort_key(marker: Path) -> tuple[int, int, str]:
    """Order legacy records first, then current records by sequence."""
    prefix, separator, _rest = marker.name.partition("-")
    if separator and prefix.isdigit():
        return (1, int(prefix), marker.name)
    try:
        modified = marker.stat().st_mtime_ns
    except OSError:
        modified = 0
    return (0, modified, marker.name)


def _quarantine(marker: Path, reason: str) -> None:
    """Retain an unreadable record outside the active queue and surface it."""
    target = marker.with_suffix(marker.suffix + ".invalid")
    try:
        marker.replace(target)
    except OSError:
        target = marker
    print(
        f"[pending-inbound] quarantined {target}: {reason}",
        file=sys.stderr,
    )


def _retry_pending_inbound_locked(queue: Path) -> int:
    from ..telegram.inject import submit_to_tmux

    delivered = 0
    blocked_agents: set[str] = set()
    blocked_sessions: set[str] = set()
    for marker in sorted(queue.glob("*.json"), key=_marker_sort_key):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            from_user = data["from_user"]
            text = data["text"]
            stored_session = data["session"]
            target_agent_id = data.get("target_agent_id")
            chat_id = data.get("chat_id")
            surface_id = data.get("surface_id", "telegram")
            thread_id = data.get("thread_id")
            reply_command = data.get("reply_command")
            if not all(isinstance(value, str) for value in (
                from_user, text, stored_session, surface_id,
            )):
                raise ValueError("invalid pending inbound fields")
            legacy = target_agent_id is None and chat_id is None
            if not legacy and not all(isinstance(value, str) for value in (
                target_agent_id, chat_id,
            )):
                raise ValueError("invalid pending inbound routing fields")
            if thread_id is not None and not isinstance(thread_id, int):
                raise ValueError("invalid pending inbound thread id")
            if reply_command is not None and not isinstance(reply_command, str):
                raise ValueError("invalid pending inbound reply command")
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            _quarantine(marker, str(exc))
            continue

        if legacy:
            # PR #42's first queue schema predated explicit agent/chat routing.
            # Its stored session is the only safe delivery destination; do not
            # discard the message merely because a rolling upgrade changed the
            # schema. Legacy records sort before newly sequenced records.
            session = stored_session
            target_key = f"legacy-session:{session}"
        else:
            assert isinstance(target_agent_id, str)
            if target_agent_id in blocked_agents:
                continue
            try:
                from ..session import _resolve_session

                session = _resolve_session(target_agent_id)
            except Exception:
                blocked_agents.add(target_agent_id)
                continue
            target_key = target_agent_id
        if session in blocked_sessions:
            continue

        if not submit_to_tmux(
            from_user,
            text,
            session=session,
            defer_if_busy=False,
            escape_prefix=False,
            surface_id=surface_id,
            reply_command=reply_command,
        ):
            # Preserve FIFO for this target: tmux acceptance means queued, so
            # a later instruction must never leapfrog a blocked earlier one.
            blocked_agents.add(target_key)
            blocked_sessions.add(session)
            continue
        try:
            marker.unlink()
        except OSError:
            # A later retry can duplicate a successfully delivered message,
            # but retaining it is safer than claiming durable state vanished.
            continue
        delivered += 1
    return delivered


def retry_pending_inbound(paths: Paths | None = None) -> int:
    """Retry queued inbound messages and remove only confirmed deliveries.

    A dedicated consumer lock covers the complete read-submit-unlink
    transaction. Enqueue uses a separate lock, so new inbound can still be
    persisted while a slow tmux submission is in progress.
    """
    paths = paths or resolve()
    queue = _queue_dir(paths)
    if not queue.is_dir():
        return 0
    with file_lock(queue / ".retry.lock"):
        return _retry_pending_inbound_locked(queue)
