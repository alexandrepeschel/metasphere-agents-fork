"""Per-turn context block builder.

Section order is load-bearing: the orchestrator's per-turn cognition
keys off the layout (status header → drift warning → telegram →
messages → tasks → events → FTS).

Pure stdlib. No third-party deps.
"""

from __future__ import annotations

import collections as _collections
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import messages as _msgs
from . import tasks as _tasks
from .identity import resolve_agent_id
from .paths import Paths, rel_path, resolve

# Files baked into the REPL at session start. Order is irrelevant —
# sorted before concatenating for deterministic hashing.
_HARNESS_FILES = (
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/settings.local.json",
)

DEFAULT_SECTION_BUDGET = 2048


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def truncate_section(text: str, budget: int = DEFAULT_SECTION_BUDGET) -> str:
    """Cap a section to ``budget`` bytes, appending a truncation note."""
    if budget <= 0 or text is None:
        return ""
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    cut = data[:budget].decode("utf-8", errors="ignore").rstrip()
    return cut + f"\n_(truncated: {len(data)} bytes total)_\n"


# ---------------------------------------------------------------------------
# Harness drift detector
# ---------------------------------------------------------------------------


def _existing_harness_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for rel in _HARNESS_FILES:
        p = base / rel
        if p.is_file():
            out.append(p)
    return out


def harness_hash(paths: Paths) -> str:
    """Sha256 of the harness files the claude REPL actually baked in.

    Reads from ``paths.root`` (= ``~/.metasphere``) — the dir whose
    ``CLAUDE.md`` / ``.claude/settings*.json`` the claude CLI auto-
    loads when it starts a session. Previously used
    ``paths.project_root``, which diverged between the baseline writer
    (gateway daemon with ``METASPHERE_REPO_ROOT`` set to the source
    repo) and the reader (REPL whose CWD resolves to ``~/.metasphere``
    via ``git rev-parse`` fallback). That divergence produced a drift
    banner that could never clear — baseline always mismatched live.
    Rooting both to ``paths.root`` eliminates the env-hygiene class of
    bug entirely.

    Returns "" if no files exist.
    """
    files = _existing_harness_files(paths.root)
    if not files:
        return ""
    files_sorted = sorted(str(p) for p in files)
    h = hashlib.sha256()
    for fp in files_sorted:
        try:
            with open(fp, "rb") as f:
                h.update(f.read())
        except OSError:
            continue
    return h.hexdigest()


def _baseline_hash(paths: Paths) -> str:
    p = paths.state / "harness_hash_baseline"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


_STATUS_ICON = {
    _msgs.STATUS_UNREAD: "○",
    _msgs.STATUS_READ: "◐",
    _msgs.STATUS_REPLIED: "◑",
    _msgs.STATUS_COMPLETED: "●",
}


def _render_status_header(paths: Paths, agent: str) -> str:
    status = "unknown"
    sf = _agent_file(paths, agent, "status")
    if sf.is_file():
        try:
            status = sf.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            pass
    out = [f"# Metasphere Delta ({agent})", "", f"_Status: {status}_", ""]
    return "\n".join(out)


