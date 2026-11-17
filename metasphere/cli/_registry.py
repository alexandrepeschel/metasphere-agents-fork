"""Single source of truth for ``metasphere`` CLI subcommands.

Each top-level subcommand resolves to a ``module:function`` entry. The
module is lazy-imported on demand. Every handler module exports two
module-level constants used to compose ``metasphere --help`` and
``docs/CLI.md`` from a single source:

* ``DESCRIPTION`` — one-sentence summary used in the top-level listing.
* ``USAGE`` — full ``metasphere <subcmd> --help`` body.

Help-rendering helpers in this module never run any handler's main()
function: they only import the handler module so its constants can be
read. The top-level dispatcher in ``metasphere.cli.main`` uses
``REGISTRY`` directly for routing and only reads constants when the
caller asks for help.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class Subcommand:
    """One row in the top-level registry."""

    name: str
    module: str  # dotted module path, e.g. ``metasphere.cli.agents``
    function: str = "main"

    @property
    def import_path(self) -> str:
        return f"{self.module}:{self.function}"


# Order is preserved in --help output and docs/CLI.md.
SUBCOMMANDS: tuple[Subcommand, ...] = (
    Subcommand("status",       "metasphere.cli.status"),
    Subcommand("ls",           "metasphere.cli.ls"),
    Subcommand("agent",        "metasphere.cli.agents"),
    Subcommand("msg",          "metasphere.cli.messages"),
    Subcommand("task",         "metasphere.cli.tasks"),
    Subcommand("questions",    "metasphere.cli.questions"),
    Subcommand("message",      "metasphere.cli.message"),
    Subcommand("telegram",     "metasphere.cli.telegram"),
    Subcommand("slack",        "metasphere.cli.slack"),
    Subcommand("matrix",       "metasphere.cli.matrix"),
    Subcommand("hooks",        "metasphere.cli.hooks"),
    Subcommand("schedule",     "metasphere.cli.schedule"),
    Subcommand("consolidate",  "metasphere.cli.consolidate"),
    Subcommand("heartbeat",    "metasphere.cli.heartbeat"),
    Subcommand("events",       "metasphere.cli.events"),
    Subcommand("memory",       "metasphere.cli.memory"),
    Subcommand("trace",        "metasphere.cli.trace"),
    Subcommand("session",      "metasphere.cli.session"),
    Subcommand("sessions",     "metasphere.cli.sessions"),
    Subcommand("liveness",     "metasphere.cli.liveness"),
    Subcommand("project",      "metasphere.cli.project"),
    Subcommand("gateway",      "metasphere.cli.gateway"),
    Subcommand("daemon",       "metasphere.cli.daemon"),
    Subcommand("logs",         "metasphere.cli.logs"),
    Subcommand("config",       "metasphere.cli.config"),
    Subcommand("restart",      "metasphere.cli.restart"),
    Subcommand("update",       "metasphere.cli.update"),
    Subcommand("accounts",     "metasphere.cli.accounts"),
    Subcommand("addressbook",  "metasphere.cli.addressbook"),
    Subcommand("audit-docs",   "metasphere.cli.audit_docs"),
    Subcommand("migrate-project-dirs", "metasphere.cli.migrate"),
    Subcommand("docs",         "metasphere.cli.docs"),
    Subcommand("version",      "metasphere.cli.version"),
)

# Convenience lookup for the dispatcher (kept compatible with the
# pre-refactor dict[str, str] shape).
REGISTRY: dict[str, str] = {sc.name: sc.import_path for sc in SUBCOMMANDS}


def resolve(name: str) -> Callable[[list[str]], int]:
    """Return the handler callable for ``name`` (raises KeyError on miss)."""
    sc = _by_name(name)
    mod = importlib.import_module(sc.module)
    return getattr(mod, sc.function)


def describe(name: str) -> str:
    """Return the one-line ``DESCRIPTION`` for ``name``."""
    sc = _by_name(name)
    mod = importlib.import_module(sc.module)
    desc = getattr(mod, "DESCRIPTION", None)
    if not desc:
        return ""
    return str(desc).strip().splitlines()[0]


def usage(name: str) -> str:
    """Return the full ``USAGE`` block for ``name``."""
    sc = _by_name(name)
    mod = importlib.import_module(sc.module)
    body = getattr(mod, "USAGE", None) or getattr(mod, "__doc__", "") or ""
    return str(body).rstrip() + "\n"


def render_top_help() -> str:
    """Compose ``metasphere --help`` text from each subcommand's DESCRIPTION."""
    lines = [
        "metasphere - unified CLI for the Metasphere agent system",
        "",
        "Usage: metasphere <subcommand> [args...]",
        "",
        "Subcommands:",
    ]
    width = max(len(sc.name) for sc in SUBCOMMANDS) + 2
    for sc in SUBCOMMANDS:
        try:
            desc = describe(sc.name)
        except Exception as exc:  # noqa: BLE001
            desc = f"(could not import: {exc})"
        lines.append(f"  {sc.name.ljust(width)}{desc}")
    lines += [
        "",
        "Run `metasphere <subcommand> --help` for details.",
        "",
    ]
    return "\n".join(lines)


def render_docs_md() -> str:
    """Compose ``docs/CLI.md`` from per-handler USAGE blocks."""
    parts = [
        "<!--",
        "  Auto-generated by `metasphere docs` from each handler's",
        "  DESCRIPTION + USAGE constants. Do NOT edit by hand — edit the",
        "  source module under metasphere/cli/ and re-run `metasphere docs`.",
        "-->",
        "",
        "# Metasphere CLI Reference",
        "",
        "The unified `metasphere` CLI surface. All subcommands resolve",
        "through `metasphere.cli.main`; each handler module declares its",
        "own `DESCRIPTION` and `USAGE` constants which feed both the",
        "interactive `--help` output and this document.",
        "",
        "## Top-level help",
        "",
        "```",
        render_top_help().rstrip(),
        "```",
        "",
        "## Subcommands",
        "",
    ]
    for sc in SUBCOMMANDS:
        try:
            desc = describe(sc.name)
            body = usage(sc.name).rstrip()
        except Exception as exc:  # noqa: BLE001
            desc = f"(import error: {exc})"
            body = ""
        parts.append(f"### `metasphere {sc.name}`")
        parts.append("")
        if desc:
            parts.append(desc)
            parts.append("")
        parts.append("```")
        parts.append(body if body else "(no usage block)")
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def all_names() -> Iterable[str]:
    return (sc.name for sc in SUBCOMMANDS)


def _by_name(name: str) -> Subcommand:
    for sc in SUBCOMMANDS:
        if sc.name == name:
            return sc
    raise KeyError(name)
