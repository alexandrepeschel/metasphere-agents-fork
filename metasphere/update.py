"""Configurable auto-updates for metasphere hosts.

Operator hosts keep themselves current with main without manual
intervention. This module owns:

* parsing ``$METASPHERE_DIR/config/auto-update.env``
* registering / unregistering the cron job in
  ``$METASPHERE_DIR/schedule/jobs.json``
* the actual update flow (git pull / claude integration / daemon restart
  / re-pip-install / tests / telegram notify)
* status / enable / disable wrappers driven by the CLI

All subprocess shelling is contained in module-level helpers so tests can
monkeypatch them. The legacy ``scripts/metasphere update`` bash path was
retired; see git history if you need the shell version.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import shlex
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import schedule as _sched
from .paths import Paths, resolve

logger = logging.getLogger(__name__)


# ---------- config ----------

CONFIG_FILENAME = "auto-update.env"
LOG_FILENAME = "auto-update.log"
STATE_FILENAME = "auto-update.state.json"
JOB_ID = "metasphere-auto-update"
JOB_NAME = "metasphere:auto-update"
_VENV_METASPHERE_BIN = "venv/bin/metasphere"

INTERVAL_TO_CRON = {
    "daily": "0 4 * * *",
    "hourly": "0 * * * *",
    "6h": "0 */6 * * *",
}


@dataclass
class AutoUpdateConfig:
    enabled: bool = False
    interval: str = "daily"
    branch: str = "main"
    restart_daemons: bool = True
    notify: bool = True

    def cron_expr(self) -> str:
        return interval_to_cron(self.interval)

    def to_env_text(self) -> str:
        return (
            "# metasphere auto-update configuration\n"
            "# Managed by `metasphere update --enable|--disable`.\n"
            f"AUTO_UPDATE_ENABLED={'true' if self.enabled else 'false'}\n"
            f"AUTO_UPDATE_INTERVAL={self.interval}\n"
            f"AUTO_UPDATE_BRANCH={self.branch}\n"
            f"AUTO_UPDATE_RESTART_DAEMONS={'true' if self.restart_daemons else 'false'}\n"
            f"AUTO_UPDATE_NOTIFY={'true' if self.notify else 'false'}\n"
        )


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def parse_env_text(text: str) -> AutoUpdateConfig:
    """Parse the simple ``KEY=VALUE`` format. Unknown keys ignored."""
    cfg = AutoUpdateConfig()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "AUTO_UPDATE_ENABLED":
            cfg.enabled = _truthy(v)
        elif k == "AUTO_UPDATE_INTERVAL":
            cfg.interval = v or "daily"
        elif k == "AUTO_UPDATE_BRANCH":
            cfg.branch = v or "main"
        elif k == "AUTO_UPDATE_RESTART_DAEMONS":
            cfg.restart_daemons = _truthy(v)
        elif k == "AUTO_UPDATE_NOTIFY":
            cfg.notify = _truthy(v)
    return cfg


def config_path(paths: Paths | None = None) -> Path:
    paths = paths or resolve()
    return paths.config / CONFIG_FILENAME


def log_path(paths: Paths | None = None) -> Path:
    paths = paths or resolve()
    return paths.logs / LOG_FILENAME


def state_path(paths: Paths | None = None) -> Path:
    paths = paths or resolve()
    return paths.state / STATE_FILENAME


def load_config(paths: Paths | None = None) -> AutoUpdateConfig:
    p = config_path(paths)
    if not p.exists():
        return AutoUpdateConfig()
    return parse_env_text(p.read_text(encoding="utf-8"))


def save_config(cfg: AutoUpdateConfig, paths: Paths | None = None) -> Path:
    p = config_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cfg.to_env_text(), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:  # pragma: no cover - non-fatal on weird FS
        pass
    return p


# ---------- cron expression ----------

_CRON_FIELD_RE = ("*/", "-", ",", "*")


def looks_like_cron(s: str) -> bool:
    parts = s.strip().split()
    if len(parts) != 5:
        return False
    return any(any(tok in p for tok in _CRON_FIELD_RE) or p.isdigit() for p in parts)


def interval_to_cron(interval: str) -> str:
    """Map an interval keyword to a cron expression.

    If ``interval`` already looks like a 5-field cron expression, return
    it verbatim (allows ``AUTO_UPDATE_INTERVAL='*/15 * * * *'`` for tests
    and staging).
    """
    if interval in INTERVAL_TO_CRON:
        return INTERVAL_TO_CRON[interval]
    if looks_like_cron(interval):
        return interval
    return INTERVAL_TO_CRON["daily"]


# ---------- schedule integration ----------

def _metasphere_binary(paths: Paths | None = None) -> str:
    """Return a stable absolute path to the ``metasphere`` console script.

    Prefer the canonical runtime venv under ``METASPHERE_DIR``. The command
    registering the job may itself be launched from an unrelated project's
    virtualenv; persisting that interpreter would make the daily updater
    depend on the lifetime and installed package version of that project.

    Fall back to the invoking interpreter's sibling entry point for fresh or
    non-standard installations where the canonical runtime does not exist.
    Using an absolute path avoids relying on the schedule daemon's ``PATH``.
    """
    paths = paths or resolve()
    canonical = paths.root / _VENV_METASPHERE_BIN
    if canonical.is_file():
        return str(canonical)
    candidate = Path(sys.executable).with_name("metasphere")
    if candidate.is_file():
        return str(candidate)
    return "metasphere"  # last-resort fallback


def build_job(cfg: AutoUpdateConfig, paths: Paths | None = None) -> _sched.Job:
    """Construct the auto-update Job for jobs.json."""
    cmd = f"{_metasphere_binary(paths)} update --quiet"
    return _sched.Job(
        id=JOB_ID,
        source="auto-update",
        source_id=JOB_ID,
        agent_id="auto-update",
        name=JOB_NAME,
        enabled=cfg.enabled,
        kind="cron",
        cron_expr=cfg.cron_expr(),
        tz="UTC",
        payload_kind="command",
        payload_message=cmd,
        imported_at=int(time.time()),
        command=cmd,
        full_command=cmd,
    )


def register_job(cfg: AutoUpdateConfig, paths: Paths | None = None) -> _sched.Job:
    """Idempotently install/refresh the auto-update job from ``cfg``.

    Adds the job if missing, updates ``cron_expr`` and ``enabled`` if
    present. Returns the persisted Job.
    """
    paths = paths or resolve()
    paths.schedule.mkdir(parents=True, exist_ok=True)
    new_job = build_job(cfg, paths)
    with _sched.with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        replaced = False
        for i, j in enumerate(jobs):
            if j.id == JOB_ID:
                # Preserve last_fired_at across re-registration so we
                # don't double-fire after a reinstall.
                new_job.last_fired_at = j.last_fired_at
                jobs[i] = new_job
                replaced = True
                break
        if not replaced:
            jobs.append(new_job)
        _sched.save_jobs(jobs, paths, _input_count=input_count)
    return new_job


def unregister_job(paths: Paths | None = None) -> bool:
    paths = paths or resolve()
    if not paths.schedule_jobs.exists():
        return False
    with _sched.with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        kept = [j for j in jobs if j.id != JOB_ID]
        if len(kept) == input_count:
            return False
        # Shrink-detection: only refuses 0-out-of-N. We have N-1 here so
        # save is allowed even when N==1 (kept=[] and input_count=1
        # would trip the guard) — handle that case explicitly.
        if not kept and input_count > 0:
            # Write directly past the shrink guard (intentional removal).
            paths.schedule_jobs.write_text("[]\n", encoding="utf-8")
            return True
        _sched.save_jobs(kept, paths, _input_count=input_count)
    return True


# ---------- state ----------

def load_state(paths: Paths | None = None) -> dict:
    p = state_path(paths)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, paths: Paths | None = None) -> None:
    p = state_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------- update flow ----------

@dataclass
class UpdateResult:
    ok: bool
    old_hash: str = ""
    new_hash: str = ""
    commits: int = 0
    subjects: list[str] = dataclasses.field(default_factory=list)
    reason: str = ""
    pip_reinstalled: bool = False
    tests_passed: Optional[bool] = None
    daemons_restarted: bool = False

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d


GitRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _git(repo: Path) -> GitRunner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return run


def _head_hash(repo: Path, runner: GitRunner | None = None) -> str:
    g = runner or _git(repo)
    r = g(["rev-parse", "HEAD"])
    return (r.stdout or "").strip()


def _commit_subjects(repo: Path, old: str, new: str, runner: GitRunner | None = None) -> list[str]:
    if not old or not new or old == new:
        return []
    g = runner or _git(repo)
    r = g(["log", "--pretty=%s", f"{old}..{new}"])
    return [line for line in (r.stdout or "").splitlines() if line]


def _has_python_changes(repo: Path, old: str, new: str, runner: GitRunner | None = None) -> bool:
    if not old or not new or old == new:
        return False
    g = runner or _git(repo)
    r = g(["diff", "--name-only", f"{old}..{new}"])
    for f in (r.stdout or "").splitlines():
        if f == "pyproject.toml" or f.startswith("metasphere/"):
            return True
    return False


def _venv_python() -> Path | None:
    """Best-effort: find the venv that owns the running python."""
    exe = Path(sys.executable)
    return exe if exe.exists() else None


def _logf(paths: Paths) -> Path:
    p = log_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_log(paths: Paths, line: str) -> None:
    ts = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with _logf(paths).open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {line}\n")


def _venv_python(paths: Paths) -> Path:
    """Canonical metasphere venv python interpreter.

    Located under ``$METASPHERE_DIR/venv/bin/python`` on Linux/macOS.
    ``paths.root`` resolves ``METASPHERE_DIR`` with the same env-var
    precedence as the rest of the harness.
    """
    return paths.root / "venv" / "bin" / "python"


def _ensure_venv(
    paths: Paths,
    repo: Path,
    log: Callable[[str], None],
) -> Path:
    """Return ``$METASPHERE_DIR/venv/bin/python``, creating the venv
    on-demand if it doesn't exist yet.

    This is the self-migration path: hosts installed before venv-first
    (pre-2026-04-16) hit PEP 668 on ``pip install -e .`` against the
    system Python. By lazily creating the venv on the first update
    that needs it, we fix those hosts without requiring the user to
    re-run install.sh.

    If venv creation or the initial bootstrap install fails, raises
    ``RuntimeError`` with a clear message so ``run_update`` surfaces it.
    """
    venv_python = _venv_python(paths)
    if venv_python.exists():
        return venv_python
    venv_dir = venv_python.parent.parent
    log(f"auto-update: creating venv at {venv_dir}")
    r = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"venv creation at {venv_dir} failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout or '').strip()}. "
            "Is python3-venv installed? (apt install python3-venv)"
        )
    log(f"auto-update: bootstrapping metasphere into new venv")
    r = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-e", str(repo),
         "--quiet", "--no-warn-script-location"],
        capture_output=True, text=True, check=False, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"initial pip install into venv failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout or '').strip()}"
        )
    return venv_python


def _venv_pip_runner(venv_python: Path) -> Callable[[list[str]], int]:
    """Return a pip_runner callable that invokes pip via the venv's
    python instead of ``sys.executable``. Same signature as
    ``_default_pip_runner`` so it drops in at the callsite."""
    def run(args: list[str]) -> int:
        return subprocess.run(
            [str(venv_python), *args],
            check=False, timeout=300,
        ).returncode
    return run


def _dirty_paths(runner: GitRunner) -> list[str]:
    """Return the list of dirty porcelain lines from ``git status``.

    Empty list = clean tree. Each element is a raw ``XY path`` line from
    ``git status --porcelain``, so callers can both count (truthiness)
    and render a useful error message.
    """
    r = runner(["status", "--porcelain"])
    if r.returncode != 0:
        # Fail closed on an unusable status call — treat "can't tell"
        # as "dirty" rather than silently proceeding to reset --hard.
        return [f"(git status failed rc={r.returncode})"]
    return [line for line in (r.stdout or "").splitlines() if line.strip()]


def _git_pull_or_reset(repo: Path, branch: str, runner: GitRunner) -> None:
    """Fast-forward ``repo`` to ``origin/<branch>`` with a hard-reset fallback.

    Refuses to proceed if the working tree has uncommitted changes —
    the fallback is ``git reset --hard``, which silently destroys WIP.
    Hit an operator on 2026-04-16 (10 files of uncommitted tmux work erased
    by a wake-triggered auto-update). Caller must commit, stash, or
    explicitly discard before re-running.

    Mirrors the bash ``git pull --ff-only`` → ``git fetch && git reset --hard``
    chain from the retired ``scripts/metasphere update`` path. Raises
    ``RuntimeError`` if the tree is dirty or if both strategies fail.
    """
    dirty = _dirty_paths(runner)
    if dirty:
        preview = "\n  ".join(dirty[:20])
        more = f"\n  ...and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        raise RuntimeError(
            "refusing to update: working tree has uncommitted changes.\n"
            "The reset --hard fallback would silently destroy them.\n"
            "Commit, stash, or `git checkout -- .` before re-running.\n"
            f"Dirty paths:\n  {preview}{more}"
        )
    ff = runner(["pull", "--ff-only", "origin", branch])
    if ff.returncode == 0:
        return
    logger.warning("git pull --ff-only failed (rc=%s), retrying via fetch+reset", ff.returncode)
    fetch = runner(["fetch", "origin"])
    if fetch.returncode != 0:
        raise RuntimeError(
            f"git fetch origin failed (rc={fetch.returncode}): "
            f"{(fetch.stderr or fetch.stdout or '').strip()}"
        )
    reset = runner(["reset", "--hard", f"origin/{branch}"])
    if reset.returncode != 0:
        raise RuntimeError(
            f"git reset --hard origin/{branch} failed (rc={reset.returncode}): "
            f"{(reset.stderr or reset.stdout or '').strip()}"
        )


CODEX_CONTEXT_LIMIT = 12_000


def _venv_metasphere_bin(paths: Paths) -> Path:
    """Path to the ``metasphere`` console script inside the venv.

    Used by :func:`_sync_hook_paths` to write absolute hook-command
    paths into ``~/.claude/settings.local.json``. Stable form
    regardless of the operator's PATH ordering.
    """
    return paths.root / _VENV_METASPHERE_BIN


def _settings_local_targets(paths: Paths, repo: Path | None,
                              home_dir: Path) -> list[Path]:
    """Return the candidate ``settings.local.json`` paths to keep in
    sync.

    Matches what ``install.sh`` writes — strictly project-scoped:
    - ``$METASPHERE_DIR/.claude/settings.local.json`` — the runtime
      file Claude Code reads at orchestrator session cwd.
    - ``<source repo>/.claude/settings.local.json`` — the dev-time
      file Claude Code reads when the operator runs ``claude`` from
      the source repo directly.

    Deliberately excludes ``~/.claude/settings.local.json`` (user
    scope). ``install.sh`` never writes there, so a rewrite would
    silently clobber any non-metasphere hooks the operator has
    configured at user level. Asymmetry between project-scoped tool
    and user-scoped Claude Code config is the correct invariant.
    The ``home_dir`` parameter is retained on the signature for
    callers' convenience but unused here.

    Returns only candidates that actually exist on disk; the
    rewrite is idempotent against missing files.
    """
    candidates: list[Path] = []
    candidates.append(paths.root / ".claude" / "settings.local.json")
    if repo is not None:
        candidates.append(repo / ".claude" / "settings.local.json")
    # Dedupe while preserving order — paths.root and repo can collide
    # when the source repo lives inside ``$METASPHERE_DIR``.
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        if p.is_file():
            out.append(p)
    return out


def _sync_hook_paths(paths: Paths, home_dir: Path,
                     repo: Path | None = None) -> int:
    """Rewrite ``UserPromptSubmit`` and ``Stop`` hook commands in
    every operator-side ``settings.local.json`` to the current
    venv-form.

    Closes the silent-failure mode where a relocated posthook/context
    script left the file pointing at a path that no longer existed
    (e.g. ``scripts/metasphere-posthook`` after the package-shim
    migration). Run on every update so path drift propagates.

    The set of files to rewrite mirrors what ``install.sh`` writes —
    typically ``$METASPHERE_DIR/.claude/`` (runtime) and the source
    repo's ``.claude/`` (dev-time).

    Behavior:
    - No-op for a settings.local.json that doesn't exist (stranger
      install that hasn't yet seeded hooks; install.sh handles that
      path).
    - Atomic write per-file (temp + rename) so a crash mid-write
      can't corrupt the file.
    - Idempotent: if the existing hook commands already match the
      target form, no rewrite for that file.
    - Preserves any other top-level keys (``permissions``, custom
      operator settings) — only the ``hooks`` block is replaced.

    Returns the number of files rewritten.
    """
    bin_path = str(_venv_metasphere_bin(paths))
    target_hooks = {
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": f"{bin_path} hooks context"}],
            }
        ],
        # PreToolUse must stay here in lockstep with install.sh: this dict
        # REPLACES the entire hooks block on every ``metasphere update``, so
        # if PreToolUse is missing here the next update silently deletes the
        # gateway interactive-prompt guard (same path-drift footgun that took
        # out Telegram auto-forward for ~22h).
        "PreToolUse": [
            {
                "matcher": "AskUserQuestion|ExitPlanMode",
                "hooks": [{"type": "command", "command": f"{bin_path} hooks pretool"}],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": f"{bin_path} hooks posthook"}],
            }
        ],
    }

    rewrote_count = 0
    for settings_path in _settings_local_targets(paths, repo, home_dir):
        if _rewrite_settings_hooks(settings_path, target_hooks):
            rewrote_count += 1
    return rewrote_count


def _rewrite_settings_hooks(settings_path: Path,
                              target_hooks: dict) -> bool:
    """Atomically rewrite a single settings.local.json's ``hooks``
    block. Returns ``True`` if a write happened, ``False`` on no-op
    (file missing, unparseable, or already correct)."""
    if not settings_path.is_file():
        return False
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "%s unparseable, skipping hook-path sync: %s",
            settings_path, e,
        )
        return False
    if not isinstance(existing, dict):
        logger.warning(
            "%s root must be an object, got %s — skipping",
            settings_path, type(existing).__name__,
        )
        return False
    if existing.get("hooks") == target_hooks:
        return False  # already correct
    existing["hooks"] = target_hooks
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        tmp.replace(settings_path)
    except OSError as e:
        logger.warning("%s rewrite failed: %s", settings_path, e)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _without_metasphere_codex_handlers(
    groups: object, managed_command: str
) -> list[object]:
    """Remove only our handlers, retaining custom handlers in their group."""
    try:
        managed_parts = shlex.split(managed_command)
        subcommand = managed_parts[-1]
    except (ValueError, IndexError):
        subcommand = ""

    def is_managed(command: object) -> bool:
        if not isinstance(command, str):
            return False
        if command == managed_command:
            return True
        try:
            parts = shlex.split(command)
        except ValueError:
            return False
        return (
            len(parts) == 3
            and Path(parts[0]).name == "metasphere"
            and parts[1:] == ["hooks", subcommand]
        )

    if not isinstance(groups, list):
        return []
    out: list[object] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            out.append(group)
            continue
        retained = [
            handler for handler in group["hooks"]
            if not (
                isinstance(handler, dict)
                and is_managed(handler.get("command"))
            )
        ]
        if retained:
            updated = dict(group)
            updated["hooks"] = retained
            out.append(updated)
    return out


def _sync_codex_hooks(paths: Paths, home_dir: Path,
                      repo: Path | None = None) -> int:
    """Install the Metasphere Codex hooks once at user scope.

    Codex composes hooks from all active layers, so legacy project/runtime
    copies would inject the same context more than once. This migration
    removes only matcher groups containing commands previously installed by
    Metasphere, preserves unrelated hooks and metadata, and writes the current
    context/Stop groups to ``~/.codex/hooks.json``.
    """
    bin_path = str(_venv_metasphere_bin(paths))
    context_command = shlex.join([bin_path, "hooks", "context"])
    posthook_command = shlex.join([bin_path, "hooks", "posthook"])
    managed = {
        "UserPromptSubmit": {
            "hooks": [{
                "type": "command",
                "command": context_command,
                "statusMessage": "Loading Metasphere context",
                "additionalContextLimit": CODEX_CONTEXT_LIMIT,
            }]
        },
        "Stop": {
            "hooks": [{
                "type": "command",
                "command": posthook_command,
                "statusMessage": "Forwarding response to Telegram",
            }]
        },
    }
    user_file = home_dir / ".codex" / "hooks.json"
    candidates = [user_file, paths.root / ".codex" / "hooks.json"]
    if repo is not None:
        candidates.append(repo / ".codex" / "hooks.json")

    changed = 0
    user_hook_ready = False
    seen: set[str] = set()
    for hook_file in candidates:
        key = str(hook_file)
        if key in seen:
            continue
        seen.add(key)
        if hook_file.is_file():
            try:
                doc = json.loads(hook_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("%s unparseable, skipping Codex hook sync: %s", hook_file, exc)
                if hook_file == user_file:
                    return 0
                continue
            if not isinstance(doc, dict):
                if hook_file == user_file:
                    return 0
                continue
        elif hook_file == user_file:
            doc = {}
        else:
            continue

        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        else:
            hooks = dict(hooks)
        for event, managed_command in (
            ("UserPromptSubmit", context_command),
            ("Stop", posthook_command),
        ):
            retained = _without_metasphere_codex_handlers(
                hooks.get(event), managed_command
            )
            if hook_file == user_file:
                retained.append(managed[event])
            if retained:
                hooks[event] = retained
            else:
                hooks.pop(event, None)
        doc["hooks"] = hooks

        rendered = json.dumps(doc, indent=2) + "\n"
        try:
            old = hook_file.read_text(encoding="utf-8") if hook_file.is_file() else None
            if old == rendered:
                if hook_file == user_file:
                    user_hook_ready = True
                continue
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = hook_file.with_suffix(".json.tmp")
            tmp.write_text(rendered, encoding="utf-8")
            tmp.replace(hook_file)
            changed += 1
            if hook_file == user_file:
                user_hook_ready = True
        except OSError as exc:
            logger.warning("%s Codex hook sync failed: %s", hook_file, exc)
            if hook_file == user_file:
                return 0
        if hook_file == user_file and not user_hook_ready:
            return 0
    return changed


def _sync_claude_integration(repo: Path, home_dir: Path,
                              paths: Paths | None = None) -> None:
    """Refresh ``~/.claude/{skills,commands}`` symlinks from the repo
    + rewrite hook paths in ``settings.local.json`` to the current
    venv form.

    * Each ``skills/<name>/`` containing ``SKILL.md`` is linked into
      ``<home>/.claude/skills/<name>``. A pre-existing real directory
      (not a symlink) with a ``.user-customized`` marker is left alone.
    * Each ``templates/claude-commands/*.md`` is linked into both
      ``<home>/.claude/commands/<basename>`` and
      ``<repo>/.claude/commands/<basename>``.
    * ``settings.local.json`` ``UserPromptSubmit`` + ``Stop`` hook
      commands are rewritten to the current venv form via
      :func:`_sync_hook_paths`.

    The hook-path step closes a silent-failure mode where a relocated
    script (e.g. the ``scripts/metasphere-posthook`` → package-shim
    migration) left the file pointing at a stale path; the operator's
    Claude Code invocations would then fail every Stop / UserPromptSubmit
    tick with no visible error to the supervising daemon.
    """
    paths = paths or resolve()
    # Skills
    src_skills = repo / "skills"
    if src_skills.is_dir():
        dst_skills = home_dir / ".claude" / "skills"
        dst_skills.mkdir(parents=True, exist_ok=True)
        for child in sorted(src_skills.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            name = child.name
            dst = dst_skills / name
            if (
                dst.exists()
                and not dst.is_symlink()
                and dst.is_dir()
                and (dst / ".user-customized").is_file()
            ):
                continue
            # Replace any existing symlink / file; leave customized real dirs alone.
            if dst.is_symlink() or dst.is_file():
                try:
                    dst.unlink()
                except OSError:
                    pass
            elif dst.exists() and dst.is_dir():
                # A real directory without .user-customized — leave it alone
                # rather than rm -rf (conservative; matches bash behaviour,
                # which used ln -sfn that fails silently against real dirs).
                continue
            try:
                os.symlink(child.resolve(), dst)
            except OSError as e:
                logger.info("skills symlink skipped for %s: %s", name, e)

    # Commands. Source moved from ``.claude/commands/`` (per-machine
    # runtime state, gitignored) to ``templates/claude-commands/``
    # (shipped). Symlinks land in both ``~/.claude/commands/`` (user-
    # level) and ``<repo>/.claude/commands/`` (project-level), mirroring
    # install.sh::install_skills.
    src_cmds = repo / "templates" / "claude-commands"
    if src_cmds.is_dir():
        dst_dirs = [
            home_dir / ".claude" / "commands",
            repo / ".claude" / "commands",
        ]
        for dst_cmds in dst_dirs:
            dst_cmds.mkdir(parents=True, exist_ok=True)
            for md in sorted(src_cmds.glob("*.md")):
                if not md.is_file():
                    continue
                dst = dst_cmds / md.name
                if dst.is_symlink() or dst.is_file():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                try:
                    os.symlink(md.resolve(), dst)
                except OSError as e:
                    logger.info(
                        "command symlink skipped for %s -> %s: %s",
                        md.name, dst, e,
                    )

    # Hook paths in settings.local.json. See _sync_hook_paths for
    # rationale (closes the silent-failure mode where a relocated
    # script left the hook command pointing at a stale path).
    # ``repo`` is passed so install.sh's source-repo .claude/ also
    # gets the rewrite if it exists.
    try:
        rewrote_count = _sync_hook_paths(paths, home_dir, repo=repo)
        if rewrote_count:
            logger.info(
                "settings.local.json hook paths rewritten to current "
                "venv form across %d file(s)", rewrote_count,
            )
    except Exception as e:
        logger.warning("hook-paths sync failed: %s", e)

    # Do not introduce a Codex user hook on Claude-only installations. The
    # installer handles fresh Codex systems; scheduled Codex updates carry
    # this same runtime marker and migrate legacy project-local copies.
    if os.environ.get("METASPHERE_AGENT_RUNTIME", "").strip().lower() == "codex":
        try:
            rewrote_count = _sync_codex_hooks(paths, home_dir, repo=repo)
            if rewrote_count:
                logger.info("Codex user hook paths synchronized across %d file(s)", rewrote_count)
        except Exception as e:
            logger.warning("Codex hook-path sync failed: %s", e)


def _restart_daemons() -> None:
    """Restart the metasphere gateway/daemon after an update.

    Defensive: if the platform's service manager or unit isn't present,
    this function logs and returns without raising.
    """
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.metasphere.plist"
        if not plist.is_file() or not shutil.which("launchctl"):
            logger.info("launchctl/plist not present; skipping daemon restart")
            return
        subprocess.run(["launchctl", "unload", str(plist)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "load", str(plist)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    # Linux (systemd --user)
    if not shutil.which("systemctl"):
        logger.info("systemctl not found; skipping daemon restart")
        return

    def _sc(*args: str) -> int:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode

    # Gateway owns polling now — stop+disable stale standalone pollers.
    for stale in ("metasphere-telegram.service", "metasphere-telegram-stream.service"):
        if _sc("is-enabled", stale) == 0:
            _sc("stop", stale)
            _sc("disable", stale)

    # Order matters: gateway LAST. Gateway supervises the tmux session
    # that this update is running inside; restarting it first kills the
    # caller before heartbeat+schedule get their turn (2026-04-21 incident
    # on commit ddf421e — deploy silently half-finished).
    for daemon in ("metasphere-heartbeat", "metasphere-schedule"):
        if _sc("is-active", daemon) == 0:
            _sc("restart", daemon)

    if _sc("is-active", "metasphere-gateway") == 0:
        _sc("restart", "metasphere-gateway")
    elif _sc("is-active", "metasphere") == 0:
        _sc("restart", "metasphere")


def notify(text: str, *, sender: Callable[[str], None] | None = None) -> None:
    """Send a notification line. Default sender uses telegram.api if a
    chat is configured; otherwise this is a no-op. Tests inject ``sender``.
    """
    if sender is not None:
        try:
            sender(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("notify sender failed: %s", e)
        return
    try:
        from .telegram.groups import send_to_topic  # type: ignore
        send_to_topic("orchestrator", text)
    except Exception as e:
        logger.info("auto-update notify suppressed: %s", e)


def _find_repo(paths: "Paths") -> Path:
    """Resolve the metasphere-agents repo root for git operations.

    ``paths.project_root`` is set from ``METASPHERE_PROJECT_ROOT`` which
    some agent panes point at ``~/.metasphere`` (the data dir, NOT a git
    repo). This caused ``git -C ~/.metasphere status`` → rc=128 →
    the dirty-check refused to update (2026-04-17 P0 update-broken bug).

    Resolution order:
    1. ``METASPHERE_REPO_ROOT`` env var (explicit override)
    2. Editable install: ``metasphere.__file__`` is inside the repo
    3. Fallback: ``paths.project_root`` (may be wrong but predictable)
    """
    env = os.environ.get("METASPHERE_REPO_ROOT")
    if env:
        p = Path(env)
        if p.is_dir() and (p / ".git").is_dir():
            return p
    try:
        import metasphere as _ms
        pkg_dir = Path(_ms.__file__).resolve().parent
        candidate = pkg_dir.parent
        if (candidate / ".git").is_dir():
            return candidate
    except Exception:
        pass
    return paths.project_root


def run_update(
    *,
    paths: Paths | None = None,
    cfg: AutoUpdateConfig | None = None,
    quiet: bool = False,
    git_runner: GitRunner | None = None,
    pip_runner: Callable[[list[str]], int] | None = None,
    test_runner: Callable[[], bool] | None = None,
    notify_sender: Callable[[str], None] | None = None,
) -> UpdateResult:
    """Run a one-shot auto-update.

    All side-effecting subprocesses are injectable so tests can monkeypatch
    them. The git pull, claude-skills/commands symlink sync, and daemon
    restart steps are handled by the module-level helpers
    :func:`_git_pull_or_reset`, :func:`_sync_claude_integration`, and
    :func:`_restart_daemons` (monkeypatch those directly in tests).
    """
    paths = paths or resolve()
    cfg = cfg or load_config(paths)
    repo = _find_repo(paths)
    runner = git_runner or _git(repo)

    def log(line: str) -> None:
        if quiet:
            _append_log(paths, line)
        else:
            print(line)
            _append_log(paths, line)

    log(f"auto-update: starting (branch={cfg.branch}, restart={cfg.restart_daemons})")

    old = _head_hash(repo, runner)
    log(f"auto-update: HEAD before: {old or '(unknown)'}")

    # 1. Git pull (with fetch+reset fallback).
    try:
        _git_pull_or_reset(repo, cfg.branch, runner)
    except Exception as e:
        reason = f"git pull failed: {e}"
        log(f"auto-update: FAILED — {reason}")
        result = UpdateResult(ok=False, old_hash=old, reason=reason)
        _record_result(paths, result, cfg, notify_sender)
        return result

    # 2. Refresh ~/.claude/{skills,commands} symlinks + rewrite the
    # hook commands in settings.local.json to the current venv form.
    try:
        _sync_claude_integration(repo, Path.home(), paths=paths)
    except Exception as e:
        # Non-fatal: log and continue. The update itself succeeded; a
        # skills-sync failure shouldn't block pip reinstall / daemon
        # restart.
        log(f"auto-update: claude integration sync warning: {e}")

    new = _head_hash(repo, runner)
    log(f"auto-update: HEAD after: {new or '(unknown)'}")

    subjects = _commit_subjects(repo, old, new, runner)
    pip_reinstalled = False

    # Ensure the dedicated venv at $METASPHERE_DIR/venv exists — even
    # if there are no python changes in this update cycle. A host
    # installed before venv-first (pre-e148ad1) has no venv; the first
    # `metasphere update` after the venv-first commit lands must
    # create one regardless of whether python_changes triggers a
    # subsequent reinstall. Otherwise the host sits in a state where
    # HEAD is current, python_changes=False, and the venv never
    # materializes (the 2026-04-16 stuck-state incident).
    #
    # _ensure_venv does a bootstrap `pip install -e .` during venv
    # creation; that counts as a reinstall for this cycle's purposes.
    venv_existed_before = _venv_python(paths).exists()
    try:
        venv_python = _ensure_venv(paths, repo, log)
    except RuntimeError as e:
        reason = f"venv bootstrap failed: {e}"
        log(f"auto-update: FAILED — {reason}")
        result = UpdateResult(
            ok=False, old_hash=old, new_hash=new, commits=len(subjects),
            subjects=subjects, reason=reason,
        )
        _record_result(paths, result, cfg, notify_sender)
        return result
    if not venv_existed_before:
        log("auto-update: new venv bootstrapped, initial pip install done")
        pip_reinstalled = True

    # Additional reinstall if git pulled actual python changes.
    if _has_python_changes(repo, old, new, runner):
        log("auto-update: python changes detected, re-installing package")
        pip_runner_for_venv = pip_runner or _venv_pip_runner(venv_python)
        rc = pip_runner_for_venv([
            "-m", "pip", "install", "-e", str(repo), "--quiet",
            "--no-warn-script-location",
        ])
        if rc != 0:
            reason = f"pip install -e exited rc={rc}"
            log(f"auto-update: FAILED — {reason}")
            result = UpdateResult(
                ok=False, old_hash=old, new_hash=new, commits=len(subjects),
                subjects=subjects, reason=reason,
            )
            _record_result(paths, result, cfg, notify_sender)
            return result
        pip_reinstalled = True

    tests_passed: Optional[bool] = None
    if test_runner is not None:
        log("auto-update: running test gate (pytest -m 'not live' -q)")
        try:
            tests_passed = bool(test_runner())
        except Exception as e:
            tests_passed = False
            log(f"auto-update: test runner raised: {e}")
        if not tests_passed:
            reason = "test gate failed"
            log(f"auto-update: FAILED — {reason}")
            result = UpdateResult(
                ok=False, old_hash=old, new_hash=new, commits=len(subjects),
                subjects=subjects, reason=reason,
                pip_reinstalled=pip_reinstalled, tests_passed=False,
            )
            _record_result(paths, result, cfg, notify_sender)
            return result

    # Template drift check (warn-only). Surfaces shipped templates that
    # differ from the operator's local copies under ~/.metasphere/.
    # Operator opts in via `metasphere update --templates`; auto-update
    # never overwrites without explicit consent.
    try:
        drifted = detect_drift(paths=paths, repo=repo)
        if drifted:
            log(
                f"auto-update: {len(drifted)} template(s) have drift. "
                f"Run 'metasphere update --templates' to opt in (with backups)."
            )
            for entry in drifted:
                log(f"  - {entry.label}: shipped {entry.src_lines}L vs local {entry.dest_lines}L")
    except Exception as e:
        log(f"auto-update: drift detection warning: {e}")

    result = UpdateResult(
        ok=True,
        old_hash=old,
        new_hash=new,
        commits=len(subjects),
        subjects=subjects,
        pip_reinstalled=pip_reinstalled,
        tests_passed=tests_passed,
        daemons_restarted=cfg.restart_daemons,
    )
    log(f"auto-update: ok ({len(subjects)} commits applied)")
    # Persist BEFORE _restart_daemons. The schedule daemon spawns the
    # cron-fired auto-update as a cgroup child; `systemctl restart
    # metasphere-schedule` inside _restart_daemons kills the caller
    # before any post-restart code runs. Same for tmux-pane-fired
    # updates: gateway-restart kills its own supervised tmux. Pre-fix,
    # state["last_run_at"] never advanced past the last externally-run
    # update — operator-facing `metasphere update --status` showed
    # stale info indefinitely (observed 2026-04-26 → 2026-05-01).
    _record_result(paths, result, cfg, notify_sender)

    # Daemon restart (skippable via cfg.restart_daemons). Runs AFTER
    # pip work and the test gate so daemons come back up against the
    # newly installed package, not the stale version that was running
    # when the update started. Pre-2026-04-30 the restart fired before
    # _ensure_venv + the python_changes pip reinstall, so daemons
    # would briefly run the OLD code while new commits sat unbuilt
    # in the venv. Hosts that fell behind on updates went silent
    # because their gateway/heartbeat daemons restarted into a stale
    # build and never picked up the new bytes.
    if cfg.restart_daemons:
        try:
            _restart_daemons()
        except Exception as e:
            log(f"auto-update: daemon restart warning: {e}")

    return result


def _default_pip_runner(args: list[str]) -> int:
    return subprocess.call([sys.executable, *args])


def _record_result(
    paths: Paths,
    result: UpdateResult,
    cfg: AutoUpdateConfig,
    sender: Callable[[str], None] | None,
) -> None:
    state = load_state(paths)
    state["last_run_at"] = int(time.time())
    state["last_result"] = result.to_dict()
    save_state(state, paths)

    if not cfg.notify:
        return
    host = os.uname().nodename
    if result.ok:
        if result.commits == 0:
            return  # nothing changed; don't ping
        head = (result.subjects[:3] + ["…"]) if len(result.subjects) > 3 else result.subjects
        msg = (
            f"auto-update: {host} now at {result.new_hash[:10]}, "
            f"was {result.old_hash[:10]}, {result.commits} commits applied: "
            + "; ".join(head)
        )
    else:
        msg = (
            f"auto-update FAILED on {host}: {result.reason}, "
            f"last good {result.old_hash[:10] or 'unknown'}, "
            f"daemons NOT restarted"
        )
    notify(msg, sender=sender)


# ---------- status ----------

def status_text(paths: Paths | None = None) -> str:
    paths = paths or resolve()
    cfg = load_config(paths)
    state = load_state(paths)
    lines = [
        "metasphere auto-update",
        f"  enabled:         {cfg.enabled}",
        f"  interval:        {cfg.interval}  ({cfg.cron_expr()})",
        f"  branch:          {cfg.branch}",
        f"  restart_daemons: {cfg.restart_daemons}",
        f"  notify:          {cfg.notify}",
        f"  config:          {config_path(paths)}",
        f"  log:             {log_path(paths)}",
    ]
    last = state.get("last_run_at")
    if last:
        lines.append(
            f"  last run:        {_dt.datetime.fromtimestamp(int(last)).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    last_result = state.get("last_result") or {}
    if last_result:
        lines.append(f"  last result:     ok={last_result.get('ok')} commits={last_result.get('commits', 0)}")
        if last_result.get("reason"):
            lines.append(f"  last reason:     {last_result['reason']}")
    # job presence
    try:
        jobs = _sched.load_jobs(paths)
        job = next((j for j in jobs if j.id == JOB_ID), None)
        if job:
            lines.append(f"  cron job:        {job.cron_expr} (enabled={job.enabled})")
            # The scheduler executes payload_message; command/full_command are
            # descriptive compatibility fields and may have drifted apart.
            command = job.payload_message or job.full_command or job.command
            lines.append(f"  job command:     {command or '(missing)'}")
            expected_command = build_job(cfg, paths).payload_message
            if command != expected_command:
                lines.append(f"  command drift:   expected {expected_command}")
        else:
            lines.append("  cron job:        (not registered)")
    except Exception as e:  # pragma: no cover - defensive
        lines.append(f"  cron job:        (error: {e})")
    return "\n".join(lines) + "\n"


# ---------- template drift detection ----------
#
# Shipped templates under ``<repo>/templates/`` get seeded into
# ``~/.metasphere/`` on first install. Operators may then customize
# their local copies. When the repo's templates evolve (new sections,
# fixes, etc.), the operator's copy "drifts" from shipped — we want to
# surface that without ever overwriting without explicit consent.
#
# Two surfaces exposed:
#   - :func:`detect_drift` — pure detection, returns list of entries.
#     Used by :func:`run_update` for warn-only aggregation.
#   - :func:`run_templates_interactive` — operator-facing prompt with
#     keep / overwrite / diff per drifted file. Wired to
#     ``metasphere update --templates``.


@dataclass
class DriftEntry:
    """One template-vs-local pair that differs by sha256."""
    src: Path           # shipped template path under <repo>/templates/
    dest: Path          # local copy under ~/.metasphere/
    label: str          # short human-readable name for log lines
    src_lines: int
    dest_lines: int


def _hash_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _agent_role(agent_dir: Path, paths: Paths) -> Optional[str]:
    """Resolve an agent's role for drift matching against templates/agents/<role>/.

    @orchestrator is hardcoded to "orchestrator" since install.sh seeds it
    directly without going through the spec system. Other agents are
    resolved from their ``spec`` file (written by ``specs.seed_agent``);
    if the file is missing or the spec can't be loaded, the agent is
    skipped silently (legacy hand-created agents).
    """
    if agent_dir.name == "@orchestrator":
        return "orchestrator"
    spec_file = agent_dir / "spec"
    if not spec_file.is_file():
        return None
    try:
        spec_name = spec_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not spec_name:
        return None
    try:
        from .specs import get_spec
        spec = get_spec(spec_name, paths=paths)
    except Exception:
        return None
    if spec is None:
        return None
    return spec.role


def _drift_pairs(paths: Paths, repo: Path) -> list[tuple[Path, Path, str]]:
    """Return the list of (src, dest, label) pairs to drift-check."""
    pairs: list[tuple[Path, Path, str]] = []

    # User manual at ~/.metasphere/CLAUDE.md
    src = repo / "templates" / "install" / "CLAUDE.md"
    if src.is_file():
        pairs.append((src, paths.root / "CLAUDE.md", "~/.metasphere/CLAUDE.md"))

    # Per-agent AGENTS.md, matched by spec.role -> templates/agents/<role>/
    agents_dir = paths.root / "agents"
    if agents_dir.is_dir():
        for agent_subdir in sorted(agents_dir.iterdir()):
            if not agent_subdir.is_dir() or not agent_subdir.name.startswith("@"):
                continue
            role = _agent_role(agent_subdir, paths)
            if role is None:
                continue
            template = repo / "templates" / "agents" / role / "AGENTS.md"
            if not template.is_file():
                continue
            local = agent_subdir / "AGENTS.md"
            if not local.is_file():
                continue
            pairs.append((template, local, f"{agent_subdir.name}/AGENTS.md"))

    return pairs


def detect_drift(paths: Paths | None = None, repo: Path | None = None) -> list[DriftEntry]:
    """Walk shipped templates against local files. Return drifted entries."""
    paths = paths or resolve()
    repo = repo or _find_repo(paths)
    drifted: list[DriftEntry] = []
    for src, dest, label in _drift_pairs(paths, repo):
        try:
            if _hash_file(src) == _hash_file(dest):
                continue
            src_lines = sum(1 for _ in src.read_text(encoding="utf-8").splitlines())
            dest_lines = sum(1 for _ in dest.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        drifted.append(DriftEntry(
            src=src, dest=dest, label=label,
            src_lines=src_lines, dest_lines=dest_lines,
        ))
    return drifted


def run_templates_interactive(
    paths: Paths | None = None,
    repo: Path | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    diff_runner: Callable[[Path, Path], None] | None = None,
) -> int:
    """Interactive ``metasphere update --templates`` flow.

    For each drifted template→local pair, prompt the operator:
      k → keep local (default), skip
      o → backup local to ``<dest>.bak-<unix>``, then copy shipped over
      d → run ``diff -u`` paged, then re-prompt

    Always preserves local without explicit ``o`` consent. Returns 0 on
    success (regardless of whether anything was overwritten); non-zero
    only if drift detection itself errored.

    ``input_fn`` and ``diff_runner`` are injectable for tests.
    """
    paths = paths or resolve()
    repo = repo or _find_repo(paths)
    drifted = detect_drift(paths=paths, repo=repo)
    if not drifted:
        print("No template drift detected.")
        return 0

    print(f"{len(drifted)} template(s) have drift:")
    for entry in drifted:
        print(f"  - {entry.label}: shipped {entry.src_lines}L vs local {entry.dest_lines}L")
    print()

    def _default_diff(local: Path, shipped: Path) -> None:
        # Run diff -u; pager if available, otherwise direct stdout.
        pager = os.environ.get("PAGER", "less -R")
        try:
            diff_proc = subprocess.Popen(
                ["diff", "-u", str(local), str(shipped)],
                stdout=subprocess.PIPE,
            )
            pager_argv = pager.split()
            pager_proc = subprocess.Popen(pager_argv, stdin=diff_proc.stdout)
            diff_proc.stdout.close()  # type: ignore[union-attr]
            pager_proc.communicate()
        except (OSError, subprocess.SubprocessError):
            # Fallback: print diff direct.
            subprocess.call(["diff", "-u", str(local), str(shipped)])

    diff_runner = diff_runner or _default_diff

    for entry in drifted:
        while True:
            print(f"DRIFT: {entry.label}")
            print(f"  shipped:  {entry.src} ({entry.src_lines} lines)")
            print(f"  local:    {entry.dest} ({entry.dest_lines} lines)")
            try:
                choice = input_fn(
                    "  shipped template differs. (k)eep mine [default], (o)verwrite, (d)iff? "
                ).strip().lower()
            except EOFError:
                choice = "k"
            if not choice:
                choice = "k"
            if choice == "k":
                print(f"  kept local {entry.label}")
                print()
                break
            if choice == "o":
                ts = int(time.time())
                backup = entry.dest.with_name(entry.dest.name + f".bak-{ts}")
                shutil.copy2(entry.dest, backup)
                shutil.copy2(entry.src, entry.dest)
                print(f"  overwrote {entry.label} (backup at {backup})")
                print()
                break
            if choice == "d":
                diff_runner(entry.dest, entry.src)
                continue
            print("  invalid choice; expected k, o, or d")
    return 0
