"""``metasphere session`` — per-agent tmux session control.

Front-end onto ``metasphere.session`` and ``metasphere.tmux`` for
operating on a single agent's session: attach, capture pane state,
kill, restart. Each persistent agent owns one tmux session running a
Claude Code REPL; this shim is the operator's read/write surface
into that session without forcing them to remember the underlying
``metasphere-<agent>`` naming convention.
"""

from __future__ import annotations

DESCRIPTION = "Inspect, attach, and control per-agent tmux sessions."

USAGE = """\
Usage: metasphere session <command> [args...]

Commands:
  list                            List all live and dormant sessions.
  info <@agent>                   Show session metadata + last activity.
  attach <@agent>                 Attach the current terminal to the
                                  agent's session.
  stop <@agent>                   Kill the agent's session.
  restart <@agent> [reason]       Restart the agent's session (kills +
                                  re-creates).
  send <@agent> <message>         Inject <message> into the agent's
                                  Claude REPL.
  exit-self                       Cleanly exit the current agent's
                                  Claude REPL (used as a scheduled-job
                                  payload tail to free its idle slot).
"""


import os
import shlex
import subprocess
import sys

from metasphere.agents import mark_exit_self, session_alive
from metasphere.events import log_event
from metasphere.gateway.session import _tmux
from metasphere.session import (
    _resolve_session,
    attach_to,
    list_sessions,
    restart_session,
    send_to_session,
    session_info,
    stop_session,
)


_USAGE_HINTS = {
    "info":    "Use: session info <@agent>",
    "attach":  "Use: session attach <@agent>",
    "stop":    "Use: session stop <@agent>",
    "restart": "Use: session restart <@agent> [reason]",
    "send":    "Use: session send <@agent> <message>",
}


