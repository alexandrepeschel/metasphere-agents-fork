"""Tests for ``metasphere.tmux.submit_to_tmux``.

The 2026-04-16 research-monitor outage was caused by stuck
``[Pasted text #N`` placeholders accumulating in agent panes: each
cron-fired wake added another paste on top of the previous unsubmitted
one, the Enter-retry loop saw "placeholder still present" and gave up
without clearing, and the next wake compounded the problem.

This module pins the fix: pre-emptive Escape×2 on every submit to
clear any pending paste before laying down a new one, plus a final
Escape if the Enter-retry loop exhausts without clearing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from metasphere import tmux as T


def _fake_cp(returncode: int = 0, stdout: str = ""):
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = ""
    return cp


def _capture_calls(monkeypatch, pane_states=None):
    """Monkeypatch subprocess.run to record every tmux call. ``pane_states``
    is a list of strings returned by successive ``capture-pane`` calls;
    the list is consumed one entry per capture-pane invocation (default
    behavior: always clean pane).
    """
    pane_states = list(pane_states or [])
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            stdout = pane_states.pop(0) if pane_states else ""
            return _fake_cp(stdout=stdout)
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    return calls


def test_submit_prefixes_single_escape_before_typing(monkeypatch):
    """Every user-inbound submit (escape_prefix=True) fires a SINGLE Escape
    to interrupt any running Claude Code turn, AFTER the pre-flush C-m.
    Esc Esc would open Claude Code's Rewind/Undo menu (not clear input),
    which is what the 2026-04-16 telegram-inbound outage was: we typed
    into the rewind menu's filter and Enter rolled back to a random prior
    turn. Exactly one Escape.

    Order: pre-flush C-m → Escape → paste-buffer → submit C-m.
    """
    calls = _capture_calls(monkeypatch)
    assert T.submit_to_tmux("sess", "hello") is True

    sendkeys = [c for c in calls if "send-keys" in c]
    escapes = [c for c in sendkeys if "Escape" in c]
    # Exactly one Escape send-keys call — the pre-type interrupt.
    # Never two (that's the rewind menu keybinding).
    assert len(escapes) == 1, (
        f"expected exactly ONE Escape (single Escape = interrupt), got {escapes}"
    )
    # First call is the pre-flush C-m (the operator's 2026-04-20 idea:
    # submit any legit pending content as its own user-turn before
    # we paste our new payload).
    assert sendkeys[0][-1] == "C-m", (
        f"first send-keys must be the pre-flush C-m, got {sendkeys[0]}"
    )
    # Escape is second (after pre-flush).
    assert "Escape" in sendkeys[1]
    # Escape must precede the paste-buffer step.
    paste_idx = next((i for i, c in enumerate(calls) if "paste-buffer" in c), -1)
    escape_idx = next((i for i, c in enumerate(calls)
                       if "send-keys" in c and "Escape" in c), -1)
    assert paste_idx > escape_idx > 0


def test_submit_typing_sequence_unchanged_after_prefix(monkeypatch):
    """Content delivery uses tmux load-buffer + paste-buffer (atomic
    paste, bracketed-paste event that TUI commits reliably). Order:
    pre-flush C-m → Escape → load-buffer → paste-buffer → submit C-m.
    2026-04-20: switched from send-keys -l per line + C-j because the
    char-burst path left TUI in a paste-detection race that ate C-m.
    """
    calls = _capture_calls(monkeypatch)
    T.submit_to_tmux("sess", "line1\nline2")
    sendkeys = [c for c in calls if "send-keys" in c]
    # First: pre-flush C-m.
    assert sendkeys[0][-1] == "C-m"
    # Second: Escape prefix.
    assert "Escape" in sendkeys[1]
    # load-buffer was called with the multi-line content.
    load_buf = [c for c in calls if "load-buffer" in c]
    assert load_buf, f"expected load-buffer call, got {calls}"
    # paste-buffer landed the content in the target session.
    paste = [c for c in calls if "paste-buffer" in c]
    assert paste, f"expected paste-buffer call, got {calls}"
    # No more send-keys -l (we paste instead of char-type).
    assert not any("-l" in c for c in sendkeys), (
        "submit_to_tmux should not use send-keys -l anymore "
        "(replaced by paste-buffer 2026-04-20)"
    )
    # Final submit uses C-m (ASCII 0x0D), NOT the Enter keysym.
    assert any(c[-1] == "C-m" for c in sendkeys)


def _buffer_name(calls, verb):
    for c in calls:
        if verb in c and "-b" in c:
            return c[c.index("-b") + 1]
    return None


def test_submit_uses_unique_buffer_name_per_call(monkeypatch):
    """Each submit uses a UNIQUE paste-buffer name (not the shared
    ``_metasphere_submit``) so a concurrent inject — e.g. a cron heartbeat
    process racing the daemon's telegram inject — can't clobber the buffer
    between load and paste. load-buffer and paste-buffer agree within a call;
    successive calls get distinct names."""
    calls1 = _capture_calls(monkeypatch)
    T.submit_to_tmux("sess", "one", escape_prefix=False)
    load1 = _buffer_name(calls1, "load-buffer")
    paste1 = _buffer_name(calls1, "paste-buffer")
    assert load1 is not None and load1.startswith("_metasphere_submit_"), (
        f"expected a unique _metasphere_submit_* buffer, got {load1!r}"
    )
    assert load1 == paste1, "load and paste must target the same buffer per call"

    calls2 = _capture_calls(monkeypatch)
    T.submit_to_tmux("sess", "two", escape_prefix=False)
    load2 = _buffer_name(calls2, "load-buffer")
    assert load2 != load1, "each submit must use a fresh buffer name"


def test_submit_uses_c_m_not_enter_keysym(monkeypatch):
    """Submit must use ``C-m`` (ASCII 0x0D) not the ``Enter`` keysym.

    2026-04-20 root cause: tmux 3.3a's ``Enter`` keysym was silently
    dropped by Claude Code's TUI input handler (Ink/React). Two
    wake-Enter races in one session (visa-lead, metasphere-lead) were
    unblocked only by ``C-m``. The ``Enter`` keysym works fine for bash
    readline but NOT for Claude Code's prompt-submit path.

    Regression guard: if someone casually "cleans up" C-m back to Enter,
    this test fails.
    """
    calls = _capture_calls(monkeypatch)
    T.submit_to_tmux("sess", "probe")
    sendkeys = [c for c in calls if "send-keys" in c]
    # All submit-like calls must use C-m, never the Enter keysym.
    for c in sendkeys:
        assert "Enter" not in c, (
            f"send-keys must use 'C-m' not 'Enter' keysym "
            f"(2026-04-20 wake-Enter race). Got: {c}"
        )
    # At least one C-m call exists (the submit).
    assert any("C-m" in c for c in sendkeys), (
        "expected at least one C-m send-keys call for submit"
    )


def test_submit_no_fallback_escape_on_retry_exhaust(monkeypatch):
    """If the Enter-retry loop exhausts with the input still dirty,
    submit_to_tmux must NOT fire a fallback Escape. Old behavior was
    Escape×2 (which opened the rewind menu — 2026-04-16 telegram
    outage); a single Escape here would interrupt the turn we just
    submitted; leaving the typed text lets submit_watchdog clean up
    asynchronously. Only one Escape send-keys call should happen
    regardless of whether retries succeed.
    """
    pane_states = [
        "[Pasted text #4 +18 lines]",     # retry iter 1 dirty
        "[Pasted text #4 +18 lines]",     # retry iter 2 dirty
        "[Pasted text #4 +18 lines]",     # retry iter 3 dirty
        "[Pasted text #4 +18 lines]",     # final return check still dirty
    ]
    calls = _capture_calls(monkeypatch, pane_states=pane_states)
    T.submit_to_tmux("sess", "m")

    sendkeys = [c for c in calls if "send-keys" in c]
    escapes = [c for c in sendkeys if "Escape" in c]
    # Exactly ONE Escape (the pre-clear), never two.
    assert len(escapes) == 1, (
        f"retry-exhaust must not fire fallback Escape, got {escapes}"
    )


def test_submit_skips_escape_prefix_when_disabled(monkeypatch):
    """Auto-injectors pass ``escape_prefix=False`` so they never interrupt
    a running Claude Code tool call. The initial Escape×2 pre-clear AND
    the fallback Escape at the end must both be suppressed. "Only
    user-inbound interrupts" (operator-confirmed 2026-04-16)."""
    calls = _capture_calls(monkeypatch)
    T.submit_to_tmux("sess", "hello", escape_prefix=False)

    sendkeys = [c for c in calls if "send-keys" in c]
    escapes = [c for c in sendkeys if "Escape" in c]
    assert escapes == [], (
        f"auto-injector must not send Escape, got {escapes}"
    )
    # Content delivered via paste-buffer, then submit C-m.
    assert any("paste-buffer" in c for c in calls), (
        f"expected paste-buffer delivery, got {calls}"
    )
    assert any(c[-1] == "C-m" for c in sendkeys)


