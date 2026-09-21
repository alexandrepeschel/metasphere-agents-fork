"""Tests for metasphere.gateway (session + watchdog + daemon)."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from metasphere.gateway import session as gw_session
from metasphere.gateway import watchdog as gw_watchdog
from metasphere.gateway import daemon as gw_daemon
from metasphere.paths import Paths


# ---------------------------------------------------------------------------
# session_health
# ---------------------------------------------------------------------------

def test_session_health_dead_when_no_session(tmp_paths: Paths):
    with patch.object(gw_session, "_agents_session_alive", return_value=False):
        alive, idle = gw_session.session_health(tmp_paths)
    assert alive is False
    assert idle == 0


def test_session_health_alive_with_idle(tmp_paths: Paths):
    disp = MagicMock(returncode=0, stdout="0\n", stderr="")
    with patch.object(gw_session, "_agents_session_alive", return_value=True), \
         patch.object(gw_session, "_tmux", return_value=disp):
        alive, idle = gw_session.session_health(tmp_paths)
    assert alive is True
    assert idle >= 0


def test_session_health_prefers_orchestrator_sidecar(tmp_paths: Paths):
    """When @orchestrator/last_active exists, idle is computed from it
    instead of tmux ``session_activity`` — the latter only advances on
    keystrokes and lies about a busy unattended REPL.
    """
    import datetime as _dt

    orch = tmp_paths.agents / "@orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=42)
    (orch / "last_active").write_text(
        stamp.isoformat().replace("+00:00", "Z") + "\n", encoding="utf-8"
    )
    # tmux mock returns a much-older activity timestamp; sidecar should win.
    disp = MagicMock(returncode=0, stdout="0\n", stderr="")
    with patch.object(gw_session, "_agents_session_alive", return_value=True), \
         patch.object(gw_session, "_tmux", return_value=disp) as mock_tmux:
        alive, idle = gw_session.session_health(tmp_paths)
    assert alive is True
    assert 40 <= idle <= 60  # ~42s, allow scheduler slack
    mock_tmux.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_session / start_session
# ---------------------------------------------------------------------------

def test_ensure_session_starts_when_dead(tmp_paths: Paths):
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        if args[:1] == ("has-session",):
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(gw_session, "_tmux", side_effect=fake_tmux), \
         patch.object(gw_session, "session_alive", return_value=False):
        gw_session.ensure_session(tmp_paths)

    cmds = [c[0] for c in calls]
    assert "new-session" in cmds


def test_ensure_session_noop_when_alive(tmp_paths: Paths):
    with patch.object(gw_session, "session_alive", return_value=True), \
         patch.object(gw_session, "_tmux") as m:
        gw_session.ensure_session(tmp_paths)
    assert m.call_count == 0


def test_start_session_builds_runtime_command_at_creation_time(
    tmp_paths: Paths, monkeypatch
):
    """Runtime selection must not be frozen when the module is imported."""
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args):
        calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("METASPHERE_ORCHESTRATOR_CODEX_MODEL", "future-model")
    monkeypatch.setenv(
        "METASPHERE_ORCHESTRATOR_CODEX_REASONING_EFFORT", "high"
    )
    with patch.object(gw_session, "session_alive", return_value=False), \
         patch.object(gw_session, "_tmux", side_effect=fake_tmux):
        assert gw_session.start_session(tmp_paths) is True

    launch = [
        call for call in calls
        if call[:3] == ("send-keys", "-t", gw_session.SESSION_NAME)
        and any("codex --no-alt-screen" in arg for arg in call)
    ]
    assert len(launch) == 1
    assert "--dangerously-bypass-hook-trust" in launch[0][3]
    assert "--model future-model" in launch[0][3]
    assert "model_reasoning_effort" in launch[0][3]
    assert "high" in launch[0][3]


def test_start_session_does_not_hardcode_codex_model(
    tmp_paths: Paths, monkeypatch
):
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args):
        calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("METASPHERE_ORCHESTRATOR_CODEX_MODEL", raising=False)
    monkeypatch.delenv(
        "METASPHERE_ORCHESTRATOR_CODEX_REASONING_EFFORT", raising=False
    )
    with patch.object(gw_session, "session_alive", return_value=False), \
         patch.object(gw_session, "_tmux", side_effect=fake_tmux):
        assert gw_session.start_session(tmp_paths) is True

    launch = next(
        call for call in calls
        if call[:3] == ("send-keys", "-t", gw_session.SESSION_NAME)
        and any("codex --no-alt-screen" in arg for arg in call)
    )
    assert "--model" not in launch[3]
    assert "model_reasoning_effort" not in launch[3]


def test_start_session_cleans_up_when_runtime_send_fails(
    tmp_paths: Paths, monkeypatch
):
    calls: list[tuple[str, ...]] = []

    def fake_tmux(*args):
        calls.append(args)
        failed = args[:3] == (
            "send-keys", "-t", gw_session.SESSION_NAME
        ) and any("codex --no-alt-screen" in arg for arg in args)
        return MagicMock(
            returncode=1 if failed else 0,
            stdout="",
            stderr="launch failed" if failed else "",
        )

    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: False)
    monkeypatch.setattr(gw_session, "_tmux", fake_tmux)

    assert gw_session.start_session(tmp_paths) is False
    assert ("kill-session", "-t", gw_session.SESSION_NAME) in calls
    assert not gw_session._restart_marker_path(tmp_paths).exists()


# ---------------------------------------------------------------------------
# Watchdog: stuck paste
# ---------------------------------------------------------------------------

def test_check_stuck_paste_detects_and_force_enters(tmp_paths: Paths):
    pane = "some output\n[Pasted text #3 +12 lines]\n> "
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "_send_keys") as send:
        # First tick: write timer baseline, no Enter yet
        first = gw_watchdog.check_stuck_paste(paths=tmp_paths, now=1000)
        assert first is False
        assert send.call_count == 0
        # Second tick: 16s later → force Enter
        second = gw_watchdog.check_stuck_paste(paths=tmp_paths, now=1016)
        assert second is True
        send.assert_called_once()
        assert "Enter" in send.call_args.args


def test_check_stuck_paste_clears_when_placeholder_gone(tmp_paths: Paths):
    state = gw_watchdog._session_state_file(
        tmp_paths, "stuck_paste_seen", gw_watchdog.SESSION_NAME
    )
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("999")
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value="clean pane"), \
         patch.object(gw_watchdog, "_send_keys") as send:
        result = gw_watchdog.check_stuck_paste(paths=tmp_paths, now=1000)
    assert result is False
    assert not state.exists()
    assert send.call_count == 0


def test_stuck_paste_timer_survives_sibling_session_tick(tmp_paths: Paths):
    """Regression: linger markers must be keyed per session.

    ``run_watchdog`` fans every check out over *all* live ``metasphere-*``
    sessions with the same ``paths``. When ``check_stuck_paste`` finds no
    placeholder it *unlinks* its marker. If that marker were global, a
    clean sibling session would wipe a genuinely-stuck session's ``T0``
    every ~5s tick, so ``now - first`` never reaches the 15s threshold and
    stuck-paste recovery is silently disabled whenever ≥2 sessions live.
    With per-session keying the stuck session's timer survives and fires.
    """
    stuck = "metasphere-orchestrator"
    clean = "metasphere-metasphere-agents-explorer"
    stuck_pane = "output\n[Pasted text #3 +12 lines]\n> "

    def pane_for(session):
        return stuck_pane if session == stuck else "clean pane"

    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", side_effect=lambda s: pane_for(s)), \
         patch.object(gw_watchdog, "_send_keys") as send:
        # Tick 0: stuck session records T0; clean sibling ticks right after.
        assert gw_watchdog.check_stuck_paste(stuck, tmp_paths, now=1000) is False
        assert gw_watchdog.check_stuck_paste(clean, tmp_paths, now=1000) is False
        # The clean sibling's unlink must NOT have wiped the stuck timer.
        stuck_marker = gw_watchdog._session_state_file(
            tmp_paths, "stuck_paste_seen", stuck
        )
        assert stuck_marker.exists()
        # Tick 1 (16s later): stuck session still stuck → force Enter fires.
        gw_watchdog.check_stuck_paste(clean, tmp_paths, now=1016)  # sibling first
        fired = gw_watchdog.check_stuck_paste(stuck, tmp_paths, now=1016)
        assert fired is True
        assert "Enter" in send.call_args.args


# ---------------------------------------------------------------------------
# Watchdog: safety hooks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pane", [
    "Some prompt\nDo you want to proceed?\n  1. Yes\n  2. No",
    "[plugin:safety-hooks] continue?\n  1. Yes\n  2. No",
    "Do you want to proceed?\nfoo\n  1. Yes\n",
])
def test_check_safety_hooks_confirmation_detects(tmp_paths: Paths, pane):
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "_send_keys") as send, \
         patch("time.sleep"):
        result = gw_watchdog.check_safety_hooks_confirmation(paths=tmp_paths, now=2000)
    assert result is True
    # Two sends: "1" then Enter
    assert send.call_count == 2


def test_check_safety_hooks_rate_limited(tmp_paths: Paths):
    pane = "Do you want to proceed?\n  1. Yes\n"
    marker = gw_watchdog._session_state_file(
        tmp_paths, "last_safety_hook_intervention", gw_watchdog.SESSION_NAME
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2000")
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "_send_keys") as send:
        # 5s later — under the 10s rate limit
        result = gw_watchdog.check_safety_hooks_confirmation(paths=tmp_paths, now=2005)
    assert result is False
    assert send.call_count == 0


def test_safety_hooks_ignores_prose_listing(tmp_paths: Paths):
    """A list-only pane without a confirm-class line must not trigger the
    watchdog."""
    pane = "Here are options for you:\n  1. Yes — go ahead\n  2. No — abort\n"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "_send_keys") as send:
        result = gw_watchdog.check_safety_hooks_confirmation(paths=tmp_paths, now=2000)
    assert result is False
    assert send.call_count == 0


# ---------------------------------------------------------------------------
# Watchdog: context limit auto-compact
# ---------------------------------------------------------------------------

def test_check_context_limit_detects_and_injects_compact(tmp_paths: Paths):
    """When the pane shows the full context-limit banner, ``/compact`` is
    submitted via the guarded submit path and the function returns True."""
    pane = (
        "\u23bf  Context limit reached \u00b7 /compact or /clear to continue\n"
        "\u276f "
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=5000)
    assert result is True
    # Exactly one guarded submit, with the raw /compact command and the
    # auto-injector flags (defer on typing, never interrupt a running turn).
    assert submit.call_count == 1
    args, kwargs = submit.call_args
    assert args[1] == "/compact", f"expected /compact payload, got {args!r}"
    assert kwargs.get("defer_if_busy") is True
    assert kwargs.get("escape_prefix") is False


def test_check_context_limit_uses_cm_submit_not_enter_keysym(tmp_paths: Paths):
    """Regression: the check must NOT use a bare tmux ``Enter`` keysym to
    submit — it routes through submit_to_tmux, which submits with a raw
    ``C-m`` byte (the Enter keysym no-ops in Claude Code's Ink TUI). We
    assert no raw _send_keys("Enter") leaks through."""
    pane = "Context limit reached \u00b7 /compact or /clear to continue\n\u276f "
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True), \
         patch.object(gw_watchdog, "_send_keys") as send:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=5000)
    assert result is True
    # No raw send-keys "Enter" — submission is delegated to submit_to_tmux.
    send.assert_not_called()


def test_check_context_limit_rate_limited(tmp_paths: Paths):
    """A second trigger within 10 minutes must be suppressed."""
    pane = "Context limit reached \u00b7 /compact or /clear to continue\n\u276f "
    marker = gw_watchdog._session_state_file(
        tmp_paths, "last_context_limit_compact", gw_watchdog.SESSION_NAME
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("5000")
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        # 60s later — well inside the 600s rate limit
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=5060)
    assert result is False
    submit.assert_not_called()


def test_check_context_limit_allowed_after_rate_limit_window(tmp_paths: Paths):
    """After 10+ minutes the check must fire again."""
    pane = "Context limit reached \u00b7 /compact or /clear to continue\n\u276f "
    marker = gw_watchdog._session_state_file(
        tmp_paths, "last_context_limit_compact", gw_watchdog.SESSION_NAME
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("5000")
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        # 601s later — just past the 600s window
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=5601)
    assert result is True
    assert submit.call_count == 1


def test_check_context_limit_no_banner_no_action(tmp_paths: Paths):
    """Clean pane must not trigger the check."""
    pane = "\u276f Tell me about the project.\n"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=9000)
    assert result is False
    submit.assert_not_called()


def test_check_context_limit_bare_phrase_without_tail_no_action(tmp_paths: Paths):
    """The bare phrase ``Context limit reached`` without the
    ``/compact or /clear`` tail (e.g. the agent typing the phrase in chat)
    must NOT fire — the match requires both correlated signals."""
    pane = (
        "Let me explain what happens when the Context limit reached state\n"
        "occurs in a long conversation.\n"
        "\u276f "
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=9000)
    assert result is False
    submit.assert_not_called()


def test_check_context_limit_stale_banner_in_scrollback_no_refire(tmp_paths: Paths):
    """A banner that has scrolled up out of the live tail region (e.g. an
    earlier context-limit that was already compacted) must NOT re-fire —
    the match is restricted to the last few lines of the pane."""
    # Banner at the top, then > _CONTEXT_LIMIT_TAIL_LINES lines of fresh
    # post-compact output pushing it out of the live region.
    filler = "\n".join(f"normal output line {n}" for n in range(20))
    pane = (
        "\u23bf  Context limit reached \u00b7 /compact or /clear to continue\n"
        + filler + "\n\u276f "
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=9000)
    assert result is False
    submit.assert_not_called()


def test_check_context_limit_deferred_when_busy_no_marker(tmp_paths: Paths):
    """If submit_to_tmux defers (human typing / busy pane) it returns
    False; the check must NOT write the rate-limit marker, so the next
    tick retries once the pane frees."""
    pane = "Context limit reached \u00b7 /compact or /clear to continue\n\u276f "
    marker = gw_watchdog._session_state_file(
        tmp_paths, "last_context_limit_compact", gw_watchdog.SESSION_NAME
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=False) as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=9000)
    assert result is False
    assert submit.call_count == 1
    assert not marker.exists(), "marker must NOT be written on a deferred submit"


def test_check_context_limit_skips_dead_session(tmp_paths: Paths):
    """Dead session must skip immediately without any tmux calls."""
    with patch.object(gw_watchdog, "session_alive", return_value=False), \
         patch.object(gw_watchdog, "_capture_pane") as cap, \
         patch.object(gw_watchdog, "submit_to_tmux") as submit:
        result = gw_watchdog.check_context_limit(paths=tmp_paths, now=9000)
    assert result is False
    cap.assert_not_called()
    submit.assert_not_called()


def test_check_context_limit_writes_marker(tmp_paths: Paths):
    """After a successful compact injection, the rate-limit marker must be
    written so the next tick within the window is suppressed."""
    pane = "Context limit reached \u00b7 /compact or /clear to continue\n\u276f "
    marker = gw_watchdog._session_state_file(
        tmp_paths, "last_context_limit_compact", gw_watchdog.SESSION_NAME
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True):
        gw_watchdog.check_context_limit(paths=tmp_paths, now=7000)
    assert marker.exists(), "rate-limit marker must be written after injection"
    assert marker.read_text().strip() == "7000"


# ---------------------------------------------------------------------------
# check_stuck_interactive_prompt (Option C backstop)
# ---------------------------------------------------------------------------

# Realistic widget tails built from the literal strings the installed Claude
# Code bundle renders (v2.1.x).
_MULTISELECT_PANE = (
    "Which features do you want to enable?\n"
    "  ◉ Auth\n  ◯ Billing\n  ◯ Search\n\n"
    "Space to toggle, Enter to confirm, a to select all, n to select none, "
    "i to invert, l to toggle\n"
)
_PLAN_PANE = (
    "Ready to code?\n\n"
    "  1. Yes\n  2. No, keep planning\n\n"
    "Would you like to proceed?\n"
)


def test_stuck_interactive_fires_after_linger_window(tmp_paths: Paths):
    """A multi-select widget that sits untouched past the threshold gets a
    single Escape + an async-channel nudge, and returns True."""
    state = gw_watchdog._session_state_file(
        tmp_paths, "stuck_interactive_seen", gw_watchdog.SESSION_NAME
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=_MULTISELECT_PANE), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True) as submit, \
         patch.object(gw_watchdog, "_send_keys") as send:
        # First sighting only records the timer — no action.
        first = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=1000)
        # Still inside the window — still no action.
        mid = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=1030)
        # Past the 45s threshold — fire.
        fired = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=1046)
    assert first is False
    assert mid is False
    assert fired is True
    # Exactly one Escape — a SINGLE press (never "Escape Escape" → Rewind).
    send.assert_called_once_with("metasphere-orchestrator", "Escape")
    # The nudge is injected without a second Escape and deferring on a busy pane.
    assert submit.call_count == 1
    _, kwargs = submit.call_args
    assert kwargs.get("escape_prefix") is False
    assert kwargs.get("defer_if_busy") is True
    # Timer cleared after firing so a re-rendered widget starts fresh.
    assert not state.exists()


def test_stuck_interactive_human_navigation_resets_timer(tmp_paths: Paths):
    """If the widget tail CHANGES between ticks (a human attached and is
    moving the selection), the linger timer restarts — we never Escape a
    menu out from under someone actively using it."""
    moved = _MULTISELECT_PANE.replace("◉ Auth", "◯ Auth").replace(
        "◯ Billing", "◉ Billing"
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "submit_to_tmux") as submit, \
         patch.object(gw_watchdog, "_send_keys") as send:
        with patch.object(gw_watchdog, "_capture_pane", return_value=_MULTISELECT_PANE):
            gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=1000)
        # 46s later the selection has moved → different fingerprint → reset.
        with patch.object(gw_watchdog, "_capture_pane", return_value=moved):
            r = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=1046)
    assert r is False
    send.assert_not_called()
    submit.assert_not_called()


def test_stuck_interactive_plan_mode_two_signals(tmp_paths: Paths):
    """Plan-approval widget (head + 'No, keep planning' option) fires."""
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=_PLAN_PANE), \
         patch.object(gw_watchdog, "submit_to_tmux", return_value=True), \
         patch.object(gw_watchdog, "_send_keys") as send:
        gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=2000)
        fired = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=2050)
    assert fired is True
    send.assert_called_once_with("metasphere-orchestrator", "Escape")


def test_stuck_interactive_ignores_prose_mentioning_proceed(tmp_paths: Paths):
    """A turn that merely echoes 'Would you like to proceed?' in prose — with
    no 'No, keep planning' option and no toggle footer — must NOT trip the
    check, even after the window elapses."""
    pane = (
        "I drafted the migration. Would you like to proceed? Let me know and "
        "I'll apply it.\n❯ "
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_capture_pane", return_value=pane), \
         patch.object(gw_watchdog, "submit_to_tmux") as submit, \
         patch.object(gw_watchdog, "_send_keys") as send:
        gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=3000)
        r = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=3100)
    assert r is False
    send.assert_not_called()
    submit.assert_not_called()


def test_stuck_interactive_clears_timer_when_widget_gone(tmp_paths: Paths):
    """Once the widget disappears (answered / cancelled), the linger-state
    file is removed so a later widget starts a fresh timer."""
    state = gw_watchdog._session_state_file(
        tmp_paths, "stuck_interactive_seen", gw_watchdog.SESSION_NAME
    )
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_send_keys"):
        with patch.object(gw_watchdog, "_capture_pane", return_value=_MULTISELECT_PANE):
            gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=4000)
        assert state.exists()
        with patch.object(gw_watchdog, "_capture_pane", return_value="clean pane\n❯ "):
            r = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=4010)
    assert r is False
    assert not state.exists()


def test_stuck_interactive_skips_dead_session(tmp_paths: Paths):
    """Dead session must skip immediately without any tmux calls."""
    with patch.object(gw_watchdog, "session_alive", return_value=False), \
         patch.object(gw_watchdog, "_capture_pane") as cap, \
         patch.object(gw_watchdog, "_send_keys") as send:
        r = gw_watchdog.check_stuck_interactive_prompt(paths=tmp_paths, now=5000)
    assert r is False
    cap.assert_not_called()
    send.assert_not_called()


# ---------------------------------------------------------------------------
# check_stuck_input (inline auto-inject recovery)
# ---------------------------------------------------------------------------

_SESS = gw_watchdog.SESSION_NAME


def test_stuck_input_fires_after_linger_window(tmp_paths: Paths):
    """A wake payload stuck inline in the box past the threshold draws a
    single recovery C-m and returns True."""
    state = gw_watchdog._session_state_file(tmp_paths, "stuck_input_seen", _SESS)
    content = "[wake] new task from @orchestrator: do the thing"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "input_box_content", return_value=content), \
         patch.object(gw_watchdog, "_send_keys") as send:
        first = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)
        mid = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1020)
        fired = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1031)
    assert first is False
    assert mid is False
    assert fired is True
    # Recovery is a raw C-m (not the unreliable Enter keysym).
    send.assert_called_once_with(_SESS, "C-m")
    # Linger timer cleared after firing.
    assert not state.exists()


def test_stuck_input_ignores_human_typing(tmp_paths: Paths):
    """Content without a harness marker is a human's message — never touched,
    and the linger timer is cleared so a later inject starts fresh."""
    human = "hey can you look at the deploy when you get a sec"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "input_box_content", return_value=human), \
         patch.object(gw_watchdog, "_send_keys") as send:
        a = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)
        b = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1031)
    assert a is False and b is False
    send.assert_not_called()


def test_stuck_input_ignores_placeholder(tmp_paths: Paths):
    """A ``[Pasted text #`` placeholder is check_stuck_paste's job — even if a
    marker precedes it, the placeholder path must not double-fire here."""
    content = "[task] see below [Pasted text #4 +30 lines]"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "input_box_content", return_value=content), \
         patch.object(gw_watchdog, "_send_keys") as send:
        a = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)
        b = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1031)
    assert a is False and b is False
    send.assert_not_called()


def test_stuck_input_changed_content_resets_timer(tmp_paths: Paths):
    """If the box content changes between ticks (turn processing / a fresh
    inject), the linger timer restarts — we don't fire on transient content."""
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_send_keys") as send:
        with patch.object(gw_watchdog, "input_box_content",
                          return_value="[wake] first"):
            gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)
        with patch.object(gw_watchdog, "input_box_content",
                          return_value="[wake] second"):
            r = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1031)
    assert r is False
    send.assert_not_called()


