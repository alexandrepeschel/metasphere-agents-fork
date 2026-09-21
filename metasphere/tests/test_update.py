"""Tests for metasphere.update (auto-update config + scheduler integration)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metasphere import schedule as _sched
from metasphere import update as _update
from metasphere.update import AutoUpdateConfig, UpdateResult


# ---------- config parsing ----------

def test_parse_env_text_defaults():
    cfg = _update.parse_env_text("")
    assert cfg.enabled is False
    assert cfg.interval == "daily"
    assert cfg.branch == "main"
    assert cfg.restart_daemons is True
    assert cfg.notify is True


def test_parse_env_text_full():
    text = """
    # comment
    AUTO_UPDATE_ENABLED=true
    AUTO_UPDATE_INTERVAL=hourly
    AUTO_UPDATE_BRANCH="staging"
    AUTO_UPDATE_RESTART_DAEMONS=false
    AUTO_UPDATE_NOTIFY=0
    """
    cfg = _update.parse_env_text(text)
    assert cfg.enabled is True
    assert cfg.interval == "hourly"
    assert cfg.branch == "staging"
    assert cfg.restart_daemons is False
    assert cfg.notify is False


def test_save_load_roundtrip(tmp_paths):
    cfg = AutoUpdateConfig(enabled=True, interval="6h", branch="main")
    p = _update.save_config(cfg, tmp_paths)
    assert p.exists()
    loaded = _update.load_config(tmp_paths)
    assert loaded == cfg


def test_interval_to_cron_keywords():
    assert _update.interval_to_cron("daily") == "0 4 * * *"
    assert _update.interval_to_cron("hourly") == "0 * * * *"
    assert _update.interval_to_cron("6h") == "0 */6 * * *"


def test_interval_to_cron_passthrough_custom():
    assert _update.interval_to_cron("*/15 * * * *") == "*/15 * * * *"


def test_interval_to_cron_unknown_falls_back_daily():
    assert _update.interval_to_cron("garbage") == "0 4 * * *"


# ---------- schedule integration ----------

def test_register_job_creates(tmp_paths):
    cfg = AutoUpdateConfig(enabled=True, interval="hourly")
    job = _update.register_job(cfg, tmp_paths)
    assert job.id == _update.JOB_ID
    assert job.cron_expr == "0 * * * *"
    assert job.enabled is True
    jobs = _sched.load_jobs(tmp_paths)
    assert any(j.id == _update.JOB_ID for j in jobs)


def test_register_job_updates_existing(tmp_paths):
    _update.register_job(AutoUpdateConfig(enabled=True, interval="hourly"), tmp_paths)
    _update.register_job(AutoUpdateConfig(enabled=False, interval="6h"), tmp_paths)
    jobs = _sched.load_jobs(tmp_paths)
    matching = [j for j in jobs if j.id == _update.JOB_ID]
    assert len(matching) == 1
    assert matching[0].cron_expr == "0 */6 * * *"
    assert matching[0].enabled is False


def test_register_job_prefers_canonical_runtime_over_invoking_venv(
    tmp_paths, monkeypatch
):
    """A project venv must not become a permanent scheduler dependency."""
    canonical = tmp_paths.root / "venv" / "bin" / "metasphere"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("#!/bin/sh\n")
    foreign_python = tmp_paths.project_root / ".venv" / "bin" / "python"
    foreign_binary = foreign_python.with_name("metasphere")
    foreign_binary.parent.mkdir(parents=True)
    foreign_binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(_update.sys, "executable", str(foreign_python))

    job = _update.register_job(AutoUpdateConfig(enabled=True), tmp_paths)

    expected = f"{canonical} update --quiet"
    assert job.payload_message == expected
    persisted = next(
        j for j in _sched.load_jobs(tmp_paths) if j.id == _update.JOB_ID
    )
    assert persisted.payload_message == expected


def test_register_job_preserves_last_fired_at(tmp_paths):
    _update.register_job(AutoUpdateConfig(enabled=True), tmp_paths)
    # Mutate last_fired_at then re-register.
    with _sched.with_locked_jobs(tmp_paths) as jobs:
        for j in jobs:
            if j.id == _update.JOB_ID:
                j.last_fired_at = 12345
        _sched.save_jobs(jobs, tmp_paths, _input_count=len(jobs))
    _update.register_job(AutoUpdateConfig(enabled=True, interval="hourly"), tmp_paths)
    jobs = _sched.load_jobs(tmp_paths)
    j = next(j for j in jobs if j.id == _update.JOB_ID)
    assert j.last_fired_at == 12345


def test_unregister_job_removes(tmp_paths):
    _update.register_job(AutoUpdateConfig(enabled=True), tmp_paths)
    assert _update.unregister_job(tmp_paths) is True
    jobs = _sched.load_jobs(tmp_paths)
    assert not any(j.id == _update.JOB_ID for j in jobs)


def test_unregister_job_noop_when_missing(tmp_paths):
    assert _update.unregister_job(tmp_paths) is False


# ---------- update flow (dry, monkeypatched) ----------

def _mk_runner(responses):
    """Build a fake git runner returning canned CompletedProcess-likes."""
    import subprocess

    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        key = args[0] if args else ""
        out = responses.get(key, "")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _patch_update_helpers(monkeypatch, *, pull_raises=None, sync_calls=None,
                          restart_calls=None):
    """Monkeypatch the three module-level side-effect helpers.

    Returns nothing; call sites read the list parameters they passed in.
    """
    def fake_pull(repo, branch, runner):
        if pull_raises is not None:
            raise pull_raises
        return None

    def fake_sync(repo, home_dir, paths=None):
        if sync_calls is not None:
            sync_calls.append((repo, home_dir))

    def fake_restart():
        if restart_calls is not None:
            restart_calls.append(True)

    def fake_ensure_venv(paths, repo, log):
        # In tests we never actually create a venv or bootstrap install;
        # the test-provided pip_runner handles the install. Return a
        # fabricated python path; pip_runner won't actually invoke it.
        return paths.root / "venv" / "bin" / "python"

    monkeypatch.setattr(_update, "_git_pull_or_reset", fake_pull)
    monkeypatch.setattr(_update, "_sync_claude_integration", fake_sync)
    monkeypatch.setattr(_update, "_restart_daemons", fake_restart)
    monkeypatch.setattr(_update, "_ensure_venv", fake_ensure_venv)
    # _find_repo must return the test's project_root, not the real
    # metasphere-agents repo (which _find_repo would discover via the
    # editable install). Without this, sync_calls/repo assertions fail
    # because the test git_runner sees the real repo, not the tmp one.
    monkeypatch.setattr(_update, "_find_repo", lambda paths: paths.project_root)


def test_run_update_happy_path(tmp_paths, monkeypatch):
    head_seq = iter(["aaaa1111", "bbbb2222"])
    responses = {
        "rev-parse": "",
        "log": "fix one\nfix two\n",
        "diff": "metasphere/update.py\npyproject.toml\n",
    }

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        return subprocess.CompletedProcess(args, 0, responses.get(args[0], ""), "")

    sent = []
    pip_calls: list[list[str]] = []
    sync_calls: list = []
    restart_calls: list = []

    _patch_update_helpers(
        monkeypatch, sync_calls=sync_calls, restart_calls=restart_calls,
    )

    def fake_pip(args):
        pip_calls.append(args)
        return 0

    cfg = AutoUpdateConfig(enabled=True, notify=True)
    result = _update.run_update(
        paths=tmp_paths,
        cfg=cfg,
        quiet=True,
        git_runner=fake_runner,
        pip_runner=fake_pip,
        test_runner=lambda: True,
        notify_sender=lambda msg: sent.append(msg),
    )
    assert result.ok is True
    assert result.old_hash == "aaaa1111"
    assert result.new_hash == "bbbb2222"
    assert result.commits == 2
    assert result.pip_reinstalled is True
    assert result.tests_passed is True
    assert result.daemons_restarted is True
    assert sync_calls and sync_calls[0][0] == tmp_paths.project_root
    assert restart_calls == [True]
    assert pip_calls and pip_calls[0][:3] == ["-m", "pip", "install"]
    assert sent and "auto-update" in sent[0]
    assert "bbbb2222"[:10] in sent[0]
    # State persisted
    state = _update.load_state(tmp_paths)
    assert state["last_result"]["ok"] is True


def test_run_update_git_pull_failure_skips_restart_and_notifies(tmp_paths, monkeypatch):
    sent = []
    restart_calls: list = []

    _patch_update_helpers(
        monkeypatch,
        pull_raises=RuntimeError("git fetch origin failed (rc=1)"),
        restart_calls=restart_calls,
    )

    def fake_runner(args):
        import subprocess
        return subprocess.CompletedProcess(args, 0, "deadbeef", "")

    result = _update.run_update(
        paths=tmp_paths,
        cfg=AutoUpdateConfig(enabled=True, notify=True),
        quiet=True,
        git_runner=fake_runner,
        notify_sender=lambda msg: sent.append(msg),
    )
    assert result.ok is False
    assert "git pull failed" in result.reason
    # Daemon restart must NOT have run on a failed pull.
    assert restart_calls == []
    assert sent and "FAILED" in sent[0]


def test_run_update_restart_skipped_when_cfg_disables(tmp_paths, monkeypatch):
    head_seq = iter(["aaaa", "bbbb"])
    restart_calls: list = []

    _patch_update_helpers(monkeypatch, restart_calls=restart_calls)

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = _update.run_update(
        paths=tmp_paths,
        cfg=AutoUpdateConfig(enabled=True, notify=False, restart_daemons=False),
        quiet=True,
        git_runner=fake_runner,
        test_runner=lambda: True,
    )
    assert result.ok is True
    assert result.daemons_restarted is False
    assert restart_calls == []


def test_restart_daemons_orders_gateway_last(monkeypatch):
    """Gateway supervises the tmux session this update runs inside;
    it MUST be restarted last so heartbeat+schedule get their turn
    before the caller dies. Regression: 2026-04-21 half-finished deploy."""
    monkeypatch.setattr(_update.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_update.shutil, "which", lambda _: "/usr/bin/systemctl")

    calls: list[tuple[str, ...]] = []

    class _FakeProc:
        def __init__(self, rc: int) -> None:
            self.returncode = rc

    def fake_run(args, **_kwargs):
        assert args[:2] == ["systemctl", "--user"]
        rest = tuple(args[2:])
        calls.append(rest)
        # is-enabled for stale services → nonzero (not enabled); everything else ok.
        if rest and rest[0] == "is-enabled":
            return _FakeProc(1)
        return _FakeProc(0)

    monkeypatch.setattr(_update.subprocess, "run", fake_run)

    _update._restart_daemons()

    restart_order = [r[1] for r in calls if r and r[0] == "restart"]
    assert restart_order == [
        "metasphere-heartbeat",
        "metasphere-schedule",
        "metasphere-gateway",
    ]


def test_run_update_records_state_before_restart(tmp_paths, monkeypatch):
    """State must persist BEFORE _restart_daemons runs. The cron-fired
    auto-update is a cgroup child of metasphere-schedule.service; the
    schedule restart inside _restart_daemons kills the caller before
    any post-restart code can run. Same for tmux-pane-fired updates
    (gateway-restart kills its own supervised tmux). Pre-fix, state
    advanced only on externally-run updates — `metasphere update
    --status` showed stale info indefinitely on an operator host
    between 2026-04-26 (last successful external run) and 2026-05-01."""
    head_seq = iter(["aaaa1111", "bbbb2222"])
    state_during_restart: dict = {}

    def fake_restart_records_state_at_call_time():
        # Capture whether _record_result already wrote state. If state
        # is empty here, the restart raced ahead of the bookkeeping
        # and the bug has regressed.
        state_during_restart.update(_update.load_state(tmp_paths))
        # Simulate the cgroup-kill: raise so post-restart code never
        # runs. The current callsite catches and logs; state must
        # already be recorded at this point.
        raise RuntimeError("simulated cgroup kill from systemctl restart")

    _patch_update_helpers(monkeypatch)
    monkeypatch.setattr(_update, "_restart_daemons",
                        fake_restart_records_state_at_call_time)

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        if args[0] == "log":
            return subprocess.CompletedProcess(args, 0, "fix one\n", "")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, "metasphere/x.py\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = _update.run_update(
        paths=tmp_paths,
        cfg=AutoUpdateConfig(enabled=True, notify=False),
        quiet=True,
        git_runner=fake_runner,
        pip_runner=lambda _args: 0,
        test_runner=lambda: True,
    )
    # Regression assertion: state was recorded BEFORE the restart ran.
    assert state_during_restart, (
        "_record_result must run before _restart_daemons; the schedule "
        "daemon restart kills the caller mid-flight"
    )
    assert state_during_restart["last_result"]["ok"] is True
    assert state_during_restart["last_result"]["new_hash"] == "bbbb2222"
    assert state_during_restart["last_run_at"] > 0
    # And the run still returns its UpdateResult (the restart-warning
    # log is the only side effect of the simulated kill).
    assert result.ok is True


def test_run_update_test_gate_failure(tmp_paths, monkeypatch):
    head_seq = iter(["aaaa", "bbbb"])
    _patch_update_helpers(monkeypatch)

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        if args[0] == "log":
            return subprocess.CompletedProcess(args, 0, "x\n", "")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = _update.run_update(
        paths=tmp_paths,
        cfg=AutoUpdateConfig(enabled=True, notify=False),
        quiet=True,
        git_runner=fake_runner,
        test_runner=lambda: False,
    )
    assert result.ok is False
    assert result.reason == "test gate failed"
    assert result.tests_passed is False


def test_run_update_no_python_changes_skips_pip(tmp_paths, monkeypatch):
    head_seq = iter(["aaaa", "bbbb"])
    pip_calls: list[list[str]] = []
    _patch_update_helpers(monkeypatch)

    # Pre-create the venv python path so run_update sees
    # "venv_existed_before=True" and doesn't mark pip_reinstalled=True
    # just because _ensure_venv bootstrapped a fresh venv.
    fake_venv_python = tmp_paths.root / "venv" / "bin" / "python"
    fake_venv_python.parent.mkdir(parents=True, exist_ok=True)
    fake_venv_python.write_text("#!/usr/bin/env python3\n")
    fake_venv_python.chmod(0o755)

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        if args[0] == "log":
            return subprocess.CompletedProcess(args, 0, "doc fix\n", "")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, "docs/foo.md\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = _update.run_update(
        paths=tmp_paths,
        cfg=AutoUpdateConfig(enabled=True, notify=False),
        quiet=True,
        git_runner=fake_runner,
        pip_runner=lambda args: pip_calls.append(args) or 0,
        test_runner=lambda: True,
    )
    assert result.ok is True
    assert result.pip_reinstalled is False
    assert pip_calls == []


# ---------- helper unit tests ----------

def test_ensure_venv_returns_existing_python_without_recreating(tmp_path, monkeypatch):
    """If $METASPHERE_DIR/venv/bin/python already exists, _ensure_venv
    must return it directly without invoking subprocess — no venv
    recreation, no bootstrap pip install."""
    from types import SimpleNamespace

    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/usr/bin/env python3\n")
    venv_python.chmod(0o755)

    # Paths shim with just .root — that's all _ensure_venv touches.
    paths = SimpleNamespace(root=tmp_path)

    sp_calls: list = []
    def fake_run(*args, **kw):
        sp_calls.append(args[0] if args else None)
        import subprocess as _sp
        return _sp.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(_update.subprocess, "run", fake_run)

    got = _update._ensure_venv(paths, Path("/tmp/repo"), lambda _: None)
    assert got == venv_python
    assert sp_calls == [], "reuse path must not call subprocess.run"


def test_ensure_venv_creates_and_bootstraps_when_missing(tmp_path, monkeypatch):
    """When the venv doesn't exist, _ensure_venv must (1) call
    `python -m venv ...` to create it, (2) install metasphere into it
    via the new venv's pip. Both subprocess calls return rc=0; after
    success the venv python path is returned even though the mock
    didn't create the file (the mock simulates the external behavior)."""
    from types import SimpleNamespace

    paths = SimpleNamespace(root=tmp_path)

    commands: list[list[str]] = []
    def fake_run(cmd, **kw):
        commands.append(cmd)
        # Simulate successful venv creation by actually touching the
        # expected python path on the first call.
        if "-m" in cmd and "venv" in cmd:
            py = tmp_path / "venv" / "bin" / "python"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("#!/usr/bin/env python3\n")
            py.chmod(0o755)
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(_update.subprocess, "run", fake_run)

    got = _update._ensure_venv(paths, Path("/tmp/repo"), lambda _: None)
    assert got == tmp_path / "venv" / "bin" / "python"
    # First call: venv creation.
    assert commands[0][1:3] == ["-m", "venv"]
    # Second call: pip install -e repo into the new venv.
    assert commands[1][1:5] == ["-m", "pip", "install", "-e"]
    assert "/tmp/repo" in commands[1]