def test_submit_zero_escapes_when_escape_prefix_false_and_dirty(monkeypatch):
    """Retry-loop exhaustion with a stuck placeholder: auto-injector
    (``escape_prefix=False``) must emit ZERO Escape send-keys. The
    stuck state is left for the submit_watchdog daemon to clean up
    on its next tick."""
    pane_states = [
        "[Pasted text #4 +18 lines]",     # retry iter 1
        "[Pasted text #4 +18 lines]",     # retry iter 2
        "[Pasted text #4 +18 lines]",     # retry iter 3
        "[Pasted text #4 +18 lines]",     # final return check (still dirty)
    ]
    calls = _capture_calls(monkeypatch, pane_states=pane_states)
    T.submit_to_tmux("sess", "m", escape_prefix=False)

    sendkeys = [c for c in calls if "send-keys" in c]
    escapes = [c for c in sendkeys if "Escape" in c]
    assert escapes == [], (
        f"escape_prefix=False must emit zero Escapes, got {escapes}"
    )


def test_submit_returns_false_when_session_missing(monkeypatch):
    """Pre-existing invariant — no tmux traffic when the session is
    dead. Pinning so the Escape prefix doesn't regress this.
    """
    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=1)  # dead
        return _fake_cp()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.submit_to_tmux("sess", "hello") is False