def test_stuck_input_clears_timer_when_box_empties(tmp_paths: Paths):
    """Once the box drains (submit landed), the linger timer is removed so a
    fresh episode must accrue the full threshold again."""
    state = gw_watchdog._session_state_file(tmp_paths, "stuck_input_seen", _SESS)
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "_send_keys"):
        with patch.object(gw_watchdog, "input_box_content", return_value="[task] x"):
            gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)
        assert state.exists()
        with patch.object(gw_watchdog, "input_box_content", return_value=None):
            r = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1010)
    assert r is False
    assert not state.exists()


def test_stuck_input_rate_limited_after_fire(tmp_paths: Paths):
    """After a recovery C-m, a still-stuck payload (queued behind a long turn)
    doesn't draw another C-m until the rate-limit window elapses."""
    content = "[heartbeat] agent x idle"
    with patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch.object(gw_watchdog, "input_box_content", return_value=content), \
         patch.object(gw_watchdog, "_send_keys") as send:
        gw_watchdog.check_stuck_input(paths=tmp_paths, now=1000)   # arm
        assert gw_watchdog.check_stuck_input(paths=tmp_paths, now=1031) is True
        # Still stuck 30s later, but inside the 90s rate-limit → no re-fire.
        assert gw_watchdog.check_stuck_input(paths=tmp_paths, now=1062) is False
        assert gw_watchdog.check_stuck_input(paths=tmp_paths, now=1093) is False
        # Past the rate-limit window and still stuck+stable → fire again.
        gw_watchdog.check_stuck_input(paths=tmp_paths, now=1122)   # re-arm timer
        again = gw_watchdog.check_stuck_input(paths=tmp_paths, now=1153)
    assert again is True
    assert send.call_count == 2


