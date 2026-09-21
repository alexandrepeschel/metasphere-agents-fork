"""Post-turn (Stop) hook: route assistant reply to Telegram + track activity.

Two responsibilities:

1. **Route** the final assistant text of a turn to Telegram for the
   ``@orchestrator`` agent only. Sub-agents communicate via the messages
   system, never Telegram.
2. **Track** turn completion: bump per-agent activity counter, update
   ``updated_at``, and log a heartbeat event every 10 turns.

The hook **must never raise**. Any exception is logged and swallowed; the
top-level entry point always returns ``0`` so the host REPL's Stop pipeline
keeps working even if metasphere is broken.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import traceback
from pathlib import Path
from typing import Any

from . import breadcrumbs as _bc
from .events import log_event
from .identity import resolve_agent_id
from .io import atomic_write_text, file_lock, read_json, write_json
from .paths import Paths, resolve


# ---------- payload + transcript parsing ----------

def read_stop_hook_payload(stdin_bytes: bytes) -> dict:
    """Parse a Claude Code or Codex JSON Stop-hook payload.

    Empty / invalid input returns ``{}`` rather than raising — the hook
    is occasionally invoked manually with no stdin.
    """
    if not stdin_bytes:
        return {}
    try:
        return json.loads(stdin_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}


def stop_hook_provider(payload: dict) -> str:
    """Return the provider whose Stop payload shape was received."""
    if "last_assistant_message" in payload:
        return "codex"
    return "claude"


def extract_stop_assistant_text(payload: dict) -> str | None:
    """Extract assistant text from a provider-neutral Stop payload.

    Codex includes the completed turn directly as
    ``last_assistant_message``. Claude Code provides a JSONL transcript,
    which needs the existing turn-aware parser.
    """
    if stop_hook_provider(payload) == "codex":
        text = payload.get("last_assistant_message")
        return text if isinstance(text, str) else None

    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    return extract_last_assistant_text(Path(transcript))


def extract_last_assistant_text(transcript_path: Path) -> str | None:
    """Walk a JSONL transcript backwards and return the assistant text
    from the most recent user turn.

    Collects text from ALL assistant entries within the same turn so that
    text emitted before tool calls is not silently dropped. A "turn" is
    bounded by the preceding genuine human message (a ``user`` entry whose
    content is not exclusively ``tool_result`` blocks).

    Returns ``None`` if the file is missing, empty, or the turn produced
    no assistant text at all.
    """
    p = Path(transcript_path)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    # Collect text segments in reverse order; reverse again before joining.
    segments: list[str] = []

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")

        if msg_type == "assistant":
            msg = obj.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if t:
                        parts.append(t)
            if parts:
                segments.append("\n".join(parts))

        elif msg_type == "user":
            # Stop at a genuine human turn. Tool-result entries (content is
            # exclusively tool_result blocks) are intra-turn plumbing — keep
            # walking backward through them.
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                continue  # tool-result splice — still within the current turn
            break  # genuine human message — turn boundary reached

    if not segments:
        return None
    # segments collected in reverse order; restore chronological sequence.
    return "\n\n".join(reversed(segments))


# ---------- silent-tick filter ----------

def should_skip_silent_tick(text: str | None) -> bool:
    """Return True if the turn produced no user-facing text and should
    not be routed to Telegram. Per CLAUDE.md "Heartbeat Turn Etiquette",
    silent ticks must be silent — no placeholders.
    """
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # Match the standardized idle token and common variants at the start.
    if _IDLE_PATTERN.match(stripped):
        return True
    # Trailing ``[idle]`` after preceding prose ALSO suppresses the turn
    # entirely (not just the trailer). Operator directive 2026-06-07 21:21Z:
    # genuinely-operator-facing surfaces go through explicit
    # ``metasphere telegram send`` — pane prose is harness-internal. When
    # an agent emits ``<prose>\n\n[idle]``, the trailer is the agent's
    # own signal that the prose was an internal triage note, not
    # operator-facing content. The previous behaviour (strip trailer,
    # forward prose) caused the same noise the bare-``[idle]`` rule was
    # designed to prevent — one-line triage narrations like "Routine —
    # marking done" leaking to Telegram. Now any trailing-``[idle]`` =
    # full suppression. The trailer is unambiguous (self-delimiting); the
    # narrower free-form variants (``standing by``, ``idle.``) are NOT
    # treated as trailers — those still need to be at turn start.
    if _TRAILING_IDLE_PATTERN.search(stripped):
        return True
    return False


# ---------- telegram routing ----------

def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _last_sent_path(paths: Paths) -> Path:
    return paths.state / "posthook_last_sent"


# Bound for the append-only best-effort diagnostic logs below. Once a log
# crosses this size it is rotated to a single ``.1`` generation so it can't
# grow without bound — mirrors the telegram debug-log rotation.
_LOG_MAX_BYTES = 2 * 1024 * 1024


def _rotate_log(path: Path) -> None:
    """Move ``path`` to its ``.1`` sibling, replacing any existing backup.

    Exactly one previous generation is kept. ``os.replace`` is atomic and
    leaves any fd already open on the old inode valid. Best-effort — the
    caller swallows ``OSError``.
    """
    os.replace(path, path.with_name(path.name + ".1"))


def _append_log_line(path: Path, line: str) -> None:
    """Append ``line`` to ``path`` under ``LOCK_EX``, rotating past the cap.

    Best-effort and never raises: callers wrap this in their own
    ``try/except OSError``. The size is checked while still holding the
    lock so a rotation can't race a concurrent append (posthook runs
    per-turn across every agent, so concurrent writers are real).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            if f.tell() >= _LOG_MAX_BYTES:
                _rotate_log(path)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _telegram_error_log(paths: Paths) -> Path:
    return paths.state / "posthook_telegram_errors.log"


