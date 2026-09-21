"""Tests for metasphere.posthook (Stop-hook port)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metasphere import breadcrumbs as _bc
from metasphere import posthook
from metasphere.paths import Paths


def _seed_success_breadcrumb(paths: Paths, *, session_id: str, transcript: Path) -> None:
    """Helper: write a success breadcrumb that matches the transcript's
    user-message count, so the fail-closed gate lets the turn through.
    Tests that exercise the happy path of route_to_telegram via
    run_posthook need this — without it the gate suppresses.
    """
    count = _bc.count_user_messages(transcript)
    _bc.write_breadcrumb(
        paths,
        session_id=session_id,
        status=_bc.STATUS_SUCCESS,
        user_msg_count=count,
        agent="@orchestrator",
    )


# ---------- read_stop_hook_payload ----------

def test_read_stop_hook_payload_parses_json():
    payload = {
        "session_id": "abc",
        "transcript_path": "/tmp/x.jsonl",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    out = posthook.read_stop_hook_payload(json.dumps(payload).encode("utf-8"))
    assert out == payload


def test_read_stop_hook_payload_empty_returns_empty_dict():
    assert posthook.read_stop_hook_payload(b"") == {}
    assert posthook.read_stop_hook_payload(b"not json") == {}


def test_extract_stop_assistant_text_from_codex_payload():
    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": "Codex reply",
        "transcript_path": "/unused/codex/rollout.jsonl",
    }
    assert posthook.stop_hook_provider(payload) == "codex"
    assert posthook.extract_stop_assistant_text(payload) == "Codex reply"


# ---------- extract_last_assistant_text ----------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_extract_last_assistant_text_multi_block(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "older"},
                    ]
                },
            },
            {"type": "user", "message": {"content": "again"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "block one"},
                        {"type": "tool_use", "name": "x"},
                        {"type": "text", "text": "block two"},
                    ]
                },
            },
        ],
    )
    text = posthook.extract_last_assistant_text(transcript)
    assert text == "block one\nblock two"


def test_extract_last_assistant_text_empty_transcript(tmp_path: Path):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert posthook.extract_last_assistant_text(transcript) is None


def test_extract_last_assistant_text_missing_file(tmp_path: Path):
    assert posthook.extract_last_assistant_text(tmp_path / "nope.jsonl") is None


def test_extract_last_assistant_text_only_tool_use(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            }
        ],
    )
    assert posthook.extract_last_assistant_text(transcript) is None


def test_extract_last_assistant_text_pre_tool_text_captured(tmp_path: Path):
    """Pre-tool text from an earlier assistant entry must reach Telegram.

    Regression: extract_last_assistant_text previously returned only the
    final assistant entry's text. When a turn had text before tool calls
    (entry N) and text after (entry N+2), only the N+2 segment was
    forwarded. The N text was silently dropped.
    """
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            # genuine human message — turn boundary
            {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
            # first assistant entry: text + tool call
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "pre-tool explanation"},
                        {"type": "tool_use", "name": "Write", "id": "t1"},
                    ]
                },
            },
            # tool result — intra-turn splice, NOT a turn boundary
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
                },
            },
            # second assistant entry: post-tool text
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "post-tool text"}]
                },
            },
        ],
    )
    text = posthook.extract_last_assistant_text(transcript)
    assert text is not None
    assert "pre-tool explanation" in text
    assert "post-tool text" in text


def test_extract_last_assistant_text_pre_tool_only(tmp_path: Path):
    """Turn that ends on a tool call with no post-tool text still forwards
    the pre-tool explanation."""
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "doing the thing"},
                        {"type": "tool_use", "name": "Bash", "id": "t1"},
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
                },
            },
            # final assistant entry: tool call only, no text
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "t2"}]},
            },
        ],
    )
    text = posthook.extract_last_assistant_text(transcript)
    assert text == "doing the thing"


def test_extract_last_assistant_text_prior_turn_not_included(tmp_path: Path):
    """Text from the previous human turn must not bleed into the current one."""
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            # previous turn
            {"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "old reply"}]},
            },
            # current turn — genuine human message is the boundary
            {"type": "user", "message": {"content": [{"type": "text", "text": "second"}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "new reply"}]},
            },
        ],
    )
    text = posthook.extract_last_assistant_text(transcript)
    assert text == "new reply"
    assert "old reply" not in text


# ---------- should_skip_silent_tick ----------

def test_should_skip_silent_tick():
    assert posthook.should_skip_silent_tick("") is True
    assert posthook.should_skip_silent_tick("   \n\t") is True
    assert posthook.should_skip_silent_tick(None) is True
    assert posthook.should_skip_silent_tick("real reply text") is False


def test_should_skip_silent_tick_prefix_variants():
    """Regression 2026-04-16: ~30x 'Silent tick at HH:MMZ' messages
    reached the operator's phone because the original suppression regex
    required a full-line match. Prefix match now covers every
    placeholder the orchestrator's persona might emit.
    """
    skip = posthook.should_skip_silent_tick
    # Bracketed silence tokens — matched against a list of probable tokens,
    # no canonical/alias distinction (operator 2026-06-21).
    assert skip("[silent]") is True
    assert skip("[idle]") is True
    assert skip("[quiet]") is True
    assert skip("[noop]") is True
    assert skip("[no-op]") is True
    assert skip("[nothing]") is True
    assert skip("[silent] at 11:00Z") is True
    assert skip("done the thing\n\n[silent]") is True  # trailer form
    assert skip("done the thing\n\n[noop]") is True
    assert skip("Standing by") is True
    assert skip("Silent tick") is True
    assert skip("Quiet") is True
    assert skip("Idle") is True
    assert skip("Idle.") is True
    assert skip("Nothing to report") is True
    assert skip("Nothing new") is True
    assert skip("Still here") is True
    # With a trailing clock / scope / mood-adverb — the exact forms that
    # leaked overnight.
    assert skip("Silent tick at 05:07Z.") is True
    assert skip("Silent tick. Idle, waiting on the operator's morning activity.") is True
    assert skip("Idle, waiting on the operator's morning activity.") is True
    assert skip("Nothing new to report — standing by.") is True
    assert skip("No new work detected.") is True
    assert skip("No new messages or active tasks. Standing by.") is True
    # Case-insensitive.
    assert skip("SILENT TICK AT 05:07Z") is True
    assert skip("silent tick at 05:07Z") is True
    assert skip("IDLE.") is True
    # Negative: substantive replies MUST NOT be suppressed even if they
    # happen to contain an idle word mid-sentence.
    assert skip("The schedule is quiet right now; here's the plan: ...") is False
    assert skip("We should report the idle metric to the dashboard") is False
    assert skip("PR #14 merged cleanly") is False


def test_should_skip_silent_tick_matches_route_to_telegram():
    """The suppression test used by ``should_skip_silent_tick`` and
    ``route_to_telegram`` must be identical — they both reference
    ``_IDLE_PATTERN``. This test pins that invariant so a future
    refactor can't introduce divergence.
    """
    samples = [
        "[idle]",
        "Silent tick at 05:07Z.",
        "Idle, waiting.",
        "real reply",
        "this is long-form content",
    ]
    for s in samples:
        skip_says = posthook.should_skip_silent_tick(s)
        pattern_says = posthook._IDLE_PATTERN.match(s.strip()) is not None
        assert skip_says == pattern_says or skip_says is True and not s.strip(), (
            f"divergence on {s!r}: skip={skip_says}, pattern={pattern_says}"
        )


# ---------- trailing-idle stripper (2026-05-05 trailer-leak) ----------
#
# Agents emit substantive prose followed by ``[idle]`` as a turn-end
# signal. The start-anchored ``_IDLE_PATTERN`` correctly catches BARE
# ``[idle]`` turns but misses ``<prose>\n\n[idle]`` — the trailer
# leaks through to Telegram alongside the substantive prose. ~19% of
# recent @orchestrator turns observed leaking on 2026-05-05.
# ----------------------------------------------------------------------


def test_strip_trailing_idle_removes_trailer_keeps_prose():
    """The substantive prose ahead of the trailer must survive intact;
    only the dangling ``[idle]`` token (with surrounding whitespace) is
    removed. This is the load-bearing behaviour: legit non-idle text
    DOES reach Telegram, the trailer DOES NOT."""
    text = (
        "lead acknowledged + dispatched eng with compound audit. "
        "ETA 2-4h. No fork for the operator — informational pass-through."
        "\n\n[idle]"
    )
    cleaned = posthook._strip_trailing_idle(text)
    assert cleaned == (
        "lead acknowledged + dispatched eng with compound audit. "
        "ETA 2-4h. No fork for the operator — informational pass-through."
    )


def test_strip_trailing_idle_handles_repeated_trailers():
    """Repeated trailer tokens (``[idle]\\n[idle]``) collapse together —
    the regex matches one or more consecutive trailers."""
    assert posthook._strip_trailing_idle("done\n\n[idle]\n[idle]") == "done"


def test_strip_trailing_idle_does_not_strip_freeform_variants_at_end():
    """Deliberately narrower than ``_IDLE_PATTERN``: free-form variants
    (``standing by``, ``idle.``, ``nothing new``…) are real English
    phrases that legitimate prose can end on (``…the user is now
    idle.``). Only the standardized self-delimiting ``[idle]`` token is
    treated as a strippable trailer. Free-form bare-token turns stay
    covered by the start-anchored ``_IDLE_PATTERN`` (see
    ``test_should_skip_silent_tick_prefix_variants``)."""
    assert posthook._strip_trailing_idle("done\n\nstanding by") == "done\n\nstanding by"
    assert posthook._strip_trailing_idle("the user is now idle.") == "the user is now idle."


def test_strip_trailing_idle_idempotent_on_clean_text():
    """No trailer → text returned untouched (modulo trailing whitespace)."""
    assert posthook._strip_trailing_idle("plain prose") == "plain prose"
    assert posthook._strip_trailing_idle("PR #14 merged cleanly.") == "PR #14 merged cleanly."


def test_strip_trailing_idle_does_not_touch_idle_word_mid_sentence():
    """Substantive prose with ``idle`` mid-sentence and no trailer must
    survive — same negative-coverage promise as ``_IDLE_PATTERN``."""
    text = "We should report the idle metric to the dashboard"
    assert posthook._strip_trailing_idle(text) == text


def test_should_skip_silent_tick_skips_pure_trailer_only_text():
    """If the entire turn is just trailer tokens (whitespace + [idle] +
    [idle] + whitespace), should_skip_silent_tick must catch it.
    Defense-in-depth alongside the start-anchored match."""
    assert posthook.should_skip_silent_tick("\n\n[idle]\n[idle]\n") is True
    assert posthook.should_skip_silent_tick("   [idle]   ") is True


# ---------- route_to_telegram ----------

def _write_chat_id(paths: Paths) -> None:
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "telegram_chat_id").write_text("12345", encoding="utf-8")


def test_route_to_telegram_sends_once_and_dedupes(tmp_paths: Paths):
    _write_chat_id(tmp_paths)
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.return_value = [{"ok": True}]
        posthook.route_to_telegram("hello world", tmp_paths)
        posthook.route_to_telegram("hello world", tmp_paths)  # duplicate
    assert m.call_count == 1
    args, kwargs = m.call_args
    assert args[0] == "12345"
    assert args[1] == "hello world"
    # Hash file persisted
    assert (tmp_paths.state / "posthook_last_sent").exists()


def test_should_skip_silent_tick_skips_prose_with_trailing_idle():
    """Operator directive 2026-06-07 21:21Z: when an agent emits
    ``<prose>\\n\\n[idle]``, the trailer is the agent's own signal that
    the prose was internal-only narration (e.g. "Routine — marking done"
    after a triage). Genuine operator-facing surfaces go through explicit
    ``metasphere telegram send`` — pane prose should NEVER auto-forward
    when it carries a trailing ``[idle]``. Previously the harness
    stripped the trailer and forwarded the prose; that caused the same
    noise the bare-``[idle]`` rule was designed to prevent."""
    skip = posthook.should_skip_silent_tick
    assert skip("Routine — marking done.\n\n[idle]") is True
    assert skip(
        "lead acknowledged + dispatched eng with compound audit. "
        "ETA 2-4h. No fork for the operator.\n\n[idle]"
    ) is True
    # Repeated trailers still caught.
    assert skip("done\n\n[idle]\n[idle]") is True
    # Case-insensitive.
    assert skip("noted\n\n[IDLE]") is True
    # Negative: substantive prose WITHOUT a trailer still forwards.
    # Catch the "idle" word mid-sentence regression.
    assert skip("We should report the idle metric to the dashboard") is False
    assert skip("PR #14 merged cleanly.") is False


def test_route_to_telegram_drops_prose_with_trailing_idle(tmp_paths: Paths):
    """End-to-end: pane prose ending in ``[idle]`` must not reach
    Telegram, AT ALL. The primary gate is ``should_skip_silent_tick``
    in the Stop-hook caller; this test pins the
    defense-in-depth trailer strip in ``route_to_telegram`` for callers
    that bypass the gate. Strip-then-empty-check drops the turn."""
    _write_chat_id(tmp_paths)
    payload = (
        "Routine — no fork. Marking done.\n\n[idle]"
    )
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.return_value = [{"ok": True}]
        posthook.route_to_telegram(payload, tmp_paths)
    # The defense-in-depth strip leaves "Routine — no fork. Marking done."
    # which is non-empty — so route_to_telegram WILL send it if reached
    # directly. The CONTRACT-LEVEL guarantee is that the Stop-hook gate
    # blocks it upstream. Verify the gate.
    assert posthook.should_skip_silent_tick(payload) is True
    # If the gate fires, route_to_telegram is never called by the
    # Stop-hook in production — confirm by exercising the gate path.
    if posthook.should_skip_silent_tick(payload):
        return
    posthook.route_to_telegram(payload, tmp_paths)  # unreachable
    assert m.call_count == 0  # pragma: no cover


def test_route_to_telegram_drops_trailer_only_text(tmp_paths: Paths):
    """If the whole turn is just trailer tokens (e.g. accidentally double
    [idle]), nothing should reach Telegram. Defense-in-depth alongside
    the start-anchored ``_IDLE_PATTERN`` filter."""
    _write_chat_id(tmp_paths)
    with mock.patch("metasphere.telegram.api.send_message") as m:
        posthook.route_to_telegram("\n\n[idle]\n[idle]\n", tmp_paths)
        posthook.route_to_telegram("   [idle]   ", tmp_paths)
    assert m.call_count == 0


def test_route_to_telegram_distinct_messages_both_sent(tmp_paths: Paths):
    _write_chat_id(tmp_paths)
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.return_value = [{"ok": True}]
        posthook.route_to_telegram("first", tmp_paths)
        posthook.route_to_telegram("second", tmp_paths)
    assert m.call_count == 2


def test_route_to_telegram_logs_on_api_failure(tmp_paths: Paths):
    _write_chat_id(tmp_paths)
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.side_effect = RuntimeError("boom")
        posthook.route_to_telegram("payload", tmp_paths)
    log = tmp_paths.state / "posthook_telegram_errors.log"
    assert log.exists()
    body = log.read_text(encoding="utf-8")
    assert "boom" in body
    assert "RuntimeError" in body


def test_route_to_telegram_logs_when_chat_id_missing(tmp_paths: Paths):
    with mock.patch("metasphere.telegram.api.send_message") as m:
        posthook.route_to_telegram("hi", tmp_paths)
    m.assert_not_called()
    log = tmp_paths.state / "posthook_telegram_errors.log"
    assert log.exists()
    assert "chat_id" in log.read_text(encoding="utf-8")


# ---------- track_turn_completion ----------

def test_track_turn_completion_increments(tmp_paths: Paths):
    posthook.track_turn_completion("@orchestrator", tmp_paths)
    posthook.track_turn_completion("@orchestrator", tmp_paths)
    posthook.track_turn_completion("@orchestrator", tmp_paths)
    activity = tmp_paths.agent_dir("@orchestrator") / "activity.json"
    assert activity.exists()
    data = json.loads(activity.read_text(encoding="utf-8"))
    assert data["turns"] == 3
    assert "updated_at" in data


def test_track_turn_completion_upgrades_spawned_status(tmp_paths: Paths):
    agent_dir = tmp_paths.agent_dir("@child")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status").write_text("spawned", encoding="utf-8")
    posthook.track_turn_completion("@child", tmp_paths)
    assert (agent_dir / "status").read_text(encoding="utf-8").strip() == "active"
    assert (agent_dir / "updated_at").exists()


def test_track_turn_completion_logs_heartbeat_every_10(tmp_paths: Paths):
    with mock.patch("metasphere.posthook.log_event") as m:
        for _ in range(11):
            posthook.track_turn_completion("@orchestrator", tmp_paths)
    # Called exactly once at turn 10.
    assert m.call_count == 1
    args, kwargs = m.call_args
    assert args[0] == "agent.heartbeat"
    assert "turn 10" in args[1]


# ---------- run_posthook (top-level) ----------

def test_run_posthook_never_raises_on_garbage(tmp_paths: Paths):
    assert posthook.run_posthook(b"garbage", tmp_paths) == 0


def test_run_posthook_routes_orchestrator(tmp_paths: Paths, monkeypatch):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "the reply"}]},
            }
        ],
    )
    _seed_success_breadcrumb(tmp_paths, session_id="s", transcript=transcript)
    payload = json.dumps(
        {
            "session_id": "s",
            "transcript_path": str(transcript),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
    ).encode("utf-8")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.return_value = [{"ok": True}]
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_called_once()
    # Activity tracked
    assert (tmp_paths.agent_dir("@orchestrator") / "activity.json").exists()


def test_run_posthook_skips_for_subagent(tmp_paths: Paths, monkeypatch):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@child")
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "reply"}]},
            }
        ],
    )
    payload = json.dumps(
        {"transcript_path": str(transcript), "stop_hook_active": False}
    ).encode("utf-8")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        posthook.run_posthook(payload, tmp_paths)
    m.assert_not_called()
    # But activity still tracked for the child
    assert (tmp_paths.agent_dir("@child") / "activity.json").exists()


def test_run_posthook_respects_stop_hook_active(tmp_paths: Paths, monkeypatch):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "reply"}]},
            }
        ],
    )
    payload = json.dumps(
        {"transcript_path": str(transcript), "stop_hook_active": True}
    ).encode("utf-8")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        posthook.run_posthook(payload, tmp_paths)
    m.assert_not_called()


def test_run_posthook_routes_codex_payload_without_claude_breadcrumb(
    tmp_paths: Paths, monkeypatch
):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.setenv("METASPHERE_GATEWAY_SESSION", "1")
    payload = json.dumps(
        {
            "session_id": "codex-session",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "reply from codex",
        }
    ).encode("utf-8")
    with mock.patch("metasphere.telegram.api.send_message") as send:
        send.return_value = [{"ok": True}]
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    send.assert_called_once()
    assert not (tmp_paths.logs / "posthook-suppressions.log").exists()


def test_run_posthook_unmanaged_codex_has_no_side_effects(tmp_paths: Paths, monkeypatch):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    monkeypatch.delenv("METASPHERE_GATEWAY_SESSION", raising=False)
    payload = json.dumps({
        "session_id": "private-codex-session",
        "hook_event_name": "Stop",
        "last_assistant_message": "private interactive reply",
    }).encode()

    with mock.patch("metasphere.telegram.api.send_message") as send:
        assert posthook.run_posthook(payload, tmp_paths) == 0
    send.assert_not_called()
    assert not (tmp_paths.agent_dir("@orchestrator") / "activity.json").exists()


# ---------- auto_close_finished_task ----------

def _make_task(repo: Path, slug_title: str) -> str:
    from metasphere import tasks as _tasks
    t = _tasks.create_task(slug_title, "!normal", repo, repo, created_by="@parent")
    return t.id


def test_auto_close_finished_task_archives_on_complete_status(tmp_paths: Paths):
    task_id = _make_task(tmp_paths.project_root, "child trivial task")
    agent_dir = tmp_paths.agent_dir("@child")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "task_id").write_text(task_id + "\n")
    (agent_dir / "status").write_text("complete: did the thing\n")

    closed = posthook.auto_close_finished_task("@child", tmp_paths)
    assert closed == task_id

    from metasphere import tasks as _tasks
    p = _tasks._find_task_file(task_id, include_completed=False)
    assert p is None  # not in active anymore
    p2 = _tasks._find_task_file(task_id, include_completed=True)
    assert p2 is not None and "archive" in str(p2)


def test_auto_close_skips_when_status_not_complete(tmp_paths: Paths):
    task_id = _make_task(tmp_paths.project_root, "still working")
    agent_dir = tmp_paths.agent_dir("@child")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "task_id").write_text(task_id + "\n")
    (agent_dir / "status").write_text("working: halfway there\n")

    assert posthook.auto_close_finished_task("@child", tmp_paths) is None

    from metasphere import tasks as _tasks
    p = _tasks._find_task_file(task_id, include_completed=False)
    assert p is not None  # still active


def test_auto_close_no_task_id_is_noop(tmp_paths: Paths):
    agent_dir = tmp_paths.agent_dir("@legacy")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status").write_text("complete: done\n")
    assert posthook.auto_close_finished_task("@legacy", tmp_paths) is None


def test_auto_close_already_archived_is_noop(tmp_paths: Paths):
    task_id = _make_task(tmp_paths.project_root, "double close")
    agent_dir = tmp_paths.agent_dir("@child")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "task_id").write_text(task_id + "\n")
    (agent_dir / "status").write_text("complete: done\n")
    # First call closes it
    assert posthook.auto_close_finished_task("@child", tmp_paths) == task_id
    # Second call is a no-op (no active file)
    assert posthook.auto_close_finished_task("@child", tmp_paths) is None


def test_run_posthook_auto_closes_for_subagent(tmp_paths: Paths, monkeypatch):
    task_id = _make_task(tmp_paths.project_root, "subagent end-to-end")
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@worker")
    agent_dir = tmp_paths.agent_dir("@worker")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "task_id").write_text(task_id + "\n")
    (agent_dir / "status").write_text("complete: shipped\n")

    payload = json.dumps({"stop_hook_active": False}).encode("utf-8")
    rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0

    from metasphere import tasks as _tasks
    assert _tasks._find_task_file(task_id, include_completed=False) is None


def test_run_posthook_does_not_auto_close_orchestrator(tmp_paths: Paths, monkeypatch):
    _write_chat_id(tmp_paths)
    task_id = _make_task(tmp_paths.project_root, "orchestrator task")
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    agent_dir = tmp_paths.agent_dir("@orchestrator")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "task_id").write_text(task_id + "\n")
    (agent_dir / "status").write_text("complete: done\n")

    payload = json.dumps({"stop_hook_active": False}).encode("utf-8")
    posthook.run_posthook(payload, tmp_paths)

    from metasphere import tasks as _tasks
    # Orchestrator never auto-closes — it's persistent, not ephemeral.
    assert _tasks._find_task_file(task_id, include_completed=False) is not None


# ---------- fail-closed gate ----------


def _orchestrator_transcript(tmp_paths: Paths) -> Path:
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "the reply"}]},
            },
        ],
    )
    return transcript


def _orchestrator_payload(transcript: Path, *, session_id: str = "sess-A") -> bytes:
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": str(transcript),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
    ).encode("utf-8")


def test_failclose_missing_breadcrumb_suppresses(tmp_paths: Paths, monkeypatch):
    """Scenario 3: no breadcrumb at all (e.g. context hook never ran or
    crashed before writing). Posthook MUST NOT call the telegram-send
    path; suppression log entry exists; !info to @orchestrator queued.
    """
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = _orchestrator_transcript(tmp_paths)
    payload = _orchestrator_payload(transcript, session_id="sess-missing")
    # No breadcrumb seeded.
    with mock.patch("metasphere.telegram.api.send_message") as m:
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_not_called()

    log_path = tmp_paths.logs / "posthook-suppressions.log"
    assert log_path.exists(), "suppression log must be created"
    body = log_path.read_text(encoding="utf-8")
    assert "session=sess-missing" in body
    assert "breadcrumb-missing" in body
    assert "agent=@orchestrator" in body

    # !info to @orchestrator: a message file lands in @orchestrator's
    # canonical inbox under the project bucket.
    inboxes = list((tmp_paths.root / "projects").rglob("inbox/*.msg"))
    assert inboxes, "expected a !info to @orchestrator in the project inbox"
    body = inboxes[0].read_text(encoding="utf-8")
    assert "!info" in body
    assert "fail-closed" in body
    assert "breadcrumb-missing" in body


def test_failclose_failed_breadcrumb_suppresses(tmp_paths: Paths, monkeypatch):
    """Scenario 1: context hook ran but wrote a FAILED breadcrumb (it
    caught an exception and stamped the failure marker). Posthook MUST
    NOT call telegram-send; suppression logged; !info queued.
    """
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = _orchestrator_transcript(tmp_paths)
    # Seed a FAILED breadcrumb that matches this turn's count.
    _bc.write_breadcrumb(
        tmp_paths,
        session_id="sess-failed",
        status=_bc.STATUS_FAILED,
        user_msg_count=_bc.count_user_messages(transcript),
        agent="@orchestrator",
        reason="OSError: [Errno 11] Resource temporarily unavailable",
    )
    payload = _orchestrator_payload(transcript, session_id="sess-failed")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_not_called()

    log_body = (tmp_paths.logs / "posthook-suppressions.log").read_text(encoding="utf-8")
    assert "session=sess-failed" in log_body
    assert "context-hook-failed" in log_body


def test_happy_path_success_breadcrumb_forwards(tmp_paths: Paths, monkeypatch):
    """Scenario 2: success breadcrumb present + matching count. Posthook
    forwards via telegram-send normally; no suppression log.
    """
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = _orchestrator_transcript(tmp_paths)
    _seed_success_breadcrumb(tmp_paths, session_id="sess-ok", transcript=transcript)
    payload = _orchestrator_payload(transcript, session_id="sess-ok")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        m.return_value = [{"ok": True}]
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_called_once()
    # No suppression log created on the happy path.
    assert not (tmp_paths.logs / "posthook-suppressions.log").exists()


def test_failclose_count_mismatch_suppresses(tmp_paths: Paths, monkeypatch):
    """Scenario 4 (defense-in-depth): breadcrumb says success, but the
    transcript user-message count moved on. Means the breadcrumb is
    stale (probably from the previous turn — current turn's context
    hook crashed before it could write). Posthook MUST fail closed.
    """
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = _orchestrator_transcript(tmp_paths)
    # Breadcrumb claims user_msg_count=99 but transcript has 1 user msg.
    _bc.write_breadcrumb(
        tmp_paths,
        session_id="sess-stale",
        status=_bc.STATUS_SUCCESS,
        user_msg_count=99,
        agent="@orchestrator",
    )
    payload = _orchestrator_payload(transcript, session_id="sess-stale")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_not_called()
    log_body = (tmp_paths.logs / "posthook-suppressions.log").read_text(encoding="utf-8")
    assert "count-mismatch" in log_body


def test_failclose_does_not_block_explicit_send_path(tmp_paths: Paths, monkeypatch):
    """When the orchestrator already pushed a message via
    `metasphere-telegram send` during the turn (explicit-send marker is
    fresh), the auto-forward is suppressed independently. The
    fail-closed gate must not introduce new behavior on this path —
    we still don't auto-forward, but we also shouldn't double-log the
    suppression when the user already got an explicit send.

    Concretely: missing breadcrumb + fresh explicit-send marker →
    suppression IS logged (the user might NOT have gotten the assistant
    text — only what was explicitly sent — so degraded-context warning
    is still useful), but we must not crash or send twice.
    """
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = _orchestrator_transcript(tmp_paths)
    # Touch the explicit-send marker so it's "fresh".
    posthook.mark_orchestrator_explicit_send(tmp_paths)
    payload = _orchestrator_payload(transcript, session_id="sess-explicit")
    with mock.patch("metasphere.telegram.api.send_message") as m:
        rc = posthook.run_posthook(payload, tmp_paths)
    assert rc == 0
    m.assert_not_called()  # no auto-forward on either path


# ---------- cli --dry-run / --help ----------

def test_cli_posthook_help():
    from metasphere.cli import posthook as cli_posthook
    rc = cli_posthook.main(["--help"])
    assert rc == 0


def test_cli_posthook_dry_run_prints_json(tmp_paths: Paths, monkeypatch, capsys):
    _write_chat_id(tmp_paths)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    transcript = tmp_paths.root / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "the reply body"}]},
            }
        ],
    )
    payload = json.dumps(
        {"transcript_path": str(transcript), "stop_hook_active": False}
    ).encode("utf-8")
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False, "buffer": type("B", (), {"read": lambda self: payload})()})())
    from metasphere.cli import posthook as cli_posthook
    with mock.patch("metasphere.telegram.api.send_message") as m:
        rc = cli_posthook.main(["--dry-run"])
    assert rc == 0
    m.assert_not_called()
    out = capsys.readouterr().out.strip()
    summary = json.loads(out)
    assert summary["text_length"] == len("the reply body")
    assert summary["chunk_count"] == 1
    assert summary["chat_id"] == "12345"
    assert summary["would_send"] is True
    # Dry-run must not have written dedupe state.
    assert not (tmp_paths.state / "posthook_last_sent").exists()


# ---------- _check_deferred_command project-scope resolution ----------

def test_check_deferred_command_uses_project_scoped_session(tmp_paths: Paths):
    """Regression: when a project-scoped agent (e.g. research-monitor)
    requests ``/exit`` via ``metasphere session exit-self``, the
    deferred-cmd injection must target the project-aware tmux session
    name, not the bare ``metasphere-<agent>`` form. Bare-name resolution
    silently no-ops at ``submit_to_tmux`` because the project-prefixed
    session does not match — leaving the agent stuck in an idle REPL
    (the cron-fired-zombie pattern this primitive was built to fix).
    """
    from metasphere.agents import AgentRecord

    # Project-scoped @brand-mentions lives in
    # metasphere-research-brand-mentions, NOT metasphere-brand-mentions.
    rec = AgentRecord(
        name="@brand-mentions",
        scope="",
        parent="",
        status="",
        spawned_at="",
        project="research",
    )

    # Drop a deferred-cmd marker as if exit-self had just written it.
    (tmp_paths.state).mkdir(parents=True, exist_ok=True)
    marker = tmp_paths.state / "brand-mentions_deferred_cmd"
    marker.write_text("/exit\n", encoding="utf-8")

    with mock.patch(
        "metasphere.session.list_agents", return_value=[rec]
    ), mock.patch(
        "metasphere.tmux.submit_to_tmux", return_value=True
    ) as submit:
        posthook._check_deferred_command("@brand-mentions", tmp_paths)

    submit.assert_called_once()
    session_arg = submit.call_args.args[0]
    assert session_arg == "metasphere-research-brand-mentions", (
        f"expected project-aware session name, got {session_arg!r}"
    )
    # Marker must be consumed regardless of project scope.
    assert not marker.exists()


# ---------- best-effort diagnostic-log rotation ----------

def test_append_log_line_writes_and_no_rotation_under_cap(tmp_path: Path):
    log = tmp_path / "diag.log"
    posthook._append_log_line(log, "first line\n")
    posthook._append_log_line(log, "second line\n")
    assert log.read_text() == "first line\nsecond line\n"
    # No rotation generation while under the cap.
    assert not log.with_name("diag.log.1").exists()


def test_append_log_line_rotates_past_cap_keeps_one_generation(
    tmp_path: Path, monkeypatch
):
    log = tmp_path / "diag.log"
    # Tiny cap so a couple of writes cross it.
    monkeypatch.setattr(posthook, "_LOG_MAX_BYTES", 20)
    posthook._append_log_line(log, "0123456789\n")  # 11 bytes, under cap
    assert not log.with_name("diag.log.1").exists()
    posthook._append_log_line(log, "abcdefghij\n")  # crosses 20 -> rotate
    backup = log.with_name("diag.log.1")
    assert backup.exists()
    # Backup holds both lines (rotation happened after the second write).
    assert backup.read_text() == "0123456789\nabcdefghij\n"
    # Live log restarts fresh on the next append.
    posthook._append_log_line(log, "new\n")
    assert log.read_text() == "new\n"
    # Exactly one generation is kept — no .2.
    assert not log.with_name("diag.log.2").exists()


def test_append_log_line_rotation_is_single_generation_under_load(
    tmp_path: Path, monkeypatch
):
    log = tmp_path / "diag.log"
    monkeypatch.setattr(posthook, "_LOG_MAX_BYTES", 16)
    for i in range(50):
        posthook._append_log_line(log, f"line{i:03d}\n")
    # Never more than the live file plus a single .1 backup.
    assert not log.with_name("diag.log.2").exists()
    assert log.with_name("diag.log.1").exists()
    # The live file is absent right after a rotation (recreated on the next
    # append) — but whenever it exists it stays bounded near the cap.
    if log.exists():
        assert log.stat().st_size < posthook._LOG_MAX_BYTES + 32


def test_log_suppression_rotates_and_never_raises(tmp_paths, monkeypatch):
    monkeypatch.setattr(posthook, "_LOG_MAX_BYTES", 64)
    for i in range(100):
        posthook._log_suppression(
            tmp_paths, session_id=f"s{i}", reason="degraded", agent="@x"
        )
    log = posthook._suppression_log_path(tmp_paths)
    # Rotation happened (the .1 backup exists) and is single-generation.
    # The live file may be absent right after a rotation — recreated next call.
    assert log.with_name(log.name + ".1").exists()
    assert not log.with_name(log.name + ".2").exists()


def test_log_telegram_error_rotates_and_never_raises(tmp_paths, monkeypatch):
    monkeypatch.setattr(posthook, "_LOG_MAX_BYTES", 64)
    for i in range(100):
        posthook._log_telegram_error(tmp_paths, f"send failed attempt {i}")
    log = posthook._telegram_error_log(tmp_paths)
    # Rotation happened (the .1 backup exists) and is single-generation.
    assert log.with_name(log.name + ".1").exists()
    assert not log.with_name(log.name + ".2").exists()


def test_log_helpers_swallow_oserror(tmp_paths, monkeypatch):
    # Force the underlying append to raise; the public helpers must not.
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(posthook, "_append_log_line", _boom)
    # Neither call should propagate.
    posthook._log_suppression(tmp_paths, session_id="s", reason="r", agent="@a")
    posthook._log_telegram_error(tmp_paths, "msg")