def test_submit_to_tmux_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise OSError("simulated")

    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    # Must return False, not raise.
    assert T.submit_to_tmux("sess", "hello") is False


# --- Input-buffer guard (Layer 2) ------------------------------------------
#
# 2026-04-16: an operator was typing into the attached orchestrator pane while a
# heartbeat fired ``submit_to_tmux``; the heartbeat's send-keys interleaved
# with his keystrokes and submitted the garbled mess. A fcntl lock cannot
# help here — human keystrokes go via the tty, bypassing in-process locks.
# An attach-aware guard (was PR #22) overreached because operators keep panes
# attached for monitoring. The correct primitive is: inspect the input box
# and defer when it shows typed content.


_BORDER = "─" * 60


def _pane(input_lines: list[str]) -> str:
    """Build a fake Claude Code pane capture with the input box borders
    wrapping the given input_lines. Matches the real layout: some tool
    output above, then top border, then the input lines, then bottom
    border, then a footer."""
    parts = ["tool output above", _BORDER]
    parts.extend(input_lines)
    parts.append(_BORDER)
    parts.append("  ⏵⏵ bypass permissions on · esc to interrupt")
    return "\n".join(parts) + "\n"


def test_input_line_has_typing_detects_typed_content(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ hello there"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is True


def test_input_line_has_typing_false_for_bare_prompt(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ "]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_input_line_has_typing_false_for_paste_placeholder(monkeypatch):
    """A pre-existing ``[Pasted text #`` placeholder is not typing —
    submit_watchdog handles those asynchronously."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ [Pasted text #4 +18 lines]"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_input_line_has_typing_false_for_bare_chevron_with_nbsp(monkeypatch):
    """Empty Claude Code prompt renders as ``❯\\xa0`` (chevron + NBSP).
    ``str.strip()`` removes NBSP; must return False."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯\xa0"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_input_line_has_typing_detects_wrapped_multiline_input(monkeypatch):
    """2026-04-16 regression: when an operator typed a long message that
    Claude Code wrapped across multiple lines, the old heuristic (walk
    last 10 lines looking for ``❯``) missed the continuation lines and
    heartbeats fired mid-typing, interleaving with his keystrokes.

    The new border-based heuristic must see ALL content between the two
    ``─────`` borders, even when the first (``❯``) line is pushed far
    above the last 10 lines by continuation wrapping."""
    wrapped = ["❯ this is a message the operator is typing"]
    # Push the chevron line well past the last-10-window.
    wrapped += [f"  continuation line {i}" for i in range(15)]

    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(wrapped))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is True


def test_input_line_has_typing_false_when_only_continuation_is_whitespace(monkeypatch):
    """An empty input box with a trailing blank continuation line (TUI
    quirk) should not be mistaken for typing."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯\xa0", "", ""]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_input_line_has_typing_false_when_no_borders_visible(monkeypatch):
    """Fail open: if capture-pane doesn't show the input box (e.g.,
    alt-screen app covering the TUI), don't block heartbeats."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout="just plain text\nno borders here\n")
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_codex_placeholder_is_not_typing(monkeypatch):
    pane = "\x1b[1m›\x1b[0m \x1b[2mRun /review on my current changes\x1b[0m\n"

    def fake_run(argv, **kw):
        assert "-e" in argv
        return _fake_cp(stdout=pane)

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