def _suppression_log_path(paths: Paths) -> Path:
    """Per-turn fail-closed suppression log.

    Lives under ``paths.logs`` (not ``paths.state``) because operators
    grep this during postmortems — keep it next to the rest of the
    operational logs, not buried in opaque runtime state.
    """
    return paths.logs / "posthook-suppressions.log"


def _log_suppression(paths: Paths, *, session_id: str, reason: str, agent: str) -> None:
    """Append a single suppression entry. Best-effort, never raises."""
    try:
        log = _suppression_log_path(paths)
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = (
            f"[{ts}] suppressed forward "
            f"agent={agent or '?'} session={session_id or '?'} reason={reason}\n"
        )
        _append_log_line(log, entry)
    except OSError:
        pass


def _notify_orchestrator_of_suppression(
    paths: Paths, *, session_id: str, reason: str, agent: str
) -> None:
    """Send a single !info to @orchestrator's inbox so degraded-context
    turns surface in the message stream. Best-effort, never raises.

    We deliberately do NOT route the suppression notice through Telegram
    — the whole point of failing closed is that we don't trust this
    turn's output channel. The inbox message is enough to alert the
    operator without re-introducing the failure mode we're guarding.
    """
    try:
        from . import messages as _msgs
        body = (
            f"posthook fail-closed: session={session_id or '?'} "
            f"agent={agent or '?'} reason={reason}\n"
            "Telegram auto-forward suppressed for this turn — context-hook "
            "breadcrumb missing or marked failed. See "
            f"{_suppression_log_path(paths)} for the full log."
        )
        _msgs.send_message(
            "@orchestrator",
            "!info",
            body,
            from_agent="@posthook",
            paths=paths,
            wake=False,
        )
    except Exception:  # noqa: BLE001 — never raise from the hook
        pass


def _explicit_send_marker_path(paths: Paths) -> Path:
    """Marker touched by ``metasphere-telegram send`` when the sender is
    @orchestrator. Posthook reads its mtime to decide whether to suppress
    the auto-forward of the final assistant text.
    """
    return paths.state / "orchestrator_explicit_send_at"


# Window inside which an explicit @orchestrator send suppresses the
# Stop-hook auto-forward. Long enough to span the gap between the CLI
# call and the assistant text being finalized; short enough that a stale
# marker from an earlier turn does not silence a genuinely new message.
EXPLICIT_SEND_SUPPRESS_WINDOW_SECONDS = 120


def _explicit_send_marker_fresh(paths: Paths) -> bool:
    marker = _explicit_send_marker_path(paths)
    try:
        if not marker.exists():
            return False
        age = _dt.datetime.now(_dt.timezone.utc).timestamp() - marker.stat().st_mtime
        return 0 <= age <= EXPLICIT_SEND_SUPPRESS_WINDOW_SECONDS
    except OSError:
        return False


