"""Matrix ``/sync`` driver.

The gateway daemon polls adapters sequentially, and Telegram already
holds the loop for up to 25 s per tick, so a second long-poll in the
same loop would delay Telegram. Like the Slack adapter, a background
thread (one per surface) runs the ``/sync`` long-poll and queues each
batch; :func:`run_poll_iteration` drains the queue on the daemon tick.

Restart safety: the ``next_batch`` token is persisted only AFTER a batch
has been handled, together with the most recent handled event ids, so a
restart resumes where it stopped and a crash mid-batch never double-
delivers. A first-ever run discards history (initial sync with an empty
timeline) so old messages are never replayed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..io import atomic_write_text
from . import api, handler

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.path.expanduser("~/.metasphere/state"))
SEEN_MAX = 300

_QUEUES: dict[str, "queue.Queue[Any]"] = {}
_WORKERS: dict[str, threading.Thread] = {}
_STOPS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()


def state_path(surface_id: str) -> Path:
    return STATE_DIR / f"matrix_sync_{surface_id}.json"


def load_state(surface_id: str) -> dict:
    try:
        d = json.loads(state_path(surface_id).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(surface_id: str, next_batch: str, seen: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(surface_id),
        json.dumps({"next_batch": next_batch, "seen": seen[-SEEN_MAX:]}),
    )


def extract_events(sync_resp: dict) -> list[tuple[str, dict]]:
    """``[(room_id, event)]`` from joined-room timelines, in order."""
    out: list[tuple[str, dict]] = []
    joined = ((sync_resp.get("rooms") or {}).get("join")) or {}
    for room_id, room in joined.items():
        for ev in ((room.get("timeline") or {}).get("events")) or []:
            out.append((room_id, ev))
    return out


def _worker(surface_id: str, evt_queue: "queue.Queue[Any]", stop: threading.Event,
            sync_fn: Callable[..., dict], sleep: Callable[[float], None]) -> None:
    state = load_state(surface_id)
    since: Optional[str] = state.get("next_batch") or None
    backoff = 1.0
    while not stop.is_set():
        try:
            if since is None:
                resp = sync_fn(None, timeout_ms=0, initial=True)
                since = resp.get("next_batch")
                if since:
                    save_state(surface_id, since, [])  # history discarded on purpose
                backoff = 1.0
                continue
            resp = sync_fn(since, timeout_ms=25000)
            nb = resp.get("next_batch") or since
            evt_queue.put((nb, extract_events(resp)))
            since = nb
            backoff = 1.0
        except api.MatrixAPIError as e:
            logger.warning("matrix sync failed: %s", e)
            wait = 60.0 if e.status in (401, 403) else backoff
            backoff = min(backoff * 2, 60.0)
            sleep(wait)
        except Exception:  # noqa: BLE001
            logger.exception("matrix sync worker error")
            sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _start_worker(surface_id: str, *, sync_fn: Optional[Callable[..., dict]] = None,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    with _LOCK:
        t = _WORKERS.get(surface_id)
        if t is not None and t.is_alive():
            return
        q: "queue.Queue[Any]" = _QUEUES.setdefault(surface_id, queue.Queue())
        stop = threading.Event()
        _STOPS[surface_id] = stop
        if sync_fn is None:
            cfg = api.load_config(surface_id)
            sync_fn = lambda since, **kw: api.sync(since, cfg=cfg, **kw)  # noqa: E731
        t = threading.Thread(
            target=_worker, args=(surface_id, q, stop, sync_fn, sleep),
            daemon=True, name=f"matrix-{surface_id}",
        )
        t.start()
        _WORKERS[surface_id] = t


def run_poll_iteration(
    surface_id: str,
    target_agent_id: str,
    timeout: int = 1,  # noqa: ARG001 — the worker owns the long-poll
    *,
    sync_fn: Optional[Callable[..., dict]] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
    cfg: Optional[api.Config] = None,
    **handler_kwargs,
) -> int:
    """Drain queued batches; returns the count of events handled."""
    _start_worker(surface_id, sync_fn=sync_fn)
    q = _QUEUES.get(surface_id)
    if q is None:
        return 0
    cfg = cfg or api.load_config(surface_id)
    n = 0
    while True:
        try:
            next_batch, events = q.get_nowait()
        except queue.Empty:
            break
        state = load_state(surface_id)
        seen: list[str] = list(state.get("seen") or [])
        seen_set = set(seen)
        for room_id, ev in events:
            eid = ev.get("event_id") or ""
            if eid and eid in seen_set:
                continue
            try:
                if on_event is not None:
                    on_event(room_id, ev)
                    ok = True
                else:
                    ok = handler.handle_event(
                        ev, room_id, surface_id=surface_id,
                        target_agent_id=target_agent_id, allowed=cfg.allowed,
                        self_user=cfg.user_id, **handler_kwargs,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("matrix handler raised for %s", eid)
                ok = False
            if eid:
                seen.append(eid)
                seen_set.add(eid)
            if ok:
                n += 1
        save_state(surface_id, next_batch, seen)
    return n


def _reset_for_tests() -> None:
    with _LOCK:
        for s in _STOPS.values():
            s.set()
        _QUEUES.clear()
        _WORKERS.clear()
        _STOPS.clear()
