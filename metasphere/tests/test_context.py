"""Tests for metasphere.context — per-turn context assembly + drift hash."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from metasphere import context as ctx
from metasphere import messages as _msgs
from metasphere import tasks as _tasks
from metasphere.paths import Paths


# ---------------------------------------------------------------------------
# harness_hash
# ---------------------------------------------------------------------------


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_harness_hash_matches_bash_recipe(tmp_paths: Paths):
    """Hash is computed over files under ``paths.root`` (= the dir
    the claude CLI actually reads CLAUDE.md from), not project_root.
    """
    _write(tmp_paths.root / "CLAUDE.md", "claude\n")
    _write(tmp_paths.root / ".claude" / "settings.json", "{settings}\n")
    _write(tmp_paths.root / ".claude" / "settings.local.json", "{local}\n")

    py_hash = ctx.harness_hash(tmp_paths)

    files = sorted(
        str(tmp_paths.root / rel)
        for rel in (
            "CLAUDE.md",
            ".claude/settings.json",
            ".claude/settings.local.json",
        )
    )
    h = hashlib.sha256()
    for f in files:
        h.update(Path(f).read_bytes())
    assert py_hash == h.hexdigest()


def test_harness_hash_empty_when_no_files(tmp_paths: Paths):
    assert ctx.harness_hash(tmp_paths) == ""


def test_harness_hash_reads_root_not_project_root(tmp_paths: Paths):
    """Regression for the 2026-04-16 divergence: baseline writer
    (gateway daemon with METASPHERE_REPO_ROOT set to the source repo)
    and reader (@orchestrator REPL with CWD=~/.metasphere) resolved
    different ``project_root`` values and hashed different CLAUDE.md
    files. Banner fired every inject. Fix roots both to ``paths.root``.

    Prove it: write DIFFERENT content to both project_root and root;
    hash must reflect root (which is where the claude CLI actually
    bakes in CLAUDE.md from), NOT project_root.
    """
    _write(tmp_paths.root / "CLAUDE.md", "ROOT content\n")
    _write(tmp_paths.project_root / "CLAUDE.md", "REPO content\n")

    py_hash = ctx.harness_hash(tmp_paths)
    expected = hashlib.sha256(b"ROOT content\n").hexdigest()
    assert py_hash == expected, (
        "harness_hash must hash paths.root/CLAUDE.md (what the claude "
        "CLI bakes in), not paths.project_root/CLAUDE.md"
    )


# ---------------------------------------------------------------------------
# truncate_section
# ---------------------------------------------------------------------------


def test_truncate_section_caps_long_text():
    long = "x" * 5000
    out = ctx.truncate_section(long, budget=100)
    # The cut keeps ≤ budget bytes plus the truncation marker.
    assert len(out.encode("utf-8")) < 5000
    assert "truncated" in out


def test_truncate_section_passthrough_short_text():
    assert ctx.truncate_section("hello", budget=2048) == "hello"


# ---------------------------------------------------------------------------
# Drift warning
# ---------------------------------------------------------------------------


def test_drift_warning_emitted_when_baseline_differs(tmp_paths: Paths):
    _write(tmp_paths.root / "CLAUDE.md", "claude v1\n")
    (tmp_paths.state).mkdir(parents=True, exist_ok=True)
    (tmp_paths.state / "harness_hash_baseline").write_text("deadbeef\n")

    out = ctx.build_context(tmp_paths)
    assert "Harness drift detected" in out


def test_drift_warning_silent_when_baseline_matches(tmp_paths: Paths):
    _write(tmp_paths.root / "CLAUDE.md", "claude v1\n")
    live = ctx.harness_hash(tmp_paths)
    (tmp_paths.state).mkdir(parents=True, exist_ok=True)
    (tmp_paths.state / "harness_hash_baseline").write_text(live + "\n")

    out = ctx.build_context(tmp_paths)
    assert "Harness drift detected" not in out


# ---------------------------------------------------------------------------
# build_context: section order + empty-state robustness
# ---------------------------------------------------------------------------


def test_build_context_emits_all_sections_in_order(tmp_paths: Paths):
    # Seed each data source so each section produces an identifiable header.
    # 1. Status
    agent_dir = tmp_paths.agent_dir("@orchestrator")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status").write_text("working: porting context.py\n")

    # 2. Drift (force a warning) — write to paths.root (where the
    # claude CLI actually bakes CLAUDE.md from; post-PR #19 the hash
    # no longer uses project_root).
    _write(tmp_paths.root / "CLAUDE.md", "claude\n")
    tmp_paths.state.mkdir(parents=True, exist_ok=True)
    (tmp_paths.state / "harness_hash_baseline").write_text("deadbeef\n")

    # 3. Telegram
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    tg_file = tmp_paths.telegram_stream / f"{today}.jsonl"
    tg_file.parent.mkdir(parents=True, exist_ok=True)
    tg_file.write_text(
        json.dumps(
            {"from": {"username": "testuser"}, "text": "hello", "date": 1775539062}
        )
        + "\n"
    )

    # 4. Messages
    _msgs.send_message(
        "@.", "!info", "test message body", "@user", paths=tmp_paths, wake=False
    )

    # 5. Tasks
    _tasks.create_task(
        "ship the port", _tasks.PRIORITY_DEFAULT, tmp_paths.scope, tmp_paths.project_root
    )

    # 6. Events
    from metasphere.events import log_event

    log_event("test.event", "hello world", paths=tmp_paths)

    out = ctx.build_context(tmp_paths)

    # Each section header must appear, in order.
    headers_in_order = [
        "# Metasphere Delta",        # status
        "## ⚠ Harness drift",        # drift
        "## Telegram (recent",       # telegram
        "## Messages",               # messages
        "## Tasks",                  # tasks
        "## Recent Events",          # events
        "## Memory Context (FTS)",   # memory
    ]
    last = -1
    for h in headers_in_order:
        idx = out.find(h)
        assert idx != -1, f"missing section header: {h}\n---\n{out}"
        assert idx > last, f"section out of order: {h}"
        last = idx


def test_build_context_empty_state_does_not_crash(tmp_paths: Paths):
    out = ctx.build_context(tmp_paths)
    # Status header is always present even with no agent dir.
    assert "Metasphere Delta" in out
    # Empty inbox / tasks / events render the "no ..." sentinels rather
    # than blowing up.
    assert "## Messages" in out
    assert "## Tasks" in out
    assert "## Recent Events" in out
    assert "## Memory Context (FTS)" in out


# --- Messages section: cap + newest-first + self-outbound (2026-07-27) ------


def _seed_msg(inbox: Path, mid: str, frm: str, created: str,
              label: str = "!task", body: str = "do the thing") -> None:
    """Write a minimal unread .msg file into an inbox dir. Uses a sacred
    label (!task) by default so ``view=True`` collection keeps it UNREAD."""
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{mid}.msg").write_text(
        "---\n"
        f"id: {mid}\n"
        f'from: "{frm}"\n'
        'to: "@me"\n'
        f'label: "{label}"\n'
        "status: unread\n"
        "scope: /\n"
        f"created: {created}\n"
        "read_at: \n"
        "reply_to: \n"
        "ping_count: 0\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_render_messages_capped_newest_first_excludes_self_outbound(
    tmp_paths: Paths, monkeypatch
):
    """Regression for the ~340KB unread dump: the section must be bounded to
    the most-recent N inbound unread (newest-first so truncation can only drop
    the oldest), must NOT render the agent's own outbound dispatch records, and
    must keep an accurate header count with a fold-away tail."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@me")
    inbox = tmp_paths.root / "messages" / "inbox"

    # 20 inbound !task from another agent, ascending created (i=19 newest).
    for i in range(20):
        _seed_msg(inbox, f"msg-100000{i:02d}-1", "@other",
                  f"2026-07-27T10:00:{i:02d}Z", body=f"inbound {i}")
    # 10 of the agent's OWN outbound dispatches — even NEWER (seconds 30-39),
    # to prove they're excluded by SENDER, not merely by recency.
    for j in range(10):
        _seed_msg(inbox, f"msg-200000{j:02d}-1", "@me",
                  f"2026-07-27T10:00:{30 + j:02d}Z", body=f"dispatch {j}")

    out = ctx._render_messages(tmp_paths)

    # Bounded: exactly the cap number of inbound entries rendered.
    assert out.count("from @other") == ctx._MESSAGES_RENDER_CAP
    # Self-outbound never rendered, despite being the newest messages.
    assert "from @me" not in out
    # Newest-first: the newest inbound (i=19) is shown; the oldest (i=0) is
    # dropped by the cap — truncation kept the fresh, not the stale.
    assert "msg-10000019-1" in out
    assert "inbound 0" not in out
    # Header counts ALL unread (20 inbound + 10 self = 30); does not render 30.
    assert "30 unread" in out
    # Fold-away tail reconciles: 20 - 15 = 5 older, 10 own outbound hidden.
    assert "+5 older unread" in out
    assert "10 own outbound dispatch(es) hidden" in out
    # The whole section stays small (no 340KB wall).
    assert len(out) < 4000