def test_ensure_venv_raises_clear_error_if_venv_creation_fails(tmp_path, monkeypatch):
    """If `python -m venv` fails (e.g. python3-venv apt pkg missing),
    _ensure_venv must raise RuntimeError with actionable text so the
    update surfaces it as 'pip install failed: <msg>'."""
    from types import SimpleNamespace
    paths = SimpleNamespace(root=tmp_path)

    def fake_run(cmd, **kw):
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 1, "", "No module named venv")

    monkeypatch.setattr(_update.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="python3-venv"):
        _update._ensure_venv(paths, Path("/tmp/repo"), lambda _: None)


def test_git_pull_or_reset_happy_path():
    """Clean tree → status porcelain empty → pull --ff-only succeeds."""
    import subprocess as _sp
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        if args[0] == "status":
            return _sp.CompletedProcess(args, 0, "", "")  # clean
        return _sp.CompletedProcess(args, 0, "", "")

    _update._git_pull_or_reset(Path("/tmp"), "main", runner)
    # status check happens first, then pull.
    assert calls[0] == ["status", "--porcelain"]
    assert ["pull", "--ff-only", "origin", "main"] in calls


def test_git_pull_or_reset_fallback_to_reset():
    """Clean tree, pull fails → fetch + reset --hard fallback runs."""
    import subprocess as _sp

    def runner(args):
        if args[0] == "status":
            return _sp.CompletedProcess(args, 0, "", "")  # clean
        if args[0] == "pull":
            return _sp.CompletedProcess(args, 1, "", "conflict")
        return _sp.CompletedProcess(args, 0, "", "")

    _update._git_pull_or_reset(Path("/tmp"), "main", runner)


