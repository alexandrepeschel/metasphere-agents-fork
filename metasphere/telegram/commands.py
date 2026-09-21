"""Slash command dispatch for the telegram bot.

Each command returns a string (the body to send back). The dispatcher
calls into ``metasphere/*`` modules where they exist; for commands not
yet implemented natively, we shell out to ``scripts/`` equivalents.

Adding a new command:
1. Define a ``cmd_<name>(args, ctx)`` function returning a string.
2. Register it in the COMMANDS dict at the bottom.

Commands receive a ``Context`` carrying chat_id, thread_id, from_user
so they can compose replies that respect forum topics.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional

# Telegram-controlled identifiers that get interpolated into filesystem
# paths or argv must match this; rejects "..", "/", whitespace, etc.
_AGENT_RE = re.compile(r"^@[A-Za-z0-9_-]+$")
_LABEL_RE = re.compile(r"^![A-Za-z0-9_-]+$")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
METASPHERE_DIR = os.path.expanduser("~/.metasphere")


@dataclass
class Context:
    chat_id: int
    from_user: str
    thread_id: Optional[int] = None


@dataclass
class Reply:
    """Rich command reply.

    A command may return either a bare ``str`` (sent as plain text) or a
    ``Reply`` carrying an explicit ``parse_mode``. The dispatcher in
    ``cli/telegram.py`` knows how to unwrap both shapes. ``parse_mode='HTML'``
    is the only mode the format module currently emits — content is
    pre-escaped via ``metasphere.format.escape_html``.
    """

    text: str
    parse_mode: Optional[str] = None


def _run(cmd: list[str], env: Optional[dict] = None, timeout: int = 15) -> str:
    """Run a subprocess and return combined output, truncated."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    except FileNotFoundError as e:
        return f"(missing: {e.filename})"


# --- Commands -------------------------------------------------------------

def cmd_start(args: str, ctx: Context) -> str:
    return (
        "Metasphere (rewrite)\n\n"
        "Commands:\n"
        "/status /tasks /agents /messages /inbox\n"
        "/send @agent !label msg\n"
        "/projects [sub args...] | /memory query | /groups | /link\n"
        "/events | /tree | /spot | /ping | /help\n\n"
        "Or just message me directly."
    )


def cmd_help(args: str, ctx: Context) -> str:
    return cmd_start(args, ctx)


def cmd_ping(args: str, ctx: Context) -> str:
    return "pong"


def cmd_status(args: str, ctx: Context) -> str:
    try:
        from metasphere.status import summary

        return summary()
    except Exception as e:
        return f"(status error: {e})"


def cmd_tasks(args: str, ctx: Context) -> "Reply | str":
    """List active tasks with HTML cards.

    Usage:
      /tasks              -- condensed one-line-per-task view across every
                             registered project, grouped by project name
      /tasks <project>    -- resolve <project> via the registry and list
                             tasks from that project's path (expanded cards)
    """
    try:
        from metasphere.tasks import (
            list_tasks,
            is_active,
            active_tasks_across_projects,
        )
        from metasphere.format import format_task_table, format_task_condensed
        from metasphere.paths import resolve
        from metasphere.project import _find_project, list_projects

        paths = resolve()
        project_name = (args or "").strip().split()[0] if args else ""

        if not project_name:
            # All-projects condensed view: open (non-terminal) tasks across the
            # whole registry, shared with /status via one helper so the two
            # counters can't drift. Includes blocked + paused open work.
            active = active_tasks_across_projects(paths)
            if not active:
                names = [p.name for p in list_projects(paths=paths)]
                return Reply(
                    "No active tasks across any registered project.\n"
                    "Known projects: "
                    + (", ".join(names) if names else "(none)"),
                    parse_mode="HTML",
                )
            body = format_task_condensed(active, html=True)
            return Reply(body, parse_mode="HTML")

        registered = _find_project(project_name, paths)
        if registered is None:
            return (
                f"(unknown project: {project_name})\n"
                "Known projects: "
                + ", ".join(p.name for p in list_projects(paths=paths))
            )
        scope = registered
        repo = registered
        tasks = list_tasks(scope, repo)
        active = [t for t in tasks if is_active(t)]
        header = f"Tasks ({project_name})"
        body = format_task_table(active, html=True)
        return Reply(f"<b>{header}</b>\n{body}", parse_mode="HTML")
    except Exception as e:
        return f"(tasks error: {e})"


