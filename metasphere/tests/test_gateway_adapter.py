"""Tests for the gateway SurfaceAdapter interface and TelegramAdapter.

Scope: structural prep only — the adapter refactor must not change
runtime behavior. These tests cover:

- :class:`TelegramAdapter` satisfies the :class:`SurfaceAdapter` Protocol.
- :class:`TelegramAdapter.receive` delegates to ``poller.run_poll_iteration``.
- :class:`TelegramAdapter.send` delegates to ``api.send_message``.
- The daemon's default ``poll_fn`` drives every registered adapter once
  per tick (so a custom adapter list is honoured).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from metasphere.gateway import daemon as gw_daemon
from metasphere.gateway.adapter import SurfaceAdapter
from metasphere.gateway.adapters.telegram import TelegramAdapter
from metasphere.paths import Paths


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------



@pytest.fixture(autouse=True)
def _no_host_matrix_surfaces(monkeypatch):
    """Host ``matrix.env`` must not leak a MatrixAdapter into default-adapter
    assertions (or start a real /sync worker from a test)."""
    monkeypatch.setattr(gw_daemon, "_discover_matrix_surface_ids", lambda *a, **k: [])

def test_telegram_adapter_implements_surface_adapter_protocol():
    adapter = TelegramAdapter()
    assert isinstance(adapter, SurfaceAdapter)
    assert adapter.surface_type == "telegram"


def test_surface_type_is_class_attribute():
    """``surface_type`` is set on the class so the daemon can look it up
    without instantiating an adapter (e.g. when reporting which surfaces
    are wired)."""
    assert TelegramAdapter.surface_type == "telegram"


# ---------------------------------------------------------------------------
# TelegramAdapter — receive() delegates to poller
# ---------------------------------------------------------------------------


def test_telegram_adapter_receive_calls_run_poll_iteration():
    adapter = TelegramAdapter()
    with patch(
        "metasphere.gateway.adapters.telegram.poller.run_poll_iteration",
        return_value=3,
    ) as run_poll:
        n = adapter.receive(timeout=5)

    assert n == 3
    run_poll.assert_called_once()
    kwargs = run_poll.call_args.kwargs
    assert kwargs["timeout"] == 5
    # on_error wired to the adapter's stored callback (None unless set).
    assert kwargs["on_error"] is None


def test_telegram_adapter_passes_handler_error_callback():
    """``on_handler_error`` from __init__ propagates to the poller's
    ``on_error`` so per-update failures keep getting logged."""
    callback = MagicMock()
    adapter = TelegramAdapter(on_handler_error=callback)
    with patch(
        "metasphere.gateway.adapters.telegram.poller.run_poll_iteration",
        return_value=0,
    ) as run_poll:
        adapter.receive()

    assert run_poll.call_args.kwargs["on_error"] is callback


# ---------------------------------------------------------------------------
# TelegramAdapter — send() delegates to api.send_message
# ---------------------------------------------------------------------------


def test_telegram_adapter_send_calls_api_send_message():
    adapter = TelegramAdapter()
    with patch(
        "metasphere.gateway.adapters.telegram.api.send_message"
    ) as send_message:
        adapter.send(12345, "hello")

    send_message.assert_called_once_with(12345, "hello", surface_id="telegram")


# ---------------------------------------------------------------------------
# Daemon routes through registered adapters
# ---------------------------------------------------------------------------


def test_run_daemon_drives_registered_adapter_each_tick(tmp_paths: Paths):
    """When ``adapters=[fake]`` is passed, the daemon's default poll_fn
    must call ``fake.receive`` once per loop iteration. Proves the loop
    is no longer hard-coded to telegram."""
    fake = MagicMock(spec=SurfaceAdapter)
    fake.surface_type = "fake"
    fake.receive.return_value = 0

    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.0,
            watchdog_interval=10_000.0,  # don't fire watchdog in this test
            stop=stop,
            adapters=[fake],
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
        )

    # Loop ran 3 iterations (stop returns True on the 4th call).
    assert iterations["n"] == 4
    assert fake.receive.call_count == 3


def test_run_daemon_drives_multiple_adapters_per_tick(tmp_paths: Paths):
    """Two registered adapters → both get their ``receive`` called every
    tick; counts sum into the daemon's poll-tick total."""
    a = MagicMock(spec=SurfaceAdapter)
    a.surface_type = "alpha"
    a.receive.return_value = 1
    b = MagicMock(spec=SurfaceAdapter)
    b.surface_type = "beta"
    b.receive.return_value = 2

    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 2

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.0,
            watchdog_interval=10_000.0,
            stop=stop,
            adapters=[a, b],
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
        )

    assert a.receive.call_count == 2
    assert b.receive.call_count == 2


