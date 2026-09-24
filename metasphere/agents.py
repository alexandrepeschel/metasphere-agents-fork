"""Agent lifecycle: spawn (ephemeral) and wake (persistent).

This module owns identity-directory creation, the spawn-vs-wake split, and
the tmux/REPL bring-up sequence for persistent agents.

Why this shape:
- Ephemeral spawn = provider-specific headless one-shot. No tmux session.
- Persistent wake = ``metasphere-<name>`` tmux session running a respawn
  loop around the configured agent REPL. Persistence is declared by the
  presence of ``MISSION.md`` in the agent dir.

Tmux paste-submission uses :mod:`metasphere.tmux`. The respawn loop and
readiness poll stay as direct tmux commands: this module just orchestrates
the tmux commands and the submit call.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re as _re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .events import log_event
from .io import atomic_write_text, file_lock
from .paths import Paths, home as _ms_home, resolve
from .tmux import submit_to_tmux as _tmux_submit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_PREFIX = "metasphere-"
_READY_TIMEOUT_S = 15
_READY_MARKERS = ("bypass permissions", "permissions: YOLO mode")


def _selected_agent_runtime(runtime: str | None = None) -> str:
    """Return the configured agent runtime, rejecting silent fallbacks."""
    selected = (
        runtime or os.environ.get("METASPHERE_AGENT_RUNTIME", "claude")
    ).strip().lower()
    if selected not in ("claude", "codex"):
        raise ValueError(f"unsupported agent runtime: {selected}")
    return selected


def _headless_runtime_command(
    harness: str,
    *,
    model: str = "",
    runtime: str | None = None,
) -> tuple[str, list[str]]:
    """Return ``(runtime, argv)`` for an ephemeral headless agent."""
    selected = _selected_agent_runtime(runtime)
    if selected == "claude":
        cmd = ["claude", "-p", harness, "--dangerously-skip-permissions"]
    else:
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            # Claude print mode works outside git repositories. Keep the
            # provider switch from narrowing valid Metasphere scopes.
            "--skip-git-repo-check",
            harness,
        ]
    if model:
        cmd.extend(["--model", model])
    return selected, cmd

# Above this body size, ``wake_persistent`` persists the task to the
# recipient's inbox and injects only a short pointer banner. The Claude
# Code TUI's bracketed-paste handler can silently drop multi-KB payloads
# — the partial body lands in the input box and C-m commits the
# truncated text. The repro case (2026-05-29) was a ~3KB dispatch
# that came up as an empty banner on the eng pane; the bootstrap-
# pointer workaround used outbox-path indirection to recover. 512B is
# well under the smallest payload that has reproduced truncation so far
# and leaves plenty of headroom inside the empirical safe zone.
_WAKE_BANNER_BODY_THRESHOLD = 512

# If a tmux session is "alive" but has had no activity for longer than
# this, wake_persistent treats it as a zombie (crashed/hung claude REPL)
# and cold-starts instead of injecting into the stale session. The old
# inject-path silently failed, leaving reap_dormant to kill the session
# moments later while the scheduler thought the task had been delivered.
_STALE_SESSION_THRESHOLD_DEFAULT = 7200


def _resolve_stale_threshold_sec() -> int:
    raw = os.environ.get("METASPHERE_STALE_SESSION_THRESHOLD_SEC")
    if raw is None or raw == "":
        return _STALE_SESSION_THRESHOLD_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        import sys as _sys
        print(
            f"metasphere: METASPHERE_STALE_SESSION_THRESHOLD_SEC expects an "
            f"integer, got {raw!r}; using default "
            f"{_STALE_SESSION_THRESHOLD_DEFAULT}",
            file=_sys.stderr,
        )
        return _STALE_SESSION_THRESHOLD_DEFAULT
    if v < 0:
        import sys as _sys
        print(
            f"metasphere: METASPHERE_STALE_SESSION_THRESHOLD_SEC must be "
            f"non-negative, got {v}; using default "
            f"{_STALE_SESSION_THRESHOLD_DEFAULT}",
            file=_sys.stderr,
        )
        return _STALE_SESSION_THRESHOLD_DEFAULT
    return v


_STALE_SESSION_THRESHOLD_SEC = _resolve_stale_threshold_sec()


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_name(name: str) -> str:
    return name if name.startswith("@") else "@" + name


_AGENT_NAME_INVALID_RE = _re.compile(r"[/\\\x00]")


def _validate_agent_name(name: str) -> None:
    """Reject empty/flag-shaped/path-bearing agent names before they hit
    disk.

    Without this a typo like ``metasphere agent spawn --help`` would
    create an agent dir literally named ``@--help`` — the same class of
    argv-leak that produced the ghost ``--help`` project entry fixed in
    df6812e. Validated at the library layer so every spawn site
    (CLI, future callers) gets the guard.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent name must be a non-empty string")
    bare = name[1:] if name.startswith("@") else name
    if not bare:
        raise ValueError("agent name must be non-empty after optional '@'")
    if bare.startswith("-"):
        raise ValueError(
            f"agent name looks like a CLI flag: {name!r} "
            f"(must not start with '-' after optional '@')"
        )
    if _AGENT_NAME_INVALID_RE.search(bare):
        raise ValueError(
            f"invalid agent name: {name!r} "
            f"(must not contain /, \\, or null)"
        )


def _tmux_bin() -> str:
    # Under pytest, hand back a guaranteed-nonexistent path so every
    # exec raises FileNotFoundError instead of reaching the host tmux
    # (cold-starting or killing REAL agent sessions from a sandboxed
    # test). session_alive and _tmux_run treat that as tmux-absent.
    from .tmux import PYTEST_TMUX_SENTINEL, tmux_sandboxed
    if tmux_sandboxed():
        return PYTEST_TMUX_SENTINEL
    return shutil.which("tmux") or "tmux"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

#: Recognised values for the ``<agent_dir>/class`` sidecar. Anything else
#: (missing file, empty, garbage) leaves the legacy MISSION.md heuristic in
#: charge.
_AGENT_CLASS_VALUES = ("ephemeral", "persistent")


@dataclass
class AgentRecord:
    name: str  # always with @ prefix
    scope: str
    parent: str
    status: str
    spawned_at: str
    mission_path: Optional[Path] = None
    pid_file: Optional[Path] = None
    agent_dir: Optional[Path] = None
    project: str = ""  # project name if project-scoped, empty if global
    #: Explicit lifecycle class read from ``<agent_dir>/class`` sidecar.
    #: ``"ephemeral"`` or ``"persistent"`` overrides the MISSION.md
    #: heuristic in :attr:`is_persistent`; ``None`` means no override.
    agent_class: Optional[str] = None

    @property
    def session_name(self) -> str:
        # Project-scoped agents include project in session name to avoid collisions
        if self.project:
            return _SESSION_PREFIX + self.project + "-" + _normalize_name(self.name)[1:]
        return session_name_for(self.name)

    @property
    def is_persistent(self) -> bool:
        # The class sidecar is the authoritative override. MISSION.md
        # presence remains the legacy fallback so agents predating the
        # sidecar keep their classification.
        if self.agent_class == "ephemeral":
            return False
        if self.agent_class == "persistent":
            return True
        return self.mission_path is not None and self.mission_path.is_file()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def session_name_for(agent_name: str) -> str:
    """Return the canonical tmux session name for ``agent_name``.

    Convention: ``metasphere-<name>`` with the leading ``@`` stripped.
    """
    return _SESSION_PREFIX + _normalize_name(agent_name)[1:]