def test_stuck_input_skips_dead_session(tmp_paths: Paths):
    """Dead session skips immediately without inspecting the pane."""
    with patch.object(gw_watchdog, "session_alive", return_value=False), \
         patch.object(gw_watchdog, "input_box_content") as content, \
         patch.object(gw_watchdog, "_send_keys") as send:
        r = gw_watchdog.check_stuck_input(paths=tmp_paths, now=5000)
    assert r is False
    content.assert_not_called()
    send.assert_not_called()


def test_check_restart_marker_uses_project_scoped_session(tmp_paths: Paths):
    """Regression: post-restart wake-up injection for a project-scoped
    agent must target ``metasphere-<project>-<agent>``, not the bare
    ``metasphere-<agent>`` form. With the bare form, ``session_alive``
    returns False (real session is project-prefixed) and the wake-msg
    is silently dropped — agent loses the ``[session restarted]``
    phrasing telling it why it just respawned. Sister-fix to the
    posthook deferred-cmd resolution bug.
    """
    import json as _json
    import time as _time
    from metasphere.agents import AgentRecord

    rec = AgentRecord(
        name="@brand-mentions",
        scope="",
        parent="",
        status="",
        spawned_at="",
        project="research",
    )

    # Marker is grace-period-aged (10s old, > 8s grace, < 120s stale).
    now = 1_000_000
    marker = tmp_paths.state / "restart_pending_brand_mentions.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_json.dumps({
        "timestamp": now - 10,
        "reason": "test",
        "agent": "@brand-mentions",
    }), encoding="utf-8")

    captured: dict[str, str] = {}

    def fake_submit(_kind, _msg, *, session, **_kwargs):
        captured["session"] = session
        return True

    with patch("metasphere.session.list_agents", return_value=[rec]), \
         patch.object(gw_watchdog, "session_alive", return_value=True), \
         patch("metasphere.telegram.inject.submit_to_tmux", fake_submit):
        result = gw_watchdog._check_restart_marker(marker, tmp_paths, now=now)

    assert result is True
    assert captured.get("session") == "metasphere-research-brand-mentions", (
        f"expected project-aware session name, "
        f"got {captured.get('session')!r}"
    )