def test_git_pull_or_reset_raises_when_reset_fails():
    import subprocess as _sp

    def runner(args):
        if args[0] == "status":
            return _sp.CompletedProcess(args, 0, "", "")  # clean
        if args[0] == "pull":
            return _sp.CompletedProcess(args, 1, "", "")
        if args[0] == "fetch":
            return _sp.CompletedProcess(args, 1, "", "network down")
        return _sp.CompletedProcess(args, 0, "", "")

    with pytest.raises(RuntimeError, match="git fetch"):
        _update._git_pull_or_reset(Path("/tmp"), "main", runner)


def test_git_pull_or_reset_refuses_on_dirty_tree():
    """Dirty tree → refuse with a RuntimeError listing the paths.

    Regression: 2026-04-16 an auto-triggered `metasphere update` ran
    against a working tree with 10 files of uncommitted WIP, hit a
    pull conflict, fell through to `git reset --hard origin/main`, and
    silently destroyed the WIP (irrecoverable from reflog). The fix
    refuses to proceed when the tree is dirty so the human has to
    commit/stash/discard explicitly.
    """
    import subprocess as _sp
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        if args[0] == "status":
            return _sp.CompletedProcess(
                args, 0,
                " M metasphere/tmux.py\n M metasphere/heartbeat.py\n?? new_file.py\n",
                "",
            )
        return _sp.CompletedProcess(args, 0, "", "")

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        _update._git_pull_or_reset(Path("/tmp"), "main", runner)

    # Critical: pull/fetch/reset must NOT have been called.
    for c in calls:
        assert c[0] not in ("pull", "fetch", "reset"), (
            f"dirty-tree refusal must abort BEFORE touching remote, got {c}"
        )


