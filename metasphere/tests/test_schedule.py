"""Tests for metasphere.schedule."""

from __future__ import annotations

import datetime as _dt
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from metasphere import schedule as _sched
from metasphere.schedule import Job


def _make_job(**overrides) -> Job:
    base = dict(
        id="job-test-1",
        source="test",
        source_id="test-1",
        agent_id="main",
        name="research-monitor:brand-mentions",
        enabled=True,
        kind="cron",
        cron_expr="* * * * *",
        tz="UTC",
        payload_kind="agentTurn",
        payload_message="do the thing",
        model="anthropic/claude-sonnet-4-5",
        session_target="isolated",
        wake_mode="next-heartbeat",
        imported_at=1700000000,
        last_fired_at=0,
        next_run=0,
        command='send @main !task "x"',
        full_command="",
    )
    base.update(overrides)
    return Job(**base)


def test_load_save_roundtrip_preserves_all_fields(tmp_paths):
    j = _make_job()
    _sched.save_jobs([j], tmp_paths, _input_count=1)
    loaded = _sched.load_jobs(tmp_paths)
    assert len(loaded) == 1
    assert loaded[0] == j


def test_shrink_detection_refuses_zero_write(tmp_paths):
    j = _make_job()
    _sched.save_jobs([j], tmp_paths, _input_count=1)
    with pytest.raises(RuntimeError, match="refusing to wipe"):
        _sched.save_jobs([], tmp_paths, _input_count=1)
    # File still has the job.
    assert len(_sched.load_jobs(tmp_paths)) == 1


def test_cron_should_fire_due_in_window():
    # "* * * * *" — fires every minute. last_fired_at=0 → must fire.
    assert _sched.cron_should_fire("* * * * *", "UTC", 0) is True


def test_cron_should_fire_already_fired():
    now = int(time.time())
    # Just fired this minute → must NOT fire again.
    assert _sched.cron_should_fire("* * * * *", "UTC", now, now=now) is False


def test_resolve_target_agent_uses_agent_id():
    """``resolve_target_agent`` returns ``"@" + job.agent_id``.

    Pre-2026-04-30, the function had hardcoded prefix-match branches
    that overrode ``agent_id``. Those are gone. Callers wire the
    target via the ``agent_id`` field; live jobs.json files were
    migrated to carry the resolved ``agent_id`` directly so the
    simplification was behavior-preserving.
    """
    assert _sched.resolve_target_agent(
        _make_job(name="research-monitor:brand-mentions",
                  agent_id="brand-mentions")
    ) == "@brand-mentions"

    assert _sched.resolve_target_agent(
        _make_job(name="acme:trading-run", agent_id="acme")
    ) == "@acme"

    assert _sched.resolve_target_agent(
        _make_job(name="Morning briefing", agent_id="example-cron")
    ) == "@example-cron"


def test_resolve_target_agent_default_main():
    """Empty ``agent_id`` falls back to ``@main``."""
    j = _make_job(name="something:else", agent_id="")
    assert _sched.resolve_target_agent(j) == "@main"


def test_resolve_target_agent_accepts_already_prefixed_ids():
    assert _sched.resolve_target_agent(_make_job(agent_id="@orchestrator")) == "@orchestrator"


def test_resolve_target_agent_ignores_name_prefix():
    """Even with a legacy-looking name, ``agent_id`` wins (no prefix magic)."""
    j = _make_job(name="research-monitor:brand-mentions",
                  agent_id="custom-agent")
    assert _sched.resolve_target_agent(j) == "@custom-agent"


def test_run_due_jobs_updates_last_fired_at(tmp_paths):
    j = _make_job(cron_expr="* * * * *", last_fired_at=0)
    _sched.save_jobs([j], tmp_paths, _input_count=1)

    fixed_now = int(time.time())
    with mock.patch("metasphere.schedule.dispatch_to_agent", return_value=True) as disp:
        results = _sched.run_due_jobs(tmp_paths, now=fixed_now)

    assert len(results) == 1
    assert results[0].fired and results[0].dispatched
    disp.assert_called_once()

    reloaded = _sched.load_jobs(tmp_paths)
    assert reloaded[0].last_fired_at == fixed_now