def mark_orchestrator_explicit_send(paths: Paths) -> None:
    """Touch the explicit-send marker. Called by metasphere-telegram CLI
    after a successful @orchestrator send so the next Stop-hook tick
    knows to suppress the duplicate auto-forward.
    """
    marker = _explicit_send_marker_path(paths)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


import re as _re

# Bracketed silence tokens an agent may emit on an empty heartbeat tick.
# No canonical/alias distinction — this is just a list of *probable*
# tokens an LLM might reach for to mean "nothing to say"; any of them
# suppresses the turn (per the operator 2026-06-21: "we should just match
# against a list of probable tokens"). Add new likely spellings here.
_SILENT_TOKENS = (
    "silent",
    "idle",
    "quiet",
    "noop",
    "no-op",
    "nothing",
    "none",
    "skip",
)
# A single bracketed token, e.g. ``[silent]`` / ``[no-op]``. Leading/
# trailing backticks are tolerated so a markdown-formatted token like
# `` `[idle]` `` (which an LLM reaches for naturally) still matches —
# without this the code-span wrapper slipped past the gate and leaked the
# marker to Telegram (operator flag 2026-06-22 23:11Z).
_BRACKET_TOKEN = r"`*\[(?:" + "|".join(_SILENT_TOKENS) + r")\]`*"

# The token agents emit by default on a silent tick.
_IDLE_TOKEN = "[silent]"

# Lead-anchored suppressor. Matches any bracketed silence token OR a
# common free-form phrase at the START of the turn — matched as a
# **prefix** (no trailing ``$``) so a placeholder with an appended clock
# / scope / mood-adverb still gets suppressed. The 2026-04-16 incident
# was ~30x "Silent tick at 05:07Z." messages reaching the operator's
# phone because the original ``^...$`` anchor required an exact full-line
# match and the timestamp suffix broke it.
_IDLE_PATTERN = _re.compile(
    r"^\s*(?:"
    + _BRACKET_TOKEN
    + r"|(?:"                          # free-form phrases — word-boundary
    r"standing by"                   #   so "idleness" isn't absorbed, but
    r"|silent tick"                  #   "idle." / "idle, " / "idle at" are.
    r"|still here"
    r"|quiet"
    r"|idle"
    r"|nothing to report"
    r"|nothing new"
    r"|no new (?:work|messages?|tasks?|updates?|activity)"
    r")\b"
    r")",
    _re.IGNORECASE,
)

# Trailer counterpart to ``_IDLE_PATTERN``: matches one or more bracketed
# silence tokens at the END of text. Agents sometimes append a token as a
# turn-end signal after substantive prose; without stripping it Telegram
# receives the prose WITH a dangling ``[silent]`` — the exact UX failure
# the token is supposed to prevent (2026-05-05: found 6/31 recent
# @orchestrator turns leaking the trailer). Only the self-delimiting
# bracketed tokens are stripped as trailers — the free-form variants
# (``standing by``, ``idle.``…) are NOT, since each is a real English
# phrase legitimate prose can end on (``…the user is now idle.``).
_TRAILING_IDLE_PATTERN = _re.compile(r"(?:\s*" + _BRACKET_TOKEN + r")+\s*\Z", _re.IGNORECASE)


def _strip_trailing_idle(text: str) -> str:
    """Strip trailing idle/standing-by tokens from the end of ``text``.

    Idempotent. Returns the cleaned text with any trailing whitespace
    also removed. Substantive prose preceding the trailer is preserved
    so it can still reach Telegram — only the dangling token is removed.
    """
    return _TRAILING_IDLE_PATTERN.sub("", text).rstrip()


