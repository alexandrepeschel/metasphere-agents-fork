"""Tests for ``metasphere.matrix.poller`` — sync-token persistence, no replay."""

from __future__ import annotations

import queue
import threading

import pytest

from metasphere.matrix import api, poller

ALEX = "@alex:hs"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    poller._reset_for_tests()
    yield
    poller._reset_for_tests()


CFG = api.Config(homeserver="h", access_token="t", user_id="@bot:hs", allowed=[ALEX])


def _resp(batch, *evs, room="!r:hs"):
    return {"next_batch": batch, "rooms": {"join": {room: {"timeline": {"events": list(evs)}}}}}


def _ev(eid):
    return {"type": "m.room.message", "event_id": eid, "sender": ALEX,
            "content": {"msgtype": "m.text", "body": eid}}


def test_extract_events_flattens_joined_rooms():
    r = _resp("s", _ev("$1"), _ev("$2"))
    assert [(rm, e["event_id"]) for rm, e in poller.extract_events(r)] == [("!r:hs", "$1"), ("!r:hs", "$2")]
    assert poller.extract_events({"next_batch": "x"}) == []


def test_state_roundtrip():
    poller.save_state("matrix", "s9", ["$a"])
    assert poller.load_state("matrix") == {"next_batch": "s9", "seen": ["$a"]}


def test_drain_handles_events_then_persists_token():
    q = queue.Queue()
    q.put(("s2", poller.extract_events(_resp("s2", _ev("$1"), _ev("$2")))))
    poller._QUEUES["matrix"] = q
    poller._WORKERS["matrix"] = threading.Thread(target=lambda: None)
    got = []
    # worker "alive" check: use a live dummy thread
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, daemon=True); t.start()
    poller._WORKERS["matrix"] = t
    n = poller.run_poll_iteration("matrix", "@orchestrator", cfg=CFG,
                                  on_event=lambda room, e: got.append(e["event_id"]))
    ev.set()
    assert n == 2 and got == ["$1", "$2"]
    st = poller.load_state("matrix")
    assert st["next_batch"] == "s2" and st["seen"] == ["$1", "$2"]


def test_seen_events_not_replayed_after_restart():
    poller.save_state("matrix", "s1", ["$1"])
    q = queue.Queue()
    q.put(("s2", poller.extract_events(_resp("s2", _ev("$1"), _ev("$3")))))  # $1 replayed by server
    poller._QUEUES["matrix"] = q
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, daemon=True); t.start()
    poller._WORKERS["matrix"] = t
    got = []
    poller.run_poll_iteration("matrix", "@orchestrator", cfg=CFG,
                              on_event=lambda room, e: got.append(e["event_id"]))
    ev.set()
    assert got == ["$3"]


def test_handler_exception_does_not_stall_batch():
    q = queue.Queue()
    q.put(("s2", poller.extract_events(_resp("s2", _ev("$1"), _ev("$2")))))
    poller._QUEUES["matrix"] = q
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, daemon=True); t.start()
    poller._WORKERS["matrix"] = t

    def cb(room, e):
        if e["event_id"] == "$1":
            raise RuntimeError("x")
    n = poller.run_poll_iteration("matrix", "@orchestrator", cfg=CFG, on_event=cb)
    ev.set()
    assert n == 1 and poller.load_state("matrix")["next_batch"] == "s2"


def test_worker_first_run_discards_history_and_saves_token():
    calls = []

    def sync_fn(since, **kw):
        calls.append((since, kw))
        if since is None:
            return {"next_batch": "s0", "rooms": {"join": {"!r:hs": {"timeline": {"events": [_ev("$old")]}}}}}
        stop.set()
        return _resp("s1", _ev("$new"))

    q = queue.Queue()
    stop = threading.Event()
    poller._worker("matrix", q, stop, sync_fn, lambda s: None)
    assert calls[0][0] is None and calls[0][1]["initial"] is True
    assert calls[1][0] == "s0"
    assert poller.load_state("matrix")["next_batch"] == "s0"
    nb, events = q.get_nowait()
    assert nb == "s1" and [e["event_id"] for _, e in events] == ["$new"]  # $old never queued


def test_worker_resumes_from_persisted_token():
    poller.save_state("matrix", "sSAVED", [])
    seen = []
    stop = threading.Event()

    def sync_fn(since, **kw):
        seen.append(since)
        stop.set()
        return {"next_batch": "sNEXT"}
    poller._worker("matrix", queue.Queue(), stop, sync_fn, lambda s: None)
    assert seen == ["sSAVED"]


def test_worker_backs_off_on_error_and_recovers():
    poller.save_state("matrix", "s1", [])
    stop = threading.Event()
    sleeps = []
    n = {"i": 0}

    def sync_fn(since, **kw):
        n["i"] += 1
        if n["i"] == 1:
            raise api.MatrixAPIError(500, "M_UNKNOWN", "boom")
        stop.set()
        return {"next_batch": "s2"}
    poller._worker("matrix", queue.Queue(), stop, sync_fn, sleeps.append)
    assert sleeps == [1.0] and n["i"] == 2