def test_render_messages_failopen_when_agent_unresolved(
    tmp_paths: Paths, monkeypatch
):
    """If the agent id can't be resolved, the section degrades to 'show recent
    unread' (nothing treated as self) rather than hiding everything or raising."""
    monkeypatch.setattr(
        "metasphere.context.resolve_agent_id",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    inbox = tmp_paths.root / "messages" / "inbox"
    for i in range(3):
        _seed_msg(inbox, f"msg-300000{i}-1", "@other",
                  f"2026-07-27T09:00:0{i}Z", body=f"m {i}")

    out = ctx._render_messages(tmp_paths)  # must not raise
    assert "3 unread" in out
    assert out.count("from @other") == 3


# --- Memory FTS: CAM wiring + query variance (2026-04-17) ------------------


def test_render_memory_fts_uses_cam_when_available(tmp_paths: Paths, monkeypatch):
    """When CamStrategy returns hits, they appear first in the output.
    TokenOverlapStrategy hits appear as fallback after cam hits."""
    from metasphere.memory.base import MemoryHit

    cam_hits = [MemoryHit(source="cam-session/test.md", score=0.95,
                           excerpt="CAM result about foo")]
    fts_hits = [MemoryHit(source="docs/fallback.md", score=0.80,
                           excerpt="FTS fallback result")]

    # Monkeypatch the strategies so no real cam/fts runs
    monkeypatch.setattr(
        "metasphere.memory.api.recall",
        lambda query, limit=10, strategies=None: cam_hits + fts_hits,
    )
    out = ctx._render_memory_fts(tmp_paths, "@test")
    assert "## Memory Context (FTS)" in out
    assert "cam-session/test.md" in out
    # CAM hit appears before FTS hit
    cam_pos = out.find("cam-session/test.md")
    fts_pos = out.find("docs/fallback.md")
    assert cam_pos < fts_pos


def test_render_memory_fts_falls_back_on_cam_failure(tmp_paths: Paths, monkeypatch):
    """When CamStrategy returns nothing (missing binary / timeout), the
    output still has token-overlap hits."""
    from metasphere.memory.base import MemoryHit

    fts_hits = [MemoryHit(source="docs/only-fts.md", score=0.7,
                           excerpt="Token overlap found this")]
    monkeypatch.setattr(
        "metasphere.memory.api.recall",
        lambda query, limit=10, strategies=None: fts_hits,
    )
    out = ctx._render_memory_fts(tmp_paths, "@test")
    assert "docs/only-fts.md" in out
    assert "Token overlap found this" in out


# --- Memory FTS: auto_memory toggle (2026-06-18) --------------------------


def test_render_memory_fts_suppressed_when_auto_memory_false(
    tmp_paths: Paths, monkeypatch
):
    """An agent whose spec has ``auto_memory: false`` gets an empty
    string from ``_render_memory_fts`` — the assembly loop drops empty
    strings, so no orphan header leaks into the heartbeat."""
    from metasphere.specs import AgentSpec

    fake_spec = AgentSpec(
        name="quiet",
        role="eng",
        description="",
        auto_memory=False,
    )
    monkeypatch.setattr(
        "metasphere.specs.get_spec_for_agent",
        lambda agent_id, paths=None: fake_spec,
    )
    assert ctx._render_memory_fts(tmp_paths, "@quiet") == ""


def test_render_memory_fts_emitted_when_auto_memory_true(
    tmp_paths: Paths, monkeypatch
):
    """auto_memory=True (the default) keeps the section rendered."""
    from metasphere.memory.base import MemoryHit
    from metasphere.specs import AgentSpec

    fake_spec = AgentSpec(name="loud", role="eng", description="")
    monkeypatch.setattr(
        "metasphere.specs.get_spec_for_agent",
        lambda agent_id, paths=None: fake_spec,
    )
    monkeypatch.setattr(
        "metasphere.memory.api.recall",
        lambda query, limit=10, strategies=None: [
            MemoryHit(source="docs/a.md", score=0.9, excerpt="hit body")
        ],
    )
    out = ctx._render_memory_fts(tmp_paths, "@loud")
    assert "## Memory Context (FTS)" in out
    assert "docs/a.md" in out


def test_render_memory_fts_missing_spec_defaults_on(tmp_paths: Paths, monkeypatch):
    """Non-seeded agents (no spec pointer) still render the section —
    spec lookup returning ``None`` falls through to the default-on path."""
    from metasphere.memory.base import MemoryHit

    monkeypatch.setattr(
        "metasphere.specs.get_spec_for_agent",
        lambda agent_id, paths=None: None,
    )
    monkeypatch.setattr(
        "metasphere.memory.api.recall",
        lambda query, limit=10, strategies=None: [
            MemoryHit(source="docs/b.md", score=0.9, excerpt="hit body")
        ],
    )
    out = ctx._render_memory_fts(tmp_paths, "@unseeded")
    assert "## Memory Context (FTS)" in out


# --- Memory FTS: prompt threading (2026-06-24) ----------------------------


def test_render_memory_fts_query_leads_with_prompt(tmp_paths: Paths, monkeypatch):
    """The user's prompt is threaded into the recall query and leads it,
    so recall is scored primarily against what was just asked rather than
    only ambient state (task file + project name + latest event)."""
    seen: dict[str, str] = {}

    def _capture(query, limit=10, strategies=None):
        seen["query"] = query
        return []

    monkeypatch.setattr("metasphere.memory.api.recall", _capture)
    ctx._render_memory_fts(tmp_paths, "@test", "how does the idle gate work")
    assert "how does the idle gate work" in seen["query"]
    # Prompt leads — it is the highest-signal part of the query.
    assert seen["query"].startswith("how does the idle gate work")


def test_render_memory_fts_empty_prompt_preserves_ambient_query(
    tmp_paths: Paths, monkeypatch
):
    """With no prompt (heartbeat/manual turns) the query is unchanged from
    the prior ambient-stem behavior — backward compatible."""
    seen: dict[str, str] = {}

    def _capture(query, limit=10, strategies=None):
        seen["query"] = query
        return []

    monkeypatch.setattr("metasphere.memory.api.recall", _capture)
    ctx._render_memory_fts(tmp_paths, "@test")
    # The ambient stem always contributes the project name; no prompt text
    # is prepended when prompt is empty.
    assert seen["query"]  # non-empty (falls back to stem, never blank)


# --- Memory FTS: fresh-signal de-noising (2026-07-27) ----------------------


def _seed_events(paths: Paths, events: list[tuple[str, str]]) -> None:
    """Append (type, message) events in order (oldest first)."""
    from metasphere import events as _events

    for etype, msg in events:
        _events.log_event(etype, msg, agent="@orchestrator", paths=paths)


def test_fresh_activity_signal_excludes_low_entropy_ticks(tmp_paths: Paths):
    """The fresh signal must skip tick machinery (cron_fire, heartbeat.invoke,
    agent.heartbeat, message.consolidate) so recall tracks real activity, not
    the near-constant tick that pinned the same memos every heartbeat."""
    _seed_events(tmp_paths, [
        ("message.send", "@orchestrator -> @agent-b: review the audit report"),
        ("schedule.cron_fire", "task:consolidate"),
        ("agent.heartbeat", "@orchestrator turn 33820"),
        ("heartbeat.invoke", "injected heartbeat into metasphere-orchestrator"),
        ("schedule.cron_fire", "task:consolidate"),
    ])

    signal = ctx._fresh_activity_signal(tmp_paths)

    # The one substantive event survives...
    assert "audit report" in signal
    # ...and none of the tick tokens leak into the signal.
    assert "consolidate" not in signal
    assert "heartbeat" not in signal
    assert "turn 33820" not in signal


def test_fresh_activity_signal_failopen_on_no_events(tmp_paths: Paths):
    """No events (or only ticks) yields ``''`` — never raises, so a degenerate
    fresh signal can't crash context assembly (fail-open at the call site)."""
    assert ctx._fresh_activity_signal(tmp_paths) == ""
    _seed_events(tmp_paths, [
        ("schedule.cron_fire", "task:consolidate"),
        ("heartbeat.invoke", "injected heartbeat into metasphere-orchestrator"),
    ])
    # Only ticks present -> nothing substantive survives -> empty, no crash.
    assert ctx._fresh_activity_signal(tmp_paths) == ""


def test_heartbeat_query_not_pinned_by_consolidate_tick(
    tmp_paths: Paths, monkeypatch
):
    """Regression for the operator-flagged fixation: across successive
    heartbeat turns where only tick events fire, the recall query must not be
    dominated by the "consolidate" tick token (which self-reinforced by
    matching the consolidation-themed memos). The query should carry the last
    real activity and never the tick machinery."""
    seen: list[str] = []

    def _capture(query, limit=10, strategies=None):
        seen.append(query)
        return []

    monkeypatch.setattr("metasphere.memory.api.recall", _capture)

    # Real activity, then a burst of ticks ending on the consolidate cron —
    # so the *newest* event (what the reverted single-latest logic would grab)
    # is "task:consolidate". This makes the assertion below a true revert guard:
    # under the old code the query WOULD contain "consolidate".
    _seed_events(tmp_paths, [
        ("message.send", "@orchestrator -> @agent-b: ship the config fix"),
        ("agent.heartbeat", "@orchestrator turn 41001"),
        ("schedule.cron_fire", "task:consolidate"),
    ])
    ctx._render_memory_fts(tmp_paths, "@orchestrator")

    # More ticks land before the next heartbeat, again ending on the tick.
    _seed_events(tmp_paths, [
        ("agent.heartbeat", "@orchestrator turn 41002"),
        ("heartbeat.invoke", "injected heartbeat into metasphere-orchestrator"),
        ("schedule.cron_fire", "task:consolidate"),
    ])
    ctx._render_memory_fts(tmp_paths, "@orchestrator")

    assert len(seen) == 2
    for q in seen:
        # The tick token that pinned the memos must never dominate the query.
        assert "consolidate" not in q
        # Real recent activity is what the query reflects instead.
        assert "config fix" in q


def test_no_hits_affordance_anchored_to_project_root_not_pwd(
    tmp_paths: Paths, monkeypatch
):
    """The 'No memories matched' affordance must name the agent's real memory
    folder — derived from project_root, not the process PWD. Regression for the
    live-observed truncated path '.../-home-u/memory' produced when the
    REPL's cwd on a heartbeat turn was $HOME rather than the project dir."""
    # Force the no-hits branch: recall returns nothing.
    monkeypatch.setattr("metasphere.memory.api.recall", lambda *a, **k: [])
    # A bogus/truncating PWD must NOT influence the rendered folder.
    monkeypatch.setenv("PWD", "/home/u")

    out = ctx._render_memory_fts(tmp_paths, "@orchestrator")

    expected = str(ctx._auto_memory_dir_for_path(str(tmp_paths.project_root)))
    assert expected in out
    # The PWD-derived truncated slug must never appear.
    assert "/-home-u/memory" not in out


# --- Last-edited files section (2026-04-17) ---------------------------------


def test_last_edited_files_excludes_noise(tmp_path, monkeypatch):
    """Noise dirs (__pycache__, .git, node_modules, .venv) are excluded
    from the last-edited listing."""
    from metasphere import project as _project

    proj_path = tmp_path / "myproject"
    proj_path.mkdir()

    # Real files
    (proj_path / "src").mkdir()
    (proj_path / "src" / "main.py").write_text("code")
    (proj_path / "README.md").write_text("readme")

    # Noise files
    (proj_path / "__pycache__").mkdir()
    (proj_path / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"noise")
    (proj_path / ".git").mkdir()
    (proj_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (proj_path / "node_modules").mkdir()
    (proj_path / "node_modules" / "pkg.js").write_text("js")

    # Mock project resolution
    from types import SimpleNamespace
    fake_proj = SimpleNamespace(path=str(proj_path), name="myproject")
    monkeypatch.setattr(
        _project, "project_for_scope",
        lambda scope, paths=None: fake_proj,
    )

    from metasphere.paths import Paths
    paths = Paths(
        root=tmp_path / ".metasphere",
        scope=proj_path,
        project_root=proj_path,
    )
    (paths.root / "agents" / "@test").mkdir(parents=True)

    out = ctx._render_last_edited_files(paths)
    assert "main.py" in out
    assert "README.md" in out
    assert "__pycache__" not in out
    assert ".git" not in out
    assert "node_modules" not in out


def test_last_edited_files_respects_10_cap(tmp_path, monkeypatch):
    """Only the 10 most recently edited files are shown, even if more
    exist."""
    from metasphere import project as _project

    proj_path = tmp_path / "proj"
    proj_path.mkdir()
    for i in range(20):
        (proj_path / f"file_{i:02d}.txt").write_text(f"content {i}")

    from types import SimpleNamespace
    monkeypatch.setattr(
        _project, "project_for_scope",
        lambda scope, paths=None: SimpleNamespace(path=str(proj_path), name="proj"),
    )

    from metasphere.paths import Paths
    paths = Paths(
        root=tmp_path / ".metasphere",
        scope=proj_path,
        project_root=proj_path,
    )
    (paths.root / "agents" / "@test").mkdir(parents=True)

    out = ctx._render_last_edited_files(paths)
    # Count file lines (each starts with 2 spaces)
    file_lines = [l for l in out.splitlines() if l.startswith("  ")]
    assert len(file_lines) == 10


def test_render_project_includes_timestamps(tmp_paths: Paths, monkeypatch):
    """The project section's Recent: line includes a UTC timestamp
    when a last commit is available."""
    from metasphere import project as _project
    from types import SimpleNamespace

    proj_path = tmp_paths.scope
    # Create .git dir so the git-log branch fires
    (proj_path / ".git").mkdir(exist_ok=True)
    fake_proj = SimpleNamespace(
        path=str(proj_path), name="test-proj", goal="test goal",
        members=[], status="active",
    )
    monkeypatch.setattr(
        _project, "project_for_scope",
        lambda scope, paths=None: fake_proj,
    )
    # Simulate git log returning subject|timestamp
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="fix: something|2026-04-16T20:00:00+00:00\n",
        ) if "git" in str(a) else SimpleNamespace(returncode=1, stdout=""),
    )

    out = ctx._render_project(tmp_paths)
    assert "Scope:" in out
    assert "Recent:" in out
    # Timestamp from commit should be present
    assert "2026-04-16T20:00" in out


