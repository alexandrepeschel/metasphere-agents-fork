"""Tests for metasphere.agents (spawn + wake lifecycle module)."""

from __future__ import annotations

import os
import subprocess
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from metasphere import agents
from metasphere.paths import Paths


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_session_name_for_strips_at_prefix():
    assert agents.session_name_for("@example-cron") == "metasphere-example-cron"
    assert agents.session_name_for("example-cron") == "metasphere-example-cron"


def test_list_agents_finds_created_agents(tmp_paths: Paths):
    a1 = tmp_paths.agents / "@alpha"
    a2 = tmp_paths.agents / "@beta"
    for d in (a1, a2):
        d.mkdir(parents=True)
        (d / "scope").write_text(str(tmp_paths.project_root))
        (d / "parent").write_text("@orchestrator")
        (d / "status").write_text("spawned")
        (d / "spawned_at").write_text("2026-04-07T00:00:00Z")
    (a1 / "MISSION.md").write_text("alpha mission")

    found = agents.list_agents(tmp_paths)
    names = [a.name for a in found]
    assert "@alpha" in names
    assert "@beta" in names


class TestStaleThresholdEnvParse:
    """``METASPHERE_STALE_SESSION_THRESHOLD_SEC`` parse hygiene.

    Read at import time, so a bad value must not crash the import or
    silently corrupt the threshold. Bad inputs warn to stderr and fall
    back to the default (7200).
    """

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "METASPHERE_STALE_SESSION_THRESHOLD_SEC", raising=False
        )
        assert agents._resolve_stale_threshold_sec() == 7200

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("METASPHERE_STALE_SESSION_THRESHOLD_SEC", "")
        assert agents._resolve_stale_threshold_sec() == 7200

    def test_valid_int_parsed(self, monkeypatch):
        monkeypatch.setenv("METASPHERE_STALE_SESSION_THRESHOLD_SEC", "300")
        assert agents._resolve_stale_threshold_sec() == 300

    def test_zero_is_valid(self, monkeypatch):
        # zero means "every alive session is stale" — possibly useful for
        # tests; explicit operator intent, not silent corruption.
        monkeypatch.setenv("METASPHERE_STALE_SESSION_THRESHOLD_SEC", "0")
        assert agents._resolve_stale_threshold_sec() == 0

    def test_non_int_falls_back_with_warning(self, monkeypatch, capsys):
        monkeypatch.setenv(
            "METASPHERE_STALE_SESSION_THRESHOLD_SEC", "two-hours"
        )
        assert agents._resolve_stale_threshold_sec() == 7200
        err = capsys.readouterr().err
        assert "expects an integer" in err
        assert "two-hours" in err

    def test_negative_falls_back_with_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("METASPHERE_STALE_SESSION_THRESHOLD_SEC", "-100")
        assert agents._resolve_stale_threshold_sec() == 7200
        err = capsys.readouterr().err
        assert "non-negative" in err
        assert "-100" in err


def test_is_persistent_requires_mission(tmp_paths: Paths):
    d = tmp_paths.agents / "@persistent"
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("be a thing")
    rec = agents._agent_record_from_dir(d)
    assert agents.is_persistent(rec) is True

    d2 = tmp_paths.agents / "@ephemeral"
    d2.mkdir(parents=True)
    rec2 = agents._agent_record_from_dir(d2)
    assert agents.is_persistent(rec2) is False


# ---------------------------------------------------------------------------
# class sidecar override (2026-05-05 research-monitor zombies)
#
# An agent with MISSION.md AND a ``class=ephemeral`` sidecar must classify
# as ephemeral — the sidecar is the authoritative override. This is the
# load-bearing knob for the research-monitor reclassify: those agents
# keep their MISSION.md (the harness/payload renderer needs it) while
# their lifecycle treatment flips to ephemeral so reap_ephemeral_idle
# can collect their idle panes after the cron payload finishes.
# ---------------------------------------------------------------------------


def test_class_sidecar_ephemeral_overrides_mission_presence(tmp_paths: Paths):
    """MISSION.md present + class=ephemeral sidecar → not persistent.
    This is the exact configuration the morning research-monitors land
    in once their dirs get the class flip."""
    d = tmp_paths.agents / "@brand-mentions"
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("monitor the brand")
    (d / "class").write_text("ephemeral\n")
    rec = agents._agent_record_from_dir(d)
    assert rec.agent_class == "ephemeral"
    assert rec.is_persistent is False


def test_class_sidecar_persistent_overrides_missing_mission(tmp_paths: Paths):
    """No MISSION.md + class=persistent sidecar → persistent. Symmetric
    to the ephemeral override at the AgentRecord dataclass layer.

    Scope note: this asserts the dataclass-level classification only —
    ``wake_persistent`` itself still raises ValueError on MISSION-absent
    before reaching ``rec.is_persistent``, so a ``class=persistent``
    sidecar without MISSION.md cannot wake a tmux REPL today. The
    override matters for downstream consumers (lifecycle reapers,
    counters, list filters) that classify an already-known agent
    dir."""
    d = tmp_paths.agents / "@declared-persistent"
    d.mkdir(parents=True)
    (d / "class").write_text("persistent\n")
    rec = agents._agent_record_from_dir(d)
    assert rec.agent_class == "persistent"
    assert rec.is_persistent is True


def test_class_sidecar_garbage_falls_back_to_mission_heuristic(tmp_paths: Paths):
    """Unrecognised class value (typo, partial write) is ignored — the
    MISSION.md heuristic stays in charge. Prevents a malformed sidecar
    from silently flipping classification."""
    d = tmp_paths.agents / "@typo"
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("mission")
    (d / "class").write_text("EPHMERAL\n")  # typo
    rec = agents._agent_record_from_dir(d)
    assert rec.agent_class is None
    assert rec.is_persistent is True


def test_class_sidecar_empty_falls_back_to_mission_heuristic(tmp_paths: Paths):
    """Empty class file is ignored — same fallback path as garbage."""
    d = tmp_paths.agents / "@empty-class"
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("mission")
    (d / "class").write_text("")
    rec = agents._agent_record_from_dir(d)
    assert rec.agent_class is None
    assert rec.is_persistent is True


# ---------------------------------------------------------------------------
# _resolve_scope (path-doubling regression)
# ---------------------------------------------------------------------------


class TestResolveScope:
    """Regression tests for the _resolve_scope path-doubling bug.

    Before the fix, passing an absolute filesystem path inside
    project_root (e.g. "/home/.../metasphere-agents/scripts") unconditionally
    lstrip'd the leading "/" and prepended project_root, yielding
    <project_root>/<project_root_without_leading_slash>/scripts. The
    workaround everywhere was to spawn with scope="/" so lstrip produced
    "" and project_root / "" == project_root. The fix strips a matching
    project_root prefix first; project-relative forms are unchanged.
    """

    def test_root_slash_resolves_to_project_root(self, tmp_paths: Paths):
        assert agents._resolve_scope("/", tmp_paths.project_root) == tmp_paths.project_root

    def test_project_relative_absolute_resolves_under_project_root(self, tmp_paths: Paths):
        assert (
            agents._resolve_scope("/scripts", tmp_paths.project_root)
            == tmp_paths.project_root / "scripts"
        )

    def test_absolute_project_root_string_resolves_to_project_root(self, tmp_paths: Paths):
        assert (
            agents._resolve_scope(str(tmp_paths.project_root), tmp_paths.project_root)
            == tmp_paths.project_root
        )

    def test_absolute_path_inside_project_root_resolves_correctly(self, tmp_paths: Paths):
        scope = str(tmp_paths.project_root) + "/scripts"
        assert (
            agents._resolve_scope(scope, tmp_paths.project_root)
            == tmp_paths.project_root / "scripts"
        )

    def test_bare_relative_path_resolves_under_project_root(self, tmp_paths: Paths):
        assert (
            agents._resolve_scope("scripts", tmp_paths.project_root)
            == tmp_paths.project_root / "scripts"
        )

    def test_cross_project_absolute_real_dir_resolves_as_is(
        self, tmp_paths: Paths, tmp_path: Path
    ):
        # Bug: spawning @agent on /home/.../other.proj/ from cwd inside
        # metasphere-agents produced scope=<project_root>/home/.../other.proj/.
        # Fix: a leading-slash path that exists on disk and is outside
        # project_root is used verbatim.
        other_proj = tmp_path / "other_proj"
        other_proj.mkdir()
        assert (
            agents._resolve_scope(str(other_proj), tmp_paths.project_root)
            == other_proj
        )

    def test_absolute_nonexistent_path_falls_back_to_project_relative(
        self, tmp_paths: Paths
    ):
        # /scripts (or any leading-slash path that does NOT exist as a real
        # directory) keeps its conventional project-relative interpretation.
        assert (
            agents._resolve_scope("/scripts", tmp_paths.project_root)
            == tmp_paths.project_root / "scripts"
        )

    def test_bare_registered_project_name_resolves_to_project_path(
        self, tmp_paths: Paths, tmp_path: Path, monkeypatch
    ):
        # A bare name like "writing" that matches a registered project should
        # resolve to that project's absolute path, not project_root/writing.
        sibling = tmp_path / "writing"
        sibling.mkdir()
        proj_dir = tmp_paths.root / "projects" / "writing"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.json").write_text(
            json.dumps({"path": str(sibling)})
        )
        monkeypatch.setattr("metasphere.agents._ms_home", lambda: tmp_paths.root)
        assert agents._resolve_scope("writing", tmp_paths.project_root) == sibling

    def test_bare_unregistered_name_resolves_under_project_root(
        self, tmp_paths: Paths, monkeypatch
    ):
        # A bare name with no matching project.json falls through to the
        # project-relative interpretation.
        monkeypatch.setattr("metasphere.agents._ms_home", lambda: tmp_paths.root)
        assert (
            agents._resolve_scope("scripts", tmp_paths.project_root)
            == tmp_paths.project_root / "scripts"
        )

    def test_bare_registered_name_via_registry_when_no_canonical_project_json(
        self, tmp_paths: Paths, tmp_path: Path, monkeypatch
    ):
        # Bug: projects registered before PR #10 (or with project.json
        # still in-repo) lack ``~/.metasphere/projects/<name>/project.json``,
        # so spawn(scope="widget") fell through to project_root/widget
        # and created an empty stub dir. Fallback: consult the registry
        # index (~/.metasphere/projects.json) when canonical project.json
        # is absent.
        sibling = tmp_path / "sources-scraper"
        sibling.mkdir()
        registry = tmp_paths.root / "projects.json"
        registry.write_text(json.dumps([
            {"name": "widget", "path": str(sibling)},
        ]))
        monkeypatch.setattr("metasphere.agents._ms_home", lambda: tmp_paths.root)
        assert agents._resolve_scope("widget", tmp_paths.project_root) == sibling

    def test_canonical_project_json_takes_precedence_over_registry(
        self, tmp_paths: Paths, tmp_path: Path, monkeypatch
    ):
        # If both signals exist and disagree, the canonical project.json
        # wins (it's the source of truth for projects created via
        # ``metasphere project new``).
        canonical_target = tmp_path / "canonical"
        canonical_target.mkdir()
        registry_target = tmp_path / "registry"
        registry_target.mkdir()
        proj_dir = tmp_paths.root / "projects" / "stage"
        proj_dir.mkdir(parents=True)
        (proj_dir / "project.json").write_text(
            json.dumps({"path": str(canonical_target)})
        )
        (tmp_paths.root / "projects.json").write_text(json.dumps([
            {"name": "stage", "path": str(registry_target)},
        ]))
        monkeypatch.setattr("metasphere.agents._ms_home", lambda: tmp_paths.root)
        assert agents._resolve_scope("stage", tmp_paths.project_root) == canonical_target


