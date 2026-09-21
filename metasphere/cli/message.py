"""``message`` CLI — cross-surface outbound dispatch.

The single entry point for sending a message to a human-facing surface
(Telegram, Slack, future: email). ``--surface auto`` reads the active
conversation pin written by the matching inbound handler so a
multi-message reply stays on whichever surface the user most recently
addressed the agent on.

``metasphere telegram send`` is preserved as a thin wrapper for
back-compat — too many existing scripts pin the legacy form.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from metasphere import contacts as _contacts


DESCRIPTION = "Send a message across any surface (Telegram, Slack, ...)."

USAGE = """\
Usage: metasphere message send "<text>" [--surface auto|<id>] [--to <name>] [--chat-id <id>] [--thread-id <id>]

Options:
  --surface auto|<id>   Pick the surface for this send. Default: auto
                        (reads the calling agent's active_conversation
                        pin from ~/.metasphere/agents/@<id>/active_conversation).
                        Explicit values: telegram, telegram-relay,
                        slack-cluster-1, ...
  --to <name>           Named contact from ~/.metasphere/ADDRESSBOOK.yaml.
                        Surface-aware: a contact entry can carry a
                        per-surface key (telegram-cluster-1: ...) or fall
                        back to the surface_type key (telegram: ...).
  --chat-id <id>        Raw chat id; bypasses addressbook.
  --thread-id <id>      Telegram forum topic id; requires a Telegram surface.
  --body-file PATH      Read the body verbatim from a file (no shell quoting).

For rich content — parens, bullets (•), backticks, $, quotes, newlines —
pass the body via stdin (positional "-") or --body-file to avoid ALL shell
escaping, e.g. `metasphere message send - --to alice < body.txt`.
In positional text, literal \\n and \\r\\n sequences become line breaks;
double the backslash to send a literal \\n sequence.

Resolution order for `--surface auto`:
  1. ~/.metasphere/agents/@<METASPHERE_AGENT_ID>/active_conversation
     → uses {surface_id, chat_id} verbatim.
  2. Legacy fallback: default Telegram chat id (single-surface back-compat).