def route_to_telegram(text: str, paths: Paths) -> None:
    """Send ``text`` to Telegram, deduping
    against the last-sent hash. Failures are logged, never raised.
    """
    if not text:
        return

    # Defense-in-depth: the primary gate is ``should_skip_silent_tick``
    # in the Stop-hook caller, which now suppresses ANY turn containing
    # a trailing ``[idle]`` (per operator directive 2026-06-07 21:21Z).
    # The strip here remains as a belt-and-braces guard against direct
    # ``route_to_telegram`` callers that bypass the silent-tick gate —
    # if a trailer slips through, strip it; if nothing remains, drop.
    text = _strip_trailing_idle(text.strip())
    if not text:
        return

    # Filter idle-tick placeholders — never forward these.
    if _IDLE_PATTERN.match(text):
        return

    digest = _hash_text(text)
    last_file = _last_sent_path(paths)
    try:
        if last_file.exists():
            prev = last_file.read_text(encoding="utf-8").strip()
            if prev == digest:
                return
    except OSError:
        pass

    try:
        from .telegram import api as telegram_api

        chat_id = _resolve_chat_id(paths)
        if chat_id is None:
            _log_telegram_error(paths, "no chat_id configured (telegram_chat_id missing)")
            return
        telegram_api.send_with_cc(chat_id, text)
    except Exception as exc:  # noqa: BLE001 — must never raise
        # Persist nothing on failure: a transient send error must not
        # poison the dedupe state and silently swallow the next retry.
        _log_telegram_error(paths, f"{type(exc).__name__}: {exc}")
        return

    # Only persist the dedupe hash after a confirmed-good send.
    try:
        atomic_write_text(last_file, digest + "\n")
    except OSError:
        pass