# ---------------------------------------------------------------------------
# spawn_ephemeral
# ---------------------------------------------------------------------------

def test_spawn_ephemeral_writes_files_and_skips_exec(tmp_paths: Paths, monkeypatch):
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")

    with patch("metasphere.agents.subprocess.Popen") as popen_mock:
        rec = agents.spawn_ephemeral(
            "@spawnling",
            "/sub/",
            "do the thing",
            parent="@orchestrator",
            paths=tmp_paths,
        )
        popen_mock.assert_not_called()

    agent_dir = tmp_paths.agents / "@spawnling"
    assert (agent_dir / "task").read_text().strip() == "do the thing"
    assert (agent_dir / "status").read_text().startswith("spawned:")
    assert (agent_dir / "parent").read_text().strip() == "@orchestrator"
    assert (agent_dir / "harness.md").is_file()
    assert "do the thing" in (agent_dir / "harness.md").read_text()
    assert rec.name == "@spawnling"


def test_spawn_ephemeral_contract_fields_persist_and_render(tmp_paths: Paths, monkeypatch):
    # Contract-first delegation: authority/responsibility/accountability
    # are persisted to the agent dir and rendered into the harness so
    # the spawned agent can see them up front.
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")

    rec = agents.spawn_ephemeral(
        "@contractor",
        "/",
        "fix the thing",
        parent="@orchestrator",
        paths=tmp_paths,
        authority="Read/write metasphere/consolidate.py and its test file only.",
        responsibility="Ship a commit that stops UNOWNED re-escalation after N pings.",
        accountability="I will re-run pytest and grep for noop-pinged-out in events.",
    )

    agent_dir = tmp_paths.agents / "@contractor"
    # Persisted to disk
    assert (agent_dir / "authority").read_text().strip() == (
        "Read/write metasphere/consolidate.py and its test file only."
    )
    assert (agent_dir / "responsibility").read_text().strip() == (
        "Ship a commit that stops UNOWNED re-escalation after N pings."
    )
    assert (agent_dir / "accountability").read_text().strip() == (
        "I will re-run pytest and grep for noop-pinged-out in events."
    )
    # Rendered into the harness as a contract block
    harness = (agent_dir / "harness.md").read_text()
    assert "Delegation Contract" in harness
    assert "Authority (what you MAY do)" in harness
    assert "Responsibility (what you MUST produce)" in harness
    assert "Accountability (how parent will verify)" in harness
    assert "Read/write metasphere/consolidate.py" in harness


def test_spawn_ephemeral_legacy_no_contract_still_works(tmp_paths: Paths, monkeypatch):
    # Back-compat: spawning without any contract fields produces a
    # harness with no Delegation Contract block, and the agent dir has
    # no authority/responsibility/accountability sidecar files.
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")

    agents.spawn_ephemeral(
        "@legacy", "/", "prose task", parent="@orchestrator", paths=tmp_paths,
    )

    agent_dir = tmp_paths.agents / "@legacy"
    harness = (agent_dir / "harness.md").read_text()
    assert "Delegation Contract" not in harness
    assert not (agent_dir / "authority").exists()
    assert not (agent_dir / "responsibility").exists()
    assert not (agent_dir / "accountability").exists()


def test_spawn_ephemeral_harness_uses_canonical_cli_not_legacy_shims(
    tmp_paths: Paths, monkeypatch
):
    # The harness template tells the spawned agent how to talk back to
    # its parent and how to bump task heartbeat. After the bash-shim
    # deprecation those invocations must name the canonical CLI
    # (`metasphere msg send` / `metasphere task update`) — otherwise
    # every ephemeral spawn warns about a deprecated entry point on
    # each communication. Sibling rot to fa8243b (cli/messages.py
    # USAGE_HINTS).
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")
    agents.spawn_ephemeral(
        "@canon",
        "/",
        "task",
        parent="@orchestrator",
        paths=tmp_paths,
        authority="auth",
        responsibility="resp",
        accountability="acct",
    )
    harness = (tmp_paths.agents / "@canon" / "harness.md").read_text()
    # Canonical surface present
    assert "metasphere msg send @.." in harness
    assert "metasphere task update <id>" in harness
    # Legacy shim surface absent
    assert "messages send @.." not in harness
    assert "tasks update <id>" not in harness


def test_spawn_ephemeral_normalizes_unprefixed_name(tmp_paths: Paths, monkeypatch):
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")
    rec = agents.spawn_ephemeral("noprefix", "/", "task", paths=tmp_paths)
    assert rec.name == "@noprefix"
    assert (tmp_paths.agents / "@noprefix").is_dir()


def test_spawn_ephemeral_does_not_pollute_scope_inbox(tmp_paths: Paths, monkeypatch):
    # Regression: a previous version sent an initial `!task` message
    # into the scope inbox alongside the harness, claiming to "let the
    # agent see the task". The harness already embeds the task in its
    # `Your Task` section, so the message was redundant — and at
    # shared scopes (parent and child both at /) it permanently
    # polluted the parent's inbox with an orphan PINNED !task per
    # spawn. The send is now elided.
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")
    agents.spawn_ephemeral(
        "@quiet-spawn", "/", "do work", parent="@orchestrator", paths=tmp_paths,
    )
    # Scope-/ resolves to project_root in spawn_ephemeral.
    inbox = tmp_paths.project_root / ".messages" / "inbox"
    msgs = list(inbox.glob("*.msg")) if inbox.exists() else []
    assert msgs == [], f"spawn should not create scope-inbox messages, got {msgs}"


