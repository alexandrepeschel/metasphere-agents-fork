"""Tests for metasphere.cli.context — UserPromptSubmit hook entry point.

Verifies the breadcrumb writer behavior end-to-end:
- success path writes a SUCCESS breadcrumb keyed by session_id with
  the correct user-message count
- a context-build exception writes a FAILED breadcrumb (best-effort)
  and still exits 0 so the host turn isn't broken
- absent stdin (manual invocation) is a no-op for breadcrumb writes
- an interactive (non-managed) session injects nothing and writes no
  breadcrumb (issue #150) — every managed agent sets the env marker
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metasphere import breadcrumbs as _bc
from metasphere.cli import context as cli_context
from metasphere import context as context_builder
from metasphere import update as _update
from metasphere.paths import Paths


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class _FakeStdin:
    """Stand-in for sys.stdin that yields a fixed payload."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = type("B", (), {"read": lambda self_: payload})()

    def isatty(self) -> bool:  # noqa: D401
        return False


def _payload(transcript: Path, session_id: str) -> bytes:
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(transcript),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "a user prompt",
            "cwd": str(transcript.parent),
        }
    ).encode("utf-8")


def test_cli_context_writes_success_breadcrumb(tmp_paths: Paths, monkeypatch, capsys):
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")  # managed agent session
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(transcript, [{"type": "user"}, {"type": "user"}])  # 2 user msgs

    payload = _payload(transcript, session_id="cli-success")
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))

    rc = cli_context.main([])
    assert rc == 0

    bc = _bc.read_breadcrumb(tmp_paths, "cli-success")
    assert bc is not None
    assert bc["status"] == _bc.STATUS_SUCCESS
    assert bc["session_id"] == "cli-success"
    assert bc["user_msg_count"] == 2
    assert bc["agent"] == "@orchestrator"

    # build_context emitted to stdout — at minimum the status header.
    out = capsys.readouterr().out
    assert "@orchestrator" in out
    # Claude payloads have no Codex-only turn_id and retain plain stdout.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_context_writes_failed_breadcrumb_on_exception(tmp_paths: Paths, monkeypatch):
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")  # managed agent session
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])

    payload = _payload(transcript, session_id="cli-failed")
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))

    with mock.patch("metasphere.cli.context.build_context", side_effect=RuntimeError("boom")):
        rc = cli_context.main([])
    assert rc == 0  # never crash the host

    bc = _bc.read_breadcrumb(tmp_paths, "cli-failed")
    assert bc is not None
    assert bc["status"] == _bc.STATUS_FAILED
    assert bc["session_id"] == "cli-failed"
    assert bc["user_msg_count"] == 1
    assert "RuntimeError" in (bc.get("reason") or "")