def consume_pending_ack(paths: Paths) -> None:
    """Replace the gateway's 👀 with 👍 on the user message that triggered
    this turn. Called from ``run_posthook`` after we know the turn produced
    user-visible text — regardless of which path (auto-forward or explicit
    ``metasphere-telegram send``) delivered the reply. Always consumes the
    marker so a stale entry can't ack the next unrelated turn. Best-effort.
    """
    pending = paths.state / "telegram_pending_ack.json"
    try:
        if not pending.exists():
            return
        data = json.loads(pending.read_text(encoding="utf-8"))
        try:
            pending.unlink()
        except OSError:
            pass
        cid = data.get("chat_id")
        mid = data.get("message_id")
        if cid is None or mid is None:
            return
        try:
            from .telegram import api as telegram_api
            telegram_api.set_message_reaction(cid, int(mid), "👍")
        except Exception as exc:  # noqa: BLE001
            _log_telegram_error(paths, f"ack reaction failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        _log_telegram_error(paths, f"ack pending read failed: {exc}")


def _resolve_chat_id(paths: Paths) -> str | None:
    """Read the Telegram chat id from the standard config locations.

    Resolution order matches the install-script defaults:

    1. ``$paths.config/telegram.env`` parsed for ``TELEGRAM_CHAT_ID=...``
       (the canonical install-script layout — KEY=VALUE env file).
    2. ``$paths.config/telegram_chat_id`` (one-line bare value).
    3. ``$paths.root/telegram_chat_id`` (legacy fallback).

    Loads the chat id from ``telegram.env`` (KEY=VALUE env file),
    falling back to a bare one-line ``telegram_chat_id`` file.
    """
    env_file = paths.config / "telegram.env"
    try:
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "TELEGRAM_CHAT_ID":
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    except OSError:
        pass

    candidates = [
        paths.config / "telegram_chat_id",
        paths.root / "telegram_chat_id",
    ]
    for c in candidates:
        try:
            if c.exists():
                v = c.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    return None


def _log_telegram_error(paths: Paths, msg: str) -> None:
    try:
        log = _telegram_error_log(paths)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _append_log_line(log, f"[{ts}] {msg}\n")
    except OSError:
        pass


# ---------- activity tracking ----------

def track_turn_completion(agent: str, paths: Paths) -> None:
    """Bump the per-agent activity counter under flock and update
    ``updated_at``. Logs an ``agent.heartbeat`` event every 10 turns.
    """
    agent_dir = paths.agent_dir(agent)
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    activity_file = agent_dir / "activity.json"
    turns = 0
    # One lock spans the activity-json + status + updated_at writes so a
    # racing posthook from a sibling process can never observe a
    # half-updated agent state; the explicit lock ensures atomicity.
    try:
        with file_lock(activity_file):
            data: dict[str, Any] = {}
            if activity_file.exists():
                try:
                    data = json.loads(activity_file.read_text(encoding="utf-8")) or {}
                except (OSError, json.JSONDecodeError):
                    data = {}
            turns = int(data.get("turns") or 0) + 1
            now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data["turns"] = turns
            data["updated_at"] = now
            atomic_write_text(activity_file, json.dumps(data, indent=2, sort_keys=True) + "\n")

            # Upgrade "spawned" → "active" + bump updated_at marker.
            status_file = agent_dir / "status"
            if status_file.exists():
                try:
                    cur = status_file.read_text(encoding="utf-8").strip()
                    if cur == "spawned":
                        atomic_write_text(status_file, "active\n")
                except OSError:
                    pass
            try:
                atomic_write_text(agent_dir / "updated_at", now + "\n")
            except OSError:
                pass
    except OSError:
        return

    # Refresh last_active so reap_dormant sees this Stop signal as input
    # the agent actually processed (the tmux #{session_activity} field
    # alone misses turns where the model produced no terminal output).
    from .agents import touch_last_active
    touch_last_active(agent, paths)

    if turns > 0 and turns % 10 == 0:
        try:
            log_event(
                "agent.heartbeat",
                f"{agent} turn {turns}",
                agent=agent,
                paths=paths,
            )
        except Exception:  # noqa: BLE001
            pass


# ---------- ephemeral-agent task auto-close ----------

def auto_close_finished_task(agent: str, paths: Paths) -> str | None:
    """If ``agent`` has a linked task and its status indicates a clean
    completion, archive the task. Returns the closed task slug, or None.

    Conditions for auto-close (all must hold):
      - ``agent_dir/task_id`` exists and names an active task
      - ``agent_dir/status`` exists and starts with ``complete``
        (the harness completion protocol writes ``complete: <summary>``)
      - the task is not already archived

    Edge cases:
      - panic / error exit → status will not start with ``complete``,
        so we leave the task pending for human triage
      - multiple agents share a task → last writer wins, harmless
      - task already archived → ``_find_task_file`` returns None, no-op
      - missing ``task_id`` (legacy spawns from before this fix) → no-op
    """
    agent_dir = paths.agent_dir(agent)
    task_id_file = agent_dir / "task_id"
    status_file = agent_dir / "status"
    if not task_id_file.exists() or not status_file.exists():
        return None
    try:
        task_id = task_id_file.read_text(encoding="utf-8").strip()
        status = status_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not task_id or not status.lower().startswith("complete"):
        return None

    # Local imports avoid pulling tasks → io chain into every Stop tick
    # and dodge any potential cycle through the cli shims.
    from . import tasks as _tasks

    active_path = _tasks._find_task_file(task_id, include_completed=False)
    if active_path is None:
        return None
    if active_path.parent.name != "active":
        # Already archived (somehow). No-op, but clear the linkage so
        # we don't keep retrying every turn.
        return None

    summary = status.split(":", 1)[1].strip() if ":" in status else status
    try:
        _tasks.complete_task(task_id, f"auto-closed by posthook: {summary}", paths.project_root)
    except Exception:  # noqa: BLE001
        return None
    return task_id


# ---------- deferred slash-command injection ----------

def _check_deferred_command(agent: str, paths: Paths) -> None:
    """If the agent left a deferred command marker, inject it into the
    tmux session as the next user input.

    This lets agents request ``/exit`` (or any slash command) from within
    their assistant response. The posthook runs *after* the turn is
    complete, so the injected text becomes a fresh user message.

    Marker: ``state/<agent>_deferred_cmd`` containing the command text.
    Consumed on read (deleted immediately).
    """
    safe_name = agent.lstrip("@") or "orchestrator"
    marker = paths.state / f"{safe_name}_deferred_cmd"
    try:
        if not marker.exists():
            return
        cmd = marker.read_text(encoding="utf-8").strip()
        marker.unlink(missing_ok=True)
        if not cmd:
            return
    except OSError:
        return

    try:
        from .tmux import submit_to_tmux
        from .session import _resolve_session

        # Project-scoped agents live in ``metasphere-<project>-<agent>``
        # sessions. Bare-name ``session_name_for`` would miss these
        # and target a session that does not exist;
        # ``_resolve_session`` walks the agent
        # registry and returns the project-aware name. Same resolver
        # the CLI ``metasphere session stop|info|restart`` uses.
        session = _resolve_session(agent)
        # defer_if_busy=True: deferred-cmd injection is automatic; if
        # the pane shows a human mid-typing, drop this tick rather
        # than interleave. The cmd was already "deferred" anyway.
        # escape_prefix=False: never interrupt a tool call with a
        # deferred-cmd inject; queue behind the current tool.
        submit_to_tmux(session, cmd, defer_if_busy=True, escape_prefix=False)
    except Exception:  # noqa: BLE001
        pass


def request_deferred_command(cmd: str, paths: Paths | None = None, agent: str | None = None) -> None:
    """Write a deferred command marker so the next posthook tick injects
    ``cmd`` into the agent's tmux session.

    Usage from agent code::

        from metasphere.posthook import request_deferred_command
        request_deferred_command("/exit")
    """
    paths = paths or resolve()
    agent = agent or resolve_agent_id(paths)
    safe_name = agent.lstrip("@") or "orchestrator"
    marker = paths.state / f"{safe_name}_deferred_cmd"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(marker, cmd + "\n")
    except OSError:
        pass


# ---------- top-level entry ----------

def run_posthook(stdin_bytes: bytes, paths: Paths | None = None) -> int:
    """Top-level Stop-hook entry point. Always returns 0."""
    try:
        paths = paths or resolve()
        payload = read_stop_hook_payload(stdin_bytes)

        # The user-scoped Codex Stop hook is visible to ordinary interactive
        # sessions too.  Only supervised Metasphere sessions may mutate agent
        # state or forward their response to Telegram.
        if (
            stop_hook_provider(payload) == "codex"
            and not os.environ.get("METASPHERE_GATEWAY_SESSION")
        ):
            return 0

        agent = resolve_agent_id(paths)

        # Re-entrancy guard: bail if claude-code is already inside a Stop hook.
        already_active = bool(payload.get("stop_hook_active"))

        if not already_active and agent == "@orchestrator":
            transcript = payload.get("transcript_path")
            text = extract_stop_assistant_text(payload)
            if not should_skip_silent_tick(text):
                explicit_fresh = _explicit_send_marker_fresh(paths)
                session_id = str(payload.get("session_id") or "")
                provider = stop_hook_provider(payload)

                # Claude Code's UserPromptSubmit hook writes a per-turn
                # success breadcrumb. Keep its existing fail-closed gate
                # unchanged. Codex supplies the completed reply directly
                # in its Stop payload and has no matching context-hook
                # breadcrumb in this adapter.
                if provider == "claude":
                    ok, reason = _bc.evaluate(
                        paths,
                        session_id=session_id,
                        transcript_path=transcript,
                    )
                else:
                    ok, reason = True, ""

                if not ok:
                    _log_suppression(
                        paths,
                        session_id=session_id,
                        reason=reason,
                        agent=agent,
                    )
                    _notify_orchestrator_of_suppression(
                        paths,
                        session_id=session_id,
                        reason=reason,
                        agent=agent,
                    )
                elif not explicit_fresh:
                    route_to_telegram(text or "", paths)

                if ok or explicit_fresh:
                    try:
                        consume_pending_ack(paths)
                    except Exception:  # noqa: BLE001
                        pass

        track_turn_completion(agent, paths)

        # Auto-close the agent's backing task when an ephemeral agent
        # has finished cleanly. This is the fix for the 2026-04-08
        # backlog drift: previously, ephemeral agents had no
        # task↔agent linkage, so finished work piled up as
        # stale-pending tasks. spawn_ephemeral now writes task_id;
        # this hook closes it on clean exit.
        if agent != "@orchestrator":
            try:
                auto_close_finished_task(agent, paths)
            except Exception:  # noqa: BLE001
                pass

        # Check for deferred slash commands (/exit, etc.) that the agent
        # requested during its turn. Must run last — if the command is
        # /exit, the session will terminate shortly after injection.
        try:
            _check_deferred_command(agent, paths)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — Stop hook must never break the host
        try:
            paths = paths or resolve()
            _log_telegram_error(paths, "run_posthook crash: " + traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass
    return 0


# ---------- CLI entry point ----------


def main() -> int:
    """CLI entry for the Stop hook. Reads the payload from stdin."""
    import sys
    stdin_bytes = sys.stdin.buffer.read()
    return run_posthook(stdin_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