def test_spawn_ephemeral_codex_runtime_uses_headless_exec(
    tmp_paths: Paths, monkeypatch
):
    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    monkeypatch.setattr(agents.shutil, "which", lambda binary: f"/bin/{binary}")

    with patch("metasphere.agents.subprocess.Popen") as popen:
        popen.return_value.pid = 4242
        rec = agents.spawn_ephemeral(
            "@codex-child",
            "/",
            "execute the contract",
            paths=tmp_paths,
            model="gpt-test",
        )

    cmd = popen.call_args.args[0]
    assert cmd[:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-bypass-hook-trust" not in cmd
    assert "--skip-git-repo-check" in cmd
    assert cmd[-2:] == ["--model", "gpt-test"]
    assert "execute the contract" in cmd[-3]
    assert rec.pid_file is not None
    assert rec.pid_file.read_text().strip() == "4242"


def test_spawn_ephemeral_probes_only_selected_runtime(
    tmp_paths: Paths, monkeypatch
):
    monkeypatch.setenv("METASPHERE_AGENT_RUNTIME", "codex")
    probes: list[str] = []

    def fake_which(binary: str) -> None:
        probes.append(binary)
        return None

    monkeypatch.setattr(agents.shutil, "which", fake_which)
    with patch("metasphere.agents.subprocess.Popen") as popen:
        rec = agents.spawn_ephemeral(
            "@codex-missing", "/", "task", paths=tmp_paths
        )

    assert probes == ["codex"]
    popen.assert_not_called()
    assert rec.pid_file is None


def test_headless_runtime_command_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("METASPHERE_AGENT_RUNTIME", raising=False)
    runtime, cmd = agents._headless_runtime_command("harness", model="sonnet")
    assert runtime == "claude"
    assert cmd == [
        "claude",
        "-p",
        "harness",
        "--dangerously-skip-permissions",
        "--model",
        "sonnet",
    ]


@pytest.mark.parametrize(
    "bad_name",
    [
        "--help",
        "-h",
        "@--help",
        "@-typo",
        "",
        "   ",
        "with/slash",
        "with\\backslash",
        "with\x00null",
    ],
)
def test_spawn_ephemeral_rejects_invalid_names(
    tmp_paths: Paths, monkeypatch, bad_name: str
):
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")
    with pytest.raises(ValueError):
        agents.spawn_ephemeral(
            bad_name, "/", "task", parent="@orchestrator", paths=tmp_paths,
        )
    # No ghost dir under the bad name.
    assert not (tmp_paths.agents / bad_name).exists()


# ---------------------------------------------------------------------------
# wake_persistent
# ---------------------------------------------------------------------------

def _make_persistent(tmp_paths: Paths, name: str = "@waker") -> Path:
    d = tmp_paths.agents / name
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("mission")
    (d / "scope").write_text(str(tmp_paths.project_root))
    return d


def test_wake_persistent_rejects_non_persistent(tmp_paths: Paths):
    (tmp_paths.agents / "@nope").mkdir(parents=True)
    with pytest.raises(ValueError, match="not a persistent agent"):
        agents.wake_persistent("@nope", paths=tmp_paths)


def test_wait_for_ready_accepts_codex_yolo_marker(monkeypatch):
    monkeypatch.setattr(
        agents, "_capture_pane", lambda session: "permissions: YOLO mode"
    )
    assert agents._wait_for_ready("metasphere-worker", timeout_s=1) is True


def test_wake_persistent_cold_start_runs_tmux_new_session(tmp_paths: Paths):
    _make_persistent(tmp_paths)
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        # Pretend has-session returns nonzero (no session yet) and ready
        # marker is present so the wait loop exits fast.
        cp = MagicMock()
        if "has-session" in cmd:
            cp.returncode = 1
            cp.stdout = ""
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        else:
            cp.returncode = 0
            cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@waker", paths=tmp_paths)

    new_session_calls = [c for c in calls if "new-session" in c]
    assert new_session_calls, f"expected tmux new-session call, got {calls}"
    nc = new_session_calls[0]
    assert "-s" in nc
    assert "metasphere-waker" in nc

    # PR #21: env_export for the pane must include the feedback-modal
    # disable flags so the claude TUI doesn't show "How is Claude doing
    # this session?" for metasphere-spawned agent REPLs (the modal
    # captures input and caused stuck-paste accumulation 2026-04-16).
    send_keys_calls = [c for c in calls if "send-keys" in c]
    env_exports = [
        c for c in send_keys_calls
        if any("METASPHERE_AGENT_ID" in part for part in c)
    ]
    assert env_exports, f"expected an env-export send-keys call, got {send_keys_calls}"
    env_line = " ".join(env_exports[0])
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1" in env_line
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in env_line


def test_wake_persistent_cold_start_refreshes_last_active(tmp_paths: Paths):
    """Cold-starting a persistent agent must refresh its ``last_active``
    sidecar. ``reap_dormant`` reads that file (preferring it over tmux
    activity) to decide whether a session has gone idle past
    ``dormancy_max_idle_seconds``. If wake_persistent leaves a stale
    sidecar in place, the just-woken session gets reaped within a single
    dormancy interval — the exact regression 6b05ad5 closed for the
    @writing-lead wake on 2026-05-14.
    """
    d = _make_persistent(tmp_paths)
    # Seed a stale last_active that would trip reap_dormant immediately.
    stale = "2020-01-01T00:00:00+00:00"
    (d / "last_active").write_text(stale)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        if "has-session" in cmd:
            cp.returncode = 1
            cp.stdout = ""
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        else:
            cp.returncode = 0
            cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@waker", paths=tmp_paths)

    refreshed = (d / "last_active").read_text().strip()
    assert refreshed != stale, (
        "wake_persistent cold-start must touch last_active; stale value "
        "left in place would let reap_dormant kill the just-woken session"
    )
    # Idle should now be near-zero, well under any sane reap threshold.
    idle = agents._last_active_idle_seconds(d)
    assert idle is not None and idle < 60, (
        f"expected fresh idle (<60s) post-wake, got {idle!r}"
    )


def test_wake_persistent_project_scoped_uses_project_cwd(tmp_paths: Paths, tmp_path: Path):
    # When a project-scoped agent has no explicit `scope` file but its
    # `project` file names a registered project, the tmux new-session
    # must use the project's filesystem path as cwd — not the harness
    # project_root. Otherwise the agent inherits the wrong
    # .claude/settings.local.json and crashes on startup.
    proj_path = tmp_path / "example-proj"
    proj_path.mkdir(parents=True)
    # Seed projects.json so get_project() resolves it. Post-PR #11,
    # load_project reads the canonical location only, so we also seed
    # ``~/.metasphere/projects/example-proj/project.json``.
    (tmp_paths.root / "projects.json").write_text(json.dumps(
        [{"name": "example-proj", "path": str(proj_path), "registered": "x"}]
    ))
    (proj_path / ".metasphere").mkdir(parents=True)
    canonical_pf = tmp_paths.projects / "example-proj" / "project.json"
    canonical_pf.parent.mkdir(parents=True, exist_ok=True)
    canonical_pf.write_text(json.dumps(
        {"name": "example-proj", "path": str(proj_path), "goal": "", "members": []}
    ))
    # Create the project-scoped agent dir (not in global agents/).
    agent_dir = tmp_paths.projects / "example-proj" / "agents" / "@worker"
    agent_dir.mkdir(parents=True)
    (agent_dir / "MISSION.md").write_text("mission")
    (agent_dir / "project").write_text("example-proj")
    # NOTE: intentionally no `scope` file — this is the common case.

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        cp = MagicMock()
        if "has-session" in cmd:
            cp.returncode = 1
            cp.stdout = ""
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        else:
            cp.returncode = 0
            cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@worker", paths=tmp_paths)

    new_session = next((c for c in calls if "new-session" in c), None)
    assert new_session, f"expected tmux new-session, got {calls}"
    # -c <cwd> should point at the project path, not the harness.
    cwd_idx = new_session.index("-c") + 1
    assert new_session[cwd_idx] == str(proj_path), (
        f"expected cwd={proj_path}, got {new_session[cwd_idx]}"
    )


def test_wake_persistent_threads_ephemeral_class_into_respawn_cmd(tmp_paths: Paths):
    """An agent with a ``class=ephemeral`` sidecar must get the
    ephemeral respawn shape (no while-true loop) sent to its tmux pane.
    This is the cron-fire integration premise: schedule fires, wake_persistent
    cold-starts the session, claude runs once, exits cleanly, and the pane
    falls back to bash for reap_ephemeral_idle to pick up. The bug closed:
    persistent-shape respawn loops a clean exit-self into a /exit-menu stall.
    """
    d = _make_persistent(tmp_paths, "@ephemeral-cron")
    (d / "class").write_text("ephemeral\n")

    send_keys_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 1  # cold start
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        elif "send-keys" in cmd:
            cp.returncode = 0
            send_keys_calls.append(list(cmd))
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@ephemeral-cron", paths=tmp_paths)

    # The respawn command is the send-keys call whose argv contains
    # ``claude --dangerously-skip-permissions``. Find it and assert
    # ephemeral shape: no infinite respawn loop, no restart marker.
    respawn_calls = [
        c for c in send_keys_calls
        if any("claude --dangerously-skip-permissions" in part for part in c)
    ]
    assert respawn_calls, (
        f"expected a send-keys call carrying the claude respawn cmd; "
        f"got {send_keys_calls}"
    )
    respawn_cmd = " ".join(respawn_calls[0])
    assert "while true" not in respawn_cmd, (
        "ephemeral-classed agent must NOT receive a while-true respawn loop"
    )
    assert "restart_pending" not in respawn_cmd, (
        "ephemeral-classed agent must NOT write a restart marker (the "
        "watchdog injects continuations on those, which is the failure "
        "mode this fix closes)"
    )
    assert "exec bash" in respawn_cmd, (
        "ephemeral-classed agent must drop to interactive bash on exit "
        "so reap_ephemeral_idle picks up the idle pane"
    )


def test_wake_persistent_default_class_keeps_loop(tmp_paths: Paths):
    """Backward-compat: an agent with no class sidecar (MISSION.md only)
    keeps the persistent-shape respawn loop. Ensures the class change
    doesn't silently downgrade existing persistent agents."""
    _make_persistent(tmp_paths, "@plain-persistent")

    send_keys_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 1
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        elif "send-keys" in cmd:
            cp.returncode = 0
            send_keys_calls.append(list(cmd))
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@plain-persistent", paths=tmp_paths)

    respawn_calls = [
        c for c in send_keys_calls
        if any("claude --dangerously-skip-permissions" in part for part in c)
    ]
    assert respawn_calls
    respawn_cmd = " ".join(respawn_calls[0])
    assert "while true" in respawn_cmd
    assert "restart_pending.@plain-persistent.json" in respawn_cmd


def test_wake_persistent_already_alive_injects_task(tmp_paths: Paths):
    _make_persistent(tmp_paths)
    submitted: list[tuple[str, str]] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        else:
            cp.returncode = 0
        return cp

    def fake_tmux_submit(session: str, message: str, **kwargs) -> bool:
        submitted.append((session, message, kwargs))
        return True

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.agents._tmux_submit", side_effect=fake_tmux_submit):
        agents.wake_persistent("@waker", first_task="hello", paths=tmp_paths)

    assert submitted, "expected _tmux_submit call"
    assert submitted[0][0] == "metasphere-waker"
    assert "hello" in submitted[0][1]
    # Wakes must pass escape_prefix=False — Escape on idle panes triggers
    # Claude Code's session-rating dialog / Rewind menu, racing with our
    # typed content + C-m submit (2026-04-20 buffered-wake incidents).
    assert submitted[0][2].get("escape_prefix") is False, (
        f"wake must pass escape_prefix=False, got {submitted[0][2]}"
    )


def test_wake_persistent_returns_delivered_true_on_successful_inject(tmp_paths: Paths):
    """Issue #106: wake_persistent must report whether the first_task
    actually landed on the pane so dispatch_to_agent can decide to fall
    through to inbox delivery. Success case: tmux submit returns True →
    delivered is True."""
    _make_persistent(tmp_paths)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.returncode = 0 if "has-session" in cmd else 0
        cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.agents._tmux_submit", return_value=True):
        rec, delivered = agents.wake_persistent(
            "@waker", first_task="hello", paths=tmp_paths,
        )

    assert rec.name == "@waker"
    assert delivered is True


def test_wake_persistent_returns_delivered_false_on_silent_submit_failure(tmp_paths: Paths):
    """Issue #106: when _tmux_submit silently returns False (session
    vanished mid-fire, defer-if-busy, tmux binary gone), wake_persistent
    must propagate that as ``delivered=False`` so the schedule daemon
    falls through to inbox-only delivery instead of stamping
    last_fired_at and losing the task forever."""
    _make_persistent(tmp_paths)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.returncode = 0  # session alive
        cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.agents._tmux_submit", return_value=False):
        rec, delivered = agents.wake_persistent(
            "@waker", first_task="hello", paths=tmp_paths,
        )

    assert rec.name == "@waker"
    assert delivered is False


def test_wake_persistent_returns_delivered_true_when_no_first_task(tmp_paths: Paths):
    """With ``first_task=None`` there's nothing to deliver, so the
    delivery bool is vacuously True (the session is up, that's all the
    caller asked for)."""
    _make_persistent(tmp_paths)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = ""
        cp.stderr = ""
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.agents._tmux_submit", return_value=False):
        rec, delivered = agents.wake_persistent("@waker", paths=tmp_paths)

    assert rec.name == "@waker"
    assert delivered is True


# ---------------------------------------------------------------------------
# wake_persistent stale-kill hardening (mirrors the reap_dormant interlock):
# the wake-path stale-session kill must use the same false-reap protections
# reap_dormant already carries, or a busy-but-quiet session gets nuked on the
# next wake. Regression source: 2026-07-05 @worker killed at "idle=7648s"
# (tmux output-only signal) mid-compute while running silent CPU probes.
# ---------------------------------------------------------------------------

def test_wake_persistent_stale_kills_when_idle_and_not_generating(tmp_paths: Paths):
    """Baseline preserved: a genuinely idle session (both activity signals
    past threshold, pane shows no generation indicator) is still killed and
    cold-started. The last_active/liveness hardening must not weaken this."""
    _make_persistent(tmp_paths, "@stale-waker")
    d = tmp_paths.agents / "@stale-waker"
    # Input-side signal ancient → idle ≫ threshold.
    (d / "last_active").write_text("2020-01-01T00:00:00+00:00")

    kill_sessions: list[str] = []
    new_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # tmux idle also ancient
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"  # ready marker, no gen indicator
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        elif "new-session" in cmd:
            cp.returncode = 0
            new_sessions.append(cmd[cmd.index("-s") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@stale-waker", paths=tmp_paths)

    assert kill_sessions == ["metasphere-stale-waker"], (
        f"idle non-generating session must still be reaped, got {kill_sessions}"
    )
    assert "metasphere-stale-waker" in new_sessions, (
        f"a fresh session must be cold-started after the kill, got {new_sessions}"
    )


def test_wake_persistent_failed_stale_kill_does_not_cold_start(
    tmp_paths: Paths,
):
    """A failed stale-session kill must not claim or attempt replacement.

    Starting another tmux session with the same name cannot succeed while the
    original remains alive. Returning ``delivered=False`` lets message callers
    retain the task in the durable inbox instead of falsely reporting a wake.
    """
    _make_persistent(tmp_paths, "@kill-failed")
    d = tmp_paths.agents / "@kill-failed"
    (d / "last_active").write_text("2020-01-01T00:00:00+00:00")
    (d / "status").write_text("active: persistent session\n")
    new_sessions: list[str] = []
    events: list[tuple[str, str]] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # still alive after failed kill
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        elif "kill-session" in cmd:
            cp.returncode = 1
            cp.stderr = "simulated tmux failure"
        elif "new-session" in cmd:
            cp.returncode = 0
            new_sessions.append(cmd[cmd.index("-s") + 1])
        else:
            cp.returncode = 0
        return cp

    def fake_log(event_type, message, **kwargs):
        events.append((event_type, message))

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.agents.log_event", side_effect=fake_log):
        _rec, delivered = agents.wake_persistent(
            "@kill-failed", first_task="durable task", paths=tmp_paths,
        )

    assert delivered is False
    assert new_sessions == []
    assert (d / "status").read_text() == "active: persistent session\n"
    assert any(kind == "agent.session.cleanup_failed" for kind, _ in events)
    assert not any("killed before cold-start" in msg for _, msg in events)


def test_wake_persistent_fresh_last_active_survives_stale_tmux(tmp_paths: Paths):
    """The fix: a session whose tmux OUTPUT has been silent past the
    threshold (a long quiet compute) but whose input-side ``last_active`` is
    fresh must NOT be killed — deliver into it. Previously the wake path read
    tmux-only and nuked it (2026-07-05 @worker)."""
    _make_persistent(tmp_paths, "@busy-quiet")
    # Input-side signal fresh even though tmux output activity is ancient.
    agents.touch_last_active("@busy-quiet", paths=tmp_paths)

    kill_sessions: list[str] = []
    new_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # tmux idle ancient → would trip a tmux-only path
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        elif "new-session" in cmd:
            cp.returncode = 0
            new_sessions.append(cmd[cmd.index("-s") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        rec, delivered = agents.wake_persistent("@busy-quiet", paths=tmp_paths)

    assert kill_sessions == [], (
        f"fresh last_active must prevent the stale-kill, got {kill_sessions}"
    )
    assert new_sessions == [], (
        f"a preserved session must not be cold-started, got {new_sessions}"
    )
    assert delivered is True


def test_wake_persistent_vetoes_generating_session(tmp_paths: Paths):
    """Liveness interlock on the wake path: even when BOTH activity signals
    are past the threshold, a pane actively generating ('esc to interrupt')
    is not killed — the exact @worker case: a silent multi-hour compute
    that fires no hook and writes nothing tmux counts as activity, yet is
    doing real work."""
    _make_persistent(tmp_paths, "@gen-waker")
    d = tmp_paths.agents / "@gen-waker"
    (d / "last_active").write_text("2020-01-01T00:00:00+00:00")  # input-side also stale

    kill_sessions: list[str] = []
    new_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # tmux idle ancient
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "crunching numbers\n  esc to interrupt"
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        elif "new-session" in cmd:
            cp.returncode = 0
            new_sessions.append(cmd[cmd.index("-s") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        agents.wake_persistent("@gen-waker", paths=tmp_paths)

    assert kill_sessions == [], (
        f"a generating session must not be killed, got {kill_sessions}"
    )
    assert new_sessions == [], f"no cold-start on veto, got {new_sessions}"


def test_wake_persistent_probe_exception_fails_open_to_kill(tmp_paths: Paths):
    """Core-loop guard: if ``agent_liveness`` violates its never-raise
    contract, the wake-path stale-kill must fail OPEN (treat as
    not-generating) and still cold-start, rather than propagate and wedge
    the wake for this agent."""
    _make_persistent(tmp_paths, "@boom-waker")
    d = tmp_paths.agents / "@boom-waker"
    (d / "last_active").write_text("2020-01-01T00:00:00+00:00")

    kill_sessions: list[str] = []
    new_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "bypass permissions on"
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        elif "new-session" in cmd:
            cp.returncode = 0
            new_sessions.append(cmd[cmd.index("-s") + 1])
        else:
            cp.returncode = 0
        return cp

    def boom(*_a, **_kw):
        raise RuntimeError("probe contract violated")

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.liveness.agent_liveness", side_effect=boom):
        agents.wake_persistent("@boom-waker", paths=tmp_paths)

    assert kill_sessions == ["metasphere-boom-waker"], (
        f"probe fault must fail open to the kill, got {kill_sessions}"
    )
    assert "metasphere-boom-waker" in new_sessions, (
        f"cold-start must still run after the fail-open kill, got {new_sessions}"
    )


# ---------------------------------------------------------------------------
# gc_dormant
# ---------------------------------------------------------------------------

def test_gc_dormant_returns_idle_agents(tmp_paths: Paths):
    _make_persistent(tmp_paths, "@idleone")
    _make_persistent(tmp_paths, "@freshone")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # both alive
        elif "display-message" in cmd:
            session = cmd[cmd.index("-t") + 1]
            now_minus_two_days = "1000000000"  # very old
            now_recent = str(2_000_000_000)  # future-ish, idle≈0
            cp.returncode = 0
            cp.stdout = now_minus_two_days if "idleone" in session else now_recent
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        dormant = agents.gc_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@idleone" in dormant
    assert "@freshone" not in dormant


# ---------------------------------------------------------------------------
# reap_dormant (session hygiene: idle-TTL dormancy)
# ---------------------------------------------------------------------------

def test_reap_dormant_kills_idle_session_preserves_persona(tmp_paths: Paths):
    """A persistent agent whose tmux session has been idle longer than
    ``max_idle_seconds`` is:
      - transitioned to ``status: dormant: ...``
      - tmux session killed
      - MISSION / SOUL / LEARNINGS / contract sidecars preserved on disk
    """
    _make_persistent(tmp_paths, "@idle-boss")
    d = tmp_paths.agents / "@idle-boss"
    # Seed persona + contract sidecars that MUST survive the reap.
    (d / "SOUL.md").write_text("soul content")
    (d / "LEARNINGS.md").write_text("learnings content")
    (d / "HEARTBEAT.md").write_text("heartbeat content")
    (d / "authority").write_text("may read")
    (d / "responsibility").write_text("ship")
    (d / "accountability").write_text("verify")
    (d / "harness.md").write_text("# Agent: @idle-boss")
    (d / "status").write_text("active: persistent session\n")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # very old → idle ≫ TTL
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@idle-boss" in reaped
    assert kill_sessions == ["metasphere-idle-boss"], (
        f"expected exactly metasphere-idle-boss killed, got {kill_sessions}"
    )
    status = (d / "status").read_text().strip()
    assert status.startswith("dormant:"), f"expected dormant: status, got {status!r}"
    assert "idle" in status and "s" in status

    # Persona + contract files preserved — next ``metasphere agent wake``
    # must restart cleanly from these.
    for preserved in (
        "MISSION.md", "SOUL.md", "LEARNINGS.md", "HEARTBEAT.md",
        "authority", "responsibility", "accountability", "harness.md",
    ):
        assert (d / preserved).exists(), (
            f"persona/contract file {preserved} must survive reap"
        )


def test_reap_dormant_failed_kill_preserves_active_status(tmp_paths: Paths):
    """A failed tmux kill is not dormancy and must remain retryable.

    Publishing ``dormant:`` while the persistent session is still alive hides
    an active agent behind terminal durable state and suppresses future crash
    handling. The daemon must report only confirmed kills as transitioned.
    """
    _make_persistent(tmp_paths, "@kill-failed")
    d = tmp_paths.agents / "@kill-failed"
    (d / "status").write_text("active: persistent session\n")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "kill-session" in cmd:
            cp.returncode = 1
            cp.stderr = "simulated tmux failure"
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(
            paths=tmp_paths, max_idle_seconds=3600
        )

    assert reaped == []
    assert (d / "status").read_text() == "active: persistent session\n"


def test_reap_dormant_skips_fresh_persistent_and_ephemerals(tmp_paths: Paths):
    """Only idle persistent agents are transitioned. Fresh persistent
    agents (idle < TTL) stay alive with unchanged state, and ephemerals
    (no MISSION.md) are never swept by reap_dormant regardless of idle.
    """
    _make_persistent(tmp_paths, "@fresh-boss")
    # Ephemeral: exists in agents/ but has no MISSION.md.
    ephemeral_dir = tmp_paths.agents / "@ephemeral-one"
    ephemeral_dir.mkdir(parents=True)
    (ephemeral_dir / "scope").write_text(str(tmp_paths.project_root))
    (ephemeral_dir / "status").write_text("spawned: do stuff\n")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = str(2_000_000_000)  # future-ish → idle ≈ 0
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert reaped == [], f"expected no reaps, got {reaped}"
    assert kill_sessions == [], f"expected no kills, got {kill_sessions}"
    # _make_persistent doesn't seed a status file; reap_dormant must not
    # have created one for the fresh agent (no transition fired).
    fresh_status = tmp_paths.agents / "@fresh-boss" / "status"
    if fresh_status.exists():
        assert "dormant" not in fresh_status.read_text()
    # Ephemeral status untouched (no MISSION.md → not even considered).
    assert "spawned:" in (ephemeral_dir / "status").read_text()


def test_reap_dormant_no_op_when_session_already_dead(tmp_paths: Paths):
    """If the persistent agent has no live tmux session, reap_dormant
    must NOT write a dormant status — there's nothing to transition."""
    _make_persistent(tmp_paths, "@dead-one")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 1  # NOT alive
        elif "kill-session" in cmd:
            cp.returncode = 1
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=1)

    assert reaped == []


def test_reap_dormant_vetoes_generating_agent(tmp_paths: Paths):
    """Liveness interlock: a persistent agent idle past the TTL whose pane
    is actively generating (Claude footer 'esc to interrupt') is NOT reaped.
    Guards the edge where every hook signal went quiet >TTL yet the pane is
    still producing output (a single multi-hour tool call, a wedged-but-
    printing REPL) — today that session would be killed mid-work."""
    _make_persistent(tmp_paths, "@busy-boss")
    d = tmp_paths.agents / "@busy-boss"
    (d / "status").write_text("active: persistent session\n")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # very old → idle ≫ TTL
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "running a long tool\n  esc to interrupt"
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert reaped == [], f"generating agent must not be reaped, got {reaped}"
    assert kill_sessions == [], f"no kill expected on veto, got {kill_sessions}"
    status = (d / "status").read_text().strip()
    assert "dormant" not in status, (
        f"veto must leave status untouched, got {status!r}"
    )


def test_reap_dormant_still_reaps_non_generating_agent(tmp_paths: Paths):
    """The interlock is narrow: it only vetoes GENERATING. An idle pane past
    the TTL (cold probe, no indicator → UNKNOWN, not GENERATING) still reaps,
    so the interlock does not weaken the existing dormancy GC."""
    _make_persistent(tmp_paths, "@quiet-boss")
    d = tmp_paths.agents / "@quiet-boss"
    (d / "status").write_text("active: persistent session\n")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # very old → idle ≫ TTL
        elif "capture-pane" in cmd:
            cp.returncode = 0
            cp.stdout = "quiet idle pane\n❯ "  # no generation indicator
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@quiet-boss" in reaped, f"idle agent must still reap, got {reaped}"
    assert kill_sessions == ["metasphere-quiet-boss"], (
        f"expected quiet-boss killed, got {kill_sessions}"
    )


def test_reap_dormant_guards_probe_exception_and_still_reaps(tmp_paths: Paths):
    """Belt-and-suspenders: the interlock call site is guarded, so if
    ``agent_liveness`` ever violates its never-raise contract, the exception
    does NOT propagate and crash the reap sweep for every other agent — it
    fails OPEN to the existing reap (generating treated as False). The idle
    agent is still collected. Core-loop blast-radius guard (PR #209 review)."""
    _make_persistent(tmp_paths, "@boom-boss")
    d = tmp_paths.agents / "@boom-boss"
    (d / "status").write_text("active: persistent session\n")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # very old → idle ≫ TTL
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    def boom(*_a, **_kw):
        raise RuntimeError("probe contract violated")

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run), \
         patch("metasphere.liveness.agent_liveness", side_effect=boom):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@boom-boss" in reaped, (
        f"probe crash must fail-open to reap, not propagate, got {reaped}"
    )
    assert kill_sessions == ["metasphere-boom-boss"], (
        f"expected boom-boss killed, got {kill_sessions}"
    )


# ---------------------------------------------------------------------------
# touch_last_active + last_active idle pathway
#
# Directive (2026-04-25, evt-1777144985427): reap_dormant must derive
# idle from input the agent actually processed (UserPromptSubmit, Stop,
# telegram-inject, heartbeat-tick), not from tmux session_activity which
# misses turns the model produced no terminal output for. AND
# @orchestrator is unconditionally exempt from time-based reap.
# ---------------------------------------------------------------------------


def test_touch_last_active_writes_iso_timestamp(tmp_paths: Paths):
    """The helper writes a parseable UTC ISO timestamp, creates the
    agent dir if missing, and never raises on a clean call."""
    agents.touch_last_active("@touched", paths=tmp_paths)
    p = tmp_paths.agents / "@touched" / "last_active"
    assert p.is_file()
    raw = p.read_text(encoding="utf-8").strip()
    assert raw.endswith("Z"), f"expected UTC ISO with Z suffix, got {raw!r}"
    # Round-trips through fromisoformat (the read path's parser).
    import datetime as _dt
    parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_touch_last_active_normalises_missing_at_prefix(tmp_paths: Paths):
    """Hook callers may pass agent ids with or without the @ prefix;
    the file must land in the canonical @-prefixed dir either way."""
    agents.touch_last_active("bare-name", paths=tmp_paths)
    assert (tmp_paths.agents / "@bare-name" / "last_active").is_file()


def test_touch_last_active_writes_to_project_scoped_dir_when_present(
    tmp_paths: Paths,
):
    """Project-scoped agents (e.g. @install-lead under metasphere-agents)
    must have last_active written into their project-scoped dir, not the
    global ~/.metasphere/agents/<id>/. Witnessed bug (2026-05-09): the
    helper unconditionally wrote to the global path, creating a ghost
    dir with no MISSION.md that consolidate._gc_ephemeral_agents()
    classified as a dead ephemeral and deleted — five minutes later the
    next hook recreated it. Infinite churn cycle."""
    proj_agent = tmp_paths.projects / "testproj" / "agents" / "@scoped"
    proj_agent.mkdir(parents=True)
    (proj_agent / "MISSION.md").write_text("be a thing")

    agents.touch_last_active("@scoped", paths=tmp_paths)

    assert (proj_agent / "last_active").is_file(), (
        "project-scoped dir must receive the timestamp"
    )
    assert not (tmp_paths.agents / "@scoped").exists(), (
        "global ghost dir must NOT be created when project-scoped dir "
        "already exists; that's the GC-churn vector"
    )


def test_reap_dormant_skips_when_last_active_is_fresh(tmp_paths: Paths):
    """Hook-event refresh blocks the reap. Even if tmux reports the
    session has been idle for hours (the session_activity-only path
    that produced evt-1777144985427), a recent touch_last_active call
    keeps the agent alive — that is the whole point of the input-side
    timestamp."""
    _make_persistent(tmp_paths, "@hook-touched")
    # Simulate the four hook signal paths: each calls touch_last_active.
    agents.touch_last_active("@hook-touched", paths=tmp_paths)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            # tmux says ancient — without the input-side override this
            # would cross the threshold and reap the session.
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@hook-touched" not in reaped, (
        f"fresh last_active must block reap; got {reaped}"
    )
    status = tmp_paths.agents / "@hook-touched" / "status"
    if status.exists():
        assert "dormant" not in status.read_text()


def test_reap_dormant_falls_back_to_tmux_when_no_last_active(tmp_paths: Paths):
    """Backward compat: agents whose dir predates the last_active file
    still get reaped via the tmux session_activity signal."""
    _make_persistent(tmp_paths, "@legacy-idle")
    # No touch_last_active call: simulates an agent dir that predates
    # the input-side timestamp.

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # ancient
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@legacy-idle" in reaped, (
        f"missing last_active must fall through to tmux signal; got {reaped}"
    )


def test_reap_dormant_unparseable_last_active_falls_back_to_tmux(tmp_paths: Paths):
    """A corrupt/empty last_active file must not pin an agent alive
    forever — the parser returns None and the tmux fallback decides."""
    _make_persistent(tmp_paths, "@corrupt-active")
    d = tmp_paths.agents / "@corrupt-active"
    (d / "last_active").write_text("not-a-timestamp\n")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@corrupt-active" in reaped


def test_reap_dormant_exempts_orchestrator_unconditionally(tmp_paths: Paths):
    """@orchestrator is never time-reaped. Repro: 2026-04-25T19:23:05Z
    evt-1777144985427 logged 'orchestrator dormant after 86466s idle'
    and killed the active session despite the operator actively chatting.
    Crash detection (reap_crashed) and explicit kills still apply, but
    the dormancy daemon must never transition the resident agent on
    any idle threshold — even with NO last_active sidecar AND ancient
    tmux activity (the worst-case shape of the original incident)."""
    _make_persistent(tmp_paths, "@orchestrator")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0  # alive
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # ancient — would otherwise reap
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@orchestrator" not in reaped, (
        f"@orchestrator must be exempt from idle-time reap; got {reaped}"
    )
    status = tmp_paths.agents / "@orchestrator" / "status"
    if status.exists():
        assert "dormant" not in status.read_text(), (
            "exempt agent must not be transitioned to dormant status"
        )


def test_reap_dormant_exemption_does_not_spare_other_agents(tmp_paths: Paths):
    """The @orchestrator carve-out is name-specific: a sibling
    persistent agent in the same sweep with ancient tmux activity and
    no last_active file still gets reaped."""
    _make_persistent(tmp_paths, "@orchestrator")
    _make_persistent(tmp_paths, "@sibling-stale")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "has-session" in cmd:
            cp.returncode = 0
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_dormant(paths=tmp_paths, max_idle_seconds=3600)

    assert "@orchestrator" not in reaped
    assert "@sibling-stale" in reaped


# ---------------------------------------------------------------------------
# reap_ephemeral_idle (zombie cron-pane sweeper)
# ---------------------------------------------------------------------------


def _make_ephemeral_dir(tmp_paths: Paths, name: str) -> Path:
    """Seed an ephemeral agent dir (no MISSION.md → not persistent)."""
    d = tmp_paths.agents / name
    d.mkdir(parents=True)
    (d / "scope").write_text(str(tmp_paths.project_root))
    (d / "status").write_text("spawned: do stuff\n")
    return d


def test_reap_ephemeral_idle_kills_only_ephemeral_idle_sessions(tmp_paths: Paths):
    """Mixed bag: persistent-active, persistent-idle, ephemeral-active,
    ephemeral-idle, orchestrator. Only the ephemeral-idle session is
    killed by reap_ephemeral_idle. Persistent-idle is reap_dormant's job;
    persistent-active and orchestrator must never be touched.
    """
    _make_persistent(tmp_paths, "@persist-active")
    _make_persistent(tmp_paths, "@persist-idle")
    _make_persistent(tmp_paths, "@orchestrator")
    _make_ephemeral_dir(tmp_paths, "@cron-active")
    _make_ephemeral_dir(tmp_paths, "@cron-zombie")

    # Idle activity timestamps: tmux returns session_activity as unix
    # epoch seconds. Anything ancient (1_000_000_000 ≈ 2001) is "idle
    # ≫ TTL"; anything in the future (2_000_000_000) reads as ~0 idle.
    activity_by_session = {
        "metasphere-persist-active": "2000000000",
        "metasphere-persist-idle": "1000000000",
        "metasphere-orchestrator": "1000000000",  # still must NOT be reaped
        "metasphere-cron-active": "2000000000",
        "metasphere-cron-zombie": "1000000000",
    }
    listed_sessions = "\n".join(activity_by_session.keys())

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = listed_sessions + "\n"
        elif "display-message" in cmd:
            target = cmd[cmd.index("-t") + 1]
            cp.returncode = 0
            cp.stdout = activity_by_session.get(target, "0")
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == ["metasphere-cron-zombie"], (
        f"expected only cron-zombie reaped, got {reaped}"
    )
    assert kill_sessions == ["metasphere-cron-zombie"], (
        f"expected only cron-zombie killed, got {kill_sessions}"
    )


def test_reap_ephemeral_idle_threshold_just_under_does_not_reap(tmp_paths: Paths):
    """An ephemeral idle exactly at or just under the threshold must
    NOT be reaped. The reaper uses strict `>` like reap_dormant — `==`
    is treated as "still within TTL"."""
    import time as _real_time

    _make_ephemeral_dir(tmp_paths, "@boundary-low")

    # Activity = now - 1799 seconds → 1799 < 1800 max_idle_seconds.
    just_under = str(int(_real_time.time()) - 1799)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-boundary-low\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_under
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == [], f"under-threshold session must NOT reap, got {reaped}"


def test_reap_ephemeral_idle_threshold_just_over_does_reap(tmp_paths: Paths):
    """An ephemeral idle just past the threshold IS reaped."""
    import time as _real_time

    _make_ephemeral_dir(tmp_paths, "@boundary-high")

    # Activity = now - 1801 seconds → 1801 > 1800.
    just_over = str(int(_real_time.time()) - 1801)
    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-boundary-high\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == ["metasphere-boundary-high"]
    assert kill_sessions == ["metasphere-boundary-high"]
    status = (
        tmp_paths.agents / "@boundary-high" / "status"
    ).read_text().strip()
    assert status.startswith("complete: ephemeral idle session reaped"), (
        "a successfully reaped one-shot session must become terminal; "
        f"got {status!r}"
    )


def test_reap_ephemeral_idle_failed_kill_stays_nonterminal(tmp_paths: Paths):
    """A failed tmux kill is not a reap and must remain retryable.

    Marking the agent complete after a nonzero kill result would hide a live
    zombie session behind terminal durable state, and returning it as reaped
    would make the daemon's batch event falsely claim cleanup succeeded.
    """
    import time as _real_time

    d = _make_ephemeral_dir(tmp_paths, "@kill-failed")
    just_over = str(int(_real_time.time()) - 1801)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-kill-failed\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            cp.returncode = 1
            cp.stderr = "simulated tmux failure"
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == []
    assert (d / "status").read_text() == "spawned: do stuff\n"


def test_reap_ephemeral_idle_unlinks_stale_deferred_cmd_marker(tmp_paths: Paths):
    """When the reaper kills an ephemeral session, it must also unlink
    that agent's ``state/<agent>_deferred_cmd`` marker if present.
    Without this coupling, a stale marker (e.g. a `/exit` left when a
    prior posthook never ran) would inject into the NEXT cron-fire's
    fresh session before it could do any real work — the failure mode
    @explorer found 2026-05-05 (4 markers from 2026-04-30 sitting on
    the just-reaped research-monitor agents)."""
    import time as _real_time

    _make_ephemeral_dir(tmp_paths, "@brand-mentions")
    # Leftover marker from a prior fire whose Stop hook never ran. The
    # `safe_name` rule in posthook drops the leading @ — mirror it here.
    paths_state = tmp_paths.state
    paths_state.mkdir(parents=True, exist_ok=True)
    marker = paths_state / "brand-mentions_deferred_cmd"
    marker.write_text("/exit\n")
    assert marker.exists()

    just_over = str(int(_real_time.time()) - 1801)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-brand-mentions\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == ["metasphere-brand-mentions"]
    assert not marker.exists(), (
        "stale deferred_cmd marker must be unlinked when the reaper "
        "kills the agent's session — otherwise it injects into the "
        "next fire's fresh session"
    )


def test_reap_ephemeral_idle_no_marker_is_a_noop(tmp_paths: Paths):
    """The unlink must be safe when no marker is present — the common
    case. Reaper still kills the session, no error from unlink(missing_ok)."""
    import time as _real_time

    _make_ephemeral_dir(tmp_paths, "@no-marker")
    just_over = str(int(_real_time.time()) - 1801)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-no-marker\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            cp.returncode = 0
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == ["metasphere-no-marker"]


def test_reap_ephemeral_idle_reaps_class_ephemeral_with_mission(tmp_paths: Paths):
    """Cron-fire integration premise: a research-monitor-style agent
    with MISSION.md AND ``class=ephemeral`` is reap-eligible for
    reap_ephemeral_idle. Without the class sidecar, the MISSION.md
    heuristic would mark it persistent and reap_ephemeral_idle would
    skip it (the lane that produced the 2026-05-05 zombie pile-up).

    Asserts the LAST link of the dependency chain the lead flagged:
    schedule daemon fires → wake spins up tmux+REPL → claude exits clean
    → ephemeral respawn drops to bash (covered by the wake test above)
    → reap_ephemeral_idle picks the idle pane up at threshold.
    """
    import time as _real_time

    # Simulate the post-cron-fire on-disk shape: MISSION.md present
    # (rendered by wake), class=ephemeral sidecar (instance-flipped).
    d = tmp_paths.agents / "@research-monitor-style"
    d.mkdir(parents=True)
    (d / "MISSION.md").write_text("scan and exit-self")
    (d / "class").write_text("ephemeral\n")
    (d / "scope").write_text(str(tmp_paths.project_root))
    (d / "status").write_text("spawned: scan\n")

    # The agent's tmux session has been idle past the threshold (claude
    # exited cleanly; bash has been silent since).
    just_over = str(int(_real_time.time()) - 1801)
    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-research-monitor-style\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            cp.returncode = 0
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == ["metasphere-research-monitor-style"], (
        f"class=ephemeral with MISSION.md must be reap-eligible; got {reaped}"
    )
    assert kill_sessions == ["metasphere-research-monitor-style"]


def test_reap_ephemeral_idle_still_skips_class_persistent(tmp_paths: Paths):
    """Symmetric guard: an agent without MISSION.md but with
    ``class=persistent`` sidecar must NOT be reaped by
    reap_ephemeral_idle. The class sidecar has to flip BOTH directions
    or it's only half a feature."""
    import time as _real_time

    d = tmp_paths.agents / "@long-lived"
    d.mkdir(parents=True)
    (d / "class").write_text("persistent\n")
    (d / "scope").write_text(str(tmp_paths.project_root))
    (d / "status").write_text("active\n")

    just_over = str(int(_real_time.time()) - 1801)

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-long-lived\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = just_over
        elif "kill-session" in cmd:
            raise AssertionError(
                f"persistent-classed agent must not be killed by "
                f"reap_ephemeral_idle; got {cmd!r}"
            )
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == []


def test_reap_ephemeral_idle_skips_unknown_sessions(tmp_paths: Paths):
    """A metasphere-prefixed tmux session with no matching AgentRecord
    (orphan, viewer, hand-rolled) must not be touched. The defensive
    skip exists so an operator-managed pane isn't surprise-killed by
    the daemon."""
    # No agent dirs created → the only thing list_agents returns is empty.

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "list-sessions" in cmd:
            cp.returncode = 0
            cp.stdout = "metasphere-mystery-pane\nmetasphere-all\n"
        elif "display-message" in cmd:
            cp.returncode = 0
            cp.stdout = "1000000000"  # ancient
        elif "kill-session" in cmd:
            raise AssertionError(
                f"kill-session must not be called for unknown sessions; "
                f"got {cmd!r}"
            )
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        reaped = agents.reap_ephemeral_idle(
            paths=tmp_paths, max_idle_seconds=1800
        )

    assert reaped == []


def test_touch_last_active_wired_into_all_signal_paths():
    """All four hook signals named in the directive must call
    touch_last_active. This guards against the helper landing without
    a wire-up (the field-defined-but-never-updated failure mode the
    accountability check explicitly looks for)."""
    import metasphere.posthook as _ph
    import metasphere.heartbeat as _hb
    import metasphere.telegram.inject as _inj
    import metasphere.cli.context as _ctx

    for module, label in (
        (_ph, "Stop hook (posthook.track_turn_completion)"),
        (_ctx, "UserPromptSubmit hook (cli.context)"),
        (_inj, "telegram-inject (telegram.inject.submit_to_tmux)"),
        (_hb, "heartbeat-tick (heartbeat.invoke_agent_heartbeat)"),
    ):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "touch_last_active" in src, (
            f"{label} must call touch_last_active; not found in "
            f"{module.__file__}"
        )


# ---------------------------------------------------------------------------
# reap_crashed (silent-death detection)
# ---------------------------------------------------------------------------


def _seed_ephemeral_with_pid(
    tmp_paths: Paths,
    name: str,
    *,
    pid: int = 99999,
    status: str = "spawned: do work",
    parent: str = "@orchestrator",
    write_pid: bool = True,
) -> Path:
    """Make a minimal ephemeral agent dir for reap_crashed exercises.

    Mirrors the on-disk shape spawn_ephemeral produces (no MISSION.md,
    no harness.md needed), with explicit knobs for the cases the spec
    enumerates: live/dead pid, terminal/non-terminal status,
    missing-pid, missing-parent.
    """
    d = tmp_paths.agents / name
    d.mkdir(parents=True)
    (d / "scope").write_text(str(tmp_paths.project_root))
    if parent:
        (d / "parent").write_text(parent)
    (d / "spawned_at").write_text("2026-04-21T00:00:00Z")
    (d / "task").write_text("do work")
    (d / "status").write_text(status)
    if write_pid:
        (d / "pid").write_text(f"{pid}\n")
    return d


_HAS_PROCFS = Path("/proc/self/status").exists()


@pytest.mark.skipif(not _HAS_PROCFS, reason="zombie detection is procfs-only")
def test_pid_alive_reports_zombie_as_dead():
    """A zombie is dead, and signal-0 cannot tell.

    Forks a real child that exits immediately and deliberately does NOT
    reap it, so the pid entry survives as ``Z (zombie)``. ``os.kill(pid, 0)``
    still succeeds against it — that is the bug this guards. Regression for
    the 2026-09-24 case where an agent sat defunct for eight hours in status
    ``spawned:``, never alerted, and its scheduled job silently never ran.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child never returns
        os._exit(0)
    try:
        # Wait for the child to actually reach zombie state rather than
        # assuming the fork/exit race resolved in our favour.
        for _ in range(500):
            if agents._pid_is_zombie(pid):
                break
            time.sleep(0.01)

        assert agents._pid_is_zombie(pid), "child never became a zombie"
        # The precondition that makes the bug possible: the canonical probe
        # says this dead process is alive.
        os.kill(pid, 0)
        assert agents._pid_alive(pid) is False, (
            "zombie must read as dead — signal-0 succeeds against it, so "
            "reap_crashed would skip it forever"
        )
    finally:
        os.waitpid(pid, 0)

    # Once reaped the pid is gone entirely, and both probes agree.
    assert agents._pid_alive(pid) is False
    assert agents._pid_is_zombie(pid) is False


@pytest.mark.skipif(not _HAS_PROCFS, reason="zombie detection is procfs-only")
def test_reap_crashed_transitions_zombie_pid(tmp_paths: Paths):
    """End to end: an agent whose recorded pid is a zombie gets reaped.

    Exercises the real ``_pid_alive`` rather than patching it — patching it
    is what let this bug live, since every existing reap test asserts on a
    mocked liveness answer.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child never returns
        os._exit(0)
    try:
        for _ in range(500):
            if agents._pid_is_zombie(pid):
                break
            time.sleep(0.01)
        assert agents._pid_is_zombie(pid), "child never became a zombie"

        d = _seed_ephemeral_with_pid(
            tmp_paths, "@zombie-agent", pid=pid, parent="@orchestrator",
        )

        sent: list[tuple] = []

        def fake_send(target, label, body, from_agent, paths=None, **kwargs):
            sent.append((target, label))
            m = MagicMock()
            m.id = "msg-fake"
            return m

        with patch("metasphere.agents.session_alive", return_value=False), \
             patch("metasphere.messages.send_message", side_effect=fake_send):
            reaped = agents.reap_crashed(paths=tmp_paths)
    finally:
        os.waitpid(pid, 0)

    assert reaped == ["@zombie-agent"]
    assert (d / "status").read_text().startswith("crashed:")
    assert sent == [("@orchestrator", "!alert")]


def test_reap_crashed_live_pid_no_op(tmp_paths: Paths):
    """An agent whose pid is alive must NOT be transitioned, even if its
    tmux session happens to be missing — pid liveness alone keeps it
    out of the silent-death bucket."""
    d = _seed_ephemeral_with_pid(tmp_paths, "@still-alive", pid=12345)

    sent: list[tuple] = []

    def fake_send(target, label, body, from_agent, paths=None, **kwargs):
        sent.append((target, label, from_agent))
        return MagicMock(id="msg-x")

    with patch("metasphere.agents._pid_alive", return_value=True), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == [], f"live pid must not be reaped, got {reaped}"
    assert sent == [], f"no parent alert expected, got {sent}"
    # Status untouched.
    assert (d / "status").read_text() == "spawned: do work"


def test_reap_crashed_dead_pid_marks_crashed_and_alerts_parent(tmp_paths: Paths):
    """Both pid AND tmux session gone, status non-terminal:
      - status rewritten to ``crashed: pid <N> dead, session gone``
      - !alert message sent from agent to its parent
      - agent name returned in the reaped list
    """
    d = _seed_ephemeral_with_pid(
        tmp_paths, "@silent-dead", pid=99999, parent="@orchestrator",
    )

    sent: list[dict] = []

    def fake_send(target, label, body, from_agent, paths=None, **kwargs):
        sent.append({
            "target": target, "label": label, "body": body,
            "from": from_agent, "wake": kwargs.get("wake"),
        })
        m = MagicMock()
        m.id = "msg-fake"
        return m

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == ["@silent-dead"]
    new_status = (d / "status").read_text().strip()
    assert new_status.startswith("crashed:"), (
        f"expected crashed: status, got {new_status!r}"
    )
    assert "pid 99999" in new_status and "session gone" in new_status

    assert len(sent) == 1, f"expected one !alert, got {sent}"
    msg = sent[0]
    assert msg["target"] == "@orchestrator"
    assert msg["label"] == "!alert"
    assert msg["from"] == "@silent-dead"
    assert "@silent-dead" in msg["body"] and "99999" in msg["body"]
    # wake=False to avoid triggering tmux side-effects on the parent
    # session from inside a daemon tick.
    assert msg["wake"] is False, f"expected wake=False, got {msg['wake']!r}"


def test_reap_crashed_terminal_status_no_op(tmp_paths: Paths):
    """Status already in a terminal bucket (complete/dormant/crashed/failed)
    short-circuits the sweep — even if pid+session would otherwise look
    dead. Idempotency: a second sweep over an already-crashed agent must
    not re-mark it or re-alert."""
    sent: list[tuple] = []

    def fake_send(*a, **k):
        sent.append((a, k))
        return MagicMock(id="x")

    for terminal_status in (
        "complete: !done delivered",
        "dormant: idle 90000s (auto-ttl at 2026-04-21T00:00:00Z)",
        "crashed: pid 1 dead, session gone",
        "failed: harness load error",
    ):
        # Fresh agent name per iteration so they don't collide.
        name = "@term-" + terminal_status.split(":", 1)[0]
        d = _seed_ephemeral_with_pid(
            tmp_paths, name, pid=99999, status=terminal_status,
        )
        with patch("metasphere.agents._pid_alive", return_value=False), \
             patch("metasphere.agents.session_alive", return_value=False), \
             patch("metasphere.messages.send_message", side_effect=fake_send):
            reaped = agents.reap_crashed(paths=tmp_paths)
        assert name not in reaped, (
            f"{name} with status={terminal_status!r} must not be reaped"
        )
        # Status untouched verbatim.
        assert (d / "status").read_text() == terminal_status, (
            f"terminal status {terminal_status!r} was rewritten"
        )

    assert sent == [], f"no alerts expected for terminal agents, got {sent}"


def test_reap_crashed_missing_pid_file_no_op(tmp_paths: Paths):
    """No pid file → no recorded liveness signal → reap_crashed must not
    transition. This is the legacy ``METASPHERE_SPAWN_NO_EXEC`` shape
    and the pre-pid-write window during spawn — both cases would
    otherwise be misclassified as silent deaths."""
    d = _seed_ephemeral_with_pid(
        tmp_paths, "@no-pid", write_pid=False, status="spawned: do work",
    )
    assert not (d / "pid").exists()

    sent: list[tuple] = []

    def fake_send(*a, **k):
        sent.append((a, k))
        return MagicMock(id="x")

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == [], f"agent without pid file must not be reaped, got {reaped}"
    assert sent == []
    assert (d / "status").read_text() == "spawned: do work"


def test_reap_crashed_missing_parent_marks_status_skips_alert(tmp_paths: Paths):
    """Silent-death detection still fires when the ``parent`` sidecar is
    missing — the status transition is the local effect, the parent
    !alert is the network effect. Skip the alert (no addressee), keep
    the transition."""
    d = _seed_ephemeral_with_pid(
        tmp_paths, "@orphan", pid=99999, parent="",  # no parent sidecar
    )
    # Sanity: parent file should not exist (helper writes it iff parent truthy).
    assert not (d / "parent").exists()

    sent: list[tuple] = []

    def fake_send(*a, **k):
        sent.append((a, k))
        return MagicMock(id="x")

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == ["@orphan"]
    assert (d / "status").read_text().strip().startswith("crashed:")
    assert sent == [], f"no parent → no alert, got {sent}"


def test_reap_crashed_swallows_send_message_failure(tmp_paths: Paths):
    """A send_message failure (broken inbox dir, IO error) must NOT
    prevent the status transition or other agents from being processed.
    Reaper runs on a daemon tick; one bad agent cannot abort the sweep."""
    d_bad = _seed_ephemeral_with_pid(tmp_paths, "@bad-alert", pid=11111)
    d_ok = _seed_ephemeral_with_pid(tmp_paths, "@ok-alert", pid=22222)

    call_targets: list[str] = []

    def flaky_send(target, label, body, from_agent, paths=None, **kwargs):
        call_targets.append(from_agent)
        if from_agent == "@bad-alert":
            raise OSError("simulated inbox write failure")
        m = MagicMock()
        m.id = "msg-ok"
        return m

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=flaky_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    # Both agents must be transitioned regardless of alert success.
    assert sorted(reaped) == ["@bad-alert", "@ok-alert"]
    assert (d_bad / "status").read_text().strip().startswith("crashed:")
    assert (d_ok / "status").read_text().strip().startswith("crashed:")
    # Both alerts were attempted — flaky_send saw both senders.
    assert sorted(call_targets) == ["@bad-alert", "@ok-alert"]


def test_reap_crashed_exit_self_tombstone_marks_complete_not_crashed(
    tmp_paths: Paths,
):
    """An agent that announced its own exit (``exit_self`` tombstone,
    written by ``metasphere session exit-self``) and is then found
    pid-dead/session-gone is a CLEAN exit the sweep beat cleanup to:
      - status → ``complete:`` (not ``crashed:``)
      - NO ``!alert`` to the parent
      - tombstone consumed (one-shot)
    2026-07-05 regression: @writing-lead's clean W27-roundup exit was
    flagged as silent death 4 minutes after its ``agent.exit_self``."""
    d = _seed_ephemeral_with_pid(
        tmp_paths, "@clean-exit", pid=99999, parent="@orchestrator",
    )
    tomb = d / "exit_self"
    tomb.write_text("2026-07-05T20:05:39Z session metasphere-clean-exit\n")
    # Tombstone postdates the pid write (normal life ordering).
    os.utime(d / "pid", (1000, 1000))
    os.utime(tomb, (2000, 2000))

    sent: list[tuple] = []

    def fake_send(*a, **k):
        sent.append((a, k))
        return MagicMock(id="x")

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == ["@clean-exit"], (
        f"clean exit must still be transitioned this sweep, got {reaped}"
    )
    new_status = (d / "status").read_text().strip()
    assert new_status.startswith("complete:"), (
        f"expected complete: status for clean exit_self, got {new_status!r}"
    )
    assert "exit_self" in new_status
    assert sent == [], (
        f"clean self-termination must not !alert the parent, got {sent}"
    )
    assert not tomb.exists(), "tombstone must be consumed one-shot"


def test_reap_crashed_stale_tombstone_still_marks_crashed(tmp_paths: Paths):
    """A tombstone OLDER than the pid file belongs to a prior life —
    the agent was relaunched after the exit intent, so a later
    pid-dead/session-gone state is a genuine crash. The leftover
    sidecar must not mask it."""
    d = _seed_ephemeral_with_pid(
        tmp_paths, "@respawned", pid=88888, parent="@orchestrator",
    )
    tomb = d / "exit_self"
    tomb.write_text("2026-07-05T18:00:00Z session metasphere-respawned\n")
    # Pid file rewritten AFTER the tombstone (relaunch) → tombstone stale.
    os.utime(tomb, (1000, 1000))
    os.utime(d / "pid", (2000, 2000))

    sent: list[dict] = []

    def fake_send(target, label, body, from_agent, paths=None, **kwargs):
        sent.append({"target": target, "label": label})
        return MagicMock(id="msg-x")

    with patch("metasphere.agents._pid_alive", return_value=False), \
         patch("metasphere.agents.session_alive", return_value=False), \
         patch("metasphere.messages.send_message", side_effect=fake_send):
        reaped = agents.reap_crashed(paths=tmp_paths)

    assert reaped == ["@respawned"]
    new_status = (d / "status").read_text().strip()
    assert new_status.startswith("crashed:"), (
        f"stale tombstone must not mask a real crash, got {new_status!r}"
    )
    assert len(sent) == 1 and sent[0]["label"] == "!alert"


def test_mark_exit_self_writes_tombstone(tmp_paths: Paths):
    """mark_exit_self drops the ``exit_self`` sidecar into the agent's
    dir (found via _find_agent_dir, so project-scoped agents resolve
    too) and returns True; unknown agents return False, never raise."""
    d = _seed_ephemeral_with_pid(tmp_paths, "@about-to-exit", pid=77777)

    ok = agents.mark_exit_self(
        "@about-to-exit", "metasphere-about-to-exit", paths=tmp_paths,
    )
    assert ok is True
    tomb = d / "exit_self"
    assert tomb.is_file()
    assert "metasphere-about-to-exit" in tomb.read_text()

    assert agents.mark_exit_self(
        "@never-spawned", "metasphere-never-spawned", paths=tmp_paths,
    ) is False


def test_spawn_ephemeral_clears_stale_exit_self_tombstone(
    tmp_paths: Paths, monkeypatch,
):
    """Name reuse: respawning an agent whose prior life left an
    ``exit_self`` tombstone must clear it — otherwise the new life's
    first genuine crash would be misread as a clean exit."""
    monkeypatch.setenv("METASPHERE_SPAWN_NO_EXEC", "1")
    stale_dir = tmp_paths.agents / "@recycled"
    stale_dir.mkdir(parents=True)
    (stale_dir / "exit_self").write_text(
        "2026-07-05T18:00:00Z session metasphere-recycled\n"
    )

    with patch("metasphere.agents.subprocess.Popen"):
        agents.spawn_ephemeral(
            "@recycled", "/sub/", "second life", paths=tmp_paths,
        )

    assert not (stale_dir / "exit_self").exists(), (
        "spawn_ephemeral must clear a prior life's exit_self tombstone"
    )
    assert (stale_dir / "status").read_text().startswith("spawned:")


# ---------------------------------------------------------------------------
# on_done_delivered (session hygiene: ephemeral-!done cleanup)
# ---------------------------------------------------------------------------

def _make_ephemeral(tmp_paths: Paths, name: str = "@ephi") -> "Path":
    d = tmp_paths.agents / name
    d.mkdir(parents=True)
    (d / "scope").write_text(str(tmp_paths.project_root))
    (d / "parent").write_text("@orchestrator")
    (d / "spawned_at").write_text("2026-04-21T00:00:00Z")
    (d / "task").write_text("do a thing")
    (d / "status").write_text("working: doing a thing")
    (d / "harness.md").write_text(f"# Agent: {name}\n")
    (d / "authority").write_text("read-only")
    (d / "responsibility").write_text("ship a note")
    (d / "accountability").write_text("verify note exists")
    (d / "pid").write_text("12345\n")
    (d / "task_id").write_text("task-abc\n")
    return d


def test_on_done_delivered_ephemeral_kills_tmux_and_clears_state(tmp_paths: Paths):
    """An ephemeral sender's !done triggers:
      - ``tmux kill-session -t metasphere-<sender>`` (no-op if absent)
      - removal of pid + task_id pointers
      - status rewritten to ``complete: !done delivered``
      - harness/contract/persona-equivalent files preserved
    """
    d = _make_ephemeral(tmp_paths, "@ephi")

    kill_sessions: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "kill-session" in cmd:
            cp.returncode = 0  # pretend session was alive and got killed
            kill_sessions.append(cmd[cmd.index("-t") + 1])
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        killed = agents.on_done_delivered("@ephi", paths=tmp_paths)

    assert killed == "metasphere-ephi", f"expected session name returned, got {killed!r}"
    assert kill_sessions == ["metasphere-ephi"], (
        f"expected single kill-session call for metasphere-ephi, got {kill_sessions}"
    )
    # Runtime pointers cleared
    assert not (d / "pid").exists()
    assert not (d / "task_id").exists()
    # Status transitioned
    assert (d / "status").read_text().strip() == "complete: !done delivered"
    # Harness + contract preserved
    for survived in ("harness.md", "authority", "responsibility",
                     "accountability", "scope", "parent", "spawned_at", "task"):
        assert (d / survived).exists(), f"{survived} must survive ephemeral done"


def test_on_done_delivered_failed_live_kill_preserves_runtime_state(
    tmp_paths: Paths,
):
    """A failed kill of a still-live session must remain retryable.

    The !done message itself has already been delivered, but publishing
    completion and deleting pid/task pointers would hide the live orphan from
    lifecycle observers. The idle reaper can retry once the session is idle.
    """
    d = _make_ephemeral(tmp_paths, "@kill-failed")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        if "kill-session" in cmd:
            cp.returncode = 1
            cp.stderr = "simulated tmux failure"
        elif "has-session" in cmd:
            cp.returncode = 0  # kill failed and session is still live
        else:
            cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        killed = agents.on_done_delivered("@kill-failed", paths=tmp_paths)

    assert killed is None
    assert (d / "pid").read_text() == "12345\n"
    assert (d / "task_id").read_text() == "task-abc\n"
    assert (d / "status").read_text() == "working: doing a thing"


def test_on_done_delivered_absent_headless_session_still_completes(
    tmp_paths: Paths,
):
    """tmux rc=1 means success for a headless ephemeral with no session."""
    d = _make_ephemeral(tmp_paths, "@headless")

    def fake_run(cmd, *args, **kwargs):
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        cp.returncode = 1  # kill and has-session both report absent
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        killed = agents.on_done_delivered("@headless", paths=tmp_paths)

    assert killed == "metasphere-headless"
    assert not (d / "pid").exists()
    assert not (d / "task_id").exists()
    assert (d / "status").read_text().strip() == "complete: !done delivered"


def test_on_done_delivered_persistent_does_NOT_kill_tmux(tmp_paths: Paths):
    """A persistent sender's !done is a strict no-op: tmux stays up,
    no status change, no pointer removal. Persistent lifecycle is
    governed by ``reap_dormant`` idle-TTL, not by !done delivery."""
    d = _make_persistent(tmp_paths, "@boss")
    # Simulate a persistent agent that also happens to have runtime
    # pointers (e.g. a pid from an externally-supervised REPL).
    (d / "pid").write_text("77777\n")
    (d / "task_id").write_text("task-xyz\n")
    (d / "status").write_text("active: persistent session\n")

    any_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        any_calls.append(list(cmd))
        cp = MagicMock()
        cp.stdout = ""
        cp.stderr = ""
        cp.returncode = 0
        return cp

    with patch("metasphere.agents.subprocess.run", side_effect=fake_run):
        killed = agents.on_done_delivered("@boss", paths=tmp_paths)

    assert killed is None
    # No subprocess call at all — persistent branch returns early.
    kill_calls = [c for c in any_calls if "kill-session" in c]
    assert kill_calls == [], f"persistent !done must never kill-session, got {kill_calls}"
    # Runtime state untouched.
    assert (d / "pid").read_text().strip() == "77777"
    assert (d / "task_id").read_text().strip() == "task-xyz"
    assert (d / "status").read_text().strip() == "active: persistent session"


def test_on_done_delivered_ignores_user_and_scope_senders(tmp_paths: Paths):
    """Non-agent senders (``@user``, ``@..``, ``@.``, ``@/scope/``) are
    skipped — the hook is strictly about ephemeral AGENT cleanup."""
    for bogus in ("@user", "@..", "@.", "@/abs/path/", "", "not-an-at-prefix"):
        killed = agents.on_done_delivered(bogus, paths=tmp_paths)
        assert killed is None, f"expected no-op for sender={bogus!r}, got {killed!r}"


def test_on_done_delivered_unknown_agent_is_noop(tmp_paths: Paths):
    """Sender with no corresponding agent dir → nothing to clean up."""
    killed = agents.on_done_delivered("@ghost-sender", paths=tmp_paths)
    assert killed is None


# ---------------------------------------------------------------------------
# contract_main (contract retrieval)
# ---------------------------------------------------------------------------


def test_contract_live_agent_with_contract(tmp_paths: Paths):
    """contract_main reads contract sidecar files from a live agent dir."""
    from metasphere.cli.agents import contract_main
    from io import StringIO

    agent_dir = tmp_paths.agents / "@test-auditor"
    agent_dir.mkdir(parents=True)
    (agent_dir / "status").write_text("working: auditing")
    (agent_dir / "task").write_text("audit the thing")
    (agent_dir / "parent").write_text("@orchestrator")
    (agent_dir / "spawned_at").write_text("2026-04-12T10:00:00Z")
    (agent_dir / "authority").write_text("Read-only. MAY NOT write.")
    (agent_dir / "responsibility").write_text("Produce REPORT.md")
    (agent_dir / "accountability").write_text("File exists with 3+ sections")

    import sys
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        rc = contract_main(["@test-auditor"])
    finally:
        sys.stdout = old_stdout
    output = captured.getvalue()

    assert rc == 0
    assert "DELEGATION CONTRACT for @test-auditor" in output
    assert "Read-only. MAY NOT write." in output
    assert "Produce REPORT.md" in output
    assert "File exists with 3+ sections" in output
    assert "(live agent dir:" in output


def test_contract_gcd_agent_from_log(tmp_paths: Paths):
    """contract_main falls back to the GC preservation log when agent dir
    is gone, and extracts contract from preserved sidecar sections."""
    from metasphere.cli.agents import contract_main
    from io import StringIO

    # Create a GC log with preserved sidecar sections (post-e3d6100 format)
    log_dir = tmp_paths.logs / "agents" / "_global"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "@dead-auditor.log"
    log_file.write_text(
        "# @dead-auditor — 2026-04-12T12:00:00Z\n"
        "Status: complete: done\n"
        "Reason: completed\n\n"
        "--- task ---\n"
        "audit something\n"
        "--- status ---\n"
        "complete: done\n"
        "--- parent ---\n"
        "@orchestrator\n"
        "--- spawned_at ---\n"
        "2026-04-12T09:00:00Z\n"
        "--- authority ---\n"
        "Read files only.\n"
        "--- responsibility ---\n"
        "Ship a report.\n"
        "--- accountability ---\n"
        "Report has 5 sections.\n"
        "--- harness.md ---\n"
        "# Agent: @dead-auditor\n"
    )

    import sys
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        rc = contract_main(["@dead-auditor"])
    finally:
        sys.stdout = old_stdout
    output = captured.getvalue()

    assert rc == 0
    assert "DELEGATION CONTRACT for @dead-auditor" in output
    assert "Read files only." in output
    assert "Ship a report." in output
    assert "Report has 5 sections." in output
    assert "(from GC log:" in output


def test_contract_gcd_agent_harness_fallback(tmp_paths: Paths):
    """For agents GC'd before the sidecar-preserve fix, verify extracts
    contract from the rendered harness.md section."""
    from metasphere.cli.agents import contract_main
    from io import StringIO

    log_dir = tmp_paths.logs / "agents" / "_global"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "@old-audit.log"
    log_file.write_text(
        "# @old-audit — 2026-04-11T20:00:00Z\n"
        "Status: complete: done\n"
        "Reason: completed\n\n"
        "--- task ---\n"
        "old audit task\n"
        "--- status ---\n"
        "complete: done\n"
        "--- harness.md ---\n"
        "# Agent: @old-audit\n\n"
        "## Delegation Contract\n\n"
        "### Authority (what you MAY do)\n\n"
        "Only read.\n\n"
        "### Responsibility (what you MUST produce)\n\n"
        "A findings doc.\n\n"
        "### Accountability (how parent will verify)\n\n"
        "Doc has intro + 2 sections.\n\n"
        "---\n\n"
        "You are autonomous.\n"
    )

    import sys
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        rc = contract_main(["@old-audit"])
    finally:
        sys.stdout = old_stdout
    output = captured.getvalue()

    assert rc == 0
    assert "Only read." in output
    assert "A findings doc." in output
    assert "Doc has intro + 2 sections." in output


def test_contract_nonexistent_returns_error(tmp_paths: Paths):
    """contract_main returns 1 when no agent dir or log exists."""
    from metasphere.cli.agents import contract_main
    rc = contract_main(["@ghost"])
    assert rc == 1


# ---------------------------------------------------------------------------
# touch_last_active_if_exists (no-create outbound-activity variant)
# ---------------------------------------------------------------------------


def test_touch_last_active_if_exists_refreshes_existing_dir(tmp_paths: Paths):
    d = tmp_paths.agents / "@busy"
    d.mkdir(parents=True)
    (d / "last_active").write_text("2026-07-01T00:00:00Z\n")

    assert agents.touch_last_active_if_exists("@busy", paths=tmp_paths) is True
    idle = agents._last_active_idle_seconds(d)
    assert idle is not None and idle < 120


def test_touch_last_active_if_exists_never_creates_dir(tmp_paths: Paths):
    """The no-create contract is the point of the variant: synthetic
    bus senders must not get MISSION-less ghost dirs (which the
    ephemeral GC would then sweep in a churn cycle)."""
    assert agents.touch_last_active_if_exists(
        "@consolidate", paths=tmp_paths,
    ) is False
    assert not (tmp_paths.agents / "@consolidate").exists()


def test_outbound_send_defeats_staleness(tmp_paths: Paths):
    """End-to-end pin of the 2026-07-05 21:05 incident: ancient input
    signal + huge tmux idle, but the agent just SENT a bus message —
    the shared stale-kill predicate must read it as active."""
    from metasphere import messages as _m

    d = tmp_paths.agents / "@quiet-worker"
    d.mkdir(parents=True)
    (d / "last_active").write_text("2026-07-01T00:00:00Z\n")
    rec = agents.AgentRecord(
        name="@quiet-worker", scope="", parent="", status="",
        spawned_at="", agent_dir=d,
    )

    _m.send_message(
        "@orchestrator", "!task", "silent progress", "@quiet-worker",
        paths=tmp_paths, wake=False,
    )

    with patch("metasphere.agents._session_idle_seconds", return_value=99999):
        stale, idle = agents._stale_and_not_generating(
            rec, d, "metasphere-quiet-worker", 7200, tmp_paths,
        )
    assert stale is False, (
        "agent that sent bus traffic moments ago must not be stale"
    )
    assert idle is not None and idle < 120
