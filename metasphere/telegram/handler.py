"""Single source-of-truth handler for incoming Telegram updates.

Called by ``metasphere.telegram.poller.run_poll_iteration`` on every
update the gateway daemon sees. There is no other per-update path:
both the old CLI ``_handle_update`` wrapper and the ``metasphere
telegram poll/once`` subcommands have been removed (the gateway
systemd service is the single source of truth for inbound polling).
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from ..io import atomic_write_text
from ..paths import resolve as _resolve_paths
from . import api, archiver, attachments, commands, inject, poller, routing


# Injectable hooks, mostly for tests. The defaults call the real
# production helpers; tests pass stubs to inspect side effects without
# monkeypatching module-globals.
Sender = Callable[..., object]
Reactor = Callable[..., object]
TmuxSubmit = Callable[..., bool]
ChatIdSaver = Callable[[int], None]
PendingAckWriter = Callable[[int, int], None]
PendingInboundWriter = Callable[..., object]
# (chat_id, username) -> (agent_id, allowed, deny_message_or_None)
TargetResolver = Callable[..., routing.Resolution]


def _default_save_chat_id(chat_id: int) -> None:
    """Persist the rewrite chat id to the canonical file.

    Kept here (not imported from cli/telegram.py) so the gateway path
    doesn't have to depend on the CLI module — avoids an import cycle.
    """
    path = os.path.expanduser("~/.metasphere/config/telegram_chat_id_rewrite")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_text(path, str(chat_id))
    except Exception:
        pass


def _is_addressed_to_bot(u: poller.Update, bot_username: str,
                         bot_id: Optional[int]) -> bool:
    """Return True iff the update should wake the agent.

    Restrictive only for ``chat_type in ("group", "supergroup")``.
    Private DMs and channels (and unknown chat types — e.g.,
    pre-2026-05-01 fixtures that don't set ``chat.type``) wake on
    every body-bearing inbound, preserving existing behavior. Group
    chatter, in contrast, only wakes when the message is explicitly
    addressed to the bot — privacy mode is OFF on the production
    bot so we receive every group message, and unaddressed lines
    must NOT clobber the orchestrator's tmux REPL.

    "Addressed" in a group means any of:
      - text starts with ``/`` (slash command, with or without
        ``@bot`` suffix).
      - reply_to_message authored by the bot itself.
      - explicit ``@<bot_username>`` mention entity in the message.

    ``bot_username`` should be lowercased; either may be falsy if
    ``getMe`` failed (we degrade to "no group wakes" — safer than
    waking on everything).
    """
    if u.chat_type not in ("group", "supergroup"):
        return True
    msg = u.raw.get("message") or u.raw.get("edited_message") or {}
    text = u.text or msg.get("caption") or ""
    if text.startswith("/"):
        return True
    if bot_id is not None and u.reply_to_from_id == bot_id:
        return True
    if not bot_username:
        return False
    target = "@" + bot_username
    for entity in (msg.get("entities") or msg.get("caption_entities") or []):
        if entity.get("type") != "mention":
            continue
        offset = entity.get("offset") or 0
        length = entity.get("length") or 0
        handle = text[offset:offset + length].lower()
        if handle == target:
            return True
    return False


def _default_pending_ack_writer(chat_id: int, message_id: int) -> None:
    """Stash ``{chat_id, message_id}`` so the posthook can flip 👀 → 👍
    once the orchestrator's reply lands. Best-effort.
    """
    try:
        paths = _resolve_paths()
        paths.state.mkdir(parents=True, exist_ok=True)
        (paths.state / "telegram_pending_ack.json").write_text(
            json.dumps({"chat_id": chat_id, "message_id": message_id})
        )
    except Exception:
        pass


def _default_pending_inbound_writer(**kwargs) -> None:
    """Queue an addressed inbound message when immediate tmux delivery fails."""
    try:
        from ..gateway.pending import enqueue_inbound

        enqueue_inbound(**kwargs)
    except Exception:
        pass


def handle_update(
    u: poller.Update,
    *,
    sender: Optional[Sender] = None,
    reactor: Optional[Reactor] = None,
    tmux_submit: Optional[TmuxSubmit] = None,
    save_chat_id: Optional[ChatIdSaver] = None,
    write_pending_ack: Optional[PendingAckWriter] = None,
    write_pending_inbound: Optional[PendingInboundWriter] = None,
    resolve_target: Optional[TargetResolver] = None,
    surface_id: str = "telegram",
    target_agent_id: str = "@orchestrator",
) -> None:
    """Process a single inbound Telegram update.

    Flow (union of what cli/telegram.py and gateway/daemon.py each did
    partially):

    1. Reaction → archive_reaction, return.
    2. No chat_id → early return.
    3. Parse attachments. Body = text or caption.
    4. Debug log post_parse.
    5. No body and no refs → early return.
    6. Save chat_id (non-forum DM only).
    7. Archive (try/except — must not block injection).
    8. Slash command → dispatch + reply, return.
    9. Download attachments, render block, append to payload.
    10. React 👀, stash pending_ack.
    11. Inject payload into the orchestrator tmux session.
    """
    sender = sender or api.send_message
    reactor = reactor or api.set_message_reaction
    tmux_submit = tmux_submit or inject.submit_to_tmux
    save_chat_id = save_chat_id or _default_save_chat_id
    write_pending_ack = write_pending_ack or _default_pending_ack_writer
    write_pending_inbound = (
        write_pending_inbound or _default_pending_inbound_writer
    )
    resolve_target = resolve_target or routing.resolve_target

    if u.kind == "reaction":
        if u.chat_id is None:
            return
        archiver.archive_reaction(
            target_message_id=u.reaction_target_message_id,
            emojis=u.reaction_emojis,
            from_username=u.from_username,
            chat_id=u.chat_id,
            date=u.date,
        )
        return

    if u.chat_id is None:
        attachments.debug_log({
            "stage": "early_return", "reason": "chat_id is None",
            "update_id": u.update_id,
        })
        return

    msg = u.raw.get("message") or u.raw.get("edited_message") or u.raw
    refs = attachments.parse_attachments(msg)
    # Photos/videos/documents carry a ``caption`` instead of ``text``.
    caption = msg.get("caption") or ""
    body = u.text or caption

    attachments.debug_log({
        "stage": "post_parse",
        "update_id": u.update_id,
        "summary": attachments.summarize_message_for_debug(msg),
        "refs": [
            {"kind": r.kind, "file_id": r.file_id, "file_size": r.file_size,
             "file_name": r.file_name, "mime_type": r.mime_type}
            for r in refs
        ],
        "body_source": "text" if u.text else ("caption" if caption else None),
        "body_len": len(body or ""),
    })

    if not body and not refs:
        attachments.debug_log({
            "stage": "early_return", "reason": "no body and no refs",
            "update_id": u.update_id,
        })
        return

    # Save chat id only on DMs (forum topics have thread_id).
    if not u.thread_id:
        try:
            save_chat_id(u.chat_id)
        except Exception:
            pass

    try:
        archiver.archive_message(msg, surface_id=surface_id)
        archiver.save_latest(msg)
    except Exception as e:
        attachments.debug_log({
            "stage": "archive_error",
            "update_id": u.update_id,
            "error": f"{type(e).__name__}: {e}",
        })

    ctx = commands.Context(
        chat_id=u.chat_id, from_user=u.from_username or "?", thread_id=u.thread_id
    )

    # Slash dispatch — key on u.text only (a caption starting with "/"
    # is not a command).
    if u.text and u.text.startswith("/"):
        reply = commands.dispatch(u.text, ctx)
        if reply:
            try:
                if isinstance(reply, commands.Reply):
                    sender(
                        u.chat_id, reply.text,
                        parse_mode=reply.parse_mode,
                        message_thread_id=u.thread_id,
                    )
                else:
                    sender(u.chat_id, reply, message_thread_id=u.thread_id)
            except Exception:
                pass
        return

    payload = body or ""
    downloaded: list = []
    if refs:
        downloaded = attachments.download_attachments(
            u.message_id or u.update_id, refs,
        )
        block = attachments.render_attachment_block(downloaded)
        payload = f"{payload}\n\n{block}".strip() if payload else block

    attachments.debug_log({
        "stage": "pre_inject",
        "update_id": u.update_id,
        "downloaded": [
            {"kind": d.kind,
             "path": str(d.path) if d.path else None,
             "file_size": d.file_size,
             "error": d.error}
            for d in downloaded
        ],
        "payload_preview": payload[:200],
    })

    # React 👀 + stash pending_ack BEFORE injecting so the user sees an
    # acknowledgement quickly, even if the tmux submit stalls.
    if u.message_id:
        try:
            reactor(u.chat_id, u.message_id, "👀")
        except Exception:
            pass
        try:
            write_pending_ack(u.chat_id, u.message_id)
        except Exception:
            pass

    # Wake gating: privacy mode is OFF on the production bot, so we
    # receive every group message — but only addressed ones should
    # clobber the orchestrator's tmux REPL. Unaddressed group chatter
    # stays in the stream/archive (already written above) and waits
    # for the next heartbeat tick to surface.
    bot = api.bot_identity()
    if not _is_addressed_to_bot(u, bot.get("username") or "", bot.get("id")):
        attachments.debug_log({
            "stage": "wake_skipped",
            "reason": "unaddressed group message",
            "update_id": u.update_id,
            "chat_type": u.chat_type,
        })
        return

    # Per-sender routing + access gate (telegram-access.yaml; fail-open).
    # Resolve which agent this sender reaches. With no config every sender
    # resolves to (@orchestrator, allowed=True) — the historical
    # single-operator behavior. A mapped sender routes to their own agent;
    # an unmapped sender under `enforce: true` is DENIED (deny text sent,
    # no inject). Only applied when the caller left target_agent_id at the
    # default so an explicit caller-pinned target is never overridden.
    if target_agent_id == "@orchestrator":
        try:
            resolved_agent, allowed, deny_message = resolve_target(
                u.chat_id, u.from_username,
            )
        except Exception as e:  # noqa: BLE001 — routing must never break inbound
            attachments.debug_log({
                "stage": "route_resolve_failed",
                "update_id": u.update_id,
                "error": f"{type(e).__name__}: {e}",
            })
            resolved_agent, allowed, deny_message = "@orchestrator", True, None
        if not allowed:
            attachments.debug_log({
                "stage": "access_denied",
                "update_id": u.update_id,
                "chat_id": u.chat_id,
                "from": u.from_username,
            })
            if deny_message:
                try:
                    sender(u.chat_id, deny_message, message_thread_id=u.thread_id)
                except Exception:
                    pass
            return
        target_agent_id = resolved_agent

    # Addressed inbound — the target agent's tmux session must be alive
    # when we inject, otherwise tmux_submit returns False and the message
    # sits in the archive until the next heartbeat tick (the dormant-
    # session 9-min-latency incident from 2026-04-30). Both paths are
    # idempotent no-ops when the session already exists.
    try:
        if target_agent_id == "@orchestrator":
            from ..gateway.session import start_session as _start_session
            _start_session()
        else:
            from ..agents import wake_persistent
            wake_persistent(target_agent_id)
    except Exception as e:  # noqa: BLE001 — never break inject on session-create failure
        # Surface the failure in the debug log so the original
        # 9-min-latency bug doesn't recur silently when session
        # creation itself starts failing (tmux missing, disk full, etc.).
        attachments.debug_log({
            "stage": "session_start_failed",
            "update_id": u.update_id,
            "target_agent": target_agent_id,
            "error": f"{type(e).__name__}: {e}",
        })

    # Pin (surface_id, chat_id) as the agent's active conversation so
    # outbound under `--surface auto` defaults back to this surface for
    # the duration of the reply burst. Best-effort: a write failure
    # must not block the inject.
    try:
        from ..paths import resolve as _resolve
        from ..routing.active import set_active_conversation
        set_active_conversation(
            target_agent_id, surface_id, u.chat_id, _resolve(),
        )
    except Exception as e:  # noqa: BLE001
        attachments.debug_log({
            "stage": "active_conversation_write_failed",
            "update_id": u.update_id,
            "error": f"{type(e).__name__}: {e}",
        })

    # Courtesy reply on rate-limit: if the target agent's pane is showing a
    # Claude usage/rate limit it can't generate a reply, so this inbound would
    # otherwise get silence. Tell the user once (deduped per agent). Still
    # inject below so the message is processed when the limit clears. The send
    # is bound to THIS chat+thread so the courtesy lands on the origin surface.
    if u.chat_id is not None:
        chat_id = u.chat_id  # narrowed for the closure
        thread_id = u.thread_id

        def _courtesy_send(text: str) -> None:
            sender(chat_id, text, message_thread_id=thread_id)

        try:
            from ..cli.failsafe import maybe_courtesy_reply
            from ..session import _resolve_session

            maybe_courtesy_reply(
                target_agent_id,
                _resolve_session(target_agent_id),
                _courtesy_send,
            )
        except Exception:
            pass

    # defer_if_busy=False: telegram-user inbound IS the user typing.
    # Clobbering visible REPL content with a telegram message is not a
    # race — it's precisely what the user wants. Only NON-user injectors
    # (heartbeat, scheduled cron, agent-to-agent wakes, restart-wake)
    # defer; this path must always land. PR #23 originally set this to
    # True and broke the user's telegram inbound when the orchestrator
    # pane had any visible typed content (12:15/12:48/12:54 CEST went
    # to /dev/null with status=read but no user-turn).
    #
    # session: route to the RESOLVED agent's tmux session. For
    # @orchestrator this is DEFAULT_SESSION (byte-identical to the
    # historical call); a per-sender-mapped agent gets its own session.
    try:
        from ..session import _resolve_session
        target_session = _resolve_session(target_agent_id)
    except Exception:
        target_session = inject.DEFAULT_SESSION
    # QUEUE the inbound message behind any in-flight turn instead of
    # interrupting it. escape_prefix=True (the old default) fired an Escape to
    # kill the running turn before pasting; when that Escape landed mid-tool-
    # call / mid-stream it wedged the session ("Request interrupted by user" →
    # "Something went wrong / use /new"). escape_prefix=False lets Claude Code's
    # built-in user-turn queue hold the message and process it in order after
    # the current turn finishes — the same safe path every auto-injector
    # (heartbeat/wake/posthook) already uses.
    #
    # defer_if_busy stays False: telegram-user inbound must land even when the
    # REPL input box shows typed content (that is precisely what the user is
    # replacing). The lower-level Codex startup-selector safety gate is the
    # intentional exception; a failed addressed delivery is persisted below
    # and retried by the watchdog until tmux confirms it landed.
    # Setting defer_if_busy=True silently dropped user messages
    # whenever the pane had typed content — the 2026-04-16 PR #23 regression
    # guarded by test_handle_update_telegram_inject_does_not_defer. So only the
    # interrupt behaviour changes here, not the delivery guarantee.
    #
    # Trade-off: telegram no longer interrupts a running turn; a deliberate
    # mid-turn stop would need an explicit /stop command (follow-up).
    delivered = tmux_submit(
        f"@{u.from_username or 'user'}", payload,
        session=target_session, defer_if_busy=False, escape_prefix=False,
    )
    if delivered is False:
        write_pending_inbound(
            delivery_id=f"{surface_id}:{u.update_id}:{target_agent_id}",
            from_user=f"@{u.from_username or 'user'}",
            text=payload,
            session=target_session,
            surface_id="telegram",
            reply_command=None,
        )