def cmd_messages(args: str, ctx: Context) -> str:
    try:
        from metasphere import messages as ms_messages

        return ms_messages.list_messages_text()
    except Exception as e:
        return f"(messages error: {e})"


def cmd_agents(args: str, ctx: Context) -> "Reply | str":
    """List all agents across global + all projects."""
    try:
        from collections import defaultdict
        from metasphere import agents as _agents
        from metasphere.paths import resolve
        items = _agents.list_agents(resolve())
        persistent = [a for a in items if a.is_persistent]
        if not persistent:
            return "No agents registered."
        alive_count = sum(1 for a in persistent if _agents.session_alive(a.session_name))
        lines = [f"Agents ({alive_count}/{len(persistent)} alive)\n"]
        by_project: dict[str, list] = defaultdict(list)
        for a in persistent:
            by_project[a.project or "(global)"].append(a)
        for proj_name in sorted(by_project.keys()):
            agents = by_project[proj_name]
            if len(by_project) > 1:
                lines.append(f"\n<b>{proj_name}</b>")
            for a in agents:
                alive = _agents.session_alive(a.session_name)
                icon = "\U0001f7e2" if alive else "\u26aa"
                spec_file = a.agent_dir / "spec" if a.agent_dir else None
                spec_label = ""
                if spec_file and spec_file.is_file():
                    spec_label = f" ({spec_file.read_text().strip()})"
                display_name = a.name.lstrip("@")
                lines.append(_DIVIDER)
                lines.append(f"{icon}  {display_name}{spec_label}")
                lines.append(f"       Status: {a.status or '-'}")
        lines.append(_DIVIDER)
        return Reply("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        return f"Error: {e}"


def cmd_inbox(args: str, ctx: Context) -> str:
    target = (args.strip() or "@orchestrator")
    if not _AGENT_RE.match(target):
        return "Invalid agent name"
    try:
        from metasphere import messages as ms_messages

        return ms_messages.list_messages_text(agent=target)
    except Exception as e:
        return f"(inbox error: {e})"


def cmd_send(args: str, ctx: Context) -> str:
    parts = args.split(None, 2)
    if len(parts) < 3:
        return "Usage: /send @target !label message"
    target, label, message = parts
    if not _AGENT_RE.match(target):
        return "Invalid target (expected @name)"
    if not _LABEL_RE.match(label):
        return "Invalid label (expected !name)"
    try:
        from metasphere.messages import send_message
        from metasphere.paths import resolve

        send_message(
            target=target,
            label=label,
            body=message,
            from_agent="@user",
            paths=resolve(),
        )
        return f"Sent {label} to {target}"
    except Exception as e:
        return f"(send error: {e})"


def cmd_cam(args: str, ctx: Context) -> str:
    if not args.strip():
        return "Usage: /memory <query>"
    return _run(["cam", "search", args.strip(), "--limit", "5"])


def cmd_groups(args: str, ctx: Context) -> str:
    try:
        from metasphere.telegram import groups as tg_groups
        from metasphere.paths import resolve

        if not args.strip():
            topics = tg_groups.list_topics(paths=resolve())
            if not topics:
                return "(no forum topics)"
            return "\n".join(f"- {t.name} (id={t.id})" for t in topics)
        return f"Usage: /groups"
    except Exception as e:
        return f"(groups error: {e})"


def cmd_link(args: str, ctx: Context) -> str:
    name = args.strip().strip('"')
    if ctx.thread_id and not name:
        chat_clean = str(ctx.chat_id).removeprefix("-100")
        return f"Topic link: https://t.me/c/{chat_clean}/{ctx.thread_id}"
    if not name:
        return 'Usage: /link "Project Name"'
    try:
        from metasphere.project import get_project
        from metasphere.paths import resolve

        proj = get_project(name, paths=resolve())
        if proj is None:
            return f"Project not found: {name}"
        if not proj.telegram_topic:
            return f"No Telegram topic for {name}"
        return f"Topic: {proj.telegram_topic.get('name')} (id={proj.telegram_topic.get('id')})"
    except Exception as e:
        return f"(link error: {e})"


def cmd_events(args: str, ctx: Context) -> str:
    from metasphere.events import tail_events

    return tail_events(10)


def cmd_tree(args: str, ctx: Context) -> str:
    try:
        from metasphere.agents import list_agents, session_alive
        from metasphere.paths import resolve

        agents = list_agents(resolve())
        if not agents:
            return "(no agents)"
        lines = []
        for a in agents:
            live = ""
            try:
                if session_alive(a.session_name):
                    live = " [live]"
            except Exception:
                pass
            lines.append(f"{a.name}{live} — {a.status or 'unknown'}")
        return "\n".join(lines)
    except Exception as e:
        return f"(tree error: {e})"


def _run_project_cli(sub_argv: list[str]) -> str:
    """Dispatch to ``metasphere.cli.project.main`` in-process.

    Captures stdout + stderr and returns combined output. This avoids
    shelling out to the ``metasphere`` console script, so the bot stays
    inside one Python process and benefits from normal tracebacks.
    """
    import contextlib
    import io as _io

    from metasphere.cli import project as cli_project

    buf_out, buf_err = _io.StringIO(), _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = cli_project.main(sub_argv)
    except SystemExit as e:  # argparse error paths
        rc = int(e.code) if isinstance(e.code, int) else 2
    except Exception as e:  # pragma: no cover - defensive
        return f"project {' '.join(sub_argv)} failed: {e}"
    out = (buf_out.getvalue() + buf_err.getvalue()).strip()
    if rc != 0 and not out:
        out = f"(exit {rc})"
    return out or "(no output)"


def cmd_project(args: str, ctx: Context) -> "Reply | str":
    """Dispatch to ``metasphere project`` subcommands in-process.

    Bare ``/projects`` -> list with HTML cards. ``/projects <sub> [args...]``
    -> in-process call. Quoted args are honored via shlex.
    """
    import shlex
    sub_argv = shlex.split(args) if args.strip() else ["list"]

    # For 'list', render with the format module's card layout + HTML.
    if sub_argv[0] in ("list", "ls"):
        try:
            from metasphere.project import list_projects
            from metasphere.format import format_project_table
            from metasphere.paths import resolve

            projects = list_projects(paths=resolve())
            return Reply(format_project_table(projects, html=True), parse_mode="HTML")
        except Exception:
            pass  # fall through to CLI

    return _run_project_cli(sub_argv)


def cmd_spot(args: str, ctx: Context) -> str:
    """Check status of a remote host. Configure METASPHERE_REMOTE_HOST."""
    host = os.environ.get("METASPHERE_REMOTE_HOST")
    if not host:
        return "(METASPHERE_REMOTE_HOST not set)"
    port = os.environ.get("METASPHERE_REMOTE_PORT", "22")
    return _run(
        [
            "ssh", "-p", port, "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
            host, "metasphere status",
        ],
        timeout=10,
    )


def cmd_schedule(args: str, ctx: Context) -> str:
    """Inspect or interact with the metasphere schedule (cron-style jobs).

    Default action is ``list``. Forwards to ``metasphere schedule ...``.
    """
    import shlex
    sub_argv = shlex.split(args) if args.strip() else ["list"]
    try:
        from metasphere.cli import schedule as cli_schedule  # type: ignore
        import contextlib
        import io as _io

        prev_plain = os.environ.get("METASPHERE_PLAIN")
        prev_html = os.environ.get("METASPHERE_HTML")
        os.environ["METASPHERE_PLAIN"] = "1"
        os.environ["METASPHERE_HTML"] = "1"
        try:
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    rc = cli_schedule.main(sub_argv)
                except SystemExit as e:
                    rc = int(e.code) if isinstance(e.code, int) else 2
        finally:
            if prev_plain is None:
                os.environ.pop("METASPHERE_PLAIN", None)
            else:
                os.environ["METASPHERE_PLAIN"] = prev_plain
            if prev_html is None:
                os.environ.pop("METASPHERE_HTML", None)
            else:
                os.environ["METASPHERE_HTML"] = prev_html
        out = buf.getvalue().strip()
        if rc != 0 and not out:
            out = f"(exit {rc})"
        return Reply(out or "(no output)", parse_mode="HTML")
    except Exception:
        # Fallback to the entry-point binary
        return _run(
            ["metasphere", "schedule", *sub_argv],
            timeout=10,
        )


def cmd_session(args: str, ctx: Context) -> str:
    """Restart the orchestrator REPL so it picks up new CLAUDE.md / hooks.

    Default action is ``restart``. The gateway recreates the narrowly scoped
    orchestrator tmux session so recovery does not depend on a responsive TUI.

    Subcommands:
      restart  -> metasphere-gateway restart-orchestrator (default)
      status   -> systemctl --user status metasphere-gateway
    """
    sub = (args or "restart").strip().split(None, 1)[0] or "restart"
    if sub == "status":
        return _run(["systemctl", "--user", "status", "metasphere-gateway", "--no-pager"], timeout=5)
    if sub == "restart":
        try:
            from metasphere.gateway.session import restart_session
            from metasphere.paths import resolve

            if restart_session("Telegram /session restart", resolve()):
                return "Recreated orchestrator session and REPL."
            return "(restart error: failed to recreate orchestrator session)"
        except Exception as e:
            return f"(restart error: {e})"
    return f"Unknown /session subcommand: {sub}\nUsage: /session [restart|status]"


_DIVIDER = "\u2014" * 25


def cmd_specs(args: str, ctx: Context) -> "Reply | str":
    """List available agent specs."""
    try:
        from metasphere.specs import list_specs
        specs = list_specs()
        if not specs:
            return "No specs found."
        lines = [f"<b>Agent Specs</b> ({len(specs)})\n"]
        for s in specs:
            lines.append(_DIVIDER)
            lines.append(f"{s.name} ({s.role})")
            lines.append(f"       {s.description}")
            lines.append(f"       Sandbox: {s.sandbox}")
        lines.append(_DIVIDER)
        return Reply("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        return f"Error listing specs: {e}"


def cmd_questions(args: str, ctx: Context) -> str:
    """Show the cross-project 'needs from the operator' question ledger.

    Usage:
      /questions          -- every live 🔴/🟡/🟢 item, flag-sorted
      /questions red      -- only 🔴 blocking items
      /questions open     -- 🔴 + 🟡 (skip 🟢 FYI)
    """
    try:
        from metasphere.cli.questions import render_questions

        filter_ = (args or "").strip().split()[0] if args else None
        body, _rc = render_questions(filter_)
        return body
    except Exception as e:  # noqa: BLE001
        return f"(questions error: {e})"


def cmd_team(args: str, ctx: Context) -> "Reply | str":
    """Team operations: seed, wake, status.

    /team specs          - list available specs
    /team seed <spec> @name  - seed agent from spec
    /team status         - show team member status
    /team wake @name     - wake a seeded agent
    """
    import shlex as _shlex
    sub_argv = _shlex.split(args) if args.strip() else ["status"]
    sub = sub_argv[0] if sub_argv else "status"

    if sub == "specs":
        return cmd_specs("", ctx)

    if sub in ("assign", "dispatch"):
        # /team assign @agent "task description" [--project name]
        if len(sub_argv) < 3:
            return "Usage: /team assign agent-name \"task description\" [--project name]"
        target_agent = sub_argv[1]
        if not target_agent.startswith("@"):
            target_agent = "@" + target_agent
        task_title = sub_argv[2]
        project_name = ""
        if "--project" in sub_argv:
            idx = sub_argv.index("--project")
            if idx + 1 < len(sub_argv):
                project_name = sub_argv[idx + 1]
        try:
            from metasphere.tasks import dispatch_task
            result = dispatch_task(
                title=task_title,
                agent_id=target_agent,
                project=project_name,
            )
            task = result["task"]
            agent = result["agent"]
            agent_status = "woken" if agent else "not persistent"
            lines = [
                f"\u2705 Task dispatched",
                f"       Task: {task.id}",
                f"       Agent: {target_agent.lstrip('@')} ({agent_status})",
                f"       Title: {task_title}",
            ]
            if project_name:
                lines.append(f"       Project: {project_name}")
            return Reply("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            return f"Dispatch failed: {e}"

    if sub == "seed":
        if len(sub_argv) < 3:
            return "Usage: /team seed &lt;spec-name&gt; @agent-name [--project name]"
        spec_name = sub_argv[1]
        agent_id = sub_argv[2]
        project_name = ""
        if "--project" in sub_argv:
            idx = sub_argv.index("--project")
            if idx + 1 < len(sub_argv):
                project_name = sub_argv[idx + 1]
        try:
            from metasphere.specs import get_spec, seed_agent
            spec = get_spec(spec_name)
            if not spec:
                return f"Spec '{spec_name}' not found. Try /team specs"
            d = seed_agent(agent_id, spec, project_name=project_name)
            lines = [
                f"\u2705 Seeded {agent_id.lstrip('@')} from spec {spec_name}",
                "",
                f"       Dir: {d}",
                f"       Files: SOUL.md, MISSION.md, persona-index.md",
                f"       Wake: /team wake {agent_id}",
            ]
            return Reply("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            return f"Seed failed: {e}"

    if sub == "wake":
        if len(sub_argv) < 2:
            return "Usage: /team wake @agent-name"
        agent_id = sub_argv[1]
        try:
            from metasphere import agents as _agents
            rec = _agents.wake_persistent(agent_id)
            return f"\u2705 {rec.name.lstrip('@')} awake (session: {rec.session_name})"
        except Exception as e:
            return f"Wake failed: {e}"

    if sub == "status":
        try:
            from metasphere import agents as _agents
            from metasphere.paths import resolve
            # Optional project filter: /team status <project-name>
            project_filter = sub_argv[1] if len(sub_argv) > 1 else ""
            items = _agents.list_agents(resolve(), project=project_filter)
            persistent = [a for a in items if a.is_persistent]
            if not persistent:
                return "No persistent agents." + (f" (project: {project_filter})" if project_filter else "")
            alive_count = sum(1 for a in persistent if _agents.session_alive(a.session_name))
            title = f"<b>Team Status</b> ({alive_count}/{len(persistent)} alive)"
            if project_filter:
                title += f" — project: <b>{project_filter}</b>"
            lines = [title + "\n"]

            # Group by project
            from collections import defaultdict
            by_project: dict[str, list] = defaultdict(list)
            for a in persistent:
                by_project[a.project or "(global)"].append(a)

            for proj_name in sorted(by_project.keys()):
                agents = by_project[proj_name]
                if len(by_project) > 1:
                    lines.append(f"\n<b>{proj_name}</b>")
                for a in agents:
                    spec_file = a.agent_dir / "spec" if a.agent_dir else None
                    spec_label = ""
                    if spec_file and spec_file.is_file():
                        spec_label = f" ({spec_file.read_text().strip()})"
                    alive = _agents.session_alive(a.session_name)
                    icon = "\U0001f7e2" if alive else "\u26aa"
                    status = a.status or "-"
                    display_name = a.name.lstrip("@")
                    lines.append(_DIVIDER)
                    lines.append(f"{icon}  {display_name}{spec_label}")
                    lines.append(f"       Status: {status}")
            lines.append(_DIVIDER)
            return Reply("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            return f"Status failed: {e}"

    return (
        "Usage: /team &lt;subcommand&gt;\n"
        "  specs   - list available agent specs\n"
        "  seed    - seed agent from spec\n"
        "  wake    - wake a seeded agent\n"
        "  status  - show team member status"
    )


COMMANDS: Dict[str, Callable[[str, Context], str]] = {
    "start": cmd_start,
    "help": cmd_help,
    "h": cmd_help,
    "ping": cmd_ping,
    "status": cmd_status,
    "s": cmd_status,
    "tasks": cmd_tasks,
    "t": cmd_tasks,
    "questions": cmd_questions,
    "q": cmd_questions,
    "messages": cmd_messages,
    "m": cmd_messages,
    "agents": cmd_agents,
    "a": cmd_agents,
    "inbox": cmd_inbox,
    "send": cmd_send,
    "memory": cmd_cam,
    "cam": cmd_cam,  # backward compat alias
    "groups": cmd_groups,
    "link": cmd_link,
    "events": cmd_events,
    "tree": lambda args, ctx: cmd_team("status", ctx),  # legacy alias
    "spot": cmd_spot,
    "project": cmd_project,
    "projects": cmd_project,  # plural alias — /projects list etc.
    "p": cmd_project,
    # Hidden aliases — not in BOT_COMMANDS_MANIFEST. These exist so cached
    # autocomplete in users' Telegram clients (from when project_list/etc.
    # were published commands) still routes correctly instead of returning
    # "Unknown command". They forward to cmd_project with the subcommand
    # injected as the first arg.
    "project_list": lambda args, ctx: cmd_project(("list " + args).strip(), ctx),
    "project_show": lambda args, ctx: cmd_project(("show " + args).strip(), ctx),
    "project_new": lambda args, ctx: cmd_project(("new " + args).strip(), ctx),
    "project_wake": lambda args, ctx: cmd_project(("wake " + args).strip(), ctx),
    "project_chat": lambda args, ctx: cmd_project(("chat " + args).strip(), ctx),
    "schedule": cmd_schedule,
    "sched": cmd_schedule,
    "session": cmd_session,
    "team": cmd_team,
    "specs": cmd_specs,
}


# Manifest published to BotFather via setMyCommands. Short descriptions
# only — Telegram caps descriptions at 256 chars and the autocomplete UI
# truncates aggressively. Keep one-line, imperative.
BOT_COMMANDS_MANIFEST: list[tuple[str, str]] = [
    ("status", "Show orchestrator status"),
    ("tasks", "List active tasks"),
    ("questions", "Show 'needs from the operator' ledger"),
    ("messages", "Show inbox messages"),
    ("agents", "List all agents across projects"),
    ("team", "Project teams: /team [status|specs|seed|wake]"),
    ("specs", "List available agent specs"),
    ("send", "Send: /send @agent !label message"),
    ("projects", "Projects: /projects [list|show|new|wake|chat ...]"),
    ("schedule", "Inspect schedule: /schedule [list|show|run ...]"),
    ("memory", "Search agent memory: /memory <query>"),
    ("events", "Tail recent events"),
    ("spot", "Show remote host status"),
    ("session", "Restart orchestrator REPL"),
    ("help", "Show help"),
    ("ping", "Ping the bot"),
]


def register_bot_commands() -> dict:
    """Publish BOT_COMMANDS_MANIFEST to Telegram via setMyCommands.

    Returns the API response. Raises ``TelegramAPIError`` on failure.
    """
    import json as _json

    from metasphere.telegram import api as _api

    payload = [{"command": c, "description": d} for c, d in BOT_COMMANDS_MANIFEST]
    return _api.call("setMyCommands", commands=_json.dumps(payload))


def dispatch(text: str, ctx: Context) -> "Reply | str | None":
    """Dispatch a slash command.

    Returns one of:
      * ``None`` — not a slash command
      * ``str`` — plain-text reply
      * :class:`Reply` — text + parse_mode (e.g. ``'HTML'`` for /tasks)
    """
    if not text.startswith("/"):
        return None
    body = text[1:]
    name, _, args = body.partition(" ")
    # Strip @botname suffix Telegram appends in groups
    name = name.split("@", 1)[0].lower()
    fn = COMMANDS.get(name)
    if fn is None:
        return "Unknown command. Try /help"
    try:
        return fn(args, ctx)
    except Exception as e:  # pragma: no cover - defensive
        return f"Command /{name} failed: {e}"
