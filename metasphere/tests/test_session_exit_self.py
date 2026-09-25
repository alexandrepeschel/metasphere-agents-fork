"""Tests for ``metasphere session exit-self``.

exit-self schedules ``tmux kill-session -t <target>`` for the
caller's own session via a detached background process. The 3-day
soak of the prior keystroke-injection path (C-c x2 + /exit + Enter)
showed 0/10 successes — claude panes mid-turn ignored or buffered
the injected keys, and every fire fell through to the 30-min
ephemeral reaper. Replaced with a hard session kill.

Detaching via ``subprocess.Popen(start_new_session=True)`` is still
load-bearing: the kill targets the caller's own session, which
contains this CLI process; an inline ``tmux kill-session`` would
terminate ourselves before we could log the event or return.
"""

from __future__ import annotations

from unittest.mock import patch

from metasphere.cli import session as cli_session


def _agent_record(name: str, project: str = ""):
    """Minimal AgentRecord stand-in for resolver tests."""
    from metasphere.agents import AgentRecord

    return AgentRecord(
        name=name,
        scope="",
        parent="",
        status="",
        spawned_at="",
        project=project,
    )


def test_exit_self_schedules_kill_via_detached_subprocess(monkeypatch):
    """Happy path: agent set, session alive → a detached Popen fires
    ``tmux kill-session -t <target>`` after a pre-sleep. The main
    process MUST NOT run the kill inline — that would destroy the
    session this CLI process is running in before we could return or
    log the event.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker-cron-1")

    popen_calls: list[dict] = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append({"args": args, "kwargs": kwargs})

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-worker-cron-1",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session._tmux",
        side_effect=AssertionError(
            "exit-self must NOT call _tmux from the main process — that "
            "would destroy the caller's own session before this CLI "
            "process could return. Use a detached subprocess instead."
        ),
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert len(popen_calls) == 1, f"expected one detached Popen, got {popen_calls}"
    call = popen_calls[0]

    # Detached: must use start_new_session so the child survives parent death.
    assert call["kwargs"].get("start_new_session") is True, (
        "Popen must set start_new_session=True so the kill survives "
        f"the parent metasphere CLI exiting. kwargs={call['kwargs']}"
    )

    # Argv shape: ["bash", "-c", "<script>"]
    assert call["args"][:2] == ["bash", "-c"]
    script = call["args"][2]

    # The script must schedule a hard ``tmux kill-session`` on the
    # resolved session name — no keystroke injection.
    assert "tmux kill-session -t metasphere-worker-cron-1" in script, (
        f"script must contain a kill-session targeting the resolved "
        f"session; got script={script!r}"
    )
    # Pre-sleep delays the kill so the caller's Bash tool / Stop hook
    # can return before the session is destroyed.
    assert "sleep" in script
    # Belt-and-braces: the previous keystroke-injection path must be
    # gone. 3-day soak showed it 0/10 effective.
    assert "send-keys" not in script, (
        "exit-self must not fall back to tmux send-keys keystroke "
        f"injection; got script={script!r}"
    )


def test_exit_self_no_agent_env_returns_1(monkeypatch, capsys):
    """No $METASPHERE_AGENT_ID → exit code 1 + stderr message, no kill spawn."""
    monkeypatch.delenv("METASPHERE_AGENT_ID", raising=False)

    with patch(
        "metasphere.cli.session.subprocess.Popen",
        side_effect=AssertionError("Popen must not be called"),
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "METASPHERE_AGENT_ID" in err


def test_exit_self_headless_records_tombstone_without_kill(monkeypatch):
    """Headless ``claude -p`` agent (no tmux session) → no kill spawn,
    but the clean-exit tombstone is written and rc is 0. Without it the
    natural process exit reads as a silent death to reap_crashed
    (2026-09-25 @writers-room-conversion false crash !alert)."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@headless-spawn")
    marked: list[tuple[str, str]] = []

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-headless-spawn",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=False
    ), patch(
        "metasphere.cli.session.subprocess.Popen",
        side_effect=AssertionError("Popen must not be called"),
    ), patch(
        "metasphere.cli.session.mark_exit_self",
        side_effect=lambda c, t, paths=None: marked.append((c, t)) or True,
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert marked == [("@headless-spawn", "metasphere-headless-spawn")]


def test_exit_self_headless_no_agent_dir_returns_1(monkeypatch, capsys):
    """Headless and the tombstone cannot be written (no agent dir) →
    exit code 1 naming the caller and session, no kill spawn."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@headless-spawn")

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-headless-spawn",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=False
    ), patch(
        "metasphere.cli.session.subprocess.Popen",
        side_effect=AssertionError("Popen must not be called"),
    ), patch(
        "metasphere.cli.session.mark_exit_self", return_value=False
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "@headless-spawn" in err
    assert "metasphere-headless-spawn" in err


def test_exit_self_emits_agent_exit_self_event(monkeypatch):
    """Successful exit-self appends an ``agent.exit_self`` record so the
    silent-success path is observable in the events log. Without this
    emit, a cron-fired session that exits cleanly leaves no trace
    between ``agent.session`` (start) and the next reap sweep.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker-cron-1")

    recorded: list[dict] = []

    def _fake_log_event(type_, message, *, agent=None, meta=None, **_kw):
        recorded.append(
            {"type": type_, "message": message, "agent": agent, "meta": meta or {}}
        )

    class _FakePopen:
        def __init__(self, *_args, **_kwargs):
            pass

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-worker-cron-1",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ), patch(
        "metasphere.cli.session.log_event", side_effect=_fake_log_event
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    exit_evts = [r for r in recorded if r["type"] == "agent.exit_self"]
    assert len(exit_evts) == 1, f"expected one agent.exit_self event, got {recorded}"
    evt = exit_evts[0]
    assert evt["agent"] == "@worker-cron-1"
    assert evt["meta"].get("session") == "metasphere-worker-cron-1"


def test_exit_self_event_emit_failure_does_not_break_exit(monkeypatch):
    """If ``log_event`` raises (disk full, permissions, etc), the actual
    kill spawn must still complete and the call must still return 0 —
    observability is best-effort, the kill is load-bearing.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker-cron-1")

    spawned: list[bool] = []

    class _FakePopen:
        def __init__(self, *_args, **_kwargs):
            spawned.append(True)

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-worker-cron-1",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ), patch(
        "metasphere.cli.session.log_event",
        side_effect=OSError("disk full"),
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert spawned, "kill spawn must run even when log_event fails"


def test_exit_self_resolves_project_scoped_agent(monkeypatch):
    """Project-scoped agents must resolve to the project-prefixed session
    name, not the bare ``session_name_for`` form. Regression mirrors the
    bug class fixed in 107c792 for ``_check_deferred_command``'s resolver.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@accelerator-programs")

    popen_calls: list[dict] = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            popen_calls.append({"args": args, "kwargs": kwargs})

    rec = _agent_record("@accelerator-programs", project="research")

    # Drive the real ``_resolve_session`` so the project lookup is exercised.
    with patch(
        "metasphere.session.list_agents", return_value=[rec]
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert popen_calls, "expected one detached Popen for the kill"
    script = popen_calls[0]["args"][2]
    expected = "tmux kill-session -t metasphere-research-accelerator-programs"
    assert expected in script, (
        f"detached kill script must target project-scoped session "
        f"{expected!r}; got script={script!r}"
    )
    # Verify the bare (un-prefixed) session name is NOT what we kill.
    bare_pattern = "tmux kill-session -t metasphere-accelerator-programs"
    assert bare_pattern not in script, (
        "detached kill script must not target the bare session name "
        "(regression: 04-28 project-scope resolver bug)"
    )


def test_exit_self_writes_tombstone_before_kill(monkeypatch):
    """exit-self must record the clean-exit intent (mark_exit_self
    tombstone) BEFORE queuing the detached kill — once the kill lands,
    pid and session both read dead and reap_crashed would classify the
    exit as a silent death (false crash !alert, 2026-07-05
    @writing-lead case)."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker-cron-1")

    order: list[str] = []

    def _fake_mark(caller, target, paths=None):
        order.append(f"tombstone:{caller}:{target}")
        return True

    class _FakePopen:
        def __init__(self, *_args, **_kwargs):
            order.append("kill-queued")

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-worker-cron-1",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ), patch(
        "metasphere.cli.session.mark_exit_self", side_effect=_fake_mark
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert order == [
        "tombstone:@worker-cron-1:metasphere-worker-cron-1",
        "kill-queued",
    ], f"tombstone must be written before the kill is queued; got {order}"


def test_exit_self_tombstone_failure_does_not_block_kill(monkeypatch):
    """A mark_exit_self failure (missing agent dir, IO error) is
    bookkeeping — the kill spawn must still run and exit-self must
    still return 0. Worst case is the pre-fix behavior (one false
    crash alert), never a wedged exit path."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker-cron-1")

    spawned: list[bool] = []

    class _FakePopen:
        def __init__(self, *_args, **_kwargs):
            spawned.append(True)

    with patch(
        "metasphere.cli.session._resolve_session",
        return_value="metasphere-worker-cron-1",
    ), patch(
        "metasphere.cli.session.session_alive", return_value=True
    ), patch(
        "metasphere.cli.session.subprocess.Popen", _FakePopen
    ), patch(
        "metasphere.cli.session.mark_exit_self",
        side_effect=OSError("agent dir vanished"),
    ):
        rc = cli_session.main(["exit-self"])

    assert rc == 0
    assert spawned, "kill spawn must run even when the tombstone write fails"
