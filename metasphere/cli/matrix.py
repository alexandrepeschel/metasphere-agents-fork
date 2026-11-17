"""``matrix`` CLI — Matrix-specific send, mirroring ``metasphere telegram send``.

Most workflows should prefer ``metasphere message send --surface matrix``
(cross-surface). This is the shorthand with Matrix-only options
(``--html``, ``--voice``, documents).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

DESCRIPTION = "Send Matrix messages, documents and voice notes."

USAGE = """\
Usage: metasphere matrix send "<markdown>" [--room <id>] [--surface <id>]
       metasphere matrix send - < body.md
       metasphere matrix send --html "<b>bold</b> text" [--room <id>]
       metasphere matrix send-document PATH [--caption TEXT] [--voice] [--room <id>]

Options:
  --room <id>       Room id (!abc:server). Default: MATRIX_ROOM_ALEX from
                    ~/.metasphere/config/matrix.env (the DM with the operator).
  --surface <id>    Matrix surface id (default: matrix).
  --html            Treat the body as HTML (b, i, a, code, ...) instead of
                    markdown.
  --body-file PATH  Read the body verbatim from a file (no shell quoting).
  --voice           (send-document) post as a voice message (m.audio).

Markdown (numbered lists, bold, code) is rendered by Element. For rich
content skip shell quoting: `metasphere matrix send - <<'EOF' ... EOF`.
"""


def _mark_explicit(agent: str) -> None:
    if agent != "@orchestrator":
        return
    try:
        from metasphere import paths as _paths
        from metasphere.posthook import mark_orchestrator_explicit_send

        mark_orchestrator_explicit_send(_paths.resolve())
    except Exception:  # noqa: BLE001
        pass


def _room(args: argparse.Namespace, cfg) -> Optional[str]:
    return args.room or cfg.room_alex or None


def cmd_send(args: argparse.Namespace) -> int:
    from metasphere.cli._body import resolve_body
    from metasphere.matrix import api

    try:
        text = resolve_body(args.text, args.body_file)
    except ValueError as e:
        print(f"Error: {e}.", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: cannot read --body-file: {e}", file=sys.stderr)
        return 2
    try:
        cfg = api.load_config(args.surface)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    room = _room(args, cfg)
    if not room:
        print("Error: no room. Pass --room or set MATRIX_ROOM_ALEX in matrix.env.",
              file=sys.stderr)
        return 2
    agent = os.environ.get("METASPHERE_AGENT_ID", "@orchestrator")
    try:
        if args.html:
            api.send_with_cc(room, html=text, cfg=cfg)
        else:
            api.send_with_cc(room, text, cfg=cfg)
    except api.MatrixAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    _mark_explicit(agent)
    try:
        from metasphere.telegram import archiver

        archiver.archive_outgoing(agent, text, room, surface_id=args.surface)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass
    print(f"Sent to {room} via {agent} (surface={args.surface})")
    return 0


def cmd_send_document(args: argparse.Namespace) -> int:
    from metasphere.matrix import api

    if not os.path.exists(args.path):
        print(f"Error: file not found: {args.path}", file=sys.stderr)
        return 2
    try:
        cfg = api.load_config(args.surface)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    room = _room(args, cfg)
    if not room:
        print("Error: no room. Pass --room or set MATRIX_ROOM_ALEX.", file=sys.stderr)
        return 2
    agent = os.environ.get("METASPHERE_AGENT_ID", "@orchestrator")
    caption = args.caption
    if agent != "@orchestrator" and caption:
        caption = f"[{agent.lstrip('@')}] {caption}"
    try:
        eid = api.send_with_cc(
            room, document_path=args.path, caption=caption,
            filename=args.filename, voice=args.voice, cfg=cfg,
        )
    except api.MatrixAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    _mark_explicit(agent)
    kind = "voice" if args.voice else "document"
    print(f"Sent {kind} {args.path} to {room} via {agent} (event={eid})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="matrix", description="metasphere matrix CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a message to a Matrix room")
    s.add_argument("text", nargs="?", default=None,
                   help='message text (or "-" to read the body from stdin)')
    s.add_argument("--room", default=None)
    s.add_argument("--surface", default="matrix")
    s.add_argument("--html", action="store_true")
    from metasphere.cli._body import add_body_file_arg
    add_body_file_arg(s)
    s.set_defaults(func=cmd_send)

    d = sub.add_parser("send-document", help="upload a file / voice note")
    d.add_argument("path")
    d.add_argument("--caption", default=None)
    d.add_argument("--filename", default=None)
    d.add_argument("--voice", action="store_true")
    d.add_argument("--room", default=None)
    d.add_argument("--surface", default="matrix")
    d.set_defaults(func=cmd_send_document)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    args = build_parser().parse_args(args_list)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
