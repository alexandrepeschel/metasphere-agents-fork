"""``metasphere gateway`` — control surface for the gateway daemon.

Front-end onto ``metasphere.gateway`` for operator-driven actions: ad-hoc
Telegram injection (drop a message straight onto an agent's inbox as if
it had arrived from the bot), status probes, and start/stop helpers
that proxy the systemd unit. The gateway daemon itself runs out of
``metasphere.gateway.daemon``; this shim never embeds the long-running
loop — it only emits signals and one-shot RPC-style commands.
"""

from __future__ import annotations

DESCRIPTION = "Gateway daemon control + Telegram injection helpers."

USAGE = """\
Usage: metasphere gateway <command> [args...]

Commands:
  daemon [<interval>]    Run the gateway daemon (Telegram poll +
                         orchestrator REPL watchdog). <interval>
                         is the inter-poll sleep in seconds (default 0.5;
                         message latency is driven by the 25s long-poll
                         timeout, not this value).
  inject "msg"           Inject <msg> directly into the orchestrator's
                         tmux session (bypasses Telegram).
  ensure                 Start the orchestrator session if it is not
                         already alive.
  status                 Print orchestrator session liveness + idle.
  restart                Recreate the orchestrator tmux session and REPL.
"""


import argparse
import sys
from typing import List, Optional

from metasphere.gateway import (
    SESSION_NAME,
    ensure_session,
    render_status as render_monitoring_status,
    restart_session,
    run_daemon,
    session_health,
    start_session,
)
from metasphere.paths import resolve
from metasphere.telegram.inject import submit_to_tmux


def cmd_daemon(args: argparse.Namespace) -> int:
    paths = resolve()
    run_daemon(paths, poll_interval=float(args.interval))
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    paths = resolve()
    ensure_session(paths)
    ok = submit_to_tmux("@cli", args.text, session=SESSION_NAME)
    return 0 if ok else 1


def cmd_ensure(args: argparse.Namespace) -> int:
    paths = resolve()
    ensure_session(paths)
    alive, idle = session_health(paths)
    print(f"session={SESSION_NAME} alive={alive} idle={idle}s")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = resolve()
    alive, idle = session_health(paths)
    print(f"session={SESSION_NAME} alive={alive} idle={idle}s")
    print(render_monitoring_status(paths))
    return 0 if alive else 1


def cmd_restart(args: argparse.Namespace) -> int:
    paths = resolve()
    alive, _ = session_health(paths)
    if not alive:
        if start_session(paths):
            print(f"session={SESSION_NAME} started")
            return 0
        print(f"session={SESSION_NAME} start failed", file=sys.stderr)
        return 1
    if restart_session("CLI restart", paths):
        print(f"session={SESSION_NAME} recreated")
        return 0
    print(f"session={SESSION_NAME} restart failed", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway", description="metasphere gateway CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_daemon = sub.add_parser("daemon", help="run gateway daemon (poll + watchdog)")
    p_daemon.add_argument("interval", nargs="?", default="0.5", help="inter-poll sleep seconds (default 0.5)")
    p_daemon.set_defaults(func=cmd_daemon)

    p_inject = sub.add_parser("inject", help="inject text into orchestrator session")
    p_inject.add_argument("text")
    p_inject.set_defaults(func=cmd_inject)

    p_ensure = sub.add_parser("ensure", help="start session if not alive")
    p_ensure.set_defaults(func=cmd_ensure)

    p_status = sub.add_parser("status", help="session status")
    p_status.set_defaults(func=cmd_status)

    p_restart = sub.add_parser("restart", help="restart claude inside session")
    p_restart.set_defaults(func=cmd_restart)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    parser = build_parser()
    args = parser.parse_args(args_list)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
