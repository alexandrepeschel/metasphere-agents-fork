"""Cron-style job scheduler.

Uses the on-disk schema in ``$METASPHERE_DIR/schedule/jobs.json``.

* **No ``eval``.** Dispatch never shells out to a string; the only
  dispatch path is :func:`dispatch_to_agent`, which uses ``subprocess.run``
  with an explicit argv.
* **File locking on every read-modify-write.** ``load_jobs`` /
  ``save_jobs`` go through :func:`metasphere.io.file_lock` +
  :func:`metasphere.io.write_json` (atomic tmp+rename + flock).
* **Shrink-detection guard.** ``save_jobs`` refuses to write zero jobs
  when the input had jobs — protects against the subshell-pipe wipe bug
  that previously truncated ``jobs.json`` to ``[]``.
* **180s fire window.** Protects against tick drift, restarts,
  briefly-paused daemons.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    from croniter import croniter
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "metasphere.schedule requires the 'croniter' package."
    ) from e

from . import agents as _agents
from .events import log_event
from .io import atomic_write_text, file_lock
from .messages import send_message
from .paths import Paths, resolve

logger = logging.getLogger(__name__)


# ---------- schema ----------

@dataclass
class Job:
    """A scheduled job. Mirrors jobs.json field-for-field."""

    id: str
    source: str = ""
    source_id: str = ""
    agent_id: str = "main"
    name: str = ""
    enabled: bool = True
    kind: str = "cron"
    cron_expr: str = ""
    tz: str = "UTC"
    payload_kind: str = "agentTurn"
    payload_message: str = ""
    model: str = ""
    session_target: str = "isolated"
    wake_mode: str = "next-heartbeat"
    imported_at: int = 0
    last_fired_at: int = 0
    next_run: int = 0
    command: str = ""
    full_command: str = ""
    # When True, ``metasphere schedule wire-exit-self`` appends the
    # session-cleanup stanza to this job's payload_message so the
    # target agent calls ``metasphere session exit-self`` at the end
    # of its turn. Set per-job rather than via a global allow-list so
    # operators opt jobs in/out without editing library code.
    wants_exit_self_cleanup: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class FireResult:
    job_id: str
    name: str
    target_agent: str
    fired: bool
    dispatched: bool
    error: str = ""


# ---------- load / save ----------

def _read_jobs_unlocked(jobs_path: Path) -> list[Job]:
    if not jobs_path.exists():
        return []
    try:
        raw = json.loads(jobs_path.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [Job.from_dict(j) for j in raw if isinstance(j, dict)]


def _write_jobs_unlocked(jobs_path: Path, jobs: list[Job], *, input_count: int) -> None:
    """Write jobs.json without acquiring a lock. Caller must hold one.

    Honors the shrink-detection guard.
    """
    if input_count > 0 and len(jobs) == 0:
        raise RuntimeError(
            f"refusing to wipe jobs.json: input had {input_count} jobs, output has 0"
        )
    payload = json.dumps([j.to_dict() for j in jobs], indent=2, sort_keys=True) + "\n"
    atomic_write_text(jobs_path, payload)


def load_jobs(paths: Paths | None = None) -> list[Job]:
    """Snapshot read of jobs.json under a shared lock."""
    paths = paths or resolve()
    jobs_path = paths.schedule_jobs
    with file_lock(jobs_path, exclusive=False):
        return _read_jobs_unlocked(jobs_path)


@contextmanager
def with_locked_jobs(paths: Paths | None = None) -> Iterator[list[Job]]:
    """Hold a single exclusive lock for the entire load→mutate→save cycle.

    Yields the current jobs list. Callers commit by calling
    :func:`save_jobs` *inside* the block — that path skips relocking and
    uses the surrounding lock as the only critical section.
    """
    paths = paths or resolve()
    jobs_path = paths.schedule_jobs
    with file_lock(jobs_path):
        yield _read_jobs_unlocked(jobs_path)


def save_jobs(jobs: list[Job], paths: Paths | None = None, *, _input_count: int) -> None:
    """Write jobs.json. Refuses to wipe if ``_input_count`` > 0 and ``jobs`` is empty.

    ``_input_count`` is mandatory: callers compute it under the lock they
    hold around the load, eliminating the TOCTOU window.
    Must be called from within a :func:`with_locked_jobs` block (or the
    caller must otherwise hold the schedule_jobs flock).
    """
    paths = paths or resolve()
    _write_jobs_unlocked(paths.schedule_jobs, jobs, input_count=_input_count)


# ---------- cron evaluation ----------

# 180s window. Wide enough to survive a missed
# tick from a restart/pause, narrow enough to not double-fire on the next
# minute.
CRON_WINDOW_SECS = 180


def cron_should_fire(
    expr: str,
    tz: str,
    last_fired_at: int,
    now: int | None = None,
) -> bool:
    """Return True if the cron expression is due to fire right now.

    Uses croniter for cron parsing. Honors timezone via
    zoneinfo so weekday/hour calculations are local-time correct.

    Fires when the most recent expected fire time is within the last
    ``CRON_WINDOW_SECS`` seconds AND we have not already fired since then
    (``prev_epoch > last_fired_at``).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - py < 3.9
        ZoneInfo = None  # type: ignore

    if not expr:
        return False

    now = int(now if now is not None else time.time())

    try:
        zone = ZoneInfo(tz) if ZoneInfo else None
    except Exception:
        zone = ZoneInfo("UTC") if ZoneInfo else None

    now_dt = _dt.datetime.fromtimestamp(now, tz=zone) if zone else _dt.datetime.fromtimestamp(now)
    try:
        itr = croniter(expr, now_dt)
        prev = itr.get_prev(_dt.datetime)
    except Exception:
        return False

    prev_epoch = int(prev.timestamp())
    delta = now - prev_epoch
    return 0 <= delta < CRON_WINDOW_SECS and prev_epoch > int(last_fired_at or 0)