def test_git_pull_or_reset_refuses_when_status_fails():
    """Unusable `git status` → fail closed (treat as dirty)."""
    import subprocess as _sp

    def runner(args):
        if args[0] == "status":
            return _sp.CompletedProcess(args, 128, "", "not a git repo")
        return _sp.CompletedProcess(args, 0, "", "")

    with pytest.raises(RuntimeError, match="uncommitted|git status"):
        _update._git_pull_or_reset(Path("/tmp"), "main", runner)


def test_find_repo_prefers_editable_install_over_project_root(tmp_path, monkeypatch):
    """When METASPHERE_PROJECT_ROOT points at the data dir (not a git
    repo), _find_repo should discover the actual repo via the editable
    install path (metasphere.__file__)."""
    from metasphere.paths import Paths

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    paths = Paths(root=data_dir, scope=data_dir, project_root=data_dir)
    monkeypatch.delenv("METASPHERE_REPO_ROOT", raising=False)

    # Mock metasphere.__file__ to point inside the fake repo
    import metasphere as _ms
    monkeypatch.setattr(_ms, "__file__", str(repo_dir / "metasphere" / "__init__.py"))

    result = _update._find_repo(paths)
    assert result == repo_dir, (
        f"Expected {repo_dir}, got {result}. _find_repo should resolve "
        f"from editable install when project_root is not a git repo"
    )