def test_restart_marker_survives_codex_selector_then_retries(
    tmp_paths: Paths, monkeypatch
):
    """Selector blocking and restart retry must compose without losing wake."""
    import json as _json
    from metasphere import tmux as tmux_module

    now = 1_000_000
    marker = gw_session._restart_marker_path(tmp_paths)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_json.dumps({
        "timestamp": now - 10,
        "reason": "test restart",
        "agent": "@orchestrator",
    }), encoding="utf-8")

    selector_visible = iter((True, False))
    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr(gw_watchdog, "session_alive", lambda name=None: True)
    monkeypatch.setattr(tmux_module, "_find_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(tmux_module, "_has_session", lambda *_args: True)
    monkeypatch.setattr(
        tmux_module,
        "_codex_startup_selector_in_pane",
        lambda *_args: next(selector_visible),
    )
    monkeypatch.setattr(tmux_module, "_has_pending_paste", lambda *_args: False)
    monkeypatch.setattr(tmux_module, "_input_line_has_typing", lambda *_args: False)
    monkeypatch.setattr(tmux_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tmux_module.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(returncode=0, stdout="", stderr=""),
    )

    assert gw_watchdog._check_restart_marker(
        marker, tmp_paths, now=now
    ) is False
    retained = _json.loads(marker.read_text(encoding="utf-8"))
    assert retained["timestamp"] == now
    assert retained["attempts"] == 1

    assert gw_watchdog._check_restart_marker(
        marker, tmp_paths, now=now + gw_watchdog._RESTART_GRACE_S
    ) is True
    assert not marker.exists()


def test_restart_marker_throttles_deferred_events_but_logs_recovery(
    tmp_paths: Paths, monkeypatch
):
    """Durable retries stay frequent while their event trail stays useful."""
    import json as _json

    now = 1_000_000
    marker = gw_session._restart_marker_path(tmp_paths)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_json.dumps({
        "timestamp": now - gw_watchdog._RESTART_GRACE_S,
        "reason": "test restart",
        "agent": "@orchestrator",
    }), encoding="utf-8")

    submit = MagicMock(side_effect=[False] * 11 + [True])
    events: list[str] = []
    monkeypatch.setattr(gw_watchdog, "session_alive", lambda name=None: True)
    monkeypatch.setattr("metasphere.telegram.inject.submit_to_tmux", submit)
    monkeypatch.setattr(
        gw_watchdog,
        "log_event",
        lambda _kind, message, **_kwargs: events.append(message),
    )

    for attempt in range(1, 12):
        current = now + (attempt - 1) * gw_watchdog._RESTART_GRACE_S
        assert gw_watchdog._check_restart_marker(
            marker, tmp_paths, now=current
        ) is False

    retained = _json.loads(marker.read_text(encoding="utf-8"))
    assert retained["attempts"] == 11
    assert len(events) == 2
    assert "attempt 1" in events[0]
    assert "attempt 10" in events[1]

    recovered_at = now + 11 * gw_watchdog._RESTART_GRACE_S
    assert gw_watchdog._check_restart_marker(
        marker, tmp_paths, now=recovered_at
    ) is True
    assert not marker.exists()
    assert len(events) == 3
    assert "after 11 deferrals" in events[-1]
    assert submit.call_count == 12


def test_pending_inbound_retries_until_confirmed(tmp_paths: Paths, monkeypatch):
    from metasphere.gateway import pending as gw_pending

    marker = gw_pending.enqueue_inbound(
        delivery_id="telegram:42:@orchestrator",
        from_user="@operator",
        text="do not lose this",
        session=gw_session.SESSION_NAME,
        target_agent_id="@orchestrator",
        chat_id=42,
        paths=tmp_paths,
    )
    submit = MagicMock(side_effect=[False, True])
    monkeypatch.setattr("metasphere.telegram.inject.submit_to_tmux", submit)

    assert gw_pending.retry_pending_inbound(tmp_paths) == 0
    assert marker.exists()
    assert gw_pending.retry_pending_inbound(tmp_paths) == 1
    assert not marker.exists()
    assert submit.call_count == 2
    assert submit.call_args.kwargs["escape_prefix"] is False


def test_pending_inbound_preserves_fifo_behind_blocked_head(
    tmp_paths: Paths, monkeypatch
):
    from metasphere.gateway import pending as gw_pending

    for update_id, text in ((5100, "FIRST"), (5101, "SECOND")):
        gw_pending.enqueue_inbound(
            delivery_id=f"telegram:{update_id}:@orchestrator",
            from_user="@operator",
            text=text,
            session=gw_session.SESSION_NAME,
            target_agent_id="@orchestrator",
            chat_id=42,
            paths=tmp_paths,
        )

    attempts: list[str] = []

    def blocked_submit(_from, text, **_kwargs):
        attempts.append(text)
        return False

    monkeypatch.setattr(
        "metasphere.telegram.inject.submit_to_tmux", blocked_submit
    )
    assert gw_pending.retry_pending_inbound(tmp_paths) == 0
    assert attempts == ["FIRST"]

    monkeypatch.setattr(
        "metasphere.telegram.inject.submit_to_tmux",
        lambda _from, text, **_kwargs: attempts.append(text) or True,
    )
    assert gw_pending.retry_pending_inbound(tmp_paths) == 2
    assert attempts == ["FIRST", "FIRST", "SECOND"]


def test_pending_inbound_cross_chat_envelopes_keep_explicit_destinations(
    tmp_paths: Paths, monkeypatch
):
    from metasphere.gateway import pending as gw_pending
    from metasphere.telegram import inject as tg_inject

    destinations = (
        (5100, "FIRST", "telegram-alpha", 111, 7),
        (5101, "SECOND", "telegram-beta", 222, 9),
    )
    for update_id, text, surface, chat_id, thread_id in destinations:
        reply = (
            f"metasphere message send --surface {surface} "
            f"--chat-id {chat_id} --thread-id {thread_id} \"<reply>\""
        )
        gw_pending.enqueue_inbound(
            delivery_id=f"{surface}:{update_id}:@orchestrator",
            from_user="@operator",
            text=text,
            session="stale-session-name",
            target_agent_id="@orchestrator",
            chat_id=chat_id,
            surface_id=surface,
            thread_id=thread_id,
            reply_command=reply,
            paths=tmp_paths,
        )

    payloads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "metasphere.session._resolve_session",
        lambda agent: "current-orchestrator-session",
    )
    monkeypatch.setattr(
        tg_inject,
        "_tmux_submit",
        lambda session, payload, **_kwargs: (
            payloads.append((session, payload)) or True
        ),
    )

    assert gw_pending.retry_pending_inbound(tmp_paths) == 2
    assert [payload for _session, payload in payloads] == [
        "[telegram from operator | reply: metasphere message send "
        "--surface telegram-alpha --chat-id 111 --thread-id 7 \"<reply>\"] FIRST",
        "[telegram from operator | reply: metasphere message send "
        "--surface telegram-beta --chat-id 222 --thread-id 9 \"<reply>\"] SECOND",
    ]
    assert {session for session, _payload in payloads} == {
        "current-orchestrator-session"
    }


def test_pending_inbound_delivers_legacy_schema_v1_record(
    tmp_paths: Paths, monkeypatch
):
    """A rolling upgrade must not delete the preceding queue schema."""
    import json as _json
    from metasphere.gateway import pending as gw_pending

    queue = tmp_paths.state / "pending_inbound"
    queue.mkdir(parents=True, exist_ok=True)
    marker = queue / ("a" * 64 + ".json")
    marker.write_text(_json.dumps({
        "delivery_id": "telegram:5000:@orchestrator",
        "from_user": "@operator",
        "text": "LEGACY",
        "session": "stored-legacy-session",
        "surface_id": "telegram",
        "reply_command": None,
    }), encoding="utf-8")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "metasphere.telegram.inject.submit_to_tmux",
        lambda _from, text, *, session, **_kwargs: (
            calls.append((session, text)) or True
        ),
    )

    assert gw_pending.retry_pending_inbound(tmp_paths) == 1
    assert calls == [("stored-legacy-session", "LEGACY")]
    assert not marker.exists()