def test_cli_context_threads_prompt_into_build_context(tmp_paths: Paths, monkeypatch):
    """The user's prompt is extracted from the hook payload and passed to
    build_context so memory recall can score against it (fixes the query
    that previously never saw the prompt)."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")  # managed agent session
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])

    payload = _payload(transcript, session_id="cli-prompt")  # prompt="a user prompt"
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))

    with mock.patch(
        "metasphere.cli.context.build_context", return_value="## ctx\n"
    ) as bc_mock:
        rc = cli_context.main([])
    assert rc == 0
    assert bc_mock.call_args.kwargs.get("prompt") == "a user prompt"


def test_cli_context_codex_contract_uses_structured_additional_context(
    tmp_paths: Paths, monkeypatch, capsys
):
    """Codex turn_id selects the official UserPromptSubmit JSON contract."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@project-agent")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")
    transcript = tmp_paths.root / "codex.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])
    event = json.loads(_payload(transcript, "codex-turn"))
    event["turn_id"] = "turn-123"
    event["prompt"] = "What did we decide about Recurse?"
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(event).encode()))

    with mock.patch(
        "metasphere.cli.context.build_context", return_value="## Current Project: Recurse\n"
    ) as build:
        assert cli_context.main([]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Current Project: Recurse" in output["hookSpecificOutput"]["additionalContext"]
    assert build.call_args.kwargs["prompt"] == "What did we decide about Recurse?"


def test_codex_installed_limit_keeps_recurse_inline(
    tmp_paths: Paths, tmp_path: Path, monkeypatch, capsys
):
    """Exercise real context output against the installed Codex transport cap."""
    home = tmp_path / "operator-home"
    home.mkdir()
    assert _update._sync_codex_hooks(tmp_paths, home) == 1
    config = json.loads((home / ".codex" / "hooks.json").read_text())
    handler = config["hooks"]["UserPromptSubmit"][-1]["hooks"][0]
    limit = handler["additionalContextLimit"]

    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")
    agent = tmp_paths.agent_dir("@orchestrator")
    agent.mkdir(parents=True)
    (agent / "SOUL.md").write_text("# soul\n\n" + ("voice detail\n" * 500), encoding="utf-8")
    (agent / "IDENTITY.md").write_text("# identity\n\nSpot identity.\n", encoding="utf-8")
    (agent / "USER.md").write_text("# user\n\nCurrent Project: Recurse\n", encoding="utf-8")
    transcript = tmp_paths.root / "orchestrator-codex.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])
    event = json.loads(_payload(transcript, "orchestrator-codex"))
    event.update({"turn_id": "turn-orchestrator", "prompt": "What is the current project?"})
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(event).encode()))
    monkeypatch.setattr(context_builder, "_render_memory_fts", lambda *a, **k: "")

    assert cli_context.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "Current Project: Recurse" in additional
    # Codex documents this setting as an approximate token limit. A strict
    # 4-byte-per-token bound proves this representative output remains inline.
    assert len(additional.encode("utf-8")) < limit * 4


def test_cli_context_interactive_session_skips_everything(tmp_paths: Paths, monkeypatch, capsys):
    """An interactive Claude Code session a human opened by hand (no
    METASPHERE_GATEWAY_SESSION marker) must NOT get the orchestrator
    persona injected — that spins up a second orchestrator that fights
    the gateway poller for Telegram getUpdates (issue #150). The hook
    emits nothing, writes no breadcrumb, and never calls build_context.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.delenv("METASPHERE_GATEWAY_SESSION", raising=False)  # interactive
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])

    payload = _payload(transcript, session_id="cli-interactive")
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))

    with mock.patch("metasphere.cli.context.build_context") as bc_mock:
        rc = cli_context.main([])
    assert rc == 0
    bc_mock.assert_not_called()  # no persona/context injection

    # Nothing emitted to stdout, and no breadcrumb (no managed bookkeeping).
    assert capsys.readouterr().out == ""
    assert _bc.read_breadcrumb(tmp_paths, "cli-interactive") is None


def test_cli_context_managed_marker_enables_injection(tmp_paths: Paths, monkeypatch):
    """With the managed marker set, the same payload that the interactive
    test skips now flows through build_context and writes a breadcrumb —
    proving the gate keys on the marker, not the payload."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])

    payload = _payload(transcript, session_id="cli-managed")
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))

    with mock.patch(
        "metasphere.cli.context.build_context", return_value="## ctx\n"
    ) as bc_mock:
        rc = cli_context.main([])
    assert rc == 0
    bc_mock.assert_called_once()
    assert _bc.read_breadcrumb(tmp_paths, "cli-managed") is not None


def test_cli_context_no_stdin_skips_breadcrumb(tmp_paths: Paths, monkeypatch):
    """Manual invocation from a shell (no JSON on stdin) is allowed —
    we just don't write a breadcrumb, and the posthook will fail-closed
    for that session, which is the correct default.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")  # managed agent session
    monkeypatch.setattr("sys.stdin", _FakeStdin(b""))
    rc = cli_context.main([])
    assert rc == 0
    # No breadcrumbs dir created (or empty if it was).
    bdir = _bc.breadcrumbs_dir(tmp_paths)
    assert not bdir.exists() or not list(bdir.iterdir())
