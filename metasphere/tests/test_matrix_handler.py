"""Tests for ``metasphere.matrix.handler`` — nothing touches ~/.metasphere."""

from __future__ import annotations

import pytest

from metasphere.matrix import handler

ALEX = "@alex:hs"
BOT = "@bot:hs"
ROOM = "!room:hs"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    handler._TEXT_CACHE.clear()
    monkeypatch.setattr(handler, "_archive", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_archive_reaction", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_pin", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_ensure_session", lambda *a, **k: None)
    import metasphere.cli.failsafe as fs
    monkeypatch.setattr(fs, "maybe_courtesy_reply", lambda *a, **k: None)
    import metasphere.session as sess
    monkeypatch.setattr(sess, "_resolve_session", lambda a: "metasphere-orchestrator")


class Rec:
    def __init__(self, ret=True):
        self.submits, self.reacts, self.sends, self.acks, self.pending = [], [], [], [], []
        self.ret = ret

    def submit(self, user, text, **kw):
        self.submits.append((user, text, kw))
        return self.ret

    def kw(self):
        return dict(
            surface_id="matrix", allowed=[ALEX], self_user=BOT,
            tmux_submit=self.submit,
            reactor=lambda r, e, k: self.reacts.append((r, e, k)),
            sender_fn=lambda r, t, **k: self.sends.append((r, t, k)),
            write_pending_ack=lambda r, e: self.acks.append((r, e)),
            write_pending_inbound=lambda **k: self.pending.append(k),
        )


def msg(body, eid="$e1", sender=ALEX, **content):
    return {"type": "m.room.message", "event_id": eid, "sender": sender,
            "origin_server_ts": 1_700_000_000_000,
            "content": {"msgtype": "m.text", "body": body, **content}}


def test_numbered_list_arrives_intact_with_envelope():
    r = Rec()
    text = "1. first\n2. second\n3. third"
    assert handler.handle_event(msg(text), ROOM, **r.kw()) is True
    user, payload, kw = r.submits[0]
    assert user == "@alex" and payload == text
    assert kw["surface_id"] == "matrix" and kw["escape_prefix"] is False
    assert kw["defer_if_busy"] is False
    assert "--chat-id '!room:hs'" in kw["reply_command"]


def test_envelope_names_matrix_and_reply_command():
    from metasphere.telegram.inject import render_envelope
    r = Rec()
    handler.handle_event(msg("hi"), ROOM, **r.kw())
    kw = r.submits[0][2]
    env = render_envelope("matrix", "alex", "hi", reply_command=kw["reply_command"])
    assert env.startswith("[alex via matrix | reply: metasphere message send")


def test_ack_reaction_and_pending_ack():
    r = Rec()
    handler.handle_event(msg("hi", eid="$x"), ROOM, **r.kw())
    assert r.reacts == [(ROOM, "$x", "👀")]
    assert r.acks == [(ROOM, "$x")]


def test_non_allowed_sender_dropped():
    r = Rec()
    assert handler.handle_event(msg("hi", sender="@evil:hs"), ROOM, **r.kw()) is False
    assert not r.submits and not r.reacts


def test_own_message_dropped():
    r = Rec()
    k = r.kw()
    k["allowed"] = [ALEX, BOT]
    assert handler.handle_event(msg("echo", sender=BOT), ROOM, **k) is False
    assert not r.submits


def test_notice_and_edit_skipped():
    r = Rec()
    n = msg("n"); n["content"]["msgtype"] = "m.notice"
    e = msg("* fixed", **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$o"}})
    assert handler.handle_event(n, ROOM, **r.kw()) is False
    assert handler.handle_event(e, ROOM, **r.kw()) is False
    assert not r.submits


def test_unknown_event_type_ignored():
    r = Rec()
    ev = {"type": "m.room.topic", "sender": ALEX, "content": {}}
    assert handler.handle_event(ev, ROOM, **r.kw()) is False


def test_reply_quote_line_from_cache():
    r = Rec()
    handler.remember_text("$bot1", "Here is the plan\nwith two lines")
    ev = msg("ok do it", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$bot1"}}})
    handler.handle_event(ev, ROOM, **r.kw())
    assert r.submits[0][1] == '↩ replying to: "Here is the plan with two lines"\nok do it'


def test_reply_quote_fetches_when_uncached_and_truncates():
    r = Rec()
    k = r.kw()
    k["fetch_event"] = lambda room, eid: {"content": {"body": "x" * 300}}
    ev = msg("re", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$old"}}})
    handler.handle_event(ev, ROOM, **k)
    line = r.submits[0][1].split("\n")[0]
    assert line.endswith('..."') and line.startswith('↩ replying to: "xxx')


def test_reply_fetch_failure_degrades_to_plain():
    r = Rec()
    k = r.kw()
    def boom(room, eid):
        raise RuntimeError("net")
    k["fetch_event"] = boom
    ev = msg("re", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$old"}}})
    handler.handle_event(ev, ROOM, **k)
    assert r.submits[0][1] == "re"


def test_strip_reply_fallback():
    body = "> <@bot:hs> quoted line\n> more\n\nthe real reply"
    assert handler.strip_reply_fallback(body) == "the real reply"
    assert handler.strip_reply_fallback("> a normal quote start") == "> a normal quote start"


def test_reply_fallback_stripped_from_injected_text():
    r = Rec()
    handler.remember_text("$b", "orig")
    ev = msg("> <@bot:hs> orig\n\nanswer", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$b"}}})
    handler.handle_event(ev, ROOM, **r.kw())
    assert r.submits[0][1] == '↩ replying to: "orig"\nanswer'


def test_reaction_archived_not_injected(monkeypatch):
    seen = []
    monkeypatch.setattr(handler, "_archive_reaction", lambda *a, **k: seen.append(a))
    r = Rec()
    ev = {"type": "m.reaction", "event_id": "$re", "sender": ALEX,
          "origin_server_ts": 1, "content": {"m.relates_to": {
              "rel_type": "m.annotation", "event_id": "$t", "key": "👍"}}}
    assert handler.handle_event(ev, ROOM, **r.kw()) is True
    assert seen and seen[0][3] == "👍" and seen[0][4] == "$t"
    assert not r.submits


def test_delivery_failure_queues_pending():
    r = Rec(ret=False)
    handler.handle_event(msg("hi", eid="$p"), ROOM, **r.kw())
    assert r.pending and r.pending[0]["delivery_id"] == "matrix:$p:@orchestrator"
    assert r.pending[0]["chat_id"] == ROOM


def test_voice_attachment_downloaded_and_rendered(tmp_path):
    r = Rec()
    k = r.kw()
    k["attachments_root"] = tmp_path
    got = {}

    def dl(mxc, dest):
        got["mxc"], got["dest"] = mxc, dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"OggS")
        return 4
    k["downloader"] = dl
    ev = msg("voice.ogg", msgtype="m.audio", url="mxc://hs/v",
             info={"mimetype": "audio/ogg", "size": 4, "duration": 3000},
             **{"org.matrix.msc3245.voice": {}})
    ev["content"]["msgtype"] = "m.audio"
    handler.handle_event(ev, ROOM, **k)
    payload = r.submits[0][1]
    assert got["mxc"] == "mxc://hs/v" and got["dest"].name == "voice.oga"
    assert payload.startswith("[attachments]") and "voice" in payload


def test_file_with_caption_and_download_failure(tmp_path):
    r = Rec()
    k = r.kw()
    k["attachments_root"] = tmp_path
    def dl(mxc, dest):
        raise RuntimeError("gone")
    k["downloader"] = dl
    ev = msg("look at this", msgtype="m.file", url="mxc://hs/f", filename="r.pdf",
             info={"mimetype": "application/pdf"})
    ev["content"]["msgtype"] = "m.file"
    handler.handle_event(ev, ROOM, **k)
    payload = r.submits[0][1]
    assert payload.startswith("look at this")
    assert "download failed" in payload


def test_encrypted_media_reported_not_crashing(tmp_path):
    r = Rec()
    k = r.kw()
    k["attachments_root"] = tmp_path
    ev = msg("p.png", msgtype="m.image", file={"url": "mxc://x"})
    ev["content"]["msgtype"] = "m.image"
    handler.handle_event(ev, ROOM, **k)
    assert "E2EE" in r.submits[0][1]


def test_slash_command_answered_not_injected(monkeypatch):
    import metasphere.telegram.commands as cmds
    monkeypatch.setattr(cmds, "dispatch", lambda text, ctx: "pong")
    r = Rec()
    assert handler.handle_event(msg("/ping"), ROOM, **r.kw()) is True
    assert r.sends == [(ROOM, "pong", {})] and not r.submits
