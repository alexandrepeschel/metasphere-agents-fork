from metasphere.matrix import routing


def test_allow_list_fails_closed():
    assert routing.is_allowed("@a:hs", ["@a:hs"])
    assert not routing.is_allowed("@b:hs", ["@a:hs"])
    assert not routing.is_allowed("@a:hs", [])
    assert not routing.is_allowed("", ["@a:hs"])


def test_derive_target_agent():
    assert routing.derive_target_agent("matrix") == "@orchestrator"
    assert routing.derive_target_agent("matrix-relay") == "@relay"


def test_reply_command_is_shell_safe():
    cmd = routing.reply_command("matrix", "!abc:matrix.org")
    assert cmd == "metasphere message send --surface matrix --chat-id '!abc:matrix.org' \"<reply>\""


def test_short_name():
    assert routing.short_name("@alexandre_peschel:matrix.org") == "alexandre_peschel"
