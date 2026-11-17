"""Tests for ``metasphere.matrix.api`` (no network: requests is stubbed)."""

from __future__ import annotations

import json

import pytest

from metasphere.matrix import api


class _Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = content

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


@pytest.fixture
def cfg():
    return api.Config(homeserver="https://hs.example", access_token="SECRET",
                      user_id="@bot:hs", room_alex="!r:hs", alex="@alex:hs",
                      allowed=["@alex:hs"])


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)


def _use(monkeypatch, responses):
    s = _Session(responses)
    monkeypatch.setattr(api, "_SESSION", s)
    return s


def test_load_config_reads_env_and_token(tmp_path, monkeypatch):
    (tmp_path / "matrix.env").write_text(
        "MATRIX_HOMESERVER=https://hs.example/\nMATRIX_USER=@bot:hs\n"
        "MATRIX_ROOM_ALEX=!r:hs\nMATRIX_ALEX=@alex:hs\nMATRIX_ALLOWED=@bob:hs\n")
    tok = tmp_path / "t.token"
    tok.write_text(json.dumps({"access_token": "TOK"}))
    monkeypatch.setattr(api, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(api, "TOKEN_PATH", str(tok))
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    c = api.load_config()
    assert c.homeserver == "https://hs.example"
    assert c.access_token == "TOK"
    assert c.allowed == ["@alex:hs", "@bob:hs"]
    assert "TOK" not in repr(c)


def test_load_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(api, "TOKEN_PATH", str(tmp_path / "none"))
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    with pytest.raises(FileNotFoundError):
        api.load_config()


def test_markdown_numbered_list_renders_ol():
    html = api.markdown_to_html("1. one\n2. two\n\n**bold**")
    assert "<ol>" in html and "<li>one</li>" in html
    assert "<strong>bold</strong>" in html


def test_markdown_escapes_raw_html():
    assert "<script>" not in api.markdown_to_html("<script>x</script>")


def test_send_message_markdown_body_and_html(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"event_id": "$e1"})])
    ids = api.send_message("!r:hs", "1. a\n2. b", cfg=cfg)
    assert ids == ["$e1"]
    method, url, kw = s.calls[0]
    assert method == "PUT"
    assert "/rooms/%21r%3Ahs/send/m.room.message/" in url
    assert kw["json"]["body"] == "1. a\n2. b"
    assert kw["json"]["format"] == "org.matrix.custom.html"
    assert "<ol>" in kw["json"]["formatted_body"]
    assert kw["headers"]["Authorization"] == "Bearer SECRET"


def test_send_message_reply_relation(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"event_id": "$e"})])
    api.send_message("!r:hs", "hi", reply_to="$orig", cfg=cfg)
    rel = s.calls[0][2]["json"]["m.relates_to"]
    assert rel == {"m.in_reply_to": {"event_id": "$orig"}}


def test_send_message_html_passthrough(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"event_id": "$e"})])
    api.send_message("!r:hs", "", html="<b>x</b> &amp; y", cfg=cfg)
    j = s.calls[0][2]["json"]
    assert j["formatted_body"] == "<b>x</b> &amp; y"
    assert j["body"] == "x & y"


def test_send_message_chunks_long_text(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"event_id": "$a"}), _Resp(200, {"event_id": "$b"})])
    text = ("para\n\n" * 4000)
    ids = api.send_message("!r:hs", text, cfg=cfg)
    assert len(ids) == len(s.calls) >= 2
    assert s.calls[0][2]["json"]["body"].startswith("[1/")


def test_empty_text_rejected(cfg):
    with pytest.raises(ValueError):
        api.send_message("!r:hs", "", cfg=cfg)


def test_error_does_not_leak_token(monkeypatch, cfg):
    _use(monkeypatch, [_Resp(403, {"errcode": "M_FORBIDDEN", "error": "no"})])
    with pytest.raises(api.MatrixAPIError) as ei:
        api.send_message("!r:hs", "x", cfg=cfg)
    assert ei.value.status == 403 and "SECRET" not in str(ei.value)


def test_429_retries_then_succeeds(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(429, {"retry_after_ms": 10}), _Resp(200, {"event_id": "$z"})])
    assert api.send_message("!r:hs", "x", cfg=cfg) == ["$z"]
    assert len(s.calls) == 2


def test_5xx_gives_up_after_retries(monkeypatch, cfg):
    _use(monkeypatch, [_Resp(502), _Resp(502), _Resp(502)])
    with pytest.raises(api.MatrixAPIError) as ei:
        api.send_message("!r:hs", "x", cfg=cfg)
    assert ei.value.status == 502


def test_sync_initial_asks_for_empty_timeline(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"next_batch": "s1"})])
    api.sync(None, timeout_ms=0, cfg=cfg, initial=True)
    kw = s.calls[0][2]
    assert "since" not in kw["params"]
    assert json.loads(kw["params"]["filter"])["room"]["timeline"]["limit"] == 0


def test_sync_incremental_sends_since(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"next_batch": "s2"})])
    api.sync("s1", timeout_ms=1000, cfg=cfg)
    assert s.calls[0][2]["params"]["since"] == "s1"


def test_react_event_shape(monkeypatch, cfg):
    s = _use(monkeypatch, [_Resp(200, {"event_id": "$r"})])
    api.react("!r:hs", "$t", "👀", cfg=cfg)
    assert "/send/m.reaction/" in s.calls[0][1]
    assert s.calls[0][2]["json"]["m.relates_to"] == {
        "rel_type": "m.annotation", "event_id": "$t", "key": "👀"}


def test_send_file_voice_is_flagged_audio(monkeypatch, cfg, tmp_path):
    f = tmp_path / "v.ogg"
    f.write_bytes(b"OggS")
    s = _use(monkeypatch, [_Resp(200, {"content_uri": "mxc://hs/abc"}),
                           _Resp(200, {"event_id": "$v"})])
    assert api.send_file("!r:hs", str(f), voice=True, cfg=cfg) == "$v"
    up, send = s.calls
    assert "/_matrix/media/v3/upload" in up[1] and up[2]["data"] == b"OggS"
    c = send[2]["json"]
    assert c["msgtype"] == "m.audio" and c["url"] == "mxc://hs/abc"
    assert "org.matrix.msc3245.voice" in c


def test_send_file_document(monkeypatch, cfg, tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF")
    s = _use(monkeypatch, [_Resp(200, {"content_uri": "mxc://hs/d"}),
                           _Resp(200, {"event_id": "$d"})])
    api.send_file("!r:hs", str(f), caption="see", cfg=cfg)
    c = s.calls[1][2]["json"]
    assert c["msgtype"] == "m.file" and c["body"] == "see" and c["filename"] == "r.pdf"


def test_download_media_falls_back_to_v3(monkeypatch, cfg, tmp_path):
    s = _use(monkeypatch, [_Resp(404, {"errcode": "M_UNRECOGNIZED"}),
                           _Resp(200, content=b"DATA")])
    dest = tmp_path / "x" / "f.bin"
    assert api.download_media("mxc://hs/abc", dest, cfg=cfg) == 4
    assert dest.read_bytes() == b"DATA"
    assert "/_matrix/client/v1/media/download/" in s.calls[0][1]
    assert "/_matrix/media/v3/download/" in s.calls[1][1]


def test_download_media_rejects_non_mxc(cfg, tmp_path):
    with pytest.raises(ValueError):
        api.download_media("https://x/y", tmp_path / "f", cfg=cfg)
