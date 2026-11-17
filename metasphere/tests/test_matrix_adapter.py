"""MatrixAdapter + daemon wiring + CLI dispatch + posthook surface routing."""

from __future__ import annotations

import json
from unittest.mock import patch

from metasphere.gateway import daemon as gw_daemon
from metasphere.gateway.adapter import SurfaceAdapter
from metasphere.gateway.adapters.matrix import MatrixAdapter


def test_adapter_satisfies_protocol():
    a = MatrixAdapter()
    assert isinstance(a, SurfaceAdapter)
    assert a.surface_type == "matrix" and a.surface_id == "matrix"
    assert a._target == "@orchestrator"
    assert MatrixAdapter("matrix-relay")._target == "@relay"


def test_receive_delegates_to_poller():
    with patch("metasphere.gateway.adapters.matrix._poller.run_poll_iteration", return_value=2) as r:
        assert MatrixAdapter().receive(timeout=5) == 2
    assert r.call_args.args == ("matrix", "@orchestrator")


def test_send_delegates_to_api():
    with patch("metasphere.gateway.adapters.matrix._api.send_message") as s, \
         patch("metasphere.gateway.adapters.matrix._api.load_config", return_value="CFG"):
        MatrixAdapter().send("!r:hs", "hi")
    s.assert_called_once_with("!r:hs", "hi", cfg="CFG")


def test_discover_requires_resolvable_config(tmp_path):
    (tmp_path / "matrix.env").write_text("MATRIX_HOMESERVER=https://h\n")
    (tmp_path / "matrix-bad.env").write_text("X=1\n")
    with patch("metasphere.matrix.api.load_config") as lc:
        lc.side_effect = lambda sid: None if sid == "matrix" else (_ for _ in ()).throw(FileNotFoundError())
        assert gw_daemon._discover_matrix_surface_ids(str(tmp_path)) == ["matrix"]


def test_default_adapters_include_matrix(monkeypatch):
    monkeypatch.setattr(gw_daemon, "_discover_matrix_surface_ids", lambda *a, **k: ["matrix"])
    monkeypatch.setattr(gw_daemon, "_discover_slack_surface_ids", lambda *a, **k: [])
    monkeypatch.setattr(gw_daemon, "_discover_telegram_surface_ids", lambda *a, **k: [])
    monkeypatch.setattr(gw_daemon, "_telegram_configured", lambda: False)
    assert [a.surface_id for a in gw_daemon._default_adapters()] == ["matrix"]


# ---- message CLI -----------------------------------------------------------

def test_message_send_matrix_dispatch(monkeypatch, capsys):
    from metasphere.cli import message
    sent = []
    monkeypatch.setattr("metasphere.matrix.api.send_with_cc", lambda room, text, cfg=None, **k: sent.append((room, text)))
    monkeypatch.setattr("metasphere.matrix.api.load_config", lambda sid="matrix": object())
    monkeypatch.setattr("metasphere.telegram.archiver.archive_outgoing", lambda *a, **k: None)
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@someagent")
    rc = message._dispatch("matrix", "!r:hs", "hello", sender_agent="@someagent")
    assert rc == 0 and sent == [("!r:hs", "hello")]


def test_auto_surface_borrows_orchestrator_pin_surface_only(monkeypatch):
    from metasphere.cli import message
    pins = {"@orchestrator": {"surface_id": "matrix", "chat_id": "!room:hs"}}
    monkeypatch.setattr("metasphere.routing.active.get_active_conversation",
                        lambda agent, paths: pins.get(agent))
    assert message._resolve_auto_surface("@cron-job") == ("matrix", None)
    assert message._resolve_auto_surface("@orchestrator") == ("matrix", "!room:hs")


def test_message_send_to_alex_follows_pin_to_matrix(monkeypatch):
    from metasphere.cli import message
    monkeypatch.setattr(message, "_resolve_auto_surface", lambda agent: ("matrix", None))
    monkeypatch.setattr(message._contacts, "lookup_contact", lambda n, s: "!dm:hs" if s == "matrix" else "1")
    seen = {}
    monkeypatch.setattr(message, "_dispatch", lambda sid, chat, text, **k: seen.update(sid=sid, chat=chat) or 0)
    assert message.main(["send", "hi", "--surface", "auto", "--to", "alex"]) == 0
    assert seen == {"sid": "matrix", "chat": "!dm:hs"}


# ---- matrix CLI ------------------------------------------------------------

def test_matrix_cli_send_and_voice(monkeypatch, tmp_path, capsys):
    from metasphere.cli import matrix as cli
    from metasphere.matrix import api
    cfg = api.Config("h", "t", room_alex="!dm:hs")
    monkeypatch.setattr(api, "load_config", lambda sid="matrix": cfg)
    calls = []
    monkeypatch.setattr(api, "send_with_cc", lambda room, text=None, **k: calls.append((room, text, k)) or "$e")
    monkeypatch.setattr(cli, "_mark_explicit", lambda a: None)
    monkeypatch.setattr("metasphere.telegram.archiver.archive_outgoing", lambda *a, **k: None)
    assert cli.main(["send", "1. a\n2. b"]) == 0
    assert calls[0][:2] == ("!dm:hs", "1. a\n2. b")
    f = tmp_path / "v.ogg"; f.write_bytes(b"x")
    assert cli.main(["send-document", str(f), "--voice"]) == 0
    assert calls[1][2]["voice"] is True and calls[1][2]["document_path"] == str(f)
    assert cli.main(["send-document", str(tmp_path / "nope")]) == 2


# ---- posthook --------------------------------------------------------------

def test_posthook_routes_to_matrix_when_pinned(tmp_paths, monkeypatch):
    from metasphere import posthook
    from metasphere.routing.active import set_active_conversation
    set_active_conversation("@orchestrator", "matrix", "!dm:hs", tmp_paths)
    sent = []
    monkeypatch.setattr("metasphere.matrix.api.send_message", lambda room, text, cfg=None, **k: sent.append((room, text)))
    monkeypatch.setattr("metasphere.matrix.api.load_config", lambda sid="matrix": object())
    posthook.route_to_telegram("final answer", tmp_paths)
    assert sent == [("!dm:hs", "final answer")]
    posthook.route_to_telegram("final answer", tmp_paths)  # dedupe
    assert len(sent) == 1


def test_posthook_defaults_to_telegram_without_matrix_pin(tmp_paths, monkeypatch):
    from metasphere import posthook
    sent = []
    monkeypatch.setattr(posthook, "_resolve_chat_id", lambda p: "42")
    monkeypatch.setattr("metasphere.telegram.api.send_with_cc", lambda cid, text, **k: sent.append((cid, text)))
    posthook.route_to_telegram("hello", tmp_paths)
    assert sent == [("42", "hello")]


def test_matrix_ack_reaction(tmp_paths, monkeypatch):
    from metasphere import posthook
    tmp_paths.state.mkdir(parents=True, exist_ok=True)
    (tmp_paths.state / "matrix_pending_ack.json").write_text(json.dumps({"room_id": "!r:hs", "event_id": "$e"}))
    reacts = []
    monkeypatch.setattr("metasphere.matrix.api.react", lambda r, e, k, **kw: reacts.append((r, e, k)))
    posthook.consume_pending_ack(tmp_paths)
    assert reacts == [("!r:hs", "$e", "👍")]
    assert not (tmp_paths.state / "matrix_pending_ack.json").exists()