# ---------------------------------------------------------------------------
# _render_voice_capsule — full persona injection (PR B)
# ---------------------------------------------------------------------------
#
# Pre-PR-B behaviour: capsule loaded VOICE.md or SOUL.md only, capped
# at 1500B / 40 lines. IDENTITY.md and USER.md were never injected.
# Result: persona drift as the kaomoji/warmth-marker/user-model context
# never reached the model.

def _seed_agent_dir(tmp_paths: Paths, agent: str) -> Path:
    d = tmp_paths.agent_dir(agent)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_voice_capsule_loads_soul_only(tmp_paths: Paths):
    d = _seed_agent_dir(tmp_paths, "@orchestrator")
    (d / "SOUL.md").write_text(
        "# @orchestrator — soul\n\nCalm intensity, thinking companion.\n",
        encoding="utf-8",
    )
    out = ctx._render_voice_capsule(tmp_paths, "@orchestrator")
    assert "## Voice (who you are, how you sound)" in out
    assert "Calm intensity, thinking companion." in out
    # H1 stripped
    assert "@orchestrator — soul" not in out
    # Single section, no Identity / User-model emitted
    assert "## Identity" not in out
    assert "## User-model" not in out
    # Trailing pointer line emitted because at least one file landed
    assert "Persona files at" in out