The legacy `metasphere telegram send` CLI continues to work and is a
thin wrapper that pins surface_id to "telegram".
"""


_SUPPORTED_SURFACE_TYPES = ("telegram", "slack")


def _surface_type(surface_id: str) -> str:
    head, _, _ = surface_id.partition("-")
    return head


def _resolve_calling_agent() -> str:
    agent = os.environ.get("METASPHERE_AGENT_ID", "@orchestrator")
    if not agent.startswith("@"):
        agent = "@" + agent
    return agent


def _resolve_auto_surface(agent: str) -> tuple[Optional[str], Optional[str | int]]:
    """Read the agent's active_conversation pin.

    Returns ``(surface_id, chat_id)`` or ``(None, None)`` if no pin
    exists. Caller decides whether to fall back to the legacy default.
    """
    from metasphere.paths import resolve as _resolve
    from metasphere.routing.active import get_active_conversation

    pin = get_active_conversation(agent, _resolve())
    if not pin:
        return None, None
    return str(pin.get("surface_id") or ""), pin.get("chat_id")


def _dispatch(
    surface_id: str,
    chat_id: str | int,
    text: str,
    *,
    sender_agent: str,
    thread_id: int | None = None,
) -> int:
    """Route ``text`` to the right adapter for ``surface_id``."""
    stype = _surface_type(surface_id)
    if stype == "telegram":
        from metasphere.telegram import api as _tg_api, archiver as _tg_arch
        try:
            tg_chat_id: int = int(chat_id)
        except (TypeError, ValueError):
            print(
                f"Error: telegram chat_id must be numeric (got {chat_id!r}).",
                file=sys.stderr,
            )
            return 2
        kwargs = {"surface_id": surface_id}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        _tg_api.send_with_cc(tg_chat_id, text, **kwargs)
        _tg_arch.archive_outgoing(
            sender_agent, text, tg_chat_id, surface_id=surface_id
        )
        print(f"Sent to {tg_chat_id} via {sender_agent} (surface={surface_id})")
        return 0
    if stype == "slack":
        if thread_id is not None:
            print("Error: --thread-id is only valid for Telegram.", file=sys.stderr)
            return 2
        try:
            from metasphere.slack import api as _sl_api  # PR2 lands this
        except ImportError:
            print(
                f"Error: surface_id '{surface_id}' selects slack but the "
                f"slack adapter is not installed in this build. Install "
                f"the slack-bolt / slack-sdk dependencies and ensure "
                f"metasphere.slack is importable.",
                file=sys.stderr,
            )
            return 2
        _sl_api.send_with_cc(
            surface_id, str(chat_id), text, sender_agent_id=sender_agent,
        )
        print(f"Sent to {chat_id} via {sender_agent} (surface={surface_id})")
        return 0
    print(
        f"Error: unknown surface_type '{stype}' for surface_id "
        f"'{surface_id}'. Supported: {', '.join(_SUPPORTED_SURFACE_TYPES)}.",
        file=sys.stderr,
    )
    return 2


def cmd_send(args: argparse.Namespace) -> int:
    from metasphere.cli._body import resolve_body

    try:
        text = resolve_body(args.text, args.body_file)
    except ValueError as e:
        print(f"Error: {e}.", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: cannot read --body-file: {e}", file=sys.stderr)
        return 2

    sender = _resolve_calling_agent()
    surface_id: Optional[str] = args.surface
    chat_id: Optional[str | int] = args.chat_id

    # Auto surface resolution: read the active_conversation pin.
    if surface_id is None or surface_id == "auto":
        auto_surface, auto_chat = _resolve_auto_surface(sender)
        if auto_surface:
            surface_id = auto_surface
            if chat_id is None and args.to is None:
                chat_id = auto_chat
        else:
            # Back-compat: no pin, fall back to legacy Telegram default.
            print(
                "[WARN] message send --surface auto: no active_conversation "
                f"pin for {sender}; falling back to legacy telegram default.",
                file=sys.stderr,
            )
            surface_id = "telegram"

    # Addressbook lookup (only when chat_id wasn't set inline / by auto).
    if chat_id is None and args.to:
        handle = _contacts.lookup_contact(args.to, surface_id)
        if handle is None:
            print(
                f"Error: contact '{args.to}' has no entry for surface "
                f"'{surface_id}' (or its surface_type fallback) in "
                f"~/.metasphere/ADDRESSBOOK.yaml.",
                file=sys.stderr,
            )
            return 2
        chat_id = handle

    # Final fallback: legacy default Telegram recipient.
    if chat_id is None and _surface_type(surface_id) == "telegram":
        chat_id = _contacts.default_telegram_chat_id()

    if chat_id is None:
        print(
            "Error: no chat id. Pass --chat-id, --to <name>, or set "
            "`default-recipient: <name>` in ~/.metasphere/ADDRESSBOOK.yaml.",
            file=sys.stderr,
        )
        return 2

    return _dispatch(
        surface_id, chat_id, text,
        sender_agent=sender,
        thread_id=args.thread_id,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="message", description="metasphere message CLI (cross-surface)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send", help="send a message on any surface")
    p_send.add_argument("text", nargs="?", default=None,
                        help='message text (or "-" to read the body from stdin)')
    p_send.add_argument(
        "--surface", default="auto",
        help='Surface id ("telegram", "telegram-relay", "slack-cluster-1", '
             '...) or "auto" (default — read active_conversation pin)',
    )
    p_send.add_argument(
        "--to", default=None,
        help="Named contact from ~/.metasphere/ADDRESSBOOK.yaml",
    )
    p_send.add_argument(
        "--chat-id", default=None,
        help="Raw chat id; bypasses addressbook",
    )
    p_send.add_argument(
        "--thread-id", type=int, default=None,
        help="Telegram forum topic id",
    )
    from metasphere.cli._body import add_body_file_arg
    add_body_file_arg(p_send)
    p_send.set_defaults(func=cmd_send)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    parser = build_parser()
    args = parser.parse_args(args_list)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