# ---------- target resolution ----------

def resolve_target_agent(job: Job) -> str:
    """Map a job to its persistent collaborator agent via ``agent_id``.

    The job's ``agent_id`` field is the single source of truth for
    routing — whatever name is stored there (sans the leading ``@``)
    becomes the target. Default ``"main"`` resolves to ``@main``.

    Pre-2026-04-30 versions of this function had hardcoded
    prefix-match branches that overrode ``agent_id`` for specific
    job-name prefixes; those are removed and live jobs.json files
    were migrated to carry the resolved ``agent_id`` directly.
    """
    raw = (job.agent_id or "main").strip() or "main"
    return raw if raw.startswith("@") else "@" + raw


# ---------- dispatch ----------

def _find_mission(target_agent: str, paths: Paths) -> Path | None:
    """Return the ``MISSION.md`` path for ``target_agent`` if it names a
    persistent agent (global or project-scoped), else None.

    Mirrors :func:`metasphere.agents._find_agent_dir` precedence —
    project-scoped dirs first, then global. We only need the existence
    check, not a full :class:`AgentRecord`.
    """
    if paths.projects.is_dir():
        try:
            for proj_dir in sorted(paths.projects.iterdir()):
                if not proj_dir.is_dir():
                    continue
                mission = proj_dir / "agents" / target_agent / "MISSION.md"
                if mission.is_file():
                    return mission
        except OSError:
            pass
    mission = paths.agent_dir(target_agent) / "MISSION.md"
    if mission.is_file():
        return mission
    return None