def test_run_due_jobs_persists_last_fired_before_dispatch(tmp_paths):
    """If a dispatch crashes the daemon mid-fire (e.g. metasphere update
    restarting metasphere-schedule), last_fired_at must already be on
    disk so the next tick doesn't re-fire within the cron window. This
    is the 04:01-04:03Z 2026-04-27 auto-update storm scenario."""
    j = _make_job(cron_expr="* * * * *", last_fired_at=0, payload_kind="command")
    _sched.save_jobs([j], tmp_paths, _input_count=1)

    # Pin to second 5 of the current minute so the second call at
    # ``fixed_now + 15`` stays within the same minute. Without this,
    # CI runs that landed at second >=45 of a minute saw the second
    # call cross the next ``* * * * *`` boundary and re-fire legit.
    fixed_now = (int(time.time()) // 60) * 60 + 5
    last_fired_during_dispatch: list[int] = []

    def _crashing_dispatch(*_args, **_kwargs):
        # Simulate the daemon being able to read jobs.json mid-dispatch
        # (i.e. another process). Stamp must already be persisted.
        reloaded = _sched.load_jobs(tmp_paths)
        last_fired_during_dispatch.append(reloaded[0].last_fired_at)
        raise RuntimeError("simulated daemon restart mid-dispatch")

    with mock.patch("metasphere.schedule.dispatch_command", side_effect=_crashing_dispatch):
        with pytest.raises(RuntimeError, match="simulated daemon restart"):
            _sched.run_due_jobs(tmp_paths, now=fixed_now)

    assert last_fired_during_dispatch == [fixed_now], (
        "last_fired_at must be persisted BEFORE dispatch runs"
    )

    # Re-running run_due_jobs in the same cron window must NOT re-fire,
    # because last_fired_at == prev_epoch (already-fired guard).
    with mock.patch("metasphere.schedule.dispatch_command") as disp2:
        results2 = _sched.run_due_jobs(tmp_paths, now=fixed_now + 15)
    assert results2 == []
    disp2.assert_not_called()


def test_set_enabled_accepts_id_or_name(tmp_paths):
    j = _make_job(id="metasphere-auto-update", name="metasphere:auto-update", enabled=True)
    _sched.save_jobs([j], tmp_paths, _input_count=1)

    assert _sched.set_enabled("metasphere-auto-update", False, tmp_paths) is True
    assert _sched.load_jobs(tmp_paths)[0].enabled is False

    # Re-enable using the displayed ``name`` (the inconsistency that bit
    # users on 2026-04-27).
    assert _sched.set_enabled("metasphere:auto-update", True, tmp_paths) is True
    assert _sched.load_jobs(tmp_paths)[0].enabled is True

    assert _sched.set_enabled("does-not-exist", False, tmp_paths) is False


def test_dispatch_prefers_wake_persistent_when_global_mission_exists(tmp_paths):
    target = "@acme"
    agent_dir = tmp_paths.agent_dir(target)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock:
        wake_mock.return_value = (mock.Mock(), True)
        ok = _sched.dispatch_to_agent(target, "payload-text", paths=tmp_paths)

    assert ok is True
    wake_mock.assert_called_once()
    # wake_persistent(target, first_task=payload, paths=tmp_paths)
    args, kwargs = wake_mock.call_args
    assert args[0] == target
    assert kwargs.get("first_task") == "payload-text"
    assert kwargs.get("paths") is tmp_paths


def test_dispatch_wakes_project_scoped_persistent_agent(tmp_paths):
    """Project-scoped research agents live under
    ``projects/<proj>/agents/@name/MISSION.md``. Dispatching must find
    them too, or @research-* jobs pile up unread (the operator's bug report)."""
    target = "@research-brand-mentions"
    proj_agent_dir = tmp_paths.project_agent_dir("research", target)
    proj_agent_dir.mkdir(parents=True, exist_ok=True)
    (proj_agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock:
        wake_mock.return_value = (mock.Mock(), True)
        ok = _sched.dispatch_to_agent(target, "scan now", paths=tmp_paths)

    assert ok is True
    wake_mock.assert_called_once()
    args, kwargs = wake_mock.call_args
    assert args[0] == target
    assert kwargs.get("first_task") == "scan now"


def test_dispatch_to_agent_falls_back_to_inbox_when_wake_fails(tmp_paths):
    target = "@acme"
    agent_dir = tmp_paths.agent_dir(target)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch(
        "metasphere.schedule._agents.wake_persistent",
        side_effect=RuntimeError("tmux died"),
    ), mock.patch("metasphere.schedule.send_message") as send_mock:
        send_mock.return_value = mock.Mock()
        ok = _sched.dispatch_to_agent(target, "payload", paths=tmp_paths)

    assert ok is True
    send_mock.assert_called_once()


def test_dispatch_to_agent_falls_back_to_inbox_when_inject_silently_fails(tmp_paths):
    """Issue #106 core regression: wake_persistent reaches the agent's
    session but ``_submit_via_tmux`` returns False (gateway cascade-reap
    mid-fire, session vanished between has-session and send-keys, etc.).
    Previously the scheduler treated this as success, stamped
    last_fired_at, and lost the task. Now it must fall through to
    send_message so the inbox has the audit trail."""
    target = "@acme"
    agent_dir = tmp_paths.agent_dir(target)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.send_message") as send_mock:
        # Session came up but the inject silently failed.
        wake_mock.return_value = (mock.Mock(), False)
        send_mock.return_value = mock.Mock()
        ok = _sched.dispatch_to_agent(target, "payload", paths=tmp_paths)

    assert ok is True
    wake_mock.assert_called_once()
    send_mock.assert_called_once()
    args, kwargs = send_mock.call_args
    assert args[0] == target
    assert args[1] == "!task"
    assert args[2] == "payload"


def test_dispatch_to_agent_ephemeral_uses_inbox(tmp_paths):
    # No MISSION.md anywhere → drop to inbox, no wake.
    target = "@someone"
    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.send_message") as send_mock:
        send_mock.return_value = mock.Mock()
        ok = _sched.dispatch_to_agent(target, "payload", paths=tmp_paths)

    assert ok is True
    wake_mock.assert_not_called()
    send_mock.assert_called_once()


# ---------- dispatch_command: wake-before-send ----------


def test_extract_messages_send_target_bare_command():
    assert (
        _sched._extract_messages_send_target(
            'messages send @acme !task "run pipeline"'
        )
        == "@acme"
    )


def test_extract_messages_send_target_full_path():
    assert (
        _sched._extract_messages_send_target(
            '/usr/local/bin/messages send @research-brand !task "scan"'
        )
        == "@research-brand"
    )


def test_extract_messages_send_target_not_a_send_command():
    assert _sched._extract_messages_send_target("echo hi") is None
    assert _sched._extract_messages_send_target("messages inbox") is None
    # Send but no @-target (shouldn't happen, but don't crash).
    assert _sched._extract_messages_send_target("messages send !task hi") is None


def test_extract_messages_send_target_malformed_payload():
    # Unbalanced quote → shlex raises → return None cleanly.
    assert _sched._extract_messages_send_target('messages send @x "oops') is None


def test_extract_messages_send_target_canonical_msg_bare():
    # Canonical bare `msg` console-script (shim form).
    assert (
        _sched._extract_messages_send_target('msg send @acme !task "x"')
        == "@acme"
    )


def test_extract_messages_send_target_canonical_metasphere_msg():
    # Canonical unified-CLI form, bare and full-path.
    assert (
        _sched._extract_messages_send_target(
            'metasphere msg send @acme !task "run pipeline"'
        )
        == "@acme"
    )
    assert (
        _sched._extract_messages_send_target(
            '/home/alice/.metasphere/bin/metasphere msg send '
            '@research-brand !task "scan"'
        )
        == "@research-brand"
    )


def test_extract_messages_send_target_metasphere_non_send():
    # `metasphere msg read` etc. must not match.
    assert _sched._extract_messages_send_target("metasphere msg read") is None
    assert _sched._extract_messages_send_target("metasphere msg send !task hi") is None


def test_dispatch_command_pre_wakes_messages_send_task_target(tmp_paths):
    """The main regression fix: scheduled `messages send @acme !task`
    commands must cold-start the agent's tmux+REPL before sending, so
    the inbox notice has a live session to inject into."""
    target = "@acme"
    agent_dir = tmp_paths.agent_dir(target)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        wake_mock.return_value = (mock.Mock(), True)
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ok = _sched.dispatch_command(
            'messages send @acme !task "run the acme pipeline"',
            paths=tmp_paths,
        )

    assert ok is True
    wake_mock.assert_called_once()
    args, kwargs = wake_mock.call_args
    assert args[0] == target
    # Pre-wake should not pass a first_task — the subsequent shell
    # command carries the actual inbox notice.
    assert kwargs.get("first_task") is None
    # The real shell command must still run after the pre-wake.
    run_mock.assert_called_once()


def test_dispatch_command_pre_wakes_project_scoped_research_target(tmp_paths):
    target = "@research-brand-mentions"
    proj_agent_dir = tmp_paths.project_agent_dir("research", target)
    proj_agent_dir.mkdir(parents=True, exist_ok=True)
    (proj_agent_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        wake_mock.return_value = (mock.Mock(), True)
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ok = _sched.dispatch_command(
            'messages send @research-brand-mentions !task "do the scan"',
            paths=tmp_paths,
        )

    assert ok is True
    wake_mock.assert_called_once()


def test_dispatch_command_skips_wake_for_ephemeral_target(tmp_paths):
    # No MISSION.md — nothing to wake, command still runs.
    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ok = _sched.dispatch_command(
            'messages send @ephemeral !task "x"',
            paths=tmp_paths,
        )

    assert ok is True
    wake_mock.assert_not_called()
    run_mock.assert_called_once()


def test_dispatch_command_does_not_wake_for_non_send_command(tmp_paths):
    # Arbitrary command, not `messages send` — never wake.
    target_dir = tmp_paths.agent_dir("@acme")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "MISSION.md").write_text("mission\n")

    with mock.patch("metasphere.schedule._agents.wake_persistent") as wake_mock, \
            mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ok = _sched.dispatch_command("echo hello", paths=tmp_paths)

    assert ok is True
    wake_mock.assert_not_called()


# ---------- dispatch_command: failure-diagnostic surfacing ----------


def test_dispatch_command_failure_logs_full_argv_and_stdout_fallback(
    tmp_paths, caplog
):
    """Non-zero exit must surface (a) the full argv, not just argv[0],
    and (b) stdout when stderr is empty — fixes the silent
    `exited 1: ` entries that accumulated in schedule.log when the
    failing process wrote diagnostic to stdout."""
    import logging

    caplog.set_level(logging.WARNING, logger="metasphere.schedule")
    with mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(
            returncode=1, stdout="cli error: bad flag", stderr=""
        )
        ok = _sched.dispatch_command(
            "/usr/bin/metasphere agents list --busted-flag", paths=tmp_paths
        )

    assert ok is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "/usr/bin/metasphere agents list --busted-flag" in m
        and "cli error: bad flag" in m
        for m in msgs
    ), msgs


def test_dispatch_command_failure_logs_no_output_marker(tmp_paths, caplog):
    """If both stderr and stdout are empty, the warning must still
    name the failure with a `(no output)` marker rather than render
    as a bare `exited 1: ` line that gives operators nothing to act
    on."""
    import logging

    caplog.set_level(logging.WARNING, logger="metasphere.schedule")
    with mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(returncode=2, stdout="", stderr="")
        ok = _sched.dispatch_command("/bin/false", paths=tmp_paths)

    assert ok is False
    assert any("(no output)" in r.getMessage() for r in caplog.records)


def test_dispatch_command_failure_prefers_stderr(tmp_paths, caplog):
    """Stderr is the conventional diagnostic surface; when both
    streams have content, stderr wins."""
    import logging

    caplog.set_level(logging.WARNING, logger="metasphere.schedule")
    with mock.patch("metasphere.schedule.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(
            returncode=1, stdout="stdout-msg", stderr="stderr-msg"
        )
        _sched.dispatch_command("/bin/false", paths=tmp_paths)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("stderr-msg" in m for m in msgs)
    assert not any("stdout-msg" in m for m in msgs)


# ---------- wire-exit-self migration tool ----------

from metasphere.cli.wire_exit_self import (
    SENTINEL,
    wire_exit_self,
)

# A representative set of job names for the per-job-flag tests. The
# contract is now "any job with wants_exit_self_cleanup=True"; here we
# just need a few names to exercise it.
_SAMPLE_FLAGGED_NAMES: tuple[str, ...] = (
    "Morning briefing",
    "rage-changelog-update",
    "research-monitor:brand-mentions",
    "research-monitor:memory-architectures",
)


def _seed_jobs_for_wire_test(tmp_paths, *, payloads=None):
    """Seed jobs.json with flagged jobs + one off-target control.

    ``payloads`` is an optional dict ``{name: payload_message}``; missing
    entries default to a short instructional body so the append has
    something to attach to.
    """
    payloads = payloads or {}
    jobs = []
    for i, name in enumerate(_SAMPLE_FLAGGED_NAMES):
        body = payloads.get(name, f"Run {name}\nStep 1: do thing\nStep 2: report")
        jobs.append(
            _make_job(
                id=f"job-target-{i}",
                source_id=f"target-{i}",
                name=name,
                payload_message=body,
                wants_exit_self_cleanup=True,
            )
        )
    # Untouched control: persistent agent without the flag.
    jobs.append(
        _make_job(
            id="job-control-acme",
            source_id="control-acme",
            name="acme:trading-run",
            payload_message="run acme pipeline",
            wants_exit_self_cleanup=False,
        )
    )
    _sched.save_jobs(jobs, tmp_paths, _input_count=len(jobs))


def test_wire_exit_self_appends_to_flagged_jobs(tmp_paths):
    _seed_jobs_for_wire_test(tmp_paths)
    result = wire_exit_self(tmp_paths)

    assert sorted(result["modified"]) == sorted(_SAMPLE_FLAGGED_NAMES)
    assert result["skipped"] == []

    saved = {j.name: j for j in _sched.load_jobs(tmp_paths)}
    for name in _SAMPLE_FLAGGED_NAMES:
        assert SENTINEL in saved[name].payload_message, name
        # Original instructions still present at the head of the body.
        assert saved[name].payload_message.startswith(f"Run {name}")

    # Control job (flag unset) — payload must be unchanged.
    assert saved["acme:trading-run"].payload_message == "run acme pipeline"
    assert SENTINEL not in saved["acme:trading-run"].payload_message


def test_wire_exit_self_is_idempotent(tmp_paths):
    _seed_jobs_for_wire_test(tmp_paths)
    first = wire_exit_self(tmp_paths)
    assert len(first["modified"]) == len(_SAMPLE_FLAGGED_NAMES)

    # Capture payload state after first run for byte-for-byte equality.
    after_first = {j.name: j.payload_message for j in _sched.load_jobs(tmp_paths)}

    second = wire_exit_self(tmp_paths)
    assert second["modified"] == []
    assert sorted(second["skipped"]) == sorted(_SAMPLE_FLAGGED_NAMES)

    after_second = {j.name: j.payload_message for j in _sched.load_jobs(tmp_paths)}
    assert after_second == after_first


def test_wire_exit_self_dry_run_does_not_mutate(tmp_paths):
    _seed_jobs_for_wire_test(tmp_paths)
    before = {j.name: j.payload_message for j in _sched.load_jobs(tmp_paths)}

    result = wire_exit_self(tmp_paths, dry_run=True)
    assert sorted(result["modified"]) == sorted(_SAMPLE_FLAGGED_NAMES)

    after = {j.name: j.payload_message for j in _sched.load_jobs(tmp_paths)}
    assert before == after, "dry-run must not write to jobs.json"


def test_wire_exit_self_no_flagged_jobs_is_no_op(tmp_paths):
    """jobs.json with no wants_exit_self_cleanup=True jobs -> empty result."""
    _sched.save_jobs(
        [_make_job(id="placeholder", name="other:job",
                   wants_exit_self_cleanup=False)],
        tmp_paths,
        _input_count=1,
    )
    result = wire_exit_self(tmp_paths)
    assert result["modified"] == []
    assert result["skipped"] == []


def test_wire_exit_self_preserves_existing_funding_sink_stanza(tmp_paths):
    # 3 of the live research-monitor payloads end with a `---` Funding
    # pipeline sink stanza (added 2026-04-14). The cleanup append must
    # sit BELOW the sink without disturbing it.
    sink_payload = (
        "Run research-monitor for area: residency-programs\n"
        "Steps...\n\n"
        "---\n"
        "**Funding pipeline sink:** copy report to "
        "~/.metasphere/agents/@example-research/sources/residency/<date>.md"
    )
    job = _make_job(
        id="job-residency",
        name="research-monitor:residency-programs",
        payload_message=sink_payload,
        wants_exit_self_cleanup=True,
    )
    _sched.save_jobs([job], tmp_paths, _input_count=1)

    result = wire_exit_self(tmp_paths)
    assert result["modified"] == ["research-monitor:residency-programs"]

    saved = _sched.load_jobs(tmp_paths)[0].payload_message
    # Sink stanza is still intact at its original position.
    assert "**Funding pipeline sink:**" in saved
    sink_idx = saved.index("**Funding pipeline sink:**")
    cleanup_idx = saved.index("Session cleanup")
    # Cleanup is appended AFTER the sink — both head and tail divider
    # are preserved.
    assert sink_idx < cleanup_idx
    assert SENTINEL in saved


# ---------- generic scheduled agent wakes ----------


def test_dispatch_wakes_orchestrator_without_mission_file(tmp_paths):
    with mock.patch(
        "metasphere.gateway.session.start_session", return_value=True
    ) as start, mock.patch(
        "metasphere.schedule._agents._submit_via_tmux", return_value=True
    ) as submit:
        ok = _sched.dispatch_to_agent(
            "@orchestrator", "scheduled wake", paths=tmp_paths
        )

    assert ok is True
    start.assert_called_once_with(tmp_paths)
    submit.assert_called_once_with(
        "metasphere-orchestrator", "[task] scheduled wake"
    )


def test_isolated_job_without_model_still_spawns_ephemeral(tmp_paths):
    launched = mock.MagicMock(pid_file=tmp_paths.state / "cron.pid")
    with mock.patch(
        "metasphere.schedule._agents.spawn_ephemeral", return_value=launched
    ) as spawn, mock.patch(
        "metasphere.gateway.session.start_session", return_value=True
    ) as start, mock.patch(
        "metasphere.schedule._agents._submit_via_tmux", return_value=True
    ) as submit:
        ok = _sched.dispatch_to_agent(
            "@orchestrator",
            "run the thing",
            paths=tmp_paths,
            job_name="writers-room:daily-checkin",
            model="",
            session_target="isolated",
        )

    assert ok is True
    start.assert_not_called()
    submit.assert_not_called()
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["model"] == ""


def test_isolated_agent_names_do_not_collide_within_one_minute(monkeypatch):
    ids = iter((SimpleNamespace(hex="a" * 32), SimpleNamespace(hex="b" * 32)))
    monkeypatch.setattr(_sched.uuid, "uuid4", lambda: next(ids))

    first = _sched._isolated_agent_name("writers-room:daily-checkin")
    second = _sched._isolated_agent_name("writers-room:daily-checkin")

    assert first != second
    assert first.startswith("@writers-room-daily-checkin-")
    assert second.startswith("@writers-room-daily-checkin-")


def test_isolated_spawn_without_process_falls_back_to_orchestrator(tmp_paths):
    not_launched = mock.MagicMock(pid_file=None)
    with mock.patch(
        "metasphere.schedule._agents.spawn_ephemeral", return_value=not_launched
    ), mock.patch(
        "metasphere.gateway.session.start_session", return_value=True
    ), mock.patch(
        "metasphere.schedule._agents._submit_via_tmux", return_value=True
    ) as submit:
        ok = _sched.dispatch_to_agent(
            "@orchestrator",
            "run the thing",
            paths=tmp_paths,
            job_name="daily",
            model="claude-haiku-4-5-20251001",
            session_target="isolated",
        )

    assert ok is True
    submit.assert_called_once()


def test_run_due_jobs_passes_session_target(tmp_paths):
    job = _make_job(agent_id="@orchestrator", session_target="isolated")
    _sched.save_jobs([job], tmp_paths, _input_count=1)

    with mock.patch(
        "metasphere.schedule.cron_should_fire", return_value=True
    ), mock.patch(
        "metasphere.schedule.dispatch_to_agent", return_value=True
    ) as dispatch:
        _sched.run_due_jobs(tmp_paths, now=1700000100)

    assert dispatch.call_args.kwargs["session_target"] == "isolated"


def test_upsert_agent_job_creates_and_preserves_fire_history(tmp_paths):
    created = _sched.upsert_agent_job(
        "daily-check",
        agent="@orchestrator",
        cron_expr="0 9 * * *",
        message="first payload",
        tz="America/Los_Angeles",
        paths=tmp_paths,
    )
    created.last_fired_at = 12345
    _sched.save_jobs([created], tmp_paths, _input_count=1)

    updated = _sched.upsert_agent_job(
        "daily-check",
        agent="orchestrator",
        cron_expr="30 9 * * *",
        message="updated payload",
        tz="America/Los_Angeles",
        paths=tmp_paths,
    )

    assert updated.agent_id == "orchestrator"
    assert updated.payload_message == "updated payload"
    assert updated.last_fired_at == 12345
    assert len(_sched.load_jobs(tmp_paths)) == 1


def test_upsert_agent_job_rejects_invalid_cron_and_timezone(tmp_paths):
    with pytest.raises(ValueError, match="invalid cron expression"):
        _sched.upsert_agent_job(
            "bad-cron",
            agent="@orchestrator",
            cron_expr="not cron",
            message="payload",
            paths=tmp_paths,
        )
    with pytest.raises(ValueError, match="invalid timezone"):
        _sched.upsert_agent_job(
            "bad-zone",
            agent="@orchestrator",
            cron_expr="0 9 * * *",
            message="payload",
            tz="Mars/Olympus",
            paths=tmp_paths,
        )


def test_remove_job_can_remove_final_registry_entry(tmp_paths):
    _sched.upsert_agent_job(
        "one-shot",
        agent="@orchestrator",
        cron_expr="0 9 * * *",
        message="payload",
        paths=tmp_paths,
    )
    assert _sched.remove_job("one-shot", tmp_paths) is True
    assert _sched.load_jobs(tmp_paths) == []


def test_fire_job_dispatches_without_changing_last_fired(tmp_paths):
    job = _sched.upsert_agent_job(
        "manual-check",
        agent="@orchestrator",
        cron_expr="0 9 * * *",
        message="payload",
        paths=tmp_paths,
    )
    with mock.patch("metasphere.schedule.dispatch_to_agent", return_value=True) as dispatch:
        result = _sched.fire_job(job.id, tmp_paths)
    assert result is not None and result.dispatched is True
    dispatch.assert_called_once_with(
        "@orchestrator",
        "payload",
        paths=tmp_paths,
        job_name="manual-check",
        model="",
        session_target="persistent",
    )
    assert _sched.load_jobs(tmp_paths)[0].last_fired_at == 0


def test_fire_job_passes_session_target(tmp_paths):
    job = _make_job(
        id="manual-isolated",
        agent_id="@orchestrator",
        session_target="isolated",
    )
    _sched.save_jobs([job], tmp_paths, _input_count=1)

    with mock.patch(
        "metasphere.schedule.dispatch_to_agent", return_value=True
    ) as dispatch:
        _sched.fire_job("manual-isolated", tmp_paths)

    assert dispatch.call_args.kwargs["session_target"] == "isolated"