def test_find_repo_uses_metasphere_repo_root_env(tmp_path, monkeypatch):
    """METASPHERE_REPO_ROOT env var takes highest precedence."""
    from metasphere.paths import Paths

    env_dir = tmp_path / "env-repo"
    env_dir.mkdir()
    (env_dir / ".git").mkdir()
    monkeypatch.setenv("METASPHERE_REPO_ROOT", str(env_dir))

    paths = Paths(root=tmp_path, scope=tmp_path, project_root=tmp_path)
    assert _update._find_repo(paths) == env_dir


def test_find_repo_falls_back_to_project_root(tmp_path, monkeypatch):
    """When neither env var nor editable install resolves, fall back."""
    from metasphere.paths import Paths

    monkeypatch.delenv("METASPHERE_REPO_ROOT", raising=False)
    import metasphere as _ms
    monkeypatch.setattr(_ms, "__file__", str(tmp_path / "nowhere" / "__init__.py"))

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    paths = Paths(root=data_dir, scope=data_dir, project_root=data_dir)
    assert _update._find_repo(paths) == data_dir


def test_sync_claude_integration_creates_symlinks(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    # Build a fake repo with one skill and one command.
    skill_dir = repo / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo\n")
    # Slash command source lives under templates/claude-commands/
    # (the .claude/ directory is per-machine runtime state and
    # gitignored).
    cmd_dir = repo / "templates" / "claude-commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "foo.md").write_text("cmd\n")

    _update._sync_claude_integration(repo, home)

    skill_link = home / ".claude" / "skills" / "demo"
    user_cmd_link = home / ".claude" / "commands" / "foo.md"
    repo_cmd_link = repo / ".claude" / "commands" / "foo.md"
    assert skill_link.is_symlink()
    assert skill_link.resolve() == skill_dir.resolve()
    # Both user-level and repo-level symlinks land — same source.
    assert user_cmd_link.is_symlink()
    assert user_cmd_link.resolve() == (cmd_dir / "foo.md").resolve()
    assert repo_cmd_link.is_symlink()
    assert repo_cmd_link.resolve() == (cmd_dir / "foo.md").resolve()


def test_sync_claude_integration_respects_user_customized(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    skill_dir = repo / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo\n")

    # Pre-create a real dir with .user-customized marker.
    user_dir = home / ".claude" / "skills" / "demo"
    user_dir.mkdir(parents=True)
    (user_dir / ".user-customized").write_text("keep\n")
    (user_dir / "SKILL.md").write_text("# customized\n")

    _update._sync_claude_integration(repo, home)

    assert user_dir.is_dir() and not user_dir.is_symlink()
    assert (user_dir / ".user-customized").is_file()


# ---------- status ----------

def test_status_text_unconfigured(tmp_paths):
    out = _update.status_text(tmp_paths)
    assert "enabled:" in out
    assert "False" in out
    assert "(not registered)" in out


def test_status_text_after_enable(tmp_paths):
    _update.save_config(AutoUpdateConfig(enabled=True, interval="hourly"), tmp_paths)
    _update.register_job(AutoUpdateConfig(enabled=True, interval="hourly"), tmp_paths)
    out = _update.status_text(tmp_paths)
    assert "True" in out
    assert "0 * * * *" in out
    assert "(not registered)" not in out


# ---------- CLI dispatcher ----------

def test_cli_enable_disable_status(tmp_paths, capsys):
    from metasphere.cli import update as cli_update

    rc = cli_update.main(["--enable"])
    assert rc == 0
    cfg = _update.load_config(tmp_paths)
    assert cfg.enabled is True
    jobs = _sched.load_jobs(tmp_paths)
    assert any(j.id == _update.JOB_ID for j in jobs)

    rc = cli_update.main(["--status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "enabled:" in out

    rc = cli_update.main(["--disable"])
    assert rc == 0
    assert _update.load_config(tmp_paths).enabled is False
    jobs = _sched.load_jobs(tmp_paths)
    assert not any(j.id == _update.JOB_ID for j in jobs)


def test_cli_register_job(tmp_paths):
    from metasphere.cli import update as cli_update
    _update.save_config(AutoUpdateConfig(enabled=True, interval="6h"), tmp_paths)
    rc = cli_update.main(["--register-job"])
    assert rc == 0
    jobs = _sched.load_jobs(tmp_paths)
    j = next(j for j in jobs if j.id == _update.JOB_ID)
    assert j.cron_expr == "0 */6 * * *"


# ---------- template drift detection ----------

def _seed_drift_fixture(tmp_paths, *, drift: bool = True):
    """Create shipped templates + local copies under tmp_paths.

    Returns the repo path used as the source-of-truth root for templates.
    When ``drift=True``, local copies have different content than shipped;
    when False, they're byte-identical.
    """
    repo = tmp_paths.project_root
    (repo / "templates" / "install").mkdir(parents=True)
    (repo / "templates" / "install" / "CLAUDE.md").write_text("SHIPPED USER MANUAL\n")
    (repo / "templates" / "agents" / "orchestrator").mkdir(parents=True)
    (repo / "templates" / "agents" / "orchestrator" / "AGENTS.md").write_text(
        "SHIPPED ORCH AGENTS\n"
    )
    (tmp_paths.root / "CLAUDE.md").write_text(
        "LOCAL USER MANUAL\n" if drift else "SHIPPED USER MANUAL\n"
    )
    orch_dir = tmp_paths.root / "agents" / "@orchestrator"
    orch_dir.mkdir(parents=True)
    (orch_dir / "AGENTS.md").write_text(
        "LOCAL ORCH AGENTS\n" if drift else "SHIPPED ORCH AGENTS\n"
    )
    return repo


def test_detect_drift_finds_drifted_files(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    drifted = _update.detect_drift(paths=tmp_paths, repo=repo)
    labels = sorted(e.label for e in drifted)
    assert labels == ["@orchestrator/AGENTS.md", "~/.metasphere/CLAUDE.md"]


def test_detect_drift_silent_when_identical(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=False)
    drifted = _update.detect_drift(paths=tmp_paths, repo=repo)
    assert drifted == []


def test_detect_drift_skips_missing_local(tmp_paths):
    """Missing local files aren't drift — they're 'not yet seeded'."""
    repo = tmp_paths.project_root
    (repo / "templates" / "install").mkdir(parents=True)
    (repo / "templates" / "install" / "CLAUDE.md").write_text("SHIPPED\n")
    drifted = _update.detect_drift(paths=tmp_paths, repo=repo)
    assert drifted == []


def test_detect_drift_records_line_counts(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    drifted = _update.detect_drift(paths=tmp_paths, repo=repo)
    by_label = {e.label: e for e in drifted}
    assert by_label["~/.metasphere/CLAUDE.md"].src_lines == 1
    assert by_label["~/.metasphere/CLAUDE.md"].dest_lines == 1


def test_run_templates_keep_preserves_local(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    inputs = iter(["k", "k"])
    rc = _update.run_templates_interactive(
        paths=tmp_paths, repo=repo,
        input_fn=lambda _: next(inputs),
    )
    assert rc == 0
    assert (tmp_paths.root / "CLAUDE.md").read_text() == "LOCAL USER MANUAL\n"
    assert (tmp_paths.root / "agents" / "@orchestrator" / "AGENTS.md").read_text() \
        == "LOCAL ORCH AGENTS\n"


def test_run_templates_overwrite_creates_backup(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    inputs = iter(["o", "o"])
    rc = _update.run_templates_interactive(
        paths=tmp_paths, repo=repo,
        input_fn=lambda _: next(inputs),
    )
    assert rc == 0
    assert (tmp_paths.root / "CLAUDE.md").read_text() == "SHIPPED USER MANUAL\n"
    backups = list(tmp_paths.root.glob("CLAUDE.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "LOCAL USER MANUAL\n"


def test_run_templates_diff_reprompts(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    inputs = iter(["d", "k", "k"])
    diff_calls: list[tuple[Path, Path]] = []
    rc = _update.run_templates_interactive(
        paths=tmp_paths, repo=repo,
        input_fn=lambda _: next(inputs),
        diff_runner=lambda local, shipped: diff_calls.append((local, shipped)),
    )
    assert rc == 0
    assert len(diff_calls) == 1
    assert (tmp_paths.root / "CLAUDE.md").read_text() == "LOCAL USER MANUAL\n"


def test_run_templates_no_drift_returns_zero(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=False)
    rc = _update.run_templates_interactive(
        paths=tmp_paths, repo=repo,
        input_fn=lambda _: "k",  # should never be called
    )
    assert rc == 0


def test_run_templates_invalid_then_keep(tmp_paths):
    repo = _seed_drift_fixture(tmp_paths, drift=True)
    inputs = iter(["x", "k", "k"])
    rc = _update.run_templates_interactive(
        paths=tmp_paths, repo=repo,
        input_fn=lambda _: next(inputs),
    )
    assert rc == 0
    assert (tmp_paths.root / "CLAUDE.md").read_text() == "LOCAL USER MANUAL\n"


def test_cli_templates_dispatch(tmp_paths, monkeypatch):
    """`metasphere update --templates` invokes run_templates_interactive."""
    called = {}

    def fake_run_templates(*args, **kwargs):
        called["yes"] = True
        return 0

    monkeypatch.setattr(_update, "run_templates_interactive", fake_run_templates)
    from metasphere.cli import update as cli_update
    rc = cli_update.main(["--templates"])
    assert rc == 0
    assert called.get("yes") is True


# ---------- _sync_hook_paths ----------

def test_sync_hook_paths_rewrites_settings_local_json(tmp_paths, tmp_path):
    """settings.local.json with stale ``scripts/metasphere-posthook``
    references is rewritten to the current ``<venv>/bin/metasphere
    hooks <command>`` form. Idempotent re-run rewrites zero files."""
    # Runtime location: $METASPHERE_DIR/.claude/settings.local.json
    settings = tmp_paths.root / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '{\n'
        '  "permissions": {"allow": ["Bash(git:*)"]},\n'
        '  "hooks": {\n'
        '    "UserPromptSubmit": [{\n'
        '      "matcher": "",\n'
        '      "hooks": [{"type": "command",\n'
        '                  "command": "scripts/metasphere-context"}]\n'
        '    }],\n'
        '    "Stop": [{\n'
        '      "matcher": "",\n'
        '      "hooks": [{"type": "command",\n'
        '                  "command": "scripts/metasphere-posthook"}]\n'
        '    }]\n'
        '  }\n'
        '}\n'
    )
    home = tmp_path / "fake_home"
    home.mkdir()

    rewrote_count = _update._sync_hook_paths(tmp_paths, home)
    assert rewrote_count == 1

    import json as _json
    after = _json.loads(settings.read_text())
    bin_path = str(tmp_paths.root / "venv" / "bin" / "metasphere")
    assert after["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == \
        f"{bin_path} hooks context"
    assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == \
        f"{bin_path} hooks posthook"
    # Permissions block preserved.
    assert after["permissions"]["allow"] == ["Bash(git:*)"]

    # Idempotent: re-run on already-correct file rewrites 0 files.
    rewrote_again = _update._sync_hook_paths(tmp_paths, home)
    assert rewrote_again == 0


def test_sync_hook_paths_no_settings_file_is_noop(tmp_paths, tmp_path):
    """No settings.local.json present (stranger install / fresh host)
    → no-op, no crash, returns 0."""
    home = tmp_path / "fake_home"
    home.mkdir()
    rewrote_count = _update._sync_hook_paths(tmp_paths, home)
    assert rewrote_count == 0
    # Function did not create the file or its parent.
    assert not (tmp_paths.root / ".claude").exists()
    assert not (home / ".claude").exists()


def test_sync_hook_paths_unparseable_settings_is_noop(tmp_paths, tmp_path):
    """Malformed JSON in settings.local.json → no-op (don't blow away
    operator's file)."""
    settings = tmp_paths.root / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ this is not valid json")
    before = settings.read_text()
    home = tmp_path / "fake_home"
    home.mkdir()

    rewrote_count = _update._sync_hook_paths(tmp_paths, home)
    assert rewrote_count == 0
    # File preserved as-is.
    assert settings.read_text() == before


def test_sync_hook_paths_rewrites_repo_and_metasphere_dir(tmp_paths, tmp_path):
    """Both $METASPHERE_DIR/.claude and source-repo/.claude get
    rewritten when both have stale settings.local.json."""
    repo = tmp_paths.project_root
    home = tmp_path / "fake_home"
    home.mkdir()

    runtime_settings = tmp_paths.root / ".claude" / "settings.local.json"
    runtime_settings.parent.mkdir(parents=True)
    runtime_settings.write_text(
        '{"hooks": {"Stop": [{"matcher": "", '
        '"hooks": [{"type": "command", "command": "stale/runtime"}]}]}}\n'
    )

    repo_settings = repo / ".claude" / "settings.local.json"
    repo_settings.parent.mkdir(parents=True)
    repo_settings.write_text(
        '{"hooks": {"Stop": [{"matcher": "", '
        '"hooks": [{"type": "command", "command": "stale/repo"}]}]}}\n'
    )

    rewrote_count = _update._sync_hook_paths(tmp_paths, home, repo=repo)
    assert rewrote_count == 2

    import json as _json
    bin_path = str(tmp_paths.root / "venv" / "bin" / "metasphere")
    for sp in (runtime_settings, repo_settings):
        after = _json.loads(sp.read_text())
        assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == \
            f"{bin_path} hooks posthook"


def test_sync_codex_hooks_installs_user_scope_and_cleans_legacy_layers(tmp_paths, tmp_path):
    """Migration preserves custom hooks while removing duplicate old copies."""
    home = tmp_path / "home"
    home.mkdir()
    user_hooks = home / ".codex" / "hooks.json"
    user_hooks.parent.mkdir()
    user_hooks.write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": "/stale/bin/metasphere hooks context"},
            {"type": "command", "command": "/custom/user-hook"},
        ]}],
    }}))
    legacy = tmp_paths.root / ".codex" / "hooks.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": "/old/metasphere hooks context"},
                {"type": "command", "command": "'/old root/venv/bin/metasphere' hooks context"},
                {"type": "command", "command": "/custom/same-group"},
                {"type": "command", "command": "/opt/acme hooks context"},
            ]},
            {"hooks": [{"type": "command", "command": "/custom/prompt-hook"}]},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "/old/metasphere hooks posthook"}]}],
    }}))

    assert _update._sync_codex_hooks(tmp_paths, home) == 2
    user = json.loads((home / ".codex" / "hooks.json").read_text())
    prompt_handler = user["hooks"]["UserPromptSubmit"][-1]["hooks"][0]
    assert prompt_handler["command"].endswith("metasphere hooks context")
    assert prompt_handler["additionalContextLimit"] == _update.CODEX_CONTEXT_LIMIT
    assert user["hooks"]["UserPromptSubmit"][0]["hooks"] == [
        {"type": "command", "command": "/custom/user-hook"}
    ]
    migrated = json.loads(legacy.read_text())
    assert migrated["hooks"]["UserPromptSubmit"] == [
        {"matcher": "", "hooks": [
            {"type": "command", "command": "/custom/same-group"},
            {"type": "command", "command": "/opt/acme hooks context"},
        ]},
        {"hooks": [{"type": "command", "command": "/custom/prompt-hook"}]}
    ]
    assert "Stop" not in migrated["hooks"]

    # Re-running replaces our group in place without duplicating it.
    assert _update._sync_codex_hooks(tmp_paths, home) == 0


