# Matrix surface: build report (night of 17-18 Nov 2026)

Branch `local-stack-20260924`, pushed to `alex-fork` only (never `origin`).

## Blocker found: the DM room was end-to-end encrypted

The spec said the DM room was created unencrypted. It was not: `GET .../state/m.room.encryption` on the old room returned `m.megolm.v1.aes-sha2`. Alex's messages in it arrive as `m.room.encrypted`, which the bot cannot read (no Olm/Megolm keys, no matrix-nio). Encryption cannot be switched off once on.

What I did (reversible, nothing deleted):
- Created a new room **"Clay (unencrypted)"** (`trusted_private_chat`, `is_direct`, verified no `m.room.encryption` state) and invited Alex.
- `MATRIX_ROOM_ALEX` in `~/.metasphere/config/matrix.env` and the `alex.matrix` entry in `ADDRESSBOOK.yaml` now point at the new room. The old id is kept as `MATRIX_ROOM_ALEX_ENCRYPTED_OLD`.
- Posted a short note in the old room telling him to accept the new invite.
- Added a guard: if an allowed user writes into any encrypted room, the bot says once per room that it cannot read it, instead of failing silently.
- ASKS.md has a row for accepting the invite.

**Alex must accept the invite in Element before inbound works.** Outbound works now (messages wait in the room).

## What works (verified)

- **Outbound** (live against matrix.org): markdown body plus rendered `formatted_body` (numbered list becomes `<ol>`), reply relation, chunking, reactions, media upload, voice (`m.audio` with the voice flag), documents with captions, download of own media (authenticated v1 endpoint with v3 fallback).
- **Inbound path** (live `/sync` in a throwaway room, bot's own messages let through for the test): numbered list arrives intact, reply line `↩ replying to: "..."` (same format as Telegram, quote fetched via `GET /event` when not cached), voice downloaded to `voice.oga` and rendered in the shared `[attachments]` block (transcription needs faster-whisper, not installed, same as Telegram), document with caption. A simulated gateway restart delivered nothing twice; the persisted sync token resumed.
- **Reactions**: inbound `m.reaction` is archived as the same reaction row as Telegram. The bot reacts 👀 on receipt and the posthook turns it into 👍.
- **Access control**: only `MATRIX_ALEX` (plus optional `MATRIX_ALLOWED`) reach the agent. Fail closed.
- **Telegram untouched**: gateway restarted twice; Telegram `getMe` ok, a silent Telegram send ok, adapters list is `['telegram', 'matrix']`, no `adapter_poll_error`. Telegram inbound could not be exercised live (no one wrote overnight); its code path is unchanged and its tests pass.
- **Not verified live**: Alex's real inbound (needs the invite accepted), a real voice note from Element, `--voice` send into Alex's room (verified in the throwaway room only, to avoid pinging him).

## Design notes

- `metasphere/matrix/`: `api.py` (requests, no nio), `poller.py` (background `/sync` thread per surface, drained on the daemon tick so a 25 s long-poll never delays Telegram), `handler.py`, `routing.py`.
- Sync token is saved only after a batch is handled, with the last 300 event ids. First run discards history (initial sync with an empty timeline) and also drops events older than the run start, because a homeserver can serve a cached initial position (seen once in testing: a previous room's events came through).
- `metasphere message send --surface auto`: an agent with no pin of its own (cron jobs) now borrows the **surface** of the orchestrator's pin, never its chat id (that may be a group). With `--to alex` the chat comes from the addressbook, so cron messages land in Alex's DM on whichever surface he last used.
- Posthook: the end-of-turn text goes to Matrix when the orchestrator's active conversation is Matrix, else Telegram (unchanged default).
- Explicit sends by the orchestrator via `message send` / `matrix send` touch the explicit-send marker so the posthook does not duplicate them.
- Slash commands reuse `telegram.commands.dispatch` (Element handles `/` itself for some commands; use `//` or plain words).

## Tests

- New: 65 tests in `test_matrix_api.py` (19), `test_matrix_routing.py` (4), `test_matrix_handler.py` (20), `test_matrix_poller.py` (10), `test_matrix_adapter.py` (12: adapter, daemon discovery, message CLI, matrix CLI, posthook routing and ack).
- Full suite first run: 2461 passed, 4 failed. The 4 were `test_default_adapters_*` picking up the host's real `matrix.env`; fixed with an autouse fixture in `test_gateway.py` and `test_gateway_adapter.py`.
- FULL_SUITE_RESULT_PLACEHOLDER

## Files changed

Repo (`metasphere-agents`): `metasphere/matrix/{__init__,api,poller,handler,routing}.py`, `metasphere/gateway/adapters/matrix.py`, `metasphere/gateway/adapters/__init__.py`, `metasphere/gateway/daemon.py`, `metasphere/cli/matrix.py`, `metasphere/cli/message.py`, `metasphere/cli/posthook.py`, `metasphere/cli/_registry.py`, `metasphere/posthook.py`, `docs/CLI.md` (regenerated), `pyproject.toml` (+`requests`, `markdown-it-py`), 5 new test files plus the two gateway test files.

Outside the repo (host state, all backed up as `<file>.bak-20261118`, **not committed** in clay):
- `~/.metasphere/config/matrix.env`, `~/.metasphere/ADDRESSBOOK.yaml`, `~/.metasphere/ASKS.md`
- `~/.metasphere/lib/tg_html.py` (`bin/tg-html` is a symlink to it): now surface-aware. If the orchestrator's pin is Matrix it sends the same sanitized HTML to Alex's Matrix DM (Element renders b/i/a/code), otherwise Telegram exactly as before. This covers every prompt and cron that calls `tg-html` (the scheduler `jobs.json` entries and `index_brief.py`, `asks_nudge.py`) without editing them.
- Prompts and scripts where the send line was changed to `metasphere message send --surface auto --to alex` (24 files, 29 replacements; the old forms were `--surface telegram --chat-id 11714502`, `metasphere telegram send [--to alex]`):
REWIRED_PATHS_PLACEHOLDER

Not changed on purpose: `coach-prompt.md` line `metasphere telegram send --to orchestrator ...` (an agent-bus message, not a send to Alex), `schedule/jobs.json` (only `tg-html`, now surface-aware), history files (`MEMORY-INDEX-*`, `REVIEW-2026-10-24.md`, `.msg` outbox files). The brief said 58 files; a grep for every hardcoded form found 24 prompt/script files plus the `tg-html` path that the rest go through.

## Meter (7-day utilization)

Before: 0.42 (23:31 UTC 17 Nov). After: METER_AFTER_PLACEHOLDER.

## Open items

1. Alex accepts the invite and sends the numbered-list reply and a voice note; the orchestrator checks they arrive (ASKS.md row added).
2. The attribution trailer on these commits follows the job file ("Claude Opus 5.5 (1M context)") although the session model was Sonnet 5.5.
3. Voice transcription needs `faster-whisper` (same as Telegram).
4. E2EE is not supported. If Alex ever turns encryption on in the new room the bot goes deaf there; the guard message tells him once.
5. Clay repo (`~/.metasphere`) has the prompt edits uncommitted; commit them when convenient (no `git add -A`).
6. Telegram stays the fallback for a week as planned. The gateway was restarted twice (00:41 and 00:44 CET), each restart respawns the orchestrator session.