def test_pending_inbound_serializes_concurrent_retry_workers(
    tmp_paths: Paths, monkeypatch
):
    import threading
    import time
    from metasphere.gateway import pending as gw_pending

    gw_pending.enqueue_inbound(
        delivery_id="telegram:6000:@orchestrator",
        from_user="@operator",
        text="ONCE",
        session=gw_session.SESSION_NAME,
        target_agent_id="@orchestrator",
        chat_id=42,
        paths=tmp_paths,
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_submit(_from, text, **_kwargs):
        calls.append(text)
        entered.set()
        assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(
        "metasphere.telegram.inject.submit_to_tmux", slow_submit
    )
    results: list[int] = []
    workers = [
        threading.Thread(
            target=lambda: results.append(
                gw_pending.retry_pending_inbound(tmp_paths)
            )
        )
        for _ in range(2)
    ]
    workers[0].start()
    assert entered.wait(timeout=5)
    workers[1].start()
    time.sleep(0.1)
    assert calls == ["ONCE"]
    release.set()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert calls == ["ONCE"]
    assert sorted(results) == [0, 1]


# ---------------------------------------------------------------------------
# Daemon: must NOT exit on a single iteration error
# ---------------------------------------------------------------------------

def test_run_daemon_continues_on_poll_error(tmp_paths: Paths):
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    def bad_poll():
        raise RuntimeError("simulated transient telegram failure")

    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog") as wd:
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.0,
            stop=stop,
            poll_fn=bad_poll,
            sleep_fn=fake_sleep,
            time_fn=lambda: 0.0,
        )

    # Daemon completed all 3 iterations without exiting on poll error
    assert iterations["n"] == 4
    assert len(sleeps) == 3
    # Watchdog ran each iteration (interval=0)
    assert wd.call_count >= 1


def test_run_daemon_honors_watchdog_interval(tmp_paths: Paths):
    iterations = {"n": 0}
    times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 10.0, 11.0])

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 6

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog") as wd:
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=5.0,
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: next(times),
        )

    # First call (t=0) and the call after t crossed 5s (t=10) — at most 2 invocations
    assert wd.call_count == 2


def test_run_daemon_reensures_session_each_watchdog_tick(tmp_paths: Paths):
    """Dead-session backstop: the daemon must call ``ensure_session`` on every
    watchdog tick, not only once at boot. Regression guard for the 2026-07-03
    'gateway-up, agent-dead' silent outage — a session whose boot-time rebuild
    failed (or that died later) stayed dead ~3h because nothing re-ensured it.
    Invariant: ensure_session runs once at boot, then once per watchdog tick
    (1:1 with run_watchdog)."""
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    with patch.object(gw_daemon, "ensure_session") as ens, \
         patch.object(gw_daemon, "run_watchdog") as wd:
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.0,  # fire the tick every iteration
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
        )

    # Re-ensured on ticks, not just at boot.
    assert ens.call_count >= 2
    # One boot call + one per tick, paired 1:1 with the watchdog.
    assert ens.call_count == wd.call_count + 1


def test_run_daemon_reap_dormant_fires_on_interval(tmp_paths: Paths):
    """Daemon must invoke ``reap_dormant`` on the dormancy cadence and
    pass through ``max_idle_seconds``. Longer-interval tick than the
    watchdog: first tick at t=0, next after crossing the interval."""
    iterations = {"n": 0}
    # Times: 0, 1, 2 (under interval), 400 (crosses 300s interval), 401, 402
    times = iter([0.0, 1.0, 2.0, 400.0, 401.0, 402.0, 403.0])

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 6

    reap_calls: list[tuple] = []

    def fake_reap(p, max_idle):
        reap_calls.append((p, max_idle))
        return []

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.01,
            dormancy_interval=300.0,
            dormancy_max_idle_seconds=86400,
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: next(times),
            reap_dormant_fn=fake_reap,
        )

    # Two dormancy ticks expected: first at t=0 (last=-inf), second at t=400.
    assert len(reap_calls) == 2, f"expected 2 reap ticks, got {len(reap_calls)}"
    assert all(mi == 86400 for _, mi in reap_calls)


def test_run_daemon_continues_on_reap_dormant_error(tmp_paths: Paths):
    """A failure inside reap_dormant must not exit the gateway loop."""
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    def boom(p, max_idle):
        raise RuntimeError("simulated tmux failure in reap_dormant")

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.01,
            dormancy_interval=0.0,  # fire on every tick
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
            reap_dormant_fn=boom,
            reap_crashed_fn=lambda p: [],
        )

    # If we got here without an unhandled exception, the loop survived.
    assert iterations["n"] == 4


def test_run_daemon_reap_crashed_fires_on_dormancy_interval(tmp_paths: Paths):
    """``reap_crashed`` must be wired into the same dormancy tick as
    ``reap_dormant``: same cadence, same loop step, paths passed through."""
    iterations = {"n": 0}
    times = iter([0.0, 1.0, 2.0, 400.0, 401.0, 402.0, 403.0])

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 6

    crash_calls: list = []
    dorm_calls: list = []

    def fake_reap_dormant(p, max_idle):
        dorm_calls.append((p, max_idle))
        return []

    def fake_reap_crashed(p):
        crash_calls.append(p)
        return []

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.01,
            dormancy_interval=300.0,
            dormancy_max_idle_seconds=86400,
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: next(times),
            reap_dormant_fn=fake_reap_dormant,
            reap_crashed_fn=fake_reap_crashed,
        )

    # reap_crashed must fire on the same ticks as reap_dormant.
    assert len(crash_calls) == len(dorm_calls) == 2, (
        f"expected 2 each, got crash={len(crash_calls)} "
        f"dorm={len(dorm_calls)}"
    )
    # Paths handed through unchanged.
    for p in crash_calls:
        assert p is tmp_paths


def test_run_daemon_continues_on_reap_crashed_error(tmp_paths: Paths):
    """A failure inside reap_crashed must NOT exit the daemon AND must
    NOT prevent reap_dormant from firing on the same tick."""
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    dorm_calls: list = []

    def fake_reap_dormant(p, max_idle):
        dorm_calls.append(max_idle)
        return []

    def boom_crashed(p):
        raise RuntimeError("simulated reap_crashed failure")

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.01,
            watchdog_interval=0.01,
            dormancy_interval=0.0,  # fire on every tick
            stop=stop,
            poll_fn=lambda: 0,
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
            reap_dormant_fn=fake_reap_dormant,
            reap_crashed_fn=boom_crashed,
        )

    # Loop survived all stop() probes.
    assert iterations["n"] == 4
    # And reap_dormant kept firing despite the sibling sweep blowing up.
    assert len(dorm_calls) >= 3


# ---------------------------------------------------------------------------
# _poll_once — end-to-end attachment routing through the shared handler
#
# Regression for 2026-04-14T21:21Z: gateway/daemon.py had its own
# per-update loop that filtered on ``if u.text and u.chat_id is not
# None``, so photos (text-less with caption) were silently dropped.
# The shared ``telegram.handler.handle_update`` now owns the full flow;
# these tests prove _poll_once actually exercises it end-to-end.
# ---------------------------------------------------------------------------

import json as _json
from metasphere.telegram import api as _tg_api, attachments as _atts, inject as _tg_inject, poller as _tg_poller


