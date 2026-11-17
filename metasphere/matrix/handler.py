"""Inbound Matrix event handler.

One entry point, :func:`handle_event`, called per timeline event by the
poller. Parity with the Telegram handler:

- text -> injected as ``[<user> via matrix | reply: <cmd>] <text>``
- reply-to -> ``↩ replying to: "<quoted>"`` line (same format as Telegram)
- ``m.reaction`` -> archived as a reaction row (same stream/shape)
- voice / audio / image / file -> downloaded under ~/.metasphere/attachments,
  voice transcribed when faster-whisper is installed, rendered with the
  shared ``[attachments]`` block
- slash commands -> ``telegram.commands.dispatch`` (surface-neutral)
- 👀 reaction on receipt; the posthook flips it to 👍 when the reply lands
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

from . import api, routing

logger = logging.getLogger(__name__)

TmuxSubmit = Callable[..., bool]

_QUOTE_MAX = 120

# event_id -> plain text, so a reply-to can be quoted without a round trip.
_TEXT_CACHE: "OrderedDict[str, str]" = OrderedDict()
_TEXT_CACHE_MAX = 500


def remember_text(event_id: str, text: str) -> None:
    if not event_id or not text:
        return
    _TEXT_CACHE[event_id] = text
    _TEXT_CACHE.move_to_end(event_id)
    while len(_TEXT_CACHE) > _TEXT_CACHE_MAX:
        _TEXT_CACHE.popitem(last=False)


_FALLBACK_LINE = re.compile(r"^>( .*)?$")


def strip_reply_fallback(body: str) -> str:
    """Drop the legacy ``> <@user> quoted`` reply fallback some clients
    prepend to ``body`` (lines starting ``>`` up to the first blank line)."""
    lines = body.split("\n")
    if not lines or not lines[0].startswith("> "):
        return body
    i = 0
    while i < len(lines) and _FALLBACK_LINE.match(lines[i]):
        i += 1
    if i >= len(lines) or lines[i].strip() != "":
        return body  # no blank-line terminator: a real quote, not a fallback
    return "\n".join(lines[i + 1:]).strip()


def _in_reply_to(content: dict) -> Optional[str]:
    rel = content.get("m.relates_to") or {}
    return (rel.get("m.in_reply_to") or {}).get("event_id") or None


def reply_to_line(
    room_id: str, content: dict, *,
    fetch: Optional[Callable[[str, str], dict]] = None,
) -> str:
    """``↩ replying to: "<quoted>"`` for a reply event, else ``""``.

    Same format as ``telegram.handler._reply_to_line``. The quoted text
    comes from the local cache, else one ``GET /event``; any failure
    degrades to no line (never blocks the inbound)."""
    target = _in_reply_to(content)
    if not target:
        return ""
    quoted = _TEXT_CACHE.get(target, "")
    if not quoted:
        try:
            ev = (fetch or (lambda r, e: api.get_event(r, e)))(room_id, target)
            c = ev.get("content") or {}
            quoted = strip_reply_fallback(str(c.get("body") or ""))
        except Exception as e:  # noqa: BLE001
            logger.debug("matrix reply lookup failed: %r", e)
            return ""
    quoted = " ".join(quoted.split())
    if not quoted:
        return ""
    if len(quoted) > _QUOTE_MAX:
        quoted = quoted[:_QUOTE_MAX].rstrip() + "..."
    return f'↩ replying to: "{quoted}"'


# ------------------------------------------------------------ attachments

_MSGTYPE_KIND = {
    "m.image": "photo", "m.video": "video", "m.audio": "audio",
    "m.file": "document",
}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _attachment_kind(content: dict) -> Optional[str]:
    kind = _MSGTYPE_KIND.get(content.get("msgtype") or "")
    if kind == "audio" and "org.matrix.msc3245.voice" in content:
        return "voice"
    return kind


def _caption(content: dict) -> str:
    """Caption text for a media event. Per spec ``body`` is the filename
    unless ``filename`` is also set, in which case ``body`` is a caption."""
    fn = content.get("filename")
    body = str(content.get("body") or "")
    if fn and body and body != fn:
        return body
    return ""


def download_attachment(
    content: dict, event_id: str, *, root: Optional[Path] = None,
    downloader: Optional[Callable[..., int]] = None,
):
    """Download one media event. Never raises; errors ride in the result."""
    from ..telegram import attachments as att

    kind = _attachment_kind(content) or "document"
    info = content.get("info") or {}
    mime = info.get("mimetype")
    duration = info.get("duration")
    duration_s = int(duration / 1000) if isinstance(duration, (int, float)) else None
    size = info.get("size")
    url = content.get("url")
    if not url:
        enc = "encrypted (E2EE) media is not supported" if content.get("file") else "no url"
        return att.DownloadedAttachment(
            kind=kind, path=None, file_size=size, mime_type=mime,
            duration=duration_s, error=enc,
        )
    name = _UNSAFE.sub("_", str(content.get("filename") or content.get("body") or "file"))[:80] or "file"
    if kind == "voice" and not name.endswith(".oga"):
        name = "voice.oga"  # matches the attachments retention glob
    dest = (root or att.ATTACHMENTS_ROOT) / _UNSAFE.sub("_", event_id.lstrip("$"))[:60] / name
    try:
        n = (downloader or api.download_media)(url, dest)
    except Exception as e:  # noqa: BLE001
        return att.DownloadedAttachment(
            kind=kind, path=None, file_size=size, mime_type=mime,
            duration=duration_s, error=f"download: {e}",
        )
    item = att.DownloadedAttachment(
        kind=kind, path=dest, file_size=size or n, mime_type=mime, duration=duration_s,
    )
    if kind in ("voice", "audio"):
        att._attempt_transcription(item)
    return item


# --------------------------------------------------------------- plumbing

def _archive(ev: dict, room_id: str, sender: str, text: str, surface_id: str,
             content: dict) -> None:
    try:
        from ..telegram import archiver

        reply_target = _in_reply_to(content)
        rec: dict[str, Any] = {
            "kind": "matrix",
            "message_id": ev.get("event_id"),
            "from": {"username": routing.short_name(sender)},
            "chat": {"id": room_id},
            "text": text,
            "date": int((ev.get("origin_server_ts") or 0) / 1000),
        }
        if reply_target:
            rec["reply_to_message"] = {
                "message_id": reply_target,
                "text": _TEXT_CACHE.get(reply_target, ""),
            }
        archiver.archive_message(rec, surface_id=surface_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("matrix archive failed: %r", e)


def _archive_reaction(ev: dict, room_id: str, sender: str, key: str, target: str,
                      surface_id: str) -> None:
    from ..telegram import archiver

    archiver.archive_reaction(
        target_message_id=target, emojis=[key],
        from_username=routing.short_name(sender), chat_id=room_id,  # type: ignore[arg-type]
        date=int((ev.get("origin_server_ts") or 0) / 1000) or None,
    )


def _pin(target_agent_id: str, surface_id: str, room_id: str) -> None:
    try:
        from ..paths import resolve
        from ..routing.active import set_active_conversation

        set_active_conversation(target_agent_id, surface_id, room_id, resolve())
    except Exception as e:  # noqa: BLE001
        logger.warning("matrix pin failed: %r", e)


def _write_pending_ack(room_id: str, event_id: str) -> None:
    try:
        from ..paths import resolve

        p = resolve()
        p.state.mkdir(parents=True, exist_ok=True)
        (p.state / "matrix_pending_ack.json").write_text(
            json.dumps({"room_id": room_id, "event_id": event_id})
        )
    except Exception:  # noqa: BLE001
        pass


def _default_pending_inbound(**kwargs) -> None:
    try:
        from ..gateway.pending import enqueue_inbound

        enqueue_inbound(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def _ensure_session(target_agent_id: str) -> None:
    try:
        if target_agent_id == "@orchestrator":
            from ..gateway.session import start_session

            start_session()
        else:
            from ..agents import wake_persistent

            wake_persistent(target_agent_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("matrix session start failed for %s: %r", target_agent_id, e)


_WARNED_ENCRYPTED: set[str] = set()


def _warn_encrypted(room_id: str, sender: str, self_user: str, allowed: list[str],
                    sender_fn: Optional[Callable[..., Any]]) -> None:
    """An allowed user wrote into an END-TO-END ENCRYPTED room: the bot has no
    Olm/Megolm keys, so the message is unreadable. Say so once per room per
    process instead of failing silently (a silent drop looks like a dead bot)."""
    if sender == self_user or not routing.is_allowed(sender, allowed):
        return
    if room_id in _WARNED_ENCRYPTED:
        return
    _WARNED_ENCRYPTED.add(room_id)
    logger.warning("matrix: encrypted message in %s from %s is unreadable", room_id, sender)
    try:
        (sender_fn or (lambda room, text, **kw: api.send_message(room, text, **kw)))(
            room_id,
            "I can't read this room: it is end-to-end encrypted and I have no keys. "
            "Please use the unencrypted room I invited you to (\"Clay (unencrypted)\").",
        )
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ entry

def handle_event(
    ev: dict,
    room_id: str,
    *,
    surface_id: str = "matrix",
    target_agent_id: str = "@orchestrator",
    allowed: Optional[list[str]] = None,
    self_user: str = "",
    sender_fn: Optional[Callable[..., Any]] = None,
    reactor: Optional[Callable[..., Any]] = None,
    tmux_submit: Optional[TmuxSubmit] = None,
    write_pending_ack: Optional[Callable[[str, str], None]] = None,
    write_pending_inbound: Optional[Callable[..., Any]] = None,
    fetch_event: Optional[Callable[[str, str], dict]] = None,
    downloader: Optional[Callable[..., int]] = None,
    attachments_root: Optional[Path] = None,
) -> bool:
    """Process one timeline event. Returns True when it reached the agent
    (or a slash command was answered), False when dropped."""
    etype = ev.get("type")
    sender = str(ev.get("sender") or "")
    event_id = str(ev.get("event_id") or "")
    content = ev.get("content") or {}
    if etype == "m.room.encrypted":
        _warn_encrypted(room_id, sender, self_user, allowed or [], sender_fn)
        return False
    if etype not in ("m.room.message", "m.reaction"):
        return False
    if sender == self_user:
        return False
    if not routing.is_allowed(sender, allowed or []):
        logger.info("matrix: dropped event from non-allowed sender %s", sender)
        return False

    if etype == "m.reaction":
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") != "m.annotation":
            return False
        _archive_reaction(ev, room_id, sender, str(rel.get("key") or ""),
                          str(rel.get("event_id") or ""), surface_id)
        return True

    # Edits arrive as new events with rel_type m.replace; the original was
    # already delivered. Skip rather than inject a "* corrected" duplicate.
    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        return False
    msgtype = content.get("msgtype") or ""
    if msgtype == "m.notice":
        return False  # bot/bridge notices: never loop on them

    sender_fn = sender_fn or (lambda room, text, **kw: api.send_message(room, text, **kw))
    reactor = reactor or (lambda room, eid, key: api.react(room, eid, key))
    tmux_submit = tmux_submit or _default_submit
    write_pending_ack = write_pending_ack or _write_pending_ack
    write_pending_inbound = write_pending_inbound or _default_pending_inbound

    has_reply = bool(_in_reply_to(content))
    kind = _attachment_kind(content)
    if kind:
        body = _caption(content)
    else:
        body = str(content.get("body") or "")
        if has_reply:
            body = strip_reply_fallback(body)
        if msgtype == "m.emote":
            body = f"* {body}"
    if not body and not kind:
        return False

    remember_text(event_id, body or f"[{kind}]")
    _archive(ev, room_id, sender, body or f"[{kind}]", surface_id, content)

    # Slash commands (text only).
    if not kind and body.startswith("/"):
        try:
            from ..telegram import commands

            ctx = commands.Context(chat_id=room_id, from_user=routing.short_name(sender))  # type: ignore[arg-type]
            reply = commands.dispatch(body, ctx)
            if reply:
                if isinstance(reply, commands.Reply):
                    if reply.parse_mode == "HTML":
                        sender_fn(room_id, "", html=reply.text)
                    else:
                        sender_fn(room_id, reply.text)
                else:
                    sender_fn(room_id, reply)
        except Exception as e:  # noqa: BLE001
            logger.warning("matrix slash dispatch failed: %r", e)
        return True

    payload = body
    rl = reply_to_line(room_id, content, fetch=fetch_event)
    if rl:
        payload = f"{rl}\n{payload}" if payload else rl
    if kind:
        from ..telegram import attachments as att

        item = download_attachment(
            content, event_id, root=attachments_root, downloader=downloader,
        )
        block = att.render_attachment_block([item])
        payload = f"{payload}\n\n{block}".strip() if payload else block

    # Acknowledge fast, before the (possibly slow) injection.
    if event_id:
        try:
            reactor(room_id, event_id, "👀")
        except Exception:  # noqa: BLE001
            pass
        try:
            write_pending_ack(room_id, event_id)
        except Exception:  # noqa: BLE001
            pass

    _ensure_session(target_agent_id)
    _pin(target_agent_id, surface_id, room_id)

    def _courtesy(text: str) -> None:
        sender_fn(room_id, text)

    try:
        from ..cli.failsafe import maybe_courtesy_reply
        from ..session import _resolve_session

        maybe_courtesy_reply(target_agent_id, _resolve_session(target_agent_id), _courtesy)
    except Exception:  # noqa: BLE001
        pass

    try:
        from ..session import _resolve_session

        session = _resolve_session(target_agent_id)
    except Exception:  # noqa: BLE001
        from ..telegram.inject import DEFAULT_SESSION as session  # type: ignore[assignment]

    rcmd = routing.reply_command(surface_id, room_id)
    from_user = f"@{routing.short_name(sender)}"
    delivered = tmux_submit(
        from_user, payload, session=session, defer_if_busy=False,
        escape_prefix=False, surface_id=surface_id, reply_command=rcmd,
    )
    if delivered is False:
        write_pending_inbound(
            delivery_id=f"{surface_id}:{event_id}:{target_agent_id}",
            from_user=from_user, text=payload, session=session,
            target_agent_id=target_agent_id, chat_id=room_id,
            surface_id=surface_id, thread_id=None, reply_command=rcmd,
        )
    return True


def _default_submit(*args, **kwargs) -> bool:
    from ..telegram.inject import submit_to_tmux

    return submit_to_tmux(*args, **kwargs)