def _isolated_agent_name(job_name: str) -> str:
    """Return a readable, collision-resistant name for one cron firing."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", job_name or "cron").strip("-").lower()
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%m%d-%H%M%S")
    return f"@{slug or 'cron'}-{stamp}-{uuid.uuid4().hex[:8]}"


def _wake_target(
    target_agent: str,
    first_task: str | None,
    paths: Paths,
    *,
    model: str = "",
    session_target: str = "persistent",
    job_name: str = "",
) -> bool:
    """Wake ``target_agent`` via :func:`metasphere.agents.wake_persistent`.

    Idempotent: if the tmux session is already alive, the helper just
    injects ``first_task`` (if any) and returns. Returns True when the
    wake succeeded AND ``first_task`` (if any) actually landed on the
    pane; False on exception or on silent pane-submit failure. Callers
    fall back to inbox-only delivery so the at-most-once stamp doesn't
    swallow the task (issue #106).
    """
    try:
        if target_agent == "@orchestrator":
            from .gateway.session import SESSION_NAME, start_session

            if session_target == "isolated" and first_task:
                try:
                    record = _agents.spawn_ephemeral(
                        _isolated_agent_name(job_name),
                        str(paths.project_root),
                        first_task,
                        parent="@scheduler",
                        paths=paths,
                        model=model,
                    )
                    if record.pid_file is not None:
                        return True
                    logger.warning(
                        "isolated spawn created a harness but launched no "
                        "process for %s; falling back to orchestrator injection",
                        job_name or target_agent,
                    )
                except Exception as exc:
                    logger.warning(
                        "isolated spawn failed for %s (model=%s); falling "
                        "back to orchestrator injection: %s",
                        job_name or target_agent,
                        model or "default",
                        exc,
                    )

            if not start_session(paths):
                return False
            if first_task is None:
                return True
            if model:
                logger.warning(
                    "job %s configured model=%s but is being injected into "
                    "the persistent orchestrator pane; the setting has no effect",
                    job_name or target_agent,
                    model,
                )
            banner = _agents._prepare_wake_banner(
                target_agent, first_task, paths
            )
            return _agents._submit_via_tmux(SESSION_NAME, banner)

        _, delivered = _agents.wake_persistent(
            target_agent, first_task=first_task, paths=paths,
            model=model,
        )
        if not delivered:
            logger.warning(
                "wake_persistent reached %s but inject silently failed; "
                "falling through to inbox delivery", target_agent,
            )
        return delivered
    except Exception as e:
        logger.warning("wake_persistent failed for %s: %s", target_agent, e)
        return False


def _extract_messages_send_target(payload: str) -> str | None:
    """Parse ``payload`` as a message-send command and return ``@X`` if it
    matches, else None.

    Recognizes three argv shapes so a ``command``-kind cron entry triggers
    pre-wake regardless of which surface the job-author used:

    * ``messages send @X !label …`` — legacy bare console-script.
    * ``msg send @X !label …``      — canonical bare console-script
      (deprecated shim, but still on disk for a release or two).
    * ``metasphere msg send @X …``  — canonical unified-CLI form,
      bare or full-path. This is what `metasphere schedule add` now
      writes by default; without recognizing it, a scheduled job
      targeting a dormant persistent agent would silently fail to
      pre-wake and the inbox notice would never reach a live REPL.
    """
    import shlex

    try:
        argv = shlex.split(payload or "")
    except ValueError:
        return None
    if len(argv) < 4:
        return None
    for i in range(len(argv) - 3):
        base = Path(argv[i]).name
        # `metasphere msg send @X …`
        if (
            base == "metasphere"
            and i + 4 <= len(argv)
            and argv[i + 1] == "msg"
            and argv[i + 2] == "send"
        ):
            tgt = argv[i + 3]
            return tgt if tgt.startswith("@") else None
        # `messages send @X …` or `msg send @X …`
        if base not in ("messages", "msg"):
            continue
        if argv[i + 1] != "send":
            continue
        tgt = argv[i + 2]
        return tgt if tgt.startswith("@") else None
    return None


def dispatch_command(
    payload: str,
    *,
    paths: Paths | None = None,
    timeout: int = 600,
) -> bool:
    """Execute a ``payload_kind=="command"`` job.

    Splits ``payload`` with :func:`shlex.split` (no shell, no eval) and
    runs the resulting argv via :func:`subprocess.run`. Returns True on
    exit-code 0.

    Pre-wake: if ``payload`` is a ``messages send @X !task ...`` command
    and ``@X`` is a persistent agent (has ``MISSION.md`` under global or
    project agents), we first wake ``@X``'s tmux+REPL via
    :func:`metasphere.agents.wake_persistent`. Without this, the
    subsequent ``messages send`` writes the inbox file but
    ``wake_recipient_if_live`` silently no-ops on a dormant session, so
    scheduled tasks targeting dormant agents accumulate unread forever.
    """
    import shlex

    if not payload:
        return False
    try:
        argv = shlex.split(payload)
    except ValueError as e:
        logger.warning("dispatch_command: bad payload %r: %s", payload, e)
        return False
    if not argv:
        return False

    paths = paths or resolve()
    target = _extract_messages_send_target(payload)
    if target is not None and (
        target == "@orchestrator" or _find_mission(target, paths) is not None
    ):
        _wake_target(target, first_task=None, paths=paths)

    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            # Stderr is the conventional diagnostic surface, but some
            # failures write to stdout instead (or to nowhere at all);
            # surface whichever is non-empty so silent-fail entries
            # like "exited 1: " stop showing up in schedule.log.
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            detail = stderr or stdout or "(no output)"
            logger.warning(
                "dispatch_command: %s exited %d: %s",
                shlex.join(argv), proc.returncode, detail[:200],
            )
        return proc.returncode == 0
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("dispatch_command failed for %r: %s", argv, e)
        return False


def dispatch_to_agent(
    target_agent: str,
    payload: str,
    *,
    paths: Paths | None = None,
    job_name: str = "",
    model: str = "",
    session_target: str = "persistent",
) -> bool:
    """Wake the target agent or fall back to a ``!task`` message.

    If the agent has a ``MISSION.md`` (global **or** project-scoped) we
    treat it as a persistent collaborator and call
    :func:`metasphere.agents.wake_persistent` — this starts the tmux+REPL
    session if dormant and injects ``payload`` as a first task. Otherwise
    we drop a ``!task`` message into its inbox via
    :func:`metasphere.messages.send_message`.

    If ``model`` is set, it is passed through to the configured runtime.
    """
    paths = paths or resolve()

    if (
        target_agent == "@orchestrator"
        or _find_mission(target_agent, paths) is not None
    ):
        if _wake_target(
            target_agent,
            first_task=payload,
            paths=paths,
            model=model,
            session_target=session_target,
            job_name=job_name,
        ):
            return True
        # Fall through to inbox-only delivery if wake itself failed.

    try:
        send_message(
            target_agent,
            "!task",
            payload or job_name or "scheduled task",
            from_agent="@scheduler",
            paths=paths,
            wake=False,
        )
        return True
    except Exception as e:
        logger.warning("send_message fallback failed for %s: %s", target_agent, e)
        return False


# ---------- run ----------

def run_due_jobs(paths: Paths | None = None, *, now: int | None = None) -> list[FireResult]:
    """Iterate jobs, fire those that are due, persist last_fired_at.

    Two-phase: (1) under an exclusive lock, stamp ``last_fired_at = now``
    on every due job and persist the file; (2) release the lock and
    dispatch each stamped job. This is at-most-once: if a dispatch
    (e.g. ``metasphere update``) restarts the schedule daemon mid-fire,
    the stamp is already on disk so the next tick won't re-fire the same
    job within its 180s cron window. Without this split, a self-restarting
    job storms its window — the 04:01-04:03Z 2026-04-27 incident where
    auto-update fired every ~15s.
    """
    paths = paths or resolve()
    now = int(now if now is not None else time.time())

    due: list[tuple[Job, str]] = []
    with with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        for job in jobs:
            if not job.enabled or job.kind != "cron":
                continue
            if not cron_should_fire(job.cron_expr, job.tz, job.last_fired_at, now=now):
                continue
            job.last_fired_at = now
            due.append((job, resolve_target_agent(job)))
        if due:
            save_jobs(jobs, paths, _input_count=input_count)

    results: list[FireResult] = []
    for job, target in due:
        try:
            log_event(
                "schedule.cron_fire",
                job.name or job.id,
                agent=target,
                meta={"job_id": job.id, "cron_expr": job.cron_expr, "tz": job.tz},
                paths=paths,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("log_event failed: %s", e)

        if job.payload_kind == "command":
            ok = dispatch_command(job.payload_message, paths=paths)
        else:
            ok = dispatch_to_agent(
                target,
                job.payload_message,
                paths=paths,
                job_name=job.name,
                model=job.model or "",
                session_target=job.session_target or "persistent",
            )
        results.append(
            FireResult(
                job_id=job.id,
                name=job.name,
                target_agent=target,
                fired=True,
                dispatched=ok,
                error="" if ok else "dispatch failed",
            )
        )

    return results


# ---------- list / enable / disable ----------

def list_jobs(paths: Paths | None = None) -> list[Job]:
    return load_jobs(paths)


def set_enabled(job_ref: str, enabled: bool, paths: Paths | None = None) -> bool:
    """Enable/disable a job by ``id`` or by ``name``.

    ``metasphere schedule list`` displays the ``name`` field (e.g.
    ``metasphere:auto-update``) but jobs are keyed in jobs.json by ``id``
    (slug-form, e.g. ``metasphere-auto-update``). Accepting both forms
    avoids the CLI inconsistency where users copy-paste the displayed
    name and get ``job not found``. ``id`` wins on collision.
    """
    paths = paths or resolve()
    with with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        target = next((j for j in jobs if j.id == job_ref), None)
        if target is None:
            target = next((j for j in jobs if j.name == job_ref), None)
        if target is None:
            return False
        target.enabled = enabled
        save_jobs(jobs, paths, _input_count=input_count)
    return True


def _validate_cron(expr: str, tz: str) -> None:
    """Raise ``ValueError`` when a cron expression or timezone is invalid."""
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(tz)
    except Exception as exc:
        raise ValueError(f"invalid timezone: {tz}") from exc
    try:
        croniter(expr, _dt.datetime.now(tz=zone)).get_next(_dt.datetime)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {expr}") from exc


def upsert_agent_job(
    job_id: str,
    *,
    agent: str,
    cron_expr: str,
    message: str,
    tz: str = "UTC",
    name: str = "",
    model: str = "",
    enabled: bool = True,
    paths: Paths | None = None,
) -> Job:
    """Create or update a cron job that wakes an agent with ``message``."""
    paths = paths or resolve()
    job_id = job_id.strip()
    if not job_id or any(ch.isspace() for ch in job_id):
        raise ValueError("job id must be non-empty and contain no whitespace")
    agent_id = agent.strip().lstrip("@")
    if not agent_id:
        raise ValueError("agent must be non-empty")
    if not message.strip():
        raise ValueError("message must be non-empty")
    _validate_cron(cron_expr, tz)

    with with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        job = next((candidate for candidate in jobs if candidate.id == job_id), None)
        if job is None:
            job = Job(id=job_id, imported_at=int(time.time()))
            jobs.append(job)
        job.source = "metasphere-cli"
        job.source_id = job_id
        job.agent_id = agent_id
        job.name = name.strip() or job_id
        job.enabled = enabled
        job.kind = "cron"
        job.cron_expr = cron_expr
        job.tz = tz
        job.payload_kind = "agentTurn"
        job.payload_message = message
        job.model = model
        job.session_target = "persistent"
        job.wake_mode = "scheduled"
        save_jobs(jobs, paths, _input_count=input_count)
    return job


def remove_job(job_ref: str, paths: Paths | None = None) -> bool:
    """Remove a job by id or name."""
    paths = paths or resolve()
    with with_locked_jobs(paths) as jobs:
        input_count = len(jobs)
        target = next((job for job in jobs if job.id == job_ref), None)
        if target is None:
            target = next((job for job in jobs if job.name == job_ref), None)
        if target is None:
            return False
        jobs.remove(target)
        _write_jobs_unlocked(
            paths.schedule_jobs,
            jobs,
            input_count=0 if not jobs else input_count,
        )
    return True


def fire_job(job_ref: str, paths: Paths | None = None) -> FireResult | None:
    """Dispatch one configured job immediately without shifting its cron."""
    paths = paths or resolve()
    jobs = load_jobs(paths)
    job = next((candidate for candidate in jobs if candidate.id == job_ref), None)
    if job is None:
        job = next((candidate for candidate in jobs if candidate.name == job_ref), None)
    if job is None:
        return None
    target = resolve_target_agent(job)
    try:
        log_event(
            "schedule.manual_fire",
            job.name or job.id,
            agent=target,
            meta={"job_id": job.id},
            paths=paths,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("log_event failed: %s", exc)
    if job.payload_kind == "command":
        dispatched = dispatch_command(job.payload_message, paths=paths)
    else:
        dispatched = dispatch_to_agent(
            target,
            job.payload_message,
            paths=paths,
            job_name=job.name,
            model=job.model,
            session_target=job.session_target or "persistent",
        )
    return FireResult(
        job_id=job.id,
        name=job.name,
        target_agent=target,
        fired=True,
        dispatched=dispatched,
        error="" if dispatched else "dispatch failed",
    )