def _reject_flag_shape(value: str, op: str) -> int | None:
    from metasphere.cli._argv import reject_flag_shape

    return reject_flag_shape(
        value, op,
        command="metasphere session",
        what="agent id",
        usage=_USAGE_HINTS.get(op),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    if not args:
        sys.stderr.write(USAGE)
        return 2
    cmd, *rest = args

    if cmd in ("list", "ls"):
        if rest:
            head = rest[0]
            kind = "flag" if head.startswith("-") else "argument"
            sys.stderr.write(
                f"metasphere session list: unexpected {kind}: {head}\n"
                f"Usage: metasphere session list (takes no arguments)\n"
            )
            return 2
        rows = list_sessions()
        if not rows:
            print("(no metasphere sessions)")
            return 0
        for s in rows:
            mark = "●" if s.attached else "○"
            print(f"{mark} {s.agent:24} {s.name:32} windows={s.windows}")
        return 0

    if cmd == "info":
        if not rest:
            print("usage: session info <@agent>", file=sys.stderr)
            return 2
        rc = _reject_flag_shape(rest[0], "info")
        if rc is not None:
            return rc
        if len(rest) > 1:
            head = rest[1]
            kind = "flag" if head.startswith("-") else "argument"
            sys.stderr.write(
                f"metasphere session info: unexpected trailing {kind}: {head}\n"
                f"Usage: metasphere session info <@agent>\n"
            )
            return 2
        s = session_info(rest[0])
        if not s:
            print(f"no session: {rest[0]}", file=sys.stderr)
            return 1
        print(f"name:     {s.name}")
        print(f"agent:    {s.agent}")
        print(f"windows:  {s.windows}")
        print(f"created:  {s.created}")
        print(f"attached: {s.attached}")
        return 0

    if cmd == "attach":
        if not rest:
            print("usage: session attach <@agent>", file=sys.stderr)
            return 2
        rc = _reject_flag_shape(rest[0], "attach")
        if rc is not None:
            return rc
        return attach_to(rest[0])

    if cmd == "stop":
        if not rest:
            print("usage: session stop <@agent>", file=sys.stderr)
            return 2
        rc = _reject_flag_shape(rest[0], "stop")
        if rc is not None:
            return rc
        ok = stop_session(rest[0])
        if ok:
            print(f"stopped {rest[0]}")
        else:
            print(f"no session for {rest[0]}", file=sys.stderr)
            return 1
        return 0

    if cmd == "restart":
        if not rest:
            print("usage: session restart <@agent> [reason]", file=sys.stderr)
            return 2
        rc = _reject_flag_shape(rest[0], "restart")
        if rc is not None:
            return rc
        agent = rest[0]
        reason = " ".join(rest[1:]) if len(rest) > 1 else "CLI restart"
        ok = restart_session(agent, reason)
        if ok:
            print(f"restarting {agent}: {reason}")
        else:
            print(f"no session for {agent}", file=sys.stderr)
            return 1
        return 0

    if cmd == "send":
        if len(rest) < 2:
            print("usage: session send <@agent> <message>", file=sys.stderr)
            return 2
        rc = _reject_flag_shape(rest[0], "send")
        if rc is not None:
            return rc
        agent = rest[0]
        message = " ".join(rest[1:])
        ok = send_to_session(agent, message)
        if ok:
            print(f"sent to {agent}")
        else:
            print(f"no session for {agent}", file=sys.stderr)
            return 1
        return 0

    if cmd in ("exit-self", "exit_self"):
        # Schedule ``tmux kill-session -t <target>`` for the caller's
        # own session via a detached background process. Resolves the
        # caller from $METASPHERE_AGENT_ID.
        #
        # History: this used to inject a C-c x2 + /exit + Enter x2
        # keystroke sequence to graceful-exit claude. 3-day soak
        # (2026-05-05 → 05-07) showed 0/10 successes — the keystrokes
        # were ignored or buffered by mid-turn claude panes, and every
        # event fell through to the 30-min ephemeral reaper anyway.
        # Replaced with a hard session kill: simpler, deterministic,
        # and the only consumers (cron-fired ephemerals flagged
        # wants_exit_self_cleanup=True) want the tmux slot released
        # outright. Persistent collaborators leave the flag False and
        # never call this.
        #
        # Why detached + start_new_session: the kill targets the
        # caller's own session, which contains this CLI process. Run
        # inline, the kill would terminate ourselves before we could
        # log_event or return. A detached child survives the parent's
        # death; the pre-sleep gives the caller's Bash tool, any final
        # assistant text, and the Stop hook room to complete before
        # the session goes away.
        caller = os.environ.get("METASPHERE_AGENT_ID")
        if not caller:
            print("Error: $METASPHERE_AGENT_ID not set", file=sys.stderr)
            return 1
        target = _resolve_session(caller)
        if not session_alive(target):
            # Headless ``claude -p`` ephemeral: nothing to kill, the
            # process exits on its own when the turn ends. But the
            # clean-exit intent must still be recorded — cron jobs
            # tell every ephemeral to run exit-self last, and without
            # the tombstone that natural exit reads as pid-dead +
            # no-session, so reap_crashed files it as a silent death
            # (every headless cron run since 2026-09-24).
            try:
                marked = mark_exit_self(caller, target)
            except Exception:
                marked = False
            if not marked:
                print(
                    f"Error: no live tmux session for {caller} "
                    f"(resolved to {target}) and no agent dir to record "
                    f"the exit in.",
                    file=sys.stderr,
                )
                return 1
            try:
                log_event(
                    "agent.exit_self",
                    f"{caller} recorded clean exit (headless, no session)",
                    agent=caller,
                    meta={"session": target, "headless": True},
                )
            except Exception:
                pass
            print(
                f"headless: clean exit recorded for {caller}; "
                f"the process exits on its own when this turn ends"
            )
            return 0
        # Tombstone before the kill is queued: once the detached kill
        # lands, pid and session both read dead and the next
        # reap_crashed sweep would classify this clean exit as a
        # silent death (false crash !alert — 2026-07-05 @writing-lead
        # case). Best-effort: a failed write must not block the exit.
        try:
            mark_exit_self(caller, target)
        except Exception:
            pass
        t = shlex.quote(target)
        # 20s pre-sleep: longer than the previous 2.5s graceful path
        # because this is now a hard kill — give the caller's Bash
        # tool time to return, claude time to emit final assistant
        # text, and the Stop hook time to complete its work before
        # the session is destroyed.
        kill_sh = f"sleep 20; tmux kill-session -t {t}"
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell=True
            ["bash", "-c", kill_sh],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            log_event(
                "agent.exit_self",
                f"{caller} queued kill-session for own session {target}",
                agent=caller,
                meta={"session": target},
            )
        except Exception:
            pass
        print(f"queued kill-session for {target} ({caller}) in 20s")
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