def test_poll_once_routes_photo_through_shared_handler(tmp_path, monkeypatch):
    """A photo update arrives via getUpdates; _poll_once must:
    1. Parse the photo attachment (was dropped by the old filter).
    2. Download it through getFile + http.
    3. Inject the rendered [attachments] block into tmux.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    # Sandbox real paths the handler touches.
    monkeypatch.setattr(_atts, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    monkeypatch.setattr(_atts, "DEBUG_LOG_PATH", tmp_path / "debug.log")

    # Fake getUpdates → one photo payload.
    photo_update = {
        "update_id": 9000,
        "message": {
            "message_id": 777,
            "chat": {"id": 42, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "caption": "debug-photo-1",
            "photo": [{"file_id": "p1", "file_size": 50}],
        },
    }
    monkeypatch.setattr(
        _tg_poller, "get_updates",
        lambda offset=0, timeout=30, surface_id=None: [_tg_poller.Update.from_payload(photo_update)],
    )
    monkeypatch.setattr(_tg_poller, "load_offset", lambda path=None: 0)
    monkeypatch.setattr(_tg_poller, "save_offset", lambda offset, path=None: None)

    # Stub the api + http layers.
    http_calls: list = []

    def fake_http_get(url, timeout):
        http_calls.append(url)
        return b"BYTES:photo"

    monkeypatch.setattr(_atts, "_http_get_default", fake_http_get)

    def fake_api_call(method, **params):
        if method == "getFile":
            return {"ok": True, "result": {"file_path": f"photos/{params['file_id']}.jpg"}}
        if method == "setMessageReaction":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected api.call: {method}")

    monkeypatch.setattr(_tg_api, "call", fake_api_call)

    # Capture tmux injection.
    tmux_calls: list = []
    monkeypatch.setattr(
        _tg_inject, "submit_to_tmux",
        lambda from_user, text, session="metasphere-orchestrator", **kw:
            tmux_calls.append({"from": from_user, "text": text}) or True,
    )

    # Redirect archiver and pending-ack writes off the real home dir.
    from metasphere.telegram import archiver as _arch
    from metasphere.telegram import handler as _handler
    monkeypatch.setattr(_arch, "DEFAULT_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(_handler, "_default_save_chat_id", lambda cid: None)
    monkeypatch.setattr(_handler, "_default_pending_ack_writer", lambda cid, mid: None)

    processed = gw_daemon._poll_once(timeout=1)

    assert processed == 1
    # Photo was downloaded through the shared handler (old gateway loop
    # would never have called getFile).
    assert len(http_calls) == 1
    assert "p1" in http_calls[0]
    # Exactly one tmux submit — with caption + [attachments] block.
    assert len(tmux_calls) == 1
    payload = tmux_calls[0]["text"]
    assert payload.startswith("debug-photo-1")
    assert "[attachments]" in payload
    assert str(tmp_path / "attachments" / "777") in payload


def test_poll_once_does_not_drop_photo_only_messages(tmp_path, monkeypatch):
    """Regression for the exact production bug: a photo with no text
    and no caption used to be silently skipped by the old
    ``if u.text and u.chat_id is not None`` filter. The shared handler
    must inject a pure [attachments] block instead.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setattr(_atts, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    monkeypatch.setattr(_atts, "DEBUG_LOG_PATH", tmp_path / "debug.log")

    bare_photo = {
        "update_id": 9001,
        "message": {
            "message_id": 778,
            "chat": {"id": 42, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "photo": [{"file_id": "p2", "file_size": 50}],
        },
    }
    monkeypatch.setattr(
        _tg_poller, "get_updates",
        lambda offset=0, timeout=30, surface_id=None: [_tg_poller.Update.from_payload(bare_photo)],
    )
    monkeypatch.setattr(_tg_poller, "load_offset", lambda path=None: 0)
    monkeypatch.setattr(_tg_poller, "save_offset", lambda offset, path=None: None)

    monkeypatch.setattr(_atts, "_http_get_default", lambda url, t: b"BYTES:photo")

    def fake_call(method, **params):
        if method == "getFile":
            return {"ok": True, "result": {"file_path": "photos/p2.jpg"}}
        if method == "setMessageReaction":
            return {"ok": True, "result": True}
        raise AssertionError(method)

    monkeypatch.setattr(_tg_api, "call", fake_call)

    tmux_calls: list = []
    monkeypatch.setattr(
        _tg_inject, "submit_to_tmux",
        lambda fu, t, session="metasphere-orchestrator", **kw:
            tmux_calls.append({"from": fu, "text": t}) or True,
    )

    from metasphere.telegram import archiver as _arch
    from metasphere.telegram import handler as _handler
    monkeypatch.setattr(_arch, "DEFAULT_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(_handler, "_default_save_chat_id", lambda cid: None)
    monkeypatch.setattr(_handler, "_default_pending_ack_writer", lambda cid, mid: None)

    gw_daemon._poll_once(timeout=1)

    assert len(tmux_calls) == 1
    payload = tmux_calls[0]["text"]
    assert payload.startswith("[attachments]")
    assert "photo" in payload


def test_poll_once_handler_exception_does_not_block_offset_advance(tmp_path, monkeypatch):
    """If handle_update raises for one update, _poll_once must still
    advance the offset for that update (so we don't re-process it
    forever) and return a valid count.
    """
    fake_updates = [
        _tg_poller.Update.from_payload({
            "update_id": 9002,
            "message": {
                "message_id": 800,
                "chat": {"id": 1, "is_forum": False},
                "from": {"username": "x"},
                "text": "hi",
            },
        }),
    ]
    monkeypatch.setattr(_tg_poller, "get_updates",
                         lambda offset=0, timeout=30, surface_id=None: fake_updates)
    monkeypatch.setattr(_tg_poller, "load_offset", lambda path=None: 0)

    saved_offsets: list = []
    monkeypatch.setattr(_tg_poller, "save_offset",
                         lambda o, path=None: saved_offsets.append(o))

    from metasphere.telegram import handler as _handler
    def boom(u, **_k):
        raise RuntimeError("simulated handler failure")
    monkeypatch.setattr(_handler, "handle_update", boom)

    processed = gw_daemon._poll_once(timeout=1)

    assert processed == 1
    # Offset advanced past the failing update so we don't re-drive it.
    assert saved_offsets == [9003]


# ---------------------------------------------------------------------------
# restart_agent_session — /exit Enter-race fix (PR #18)
#
# 2026-04-16: supervisor.restart_claude fired but the tmux pane never
# cycled — the single ``send-keys /exit Enter`` invocation races the
# REPL's input-state machine post-C-c. Prior art in ``metasphere.tmux
# .submit_to_tmux`` separates the literal-text send from the Enter,
# with settles between. These tests pin the fixed sequence so a
# future refactor can't regress it.
# ---------------------------------------------------------------------------


def test_restart_claude_uses_separated_exit_and_enter_sequence(tmp_paths, monkeypatch):
    """The fix: ``/exit`` is sent via ``send-keys -l --`` (literal) as
    its own call, then Enter is a separate send-keys. Double Enter as
    belt-and-suspenders. No single ``/exit Enter`` invocation.
    """
    calls: list[list[str]] = []

    def fake_tmux(*args: str):
        calls.append(list(args))
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = ""
        cp.stderr = ""
        return cp

    monkeypatch.setattr(gw_session, "_tmux", fake_tmux)
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: True)
    # _write_restart_pending writes under paths.state; tmp_paths
    # already redirects that.
    ok = gw_session.restart_agent_session(
        agent="@orchestrator", reason="test", paths=tmp_paths,
    )
    assert ok is True

    # Sequence matches the post-fix shape:
    #   C-c, C-c, C-u, send-keys -l -- /exit, Enter, Enter
    sendkeys = [c for c in calls if c and c[0] == "send-keys"]
    # 5+ send-keys calls (2x C-c, 1x C-u, 1x /exit, 2x Enter).
    assert len(sendkeys) >= 6

    # No single call that contains BOTH "/exit" AND "Enter" — that
    # combined form is exactly the race we're fixing.
    for call in sendkeys:
        if "/exit" in call:
            assert "Enter" not in call, (
                f"/exit must not share a send-keys call with Enter: {call}"
            )

    # /exit sent via literal mode.
    exit_calls = [c for c in sendkeys if "/exit" in c]
    assert len(exit_calls) == 1
    assert "-l" in exit_calls[0], f"/exit must use -l: {exit_calls[0]}"

    # At least two separate Enter sends after the /exit.
    exit_idx = sendkeys.index(exit_calls[0])
    enters_after = [c for c in sendkeys[exit_idx + 1:] if c[-1] == "Enter"]
    assert len(enters_after) >= 2, (
        f"expected belt-and-suspenders double-Enter, got {enters_after}"
    )


def test_restart_claude_sends_c_c_twice_before_exit(tmp_paths, monkeypatch):
    """C-c × 2 with settles precedes /exit — kills any in-flight tool
    call / input buffer before issuing the slash command. Pre-fix
    this was already correct; test pins it.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(gw_session, "_tmux",
                         lambda *a: (calls.append(list(a)) or MagicMock(
                             returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: True)

    gw_session.restart_agent_session(
        agent="@orchestrator", reason="test", paths=tmp_paths,
    )

    sendkeys = [c for c in calls if c and c[0] == "send-keys"]
    c_cs = [c for c in sendkeys if "C-c" in c]
    assert len(c_cs) == 2, f"expected 2x C-c before /exit, got {len(c_cs)}"

    # C-c comes before /exit.
    exit_idx = next(i for i, c in enumerate(sendkeys) if "/exit" in c)
    c_c_idxs = [i for i, c in enumerate(sendkeys) if "C-c" in c]
    for idx in c_c_idxs:
        assert idx < exit_idx, "C-c must precede /exit"


def test_restart_claude_skips_when_session_dead(tmp_paths, monkeypatch):
    """No tmux traffic + return False when the session isn't alive.
    Fast-path for first-boot / crashed-pane states.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(gw_session, "_tmux",
                         lambda *a: (calls.append(list(a)) or MagicMock(
                             returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: False)
    ok = gw_session.restart_agent_session(
        agent="@orchestrator", reason="test", paths=tmp_paths,
    )
    assert ok is False
    # No send-keys traffic.
    sendkeys = [c for c in calls if c and c[0] == "send-keys"]
    assert sendkeys == []


# ---------------------------------------------------------------------------
# write_harness_hash_baseline — drift-banner divergence fix (PR #19)
#
# 2026-04-16: the drift banner fired every context inject even when
# nothing had changed. Baseline writer (gateway daemon with
# METASPHERE_REPO_ROOT=/home/<user>/projects/metasphere-agents)
# hashed source-repo CLAUDE.md; reader (@orchestrator REPL with
# CWD=~/.metasphere, no env) hashed ~/.metasphere/CLAUDE.md. Two
# different files, two different hashes, banner always mismatched.
# Fixed by pinning both to paths.root.
# ---------------------------------------------------------------------------


def test_write_harness_hash_baseline_hashes_root_not_project_root(tmp_paths):
    """Writer and reader must resolve the same file regardless of which
    of project_root/root they start from. Seed DIFFERENT content in
    both; baseline must match the root hash (what the REPL reader
    also computes).
    """
    import hashlib as _h
    (tmp_paths.root / "CLAUDE.md").write_text("ROOT content\n")
    (tmp_paths.project_root).mkdir(parents=True, exist_ok=True)
    (tmp_paths.project_root / "CLAUDE.md").write_text("REPO content\n")

    gw_session.write_harness_hash_baseline(tmp_paths)

    baseline_path = tmp_paths.state / "harness_hash_baseline"
    assert baseline_path.is_file()
    written = baseline_path.read_text(encoding="utf-8").strip()
    expected = _h.sha256(b"ROOT content\n").hexdigest()
    assert written == expected, (
        f"baseline must hash paths.root/CLAUDE.md (ROOT), "
        f"not project_root/CLAUDE.md (REPO). got {written}, "
        f"expected {expected}"
    )


def test_write_harness_hash_baseline_matches_reader_hash(tmp_paths):
    """End-to-end: baseline written by the gateway must equal the live
    hash computed by ``harness_hash`` on the same paths. Before PR #19
    these diverged when the two callers resolved different
    project_roots via env.
    """
    from metasphere.context import harness_hash
    (tmp_paths.root / "CLAUDE.md").write_text("same claude.md\n")
    # Also write a DIFFERENT file at project_root — would have
    # tripped the old code that read from project_root.
    tmp_paths.project_root.mkdir(parents=True, exist_ok=True)
    (tmp_paths.project_root / "CLAUDE.md").write_text("divergent content\n")

    gw_session.write_harness_hash_baseline(tmp_paths)
    live = harness_hash(tmp_paths)

    baseline = (tmp_paths.state / "harness_hash_baseline").read_text().strip()
    assert baseline == live, (
        "baseline writer and harness_hash reader must agree "
        "regardless of which CLAUDE.md lives at project_root"
    )


# ---------------------------------------------------------------------------
# _respawn_cmd — feedback-modal disabled (PR #21)
#
# 2026-04-16: Claude TUI's feedback modal ("How is Claude doing this
# session? 1: Bad 2: Fine 3: Good 0: Dismiss") captures input and
# causes stuck-paste accumulation. Disable at the env level for every
# metasphere-spawned agent REPL. The operator's own interactive claude
# sessions are NOT affected (this only touches agent-REPL respawn +
# agent-spawn env paths).
# ---------------------------------------------------------------------------


def test_respawn_cmd_exports_feedback_disable_env():
    """The respawn loop sets CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
    before each claude invocation so the modal doesn't show up."""
    cmd = gw_session._respawn_cmd("@orchestrator")
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1" in cmd
    # Superset that also kills the telemetry Dismiss would emit.
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in cmd
    # Must appear BEFORE the claude invocation, not after.
    disable_idx = cmd.index("CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1")
    claude_idx = cmd.index("claude --dangerously-skip-permissions")
    assert disable_idx < claude_idx, (
        "env exports must precede the claude invocation in the respawn "
        "loop so the new process inherits them"
    )


def test_respawn_cmd_with_model_still_disables_feedback():
    """Model flag didn't regress the env export."""
    cmd = gw_session._respawn_cmd("@worker", model="anthropic/claude-haiku-4-5")
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1" in cmd
    assert "--model anthropic/claude-haiku-4-5" in cmd


def test_respawn_cmd_codex_runtime_uses_interactive_tmux_flags():
    cmd = gw_session._respawn_cmd("@orchestrator", runtime="codex")
    assert "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-bypass-hook-trust" in cmd
    assert "claude --dangerously-skip-permissions" not in cmd
    assert "[gateway] codex exited" in cmd


def test_runtime_command_quotes_codex_reasoning_config():
    command, runtime = gw_session._runtime_command(
        runtime="codex",
        reasoning_effort='high"; unsafe=true',
    )
    assert runtime == "codex"
    assert "model_reasoning_effort" in command
    # The quote is escaped inside one shell-quoted TOML assignment rather
    # than becoming a second Codex config statement.
    assert r'\"; unsafe=true' in command


def test_respawn_command_preserves_adversarial_codex_values(
    tmp_path, monkeypatch
):
    """Exercise both shell boundaries and assert argv is preserved exactly."""
    import json as _json
    import os as _os
    import subprocess as _subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "argv.json"
    pwned = tmp_path / "pwned"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['ARGV_CAPTURE'], 'w') as fh:\n"
        "    json.dump(sys.argv[1:], fh)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    model = f"model'; $(touch {pwned})\nnext"
    effort = f'high"; $(touch {pwned})\nunsafe=true'
    command = gw_session._respawn_cmd(
        "@one-shot",
        runtime="codex",
        model=model,
        reasoning_effort=effort,
        agent_class="ephemeral",
    )
    env = _os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["ARGV_CAPTURE"] = str(capture)
    result = _subprocess.run(
        ["/bin/bash", "-c", command],
        stdin=_subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    argv = _json.loads(capture.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--config") + 1] == (
        f"model_reasoning_effort={_json.dumps(effort)}"
    )
    assert not pwned.exists()


def test_respawn_cmd_codex_project_agent_does_not_bypass_hook_trust():
    cmd = gw_session._respawn_cmd("@project-worker", runtime="codex")
    assert "codex --no-alt-screen" in cmd
    assert "--dangerously-bypass-hook-trust" not in cmd


def test_respawn_cmd_codex_runtime_from_env(monkeypatch):
    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    cmd = gw_session._respawn_cmd("@orchestrator")
    assert "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-bypass-hook-trust" in cmd


# ---------------------------------------------------------------------------
# _respawn_cmd — project_root export is cwd-INDEPENDENT (2026-07-27)
#
# The old export ran ``git rev-parse --show-toplevel`` on the pane's cwd
# FIRST and only fell back to the env, so on a tick whose cwd was $HOME (a
# git repo) it CLOBBERED the correct METASPHERE_PROJECT_ROOT with a truncated
# ``-home-<user>`` slug — flickering the memory-folder affordance and
# everything else keyed on project_root. The fix prefers the already-set env.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_class", ["persistent", "ephemeral"])
def test_respawn_cmd_prefers_env_project_root_over_cwd_git(agent_class):
    """Both launch classes must PREFER the exported METASPHERE_PROJECT_ROOT and
    only git-shell-out as a fallback — never let a cwd-keyed git rev-parse
    clobber the value set by the managed launch path. True revert guard."""
    cmd = gw_session._respawn_cmd("@orchestrator", agent_class=agent_class)
    # Env-preferring form present: ${VAR:-<fallback>}.
    assert 'METASPHERE_PROJECT_ROOT="${METASPHERE_PROJECT_ROOT:-' in cmd
    # The clobbering form (git FIRST, env only via ``|| echo``) must be gone.
    assert '|| echo "$METASPHERE_PROJECT_ROOT"' not in cmd
    # git remains only as the inner fallback, not the primary source.
    assert "git rev-parse --show-toplevel" in cmd


# ---------------------------------------------------------------------------
# _respawn_cmd — class-aware respawn (2026-05-05 research-monitor zombies)
#
# Persistent class (default) keeps the existing infinite respawn loop +
# restart_pending marker so the watchdog can inject a continuation prompt
# into each fresh claude. Ephemeral class drops the loop entirely: claude
# runs once and the pane drops to interactive bash so reap_ephemeral_idle
# collects the session via its session_activity timer. The bug the
# ephemeral path closes: clean exit-self → respawn loop respawned claude
# → watchdog injected continuation → fresh REPL stuck on /exit slash menu.
# ---------------------------------------------------------------------------


def test_respawn_cmd_persistent_default_keeps_while_loop():
    """Default class is persistent — must keep the infinite respawn loop
    AND write a restart_pending marker so the watchdog can inject."""
    cmd = gw_session._respawn_cmd("@orchestrator")
    assert "while true" in cmd
    assert "restart_pending.@orchestrator.json" in cmd


def test_respawn_cmd_ephemeral_drops_while_loop():
    """Ephemeral class must NOT contain a respawn loop; on claude exit
    the pane drops to interactive bash for reap_ephemeral_idle."""
    cmd = gw_session._respawn_cmd("@brand-mentions", agent_class="ephemeral")
    assert "while true" not in cmd
    # No restart_pending marker — the watchdog must not be invited to
    # inject a continuation into a non-existent successor process.
    assert "restart_pending" not in cmd
    # Pane lands at interactive bash after claude exits so the tmux
    # session_activity clock starts ticking for reap_ephemeral_idle.
    assert "exec bash" in cmd
    # Claude itself still runs once.
    assert "claude --dangerously-skip-permissions" in cmd


def test_respawn_cmd_ephemeral_preserves_env_disable():
    """The feedback-modal + nonessential-traffic env disable must apply
    to ephemeral class too — it's not just a respawn-loop concern."""
    cmd = gw_session._respawn_cmd("@one-shot", agent_class="ephemeral")
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1" in cmd
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in cmd
    disable_idx = cmd.index("CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1")
    claude_idx = cmd.index("claude --dangerously-skip-permissions")
    assert disable_idx < claude_idx


def test_respawn_cmd_ephemeral_with_model():
    """Model flag composes with ephemeral class."""
    cmd = gw_session._respawn_cmd(
        "@one-shot", model="claude-haiku-4-5", agent_class="ephemeral"
    )
    assert "while true" not in cmd
    assert "--model claude-haiku-4-5" in cmd


def test_restart_session_recreates_orchestrator_tmux(tmp_paths, monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: True)
    monkeypatch.setattr(
        gw_session,
        "_tmux",
        lambda *args: (
            calls.append(args)
            or MagicMock(returncode=0, stdout="", stderr="")
        ),
    )
    start = MagicMock(return_value=True)
    monkeypatch.setattr(gw_session, "start_session", start)

    assert gw_session.restart_session("test reason", tmp_paths) is True
    assert calls == [("kill-session", "-t", gw_session.SESSION_NAME)]
    start.assert_called_once_with(tmp_paths)
    marker = gw_session._restart_marker_path(tmp_paths)
    assert "test reason" in marker.read_text(encoding="utf-8")


def test_restart_session_stops_if_tmux_kill_fails(tmp_paths, monkeypatch):
    monkeypatch.setattr(gw_session, "session_alive", lambda name=None: True)
    monkeypatch.setattr(
        gw_session,
        "_tmux",
        lambda *args: MagicMock(returncode=1, stdout="", stderr="failed"),
    )
    start = MagicMock(return_value=True)
    monkeypatch.setattr(gw_session, "start_session", start)

    assert gw_session.restart_session("test", tmp_paths) is False
    start.assert_not_called()


# ---------------------------------------------------------------------------
# Slack daemon wire-up — discovery + _default_adapters (PR #154 follow-up)
# ---------------------------------------------------------------------------


def test_discover_slack_surface_ids_empty(tmp_path):
    # No slack*.env files → nothing discovered (Telegram-only installs).
    assert gw_daemon._discover_slack_surface_ids(str(tmp_path)) == []


def test_discover_slack_surface_ids_missing_dir(tmp_path):
    # A config dir that doesn't exist must not raise.
    missing = tmp_path / "nope"
    assert gw_daemon._discover_slack_surface_ids(str(missing)) == []


def test_discover_slack_surface_ids_default_and_named(tmp_path):
    (tmp_path / "slack.env").write_text("SLACK_BOT_TOKEN=xoxb-1\n")
    (tmp_path / "slack-relay.env").write_text("SLACK_BOT_TOKEN=xoxb-2\n")
    (tmp_path / "slack-cluster-1.env").write_text("SLACK_BOT_TOKEN=xoxb-3\n")
    ids = gw_daemon._discover_slack_surface_ids(str(tmp_path))
    # Sorted, surface_id == filename minus .env (slack.env → "slack").
    assert ids == ["slack", "slack-cluster-1", "slack-relay"]


def test_discover_slack_surface_ids_ignores_non_slack(tmp_path):
    (tmp_path / "slack-relay.env").write_text("SLACK_BOT_TOKEN=xoxb-1\n")
    (tmp_path / "telegram-relay.env").write_text("TELEGRAM_BOT_TOKEN=t\n")
    (tmp_path / "other.env").write_text("X=1\n")
    (tmp_path / "slack-notes.txt").write_text("not an env file\n")
    assert gw_daemon._discover_slack_surface_ids(str(tmp_path)) == ["slack-relay"]


def test_default_adapters_telegram_only_when_no_slack(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_daemon._slack_api, "CONFIG_DIR", str(tmp_path))
    adapters = gw_daemon._default_adapters()
    assert [a.surface_type for a in adapters] == ["telegram"]


def test_default_adapters_wires_slack_per_config(tmp_path, monkeypatch):
    (tmp_path / "slack.env").write_text("SLACK_BOT_TOKEN=xoxb-1\n")
    (tmp_path / "slack-relay.env").write_text("SLACK_BOT_TOKEN=xoxb-2\n")
    monkeypatch.setattr(gw_daemon._slack_api, "CONFIG_DIR", str(tmp_path))

    adapters = gw_daemon._default_adapters()
    # Telegram first, then one SlackAdapter per discovered config (sorted).
    assert adapters[0].surface_type == "telegram"
    slack = [a for a in adapters if a.surface_type == "slack"]
    assert [a.surface_id for a in slack] == ["slack", "slack-relay"]
    # Derived agent routing is preserved per surface_id.
    by_id = {a.surface_id: a for a in slack}
    assert by_id["slack-relay"]._target_agent_id == "@relay"


def test_poll_once_drives_slack_adapters(tmp_path, monkeypatch):
    # _poll_once falling back to _default_adapters must drive the wired
    # Slack adapters too — drained, not queued forever.
    (tmp_path / "slack-relay.env").write_text("SLACK_BOT_TOKEN=xoxb-2\n")
    monkeypatch.setattr(gw_daemon._slack_api, "CONFIG_DIR", str(tmp_path))

    with patch(
        "metasphere.gateway.adapters.telegram.TelegramAdapter.receive",
        return_value=1,
    ), patch(
        "metasphere.gateway.adapters.slack.SlackAdapter.receive",
        return_value=4,
    ):
        total = gw_daemon._poll_once(timeout=2)
    # 1 (telegram) + 4 (slack) drained this tick.
    assert total == 5


# --- Slack-only install: Telegram adapter is skipped when no token ---------

def test_telegram_configured_false_when_load_token_raises(monkeypatch):
    import metasphere.telegram.api as _tg_api
    monkeypatch.setattr(
        _tg_api, "_load_token",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no token")),
    )
    assert gw_daemon._telegram_configured() is False


def test_telegram_configured_true_when_token_present(monkeypatch):
    import metasphere.telegram.api as _tg_api
    monkeypatch.setattr(_tg_api, "_load_token", lambda *a, **k: "TEST:TOKEN")
    assert gw_daemon._telegram_configured() is True


def test_default_adapters_skips_telegram_when_unconfigured(monkeypatch):
    # Slack-only box: no telegram token, one slack surface discovered.
    monkeypatch.setattr(gw_daemon, "_telegram_configured", lambda: False)
    monkeypatch.setattr(gw_daemon, "_discover_telegram_surface_ids", lambda: [])
    monkeypatch.setattr(gw_daemon, "_discover_slack_surface_ids", lambda: ["slack"])
    names = [type(a).__name__ for a in gw_daemon._default_adapters()]
    assert "TelegramAdapter" not in names
    assert names == ["SlackAdapter"]


def test_default_adapters_includes_telegram_when_configured(monkeypatch):
    monkeypatch.setattr(gw_daemon, "_telegram_configured", lambda: True)
    monkeypatch.setattr(gw_daemon, "_discover_slack_surface_ids", lambda: [])
    names = [type(a).__name__ for a in gw_daemon._default_adapters()]
    assert names == ["TelegramAdapter"]