def _read_persona_body(path: Path) -> str:
    """Read a persona file; strip the leading H1 line; return the
    body. Returns ``""`` on missing file, OSError, or empty body
    after H1 stripping."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _agent_dirs(paths: Paths, agent: str) -> list[Path]:
    """Return only the active-project and global directories for ``agent``."""
    # Resolve only against the active project.  Agent ids are intentionally
    # reusable across projects; scanning every project can inject another
    # project's persona, task, or status into this turn.
    from .project import load_project
    project = load_project(paths.project_root, paths=paths)
    primary = None
    if project is not None:
        scoped = paths.project_agent_dir(project.name, agent)
        if scoped.is_dir():
            primary = scoped
    candidates = [primary] if primary is not None else []
    global_dir = paths.agent_dir(agent)
    if global_dir not in candidates:
        candidates.append(global_dir)
    return candidates


def _agent_file(paths: Paths, agent: str, *names: str) -> Path:
    """Resolve one agent file with migration-safe per-file precedence.

    The active project is authoritative when it contains the requested file.
    A sparse duplicate no longer hides a richer global identity: each missing
    file falls back independently to the global agent directory. Unrelated
    projects are never searched.
    """
    candidates = _agent_dirs(paths, agent)
    for directory in candidates:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    # Preserve a deterministic write/debug pointer when no file exists.
    return candidates[0] / names[0]


def _persona_sections(paths: Paths, agent: str) -> list[str]:
    """Render SOUL, IDENTITY, and USER as independently budgetable blocks."""
    soul_path = _agent_file(paths, agent, "SOUL.md", "VOICE.md")
    identity_path = _agent_file(paths, agent, "IDENTITY.md")
    user_path = _agent_file(paths, agent, "USER.md")
    soul_body = _read_persona_body(soul_path)
    identity_body = _read_persona_body(identity_path)
    user_body = _read_persona_body(user_path)

    sections: list[str] = []
    # Stable facts lead the hook output so Codex's head-and-tail preview still
    # carries them if a future context expansion ever crosses its spill limit.
    if user_body:
        sections.append("## User-model (who you collaborate with)\n\n" + user_body)
    if identity_body:
        sections.append("## Identity\n\n" + identity_body)
    if soul_body:
        sections.append("## Voice (who you are, how you sound)\n\n" + soul_body)
    if sections:
        roots = sorted({str(p.parent) for p in (soul_path, identity_path, user_path) if p.is_file()})
        sections.append(
            "_(Persona files at `" + "`, `".join(roots)
            + "` + persona-index.md for lazy-loadables.)_"
        )
    return sections


def _render_voice_capsule(paths: Paths, agent: str) -> str:
    """Inject the agent's full persona — SOUL / IDENTITY / USER — into
    every turn's context.

    No truncation: persona files are small and load-bearing. Pre-PR-B
    behaviour capped the capsule at 1500B / 40 lines and only loaded
    SOUL/VOICE, never IDENTITY or USER — so the kaomoji, warmth-marker,
    calm-intensity / thinking-companion lines, and full user-model sat
    on disk and never reached the model. Persona drift over time was
    the symptom.

    Each section is emitted iff its file exists. ``VOICE.md`` is a
    backward-compat alias for ``SOUL.md`` (older agents still have
    the file under the old name). The trailing pointer line is only
    emitted when at least one persona file landed.
    """
    sections = _persona_sections(paths, agent)
    if not sections:
        return ""
    return "\n\n".join(sections) + "\n"


_MISSION_BYTE_CAP = 1024
_MISSION_LINE_CAP = 30

# Per-file 2KB cap: LEARNINGS/MEMORY are the on-disk knowledge base
# and denser than mission. Section-level enforcement is delegated to
# the outer ``truncate_section`` call in ``build_context`` (default
# 2KB budget), which is the load-bearing cap — no per-section knob
# needed here.
_PROJECT_FILE_BYTE_CAP = 2048

# Reserve at the tail of a per-file render for the always-on
# file-path-pointer footer. Sized to fit:
#   "_(N more entries omitted by recency. Full file: "
#   + path string up to ~120B  + " — Read or grep for older.)_"
# Real paths in production land around 60–80B
# (``/home/<user>/.metasphere/projects/<proj>/LEARNINGS.md``), so 200B
# is comfortable headroom without crowding actual content.
_PROJECT_FOOTER_RESERVE = 200

# Header-anchored ISO date pattern (YYYY-MM-DD). The migration
# ephemerals + manual entry-write discipline both prefix entries with
# an ISO date; we sort by this when present.
_PROJECT_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _render_mission_capsule(paths: Paths, agent: str) -> str:
    """Inject the agent's MISSION.md so persistent agents know their
    purpose every turn. Capped to ~1KB / 30 lines."""
    mission_file = _agent_file(paths, agent, "MISSION.md")
    if not mission_file.is_file():
        return ""
    try:
        lines = mission_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    body_lines = lines[1 : _MISSION_LINE_CAP + 1]
    body = "\n".join(body_lines)
    data = body.encode("utf-8")[:_MISSION_BYTE_CAP]
    body = data.decode("utf-8", errors="ignore").rstrip()
    if not body:
        return ""
    return f"## Mission\n\n{body}\n"


def _infer_project_for_agent(
    paths: Paths, agent: str, agent_dir: Path
) -> str | None:
    """Path-based project inference for project-nested agents.

    Resolution: if the agent's home directory sits at
    ``~/.metasphere/projects/<P>/agents/@<id>/``, return ``<P>``.
    Returns ``None`` otherwise.

    This is the LAST-RESORT fallback in the resolution chain — see
    :func:`_render_project_capsule`. Frontmatter wins; then
    teams.yaml; then this; then no-op. The B4 name-prefix string-
    match branch was removed in B7: ``@widget-eng`` → ``widget``
    via dash-split was brittle in the general case (e.g.
    ``@polymarket-agents-research`` first-dash-split to
    ``polymarket`` which doesn't match project ``polymarket-agents``).
    teams.yaml is the canonical replacement."""
    try:
        if (
            agent_dir.parent.name == "agents"
            and agent_dir.parent.parent.parent == paths.projects
        ):
            project = agent_dir.parent.parent.name
            if (paths.projects / project).is_dir():
                return project
    except Exception:
        pass

    return None


def _parse_markdown_entries(text: str) -> list[tuple[str, str]]:
    """Walk markdown text, yield ``(header_line, body)`` tuples at
    ``##`` or ``###`` boundaries.

    The migration ephemerals that wrote per-project LEARNINGS/MEMORY
    files used those headers as entry boundaries; we use the same
    boundaries on read so each entry is an atomic unit (no mid-entry
    cuts in selection).

    Content before the first ``##``/``###`` header is emitted as a
    single tuple with ``header=""`` so file-level H1s / preambles
    aren't silently dropped. Empty bodies are preserved (header-only
    entries are valid). The header line is kept verbatim — callers
    render it as-is."""
    if not text:
        return []
    entries: list[tuple[str, str]] = []
    current_header: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_header is None:
            body = "\n".join(current_body).strip()
            if body:
                entries.append(("", body))
            return
        body = "\n".join(current_body).strip()
        entries.append((current_header, body))

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("### ") or stripped.startswith("## "):
            _flush()
            current_header = line
            current_body = []
        else:
            current_body.append(line)
    _flush()
    return entries


def _entry_render_cost(header: str, body: str) -> int:
    """Bytes for an entry rendered as ``header\\n\\nbody``."""
    if header and body:
        return len((header + "\n\n" + body).encode("utf-8"))
    return len((header or body).encode("utf-8"))


def _format_entry(header: str, body: str) -> str:
    if header and body:
        return header + "\n\n" + body
    return header or body


def _sort_entries_by_recency(
    entries: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Sort ``entries`` so the newest dated entries come first.

    Anchor: ``YYYY-MM-DD`` somewhere in the header line. Headers
    without a parseable date sort AFTER all dated entries, in their
    original document order. Among dated entries, sort key is
    ``(-date, original_index)`` so ties (same date) preserve the
    file's relative order — stable, deterministic, predictable.

    The migration ephemerals + manual entry-write discipline both
    front-date entries with ISO dates, so the parser hits the
    common case; the original-order fallback covers preambles, H1s,
    and any pre-date legacy entries without surprise."""
    if not entries:
        return []
    keyed: list[tuple[int, str, int, tuple[str, str]]] = []
    for idx, entry in enumerate(entries):
        header = entry[0]
        m = _PROJECT_DATE_RE.search(header) if header else None
        if m:
            # Dated entry: bucket 0, sort date desc via inverse.
            keyed.append((0, m.group(1), idx, entry))
        else:
            # Undated entry: bucket 1 (after all dated), preserve idx.
            keyed.append((1, "", idx, entry))
    # bucket asc (dated first), date desc within dated via inversion,
    # idx asc for tiebreak within both buckets.
    keyed.sort(
        key=lambda k: (k[0], _inverse_date(k[1]) if k[1] else "", k[2])
    )
    return [e for _b, _d, _i, e in keyed]


