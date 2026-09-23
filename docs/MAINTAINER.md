# metasphere-agents — Maintainer Guide

You're working on the metasphere-agents codebase: the harness that
runs metasphere instances. This file is for contributors. It does
not teach harness etiquette or agent runtime behavior — those live
in `~/.metasphere/CLAUDE.md` (user manual) and
`~/.metasphere/agents/<id>/AGENTS.md` (agent-runtime, per type).

## Codebase layout

- `metasphere/` — Python package. Editable-installed by
  `install.sh`. Subpackages: `cli/` (CLI entrypoints),
  `gateway/` (telegram polling + tmux session manager),
  `memory/`, `telegram/`, `tests/`. Daemon entries live as
  module-level files (`heartbeat.py`, `schedule.py`) — see
  "Daemon services" below.
- `scripts/` — last surviving bash entry points. The `metasphere`
  command is a Python console script (see `pyproject.toml`
  `[project.scripts]`), not a shim here. Only `metasphere-reaper`
  (npm-root-g zombie sweeper, run via systemd timer) and its
  sibling test remain.
- `templates/` — files copied into a user's `~/.metasphere/` on
  install or at agent-spawn time:
  - `templates/install/` — installed to `~/.metasphere/` once at
    `install.sh` first-run.
  - `templates/agents/<role>/` — per-role `AGENTS.md` materialized
    into `~/.metasphere/agents/<id>/` by `metasphere agent seed
    --spec <spec>`. The seeder reads `spec.role` and looks up
    `templates/agents/<role>/AGENTS.md` (e.g. spec `reviewer` →
    role `critic` → `templates/agents/critic/AGENTS.md`); see
    `metasphere/specs.py::seed_agent`.
  - `templates/agent-harness.md` — render template for ephemeral
    one-shot agents (used by `metasphere agent spawn`).
- `docs/` — public-facing documentation (CLI reference, known
  issues, design docs).
- `.tasks/` — repo-scoped tasks; project-scoped tasks live in
  `~/.metasphere/projects/metasphere-agents/.tasks/`.

## Daemon services

The harness's runtime layer is three systemd user services. Each is
a long-running Python process started by `install.sh` writing the
unit files into `~/.config/systemd/user/`:

- `metasphere-gateway.service` — Telegram poll + tmux session
  manager. Entry: `metasphere/gateway/` package, daemon mode.
- `metasphere-heartbeat.service` — periodic agent wake (drives the
  ~5-minute heartbeat ticks that inject context into agent REPLs).
  Entry: `metasphere/heartbeat.py` daemon mode.
- `metasphere-schedule.service` — cron-style job dispatcher (fires
  scheduled `payload`s into agents at configured times). Entry:
  `metasphere/schedule.py` daemon mode.

`metasphere status` shows the health of all three. Restart any of
them with `systemctl --user restart metasphere-<svc>.service`.

## Develop / test / ship

```bash
# Editable install (one-time)
pip install -e .

# Run scoped tests (fast)
pytest metasphere/tests/ -k '<keyword>'

# Run a single module's tests
pytest metasphere/tests/test_<module>.py -v

# Version auto-bumps on merge to main via
# .github/workflows/bump-minor.yml. To skip auto-bump on a merge,
# add [skip ci] to the commit message OR change pyproject.toml's
# `version` field manually in the same merge.

# Verify install.sh against a sandbox
METASPHERE_DIR=/tmp/ms-test bash install.sh -y
```

Scope tests to what changed; don't default to the full suite.

## Self-evolution loop

This repo is its own first test subject. The improvement cycle:

```
IDENTIFY    → friction, missing functionality, confusion
EXPERIMENT  → smallest change that addresses it
EVALUATE    → use it in real operation
INTEGRATE   → keep + commit, or revert + note what was learned
LOOP        → next thing
```

Tight feedback loops beat extensive planning. Land a working
change, observe, iterate. Document hypotheses in commit messages so
the git history reads as a record of the harness's reasoning.

## Conventions

- **Public-bound**: this repo flips public after the
  harness-vs-instance scrub completes. Never commit instance state
  (chat IDs, real agent names, host paths, captured-from-prod test
  fixtures) into shipped paths. Heuristic: *would this string be
  wrong on a stranger's install?*
- **One concern per PR**. Bug fix, feature, refactor — pick one.
- **Commit message names the why**. The diff says what changed; the
  message says why.
- **Small commits over big ones**. Easier to bisect, revert, review.
- **Tests live next to the code**: `metasphere/tests/test_<module>.py`.
- **Documentation in `docs/` is public-facing**. Internal runbooks
  go to `~/.metasphere/runbooks/`, never committed.

## Public-repository ship gate

Treat every commit, branch, pull request, test fixture, and commit message as
public. Before pushing any commit, inspect its complete diff and history for
instance-only material: real chat or account IDs, host-specific absolute
paths, private agent names or topology, persona/configuration details,
captured production data, tokens, credentials, and internal runbook content.
Use synthetic identifiers and portable paths in code and tests. If an
instance-specific detail is useful only for operating one installation, keep
it under `~/.metasphere/` or another private state store; do not commit it.

A fix to shared product behavior is not complete when a branch or PR exists.
After the relevant tests and required CI checks pass, review the final diff
for public-safety, merge the PR to `main`, verify the merge commit is present
on `origin/main`, and remove the remote topic branch. Leave a validated PR
open only when a documented blocker or explicit maintainer decision requires
it. Report branch-only work as "proposed" or "fixed on a branch," never as
shipped.

## Architecture

- Python package; users see `metasphere <subcommand>` (a console
  script declared in `pyproject.toml`) as the single entry point.
  Subcommand dispatch lives in `metasphere/cli/_registry.py`.
- Gateway daemon (`metasphere/gateway/`) runs as a systemd service,
  handles Telegram polling and tmux session lifecycle.
- Per-turn hooks installed into `~/.metasphere/.claude/settings.local.json`
  by `install.sh`: `metasphere hooks context` (UserPromptSubmit) +
  `metasphere hooks posthook` (Stop) — both subcommands of the
  unified CLI, paths rewritten by `metasphere update` on relocate. Codex uses
  the same commands from the single user-level `~/.codex/hooks.json`; update
  migrates old Metasphere-owned project/runtime copies to avoid duplicate
  hook execution while preserving unrelated hooks.
- Lifecycle daemon enforces consolidation, dormancy, reap, and ping
  cadence on tasks and agents.

## Where to find out more

- User-facing: `~/.metasphere/CLAUDE.md` (installed by this repo's
  `install.sh`).
- Project context: `~/.metasphere/projects/metasphere-agents/CLAUDE.md`.
- Public docs: `docs/CLI.md`, `docs/KNOWN_ISSUES.md`.
