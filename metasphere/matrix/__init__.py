"""Matrix surface adapter.

Mirrors :mod:`metasphere.telegram` / :mod:`metasphere.slack` for Matrix
(Element). Plain HTTPS against the client-server API via ``requests`` —
no matrix-nio, no E2EE (the bot's DM room with the operator is created
unencrypted).

- ``api``     client-server API: ``/sync``, send (markdown + HTML), media
              upload/download, reactions.
- ``poller``  background ``/sync`` long-poll worker + persisted sync token.
- ``handler`` inbound event -> archive / pin / inject into the agent REPL.
- ``routing`` access list (who may talk to the agent) + reply command.

Config lives in ``~/.metasphere/config/matrix.env`` (homeserver, bot id,
DM room, operator id); the access token in
``~/.metasphere/secrets/matrix-clay.token`` (JSON, mode 600).
"""