def test_sync_codex_hooks_preserves_legacy_when_user_target_is_malformed(
    tmp_paths, tmp_path
):
    home = tmp_path / "home"
    user = home / ".codex" / "hooks.json"
    user.parent.mkdir(parents=True)
    user.write_text("{invalid")
    legacy = tmp_paths.root / ".codex" / "hooks.json"
    legacy.parent.mkdir(parents=True)
    legacy_body = json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
        "type": "command",
        "command": f"{tmp_paths.root}/venv/bin/metasphere hooks context",
    }]}]}})
    legacy.write_text(legacy_body)

    assert _update._sync_codex_hooks(tmp_paths, home) == 0
    assert user.read_text() == "{invalid"
    assert legacy.read_text() == legacy_body


def test_run_update_restart_after_pip_reinstall(tmp_paths, monkeypatch):
    """Restart_daemons must run AFTER _ensure_venv + the pip-reinstall
    block, not before. Verified via call-order tracking on injected mocks.

    Pre-2026-04-30 the ordering was reversed: restart fired immediately
    after _sync_claude_integration, then _ensure_venv ran. Hosts that
    fell behind on updates would restart their daemons against the
    OLD package, then sit silent until the next update cycle picked
    up the new bytes.
    """
    head_seq = iter(["aaaa1111", "bbbb2222"])
    responses = {
        "rev-parse": "",
        "log": "fix\n",
        "diff": "metasphere/update.py\npyproject.toml\n",
    }

    def fake_runner(args):
        import subprocess
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, next(head_seq), "")
        return subprocess.CompletedProcess(args, 0, responses.get(args[0], ""), "")

    call_order: list[str] = []

    def fake_pull(repo, branch, runner):
        call_order.append("git_pull")

    def fake_sync(repo, home_dir, paths=None):
        call_order.append("sync_claude")

    def fake_ensure_venv(paths, repo, log):
        call_order.append("ensure_venv")
        return paths.root / "venv" / "bin" / "python"

    def fake_pip(args):
        call_order.append("pip_install")
        return 0

    def fake_restart():
        call_order.append("restart_daemons")

    monkeypatch.setattr(_update, "_git_pull_or_reset", fake_pull)
    monkeypatch.setattr(_update, "_sync_claude_integration", fake_sync)
    monkeypatch.setattr(_update, "_restart_daemons", fake_restart)
    monkeypatch.setattr(_update, "_ensure_venv", fake_ensure_venv)
    monkeypatch.setattr(_update, "_find_repo", lambda paths: paths.project_root)

    cfg = AutoUpdateConfig(enabled=True)
    result = _update.run_update(
        paths=tmp_paths,
        cfg=cfg,
        quiet=True,
        git_runner=fake_runner,
        pip_runner=fake_pip,
        test_runner=lambda: True,
    )
    assert result.ok is True

    # Order assertions: restart_daemons must come AFTER ensure_venv
    # and pip_install, not before.
    assert "ensure_venv" in call_order
    assert "pip_install" in call_order
    assert "restart_daemons" in call_order
    assert call_order.index("ensure_venv") < call_order.index("restart_daemons")
    assert call_order.index("pip_install") < call_order.index("restart_daemons")
    # And sync_claude_integration still runs first (for the hook-paths
    # rewrite that's now bundled into it).
    assert call_order.index("sync_claude") < call_order.index("ensure_venv")
