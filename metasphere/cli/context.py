"""Stdout-only emitter for the per-turn context block.

Wired into claude-code as the UserPromptSubmit hook. In addition to
printing the context block to stdout, this entry point writes a
per-turn success breadcrumb so the Stop posthook can fail-closed when
the context build crashed. Reads session_id and transcript_path from
the claude-code hook payload on stdin.
"""

from __future__ import annotations


DESCRIPTION = "UserPromptSubmit hook: emit the per-turn context block."

USAGE = """\
Usage: metasphere hooks context

UserPromptSubmit hook entrypoint. Wired into Claude Code via the
~/.metasphere/.claude/settings.local.json hooks block. Not invoked
directly by humans except for debugging.

Reads a JSON hook payload (session_id, transcript_path) from stdin
and writes a per-turn success breadcrumb so the Stop posthook can
fail-closed when context construction crashes.

Output: the rendered per-turn context block on stdout, exit code 0.
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from metasphere import breadcrumbs as _bc
from metasphere.context import build_context
from metasphere.identity import resolve_agent_id
from metasphere.paths import resolve


def _paths_for_hook_cwd(paths, cwd_value: object, *, registered_only: bool = False):
    """Resolve project/scope from the hook's cwd, not daemon-wide env.

    Direct user-level Codex hooks set ``registered_only`` so an arbitrary
    repository cannot become a trusted context root merely by supplying its
    cwd.  Managed sessions retain the Git-root fallback used by legacy launch
    paths.
    """
    if not isinstance(cwd_value, str) or not cwd_value.strip():
        if registered_only:
            return replace(paths, project_root=paths.root, scope=paths.root)
        return paths
    cwd = Path(cwd_value).expanduser().resolve()
    project_root = None
    try:
        from metasphere.project import list_projects
        matches = []
        for project in list_projects(paths=paths):
            root = Path(project.path).expanduser().resolve()
            try:
                cwd.relative_to(root)
            except ValueError:
                continue
            matches.append(root)
        if matches:
            project_root = max(matches, key=lambda path: len(path.parts))
    except Exception:  # noqa: BLE001 - hook context remains best effort
        pass
    if project_root is None and not registered_only:
        try:
            output = subprocess.check_output(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if output:
                project_root = Path(output).resolve()
        except (OSError, subprocess.SubprocessError):
            pass
    if project_root is None and registered_only:
        # Keep direct hooks on operator-controlled storage when cwd is not a
        # registered project.  The restricted builder will emit global persona
        # only and skip project memory in this case.
        return replace(paths, project_root=paths.root, scope=paths.root)
    return replace(paths, project_root=project_root or paths.project_root, scope=cwd)


def _is_managed_session() -> bool:
    """True iff this hook fired inside a metasphere-managed agent session.

    Every managed agent — the gateway tmux respawn loop AND the headless
    ``claude -p`` one-shots (ephemeral spawns in ``metasphere/agents.py``,
    the heartbeat fallback in ``metasphere/heartbeat.py``) — exports
    ``METASPHERE_GATEWAY_SESSION=1``. An interactive Claude Code session a
    human opens by hand (web UI or terminal) for dev work sets nothing.

    This is the same contract the PreToolUse deny hook already relies on
    (``metasphere/cli/pretool.py``): unset ⇒ interactive human session.
    """
    return bool(os.environ.get("METASPHERE_GATEWAY_SESSION"))


def _parse_payload(stdin_bytes: bytes) -> dict:
    if not stdin_bytes:
        return {}
    try:
        obj = json.loads(stdin_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("--help", "-h"):
        sys.stdout.write(USAGE)
        return 0

    # Read the provider payload before applying the managed-session policy:
    # direct Codex sessions are supported consumers of context, while direct
    # Claude sessions retain the issue-150 isolation behavior.
    try:
        stdin_bytes = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    except Exception:  # noqa: BLE001
        stdin_bytes = b""
    payload = _parse_payload(stdin_bytes)
    is_codex = payload.get("turn_id") is not None
    managed = _is_managed_session()

    # An interactive Claude Code session a human opened by hand (web UI or
    # terminal) for dev work is NOT a managed agent. Injecting the
    # orchestrator persona/context here spins up a second @orchestrator
    # that competes with the gateway poller for Telegram getUpdates
    # ("Conflict: terminated by other getUpdates") and accumulates state
    # as @orchestrator rather than a neutral coding assistant (issue #150).
    # Emit nothing and skip the managed-agent bookkeeping (breadcrumb +
    # liveness touch): there is no supervisor reaping this pane, and the
    # Stop posthook must not relay its replies to Telegram. Every managed
    # agent (gateway tmux AND headless ``claude -p``) sets the env marker,
    # so this only ever short-circuits a genuine interactive session.
    if not managed and not is_codex:
        return 0
    session_id = str(payload.get("session_id") or "")
    transcript_path = payload.get("transcript_path") or ""
    # The user's actual prompt for this turn. Threaded into the context
    # build so memory recall is scored against what was just asked, not
    # only stale ambient state (task file + project name + last event).
    # Empty on manual/heartbeat invocations — recall then falls back to
    # the ambient stem, preserving prior behavior.
    prompt = str(payload.get("prompt") or "")

    paths = _paths_for_hook_cwd(
        resolve(), payload.get("cwd"), registered_only=not managed
    )
    agent = resolve_agent_id(paths)
    user_msg_count = _bc.count_user_messages(transcript_path) if transcript_path else 0

    # UserPromptSubmit is one of the four hook signals reap_dormant
    # uses to decide an agent is alive. Touch BEFORE the context build
    # so even if the build crashes the supervisor still sees input
    # arrived. Best-effort by contract — touch_last_active swallows.
    if managed:
        from metasphere.agents import touch_last_active
        touch_last_active(agent, paths)

    try:
        block = build_context(
            paths,
            prompt=prompt,
            read_only=not managed,
            profile="managed" if managed else "direct-codex",
        )
        # Codex supports plain stdout for UserPromptSubmit, but the structured
        # form makes the event and trust boundary explicit and safely JSON-
        # escapes arbitrary memory/persona text. ``turn_id`` is a Codex-only
        # field, so it is a reliable discriminator that preserves Claude's
        # existing plain-text hook behavior byte-for-byte.
        if payload.get("turn_id") is not None:
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": block,
                }
            }))
        else:
            sys.stdout.write(block)
    except Exception as exc:  # noqa: BLE001 — context build must not crash the host
        # Write the FAILED breadcrumb so the posthook fail-closes this
        # turn. We deliberately do NOT re-raise: the UserPromptSubmit
        # hook is best-effort, and crashing it would break the user's
        # ability to interact with the agent at all.
        if managed and session_id:
            _bc.write_breadcrumb(
                paths,
                session_id=session_id,
                status=_bc.STATUS_FAILED,
                user_msg_count=user_msg_count,
                agent=agent,
                reason=f"{type(exc).__name__}: {exc}"[:200],
            )
        # Emit a minimal context fragment so the agent at least gets
        # *something*; the failed breadcrumb ensures the posthook
        # suppresses the resulting reply from Telegram.
        try:
            sys.stdout.write(
                "## Metasphere context build failed\n"
                f"_({type(exc).__name__})_\n"
            )
        except Exception:  # noqa: BLE001
            pass
        return 0

    # Happy path: stamp success and opportunistically prune old entries.
    if managed and session_id:
        _bc.write_breadcrumb(
            paths,
            session_id=session_id,
            status=_bc.STATUS_SUCCESS,
            user_msg_count=user_msg_count,
            agent=agent,
        )
        try:
            _bc.prune_old_breadcrumbs(paths)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