def test_codex_prompt_detects_real_typing(monkeypatch):
    pane = "\x1b[1m›\x1b[0m telegram reply in progress\n"

    def fake_run(argv, **kw):
        return _fake_cp(stdout=pane)

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is True


def test_input_line_has_typing_fails_open_on_error(monkeypatch):
    """Fail open on capture-pane errors — better to occasionally
    interleave than drop every heartbeat on tmux quirks."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(returncode=1)
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert T._input_line_has_typing("/usr/bin/tmux", "sess") is False


# ---------------------------------------------------------------------------
# input_box_content (used by the watchdog's stuck-inline-inject recovery)
# ---------------------------------------------------------------------------


def test_input_box_content_returns_typed_text(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ [wake] new task from @orchestrator"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") == "[wake] new task from @orchestrator"


def test_codex_input_box_content_ignores_placeholder(monkeypatch):
    pane = "\x1b[1m›\x1b[0m \x1b[2mRun /review on my current changes\x1b[0m\n"

    def fake_run(argv, **kw):
        return _fake_cp(stdout=pane)

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") is None


def test_input_box_content_joins_wrapped_lines(monkeypatch):
    lines = ["❯ [task] a long task body that", "wraps across two input rows"]

    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(lines))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") == (
        "[task] a long task body that\nwraps across two input rows"
    )


def test_input_box_content_none_for_empty_box(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯\xa0"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") is None


def test_input_box_content_returns_placeholder_verbatim(monkeypatch):
    """Unlike _input_line_has_typing, a lone placeholder is returned as-is —
    the watchdog decides (and excludes it, leaving it to check_stuck_paste)."""
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ [Pasted text #4 +18 lines]"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") == "[Pasted text #4 +18 lines]"


def test_input_box_content_none_when_no_borders(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(stdout="plain text, no input box\n")
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") is None


def test_input_box_content_none_on_error(monkeypatch):
    def fake_run(argv, **kw):
        if "capture-pane" in argv:
            return _fake_cp(returncode=1)
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.input_box_content("sess") is None


def test_submit_defer_if_busy_skips_when_input_has_typing(monkeypatch):
    """When ``defer_if_busy=True`` and the input area shows typed
    content, abort BEFORE firing any send-keys (no Escape, no typing,
    no Enter)."""
    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ mid-typing-text"]))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.submit_to_tmux("sess", "hello", defer_if_busy=True) is False

    sendkeys = [c for c in calls if "send-keys" in c]
    assert sendkeys == [], (
        f"deferred submit must not fire any send-keys, got: {sendkeys}"
    )


def test_submit_defer_if_busy_proceeds_on_bare_prompt(monkeypatch):
    """When ``defer_if_busy=True`` but the input is empty, the submit
    proceeds normally (Escape×2, type, Enter). Crucially: a client
    being attached to watch the pane is NOT itself a reason to defer
    — operators keep panes attached for monitoring."""
    calls = _capture_calls(monkeypatch)
    assert T.submit_to_tmux("sess", "hello", defer_if_busy=True) is True
    sendkeys = [c for c in calls if "send-keys" in c]
    assert sendkeys, "expected send-keys to fire on clean pane"
    # Content delivered via paste-buffer (not send-keys -l anymore).
    assert any("paste-buffer" in c for c in calls), (
        "expected paste-buffer content delivery"
    )


def test_submit_defer_if_busy_default_false_ignores_typing(monkeypatch):
    """Manual CLI paths use the default ``defer_if_busy=False``; these
    must NOT skip even when the pane shows pre-existing typing. The
    submit proceeds and clobbers the typed content (which is what
    the CLI user explicitly asked for).

    State machine:
      - Before Enter:  pane shows "> mid-typing" (human was typing,
                       our Escape×2 is about to clobber it).
      - After Enter:   pane shows bare "> " (our submit landed).
    """
    pane_state = {"typed": True}

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "send-keys" in argv and "C-m" in argv:
            # Our submit Enter cleared the input.
            pane_state["typed"] = False
        if "capture-pane" in argv:
            content = ["❯ mid-typing"] if pane_state["typed"] else ["❯ "]
            return _fake_cp(stdout=_pane(content))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    # Default — no defer. Must proceed despite visible typing at start.
    assert T.submit_to_tmux("sess", "hello") is True
    sendkeys = [c for c in calls if "send-keys" in c]
    assert sendkeys, "manual submit must not be gated on input-buffer"
    # Content delivered via paste-buffer.
    assert any("paste-buffer" in c for c in calls), (
        "expected paste-buffer content delivery"
    )


def test_submit_defer_coalesces_repeat_typing(monkeypatch, capsys):
    """Heartbeat fires every 5 minutes; if the operator is typing, every
    tick defers and used to emit an identical log line. Over a multi-hour
    typing session that's hundreds of identical lines drowning the
    heartbeat log. The defer log should fire once per session per
    continuous typing-period (state-transition logging), not once per
    deferred call."""
    T._deferring_sessions.discard("sess")

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ still-typing"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    for _ in range(5):
        assert T.submit_to_tmux(
            "sess", "tick", defer_if_busy=True
        ) is False

    err = capsys.readouterr().err
    defer_lines = [
        ln for ln in err.splitlines()
        if "defer: input has typing in sess" in ln
    ]
    assert len(defer_lines) == 1, (
        f"expected one defer log across 5 ticks, got {len(defer_lines)}"
    )


def test_submit_defer_logs_again_after_successful_submit(monkeypatch, capsys):
    """After the user stops typing and a heartbeat actually lands, the
    next typing-period must log defer again — operators rely on the
    line to spot when an inject got skipped. Successful submit clears
    the suppression flag."""
    T._deferring_sessions.discard("sess")
    state = {"typed": True}

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            content = ["❯ mid-typing"] if state["typed"] else ["❯ "]
            return _fake_cp(stdout=_pane(content))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    # Call 1: typing → defer + log.
    assert T.submit_to_tmux(
        "sess", "tick", defer_if_busy=True
    ) is False
    # Call 2: still typing → defer + SUPPRESSED (no new log).
    assert T.submit_to_tmux(
        "sess", "tick", defer_if_busy=True
    ) is False
    # User stops typing — successful submit clears the suppression flag.
    state["typed"] = False
    assert T.submit_to_tmux(
        "sess", "tick", defer_if_busy=True
    ) is True
    # User starts typing again — defer must log a second time.
    state["typed"] = True
    assert T.submit_to_tmux(
        "sess", "tick", defer_if_busy=True
    ) is False

    err = capsys.readouterr().err
    defer_lines = [
        ln for ln in err.splitlines()
        if "defer: input has typing in sess" in ln
    ]
    assert len(defer_lines) == 2, (
        f"expected two defer logs separated by a successful submit, "
        f"got {len(defer_lines)}: {defer_lines}"
    )


# --- Enter-race post-submit verification (2026-04-16 P0) -------------------
#
# Before this fix: submit_to_tmux returned True as long as no
# ``[Pasted text #`` placeholder was visible, ignoring whether the typed
# text actually got submitted. The Claude TUI sometimes ate the first
# Enter (post-Escape modal, autocomplete popup, paste-buffer commit
# race), leaving our text typed-but-unsubmitted while the function
# lied "True". Symptom: telegram inbound + wake prompts appeared in
# the input box but never became user-turns. Fix: retry Enter while
# EITHER paste-placeholder OR typed-text-in-input-box is still visible.


def test_submit_polls_until_input_clears_no_retry_c_m(monkeypatch):
    """Single C-m fires; submit_to_tmux polls capture-pane until the
    input box is clean. NO retry C-m fires while polling.

    Previous code retried C-m every 400ms (3x) on dirty input. That
    was the 2026-04-20 root cause: the TUI takes 3-5s to process a
    multi-line submit; the 400ms dirty-check was a false positive
    from render lag. Each retry C-m landed mid-process, leaving dirt
    that future wakes stacked on. The fix is to poll and wait — the
    single C-m always lands eventually, we just have to be patient.
    """
    poll_count = {"n": 0}

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            poll_count["n"] += 1
            # First two polls show dirty (TUI still rendering); third
            # onwards shows clean (submit landed).
            content = ["❯ PROBE"] if poll_count["n"] < 3 else ["❯ "]
            return _fake_cp(stdout=_pane(content))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    assert T.submit_to_tmux("sess", "PROBE") is True

    # EXACTLY TWO C-m fires — no retry spam.
    # 1. Pre-flush C-m (commits any legit pending content as a
    #    user-turn, no-op on clean input). Operator-confirmed 2026-04-20.
    # 2. Submit C-m (commits the payload we just typed).
    # The previous code fired 1-4 C-m: 1 submit + up to 3 retry-on-dirty.
    # Retries were the bug — they spammed C-m while TUI was still
    # processing the first submit.
    enter_calls = [c for c in calls
                   if "send-keys" in c and "C-m" in c and "-l" not in c]
    assert len(enter_calls) == 2, (
        f"expected exactly 2 C-m calls (pre-flush + submit), "
        f"got {len(enter_calls)}"
    )


def test_submit_waits_for_paste_to_land_before_firing_c_m(monkeypatch):
    """The submit C-m must not fire until the paste content is actually
    visible in the input box.

    Failure mode this guards against: ``paste-buffer`` is asynchronous
    from the TUI's perspective — content lands in the pane TTY input
    immediately, but Claude Code's Ink/React render pass can lag
    several seconds before the bracketed-paste event surfaces as
    visible content (inline or as ``[Pasted text #N]`` placeholder).
    If the submit C-m fires while the input box is still empty, it
    no-ops; the paste then arrives and sits in the input box
    forever, with no ``[Pasted text #`` marker for ``submit_watchdog``
    to recover from. Symptom: heartbeats start ``defer`` ing on the
    leftover typing signal and the agent never receives the message.

    Pin: before firing the submit C-m, ``submit_to_tmux`` polls
    ``capture-pane`` until paste-landed is observed (typing or
    placeholder). The submit C-m must come AFTER that observation.
    """
    pane_states = [
        # Pre-flight capture(s) before paste lands. The polling loop
        # checks `_has_pending_paste` (last-5-lines) and
        # `_input_line_has_typing` (inspects input box) — both must
        # report "clean" for a bare prompt. `_pane(["❯ "])` produces
        # a pane where neither signal fires.
        _pane(["❯ "]),
        _pane(["❯ "]),
        _pane(["❯ "]),
        # Paste lands — content now visible in the input box.
        _pane(["❯ payload"]),
        # Subsequent post-submit captures show clean (submit took).
        _pane(["❯ "]),
        _pane(["❯ "]),
    ]
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            stdout = pane_states.pop(0) if pane_states else _pane(["❯ "])
            return _fake_cp(stdout=stdout)
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    assert T.submit_to_tmux("sess", "payload", escape_prefix=False) is True

    # Locate paste-buffer + the two C-m fires (pre-flush + submit).
    paste_idx = next(i for i, c in enumerate(calls) if "paste-buffer" in c)
    c_m_indices = [
        i for i, c in enumerate(calls)
        if "send-keys" in c and "C-m" in c
    ]
    # The submit C-m is the C-m AFTER paste-buffer (the pre-flush one
    # is before).
    post_paste_c_m = [i for i in c_m_indices if i > paste_idx]
    assert post_paste_c_m, "expected a submit C-m after paste-buffer"
    submit_idx = post_paste_c_m[0]

    # Between paste-buffer and submit C-m, at least one capture-pane
    # must have observed the paste landing. (With the pre-fix behavior
    # — fixed 0.3s sleep then C-m — there would be ZERO capture-pane
    # calls between paste-buffer and C-m.)
    captures_between = [
        i for i, c in enumerate(calls)
        if "capture-pane" in c and paste_idx < i < submit_idx
    ]
    assert captures_between, (
        "submit C-m fired without verifying the paste landed in the "
        f"input box first. paste_idx={paste_idx}, submit_idx={submit_idx}, "
        f"calls in between: {calls[paste_idx + 1:submit_idx]}"
    )


def test_submit_returns_false_if_enter_never_lands(monkeypatch):
    """Simulate the worst case: Claude TUI eats every Enter (input box
    never clears). After 3 retries, the final check still sees typed
    content → submit_to_tmux MUST return False (not silently lie
    "True"). The fallback Escape was removed 2bf6845 — dirty state is
    left for submit_watchdog."""
    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ STUCK"]))
        return _fake_cp(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")
    assert T.submit_to_tmux("sess", "STUCK") is False


# --- Confirmed-submit: re-fire the eaten Enter (2026-08-11) -----------------
#
# Root cause of the operator's "my messages get stuck in the input, never
# submitted": the submit C-m fires while the TUI is still settling the
# bracketed-paste end-marker and gets EATEN. The payload lands INLINE
# (no ``[Pasted text #`` placeholder), so the marker-keyed watchdog —
# which only force-submits ``[wake]``/``[task]``/… payloads — never
# recovers an unmarked human message. Fix: when inline content sits
# byte-stable past a settle window, the submit was eaten → re-fire C-m.


def test_submit_confirmed_retry_recovers_eaten_enter_for_unmarked_human_msg(monkeypatch):
    """The repro: an UNMARKED human message (operator Telegram relay) whose
    submit C-m is eaten sits inline in the box. The confirmed-submit poll
    must re-fire C-m and land the message — no harness marker required,
    which is exactly the class the watchdog refuses to touch."""
    human = "hey can you look at the deploy when you get a sec"  # NO marker
    state = {"c_m": 0}

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "send-keys" in argv and "C-m" in argv:
            state["c_m"] += 1
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            # C-m #1 = pre-flush, #2 = submit (EATEN — box stays dirty with
            # the inline human text, no placeholder). Only the confirmed
            # re-fire (#3) actually submits and clears the box.
            content = ["❯ " + human] if state["c_m"] < 3 else ["❯ "]
            return _fake_cp(stdout=_pane(content))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    # Default escape_prefix=True — the real user-inbound telegram path.
    assert T.submit_to_tmux("sess", human) is True

    c_m_calls = [c for c in calls if "send-keys" in c and "C-m" in c]
    # pre-flush + eaten submit + at least one confirmed re-fire.
    assert len(c_m_calls) >= 3, (
        f"expected a confirmed-submit re-fire after the eaten Enter, "
        f"got {len(c_m_calls)} C-m calls: {c_m_calls}"
    )


def test_submit_confirmed_retry_skips_placeholder_large_payload(monkeypatch):
    """A ``[Pasted text #`` placeholder (large payload mid-commit) must NOT
    draw a confirmed re-fire C-m — that would reintroduce the 2026-04-20
    stacking regression. Only inline content is a candidate; a stuck
    placeholder is left to submit_watchdog. Exactly 2 C-m (pre-flush +
    submit), and the function returns False (dirty at timeout)."""
    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ [Pasted text #7 +90 lines]"]))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    assert T.submit_to_tmux("sess", "m", escape_prefix=False) is False
    c_m_calls = [c for c in calls if "send-keys" in c and "C-m" in c]
    assert len(c_m_calls) == 2, (
        f"placeholder must not draw a confirmed re-fire (pre-flush + submit "
        f"only), got {len(c_m_calls)}: {c_m_calls}"
    )


def test_submit_confirmed_retry_kill_switch_disables_refire(monkeypatch):
    """The fail-open kill-switch (METASPHERE_SUBMIT_RETRY_DISABLED) reverts
    to patient-poll-only: a stuck inline message draws NO re-fire — exactly
    2 C-m — and returns False. Lets an operator disable the new behavior
    without editing the live-loaded module."""
    monkeypatch.setenv("METASPHERE_SUBMIT_RETRY_DISABLED", "1")

    def fake_run(argv, **kw):
        if "has-session" in argv:
            return _fake_cp(returncode=0)
        if "capture-pane" in argv:
            return _fake_cp(stdout=_pane(["❯ STUCK inline no marker"]))
        return _fake_cp(returncode=0)

    calls: list[list[str]] = []

    def recording(argv, **kw):
        calls.append(list(argv))
        return fake_run(argv, **kw)

    monkeypatch.setattr("subprocess.run", recording)
    monkeypatch.setattr(T, "_find_tmux", lambda: "/usr/bin/tmux")

    assert T.submit_to_tmux("sess", "m", escape_prefix=False) is False
    c_m_calls = [c for c in calls if "send-keys" in c and "C-m" in c]
    assert len(c_m_calls) == 2, (
        f"kill-switch must suppress the confirmed re-fire (pre-flush + "
        f"submit only), got {len(c_m_calls)}: {c_m_calls}"
    )



def test_find_tmux_refuses_real_server_under_pytest(monkeypatch):
    """Regression — 2026-07-05: sandboxed message tests leaked real
    ``[wake] ...`` injections into live production panes because
    ``_find_tmux`` happily located the host tmux during a pytest run.
    Under pytest (``PYTEST_CURRENT_TEST`` set) discovery must return
    None so submit_to_tmux / submit_watchdog fall into their graceful
    no-tmux mode; ``METASPHERE_ALLOW_TMUX_IN_TESTS=1`` is the explicit
    opt-in for tests that really want a live server.
    """
    import os
    import shutil

    # pytest sets this for every test; the guard keys off it.
    assert os.environ.get("PYTEST_CURRENT_TEST")

    monkeypatch.delenv("METASPHERE_ALLOW_TMUX_IN_TESTS", raising=False)
    assert T._find_tmux() is None

    monkeypatch.setenv("METASPHERE_ALLOW_TMUX_IN_TESTS", "1")
    assert T._find_tmux() == shutil.which("tmux")


def test_residual_tail_round_trip_across_processes(tmp_path, monkeypatch):
    """The pre-flush guard must work when paste and pre-flush are different runs.

    Regression: 2026-09-15. `_is_residual_paste_tail` shipped inside the
    post-submit retry loop only, so the *other* route to the same symptom
    stayed open — the pre-flush `C-m` at the start of the NEXT submit,
    which assumes anything in the input box is legit pending content and
    submits it as its own user-turn. Three occurrences on 09-15 after the
    retry-side guard was already loaded, all tails of the heartbeat
    payload. Heartbeat injects are cron-spawned processes, so the hint has
    to survive on disk rather than in a module global.
    """
    import metasphere.tmux as t
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = "a heartbeat payload ending in auto-memory feedback delegate to agents"
    t._record_last_paste("metasphere-orchestrator", payload)

    # A later, separate read — stands in for the next process.
    remembered = t._read_last_paste("metasphere-orchestrator")
    assert remembered, "hint did not survive the write"

    assert t._is_residual_paste_tail(remembered, "delegate to agents")
    assert t._is_residual_paste_tail(remembered, " to agents")
    # The whole payload is a genuine eaten submit, not a leftover.
    assert not t._is_residual_paste_tail(remembered, payload)
    # Operator typing must never be discarded.
    assert not t._is_residual_paste_tail(remembered, "what is the status")


def test_read_last_paste_missing_is_empty_not_an_error(tmp_path, monkeypatch):
    """No hint on disk must degrade to the old behaviour, never raise.

    A missing file is the normal state on a fresh box or after the first
    ever submit, and it must leave the pre-flush doing exactly what it did
    before — an empty hint makes `_is_residual_paste_tail` return False,
    so the C-m path is preserved.
    """
    import metasphere.tmux as t
    monkeypatch.setenv("HOME", str(tmp_path))
    assert t._read_last_paste("never-pasted-here") == ""
    assert not t._is_residual_paste_tail("", "anything at all")


def test_hint_is_recorded_even_when_the_submit_never_confirms(
    tmp_path, monkeypatch
):
    """An unconfirmed submit still pasted, so it must still leave a hint.

    Regression: 2026-09-15, the same day the guard shipped. The hint was
    written only at the clean-poll `return True`, and a heartbeat injected
    into a *busy* agent structurally never gets there — Claude Code queues
    the paste behind the running turn, so the placeholder is still on
    screen when the 12s poll gives up and the function returns False.
    Delivery had succeeded; only the bookkeeping failed. Net effect: the
    one payload whose tail actually bleeds into the next turn was the one
    payload never recorded, so the pre-flush compared the leftover against
    a stale hint from hours earlier and let the `C-m` through. The guard
    looked installed and did nothing.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(T.time, "sleep", lambda *_a, **_k: None)
    _capture_calls(monkeypatch)
    # Never goes clean: the box keeps showing our placeholder.
    monkeypatch.setattr(T, "_has_pending_paste", lambda *a, **k: True)
    monkeypatch.setattr(
        T, "input_box_content", lambda *a, **k: "[Pasted text #7 +200 lines]"
    )

    payload = "# HEARTBEAT ... trailing words of the memory context block"
    assert T.submit_to_tmux("sess", payload, escape_prefix=False) is False

    remembered = T._read_last_paste("sess")
    assert remembered, "unconfirmed submit left no hint — guard is inert"
    assert T._is_residual_paste_tail(remembered, "memory context block")