def _inverse_date(date_str: str) -> str:
    """Invert an ISO date string for descending sort under a stable
    ascending sort. ``2026-05-29`` → string sortable so that newer
    dates produce smaller values. We do this by char-complementing
    digits against '9'."""
    out_chars: list[str] = []
    for ch in date_str:
        if ch.isdigit():
            out_chars.append(str(9 - int(ch)))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def _build_footer(omitted: int, file_path: Path) -> str:
    """Always-on file-path pointer footer. Tells the agent where the
    full file lives so they can ``Read`` or ``grep`` for older
    entries that didn't fit. ``omitted == 0`` still emits the
    pointer — the agent should always know the file's on-disk
    location.

    Wording is unified across the truncated and non-truncated cases
    so downstream parsers and grep checks have a single pattern to
    match against."""
    path_str = str(file_path)
    if omitted > 0:
        noun = "entry" if omitted == 1 else "entries"
        return (
            f"_({omitted} more {noun} omitted by recency. "
            f"Full file: {path_str} — Read or grep for older.)_"
        )
    return (
        f"_(Full file: {path_str} — Read or grep for older entries.)_"
    )


def _render_project_file(path: Path, budget: int) -> str:
    """Read ``path`` and return entry-aware, recency-sorted body
    suitable for inclusion under ``### LEARNINGS`` / ``### MEMORY``.

    Selection: parse → sort by recency (dated newest-first, undated
    after in file order) → greedy-fill within budget skipping
    overlarge entries (no mid-entry cuts) → append always-on
    file-path-pointer footer.

    On degenerate budgets where even the footer wouldn't fit
    alongside any entry, the footer wins — orch's "always tell the
    agent where the file is" constraint dominates.

    Files with no ``##``/``###`` headers fall back to byte-truncated
    head + footer."""
    if not path.is_file() or budget <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.strip():
        return ""

    entries = _parse_markdown_entries(text)
    if not entries:
        # Flat prose: byte-truncate the head, still emit pointer.
        footer = _build_footer(0, path)
        footer_cost = len(footer.encode("utf-8")) + 2  # +sep
        head_budget = max(budget - footer_cost, 0)
        data = text.strip().encode("utf-8")[:head_budget]
        head = data.decode("utf-8", errors="ignore").rstrip()
        if head:
            return head + "\n\n" + footer
        return footer

    sorted_entries = _sort_entries_by_recency(entries)

    # Reserve budget for the footer so it always lands. The footer
    # text length depends on ``omitted``, but the longest form
    # (truncated case) bounds it — use that for the reserve.
    longest_footer = _build_footer(len(entries), path)
    reserve = max(
        len(longest_footer.encode("utf-8")) + 2,  # +sep
        _PROJECT_FOOTER_RESERVE,
    )
    effective = max(budget - reserve, 1)

    rendered: list[tuple[str, str]] = []
    used = 0
    sep = 2  # "\n\n"
    for header, body in sorted_entries:
        cost = _entry_render_cost(header, body)
        cost += sep if rendered else 0
        if used + cost > effective:
            continue
        rendered.append((header, body))
        used += cost

    omitted = len(entries) - len(rendered)
    footer = _build_footer(omitted, path)
    parts = [_format_entry(h, b) for h, b in rendered]
    parts.append(footer)
    return "\n\n".join(parts)


