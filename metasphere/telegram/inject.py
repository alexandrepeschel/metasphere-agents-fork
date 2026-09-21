"""Inject incoming text messages into the orchestrator's tmux session.

The orchestrator runs inside a tmux session named
``metasphere-orchestrator``; without direct injection, incoming user
messages would only surface on the next heartbeat tick (up to 5 min
latency).

Uses :mod:`metasphere.tmux` for reliable tmux paste-submission.
"""

from __future__ import annotations

import re

from ..tmux import submit_to_tmux as _tmux_submit

_USERNAME_RE = re.compile(r"[^\w]+")

DEFAULT_SESSION = "metasphere-orchestrator"


def _surface_type(surface_id: str) -> str:
    """``<type>[-<instance>]`` → ``<type>`` (``slack-explorer`` → ``slack``)."""
    return (surface_id or "").partition("-")[0].strip().lower()


def render_envelope(
    surface_id: str,
    safe_user: str,
    text: str,
    *,
    reply_command: str | None = None,
) -> str:
    """Render the surface-aware inbound envelope prefix.

    Telegram without an explicit reply command renders identically to the
    historical format (``[telegram from <user>] <text>``). Routed inbound may
    include a copy-verbatim reply command so delayed cross-chat turns never
    depend on the mutable active-conversation pin.

    Every other surface gets a self-describing envelope that names the ACTUAL
    origin surface and bakes the **explicit, copy-verbatim reply command** the
    agent should run, so it answers back on the *same* surface instead of
    reflexively reaching for ``telegram send``::

        [alice via slack | reply: metasphere slack send --surface slack-explorer
         --channel C0BC2EV7SFM "<reply>"] <text>

    The reply command is built by the caller (the slack handler, which holds
    the origin surface + channel) and passed verbatim — the channel lives in
    the command string, NOT broadcast as a separate field. When
    ``reply_command`` is omitted the envelope still names the surface but
    carries no reply directive.
    """
    stype = _surface_type(surface_id)
    if not stype or stype == "telegram":
        if reply_command:
            return f"[telegram from {safe_user} | reply: {reply_command}] {text}"
        return f"[telegram from {safe_user}] {text}"
    if reply_command:
        return f"[{safe_user} via {stype} | reply: {reply_command}] {text}"
    return f"[{safe_user} via {stype}] {text}"


def submit_to_tmux(
    from_user: str,
    text: str,
    session: str = DEFAULT_SESSION,
    *,
    defer_if_busy: bool = False,
    escape_prefix: bool = True,
    surface_id: str = "telegram",
    reply_command: str | None = None,
) -> bool:
    """Submit a surface-aware envelope ``<prefix> <text>`` to the tmux session.

    The prefix is rendered by :func:`render_envelope` from ``surface_id`` (and
    the caller-supplied ``reply_command``) — telegram keeps the historical
    ``[telegram from <from_user>] <text>`` shape when no reply command is
    supplied. Routed Telegram and non-Telegram surfaces can carry the
    self-describing ``[<user> ... | reply: <cmd>]`` envelope.
    ``surface_id`` defaults to ``"telegram"`` and ``reply_command`` to ``None``
    so every existing caller is byte-for-byte unchanged.

    Returns True on success, False if tmux/script unavailable or session
    missing. Never raises — injection is best-effort.

    *defer_if_busy* is forwarded to :func:`metasphere.tmux.submit_to_tmux`;
    user-inbound telegram leaves it False (always fire, never silently drop a
    user message), restart-wake passes True (defer on human typing).

    *escape_prefix* still defaults True at this layer, but the user-inbound
    telegram handler (``metasphere/telegram/handler.py``) now passes False:
    escape_prefix=True fires an Escape that INTERRUPTS the in-flight turn, and
    landing mid-tool-call it wedged the session ("Something went wrong / use
    /new", 2026-07-28). escape_prefix=False queues the message behind the
    running turn via Claude Code's user-turn queue instead. restart-wake also
    passes False so it doesn't cut a mid-tool-call on the newly-respawned pane.
    """
    # Usernames / ids are attacker-controlled — sanitise to [\w]+ so they
    # can't smuggle slash-command-like prefixes into the REPL when rendered.
    safe_user = _USERNAME_RE.sub("", from_user) or "unknown"
    payload = render_envelope(
        surface_id, safe_user, text, reply_command=reply_command,
    )
    ok = _tmux_submit(
        session,
        payload,
        defer_if_busy=defer_if_busy,
        escape_prefix=escape_prefix,
    )
    if ok and session == DEFAULT_SESSION:
        # Telegram inject is the canonical "user just spoke to the
        # orchestrator" signal — refresh last_active so reap_dormant
        # treats this session as active even when no terminal output
        # follows immediately (model thinking, deferred-busy paste).
        try:
            from ..agents import touch_last_active
            touch_last_active("@orchestrator")
        except Exception:
            pass
    return ok
