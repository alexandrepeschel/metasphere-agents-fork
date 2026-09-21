"""Durable retry queue for inbound messages that could not reach a REPL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..io import atomic_write_text
from ..paths import Paths, resolve


def _queue_dir(paths: Paths) -> Path:
    return paths.state / "pending_inbound"


def enqueue_inbound(
    *,
    delivery_id: str,
    from_user: str,
    text: str,
    session: str,
    surface_id: str = "telegram",
    reply_command: str | None = None,
    paths: Paths | None = None,
) -> Path:
    """Persist one failed inbound delivery, idempotently by delivery id."""
    paths = paths or resolve()
    queue = _queue_dir(paths)
    queue.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    marker = queue / f"{digest}.json"
    atomic_write_text(
        marker,
        json.dumps({
            "delivery_id": delivery_id,
            "from_user": from_user,
            "text": text,
            "session": session,
            "surface_id": surface_id,
            "reply_command": reply_command,
        }) + "\n",
    )
    return marker


def retry_pending_inbound(paths: Paths | None = None) -> int:
    """Retry queued inbound messages and remove only confirmed deliveries."""
    paths = paths or resolve()
    queue = _queue_dir(paths)
    if not queue.is_dir():
        return 0

    from ..telegram.inject import submit_to_tmux

    delivered = 0
    for marker in sorted(queue.glob("*.json")):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            from_user = data["from_user"]
            text = data["text"]
            session = data["session"]
            surface_id = data.get("surface_id", "telegram")
            reply_command = data.get("reply_command")
            if not all(isinstance(value, str) for value in (
                from_user, text, session, surface_id,
            )):
                raise ValueError("invalid pending inbound fields")
            if reply_command is not None and not isinstance(reply_command, str):
                raise ValueError("invalid pending inbound reply command")
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            try:
                marker.unlink()
            except OSError:
                pass
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
            continue
        try:
            marker.unlink()
        except OSError:
            # A later retry can duplicate a successfully delivered message,
            # but retaining it is safer than claiming durable state vanished.
            continue
        delivered += 1
    return delivered