def test_run_daemon_default_adapters_includes_telegram(tmp_paths: Paths, monkeypatch):
    """Smoke test: when no ``adapters`` are passed, the default list
    contains a TelegramAdapter so production behavior is unchanged.

    Slack discovery is stubbed to empty so the assertion is independent of
    whatever ``slack*.env`` files exist in the operator's real
    ``~/.metasphere/config`` (the daemon auto-discovers one SlackAdapter per
    file — see ``_discover_slack_surface_ids``)."""
    monkeypatch.setattr(gw_daemon, "_discover_slack_surface_ids", lambda: [])
    defaults = gw_daemon._default_adapters()

    assert len(defaults) == 1
    assert isinstance(defaults[0], TelegramAdapter)
    assert defaults[0].surface_type == "telegram"


def test_poll_once_routes_through_telegram_adapter(monkeypatch):
    """``_poll_once`` is the default ``poll_fn`` and must drive the
    telegram adapter (not call the poller directly), so additional
    adapters added to ``_default_adapters`` are also driven.

    Slack discovery is stubbed empty: otherwise, on a host with a real
    ``slack*.env``, ``_default_adapters`` builds a live SlackAdapter whose
    ``receive`` opens a real Socket Mode websocket during the test — which
    would both hit the network in a unit test and contend with the running
    daemon's connection."""
    monkeypatch.setattr(gw_daemon, "_discover_slack_surface_ids", lambda: [])
    with patch(
        "metasphere.gateway.adapters.telegram.poller.run_poll_iteration",
        return_value=7,
    ) as run_poll:
        n = gw_daemon._poll_once(timeout=2)

    assert n == 7
    run_poll.assert_called_once()
    assert run_poll.call_args.kwargs["timeout"] == 2


def test_poll_once_uses_provided_adapters_without_rebuilding_defaults():
    """When ``adapters=`` is passed, ``_poll_once`` must drive that
    exact list and NOT call ``_default_adapters`` — otherwise stateful
    adapters (auth tokens, websocket handles) would be discarded
    every tick."""
    fake = MagicMock(spec=SurfaceAdapter)
    fake.surface_type = "fake"
    fake.receive.return_value = 4

    with patch.object(gw_daemon, "_default_adapters") as defaults:
        n = gw_daemon._poll_once(timeout=3, adapters=[fake])

    assert n == 4
    fake.receive.assert_called_once_with(timeout=3)
    defaults.assert_not_called()


def test_poll_once_isolates_failing_adapter():
    """One adapter raising must NOT starve the others. Regression: on a
    Slack-only install ``TelegramAdapter.receive`` raised every tick (no
    token), aborting the loop before Slack was polled, so the socket worker
    never started. ``_poll_once`` wraps each ``receive`` and continues."""
    boom = MagicMock(spec=SurfaceAdapter)
    boom.surface_type = "telegram"
    boom.surface_id = "telegram"
    boom.receive.side_effect = RuntimeError("no telegram bot token found")

    ok = MagicMock(spec=SurfaceAdapter)
    ok.surface_type = "slack"
    ok.surface_id = "slack-relay"
    ok.receive.return_value = 5

    n = gw_daemon._poll_once(timeout=2, adapters=[boom, ok])

    assert n == 5  # the failing adapter contributes 0, the healthy one is polled
    boom.receive.assert_called_once_with(timeout=2)
    ok.receive.assert_called_once_with(timeout=2)


def test_run_daemon_binds_adapter_list_once_across_ticks(tmp_paths: Paths):
    """The adapter list must be constructed ONCE inside ``run_daemon``
    and the SAME instances reused every tick. Proves stateful adapters
    (e.g. websocket connections) survive across the loop instead of
    being reinstantiated per iteration."""
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 3

    fake = MagicMock(spec=SurfaceAdapter)
    fake.surface_type = "fake"
    fake.receive.return_value = 0

    factory = MagicMock(return_value=[fake])

    with patch.object(gw_daemon, "ensure_session"), \
         patch.object(gw_daemon, "run_watchdog"), \
         patch.object(gw_daemon, "_default_adapters", factory):
        gw_daemon.run_daemon(
            tmp_paths,
            poll_interval=0.0,
            watchdog_interval=10_000.0,
            stop=stop,
            sleep_fn=lambda s: None,
            time_fn=lambda: 0.0,
        )

    # _default_adapters() called exactly once at run_daemon entry —
    # not once per tick. The asymmetry-fix pre-condition.
    assert factory.call_count == 1
    # Same adapter instance driven every iteration.
    assert fake.receive.call_count == 3