def test_voice_capsule_loads_all_three_in_order(tmp_paths: Paths):
    d = _seed_agent_dir(tmp_paths, "@orchestrator")
    (d / "SOUL.md").write_text("# soul\n\nVOICE-LINE.\n", encoding="utf-8")
    (d / "IDENTITY.md").write_text(
        "# identity\n\n( • ‿ • ) IDENTITY-LINE.\n", encoding="utf-8"
    )
    (d / "USER.md").write_text("# user\n\nUSER-LINE.\n", encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@orchestrator")

    # Stable USER/IDENTITY facts lead verbose voice so hook-output previews
    # preserve them even if future context growth crosses a transport limit.
    voice_idx = out.index("## Voice")
    identity_idx = out.index("## Identity")
    user_idx = out.index("## User-model")
    assert user_idx < identity_idx < voice_idx

    # All three bodies landed unchanged
    assert "VOICE-LINE." in out
    assert "( • ‿ • ) IDENTITY-LINE." in out
    assert "USER-LINE." in out


def test_voice_capsule_no_truncation(tmp_paths: Paths):
    """A 100-line / 8KB SOUL.md must render in full — no 1500B / 40-line
    cap. Persona files are load-bearing and small; truncation was the
    PR-B bug."""
    d = _seed_agent_dir(tmp_paths, "@orchestrator")
    body_lines = [f"line-{i:03d} this is non-trivial persona content."
                  for i in range(100)]
    (d / "SOUL.md").write_text("# soul\n\n" + "\n".join(body_lines) + "\n",
                                 encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@orchestrator")
    # First and last lines both present — no truncation at either end.
    assert "line-000" in out
    assert "line-099" in out


def test_voice_capsule_voice_md_alias_for_soul_md(tmp_paths: Paths):
    """Backward-compat: agents that still have VOICE.md (pre-rename)
    keep working. SOUL.md is preferred when both exist."""
    d = _seed_agent_dir(tmp_paths, "@legacy")
    (d / "VOICE.md").write_text("# voice\n\nLEGACY-VOICE-LINE.\n",
                                  encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@legacy")
    assert "LEGACY-VOICE-LINE." in out
    assert "## Voice" in out


def test_voice_capsule_soul_wins_over_voice(tmp_paths: Paths):
    """If both SOUL.md and VOICE.md exist, SOUL.md is used."""
    d = _seed_agent_dir(tmp_paths, "@dual")
    (d / "SOUL.md").write_text("# soul\n\nSOUL-WINS.\n", encoding="utf-8")
    (d / "VOICE.md").write_text("# voice\n\nVOICE-LOSES.\n", encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@dual")
    assert "SOUL-WINS." in out
    assert "VOICE-LOSES." not in out


def test_voice_capsule_identity_only_no_voice(tmp_paths: Paths):
    """Stranger agent has only IDENTITY.md — Voice section is omitted,
    Identity section + pointer line still emitted."""
    d = _seed_agent_dir(tmp_paths, "@id-only")
    (d / "IDENTITY.md").write_text(
        "# identity\n\nWARMTH-LINE.\n", encoding="utf-8"
    )
    out = ctx._render_voice_capsule(tmp_paths, "@id-only")
    assert "## Voice" not in out
    assert "## Identity" in out
    assert "WARMTH-LINE." in out
    assert "Persona files at" in out


def test_voice_capsule_no_files_returns_empty(tmp_paths: Paths):
    """Stranger install with no persona files at all → empty string,
    no pointer-line orphan."""
    _seed_agent_dir(tmp_paths, "@bare")
    out = ctx._render_voice_capsule(tmp_paths, "@bare")
    assert out == ""


def test_voice_capsule_strips_only_top_h1(tmp_paths: Paths):
    """## subheaders inside the body must survive — only the H1
    (single hash) on line 1 is stripped."""
    d = _seed_agent_dir(tmp_paths, "@struct")
    (d / "SOUL.md").write_text(
        "# top h1\n\n## a subheader\n\nbody under sub.\n",
        encoding="utf-8",
    )
    out = ctx._render_voice_capsule(tmp_paths, "@struct")
    assert "top h1" not in out
    assert "## a subheader" in out
    assert "body under sub." in out


def test_voice_capsule_drops_legacy_byte_and_line_caps(tmp_paths: Paths):
    """The _VOICE_BYTE_CAP / _VOICE_LINE_CAP constants are gone —
    nothing in metasphere.context references them anymore."""
    assert not hasattr(ctx, "_VOICE_BYTE_CAP")
    assert not hasattr(ctx, "_VOICE_LINE_CAP")


# ---------------------------------------------------------------------------
# Project-scoped agent resolution — context renderers must find persona /
# mission / status / task / child_reports under
# ~/.metasphere/projects/<proj>/agents/<id>/, not just the global
# ~/.metasphere/agents/<id>/. Pre-fix: paths.agent_dir() returned only the
# global path, so persistent project-scoped agents silently received zero
# persona injection — the renderers fell off the empty global dir without
# surfacing anything.
# ---------------------------------------------------------------------------


def _seed_project_agent_dir(tmp_paths: Paths, project: str, agent: str) -> Path:
    d = tmp_paths.project_agent_dir(project, agent)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_voice_capsule_resolves_project_scoped_agent(tmp_paths: Paths):
    d = _seed_project_agent_dir(tmp_paths, "acme", "@scoped")
    (d / "SOUL.md").write_text("# soul\n\nproject-scoped voice.\n", encoding="utf-8")
    (d / "USER.md").write_text("# user\n\nproject-scoped user-model.\n", encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@scoped")
    assert "project-scoped voice." in out
    assert "project-scoped user-model." in out


def test_mission_capsule_resolves_project_scoped_agent(tmp_paths: Paths):
    d = _seed_project_agent_dir(tmp_paths, "acme", "@scoped")
    (d / "MISSION.md").write_text(
        "# mission\n\nProject-scoped mission body line.\n", encoding="utf-8"
    )
    out = ctx._render_mission_capsule(tmp_paths, "@scoped")
    assert "## Mission" in out
    assert "Project-scoped mission body line." in out


def test_status_header_resolves_project_scoped_agent(tmp_paths: Paths):
    d = _seed_project_agent_dir(tmp_paths, "acme", "@scoped")
    (d / "status").write_text("active: persistent session", encoding="utf-8")
    out = ctx._render_status_header(tmp_paths, "@scoped")
    assert "active: persistent session" in out
    assert "_Status: unknown_" not in out


def test_voice_capsule_prefers_project_over_global(tmp_paths: Paths):
    # Both layers exist for the same id — project-scoped wins, matching
    # paths.find_agent_dir's tie-break.
    proj_d = _seed_project_agent_dir(tmp_paths, "acme", "@dual")
    (proj_d / "SOUL.md").write_text("# soul\n\nPROJECT-VOICE.\n", encoding="utf-8")
    glob_d = _seed_agent_dir(tmp_paths, "@dual")
    (glob_d / "SOUL.md").write_text("# soul\n\nGLOBAL-VOICE.\n", encoding="utf-8")
    out = ctx._render_voice_capsule(tmp_paths, "@dual")
    assert "PROJECT-VOICE." in out
    assert "GLOBAL-VOICE." not in out


def test_voice_capsule_sparse_project_duplicate_falls_back_per_file(tmp_paths: Paths):
    """A scoped SOUL override must not shadow global IDENTITY/USER files."""
    proj_d = _seed_project_agent_dir(tmp_paths, "acme", "@dual")
    (proj_d / "SOUL.md").write_text("# soul\n\nPROJECT-VOICE.\n", encoding="utf-8")
    glob_d = _seed_agent_dir(tmp_paths, "@dual")
    (glob_d / "IDENTITY.md").write_text("# identity\n\nGLOBAL-IDENTITY.\n", encoding="utf-8")
    (glob_d / "USER.md").write_text(
        "# user\n\nCurrent Project: Recurse\n", encoding="utf-8"
    )

    out = ctx._render_voice_capsule(tmp_paths, "@dual")
    assert "PROJECT-VOICE." in out
    assert "GLOBAL-IDENTITY." in out
    assert "Current Project: Recurse" in out


def test_build_context_budgets_persona_files_independently(tmp_paths: Paths, monkeypatch):
    """A large SOUL cannot truncate away deterministic USER facts."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    d = _seed_agent_dir(tmp_paths, "@orchestrator")
    (d / "SOUL.md").write_text("# soul\n\n" + ("voice detail\n" * 500), encoding="utf-8")
    (d / "IDENTITY.md").write_text("# identity\n\nSpot identity.\n", encoding="utf-8")
    (d / "USER.md").write_text("# user\n\nCurrent Project: Recurse\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "_render_memory_fts", lambda *a, **k: "")

    out = ctx.build_context(tmp_paths, budget=256, prompt="Tell me about Recurse")
    assert "_(truncated:" in out
    assert "Spot identity." in out
    assert "Current Project: Recurse" in out
