"""Stop-hook entry point.

Reads a Claude Code or Codex Stop-hook JSON payload from stdin, runs the
posthook pipeline, and exits 0 unconditionally — the Stop hook must
never break the host.
"""

from __future__ import annotations


DESCRIPTION = "Stop-hook: forward the assistant's last turn to Telegram."

USAGE = """\
Usage: metasphere hooks posthook [--dry-run]

Stop-hook entrypoint. Wired into the configured agent REPL. Reads the Stop-hook JSON
payload from stdin, applies the posthook pipeline (silent-tick
filter, identifier scrub, chunking, send), and exits 0 unconditionally
— the Stop hook must never break the host process.

Options:
  --dry-run         Parse stdin and print a JSON plan to stdout (chat
                    id, text length, chunk count, would_send). No
                    telegram send, no state-file write. This is the
                    contract e2e rigs depend on.

Always exits 0.
"""

import argparse
import json
import sys
from metasphere.paths import resolve
from metasphere.posthook import (
    _matrix_pin,
    _resolve_chat_id,
    extract_stop_assistant_text,
    read_stop_hook_payload,
    run_posthook,
    should_skip_silent_tick,
)
from metasphere.identity import resolve_agent_id


def _dry_run(stdin_bytes: bytes) -> int:
    paths = resolve()
    payload = read_stop_hook_payload(stdin_bytes)
    agent = resolve_agent_id(paths)

    text = extract_stop_assistant_text(payload) if isinstance(payload, dict) else None

    would_skip = should_skip_silent_tick(text)
    text_str = text or ""
    # Telegram hard-limits messages to 4096 chars; our chunker targets
    # ~4000 to leave room for formatting. Dry-run matches that estimate.
    chunk_size = 4000
    chunk_count = 0 if not text_str else (len(text_str) + chunk_size - 1) // chunk_size
    chat_id = _resolve_chat_id(paths)
    mx = _matrix_pin(paths)

    summary = {
        "agent": agent,
        "stop_hook_active": bool(payload.get("stop_hook_active")) if isinstance(payload, dict) else False,
        "chat_id": mx[1] if mx else chat_id,
        "surface": mx[0] if mx else "telegram",
        "text_length": len(text_str),
        "chunk_count": chunk_count,
        "would_send": (
            not would_skip
            and agent == "@orchestrator"
            and chat_id is not None
            and not (isinstance(payload, dict) and payload.get("stop_hook_active"))
        ),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    try:
        stdin_bytes = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    except Exception:  # noqa: BLE001
        stdin_bytes = b""
    if "--dry-run" in args:
        try:
            return _dry_run(stdin_bytes)
        except Exception:  # noqa: BLE001 — dry-run must also never break
            return 0
    return run_posthook(stdin_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