def session_alive(name: str) -> bool:
    """True iff a tmux session named ``name`` exists."""
    try:
        r = subprocess.run(
            [_tmux_bin(), "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _read_text(p: Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return default


def _agent_record_from_dir(agent_dir: Path, project: str = "") -> AgentRecord:
    name = agent_dir.name  # already starts with @
    mission = agent_dir / "MISSION.md"
    pid_file = agent_dir / "pid"
    # Read project pointer if not provided
    if not project:
        project = _read_text(agent_dir / "project")
    class_str = _read_text(agent_dir / "class")
    agent_class = class_str if class_str in _AGENT_CLASS_VALUES else None
    return AgentRecord(
        name=name,
        scope=_read_text(agent_dir / "scope"),
        parent=_read_text(agent_dir / "parent"),
        status=_read_text(agent_dir / "status"),
        spawned_at=_read_text(agent_dir / "spawned_at"),
        mission_path=mission if mission.is_file() else None,
        pid_file=pid_file if pid_file.is_file() else None,
        agent_dir=agent_dir,
        project=project,
        agent_class=agent_class,
    )


def _list_agents_in_dir(agents_dir: Path, project: str = "") -> list[AgentRecord]:
    """List agents from a single agents/ directory."""
    if not agents_dir.is_dir():
        return []
    out: list[AgentRecord] = []
    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("@"):
            continue
        out.append(_agent_record_from_dir(entry, project=project))
    return out


def list_agents(paths: Paths | None = None, project: str = "") -> list[AgentRecord]:
    """Enumerate agents. Walks global + all project agent dirs.

    If ``project`` is specified, returns only agents for that project.
    """
    paths = paths or resolve()
    out: list[AgentRecord] = []

    if project:
        # Only look in the specified project
        project_agents = paths.project_agents_dir(project)
        out.extend(_list_agents_in_dir(project_agents, project=project))
    else:
        # Global agents
        out.extend(_list_agents_in_dir(paths.agents))
        # Walk all project agent dirs
        if paths.projects.is_dir():
            for proj_dir in sorted(paths.projects.iterdir()):
                if not proj_dir.is_dir():
                    continue
                proj_agents = proj_dir / "agents"
                if proj_agents.is_dir():
                    proj_name = proj_dir.name
                    out.extend(_list_agents_in_dir(proj_agents, project=proj_name))
    return out


def is_persistent(agent: AgentRecord) -> bool:
    return agent.is_persistent


# ---------------------------------------------------------------------------
# Harness rendering
# ---------------------------------------------------------------------------

def _render_harness(
    agent_id: str,
    scope_path: str,
    parent: str,
    task: str,
    timestamp: str,
    *,
    authority: str = "",
    responsibility: str = "",
    accountability: str = "",
) -> str:
    # Contract-first delegation (see agent-economy/NOTES-DEEPMIND-DELEGATION.md
    # for the mapping to DeepMind's Intelligent Delegation paper). If any
    # of the three fields are empty we still render a harness — legacy
    # spawns keep working — but the orchestrator is strongly nudged to
    # fill them in and the CLI/library layer warns on empty.
    contract = ""
    if authority or responsibility or accountability:
        contract = (
            "## Delegation Contract\n\n"
            "You were spawned under an explicit contract. Read it before\n"
            "you begin. If any field is ambiguous, do NOT guess — send\n"
            "`metasphere msg send @.. !query \"clarify: …\"` and wait.\n\n"
            f"### Authority (what you MAY do)\n\n{authority or '(unspecified — ask parent before acting)'}\n\n"
            f"### Responsibility (what you MUST produce)\n\n{responsibility or '(unspecified — ask parent)'}\n\n"
            f"### Accountability (how parent will verify)\n\n{accountability or '(unspecified — ask parent)'}\n\n"
            "Your `!done` message must include attestation: the concrete\n"
            "artifacts that satisfy Accountability (commit SHAs, test\n"
            "counts, files touched, paths, IDs — whatever the spec calls\n"
            "for). `!done` without attestation will be rejected.\n\n"
            "---\n\n"
        )

    return f"""# Agent: {agent_id}

You are **{agent_id}**, an autonomous agent working in the Metasphere system.

## Your Context

| Field | Value |
|-------|-------|
| Agent ID | {agent_id} |
| Scope | `{scope_path}` |
| Parent | {parent} |
| Spawned | {timestamp} |

## Your Task

{task}

---

{contract}You are autonomous. Work through your task systematically, communicate
status via `metasphere msg send @.. !info`, ask for help if blocked, and
complete your objective.

## Keep the lifecycle system alive

At every checkpoint, call `metasphere task update <id> "progress note"` to bump
`updated_at` — this tells the lifecycle consolidator you are still
alive on this task. Even "still working on X" counts. If you go silent
for more than 15 minutes, the consolidation cycle will ping you with a
`!query` status check, and after a few ignored pings it will escalate
to `@orchestrator` or `@user`. One line every 15 minutes is enough to
stay out of that loop.

When done:

    echo "complete: summary" > ~/.metasphere/agents/{agent_id}/status
    metasphere msg send @.. !done "Completed: <summary>\\n\\nAttestation: <concrete evidence>"
"""


# ---------------------------------------------------------------------------
# Spawn (ephemeral)
# ---------------------------------------------------------------------------

def _resolve_scope(scope_path: str, project_root: Path) -> Path:
    # An absolute filesystem path already inside project_root (e.g. read
    # from an agent's scope sidecar) must have the project_root prefix
    # stripped first — otherwise lstrip("/") below would treat it as a
    # project-relative path and produce
    # <project_root>/<project_root_without_leading_slash>.
    root_str = str(project_root)
    if scope_path == root_str or scope_path == "/":
        return project_root
    if scope_path.startswith(root_str + "/"):
        scope_path = scope_path[len(root_str):]
    if scope_path.startswith("/"):
        # Two cases for a leading-slash form that did NOT match project_root:
        # (a) a real absolute path to a directory in another project
        #     (cross-project spawn, e.g. /home/.../other.proj/) — use as-is.
        # (b) a project-relative absolute path by convention (e.g. "/scripts"
        #     means project_root/scripts) — concatenate under project_root.
        # Disambiguate by whether it exists on disk as a directory. Without
        # this, (a) would be mis-routed to (b) and produce the scope-doubling
        # bug: agent's scope sidecar gets <project_root>/home/.../other.proj/.
        candidate = Path(scope_path)
        if candidate.is_dir():
            return Path(str(candidate).rstrip("/"))
        s = project_root / scope_path.lstrip("/")
    else:
        # When the bare name contains no "/" it might be a registered project
        # name rather than a subdirectory of the current project.  Consult the
        # project registry first so that spawn(scope="writing") from inside
        # metasphere-agents resolves to /home/.../writing, not
        # /home/.../metasphere-agents/writing.
        if "/" not in scope_path:
            proj_json = _ms_home() / "projects" / scope_path / "project.json"
            if proj_json.is_file():
                try:
                    data = json.loads(proj_json.read_text())
                    registered = data.get("path", "")
                    if registered:
                        return Path(str(registered).rstrip("/"))
                except (json.JSONDecodeError, OSError):
                    pass
            # Fallback: canonical project.json is optional for projects
            # registered before PR #10 or whose project.json still lives
            # in-repo. Consult ~/.metasphere/projects.json directly so
            # those projects (e.g. "widget") don't fall through and
            # create a bare-name stub dir under project_root.
            registry_file = _ms_home() / "projects.json"
            if registry_file.is_file():
                try:
                    entries = json.loads(registry_file.read_text()) or []
                    for entry in entries:
                        if entry.get("name") == scope_path:
                            registered = entry.get("path", "")
                            if registered:
                                return Path(str(registered).rstrip("/"))
                            break
                except (json.JSONDecodeError, OSError):
                    pass
        s = project_root / scope_path
    return Path(str(s).rstrip("/"))


def _atomic_meta_write(agent_dir: Path, name: str, value: str) -> None:
    """Write a small metadata file under flock so concurrent spawn/wake
    calls cannot tear it."""
    target = agent_dir / name
    with file_lock(agent_dir / f".{name}.lock"):
        atomic_write_text(target, value if value.endswith("\n") else value + "\n")


def spawn_ephemeral(
    agent_name: str,
    scope_path: str,
    task: str,
    parent: str = "@orchestrator",
    paths: Paths | None = None,
    *,
    authority: str = "",
    responsibility: str = "",
    accountability: str = "",
    model: str = "",
) -> AgentRecord:
    """Create an ephemeral one-shot agent and (unless opted out) launch
    it through the configured runtime's headless command.

    The three optional contract fields — ``authority``, ``responsibility``,
    ``accountability`` — implement a minimum-viable version of the
    contract-first decomposition described in DeepMind's Intelligent
    Delegation paper (arxiv 2602.11865). When supplied they're rendered
    into the agent's harness as an explicit Delegation Contract block
    and persisted to the agent dir so the parent can reload them on
    ``!done`` verification. Legacy calls that omit them keep working.

    Honors ``METASPHERE_SPAWN_NO_EXEC=1`` to skip execution.
    """
    _validate_agent_name(agent_name)
    paths = paths or resolve()
    agent_id = _normalize_name(agent_name)
    timestamp = _utcnow()

    scope_abs = _resolve_scope(scope_path, paths.project_root)

    # Scope dirs (so messages/tasks have somewhere to land).
    (scope_abs / ".tasks" / "active").mkdir(parents=True, exist_ok=True)
    (scope_abs / ".tasks" / "completed").mkdir(parents=True, exist_ok=True)
    (scope_abs / ".messages" / "inbox").mkdir(parents=True, exist_ok=True)
    (scope_abs / ".messages" / "outbox").mkdir(parents=True, exist_ok=True)

    agent_dir = paths.agent_dir(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Name reuse: a leftover exit_self tombstone from a prior life would
    # make reap_crashed misread this agent's next genuine crash as a
    # clean exit — clear it before the new life starts.
    try:
        (agent_dir / _EXIT_SELF_TOMBSTONE).unlink(missing_ok=True)
    except OSError:
        pass

    _atomic_meta_write(agent_dir, "task", task)
    _atomic_meta_write(agent_dir, "status", f"spawned: {task}")
    _atomic_meta_write(agent_dir, "scope", str(scope_abs))
    _atomic_meta_write(agent_dir, "parent", parent)
    _atomic_meta_write(agent_dir, "spawned_at", timestamp)
    # Persist the contract so the parent can reload it on !done and run
    # verification against accountability. Empty strings are fine — they
    # just mean this spawn was legacy/no-contract.
    if authority:
        _atomic_meta_write(agent_dir, "authority", authority)
    if responsibility:
        _atomic_meta_write(agent_dir, "responsibility", responsibility)
    if accountability:
        _atomic_meta_write(agent_dir, "accountability", accountability)

    # Create a backing .tasks/active/<slug>.md and link it to the agent
    # via agent_dir/task_id. This is what lets posthook auto-archive the
    # task on clean exit (see metasphere.posthook.auto_close_finished_task).
    # Without this linkage, every ephemeral agent leaks a stale "pending"
    # task on the backlog — which is exactly the systemic gap that the
    # 2026-04-08 backlog audit found (10/23 active tasks were already
    # done but never closed).
    try:
        from .tasks import create_task

        backing = create_task(
            title=task,
            priority="!normal",
            scope=scope_abs,
            project_root=paths.project_root,
            created_by=parent,
            assigned_to=agent_id,
        )
        _atomic_meta_write(agent_dir, "task_id", backing.id)
    except Exception:  # noqa: BLE001 — task linkage is best-effort
        pass

    # Named-agent inbox (for direct addressing).
    (paths.root / "messages" / agent_id / "inbox").mkdir(parents=True, exist_ok=True)

    harness_path = agent_dir / "harness.md"
    harness = _render_harness(
        agent_id, scope_path, parent, task, timestamp,
        authority=authority,
        responsibility=responsibility,
        accountability=accountability,
    )
    atomic_write_text(harness_path, harness)

    # No initial !task message in scope — the harness already embeds
    # the task in its `Your Task` section, so this would be redundant
    # signaling. The previous version sent one anyway, which polluted
    # the parent's scope inbox with an orphan pinned !task per spawn
    # (since parent and child share scope=/, the parent saw every
    # spawn's task message and there was nobody to act on it: the
    # child already had the task via the harness, the parent was the
    # one who issued it). See `messages_done_self_loop.md` and
    # `ephemeral_gc_no_reader_followup.md` in memory for the broader
    # "no reader" thread that this is the third instance of.

    record = AgentRecord(
        name=agent_id,
        scope=str(scope_abs),
        parent=parent,
        status=f"spawned: {task}",
        spawned_at=timestamp,
        mission_path=None,
        agent_dir=agent_dir,
    )

    # Meta is shared across every agent.spawn event variant. Contract
    # fields are always included so the events log is a usable
    # provenance ledger even for legacy spawns (empty strings are fine).
    spawn_meta = {
        "child": agent_id,
        "scope": scope_path,
        "has_contract": bool(authority or responsibility or accountability),
        "authority": authority,
        "responsibility": responsibility,
        "accountability": accountability,
    }

    no_exec = os.environ.get("METASPHERE_SPAWN_NO_EXEC", "0") == "1"
    if no_exec:
        log_event(
            "agent.spawn",
            f"{agent_id} spawned at {scope_path} (no-exec)",
            agent=parent,
            meta={**spawn_meta, "no_exec": True},
            paths=paths,
        )
        return record

    runtime, cmd = _headless_runtime_command(harness, model=model)
    if shutil.which(runtime) is None:
        log_event(
            "agent.spawn",
            f"{agent_id} harness ready at {scope_path} ({runtime} not in PATH)",
            agent=parent,
            meta={**spawn_meta, "runtime": runtime, "runtime_missing": True},
            paths=paths,
        )
        return record

    log_file = agent_dir / "output.log"
    pid_file = agent_dir / "pid"

    env = os.environ.copy()
    env.update(
        {
            "METASPHERE_AGENT_ID": agent_id,
            "METASPHERE_SCOPE": str(scope_abs),
            "METASPHERE_PROJECT_ROOT": str(paths.project_root),
            "METASPHERE_DIR": str(paths.root),
            # Mark this headless one-shot as a metasphere-managed,
            # non-interactive session — same marker the gateway tmux
            # respawn loop sets. The UserPromptSubmit context hook
            # (metasphere/cli/context.py) injects the agent's context
            # only when this is set; without it the ephemeral agent
            # would be misread as an interactive human session and get
            # no context (issue #150). Also makes the PreToolUse deny
            # hook treat its (impossible-in-print-mode) interactive
            # prompts consistently.
            "METASPHERE_GATEWAY_SESSION": "1",
        }
    )

    cwd = scope_abs if scope_abs.is_dir() else paths.project_root
    log_fh = None
    try:
        log_fh = open(log_file, "ab")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # Popen dups the fd; we can drop our handle so the parent isn't
        # holding the log file open. Guarded so an open() failure above
        # doesn't AttributeError here.
        if log_fh is not None:
            log_fh.close()

    atomic_write_text(pid_file, str(proc.pid) + "\n")
    record.pid_file = pid_file

    log_event(
        "agent.spawn",
        f"{agent_id} spawned at {scope_path} (pid {proc.pid})",
        agent=parent,
        meta={**spawn_meta, "pid": proc.pid, "runtime": runtime},
        paths=paths,
    )
    return record


# ---------------------------------------------------------------------------
# Wake (persistent)
# ---------------------------------------------------------------------------

def _tmux_run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    tmux = _tmux_bin()
    try:
        return subprocess.run(
            [tmux, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check,
        )
    except FileNotFoundError:
        # tmux absent (or the pytest sentinel from _tmux_bin) — degrade
        # to a failed CompletedProcess so callers take their existing
        # rc!=0 branches instead of crashing.
        return subprocess.CompletedProcess(
            [tmux, *args], 127, stdout="", stderr="tmux not found",
        )


def _capture_pane(session: str) -> str:
    r = _tmux_run("capture-pane", "-p", "-t", session)
    return r.stdout if r.returncode == 0 else ""


def _find_agent_dir(agent_id: str, paths: Paths) -> Optional[Path]:
    """Search for an agent directory across global + all project dirs.

    Returns the first match, preferring project-scoped agents.
    """
    # Check project agent dirs first
    if paths.projects.is_dir():
        for proj_dir in sorted(paths.projects.iterdir()):
            if not proj_dir.is_dir():
                continue
            candidate = proj_dir / "agents" / agent_id
            if candidate.is_dir():
                return candidate
    # Fall back to global
    candidate = paths.agents / agent_id
    if candidate.is_dir():
        return candidate
    return None


def _wait_for_ready(session: str, timeout_s: int = _READY_TIMEOUT_S) -> bool:
    for _ in range(timeout_s):
        pane = _capture_pane(session)
        if any(marker in pane for marker in _READY_MARKERS):
            return True
        time.sleep(1)
    return False


def _submit_via_tmux(session: str, body: str) -> bool:
    """Submit text to a tmux session via :mod:`metasphere.tmux`.

    Returns the underlying :func:`metasphere.tmux.submit_to_tmux` bool so
    the caller can detect silent inject failures (missing tmux, session
    vanished mid-flight, etc.) and fall through to inbox-only delivery.
    Discarding this return was the root of the scheduled-task wake-gap
    (issue #106): a tmux failure looked indistinguishable from success
    and the task disappeared into a stamped-but-not-delivered state.

    Wakes pass ``escape_prefix=False``: an agent wake is a task-inject,
    not an interrupt. The pre-Escape was intended to interrupt a
    running turn so the new task becomes a NEW user-turn rather than
    queueing — but on an IDLE pane, Escape is not a no-op. It can
    trigger Claude Code's session-rating dialog ("How is Claude doing
    this session? 1: Bad 2: Fine 3: Good 0: Dismiss"), the Rewind/Undo
    menu, or other modal states. When that happens the subsequent
    typing + C-m races against the modal handler and the submit is
    eaten. 2026-04-20 test matrix: WITHOUT Escape → submits cleanly
    every time; WITH Escape on idle → triggered rating dialog on
    multiple distinct panes, typed content interleaved with modal,
    C-m unreliable.

    Claude Code queues keystrokes during a running turn and processes
    them when the turn completes — so queueing is safe. The old
    "interrupt running turn to jump the queue" rationale was wrong
    for wakes: we want our task to be processed, not to displace
    whatever the agent is doing mid-flight.
    """
    return _tmux_submit(session, body, escape_prefix=False)


def _prepare_wake_banner(
    target_agent: str,
    first_task: str,
    paths: Paths,
) -> str:
    """Render the per-wake banner that gets pasted into the recipient's
    pane.

    Short bodies inline as ``[task] <body>`` (no behavioural change for
    the common case). Bodies above ``_WAKE_BANNER_BODY_THRESHOLD`` are
    persisted as a ``!task`` message via :func:`messages.send_message`
    and the banner is reduced to a pointer the recipient can read at
    leisure. That bypasses the TUI's bracketed-paste cap entirely (see
    ``_WAKE_BANNER_BODY_THRESHOLD`` for the repro).

    ``send_message`` is called with ``wake=False`` so this helper does
    not double-fire the wake — the immediate ``_submit_via_tmux`` of
    the returned banner is the wake.

    Falls back to the inline form if ``send_message`` raises: losing
    the wake entirely is strictly worse than risking re-truncation.
    """
    if len(first_task.encode("utf-8")) <= _WAKE_BANNER_BODY_THRESHOLD:
        return f"[task] {first_task}"

    try:
        from .identity import resolve_agent_id
        from .messages import send_message
        sender = resolve_agent_id(paths)
        msg = send_message(
            target=target_agent,
            label="!task",
            body=first_task,
            from_agent=sender,
            paths=paths,
            wake=False,
        )
        return (
            f"[task] Long body persisted as {msg.id} from {sender}. "
            f"Read via: metasphere msg read {msg.id}"
        )
    except Exception:
        return f"[task] {first_task}"


def wake_persistent(
    agent_name: str,
    first_task: Optional[str] = None,
    paths: Paths | None = None,
    *,
    model: str = "",
) -> tuple[AgentRecord, bool]:
    """Wake (or attach to) a persistent agent's tmux+REPL session.

    If the session is already alive, only the optional task is injected —
    no new session is created.

    Returns ``(record, delivered)``. ``delivered`` is True when there was
    nothing to deliver (``first_task`` is None) or when the inject
    actually landed on the pane. False means the session existed but the
    pane-submit silently failed (gateway cascade-reap mid-fire, tmux
    binary gone, etc.); the caller should fall through to inbox-only
    delivery so the message is at least durably written. See issue #106.
    """
    paths = paths or resolve()
    agent_id = _normalize_name(agent_name)

    # Resolve agent directory: check project dirs first, then global
    agent_dir = _find_agent_dir(agent_id, paths)
    if agent_dir is None:
        agent_dir = paths.agent_dir(agent_id)
    mission = agent_dir / "MISSION.md"
    if not mission.is_file():
        raise ValueError(
            f"{agent_id} is not a persistent agent (no MISSION.md at {mission})"
        )

    project = _read_text(agent_dir / "project")

    # Resolve cwd (scope) with sensible precedence:
    # 1. explicit scope file on the agent dir (rare, power-user override)
    # 2. the agent's project filesystem path (so project-scoped agents
    #    start in their own project's checkout, not the harness checkout)
    # 3. the harness project_root (global agents)
    #
    # Without #2, project-scoped agents cold-start with cwd set to the
    # metasphere-agents checkout, which means claude-code picks up
    # metasphere-agents/.claude/settings.local.json — including hooks
    # with hardcoded paths that don't belong to the target project.
    scope_str = _read_text(agent_dir / "scope")
    if not scope_str and project:
        try:
            from . import project as _project
            proj = _project.get_project(project, paths=paths)
            if proj is not None and proj.path:
                scope_str = str(proj.path)
        except Exception:
            pass
    if not scope_str:
        scope_str = str(paths.project_root)

    rec = _agent_record_from_dir(agent_dir, project=project)
    session = rec.session_name  # uses project-aware naming

    if session_alive(session):
        # Shared two-layer stale-kill predicate (input-side idle signal
        # + generating-veto) — single-sourced with ``reap_dormant`` so
        # a future hardening lands at both call sites at once. Only
        # the threshold is wake-specific.
        stale, idle = _stale_and_not_generating(
            rec, agent_dir, session, _STALE_SESSION_THRESHOLD_SEC, paths,
        )
        if stale:
            killed = _tmux_run("kill-session", "-t", session)
            if killed.returncode != 0 and session_alive(session):
                try:
                    log_event(
                        "agent.session.cleanup_failed",
                        f"{agent_id} stale session {session} remains alive; "
                        "cold-start skipped and delivery deferred",
                        agent=agent_id,
                        meta={
                            "session": session,
                            "idle_seconds": idle,
                            "threshold_seconds": _STALE_SESSION_THRESHOLD_SEC,
                            "reason": "stale-wake",
                            "returncode": killed.returncode,
                            "stderr": (killed.stderr or "").strip(),
                        },
                        paths=paths,
                    )
                except Exception:
                    pass
                return rec, False
            try:
                log_event(
                    "agent.session",
                    f"{agent_id} stale session {session} killed before "
                    f"cold-start (idle={idle}s > {_STALE_SESSION_THRESHOLD_SEC}s)",
                    agent=agent_id,
                    meta={
                        "session": session,
                        "idle_seconds": idle,
                        "threshold_seconds": _STALE_SESSION_THRESHOLD_SEC,
                        "reason": "stale-wake",
                    },
                    paths=paths,
                )
            except Exception:
                pass
            # Fall through to cold-start below.
        else:
            delivered = True
            if first_task:
                banner = _prepare_wake_banner(agent_id, first_task, paths)
                delivered = _submit_via_tmux(session, banner)
            return rec, delivered

    # Cold start.
    _tmux_run("new-session", "-d", "-s", session, "-c", scope_str, check=False)
    _tmux_run("set-option", "-t", session, "mouse", "on")
    _tmux_run("set-option", "-t", session, "history-limit", "100000")

    # shlex.quote each value so apostrophes in scope/path don't break the shell.
    # CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 disables the TUI feedback
    # modal that otherwise captures input and causes stuck-paste
    # accumulation (2026-04-16). ``CLAUDE_CODE_DISABLE_NONESSENTIAL_
    # TRAFFIC=1`` is the superset that also suppresses the telemetry
    # pressing Dismiss would otherwise emit.
    env_export = (
        f"export METASPHERE_AGENT_ID={shlex.quote(agent_id)} "
        f"METASPHERE_SCOPE={shlex.quote(scope_str)} "
        f"METASPHERE_PROJECT_ROOT={shlex.quote(str(paths.project_root))} "
        f"METASPHERE_DIR={shlex.quote(str(paths.root))} "
        f"CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 "
        f"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
    )
    _tmux_run("send-keys", "-t", session, env_export, "Enter")

    from .gateway.session import _respawn_cmd

    # Thread the agent's class through to the respawn command so an
    # agent classed ``ephemeral`` runs claude once and idles at bash
    # (reap_ephemeral_idle's job) rather than looping forever and
    # turning a clean exit-self into a respawn-into-/exit-menu stall.
    agent_class = "ephemeral" if not rec.is_persistent else "persistent"
    respawn = _respawn_cmd(agent_id, model=model, agent_class=agent_class)
    _tmux_run("send-keys", "-t", session, respawn, "Enter")

    _atomic_meta_write(agent_dir, "status", "active: persistent session")
    _atomic_meta_write(agent_dir, "spawned_at", _utcnow())
    touch_last_active(agent_id, paths)  # prevent reap_dormant from killing a just-woken session

    log_event(
        "agent.session",
        f"{agent_id} session started via wake",
        agent=agent_id,
        paths=paths,
    )

    _wait_for_ready(session)
    # Clear stray buffer characters from the exec-bash transition.
    # NB: send-keys C-u (no Enter) — readline kill-line. Do not "fix" by
    # adding Enter.
    _tmux_run("send-keys", "-t", session, "C-u")
    time.sleep(0.2)

    delivered = True
    if first_task:
        banner = _prepare_wake_banner(agent_id, first_task, paths)
        delivered = _submit_via_tmux(session, banner)

    return _agent_record_from_dir(agent_dir, project=project), delivered


# ---------------------------------------------------------------------------
# Dormant GC
# ---------------------------------------------------------------------------

def _session_idle_seconds(session: str) -> Optional[int]:
    r = _tmux_run("display-message", "-t", session, "-p", "#{session_activity}")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        activity = int(r.stdout.strip())
    except ValueError:
        return None
    return max(0, int(time.time()) - activity)


# ---------------------------------------------------------------------------
# Last-input activity tracking (input-side counterpart to tmux activity).
#
# tmux's #{session_activity} reflects last terminal output, which can lag
# real input arbitrarily — a REPL fielding hooks while the model is
# thinking writes nothing visible to tmux for minutes at a time. Relying
# on it alone produced evt-1777144985427 (2026-04-25T19:23:05Z), which
# reaped @orchestrator's session after 86466s "idle" while the operator was
# actively chatting via telegram-inject. ``last_active`` is refreshed by
# every signal the agent actually processes — UserPromptSubmit, Stop,
# telegram-inject, heartbeat-tick — so ``reap_dormant`` can see input
# the tmux pane never displayed. tmux activity remains the fallback for
# agents whose sidecar predates this file.
# ---------------------------------------------------------------------------

_LAST_ACTIVE_FILENAME = "last_active"

#: Resident agent that the dormancy reaper is forbidden to time-reap.
#: Crash detection (``reap_crashed``) and explicit operator kills still
#: apply; this carve-out is strictly about the idle-time pathway.
_REAP_DORMANT_EXEMPT = ("@orchestrator",)


def touch_last_active(agent: str, paths: Paths | None = None) -> None:
    """Refresh ``<agent_dir>/last_active`` to now (UTC ISO).

    Called from every hook signal the agent actually processes:
    UserPromptSubmit, Stop, telegram-inject, heartbeat-tick. Best-effort
    by contract — these call sites run inside hook handlers that must
    never raise into the host, so any OS / lookup failure is silently
    swallowed.
    """
    try:
        paths = paths or resolve()
        name = _normalize_name(agent)
        # Project-scoped agents (e.g. @install-lead under metasphere-agents)
        # live at ~/.metasphere/projects/<proj>/agents/<id>/, not the
        # global ~/.metasphere/agents/<id>/. Writing to the global path
        # creates a MISSION-less ghost dir that the ephemeral GC sweeps,
        # which then races touch_last_active in a 5-min churn cycle.
        # find_agent_dir() prefers the project-scoped dir when one exists.
        agent_dir = paths.find_agent_dir(name) or paths.agent_dir(name)
        agent_dir.mkdir(parents=True, exist_ok=True)
        _atomic_meta_write(agent_dir, _LAST_ACTIVE_FILENAME, _utcnow())
    except Exception:  # noqa: BLE001 — hook-path; never raise
        pass


def touch_last_active_if_exists(agent: str, paths: Paths | None = None) -> bool:
    """Refresh ``last_active`` for ``agent`` iff its agent dir already
    exists; never creates one.

    The no-create contract is the whole point: callers like
    ``send_message`` fire for synthetic senders too (``@consolidate``,
    ``@heartbeat``, ``@scheduler``, ``@posthook``) and the mkdir in
    :func:`touch_last_active` would spawn MISSION-less ghost dirs that
    the ephemeral GC then sweeps in a churn cycle (see the comment
    there). Returns True iff a refresh was written. Best-effort — never
    raises.
    """
    try:
        paths = paths or resolve()
        agent_dir = _find_agent_dir(_normalize_name(agent), paths)
        if agent_dir is None:
            return False
        _atomic_meta_write(agent_dir, _LAST_ACTIVE_FILENAME, _utcnow())
        return True
    except Exception:  # noqa: BLE001 — activity signal is best-effort
        return False


def _last_active_idle_seconds(agent_dir: Path) -> Optional[int]:
    """Seconds since ``agent_dir/last_active`` was touched, or ``None``
    if the file is absent / empty / unparseable. ``None`` lets the
    caller fall back to the tmux activity signal so agents predating
    the file aren't permanently exempted from reap.
    """
    p = agent_dir / _LAST_ACTIVE_FILENAME
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        ts = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    delta = _dt.datetime.now(_dt.timezone.utc) - ts
    return max(0, int(delta.total_seconds()))


def _stale_and_not_generating(
    rec: AgentRecord,
    agent_dir: Optional[Path],
    session: str,
    threshold_seconds: int,
    paths: Paths,
) -> tuple[bool, Optional[int]]:
    """Shared stale-kill predicate for the reaper call sites
    (``reap_dormant`` and ``wake_persistent``'s pre-cold-start kill).
    Thresholds stay per-site; the *decision logic* is single-sourced —
    PR #6/#7/#10 all fixed the same "second call site missed the fix"
    class, and a third copy would re-create it.

    Returns ``(stale, idle_seconds)``; ``idle_seconds`` is surfaced so
    callers can log/status it. ``stale`` is True iff BOTH layers agree
    the session is safe to kill:

    1. Idle-signal preference — input-side ``last_active`` (refreshed
       by every hook signal the agent processes: UserPromptSubmit,
       Stop, telegram-inject, heartbeat-tick) wins over tmux
       ``session_activity``, which reflects terminal OUTPUT only and
       is blind to a silent multi-hour tool call (2026-07-05
       @worker killed at "idle=7648s" mid silent CPU probes).
       Unknown idle (no signal at all) is NOT stale.
    2. Liveness interlock — never kill a session generating output
       this instant, even if every hook signal went quiet past the
       threshold (a single multi-hour tool call that prints but fires
       no hook). Fails OPEN to the kill on any probe error: both
       consumers are core-loop kill paths and a probe fault must not
       wedge the sweep/wake. Local import avoids a circular dependency
       (liveness imports from agents).
    """
    idle: Optional[int] = None
    if agent_dir is not None:
        idle = _last_active_idle_seconds(agent_dir)
    if idle is None:
        idle = _session_idle_seconds(session)
    if idle is None or idle <= threshold_seconds:
        return False, idle

    from metasphere.liveness import GENERATING, agent_liveness

    try:
        generating = agent_liveness(rec, paths=paths).state == GENERATING
    except Exception:  # noqa: BLE001 — probe must never crash a kill path
        generating = False
    if generating:
        return False, idle
    return True, idle


def gc_dormant(paths: Paths | None = None, max_idle_seconds: int = 86400) -> list[str]:
    """Return persistent agent names whose tmux session has been idle
    longer than ``max_idle_seconds``. Does NOT kill — caller decides.
    """
    paths = paths or resolve()
    out: list[str] = []
    for agent in list_agents(paths):
        if not agent.is_persistent:
            continue
        session = agent.session_name
        if not session_alive(session):
            continue
        idle = _session_idle_seconds(session)
        if idle is not None and idle > max_idle_seconds:
            out.append(agent.name)
    return out


def reap_dormant(
    paths: Paths | None = None,
    max_idle_seconds: int = 86400,
) -> list[str]:
    """Transition persistent agents to dormant when they have been idle
    longer than ``max_idle_seconds``.

    Idle is taken from ``<agent_dir>/last_active`` when present (refreshed
    by hook signals: UserPromptSubmit, Stop, telegram-inject,
    heartbeat-tick) and falls back to the tmux ``session_activity`` field
    only when the sidecar is missing. The previous tmux-only path was
    blind to inputs the REPL processed without producing visible terminal
    output (see evt-1777144985427).

    @orchestrator is unconditionally exempt from time-based reap. Crash
    detection (``reap_crashed``) and operator-driven kills still apply,
    but the dormancy daemon never transitions the resident agent on any
    idle threshold.

    For each qualifying agent:
    - ``tmux kill-session -t <session>`` (silent no-op if already gone).
    - After a successful kill, write
      ``status = "dormant: idle Ns (auto-ttl at <utc>)"`` to the agent dir
      so ``metasphere status`` and human observers can see why the session
      went away. A failed kill leaves the active state intact for retry.
    - Persona files (``MISSION.md``, ``SOUL.md``, ``LEARNINGS.md``,
      ``HEARTBEAT.md``, contract sidecars) are preserved — a future
      ``metasphere agent wake <name>`` restarts cleanly from them.

    Returns the list of agent names transitioned. Failures on a single
    agent do not abort the others: this runs on a daemon tick and must
    never exit the gateway loop.
    """
    paths = paths or resolve()
    out: list[str] = []
    for agent in list_agents(paths):
        if not agent.is_persistent:
            continue
        # Resident-agent carve-out: @orchestrator never time-reaps.
        if agent.name in _REAP_DORMANT_EXEMPT:
            continue
        session = agent.session_name
        if not session_alive(session):
            continue
        # Shared two-layer stale-kill predicate (input-side idle signal
        # + generating-veto) — single-sourced with ``wake_persistent``'s
        # pre-cold-start kill so a future hardening lands at both call
        # sites at once. Only the TTL is dormancy-specific.
        stale, idle = _stale_and_not_generating(
            agent, agent.agent_dir, session, max_idle_seconds, paths,
        )
        if not stale:
            continue
        killed = _tmux_run("kill-session", "-t", session)
        if killed.returncode != 0:
            # Do not publish terminal state for a session tmux failed to
            # remove. Keeping it active also makes the next cadence retry.
            continue
        if agent.agent_dir is not None:
            try:
                _atomic_meta_write(
                    agent.agent_dir,
                    "status",
                    f"dormant: idle {idle}s (auto-ttl at {_utcnow()})",
                )
            except OSError:
                pass
        try:
            log_event(
                "agent.dormant",
                f"{agent.name} dormant after {idle}s idle — tmux session killed",
                agent=agent.name,
                meta={"idle_seconds": idle, "session": session},
                paths=paths,
            )
        except Exception:
            pass
        out.append(agent.name)
    return out


# ---------------------------------------------------------------------------
# Ephemeral idle reaper
#
# Counterpart to ``reap_dormant``: that handles persistent agents whose
# tmux session is alive but idle (24h default TTL → status=dormant +
# kill-session). This handles the ephemeral case — a cron-fired one-shot
# session whose claude REPL has exited (e.g. after
# ``metasphere session exit-self``) but whose tmux pane is sitting idle
# at the shell prompt with no respawn loop. ``reap_dormant`` explicitly
# skips non-persistent records, so without this sweep zombie panes
# accumulate indefinitely.
#
# Default 30-minute threshold: long enough that an active ephemeral
# isn't killed mid-task, short enough that zombies clean up within the
# hour.
# ---------------------------------------------------------------------------

def reap_ephemeral_idle(
    paths: Paths | None = None,
    max_idle_seconds: int = 1800,
) -> list[str]:
    """Kill ephemeral agent tmux sessions idle longer than ``max_idle_seconds``.

    Iterates ``tmux list-sessions``, looks up the matching AgentRecord by
    session name, and kills sessions where:
    - The record exists (defensive: unknown sessions are left alone — the
      operator may be using them, or they may belong to another tool).
    - The record is NOT persistent (persistent agents are reap_dormant's
      job, and they should stay alive on their own TTL).
    - The session is not the orchestrator's resident session
      (``metasphere-orchestrator``) — never reap the resident agent.
    - tmux ``session_activity`` reports idle > ``max_idle_seconds``.

    A reaped session is transitioned to a terminal ``complete:`` status.
    Otherwise the next crash sweep observes its dead pid and newly absent
    session, rewrites the stale active/spawned status to ``crashed:``, and
    emits a false parent alert for a one-shot session we intentionally killed.

    Returns the list of killed session names. Per-session failures are
    swallowed — this runs on a daemon tick and must never abort the
    gateway loop.
    """
    paths = paths or resolve()

    # Build a session-name → AgentRecord map across global + all projects.
    by_session: dict[str, AgentRecord] = {}
    for agent in list_agents(paths):
        by_session[agent.session_name] = agent

    # Resident orchestrator session is hardcoded by the gateway and
    # imported here lazily to avoid a circular import at module load.
    from .gateway.session import SESSION_NAME as _ORCH_SESSION

    out: list[str] = []
    r = _tmux_run("list-sessions", "-F", "#{session_name}")
    if r.returncode != 0:
        return out

    for line in r.stdout.splitlines():
        session = line.strip()
        if not session.startswith(_SESSION_PREFIX):
            continue
        if session == _ORCH_SESSION:
            continue
        rec = by_session.get(session)
        if rec is None:
            continue
        if rec.is_persistent:
            continue
        idle = _session_idle_seconds(session)
        if idle is None or idle <= max_idle_seconds:
            continue
        killed = _tmux_run("kill-session", "-t", session)
        if killed.returncode != 0:
            # Keep the non-terminal status and deferred marker intact so the
            # next daemon cadence can retry. Claiming completion here would
            # hide a still-live zombie session after a tmux failure.
            continue
        if rec.agent_dir is not None:
            try:
                _atomic_meta_write(
                    rec.agent_dir,
                    "status",
                    f"complete: ephemeral idle session reaped after {idle}s",
                )
            except OSError:
                pass
        # Unlink any leftover ``state/<agent>_deferred_cmd`` marker.
        # Without this, a stale "/exit" marker (left when a posthook
        # never ran — e.g. the cron-fired session was killed externally
        # before its Stop hook fired) would inject /exit into the next
        # cron-fire's session before it could do any work (2026-04-30
        # research-monitor markers found by @explorer 2026-05-05).
        safe_name = rec.name.lstrip("@") or "orchestrator"
        marker = paths.state / f"{safe_name}_deferred_cmd"
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            log_event(
                "agent.ephemeral_idle.reap",
                f"{rec.name} ephemeral session {session} killed "
                f"after {idle}s idle (threshold {max_idle_seconds}s)",
                agent=rec.name,
                meta={
                    "session": session,
                    "idle_seconds": idle,
                    "threshold_seconds": max_idle_seconds,
                },
                paths=paths,
            )
        except Exception:
            pass
        out.append(session)
    return out


# ---------------------------------------------------------------------------
# Crash reaper (silent-death detection)
#
# Counterpart to ``reap_dormant``: that handles the well-behaved case
# where a persistent agent's tmux session is alive but idle. This handles
# the misbehaved case where the agent process died without writing a
# terminal status — leaving a stale ``status: spawned: ...`` (or
# ``working:``) on disk while the pid is gone and no tmux session
# exists. Without this hook, silent-death agents are invisible until a
# human notices the orphan status file.
#
# 2026-04-21 spawn-stall incidents (PR #35 followup): @service-restart-impl
# died at spawn-time before writing any output; the only signal was a
# stale ``status=spawned`` with no tmux session. ``reap_crashed`` closes
# that gap by promoting the implicit "no pid + no session" state to an
# explicit ``crashed:`` status and pushing a ``!alert`` to the parent.
# ---------------------------------------------------------------------------

#: Status prefixes treated as terminal — ``reap_crashed`` will not
#: re-transition an agent already in one of these states. Membership
#: only; order is irrelevant.
_TERMINAL_STATUS_PREFIXES = (
    "complete:",
    "dormant:",
    "crashed:",
    "failed:",
)

#: Sidecar written by ``metasphere session exit-self`` (via
#: :func:`mark_exit_self`) announcing a clean, intentional
#: self-termination. ``reap_crashed`` consults it to transition such
#: agents to ``complete:`` instead of ``crashed:`` — without it, every
#: clean ephemeral exit the reaper visits before cleanup reads as a
#: silent death (2026-07-05 @writing-lead false crash ``!alert``).
_EXIT_SELF_TOMBSTONE = "exit_self"


def mark_exit_self(
    agent_name: str, session: str, paths: Paths | None = None,
) -> bool:
    """Record that ``agent_name`` is about to terminate its own session.

    Writes the ``exit_self`` tombstone sidecar into the agent's dir so
    the next ``reap_crashed`` sweep classifies the upcoming
    pid-dead/session-gone state as a clean exit, not a crash. Returns
    ``False`` (never raises) when the agent dir cannot be found or the
    write fails — the caller's kill path must not be blocked by a
    bookkeeping failure.
    """
    paths = paths or resolve()
    agent_dir = _find_agent_dir(_normalize_name(agent_name), paths)
    if agent_dir is None:
        return False
    try:
        _atomic_meta_write(
            agent_dir, _EXIT_SELF_TOMBSTONE, f"{_utcnow()} session {session}",
        )
    except OSError:
        return False
    return True


def _exit_self_tombstone_current(
    tomb: Path, pid_file: Optional[Path],
) -> bool:
    """True iff ``tomb`` exists and belongs to the agent's current life.

    A tombstone older than the pid file is stale: the agent was
    (re)launched after the exit intent was recorded, so a later
    pid-dead/session-gone state is a genuine crash and must not be
    masked by the leftover sidecar.
    """
    try:
        tomb_mtime = tomb.stat().st_mtime
    except OSError:
        return False
    if pid_file is not None:
        try:
            if pid_file.stat().st_mtime > tomb_mtime:
                return False
        except OSError:
            pass
    return True


def _pid_is_zombie(pid: int) -> bool:
    """True iff ``pid`` is a zombie — exited, awaiting a parent ``wait()``.

    Linux only, via ``/proc/<pid>/status``. Anywhere without procfs this
    returns False, which restores the pre-2026-09-24 behaviour rather than
    guessing: a wrong "dead" would crash-alert on a live agent.
    """
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("State:"):
                    # "State:\tZ (zombie)" -> "Z"
                    parts = line.split()
                    return len(parts) >= 2 and parts[1] == "Z"
    except OSError:
        pass
    return False


def _pid_alive(pid: int) -> bool:
    """True iff process ``pid`` exists *and is still running*.

    Uses signal-0 — the canonical no-op liveness probe (see kill(2)).
    A ``PermissionError`` means the process exists but is owned by
    another uid; that still counts as alive (we cannot disprove it).

    **Zombies count as dead, and this is the whole point of the function.**
    signal-0 succeeds against a zombie: the process has exited but its pid
    table entry survives until the parent reaps it, and a parent that never
    calls ``wait()`` keeps that entry forever. So the canonical probe
    reports a dead agent as alive indefinitely, and
    :func:`reap_crashed` — whose entire job is catching silent deaths —
    skips the one class of death it can never later detect.

    Seen in practice: three ephemeral agents died in the same minute. Two
    were reaped normally and alerted; the third sat defunct for hours in
    status ``spawned:``, raised no ``!alert``, and its output simply never
    arrived. The only difference was who held the pid entry.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another uid's process. We cannot read its /proc status either,
        # so we cannot disprove liveness — say alive, as before.
        return True
    except OSError:
        return False
    return not _pid_is_zombie(pid)


def reap_crashed(paths: Paths | None = None) -> list[str]:
    """Detect agents that died silently and promote them to ``crashed:``.

    For every non-terminal agent with a recorded pid file:
    - If both the pid is dead (``os.kill(pid, 0)`` → ``ProcessLookupError``)
      AND the agent's tmux session is gone, atomically rewrite ``status``
      to ``crashed: pid <N> dead, session gone`` and send a ``!alert``
      message to the agent's parent (read from the ``parent`` sidecar)
      via :func:`metasphere.messages.send_message`.
    - Skips agents already in a terminal status
      (``complete:`` / ``dormant:`` / ``crashed:`` / ``failed:``).
    - Skips agents with no pid file (no liveness signal recorded — this
      is the legacy ``METASPHERE_SPAWN_NO_EXEC`` case and the
      pre-pid-write window during spawn).
    - Skips the parent ``!alert`` if the ``parent`` sidecar is missing
      (no addressee), but still writes the ``crashed:`` status.
    - Agents carrying a current ``exit_self`` tombstone (see
      :func:`mark_exit_self`) are transitioned to ``complete:`` instead
      — a clean self-termination the sweep visited before cleanup is
      not a crash, and must not ``!alert`` the parent.

    Per-agent failures are swallowed — this runs on a daemon tick and
    must never abort the gateway loop. Returns the list of agent names
    that were transitioned this sweep.
    """
    paths = paths or resolve()
    out: list[str] = []
    for agent in list_agents(paths):
        status = agent.status or ""
        if any(status.startswith(p) for p in _TERMINAL_STATUS_PREFIXES):
            continue
        if agent.pid_file is None:
            continue
        try:
            pid = int(agent.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            continue
        session = agent.session_name
        if session_alive(session):
            continue

        if agent.agent_dir is None:
            continue

        # Both signals dead, but an exit_self tombstone from this life
        # means the agent killed its own session on purpose — a clean
        # exit the sweep merely beat cleanup to. Transition to
        # ``complete:`` and skip the parent ``!alert``.
        tomb = agent.agent_dir / _EXIT_SELF_TOMBSTONE
        if _exit_self_tombstone_current(tomb, agent.pid_file):
            try:
                _atomic_meta_write(
                    agent.agent_dir, "status",
                    "complete: exit_self (clean self-termination)",
                )
            except OSError:
                continue
            try:
                tomb.unlink()
            except OSError:
                pass
            try:
                log_event(
                    "agent.exited",
                    f"{agent.name} clean exit_self confirmed — pid {pid} "
                    f"dead, session {session} gone, tombstone consumed",
                    agent=agent.name,
                    meta={"pid": pid, "session": session},
                    paths=paths,
                )
            except Exception:
                pass
            out.append(agent.name)
            continue

        # Both signals say the agent is gone — silent death.
        reason = f"crashed: pid {pid} dead, session gone"
        try:
            _atomic_meta_write(agent.agent_dir, "status", reason)
        except OSError:
            # If we can't even write the status, the alert would be
            # misleading — bail on this agent and let the next sweep
            # try again.
            continue

        if agent.parent:
            try:
                from . import messages as _messages
                _messages.send_message(
                    target=agent.parent,
                    label="!alert",
                    body=(
                        f"{agent.name} silent death detected by "
                        f"reap_crashed: pid {pid} no longer running and "
                        f"tmux session {session} not present. Status "
                        "transitioned to crashed."
                    ),
                    from_agent=agent.name,
                    paths=paths,
                    wake=False,
                )
            except Exception:
                pass

        try:
            log_event(
                "agent.crashed",
                f"{agent.name} silent death — pid {pid} dead, "
                f"session {session} gone",
                agent=agent.name,
                meta={"pid": pid, "session": session, "parent": agent.parent},
                paths=paths,
            )
        except Exception:
            pass
        out.append(agent.name)
    return out


# ---------------------------------------------------------------------------
# !done delivery hook (session hygiene)
#
# When an ephemeral agent sends !done it has no reason to keep its
# tmux pane (if any) or process linkage around — the parent's
# Accountability check runs against artifacts on disk, not against the
# child process. Persistent agents must NOT be killed on !done: they
# may send multiple !dones over their lifetime, and their session
# lifecycle is governed by ``reap_dormant`` (idle-TTL) instead.
# Classification reads ``AgentRecord.is_persistent``, so the
# ``<agent_dir>/class`` sidecar override flips behaviour the same way
# the rest of the lifecycle does.
# ---------------------------------------------------------------------------

def on_done_delivered(sender: str, paths: Paths | None = None) -> Optional[str]:
    """Fired from :func:`metasphere.messages.send_message` right after a
    ``!done`` message is delivered to its target. Kills the sender's
    tmux session and clears runtime state pointers iff the sender
    classifies as ephemeral via :attr:`AgentRecord.is_persistent`
    (``<agent_dir>/class`` sidecar override or — absent the sidecar —
    no ``MISSION.md``). Persistent senders are a no-op — their
    lifecycle is governed by idle-TTL dormancy.

    Returns the killed session name if an ephemeral cleanup ran, else
    ``None``. Persona files (``harness.md``, ``authority``,
    ``responsibility``, ``accountability``, ``scope``, ``parent``,
    ``spawned_at``) are preserved so the GC log and later audits can
    still reconstruct what the agent was contracted to do.
    """
    paths = paths or resolve()
    if not sender or not isinstance(sender, str):
        return None
    # Skip non-agent senders: user, scope targets, parent aliases.
    # A real agent id looks like "@name" (no slash, no dots).
    if not sender.startswith("@"):
        return None
    if sender in ("@user", "@.", "@.."):
        return None
    if "/" in sender:  # scope-path like "@/abs/path/"
        return None
    agent_id = _normalize_name(sender)
    agent_dir = _find_agent_dir(agent_id, paths)
    if agent_dir is None:
        return None
    project = _read_text(agent_dir / "project")
    rec = _agent_record_from_dir(agent_dir, project=project)
    if rec.is_persistent:
        return None  # persistent agents are not killed on !done

    session = rec.session_name
    # Kill-session is idempotent: rc=1 when session absent, which is exactly
    # what we want for headless-Popen ephemerals that never had a tmux pane.
    # But the same nonzero result can mean a live session could not be killed;
    # distinguish those cases before erasing the only runtime pointers.
    killed = _tmux_run("kill-session", "-t", session)
    if killed.returncode != 0 and session_alive(session):
        try:
            log_event(
                "agent.ephemeral_done.cleanup_failed",
                f"{agent_id} !done delivered but tmux session {session} "
                "remains alive; runtime state preserved for retry",
                agent=agent_id,
                meta={
                    "session": session,
                    "returncode": killed.returncode,
                    "stderr": (killed.stderr or "").strip(),
                },
                paths=paths,
            )
        except Exception:
            pass
        return None

    # Clear runtime state pointers so a future spawn with the same name
    # bootstraps from scratch. Persona/contract/harness files survive.
    for name in ("pid", "task_id"):
        try:
            f = agent_dir / name
            if f.exists():
                f.unlink()
        except OSError:
            pass
    try:
        _atomic_meta_write(agent_dir, "status", "complete: !done delivered")
    except OSError:
        pass

    try:
        log_event(
            "agent.ephemeral_done",
            f"{agent_id} !done delivered — tmux killed + runtime state cleared",
            agent=agent_id,
            meta={"session": session},
            paths=paths,
        )
    except Exception:
        pass
    return session
