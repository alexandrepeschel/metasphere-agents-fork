"""Tests for ``metasphere message send`` — cross-surface dispatch CLI."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from metasphere import contacts as _contacts
from metasphere.cli import message as _msg_cli
from metasphere.paths import Paths
from metasphere.routing.active import set_active_conversation


@pytest.fixture(autouse=True)
def _isolate_contacts_cache():
    _contacts.clear_cache()
    yield
    _contacts.clear_cache()


@pytest.fixture
def _wire_paths(tmp_paths: Paths, monkeypatch):
    """Make ``metasphere.paths.resolve`` and helper modules read
    ``tmp_paths`` instead of the real ``~/.metasphere``."""
    from metasphere import paths as _paths_module

    monkeypatch.setattr(_paths_module, "resolve", lambda: tmp_paths)
    # Set agent id so cmd_send's calling-agent resolver picks it up.
    monkeypatch.setenv("METASPHERE_AGENT_ID", "@orchestrator")
    return tmp_paths


def _run(argv):
    return _msg_cli.main(argv)


def test_message_send_surface_auto_uses_active_conversation(_wire_paths, capsys):
    """``--surface auto`` reads the pin and dispatches there."""
    set_active_conversation(
        "@orchestrator", "telegram", 99999, _wire_paths,
    )
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ) as archive_mock:
        rc = _run(["send", "hello world", "--surface", "auto"])
    assert rc == 0
    send_mock.assert_called_once_with(99999, "hello world", surface_id="telegram")
    archive_mock.assert_called_once()


def test_message_send_surface_explicit_overrides_active(_wire_paths):
    """An explicit ``--surface telegram-relay`` wins over the pin
    (operator override path)."""
    set_active_conversation(
        "@orchestrator", "telegram", 1, _wire_paths,
    )
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _run([
            "send", "explicit override",
            "--surface", "telegram-relay",
            "--chat-id", "55555",
        ])
    assert rc == 0
    send_mock.assert_called_once_with(
        55555, "explicit override", surface_id="telegram-relay",
    )


def test_message_send_targets_explicit_telegram_thread(_wire_paths):
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _run([
            "send", "topic reply",
            "--surface", "telegram-relay",
            "--chat-id", "-100123",
            "--thread-id", "77",
        ])
    assert rc == 0
    send_mock.assert_called_once_with(
        -100123,
        "topic reply",
        surface_id="telegram-relay",
        message_thread_id=77,
    )


def test_message_send_positional_newline_escapes_render_as_paragraphs(
    _wire_paths,
):
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _run([
            "send", r"first paragraph\n\nsecond paragraph",
            "--surface", "telegram",
            "--chat-id", "55555",
        ])
    assert rc == 0
    send_mock.assert_called_once_with(
        55555, "first paragraph\n\nsecond paragraph", surface_id="telegram",
    )


def test_message_send_auto_fallback_to_legacy_default_when_no_pointer(
    _wire_paths, capsys,
):
    """No active_conversation file → fall back to default-recipient
    Telegram chat id + warn that the legacy default fired."""
    (_wire_paths.root / "ADDRESSBOOK.yaml").write_text(
        "default-recipient: alpha\n"
        "contacts:\n"
        "  alpha:\n"
        "    telegram: 7777\n"
    )
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _run(["send", "fallback msg", "--surface", "auto"])
    assert rc == 0
    send_mock.assert_called_once_with(7777, "fallback msg", surface_id="telegram")
    err = capsys.readouterr().err
    assert "active_conversation" in err
    assert "legacy" in err


def test_telegram_send_legacy_wrapper_still_works(_wire_paths, capsys):
    """The old ``metasphere telegram send`` CLI keeps shipping
    messages and stamps ``surface_id='telegram'`` so the back-compat
    promise holds."""
    from metasphere.cli import telegram as _tg_cli

    (_wire_paths.root / "ADDRESSBOOK.yaml").write_text(
        "default-recipient: alpha\n"
        "contacts:\n"
        "  alpha:\n"
        "    telegram: 3333\n"
    )
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _tg_cli.main(["send", "legacy path"])
    assert rc == 0
    send_mock.assert_called_once_with(3333, "legacy path", surface_id="telegram")


def test_message_send_routes_slack_via_slack_api(_wire_paths):
    """An explicit ``--surface slack-relay`` routes through
    ``metasphere.slack.api.send_with_cc``."""
    set_active_conversation(
        "@orchestrator", "telegram", 1, _wire_paths,
    )
    with patch(
        "metasphere.slack.api.send_with_cc"
    ) as send_mock:
        rc = _run([
            "send", "hello slack",
            "--surface", "slack-relay",
            "--chat-id", "C9999",
        ])
    assert rc == 0
    send_mock.assert_called_once_with(
        "slack-relay", "C9999", "hello slack",
        sender_agent_id="@orchestrator",
    )


def test_message_send_auto_resolves_to_slack_from_pin(_wire_paths):
    """LOAD-BEARING: ``--surface auto`` reads a SLACK active_conversation pin
    and dispatches through the slack api to the pinned channel — this is what
    makes the routed agent's reply land back in Slack, not Telegram."""
    set_active_conversation(
        "@orchestrator", "slack-explorer", "C0BC2EV7SFM", _wire_paths,
    )
    with patch(
        "metasphere.slack.api.send_with_cc"
    ) as send_mock:
        rc = _run(["send", "reply into slack", "--surface", "auto"])
    assert rc == 0
    send_mock.assert_called_once_with(
        "slack-explorer", "C0BC2EV7SFM", "reply into slack",
        sender_agent_id="@orchestrator",
    )


def test_message_send_addressbook_lookup_surface_aware(_wire_paths):
    """``--to <name>`` resolves the contact via the per-surface key
    when the explicit surface is given."""
    (_wire_paths.root / "ADDRESSBOOK.yaml").write_text(
        "contacts:\n"
        "  alice:\n"
        "    telegram: 1111\n"
        "    telegram-cluster-1: 2222\n"
    )
    with patch(
        "metasphere.telegram.api.send_with_cc"
    ) as send_mock, patch(
        "metasphere.telegram.archiver.archive_outgoing"
    ):
        rc = _run([
            "send", "to cluster",
            "--surface", "telegram-cluster-1",
            "--to", "alice",
        ])
    assert rc == 0
    send_mock.assert_called_once_with(
        2222, "to cluster", surface_id="telegram-cluster-1",
    )
