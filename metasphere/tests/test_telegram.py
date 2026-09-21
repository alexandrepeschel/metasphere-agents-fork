"""Tests for metasphere.telegram.

The Telegram API is mocked by replacing ``api._http_post`` with a stub
recorder. We don't make any real network calls.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from typing import List

import pytest

from metasphere.telegram import api, archiver, poller


# --- Fixtures -------------------------------------------------------------

@pytest.fixture
def fake_post(monkeypatch):
    """Replace api._http_post with a recorder that returns canned responses."""
    calls: List[dict] = []
    queue: List[dict] = []

    def fake(url, data, timeout=35):
        calls.append({"url": url, "data": dict(data)})
        if queue:
            return queue.pop(0)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(api, "_http_post", fake)
    # Pretend we have a token so _config() works.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    fake.calls = calls  # type: ignore
    fake.queue = queue  # type: ignore
    return fake


# --- send_message --------------------------------------------------------

def test_send_message_plain_text_strips_parse_mode(fake_post):
    api.send_message(123, "hello world")
    assert len(fake_post.calls) == 1
    data = fake_post.calls[0]["data"]
    assert data["text"] == "hello world"
    assert data["chat_id"] == 123
    # Default parse_mode is None and gets dropped from the request entirely
    assert "parse_mode" not in data


def test_send_message_explicit_parse_mode_passes_through(fake_post):
    api.send_message(123, "*bold*", parse_mode="MarkdownV2")
    assert fake_post.calls[0]["data"]["parse_mode"] == "MarkdownV2"


def test_send_message_chunks_at_3900_boundary(fake_post):
    # Build a message > 3900 chars with paragraph breaks so the splitter
    # has somewhere to cut.
    para = "a" * 1000
    text = "\n\n".join([para] * 5)  # ~5004 chars
    assert len(text) > api.CHUNK_MAX

    api.send_message(123, text)

    # Should have produced 2+ chunks, each <= CHUNK_MAX (plus marker)
    assert len(fake_post.calls) >= 2
    for i, c in enumerate(fake_post.calls, start=1):
        body = c["data"]["text"]
        assert body.startswith(f"[{i}/")
        # marker plus content fits within Telegram's hard cap
        assert len(body) <= 4096


def test_send_message_single_chunk_no_marker(fake_post):
    api.send_message(123, "short message")
    assert fake_post.calls[0]["data"]["text"] == "short message"
    assert "[1/" not in fake_post.calls[0]["data"]["text"]


def test_send_message_raises_on_api_error(fake_post):
    fake_post.queue.append({"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(api.TelegramAPIError) as ei:
        api.send_message(999, "hi")
    assert "chat not found" in str(ei.value)
    assert ei.value.method == "sendMessage"


def test_send_message_empty_text_rejected(fake_post):
    with pytest.raises(ValueError):
        api.send_message(1, "")


def test_send_message_html_parse_mode(fake_post):
    api.send_message(123, "<b>hi</b>", parse_mode="HTML")
    assert fake_post.calls[0]["data"]["parse_mode"] == "HTML"
    assert fake_post.calls[0]["data"]["text"] == "<b>hi</b>"


def test_escape_html_round_trip():
    assert api.escape_html("a < b > c & d") == "a &lt; b &gt; c &amp; d"
    assert api.escape_html("") == ""
    # Ampersand first to avoid double escape
    assert api.escape_html("&lt;") == "&amp;lt;"


# --- send_with_cc --------------------------------------------------------

def _read_orchestrator_inbox(tmp_paths) -> list[str]:
    inbox = tmp_paths.root / "projects" / "testproj" / ".messages" / "inbox"
    if not inbox.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in inbox.glob("*.msg")]


def test_send_with_cc_orchestrator_sender_skips_cc(fake_post, tmp_paths, monkeypatch):
    """When METASPHERE_AGENT_ID is @orchestrator, send_with_cc performs
    the telegram send but MUST NOT write a !info to the orchestrator
    inbox (the orchestrator doesn't CC itself)."""
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    api.send_with_cc(123, "hello from orchestrator")
    # Telegram send happened.
    assert len(fake_post.calls) == 1
    assert fake_post.calls[0]["data"]["text"] == "hello from orchestrator"
    # No CC landed in the orchestrator inbox.
    assert _read_orchestrator_inbox(tmp_paths) == []


def test_send_with_cc_non_orchestrator_sender_produces_one_cc(
    fake_post, tmp_paths, monkeypatch
):
    """A non-orchestrator agent's send_with_cc call lands (1) one
    telegram sendMessage call AND (2) exactly one !info .msg file in
    the @orchestrator inbox, labeled !info, whose body quotes the
    outbound telegram text.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@ww-migrate-010")
    api.send_with_cc(123, "migration applied")
    # Exactly one telegram call.
    assert len(fake_post.calls) == 1
    assert fake_post.calls[0]["data"]["text"] == "migration applied"
    # Exactly one !info message in the orchestrator inbox.
    msgs = _read_orchestrator_inbox(tmp_paths)
    assert len(msgs) == 1
    body = msgs[0]
    assert "label: '!info'" in body or "label: \"!info\"" in body or "label: !info" in body
    assert "to: '@orchestrator'" in body or "to: \"@orchestrator\"" in body or "to: '@orchestrator'" in body
    assert "from: '@ww-migrate-010'" in body or "from: \"@ww-migrate-010\"" in body
    # Prefix + the original telegram body both present.
    assert "[telegram-cc from @ww-migrate-010]" in body
    assert "migration applied" in body


def test_send_with_cc_cc_failure_does_not_break_telegram_send(
    fake_post, tmp_paths, monkeypatch
):
    """If writing the CC !info to @orchestrator's inbox blows up for
    any reason, the telegram send path must still succeed — the CC is
    best-effort. Regression guard: a mis-configured messages store
    must not silence the user-facing channel.
    """
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@ww-cortex-bounce-push")

    # Force messages.send_message to raise, simulating a broken
    # message store (permissions issue, disk full, etc).
    from metasphere import messages as _messages

    def boom(*args, **kwargs):
        raise RuntimeError("messages store unreachable")

    monkeypatch.setattr(_messages, "send_message", boom)

    # Must not raise.
    responses = api.send_with_cc(123, "user-facing text")

    # Telegram send still happened and succeeded.
    assert len(fake_post.calls) == 1
    assert fake_post.calls[0]["data"]["text"] == "user-facing text"
    assert responses  # non-empty list of API responses

    # No message was written (the CC failed silently).
    assert _read_orchestrator_inbox(tmp_paths) == []


# --- Offset persistence --------------------------------------------------

def test_offset_round_trip(tmp_path):
    p = tmp_path / "offset"
    poller.save_offset(42, str(p))
    assert poller.load_offset(str(p)) == 42


def test_offset_atomic_no_partial_write(tmp_path):
    """Concurrent writers should never leave the file empty or partial."""
    p = tmp_path / "offset"
    poller.save_offset(1, str(p))

    errors: List[Exception] = []

    def writer(start: int):
        try:
            for i in range(start, start + 50):
                poller.save_offset(i, str(p))
                # And read it back; it should always parse to a valid int.
                v = poller.load_offset(str(p))
                assert isinstance(v, int)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(s,)) for s in (100, 200, 300)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # Final value should be a valid int
    assert isinstance(poller.load_offset(str(p)), int)


def test_offset_load_missing_returns_zero(tmp_path):
    assert poller.load_offset(str(tmp_path / "nope")) == 0


# --- Archiver ------------------------------------------------------------

def test_archive_message_appends_jsonl(tmp_path):
    msg = {"message_id": 1, "text": "hi", "from": {"username": "u"}, "chat": {"id": 5}, "date": 0}
    path = archiver.archive_message(msg, base_dir=str(tmp_path))
    assert os.path.exists(path)
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hi"


def _msg(mid, text):
    return {"message_id": mid, "text": text, "from": {"username": "u"}, "chat": {"id": 5}, "date": 0}


def test_telegram_context_filters_by_surface(tmp_path):
    base = str(tmp_path)
    archiver.archive_message(_msg(1, "clustermsg"), base_dir=base, surface_id="telegram-field-agent")
    archiver.archive_message(_msg(2, "orchmsg"), base_dir=base, surface_id="telegram")
    own = archiver.telegram_context(base_dir=base, surface_id="telegram-field-agent")
    assert "clustermsg" in own and "orchmsg" not in own
    # The legacy bare "telegram" owner sees only the bare-surface row.
    legacy = archiver.telegram_context(base_dir=base, surface_id="telegram")
    assert "orchmsg" in legacy and "clustermsg" not in legacy


def test_telegram_context_none_is_unfiltered(tmp_path):
    base = str(tmp_path)
    archiver.archive_message(_msg(1, "alphamsg"), base_dir=base, surface_id="telegram-field-agent")
    archiver.archive_message(_msg(2, "bravomsg"), base_dir=base, surface_id="telegram")
    body = archiver.telegram_context(base_dir=base)  # surface_id=None → back-compat
    assert "alphamsg" in body and "bravomsg" in body


def test_telegram_context_nonowning_surface_is_empty(tmp_path):
    base = str(tmp_path)
    archiver.archive_message(_msg(1, "onlycluster"), base_dir=base, surface_id="telegram-field-agent")
    # An agent that owns no matching surface renders the empty state.
    body = archiver.telegram_context(base_dir=base, surface_id="telegram-other")
    assert "onlycluster" not in body and "No recent messages" in body


def test_archive_outgoing_stamps_surface(tmp_path):
    path = archiver.archive_outgoing(
        "@field-agent", "reply", 5, base_dir=str(tmp_path), surface_id="telegram-field-agent"
    )
    with open(path) as f:
        rec = json.loads(f.readlines()[-1])
    assert rec["surface_id"] == "telegram-field-agent"
    assert rec["outgoing"] is True


def test_archive_concurrent_appends_no_corruption(tmp_path):
    """Many threads appending must not interleave bytes within a line."""
    base = str(tmp_path)

    def worker(n: int):
        for i in range(20):
            archiver.archive_message(
                {"message_id": n * 100 + i, "text": f"t{n}-{i}", "from": {"username": f"u{n}"}, "chat": {"id": 1}, "date": 0},
                base_dir=base,
            )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Find today's file
    stream = os.path.join(base, "stream")
    files = os.listdir(stream)
    assert len(files) == 1
    with open(os.path.join(stream, files[0])) as f:
        lines = f.readlines()
    assert len(lines) == 100
    # Every line must be valid JSON (no interleaving)
    for line in lines:
        parsed = json.loads(line)
        assert "text" in parsed


def test_save_latest_atomic(tmp_path):
    msg = {"message_id": 7, "text": "hello", "from": {"username": "u"}, "chat": {"id": 1}, "date": 1234}
    archiver.save_latest(msg, base_dir=str(tmp_path))
    with open(tmp_path / "latest.json") as f:
        data = json.load(f)
    assert data["message_id"] == 7
    assert data["from"] == "u"
    assert data["text"] == "hello"


# --- Reply / reaction capture --------------------------------------------

def test_poller_captures_reply_to_fields():
    """An update with reply_to_message should surface id + preview."""
    payload = {
        "update_id": 1001,
        "message": {
            "message_id": 50,
            "chat": {"id": 1, "is_forum": False},
            "from": {"username": "testuser"},
            "text": "yes, do it",
            "date": 1700000000,
            "reply_to_message": {
                "message_id": 42,
                "text": "Should I ship the patch now?",
                "from": {"username": "orchestrator"},
            },
        },
    }
    u = poller.Update.from_payload(payload)
    assert u.kind == "message"
    assert u.message_id == 50
    assert u.reply_to_message_id == 42
    assert u.reply_to_text_preview == "Should I ship the patch now?"


def test_poller_reply_preview_truncates_at_100_chars():
    long = "x" * 250
    payload = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "chat": {"id": 1},
            "from": {"username": "testuser"},
            "text": "ok",
            "reply_to_message": {"message_id": 1, "text": long},
        },
    }
    u = poller.Update.from_payload(payload)
    assert u.reply_to_text_preview is not None
    assert len(u.reply_to_text_preview) == 100


def test_poller_message_without_reply_has_none_reply_fields():
    payload = {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "chat": {"id": 1},
            "from": {"username": "testuser"},
            "text": "hi",
        },
    }
    u = poller.Update.from_payload(payload)
    assert u.reply_to_message_id is None
    assert u.reply_to_text_preview is None


def test_poller_parses_message_reaction_update():
    """message_reaction payloads should produce kind=='reaction' Updates."""
    payload = {
        "update_id": 9001,
        "message_reaction": {
            "chat": {"id": -1001, "is_forum": True},
            "message_id": 17,
            "user": {"id": 99, "username": "testuser"},
            "date": 1700000123,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
        },
    }
    u = poller.Update.from_payload(payload)
    assert u.kind == "reaction"
    assert u.reaction_target_message_id == 17
    assert u.reaction_emojis == ["\U0001f44d"]
    assert u.from_username == "testuser"
    assert u.chat_id == -1001


def test_poller_allowed_updates_includes_message_reaction():
    """get_updates must request message_reaction explicitly."""
    assert "message_reaction" in poller.ALLOWED_UPDATES


def test_get_updates_passes_allowed_updates(monkeypatch):
    """The JSON payload to getUpdates must list message_reaction."""
    captured = {}

    def fake_call(method, **params):
        captured["method"] = method
        captured["params"] = params
        return {"ok": True, "result": []}

    monkeypatch.setattr(poller.api, "call", fake_call)
    poller.get_updates(offset=0, timeout=1)
    assert captured["method"] == "getUpdates"
    allowed = json.loads(captured["params"]["allowed_updates"])
    assert "message_reaction" in allowed
    assert "message" in allowed


# --- Archiver: reply / reaction round-trip -------------------------------

def test_archive_message_lifts_reply_to_fields(tmp_path):
    """archive_message should surface reply_to_* at the top level."""
    msg = {
        "message_id": 60,
        "text": "replying now",
        "from": {"username": "testuser"},
        "chat": {"id": 5},
        "date": 1700000000,
        "reply_to_message": {
            "message_id": 42,
            "text": "What do you think?",
        },
    }
    path = archiver.archive_message(msg, base_dir=str(tmp_path))
    with open(path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["reply_to_message_id"] == 42
    assert row["reply_to_text_preview"] == "What do you think?"
    # Nested field is preserved (non-destructive enrichment)
    assert row["reply_to_message"]["message_id"] == 42
    # The input dict wasn't mutated
    assert "reply_to_message_id" not in msg


def test_archive_message_without_reply_unchanged(tmp_path):
    msg = {
        "message_id": 1,
        "text": "hi",
        "from": {"username": "u"},
        "chat": {"id": 1},
        "date": 0,
    }
    path = archiver.archive_message(msg, base_dir=str(tmp_path))
    with open(path) as f:
        row = json.loads(f.readline())
    assert "reply_to_message_id" not in row
    assert "reply_to_text_preview" not in row


def test_archive_reaction_round_trip(tmp_path):
    path = archiver.archive_reaction(
        target_message_id=17,
        emojis=["\U0001f44d"],
        from_username="testuser",
        chat_id=-1001,
        date=1700000000,
        base_dir=str(tmp_path),
    )
    with open(path) as f:
        row = json.loads(f.readline())
    assert row["kind"] == "reaction"
    assert row["reaction_target_message_id"] == 17
    assert row["reactions"] == [{"emoji": "\U0001f44d", "from": "testuser"}]


# --- telegram_context() rendering ----------------------------------------

def test_telegram_context_renders_reply_indicator(tmp_path):
    msg = {
        "message_id": 61,
        "text": "yes please",
        "from": {"username": "testuser"},
        "chat": {"id": 5},
        "date": 1700000000,
        "reply_to_message": {
            "message_id": 42,
            "text": "Should I ship the patch now?",
        },
    }
    archiver.archive_message(msg, base_dir=str(tmp_path))
    out = archiver.telegram_context(history=5, base_dir=str(tmp_path))
    assert "\u21a9 replying to:" in out
    assert "Should I ship the patch now?" in out


def test_telegram_context_renders_reaction_line(tmp_path):
    archiver.archive_reaction(
        target_message_id=99,
        emojis=["\U0001f525"],
        from_username="testuser",
        chat_id=-1001,
        date=1700000000,
        base_dir=str(tmp_path),
    )
    out = archiver.telegram_context(history=5, base_dir=str(tmp_path))
    assert "reaction:" in out
    assert "\U0001f525" in out
    assert "@testuser" in out
    assert "msg-99" in out


def test_telegram_context_reply_preview_truncates_at_60_chars(tmp_path):
    long_quote = "a" * 200
    msg = {
        "message_id": 1,
        "text": "ok",
        "from": {"username": "testuser"},
        "chat": {"id": 1},
        "date": 1700000000,
        "reply_to_message": {"message_id": 99, "text": long_quote},
    }
    archiver.archive_message(msg, base_dir=str(tmp_path))
    out = archiver.telegram_context(history=5, base_dir=str(tmp_path))
    # 60 chars of 'a' plus the trailing ellipsis marker
    assert ("a" * 60 + "...") in out


def test_telegram_context_backcompat_old_rows_without_new_fields(tmp_path):
    """Rows written before reply_to / reactions existed must still render."""
    import os as _os

    stream = _os.path.join(str(tmp_path), "stream")
    _os.makedirs(stream, exist_ok=True)
    # Write an old-shape row directly to today's file.
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    with open(_os.path.join(stream, f"{today}.jsonl"), "w") as f:
        f.write(json.dumps({
            "message_id": 1,
            "text": "legacy row",
            "from": {"username": "testuser"},
            "chat": {"id": 1},
            "date": 1700000000,
        }) + "\n")
    out = archiver.telegram_context(history=5, base_dir=str(tmp_path))
    assert "legacy row" in out
    assert "\u21a9" not in out  # No reply indicator when field absent
    assert "reaction:" not in out


# --- Chunk splitter unit -------------------------------------------------

def test_split_chunks_prefers_paragraph_break():
    text = "para1" + "x" * 2000 + "\n\n" + "para2" + "y" * 2000
    chunks = api._split_chunks(text, max_len=3900)
    # First chunk should end at the paragraph break, not mid-word
    assert chunks[0].endswith("x" * 2000) or chunks[0].endswith("para1" + "x" * 2000)
    assert all(len(c) <= 3900 for c in chunks)


def test_split_chunks_short_text_single_chunk():
    assert api._split_chunks("short") == ["short"]


# --- Attachment parsing / download / render ------------------------------

from pathlib import Path

from metasphere.telegram import attachments as _atts
from metasphere.telegram import handler as _handler
from metasphere.telegram import inject


# Autouse guard: redirect ATTACHMENTS_ROOT to a per-test tmp dir for every
# test in this module. Prevents the real ~/.metasphere/attachments/ from
# being written to if a test forgets to monkeypatch the root (which
# already bit us once — left 555/biggest.bin behind on the prod host).
@pytest.fixture(autouse=True)
def _no_real_attachments_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(_atts, "ATTACHMENTS_ROOT", tmp_path / "__attachments_sandbox__")
    # Also redirect the debug log so tests don't smear diagnostic output
    # into the real ~/.metasphere/state/telegram_debug.log (which the
    # live orchestrator tails for the open incident repro).
    monkeypatch.setattr(_atts, "DEBUG_LOG_PATH", tmp_path / "__telegram_debug_sandbox__.log")
    yield
    # Paranoid post-condition: even if a test bypassed the monkeypatch,
    # detect it so the suite fails loudly instead of silently polluting
    # the real dir on the next run.
    real = Path.home() / ".metasphere" / "attachments"
    if real.exists():
        # Only fail if the test wrote something during this invocation
        # that looks like test-fixture data. Any file whose bytes start
        # with ``BYTES:`` came from the fake_http_get in this module.
        for p in real.rglob("*"):
            if p.is_file() and p.read_bytes().startswith(b"BYTES:"):
                raise AssertionError(
                    f"test pollution: {p} was written to the real attachments dir"
                )


def test_parse_attachments_photo_picks_largest_size():
    """``photo`` arrives as a size-ascending array of thumbnails; we
    must pick exactly one (the largest) and tag it as kind=photo.
    """
    msg = {
        "message_id": 42,
        "photo": [
            {"file_id": "thumb", "file_size": 100, "width": 90},
            {"file_id": "mid", "file_size": 5000, "width": 320},
            {"file_id": "biggest", "file_size": 50000, "width": 1280},
        ],
    }
    refs = _atts.parse_attachments(msg)
    assert len(refs) == 1
    assert refs[0].kind == "photo"
    assert refs[0].file_id == "biggest"
    assert refs[0].file_size == 50000
    assert refs[0].mime_type == "image/jpeg"


def test_parse_attachments_document_preserves_filename_and_mime():
    msg = {
        "message_id": 7,
        "document": {
            "file_id": "doc1",
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
            "file_size": 353024,
        },
    }
    refs = _atts.parse_attachments(msg)
    assert len(refs) == 1
    r = refs[0]
    assert r.kind == "document"
    assert r.file_name == "report.pdf"
    assert r.mime_type == "application/pdf"


def test_parse_attachments_generic_catches_sticker_and_animation():
    """Spec amendment: ANY top-level dict with a file_id is an
    attachment — we don't maintain a whitelist. This protects against
    future Bot API additions (sticker, animation, video_note, etc.)
    without a code change.
    """
    msg = {
        "message_id": 9,
        "sticker": {"file_id": "sticker1", "file_size": 20000, "is_animated": False},
        "animation": {"file_id": "anim1", "file_name": "funny.mp4", "mime_type": "video/mp4"},
    }
    refs = _atts.parse_attachments(msg)
    kinds = sorted(r.kind for r in refs)
    assert kinds == ["animation", "sticker"]


def test_parse_attachments_empty_on_plain_text():
    msg = {"message_id": 1, "text": "hello", "from": {"username": "synthetic-user"}}
    assert _atts.parse_attachments(msg) == []


def test_parse_attachments_ignores_reply_to_message_nesting():
    """A reply payload is a nested dict but doesn't carry a top-level
    ``file_id``, so it must not be treated as an attachment.
    """
    msg = {
        "message_id": 2,
        "text": "reply",
        "reply_to_message": {"message_id": 1, "text": "original"},
    }
    assert _atts.parse_attachments(msg) == []


def test_download_attachment_success_writes_file(tmp_path, monkeypatch):
    """Given a successful getFile + http_get, the attachment bytes land
    at ``<dest>/<safe-filename>`` and the returned record points at it.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")

    ref = _atts.AttachmentRef(
        kind="document", file_id="doc1",
        file_name="report.pdf", mime_type="application/pdf",
        file_size=12,
    )

    def fake_call(method, **params):
        assert method == "getFile"
        assert params["file_id"] == "doc1"
        return {"ok": True, "result": {"file_path": "documents/file_99.pdf"}}

    payload = b"%PDF-fake\n\n"

    def fake_http_get(url, timeout):
        # URL must embed the bot token + the file_path from getFile
        assert "TEST:TOKEN" in url
        assert url.endswith("documents/file_99.pdf")
        return payload

    result = _atts.download_attachment(
        ref, tmp_path, http_get=fake_http_get, call_fn=fake_call,
    )
    assert result.error is None
    assert result.path == tmp_path / "report.pdf"
    assert result.path.read_bytes() == payload
    assert result.kind == "document"
    assert result.mime_type == "application/pdf"


def test_download_attachment_getfile_error_returns_note(tmp_path, monkeypatch):
    """If ``getFile`` fails (bad file_id, revoked token), we return a
    record with ``error`` set and ``path=None``. The poller must not
    crash on this path.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")

    def fake_call(method, **params):
        raise api.TelegramAPIError("getFile", "FILE_REFERENCE_EXPIRED", {})

    ref = _atts.AttachmentRef(kind="photo", file_id="old")
    result = _atts.download_attachment(
        ref, tmp_path, http_get=lambda u, t: b"", call_fn=fake_call,
    )
    assert result.path is None
    assert result.error is not None
    assert "FILE_REFERENCE_EXPIRED" in result.error
    assert result.kind == "photo"


def test_download_attachment_http_error_returns_note(tmp_path, monkeypatch):
    """A network error during the file download degrades gracefully
    instead of propagating out of the handler.
    """
    import urllib.error as _urle
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")

    def fake_call(method, **params):
        return {"ok": True, "result": {"file_path": "photos/file_1.jpg"}}

    def failing_http_get(url, timeout):
        raise _urle.URLError("connection refused")

    ref = _atts.AttachmentRef(kind="photo", file_id="p1")
    result = _atts.download_attachment(
        ref, tmp_path, http_get=failing_http_get, call_fn=fake_call,
    )
    assert result.path is None
    assert result.error is not None
    assert "connection refused" in result.error


def test_download_attachment_sanitizes_dangerous_filename(tmp_path, monkeypatch):
    """A user-supplied document name like ``../../etc/passwd`` must not
    escape the dest dir. Non-[A-Za-z0-9._-] chars collapse to underscore.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")

    def fake_call(method, **params):
        return {"ok": True, "result": {"file_path": "documents/x"}}

    ref = _atts.AttachmentRef(
        kind="document", file_id="doc",
        file_name="../../etc/passwd", file_size=3,
    )
    result = _atts.download_attachment(
        ref, tmp_path, http_get=lambda u, t: b"abc", call_fn=fake_call,
    )
    assert result.path is not None
    # The written file sits inside dest_dir, not in a parent.
    assert result.path.parent == tmp_path
    assert ".." not in result.path.name


def test_render_attachment_block_formats_paths_and_sizes():
    items = [
        _atts.DownloadedAttachment(
            kind="photo", path=Path("/tmp/att/1/image.jpg"),
            file_size=1310720,  # 1.25 MB
            mime_type="image/jpeg",
        ),
        _atts.DownloadedAttachment(
            kind="document", path=Path("/tmp/att/1/report.pdf"),
            file_size=353024,  # ~344.8 KB
            mime_type="application/pdf",
        ),
    ]
    block = _atts.render_attachment_block(items)
    lines = block.splitlines()
    assert lines[0] == "[attachments]"
    assert "- photo: /tmp/att/1/image.jpg (1.2 MB, jpeg)" in lines
    assert any(l.startswith("- document: /tmp/att/1/report.pdf (") and "pdf" in l for l in lines)


def test_render_attachment_block_surfaces_failure_note():
    items = [
        _atts.DownloadedAttachment(
            kind="audio", path=None, file_size=None,
            mime_type=None, error="getFile: FILE_NOT_FOUND",
        ),
    ]
    block = _atts.render_attachment_block(items)
    assert "audio" in block
    assert "download failed" in block
    assert "FILE_NOT_FOUND" in block


def test_render_attachment_block_empty_returns_empty_string():
    assert _atts.render_attachment_block([]) == ""


# --- CLI _handle_update integration (mocked getFile + http + tmux) -------


def _patch_handle_update(monkeypatch, tmp_path):
    """Wire a fake getFile + http fetcher + tmux sink for handle_update.

    Returns ``(http_log, tmux_log)`` so assertions can inspect what was
    downloaded and what payload got injected.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setattr(_atts, "ATTACHMENTS_ROOT", tmp_path / "attachments")

    http_log: list = []

    def fake_http_get(url, timeout):
        http_log.append(url)
        return b"BYTES:" + url.rsplit("/", 1)[-1].encode()

    monkeypatch.setattr(_atts, "_http_get_default", fake_http_get)

    def fake_api_call(method, **params):
        if method == "getFile":
            fid = params["file_id"]
            return {"ok": True, "result": {"file_path": f"media/{fid}.bin"}}
        if method == "setMessageReaction":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected api.call: {method}")

    monkeypatch.setattr(api, "call", fake_api_call)

    tmux_log: list = []

    def fake_submit_to_tmux(from_user, text, session="metasphere-orchestrator", **kwargs):
        tmux_log.append({"from": from_user, "text": text, "session": session})
        return True

    monkeypatch.setattr(inject, "submit_to_tmux", fake_submit_to_tmux)

    # Archiver + handler defaults point at ~/.metasphere — redirect all
    # of them so a test can't accidentally touch the real home dir.
    monkeypatch.setattr(archiver, "DEFAULT_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(_handler, "_default_save_chat_id", lambda cid: None)
    monkeypatch.setattr(_handler, "_default_pending_ack_writer", lambda cid, mid: None)

    return http_log, tmux_log


def test_handle_update_photo_with_caption_injects_attachment_block(tmp_path, monkeypatch):
    """Simulated trace: the operator sends a photo with caption 'look at this'.
    The handler must (1) download the largest thumbnail, (2) inject a
    payload that contains BOTH the caption and the attachment block
    pointing at the saved path.
    """
    http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)

    payload = {
        "update_id": 1000,
        "message": {
            "message_id": 555,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "caption": "look at this",
            "photo": [
                {"file_id": "thumb", "file_size": 100},
                {"file_id": "biggest", "file_size": 90000},
            ],
        },
    }
    u = poller.Update.from_payload(payload)

    _handler.handle_update(u)

    # One file was downloaded — the largest thumbnail.
    assert len(http_log) == 1
    assert "biggest" in http_log[0]

    # Exactly one tmux injection happened.
    assert len(tmux_log) == 1
    injected = tmux_log[0]["text"]
    assert tmux_log[0]["from"] == "@synthetic-user"
    # Caption present at the top of the payload.
    assert injected.startswith("look at this")
    # Attachment block present, pointing at the saved path under the tmp
    # attachments root (keyed on message_id=555).
    assert "[attachments]" in injected
    assert f"{tmp_path / 'attachments' / '555'}" in injected

    # File exists on disk.
    saved = list((tmp_path / "attachments" / "555").iterdir())
    assert len(saved) == 1


def test_handle_update_photo_only_no_caption_still_injects_block(tmp_path, monkeypatch):
    """A bare photo (no text, no caption) used to be dropped by the
    early return on ``not u.text``. It must now produce an injection
    containing just the attachment block.
    """
    http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)

    payload = {
        "update_id": 1001,
        "message": {
            "message_id": 556,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "photo": [{"file_id": "only", "file_size": 10}],
        },
    }
    u = poller.Update.from_payload(payload)

    _handler.handle_update(u)

    assert len(tmux_log) == 1
    injected = tmux_log[0]["text"]
    assert injected.startswith("[attachments]")
    assert "photo" in injected


def test_handle_update_download_failure_still_injects_note(tmp_path, monkeypatch):
    """If getFile fails, the poller must not crash — it must still
    inject a note so the orchestrator sees that a file was attempted.
    """
    _patch_handle_update(monkeypatch, tmp_path)

    # Override api.call to fail on getFile.
    def failing_call(method, **params):
        if method == "getFile":
            raise api.TelegramAPIError("getFile", "FILE_NOT_FOUND", {})
        if method == "setMessageReaction":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected: {method}")

    monkeypatch.setattr(api, "call", failing_call)

    tmux_log: list = []

    def fake_submit(from_user, text, session="metasphere-orchestrator", **kwargs):
        tmux_log.append({"from": from_user, "text": text})
        return True

    monkeypatch.setattr(inject, "submit_to_tmux", fake_submit)

    payload = {
        "update_id": 1002,
        "message": {
            "message_id": 557,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "caption": "hello",
            "document": {"file_id": "stale", "file_name": "x.pdf"},
        },
    }
    u = poller.Update.from_payload(payload)

    # Must not raise.
    _handler.handle_update(u)

    assert len(tmux_log) == 1
    injected = tmux_log[0]["text"]
    assert "hello" in injected
    assert "[attachments]" in injected
    assert "download failed" in injected
    assert "FILE_NOT_FOUND" in injected


def test_handle_update_plain_text_unchanged(tmp_path, monkeypatch):
    """Sanity: a plain-text message (no attachments) behaves exactly as
    before — no HTTP calls, inject gets the raw text, no attachment block.
    """
    http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)

    payload = {
        "update_id": 1003,
        "message": {
            "message_id": 558,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "text": "just text",
        },
    }
    u = poller.Update.from_payload(payload)

    _handler.handle_update(u)

    assert http_log == []
    assert len(tmux_log) == 1
    assert tmux_log[0]["text"] == "just text"


def test_handle_update_telegram_inject_does_not_defer(tmp_path, monkeypatch):
    """Telegram-user inbound IS the user typing — clobbering visible
    REPL content with a telegram is precisely what testuser wants. The
    handler MUST pass ``defer_if_busy=False`` (or omit it / leave the
    default) so the input-buffer guard never gates this path.

    Regression for the 2026-04-16 PR #23 overreach: handler.py originally
    passed defer_if_busy=True, which silently dropped testuser's
    12:15/12:48/12:54 CEST messages whenever the orchestrator pane had
    typed content. Telegram-side marked status=read but the pane never
    became a user-turn.
    """
    captured: list[dict] = []

    def fake_submit(from_user, text, session="metasphere-orchestrator", **kwargs):
        captured.append({"from": from_user, "text": text, "kwargs": dict(kwargs)})
        return True

    monkeypatch.setattr(inject, "submit_to_tmux", fake_submit)

    monkeypatch.setattr(archiver, "DEFAULT_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(_handler, "_default_save_chat_id", lambda cid: None)
    monkeypatch.setattr(_handler, "_default_pending_ack_writer", lambda cid, mid: None)

    payload = {
        "update_id": 2001,
        "message": {
            "message_id": 901,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "testuser"},
            "date": 1700000000,
            "text": "is this getting through?",
        },
    }
    u = poller.Update.from_payload(payload)
    _handler.handle_update(u)

    assert len(captured) == 1, "telegram path must inject exactly once"
    # The guard MUST NOT be active for telegram-user input.
    assert captured[0]["kwargs"].get("defer_if_busy") is False, (
        "handler must pass defer_if_busy=False — telegram-user input is "
        "the user typing and must always land, even when REPL has typed "
        "content visible (that is precisely what the user is replacing)."
    )


def test_handle_update_telegram_inject_does_not_interrupt(tmp_path, monkeypatch):
    """Telegram-user inbound must QUEUE behind a running turn, not interrupt it.

    Regression for the 2026-07-28 wedge: escape_prefix=True (the old default)
    fired an Escape to kill the in-flight turn before pasting; landing mid-tool-
    call it corrupted the session ("Request interrupted by user" → "Something
    went wrong / use /new"). The handler MUST pass escape_prefix=False so Claude
    Code's user-turn queue holds the message until the current turn finishes.
    """
    captured: list[dict] = []

    def fake_submit(from_user, text, session="metasphere-orchestrator", **kwargs):
        captured.append({"from": from_user, "text": text, "kwargs": dict(kwargs)})
        return True

    monkeypatch.setattr(inject, "submit_to_tmux", fake_submit)
    monkeypatch.setattr(archiver, "DEFAULT_DIR", str(tmp_path / "tg"))
    monkeypatch.setattr(_handler, "_default_save_chat_id", lambda cid: None)
    monkeypatch.setattr(_handler, "_default_pending_ack_writer", lambda cid, mid: None)

    payload = {
        "update_id": 2002,
        "message": {
            "message_id": 902,
            "chat": {"id": 123, "is_forum": False},
            "from": {"username": "testuser"},
            "date": 1700000000,
            "text": "do not interrupt my turn",
        },
    }
    u = poller.Update.from_payload(payload)
    _handler.handle_update(u)

    assert len(captured) == 1
    assert captured[0]["kwargs"].get("escape_prefix") is False, (
        "handler must pass escape_prefix=False — interrupting a running turn "
        "with an Escape wedges the session; the message must queue instead."
    )
    # Delivery guarantee is unchanged: still fires regardless of busy state.
    assert captured[0]["kwargs"].get("defer_if_busy") is False


# NOTE: there is no integration test against a real bot token. The
# poller-side path above covers the download + inject wiring with
# mocked getFile + http_get. A live end-to-end test would require a
# valid TELEGRAM_BOT_TOKEN and a controlled chat — out of scope here.


# --- Debug logging -------------------------------------------------------


def test_debug_log_writes_jsonl_with_timestamp(tmp_path):
    log = tmp_path / "telegram_debug.log"
    _atts.debug_log({"stage": "test", "value": 42}, path=log)
    _atts.debug_log({"stage": "test", "value": 43}, path=log)
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["stage"] == "test"
    assert rec["value"] == 42
    assert "ts" in rec and rec["ts"].endswith("Z")


def test_debug_log_never_raises_on_filesystem_error(tmp_path):
    # Path that can't be created (parent is a file, not a dir).
    bad_parent = tmp_path / "not_a_dir"
    bad_parent.write_text("blocker")
    bad_path = bad_parent / "log.jsonl"
    # Must not raise.
    _atts.debug_log({"stage": "test"}, path=bad_path)


def test_summarize_message_captures_media_keys():
    msg = {
        "message_id": 42,
        "chat": {"id": 1},
        "photo": [{"file_id": "x"}],
        "document": {"file_id": "d"},
        "caption": "note",
    }
    summary = _atts.summarize_message_for_debug(msg)
    assert summary["message_id"] == 42
    assert summary["chat_id"] == 1
    assert "photo" in summary["media_keys"]
    assert "document" in summary["media_keys"]
    assert summary["has_caption"] is True
    assert summary["has_text"] is False


def test_handle_update_emits_debug_log_on_attachment_path(tmp_path, monkeypatch):
    """A real-style photo payload produces a post_parse + pre_inject
    pair in the debug log, even when the download path stubs out to
    fake bytes. Critical for the open incident: lets us see the raw
    msg keys + parse result on the operator's next send.
    """
    http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)
    debug_log = tmp_path / "telegram_debug.log"
    monkeypatch.setattr(_atts, "DEBUG_LOG_PATH", debug_log)

    payload = {
        "update_id": 2001,
        "message": {
            "message_id": 900,
            "chat": {"id": 123},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            "photo": [{"file_id": "p1", "file_size": 1000}],
        },
    }
    u = poller.Update.from_payload(payload)
    _handler.handle_update(u)

    assert debug_log.exists()
    records = [json.loads(l) for l in debug_log.read_text().strip().splitlines()]
    stages = [r["stage"] for r in records]
    assert "post_parse" in stages
    assert "pre_inject" in stages
    post_parse = next(r for r in records if r["stage"] == "post_parse")
    assert post_parse["summary"]["media_keys"] == ["photo"]
    assert post_parse["refs"][0]["kind"] == "photo"


def test_handle_update_emits_debug_log_when_parse_returns_empty(tmp_path, monkeypatch):
    """A message with ONLY a non-file_id media-looking field (e.g. a
    ``contact`` or a future Bot API key we don't yet recognise) must
    still produce a post_parse log line with ``refs: []``. That's how
    we'll catch a schema mismatch in the wild.
    """
    _patch_handle_update(monkeypatch, tmp_path)
    debug_log = tmp_path / "telegram_debug.log"
    monkeypatch.setattr(_atts, "DEBUG_LOG_PATH", debug_log)

    payload = {
        "update_id": 2002,
        "message": {
            "message_id": 901,
            "chat": {"id": 123},
            "from": {"username": "synthetic-user"},
            "date": 1700000000,
            # No text, no caption, no known media keys — but a contact
            # payload, which we don't recognise as downloadable.
            "contact": {"phone_number": "+1", "first_name": "J"},
        },
    }
    u = poller.Update.from_payload(payload)
    _handler.handle_update(u)

    records = [json.loads(l) for l in debug_log.read_text().strip().splitlines()]
    post_parse = next(r for r in records if r["stage"] == "post_parse")
    assert post_parse["refs"] == []
    assert "contact" in post_parse["summary"]["keys"]
    # And we should have then early-returned because body=None, refs=[].
    assert any(r["stage"] == "early_return" and r["reason"] == "no body and no refs"
               for r in records)


# --- group-mention wake gating (PR A) ------------------------------------
#
# Privacy mode is OFF on the production bot, so the gateway receives every
# group message — but only addressed messages should clobber the
# orchestrator's tmux REPL. Unaddressed group chatter stays in the
# stream/archive without waking.

import pytest


@pytest.fixture
def _bot_identity_stub(monkeypatch):
    """Force `api.bot_identity` to a fixed lowercased username + id so
    the wake matcher has a deterministic target without hitting getMe."""
    monkeypatch.setattr(api, "bot_identity",
                        lambda: {"id": 9999, "username": "mybot"})


def _build_message_update(*, chat_type: str, text: str,
                           entities=None, reply_to=None, chat_id: int = -100,
                           update_id: int = 5000, message_id: int = 555):
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type, "is_forum": False},
            "from": {"username": "testuser"},
            "date": 1700000000,
            "text": text,
        },
    }
    if entities is not None:
        payload["message"]["entities"] = entities
    if reply_to is not None:
        payload["message"]["reply_to_message"] = reply_to
    return poller.Update.from_payload(payload)


def test_addressed_helper_private_chat_always_wakes(_bot_identity_stub):
    u = _build_message_update(chat_type="private", text="hello",
                               chat_id=123)
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is True


def test_addressed_helper_unknown_chat_type_falls_through_to_wake():
    """chat_type=None (legacy fixture without `type`) preserves the
    pre-PR-A behavior: wake on every body-bearing inbound."""
    u = _build_message_update(chat_type=None, text="hello", chat_id=42)
    # chat_type is None when the test fixture omits it (legacy shape).
    assert u.chat_type is None
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is True


def test_addressed_helper_group_with_bot_mention(_bot_identity_stub):
    text = "@mybot meet @otherbot"
    u = _build_message_update(
        chat_type="group", text=text,
        entities=[{"type": "mention", "offset": 0,
                   "length": len("@mybot")},
                  {"type": "mention",
                   "offset": text.index("@otherbot"),
                   "length": len("@otherbot")}],
    )
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is True


def test_addressed_helper_group_without_mention_no_wake():
    """Bot-to-bot group chatter that doesn't @-mention us → no wake."""
    u = _build_message_update(
        chat_type="supergroup",
        text="@otherbot pinging the other bot",
        entities=[{"type": "mention", "offset": 0,
                   "length": len("@otherbot")}],
    )
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is False


def test_addressed_helper_group_slash_command_wakes():
    """`/help` (or `/help@mybot`) in a group → addressed."""
    u = _build_message_update(chat_type="group", text="/status@mybot")
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is True


def test_addressed_helper_group_reply_to_bot_wakes():
    """Reply to a message authored by the bot → addressed even without
    explicit @-mention."""
    u = _build_message_update(
        chat_type="group", text="thanks for the brief",
        reply_to={"message_id": 100, "from": {"id": 9999, "username": "mybot"}},
    )
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is True


def test_addressed_helper_group_reply_to_other_user_no_wake():
    """Reply to a non-bot user → no wake."""
    u = _build_message_update(
        chat_type="group", text="agreed",
        reply_to={"message_id": 100, "from": {"id": 7777, "username": "testuser"}},
    )
    assert _handler._is_addressed_to_bot(u, "mybot", 9999) is False


def test_addressed_helper_no_bot_username_degrades_safe():
    """getMe failed → bot_username='' → group messages don't wake.
    Safer than waking on everything when we can't identify ourselves."""
    text = "@mybot ping"
    u = _build_message_update(
        chat_type="group", text=text,
        entities=[{"type": "mention", "offset": 0, "length": len("@mybot")}],
    )
    assert _handler._is_addressed_to_bot(u, "", None) is False


def test_handle_update_skips_wake_for_unaddressed_group(tmp_path, monkeypatch,
                                                         _bot_identity_stub):
    """Full handle_update path: unaddressed group message is archived
    + acked but tmux_submit is NOT called."""
    _http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)
    u = _build_message_update(
        chat_type="group", text="just chatting between bots", chat_id=-100,
    )
    _handler.handle_update(u)
    assert tmux_log == [], f"unexpected wake on unaddressed group: {tmux_log}"


def test_handle_update_wakes_for_group_mention(tmp_path, monkeypatch,
                                                 _bot_identity_stub):
    """Full handle_update path: group message with explicit @-mention
    of the bot → tmux_submit fires (the addressed-group case)."""
    _http_log, tmux_log = _patch_handle_update(monkeypatch, tmp_path)
    text = "@mybot meet @otherbot"
    u = _build_message_update(
        chat_type="group", text=text,
        entities=[{"type": "mention", "offset": 0,
                   "length": len("@mybot")}],
    )
    _handler.handle_update(u)
    assert len(tmux_log) == 1
    assert "@mybot meet" in tmux_log[0]["text"]


def test_handle_update_queues_failed_addressed_delivery(
    tmp_path, monkeypatch, _bot_identity_stub
):
    _patch_handle_update(monkeypatch, tmp_path)
    queued: list[dict] = []
    u = _build_message_update(
        chat_type="private",
        text="survive the startup selector",
        chat_id=123,
        update_id=5100,
    )

    _handler.handle_update(
        u,
        tmux_submit=lambda *args, **kwargs: False,
        write_pending_inbound=lambda **kwargs: queued.append(kwargs),
        surface_id="telegram-relay",
    )

    assert len(queued) == 1
    assert queued[0]["delivery_id"] == "telegram-relay:5100:@orchestrator"
    assert queued[0]["text"] == "survive the startup selector"
    assert queued[0]["session"] == "metasphere-orchestrator"
    assert queued[0]["target_agent_id"] == "@orchestrator"
    assert queued[0]["chat_id"] == 123
    assert queued[0]["surface_id"] == "telegram-relay"
    assert "--surface telegram-relay" in queued[0]["reply_command"]
    assert "--chat-id 123" in queued[0]["reply_command"]


def test_handle_update_addressed_group_calls_start_session(
        tmp_path, monkeypatch, _bot_identity_stub):
    """Addressed group inbound triggers start_session before inject so
    a dormant orchestrator session gets respawned rather than dropping
    the message until the next heartbeat tick."""
    _patch_handle_update(monkeypatch, tmp_path)
    start_calls: list = []
    import metasphere.gateway.session as _gw_session
    monkeypatch.setattr(_gw_session, "start_session",
                        lambda *a, **k: start_calls.append(a) or True)
    u = _build_message_update(
        chat_type="group", text="@mybot status?",
        entities=[{"type": "mention", "offset": 0,
                   "length": len("@mybot")}],
    )
    _handler.handle_update(u)
    assert len(start_calls) == 1, "start_session must run for addressed inbound"


def test_handle_update_unaddressed_group_skips_start_session(
        tmp_path, monkeypatch, _bot_identity_stub):
    """Unaddressed group chatter must NOT trigger start_session — that
    would defeat dormancy (every group msg revives a reaped session)."""
    _patch_handle_update(monkeypatch, tmp_path)
    start_calls: list = []
    import metasphere.gateway.session as _gw_session
    monkeypatch.setattr(_gw_session, "start_session",
                        lambda *a, **k: start_calls.append(a) or True)
    u = _build_message_update(chat_type="group", text="random chatter")
    _handler.handle_update(u)
    assert start_calls == []

# --- Per-surface token resolution (PR multi-surface-routing) ---------------


def test_load_token_per_surface_env_file_wins_over_default(
    tmp_path, monkeypatch,
):
    """Per-surface env file (`telegram-relay.env`) wins over the
    default `telegram.env` when an explicit surface_id is passed."""
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN_REWRITE', raising=False)
    config_dir = tmp_path / '.metasphere' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'telegram.env').write_text(
        'TELEGRAM_BOT_TOKEN=default-token\n'
    )
    (config_dir / 'telegram-relay.env').write_text(
        'TELEGRAM_BOT_TOKEN=relay-token\n'
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    tok = api._load_token(surface_id='telegram-relay')
    assert tok == 'relay-token'


def test_load_token_falls_back_to_default_when_per_surface_missing(
    tmp_path, monkeypatch,
):
    """surface_id with no dedicated env file → fall back to the default
    `telegram.env` (back-compat for single-bot installs)."""
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN_REWRITE', raising=False)
    config_dir = tmp_path / '.metasphere' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'telegram.env').write_text(
        'TELEGRAM_BOT_TOKEN=default-token\n'
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    tok = api._load_token(surface_id='telegram-unknown')
    assert tok == 'default-token'


def test_load_token_per_surface_file_wins_over_global_env_var(
    tmp_path, monkeypatch,
):
    """A per-agent surface's own token file is authoritative even when a
    process-wide TELEGRAM_BOT_TOKEN is ALSO set (e.g. leaked in via the
    config loader, or set by systemd) — otherwise every per-agent bot
    would silently collapse onto whichever token the process inherited,
    defeating the whole point of a dedicated surface."""
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'global-env-token')
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN_REWRITE', raising=False)
    config_dir = tmp_path / '.metasphere' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'telegram-field-agent.env').write_text(
        'TELEGRAM_BOT_TOKEN=field-agent-own-token\n'
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    tok = api._load_token(surface_id='telegram-field-agent')
    assert tok == 'field-agent-own-token'


def test_load_token_legacy_surface_still_prefers_global_env_var(
    tmp_path, monkeypatch,
):
    """The bare "telegram"/None surface keeps its pre-existing
    precedence: the global env var wins over telegram.env, byte-
    identical to single-bot installs before per-surface routing."""
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'global-env-token')
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN_REWRITE', raising=False)
    config_dir = tmp_path / '.metasphere' / 'config'
    config_dir.mkdir(parents=True)
    (config_dir / 'telegram.env').write_text(
        'TELEGRAM_BOT_TOKEN=file-token\n'
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    assert api._load_token(surface_id=None) == 'global-env-token'
    assert api._load_token(surface_id='telegram') == 'global-env-token'
