"""Tests for the surface-aware inbound envelope (``telegram.inject``).

The envelope is the headline of the slack outbound-reply fix: telegram
inbound renders byte-for-byte as before, every other surface gets a
self-describing ``[<user> via <surface> | reply: <cmd>]`` envelope with the
explicit, copy-verbatim reply command baked in (no pin dependence).
"""

from __future__ import annotations

from metasphere.telegram.inject import render_envelope


def test_telegram_envelope_unchanged():
    """Telegram keeps the historical ``[telegram from <user>] <text>``."""
    assert render_envelope("telegram", "bob", "hi") == "[telegram from bob] hi"
    # per-instance telegram surface still renders as telegram
    assert render_envelope("telegram-relay", "jens", "yo") == (
        "[telegram from jens] yo"
    )
    # empty / unknown surface defaults to telegram shape (back-compat)
    assert render_envelope("", "u", "x") == "[telegram from u] x"


def test_telegram_envelope_bakes_explicit_reply_destination():
    cmd = (
        'metasphere message send --surface telegram-relay --chat-id 42 '
        '--thread-id 7 "<reply>"'
    )
    out = render_envelope(
        "telegram-relay", "bob", "hi", reply_command=cmd,
    )
    assert out == f"[telegram from bob | reply: {cmd}] hi"


def test_slack_envelope_bakes_reply_command():
    cmd = 'metasphere slack send --surface slack-explorer --channel C0BC "<reply>"'
    out = render_envelope("slack-explorer", "bob", "status?", reply_command=cmd)
    assert out == (
        '[bob via slack | reply: metasphere slack send '
        '--surface slack-explorer --channel C0BC "<reply>"] status?'
    )
    # explicit reply command present; NOT the old --surface auto pin path
    assert "--channel C0BC" in out
    assert "--surface auto" not in out


def test_slack_envelope_without_reply_command_still_names_surface():
    out = render_envelope("slack-cluster-1", "alice", "ping")
    assert out == "[alice via slack | reply] ping" or out == "[alice via slack] ping"
    assert out.startswith("[alice via slack")
    assert out.endswith("ping")