def _render_project_capsule(paths: Paths, agent: str) -> str:
    """Inject per-project LEARNINGS+MEMORY for projects associated
    with the agent.

    Resolution chain (highest priority first):

    1. **MISSION.md frontmatter** — ``project: <name>`` or
       ``projects: [a, b]``. Explicit per-agent override, supports
       multi-project, beats every fallback. Skipped when no
       MISSION.md exists.
    2. **teams.yaml** — central agent→projects roster at
       ``~/.metasphere/teams.yaml``. Supports multi-project natively.
       Canonical replacement for B4's name-prefix string match.
    3. **Path-nested inference** — agent home at
       ``~/.metasphere/projects/<P>/agents/@<id>/`` → ``<P>``.
       Last-resort fallback for project-nested agents not yet in
       teams.yaml.

    Returns ``""`` when no source resolves any project."""
    from .specs import _parse_frontmatter
    from .teams import _lookup_agent_projects

    agent_dir = _agent_dirs(paths, agent)[0]
    mission_file = _agent_file(paths, agent, "MISSION.md")

    declared: list[str] = []
    if mission_file.is_file():
        try:
            text = mission_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            fm = _parse_frontmatter(text)
            scalar = fm.get("project")
            if isinstance(scalar, str) and scalar.strip():
                declared.append(scalar.strip())
            elif isinstance(scalar, list):
                declared.extend(
                    p for p in scalar if isinstance(p, str) and p
                )
            plural = fm.get("projects")
            if isinstance(plural, list):
                declared.extend(
                    p for p in plural if isinstance(p, str) and p
                )
            elif isinstance(plural, str) and plural.strip():
                declared.append(plural.strip())

    seen: set[str] = set()
    ordered: list[str] = []
    for p in declared:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    # (b) teams.yaml lookup — central roster. Supports multi-project.
    # Runs only when frontmatter declared nothing so explicit
    # MISSION.md frontmatter remains an unambiguous override.
    if not ordered:
        for p in _lookup_agent_projects(agent, paths):
            if p not in seen and (paths.projects / p).is_dir():
                seen.add(p)
                ordered.append(p)

    # (c) Path-nested inference — last-resort single-project fallback
    # for agents whose home dir sits under ``projects/<P>/agents/``
    # and don't have a teams.yaml entry yet.
    if not ordered:
        inferred = _infer_project_for_agent(paths, agent, agent_dir)
        if inferred:
            ordered.append(inferred)

    if not ordered:
        return ""

    # Divide the section budget across declared projects and split
    # 60/40 LEARNINGS/MEMORY within each. Under the canonical single-
    # project case (widget-eng), this gives LEARNINGS ~1228B and
    # MEMORY ~819B — both deep enough to land the most recent dated
    # entries under recency sort. ``_render_project_file`` then carves
    # the footer reserve internally so the always-on file-path pointer
    # always lands.
    per_project = max(_PROJECT_FILE_BYTE_CAP // max(len(ordered), 1), 512)
    learn_budget = int(per_project * 0.6)
    mem_budget = per_project - learn_budget

    sections: list[str] = []
    for p in ordered:
        proj_dir = paths.projects / p
        learnings = _render_project_file(
            proj_dir / "LEARNINGS.md", learn_budget,
        )
        memory = _render_project_file(
            proj_dir / "MEMORY.md", mem_budget,
        )
        parts: list[str] = []
        if learnings:
            parts.append("### LEARNINGS\n\n" + learnings)
        if memory:
            parts.append("### MEMORY\n\n" + memory)
        if parts:
            sections.append(f"## Project: {p}\n\n" + "\n\n".join(parts))

    if not sections:
        return ""

    return "\n\n".join(sections) + "\n"


def _render_project_migration_nudge(
    paths: Paths, agent: str, *, persist: bool = True
) -> str:
    """Cold-start nudge for agents whose agent-level LEARNINGS/MEMORY
    contain entries that look project-specific.

    Per the per-project memory spec (Phase 1, section "Periodic check
    in the hook"): scan the residual agent-level pool for known project
    tokens; if matches found, surface a one-line nudge to spawn a
    migration ephemeral. Strictly nudge-only — the spec explicitly says
    "Don't auto-migrate — that's destructive."

    Project tokens are the registered project names under
    ``~/.metasphere/projects/`` (one subdir per project). Match
    semantic: case-insensitive word-boundary regex per token. The
    word-boundary form avoids spurious substring hits (``widget``
    would otherwise catch ``widgetless`` and similar near-tokens).

    Sentinel cache at ``<agent_dir>/state/migration_nudge_seen`` stores
    a fingerprint of the LEARNINGS+MEMORY mtimes the last time we
    inspected. Subsequent calls suppress the nudge while the
    fingerprint matches — approximating "cache a flag once a session
    has been nudged" from the spec, extended to "until either source
    file is edited." Writing the sentinel on the no-match path too
    keeps the per-turn cost bounded for agents with nothing to migrate.

    Returns ``""`` when no agent-level files exist, no project tokens
    match, or the sentinel reports no change since last surfacing.
    """
    agent_dir = _agent_dirs(paths, agent)[0]
    learnings = _agent_file(paths, agent, "LEARNINGS.md")
    memory = _agent_file(paths, agent, "MEMORY.md")
    files = [f for f in (learnings, memory) if f.is_file()]
    if not files:
        return ""

    try:
        current_fp = "|".join(
            f"{f.name}:{f.stat().st_mtime_ns}" for f in files
        )
    except OSError:
        return ""

    sentinel = agent_dir / "state" / "migration_nudge_seen"
    if sentinel.is_file():
        try:
            stored = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            stored = ""
        if stored == current_fp:
            return ""

    projects_dir = paths.projects
    tokens: list[str] = []
    if projects_dir.is_dir():
        try:
            for entry in sorted(projects_dir.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    tokens.append(entry.name)
        except OSError:
            pass
    if not tokens:
        return ""

    matched_tokens: list[str] = []
    total_hits = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for token in tokens:
            pat = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            hits = pat.findall(text)
            if hits:
                if token not in matched_tokens:
                    matched_tokens.append(token)
                total_hits += len(hits)

    def _persist_sentinel() -> None:
        if not persist:
            return
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(current_fp, encoding="utf-8")
        except OSError:
            pass

    if not matched_tokens:
        _persist_sentinel()
        return ""

    proj_list = ", ".join(matched_tokens)
    noun = "reference" if total_hits == 1 else "references"
    body = (
        "## Per-project memory migration\n\n"
        f"{total_hits} {noun} in your agent-level LEARNINGS/MEMORY "
        f"look project-specific (matches: {proj_list}). "
        "Spawn a migration ephemeral to clean up.\n"
    )
    _persist_sentinel()
    return body


def _render_child_reports(paths: Paths, agent: str) -> str:
    """Show pending child agent completion reports (max 5)."""
    candidates = _agent_dirs(paths, agent)
    reports_dir = next(
        (directory / "child_reports" for directory in candidates
         if (directory / "child_reports").is_dir()),
        candidates[0] / "child_reports",
    )
    if not reports_dir.is_dir():
        return ""
    try:
        files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    if not files:
        return ""
    out = [f"## Child Agent Reports ({len(files)} pending)", ""]
    for f in files[:5]:
        child = f.stem.split("-")[0]
        try:
            body = f.read_text(encoding="utf-8").strip()
        except OSError:
            body = "(unreadable)"
        out.append(f"### @{child}")
        out.append(body)
        out.append("")
    return "\n".join(out)


def _render_drift_warning(paths: Paths) -> str:
    baseline = _baseline_hash(paths)
    if not baseline:
        return ""
    live = harness_hash(paths)
    if not live or live == baseline:
        return ""
    hash_file = paths.state / "harness_hash_baseline"
    return (
        "## ⚠ Harness drift detected\n"
        "\n"
        "Your baked-in CLAUDE.md / .claude/settings / metasphere-context has changed\n"
        "since this REPL started. The new content is on disk but you're still running\n"
        "the old version in memory.\n"
        "\n"
        "If the change is relevant to what you're doing (e.g. updated workflow,\n"
        "new slash command, new safety rule), run `/exit` — the tmux pane will\n"
        "respawn a fresh REPL with the latest harness. Otherwise ignore this and\n"
        "the warning will keep appearing until you reload.\n"
        "\n"
        f"_(suppress: `echo $_live_hash > {hash_file}` to silence without reloading)_\n"
    )


_TELEGRAM_BYTE_CAP = 1024


def _render_telegram(paths: Paths, history: int = 3) -> str:
    """Render the recent telegram conversation.

    Reads from :func:`metasphere.telegram.archiver.telegram_context`
    directly (no subprocess). Caps at ``_TELEGRAM_BYTE_CAP`` bytes.
    """
    from .telegram.archiver import telegram_context

    # An agent only renders its OWN bot's conversation. Derive the owned
    # surface the same way cli/telegram.py::_own_telegram_surface_id does:
    # @orchestrator keeps the bare "telegram" surface (single-bot installs
    # stay byte-identical); any other agent maps to "telegram-<agent>", so
    # an agent that owns no telegram bot matches nothing and renders the
    # empty state instead of another bot's chat.
    agent = os.environ.get("METASPHERE_AGENT_ID", "@orchestrator")
    surface_id = "telegram" if agent == "@orchestrator" else "telegram-" + agent.lstrip("@")

    try:
        body = telegram_context(
            history=history, base_dir=str(paths.telegram), surface_id=surface_id
        )
    except Exception:
        body = ""
    if not body.strip():
        return "## Telegram (recent conversation)\n(no recent messages)\n"
    data = body.encode("utf-8")[:_TELEGRAM_BYTE_CAP]
    return data.decode("utf-8", errors="ignore").rstrip() + "\n"


# Most-recent unread inbound messages to render in full. The section used to
# dump EVERY unread message with no cap: an agent that dispatches a lot (the
# orchestrator has ~2,300 unread, almost all its own outbound !task records
# that never leave UNREAD) rendered a ~340KB wall that the outer delta cap
# then hard-chopped mid-entry. Cap + newest-first ordering keeps the section
# bounded and ensures truncation can only ever drop the OLDEST, not the fresh.
_MESSAGES_RENDER_CAP = 15


def _render_messages(paths: Paths, *, view: bool = True) -> str:
    msgs = _msgs.collect_inbox(paths.scope, paths.project_root, view=view)
    unread_msgs = [m for m in msgs if m.status == _msgs.STATUS_UNREAD]
    unread = len(unread_msgs)
    total = len(msgs)
    if total == 0:
        return "## Messages: No messages in scope\n"

    # Separate the agent's OWN outbound dispatch records from real inbound.
    # A dispatched !task to a sub-agent lands UNREAD in a visible bucket and
    # never transitions — it is a send record, not an inbound ask. Rendering
    # them presents the agent's own dispatches as if they were actionable.
    # Fail-open: if the agent id can't be resolved, treat none as self so we
    # degrade to "show recent unread" rather than hiding everything.
    try:
        me = resolve_agent_id(paths)
    except Exception:
        me = ""
    inbound = [m for m in unread_msgs if not (me and m.from_ == me)]
    self_outbound = unread - len(inbound)

    # Newest-first by created (ISO strings sort lexically); id breaks ties for
    # determinism. A missing/empty created (older schema) sorts to the bottom
    # and is treated as oldest — fine, such records are effectively the oldest
    # and stay counted in the header. Cap the render so it can't balloon.
    inbound.sort(key=lambda m: (m.created, m.id), reverse=True)
    shown = inbound[:_MESSAGES_RENDER_CAP]

    out = [
        f"## Messages ({unread} unread, {total} total)",
        f"## Scope: {rel_path(paths.scope, paths.project_root)}",
        "",
    ]
    for m in shown:
        icon = _STATUS_ICON.get(m.status, "?")
        reply = f" ↩ reply to {m.reply_to}" if m.reply_to else ""
        body_preview = " ".join(m.body.split())[:60]
        out.append(f"{icon} {m.label} from {m.from_} [{m.id}]{reply}")
        out.append(f"  {m.scope} | {m.created}")
        out.append(f"  {body_preview}")
        out.append("")

    # Fold-away counts: older inbound past the cap, plus the agent's own
    # outbound dispatch records (not actionable inbound). Header count stays
    # accurate over ALL unread; these reconcile unread = shown + older + self.
    older = len(inbound) - len(shown)
    tail_bits: list[str] = []
    if older > 0:
        tail_bits.append(f"+{older} older unread")
    if self_outbound > 0:
        tail_bits.append(f"{self_outbound} own outbound dispatch(es) hidden")
    if tail_bits:
        out.append("… " + ", ".join(tail_bits))
        out.append("")
    return "\n".join(out) + "\n"


def _render_tasks(paths: Paths) -> str:
    items = _tasks.list_tasks(paths.scope, paths.project_root, include_completed=False)
    if not items:
        return "## Tasks: No active tasks in scope\n"
    out = [f"## Tasks ({len(items)} active)", ""]
    for t in items:
        icon = {
            "pending": "○",
            "in-progress": "◐",
            "blocked": "◼",
            "completed": "●",
        }.get(t.status, "?")
        suffix = f" → {t.assignee}" if t.assignee else ""
        out.append(f"{icon} {t.priority} {t.title} [{t.id}]{suffix}")
        out.append(f"  {t.scope} | {t.status}")
    return "\n".join(out) + "\n"


def _render_events(paths: Paths, n: int = 10) -> str:
    log = paths.events_log
    if not log.is_file():
        return "## Recent Events\n(no recent events)\n"
    try:
        with open(log, "r", encoding="utf-8") as f:
            # Constant memory single-pass tail.
            tail = list(_collections.deque(f, maxlen=n))
    except OSError:
        return "## Recent Events\n(no recent events)\n"
    out = ["## Recent Events", ""]
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("timestamp", "")
        typ = rec.get("type", "")
        msg = rec.get("message", "")
        out.append(f"- {ts} [{typ}] {msg}")
    return "\n".join(out) + "\n"


def _render_directives(paths: Paths) -> str:
    """Render broadcast directives from DIRECTIVES.yaml at project root."""
    from . import directives as _directives
    return _directives.render_directives(paths)


def _auto_memory_dir_for_path(repo_path: str) -> Path | None:
    """Resolve a project's Claude auto-memory folder from its repo path.

    Claude encodes an absolute repo path into its projects-dir slug by
    replacing ``/`` with ``-`` (the leading slash becomes the leading dash),
    e.g. ``/home/u/projects/widget`` → ``-home-u-projects-widget`` under
    ``~/.claude/projects/<slug>/memory``. Returns ``None`` for an empty path.
    The folder may not exist yet (a project with no curated memory); callers
    check ``.is_dir()`` before promising it holds anything.
    """
    if not repo_path:
        return None
    slug = re.sub(r"[/.]", "-", str(Path(repo_path).expanduser().resolve()))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _memory_index_head(mem_dir: Path, n: int = 5) -> list[str]:
    """Return up to ``n`` index pointer lines (``- [Title](file.md) — hook``)
    from a project's ``MEMORY.md``, newest-first by the index convention
    (entries are added at the top). Each line is byte-bounded; missing /
    unreadable index → empty list (never raises)."""
    idx = mem_dir / "MEMORY.md"
    if not idx.is_file():
        return []
    try:
        text = idx.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ["):  # MEMORY.md index pointer convention
            out.append(s if len(s) <= 160 else s[:157] + "...")
            if len(out) >= n:
                break
    return out


def _append_project_tasks(body: list[str], proj, paths: Paths, cap: int = 7) -> None:
    """Append a matched project's own open tasks to its mention capsule (Stage B
    "tasks-in-capsule"). Reads only that project's ``.tasks/active/`` — never the
    global bucket — so the agent sees where the named project's work stands
    without a relevance gate. Capped at ``cap`` (overflow summarised). Best-effort:
    any failure leaves ``body`` untouched (the capsule degrades to memory-only)."""
    try:
        from .tasks import active_tasks_for_project
        tasks = active_tasks_for_project(proj, paths)
    except Exception:
        return
    if not tasks:
        return
    body.append("open tasks:")
    for t in tasks[:cap]:
        suffix = f" → {t.assignee}" if t.assignee else ""
        body.append(f"  ○ {t.priority} {t.title} [{t.id}]{suffix}")
    if len(tasks) > cap:
        body.append(f"  … +{len(tasks) - cap} more")


def _render_mentioned_projects(paths: Paths, agent: str, prompt: str) -> str:
    """Stage A/B of the memory-context-injection-hygiene proposal.

    Deterministic, threshold-free memory injection keyed on an *explicit
    project mention* in the prompt. When the user names a registered project,
    surface that project's memory-folder location + the head of its memory
    index — no relevance gate to mis-tune (the 0.65 FTS threshold is exactly
    why "No relevant memory found" shows on most turns).

    Purely additive: returns ``""`` on empty prompts (heartbeat / manual
    turns) and on prompts that name no project — so the dominant turn shapes
    are untouched. Fires only on a high-confidence explicit mention.
    """
    if not prompt or not prompt.strip():
        return ""
    try:
        from .memory.mention import detect_mentioned_projects
        projects = detect_mentioned_projects(prompt, paths)
    except Exception:
        return ""
    if not projects:
        return ""

    blocks: list[str] = []
    for proj in projects:
        mem_dir = _auto_memory_dir_for_path(proj.path)
        body = [f"### {proj.name}"]
        if mem_dir is not None and mem_dir.is_dir():
            body.append(f"memory: {mem_dir}")
            lines = _memory_index_head(mem_dir, n=5)
            if lines:
                body.append("recent:")
                body.extend(f"  {ln}" for ln in lines)
        elif mem_dir is not None:
            body.append(f"memory: {mem_dir} (no memories written yet)")
        _append_project_tasks(body, proj, paths)
        blocks.append("\n".join(body))

    if not blocks:
        return ""
    return "## Mentioned projects\n\n" + "\n\n".join(blocks) + "\n"


def _render_memory_fts(
    paths: Paths, agent: str, prompt: str = "", *, suppress_empty: bool = False
) -> str:
    """Pull the memory section using CAM (primary) + token-overlap (fallback).

    Replaced the token-overlap-only path on 2026-04-17 because the
    near-static query (task file + project name) produced identical
    results every turn — operator-flagged the "noise" at 22:16Z.

    Now: HybridStrategy(CamStrategy + TokenOverlapStrategy) with a
    turn-varying signal injected into the query so ranking shifts.

    ``prompt`` (the user's message this turn) leads the query when
    present — recall is scored primarily against what was just asked.
    Empty on heartbeat/manual turns, where the query falls back to the
    ambient stem (task file + project name + latest event).

    ``suppress_empty`` (Stage C, FTS-suppress-on-match): when a
    deterministic project-mention capsule already rendered this turn, the
    no-hits affordance (memory-folder location + "write new memories
    here") is pure double-injection — the capsule already names the
    folder. Set this when the mentioned-projects section is non-empty so
    the FTS path returns ``""`` on no hits instead of the redundant
    dead-end block. Real FTS hits still render (they're genuinely
    additive — the "you might also recall" path).
    """
    from .memory import (
        AutoMemoryStrategy,
        CamStrategy,
        HybridStrategy,
        TokenOverlapStrategy,
        context_for as _memory_context_for,
    )
    from .specs import get_spec_for_agent

    spec = get_spec_for_agent(agent, paths)
    if spec is not None and not spec.auto_memory:
        return ""

    out = ["## Memory Context (FTS)"]

    # Build query: user prompt (highest signal) + static stem (task +
    # project) + fresh signal (last event). The prompt leads so recall
    # is scored primarily against what the user just asked; the stem and
    # fresh signal keep the query non-empty and turn-varying when there
    # is no prompt (heartbeat/manual turns).
    task_file = _agent_file(paths, agent, "task")
    query_parts: list[str] = []
    if prompt and prompt.strip():
        query_parts.append(prompt.strip())
    if task_file.is_file():
        try:
            query_parts.append(task_file.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    query_parts.append(paths.project_root.name)

    # Fresh signal: a small window of recent *substantive* activity, so the
    # query reflects what is actually happening rather than the tick
    # machinery. Taking only the single latest event (the pre-2026-07-27
    # behaviour) collapsed on heartbeat turns: the most-recent event is
    # almost always a low-entropy internal tick (cron_fire "task:consolidate",
    # heartbeat.invoke, agent.heartbeat "turn N"), so the query was ~constant
    # across heartbeats and pinned the same top-N memos every turn — and the
    # dominant "consolidate" token self-reinforced by matching the
    # consolidation-themed memos. Filtering ticks and widening to a few
    # distinct recent messages keeps the signal live and content-bearing.
    fresh = _fresh_activity_signal(paths)
    if fresh:
        query_parts.append(fresh)

    query = " ".join(p for p in query_parts if p).replace("\n", " ")
    query = " ".join(query.split())[:300] or agent

    # Auto-memory first (orchestrator's curated MEMORY.md memos —
    # highest signal, pure Python, fast), then CAM (historical Claude
    # session transcripts), then token-overlap as final fallback.
    strategies = [HybridStrategy([
        AutoMemoryStrategy(root=_auto_memory_dir_for_path(str(paths.project_root))),
        # CAM scores are batch-normalized, so the top result is always 1.0.
        # Require an independent content-token anchor and cap the automatic
        # bridge to two hits; explicit memory-search CLI calls remain ungated.
        CamStrategy(
            fast=True,
            timeout=2.0,
            # Ambient task/project/event terms help other strategies rank,
            # but must never make a CAM transcript eligible. On user turns,
            # only the actual prompt can anchor historical transcript recall;
            # heartbeat/manual turns do not surface CAM-only history.
            anchor_query=prompt,
            min_lexical_overlap=1,
            max_eligible_hits=1,
        ),
        TokenOverlapStrategy(paths),
    ])]
    body = _memory_context_for(
        query, budget_chars=2048, strategies=strategies,
    ).strip()
    if not body and suppress_empty:
        # A project-mention capsule already rendered this turn and names the
        # memory folder; the no-hits affordance below would just double-inject
        # the same pointer (and read as a scary "nothing found" right after a
        # capsule that DID surface memories). Drop the whole FTS section.
        return ""
    if not body:
        # Don't print the scary "No relevant memory found." dead-end (reads as
        # broken). Point the agent at its own memory folder instead — turns a
        # dead-end into a useful affordance (write-here). Proposal Stage C.
        #
        # Anchor the folder to this agent's project_root, NOT to the process
        # PWD (the old _default_memory_root() read os.environ["PWD"], which on
        # a heartbeat turn is whatever cwd the REPL happens to sit in — e.g.
        # $HOME — and produced a TRUNCATED slug like ".../-home-u/memory"
        # instead of the real ".../-home-u-projects-<repo>/memory").
        # _auto_memory_dir_for_path derives the slug deterministically from the
        # project path, matching the same root used everywhere else in context
        # assembly. Fail-open: any failure falls back to the bare message.
        root = None
        try:
            root = _auto_memory_dir_for_path(str(paths.project_root))
        except Exception:
            root = None
        if root is not None:
            body = (
                f"No memories matched this turn. Your memory folder: {root}\n"
                "(write new memories there as you learn — they surface here next time.)"
            )
        else:
            body = "No memories matched this turn."
    out.append(body)
    return "\n".join(out) + "\n"


# Event types that are pure tick machinery, not agent activity. They fire
# on a fixed schedule regardless of what is happening (the @consolidate cron
# alone fires every ~5 min), so on heartbeat turns the single most-recent
# event is almost always one of these. Their messages are also low-entropy
# ("task:consolidate", "injected heartbeat...", "turn 33820") and actively
# harmful as a recall signal: the constant "consolidate" token pins the
# consolidation-themed memos every turn. Excluding them makes the fresh
# signal reflect real activity (a message.send, a task, a wake reason).
_LOW_ENTROPY_EVENT_TYPES = frozenset({
    "schedule.cron_fire",
    "heartbeat.invoke",
    "agent.heartbeat",
    "message.consolidate",
})

# Matches a formatted tail line: "HH:MM:SSZ [type] @agent: message".
_TAIL_LINE_RE = re.compile(r"^\S+\s+\[([^\]]+)\]\s+[^:]*:\s?(.*)$")


def _fresh_activity_signal(paths: Paths, *, window: int = 40, keep: int = 3) -> str:
    """Return a short signal built from recent *substantive* events.

    Reads a window of recent events, drops low-entropy tick machinery
    (:data:`_LOW_ENTROPY_EVENT_TYPES`), and joins the most recent ``keep``
    distinct surviving messages (newest first). This keeps the memory-recall
    query live and content-bearing instead of pinned by the near-constant
    latest-tick message.

    Fail-open: any parsing/IO error returns ``""`` so a bad fresh signal can
    never crash context assembly — the query still stands on prompt + stem.
    """
    from . import events as _events
    try:
        tail = _events.tail_events(window, paths=paths)
        if not tail or not tail.strip():
            return ""
        picked: list[str] = []
        seen: set[str] = set()
        # Newest first so the freshest substantive activity leads.
        for line in reversed(tail.strip().splitlines()):
            m = _TAIL_LINE_RE.match(line.strip())
            if m is None:
                continue
            etype, msg = m.group(1), m.group(2).strip()
            if etype in _LOW_ENTROPY_EVENT_TYPES:
                continue
            msg = msg[:80].strip()
            if not msg or msg in seen:
                continue
            seen.add(msg)
            picked.append(msg)
            if len(picked) >= keep:
                break
        return " ".join(picked)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------


def _render_project(paths: Paths) -> str:
    """Project header for the per-turn context block.

    Walks upward from ``paths.scope`` to find the enclosing project; if
    found, emits name/goal/members/recent activity. Empty string when
    the current scope is not inside any project (keeps the turn block
    unchanged for non-project agents).
    """
    from . import project as _project
    from . import agents as _agents

    proj = _project.project_for_scope(paths.scope, paths=paths)
    if proj is None:
        return ""

    out = [f"## Project: {proj.name}"]
    if proj.goal:
        out.append(f"Goal: {proj.goal}")

    # Members with alive/dormant marker. Alive = tmux session exists.
    if proj.members:
        parts: list[str] = []
        for m in proj.members:
            marker = ""
            if m.persistent:
                # ``_resolve_session`` walks the agent registry so
                # project-scoped members (which is the common case in
                # @project context) are checked under their actual
                # ``metasphere-<project>-<agent>`` session name, not
                # the bare form that misses them.
                try:
                    from .session import _resolve_session
                    alive = _agents.session_alive(_resolve_session(m.id))
                except Exception:
                    alive = False
                marker = ", alive" if alive else ", dormant"
            parts.append(f"{m.id} ({m.role}{marker})")
        out.append("Members: " + ", ".join(parts))
    else:
        out.append("Members: (none)")

    # Scope line: show the project path + whether the agent is inside it.
    scope_inside = str(paths.scope).startswith(str(proj.path))
    scope_label = "(active)" if scope_inside else "(external)"
    out.append(f"Scope: {proj.path} {scope_label}")

    # Recent activity: count of active tasks + last commit subject with
    # timestamps so the agent can gauge freshness.
    from . import tasks as _tasks
    try:
        active = _tasks.list_tasks(Path(proj.path), paths.project_root,
                                   include_completed=False)
        task_n = len(active)
        # Most recent task update timestamp
        latest_update = ""
        for t in active:
            u = getattr(t, "updated", "") or ""
            if u > latest_update:
                latest_update = u
    except Exception:
        task_n = 0
        latest_update = ""
    last_commit = ""
    commit_ts = ""
    git_dir = Path(proj.path) / ".git"
    if git_dir.exists():
        try:
            res = subprocess.run(
                ["git", "-C", proj.path, "log", "-1",
                 "--pretty=%s|%aI"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            parts = res.stdout.strip().splitlines()[0].rsplit("|", 1) if res.stdout.strip() else [""]
            last_commit = parts[0]
            commit_ts = parts[1] if len(parts) > 1 else ""
        except (subprocess.SubprocessError, OSError, IndexError):
            pass
    activity = f"{task_n} tasks active"
    if latest_update:
        activity += f", latest task update: {latest_update[:16]}"
    if last_commit:
        ts_part = f" ({commit_ts[:16]})" if commit_ts else ""
        activity += f", last commit: {last_commit}{ts_part}"
    out.append(f"Recent: {activity}")

    # Shared artifacts: list top-level files (max 10) so agents see what
    # teammates have dropped in the cross-agent dir without having to ls.
    shared_dir = paths.projects / proj.name / "shared"
    if shared_dir.is_dir():
        try:
            entries = sorted(p.name for p in shared_dir.iterdir() if p.is_file())
        except OSError:
            entries = []
        if entries:
            shown = ", ".join(entries[:10])
            more = f" (+{len(entries) - 10} more)" if len(entries) > 10 else ""
            out.append(f"Shared: {shared_dir} — {shown}{more}")
        else:
            out.append(f"Shared: {shared_dir} (empty)")
    return "\n".join(out) + "\n"


_LAST_EDITED_NOISE = {
    "__pycache__", ".git", ".venv", "node_modules", ".metasphere",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".egg-info", ".eggs",
}
_LAST_EDITED_LIMIT = 10


def _render_last_edited_files(paths: Paths) -> str:
    """Show the most recently modified files under the project scope.

    Skipped when not inside a project (root-scope agents get no noise).
    """
    from . import project as _project

    proj = _project.project_for_scope(paths.scope, paths=paths)
    if proj is None or not proj.path:
        return ""
    proj_path = Path(proj.path)
    if not proj_path.is_dir():
        return ""

    candidates: list[tuple[float, str]] = []
    try:
        for entry in proj_path.rglob("*"):
            if not entry.is_file():
                continue
            # Skip noise directories
            parts = entry.relative_to(proj_path).parts
            if any(p in _LAST_EDITED_NOISE or p.endswith(".egg-info") for p in parts):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            rel = str(entry.relative_to(proj_path))
            candidates.append((mtime, rel))
    except OSError:
        return ""

    if not candidates:
        return ""

    candidates.sort(reverse=True)
    top = candidates[:_LAST_EDITED_LIMIT]

    out = [f"## Last Edited Files [{proj.name}]"]
    for mtime, rel in top:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        out.append(f"  {rel} — {ts}")
    return "\n".join(out) + "\n"


SECTION_NAMES = (
    "status",
    "drift",
    "project",
    "telegram",
    "messages",
    "tasks",
    "events",
    "memory",
)


def build_context(
    paths: Paths | None = None,
    *,
    budget: int = DEFAULT_SECTION_BUDGET,
    prompt: str = "",
    read_only: bool = False,
) -> str:
    """Assemble the per-turn context block. Section order is load-bearing.

    ``prompt`` is the user's message for this turn (empty on heartbeat /
    manual invocations). When present it leads the memory-recall query so
    recall is scored against what was just asked rather than only ambient
    state; when empty, recall falls back to the prior ambient-stem query.

    ``read_only`` preserves the same visible context for direct Codex turns
    while disabling inbox read receipts and migration-nudge cache writes.
    Managed gateway/headless turns retain the existing bookkeeping behavior.
    """
    paths = paths or resolve()
    agent = resolve_agent_id(paths)

    sections: list[str] = []

    # Host-health ALERT: goes at the TOP so the agent sees a zombie /
    # tmux / PID-headroom trip before any other context. Empty string
    # when nothing is tripped, which keeps the zero-impact invariant
    # on normal turns.
    try:
        from .gateway.monitoring import render_alert as _render_alert
        alert = _render_alert(paths)
    except Exception:
        alert = ""
    sections.append(truncate_section(alert, budget) if alert else "")

    sections.append(truncate_section(_render_status_header(paths, agent), budget))
    # Persona files are stable baseline context and receive independent byte
    # budgets. A long SOUL can no longer consume the shared capsule budget and
    # silently erase IDENTITY or USER facts later in the combined string.
    persona = _persona_sections(paths, agent)
    sections.extend(truncate_section(part, budget) for part in persona if part)
    mission = _render_mission_capsule(paths, agent)
    sections.append(truncate_section(mission, budget) if mission else "")
    project_capsule = _render_project_capsule(paths, agent)
    sections.append(truncate_section(project_capsule, budget) if project_capsule else "")
    migration_nudge = _render_project_migration_nudge(
        paths, agent, persist=not read_only
    )
    sections.append(truncate_section(migration_nudge, budget) if migration_nudge else "")
    drift = _render_drift_warning(paths)
    sections.append(truncate_section(drift, budget) if drift else "")
    directives_block = _render_directives(paths)
    sections.append(truncate_section(directives_block, budget) if directives_block else "")
    project_block = _render_project(paths)
    sections.append(truncate_section(project_block, budget) if project_block else "")
    sections.append(truncate_section(_render_telegram(paths), budget))
    child_reports = _render_child_reports(paths, agent)
    sections.append(truncate_section(child_reports, budget) if child_reports else "")
    sections.append(truncate_section(
        _render_messages(paths, view=not read_only), budget
    ))
    sections.append(truncate_section(_render_tasks(paths), budget))
    sections.append(truncate_section(_render_events(paths), budget))
    last_edited = _render_last_edited_files(paths)
    sections.append(truncate_section(last_edited, budget) if last_edited else "")
    mentioned = _render_mentioned_projects(paths, agent, prompt)
    sections.append(truncate_section(mentioned, budget) if mentioned else "")
    # When a deterministic project capsule rendered, suppress the FTS path's
    # no-hits affordance so the two don't double-inject the same memory-folder
    # pointer (proposal Stage C, FTS-suppress-on-match). Real FTS hits still show.
    fts = _render_memory_fts(
        paths, agent, prompt, suppress_empty=bool(mentioned)
    )
    sections.append(truncate_section(fts, budget) if fts else "")

    return "\n".join(s for s in sections if s).rstrip() + "\n"
