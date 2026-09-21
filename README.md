# Metasphere Agents

A hackable autonomous agent harness built around an interactive coding-agent REPL. Claude Code is the default runtime; experimental Codex CLI support is available through `METASPHERE_AGENT_RUNTIME=codex`. Your agent runs 24/7, you talk to it from Telegram — it thinks, works, spawns helpers, and reports back.

If you already use Claude Code and like its ecosystem — skills, hooks, MCP servers, IDE integrations — but want your agents to run persistently, be reachable from your phone, and operate with more structure than Claude Code's remote agents offer, this is the missing layer. You get Claude Code's reliability and tool ecosystem with a controllable harness around it: task management, multi-agent coordination, scheduled automation, and transparent state you can inspect and modify.

## What it does

- **Always-on agent** — runs in tmux, survives disconnects, restarts itself after crashes. You don't babysit it.
- **Telegram as your interface** — message your agent from your phone. Ask questions, give tasks, check on progress. It responds in real-time.
- **Interruptible** — you can send a message while the agent is working and it will see it on its next turn. No more waiting for a long task to finish before you can course-correct.
- **Full observability** — every agent runs in its own tmux session. `metasphere session attach @agent` and you see exactly what it's doing — the same Claude Code interface you'd see if you were sitting at the terminal. Watch it think, read its tool calls, take over if needed.
- **Multi-agent** — break complex work into child agents that run in parallel with sandboxed permissions. They report back when done.
- **Projects with transparent tasks** — every task is a markdown file in the project directory. You can read them, edit them, grep them. Nothing is hidden in a database.
- **Scheduled automation** — cron-style jobs for recurring work (health checks, memory consolidation, periodic reports)
- **Per-project memory** — each project has shared `LEARNINGS.md` (lessons + incidents) and `MEMORY.md` (facts + configs + ongoing state) files that every agent on the team reads. Their per-turn context capsule renders a recency window into these files with a footer pointer to the full path on disk, so agents can `Read` or `grep` for older content when it matters.
- **Agent memory** — persistent memory across sessions via daily logs, agent-level learnings files, and a searchable memory index
- **Your Claude Code setup, preserved** — skills, slash commands, MCP servers, hooks, keybindings — everything you've configured in Claude Code works inside your agents. The harness wraps Claude Code, it doesn't replace it.

## Installation

### Prerequisites

Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and authenticate:

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

For the experimental Codex runtime, install and authenticate Codex CLI instead,
then select it when installing:

```bash
codex login
METASPHERE_AGENT_RUNTIME=codex ./install.sh
```

Codex installs the context and Stop hooks once at user scope in
`~/.codex/hooks.json`. On the first interactive Codex launch, run `/hooks`,
review the absolute Metasphere command paths, and trust them. This keeps
Codex's normal hook trust boundary intact while making the same per-turn
context available to the orchestrator, project agents, persistent panes, and
headless runs. The context hook uses the current `UserPromptSubmit.prompt` for
recall; its JSON `additionalContext` output follows the [official Codex hook
contract](https://developers.openai.com/codex/hooks).

You also need `tmux`, `Python 3.11+`, and `jq`:

```bash
# macOS
brew install tmux jq python@3.11

# Debian/Ubuntu
sudo apt install tmux jq python3.11
```

### Install Metasphere

```bash
curl -fsSL https://raw.githubusercontent.com/julianfleck/metasphere-agents/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/julianfleck/metasphere-agents.git
cd metasphere-agents
./install.sh
```

`install.sh` supports:

- `-y` — non-interactive mode (accept defaults / env vars).
- `-v` — verbose output.
- `--no-migrate-<name>` — skip a migration source even when detected
  on disk. One flag per subdir under [`migrate/`](migrate/) — e.g.
  `--no-migrate-openclaw` skips the OpenClaw precursor import. See
  [`migrate/README.md`](migrate/README.md) for how to add your own.

## Quick Start

```bash
# Connect your Telegram bot (get a token from @BotFather). Interactive
# by default — prompts for token, validates via getMe, auto-discovers
# the chat id from your /start message.
metasphere config telegram

# Start the three daemons (gateway, heartbeat, schedule)
metasphere daemon start

# Check all three are running
metasphere daemon status

# System overview: projects, agents, active tasks
metasphere status
```

Your agent is now live. Message it on Telegram.

## Architecture

The harness runs three independent user services—systemd on Linux and launchd on macOS—plus an orchestrator REPL inside a tmux session. User-visible state lives at `~/.metasphere/`; per-project data lives at `~/.metasphere/projects/<name>/`.

### Routing

```mermaid
flowchart LR
    U((User)) -- message --> TG[Telegram Bot API]
    TG -- getUpdates --> GW[gateway daemon\n telegram.poller.run_poll_iteration]
    GW -- parse+download attachments --> ATT[~/.metasphere/attachments/]
    GW -- inject --> TMUX[tmux: metasphere-orchestrator\n agent REPL]
    TMUX -- Stop-hook --> POST[posthook]
    POST -- send --> TG
    HB[heartbeat daemon] -- wake tick --> TMUX
    SCH[schedule daemon] -- cron fire --> CON[consolidate]
    CON -- !query stale --> INBOX[project inbox\n ~/.metasphere/projects/<p>/.messages]
    INBOX -- read --> TMUX
    CON -- ping routing --> LEAD[@<project>-lead]
```

Every inbound Telegram message goes through **one** handler — `metasphere.telegram.handler.handle_update` — whether it came from the production gateway or (historically) a CLI poller. Photos, documents, audio, video, stickers, and any other media payload are downloaded to `~/.metasphere/attachments/<message_id>/` and their paths injected alongside the caption.

### Per-project state

```
~/.metasphere/
├── projects/<name>/
│   ├── project.json       # Project metadata (members, goal, telegram topic)
│   ├── LEARNINGS.md       # Per-project durable insights (auto-injected into agent context)
│   ├── MEMORY.md          # Per-project curated memos (auto-injected into agent context)
│   ├── .tasks/active/     # Task frontmatter files
│   ├── .tasks/archive/    # Dated completion archive
│   ├── .messages/inbox/   # Per-project inbox
│   ├── .messages/outbox/
│   ├── .changelog/        # Daily rollups
│   └── .learnings/        # Cross-agent knowledge base
├── tasks/                 # Global/unscoped tasks (sibling, not nested)
├── messages/              # Global/unscoped messages
├── agents/@<name>/        # Per-agent identity + scope
├── events/events-YYYY-MM-DD.jsonl
└── logs/{gateway,heartbeat,schedule}.log
```

Pre-2026-04-15 installs kept `.tasks/` / `.messages/` / `.changelog/` in the repo itself (`<repo>/.tasks/`). Run `metasphere migrate-project-dirs --apply` to collapse them into the canonical layout.

## How it works

### Every turn, the agent receives

Before each turn, the harness injects the agent's current context:
- Unread messages (from you, from other agents, from scheduled jobs)
- Active tasks across all projects
- The agent's persona and voice (from SOUL.md)
- Recent events (who did what, what fired, what completed)
- Relevant memories from past sessions

### After each turn

After the agent responds:
- The response is forwarded to your Telegram chat
- Events are logged to the event stream
- Heartbeat state is updated

This means you always see what the agent is doing without having to SSH in and attach to a terminal.

## Tasks

Every task is a markdown file in the project's `.tasks/active/` directory — a full briefing with title, priority, status, owner, acceptance criteria, and a running log of updates. When a task is completed, it moves to the archive with a dated folder.

```bash
metasphere task list                        # Show active tasks
metasphere task new "title" [!priority]     # Create (!urgent, !high, !normal, !low)
metasphere task start <task-id>             # Assign to self
metasphere task update <task-id> "note"     # Add progress
metasphere task done <task-id> "summary"    # Complete and archive
```

Tasks live at `~/.metasphere/projects/<name>/.tasks/active/` — canonical per-project storage (see [Architecture](#architecture)). Older installs that kept tasks in-repo can migrate with `metasphere migrate-project-dirs --apply`.

Why markdown files? Because they're transparent — you can read them in your editor, grep across them, version them with git. The agent sees the same files you do.

### Tasks and messages

Tasks and messages work together. When you send a `!task` message to an agent, it creates both a message (which the agent sees on its next turn) and a backing task file (which tracks progress). When the agent finishes, it sends `!done` back, and the task is archived. This means task delegation has a full paper trail — who asked for what, when it was picked up, what updates were logged, and how it was resolved.

```bash
metasphere msg                          # Show unread
metasphere msg send @agent !task "do X" # Delegate work (creates task + message)
metasphere msg reply <msg-id> "text"    # Reply to a message
metasphere msg done <msg-id> "note"     # Mark a task-message as complete
```

## Projects

Projects group agents, tasks, and goals. Each project has its own agent team, task backlog, and optionally a Telegram topic for discussion.

```bash
metasphere project new <name> [--path P] [--goal "..."] [--member @agent:role:persistent]
metasphere project list              # List all projects
metasphere project show [name]       # Project details
metasphere project member add <name> @agent [--role R] [--persistent]
metasphere project wake [name]       # Wake all persistent members
metasphere project chat <name> "msg" # Send to project Telegram topic
```

`--member @name:role:persistent` is colon-separated: the agent id (auto-prefixed with `@`), an optional role (defaults to `contributor`), and an optional persistence flag (`persistent`, `true`, `1`, `yes`, `y`). Repeat the flag to add more members.

## Agent Management

```bash
metasphere agent list              # List all agents
metasphere agent spawn @name /scope/ "task description" [@parent] \
  --authority "what the agent MAY do" \
  --responsibility "what it MUST produce" \
  --accountability "how the parent will verify on !done"
```

Agents can be **ephemeral** (one-shot, run a task and exit) or **persistent** (long-running, with their own tmux session and respawn loop). Persistent agents have a `MISSION.md` that defines their ongoing purpose.

### Sandbox levels

Each agent's sandbox is stored as a single keyword in `~/.metasphere/agents/@<name>/sandbox` and consumed by the heartbeat fallback that runs `claude -p` with a tool-allowlist matching the level:

| Level | What the agent can do |
|-------|----------------------|
| `none` | Full access (default when the file is absent) |
| `scoped` | Only files in its assigned directory |
| `nobash` | Read/write/edit but no shell commands |
| `readonly` | Only read and search — can't change anything |

## Sessions

Every persistent agent runs in its own tmux session. The gateway watchdog monitors all sessions and handles stuck prompts, safety confirmations, and restart recovery.

```bash
metasphere session list              # List active sessions
metasphere session restart @agent    # Restart with auto-continuation
metasphere session send @agent "msg" # Inject a message
metasphere session attach @agent     # Attach your terminal
metasphere session stop @agent       # Stop the agent
```

When a session restarts, the watchdog automatically injects a continuation prompt into the fresh instance so it picks up where it left off — no manual intervention needed.

## Scheduling

Cron jobs live in `~/.metasphere/schedule/jobs.json`. The schedule daemon ticks once a minute and dispatches every job whose cron expression matches.

```bash
metasphere schedule list           # Show scheduled jobs (default with no args)
metasphere schedule add daily-check --agent @orchestrator --cron "0 9 * * *" --tz America/Los_Angeles --message "Run the daily check"
metasphere schedule fire daily-check # Dispatch one job immediately
metasphere schedule remove daily-check
metasphere schedule run            # Fire one tick now: dispatch matching jobs
metasphere schedule enable <id>    # Re-enable a disabled job
metasphere schedule disable <id>   # Disable a job (kept in registry, won't fire)
metasphere schedule wire-exit-self # Append canonical exit-self tail to every job
```

`schedule add` is idempotent by job id, so rerunning it updates the existing job while preserving its fire history.

## Memory

Memory in metasphere is layered. From most-shared to most-personal:

### Per-project memory (team-shared)

Each project has two files at `~/.metasphere/projects/<name>/`:

- **`LEARNINGS.md`** — what the team learned (incidents, lessons, debugging insights).
- **`MEMORY.md`** — what the team knows (facts, configs, references, ongoing state).

These are **the primary memory store**. Every agent on the project sees them. Entries are dated `YYYY-MM-DD: title` so the per-turn context capsule can sort by recency and render the newest entries first, with a footer pointing to the absolute path on disk:

```
_(N more entries omitted by recency. Full file: /home/<user>/.metasphere/projects/<name>/LEARNINGS.md — Read or grep for older.)_
```

When agents are uncertain on a project-specific fact, they `Read` or `grep` the path from the footer instead of guessing. They're explicitly nudged not to reflex-grep on every project query — only when the answer matters and isn't in their head or capsule.

Agents resolve which project's files they receive via, in order:

1. `project: <name>` or `projects: [a, b]` in their `MISSION.md` frontmatter (explicit override; supports multi-project).
2. The central agent→projects roster at `~/.metasphere/teams.yaml` (covers agents whose name doesn't follow the `<project>-<role>` convention, like `@spot` or `@orchestrator`).
3. Path-nested location (agents under `~/.metasphere/projects/<P>/agents/@<id>/` resolve to `<P>`).

### Agent-level memory (per-agent persistent state)

- **Daily logs** — narrative entries about what happened, what was learned, what surprised.
- **`LEARNINGS.md` / `MEMORY.md`** under each agent's home (`~/.metasphere/agents/@<id>/`) — agent-specific knowledge that doesn't belong to a team.
- **Searchable index** — full-text search across all past sessions and memory files.

### Auto-memory (cross-conversation residue)

Claude Code's `~/.claude/projects/...` per-conversation residue — secondary to the project files, useful for recent-context recall but not authoritative.

```bash
metasphere memory search "query"   # Search agent memory
```

From Telegram: `/memory <query>` searches the same index.

See `~/.metasphere/CLAUDE.md` (installed by `install.sh`) for the operator-level walkthrough and `docs/PROJECTS.md` for the design rationale.

## System Management

```bash
metasphere status                      # System overview (sessions, tasks, daemons)
metasphere daemon start|stop|restart   # Daemon control (all three by default)
metasphere logs [<service>] [-f]       # View logs; index when no service given
                                       # Services: gateway, heartbeat, schedule,
                                       # reaper, posthook, update, events
metasphere update                      # Update from git (pull + reinstall + restart)
metasphere config telegram             # Wire up the Telegram bot token + chat id
```

## Telegram Bot Commands

All slash commands available in your Telegram chat (exact set from `BOT_COMMANDS_MANIFEST`):

```
/status              Show orchestrator status
/tasks               List active tasks
/messages            Show inbox messages
/agents              List all agents across projects
/team                Project teams: /team [status|specs|seed|wake]
/specs               List available agent specs
/send @agent !label msg
/projects            Projects: /projects [list|show|new|wake|chat ...]
/schedule            Inspect schedule: /schedule [list|show|run ...]
/memory <query>      Search agent memory
/events              Tail recent events
/spot                Show remote host status
/session             Restart orchestrator REPL
/help                Show help
/ping                Ping the bot
```

You can also send plain text — it goes directly into the agent's session as a new message, and the agent will see it on its very next turn.

## Telegram Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Copy the bot token
3. Run `metasphere config telegram <token>`
4. Start the daemon: `metasphere daemon start`
5. Message your bot

The gateway polls for new messages, injects them into the agent's session, and forwards responses back.

## Agent Identity

Each agent has persona files that define its voice, knowledge, and objectives:

```
~/.metasphere/agents/@orchestrator/
├── SOUL.md              # Identity, values, voice
├── USER.md              # Who the human is
├── MISSION.md           # Objectives and responsibilities
├── HEARTBEAT.md         # Current status (overwritten each update)
├── LEARNINGS.md         # Accumulated insights
└── daily/YYYY-MM-DD.md  # Daily narrative logs
```

The orchestrator reads `SOUL.md` and `USER.md` at session start to establish its voice. Everything else is loaded on demand.

## Migration

```bash
metasphere migrate-project-dirs                  # Dry-run: show the plan
metasphere migrate-project-dirs --apply          # Move all per-project dirs
metasphere migrate-project-dirs --what tasks --apply       # Tasks only
metasphere migrate-project-dirs --what messages --apply    # Messages only
metasphere migrate-project-dirs --project <name> --apply   # One project
```

The migration moves legacy in-repo per-project state — `<repo>/.tasks/`, `<repo>/.messages/`, `<repo>/.changelog/`, `<repo>/.learnings/` — into the canonical per-project home at `~/.metasphere/projects/<name>/`. Idempotent; refuses on conflict (both legacy and canonical non-empty) so you can resolve manually. See [Architecture](#architecture) for the full canonical layout.

For Codex installations, `metasphere update` also migrates Metasphere-owned
context/Stop groups from legacy project-local `.codex/hooks.json` files into
the single user-level `~/.codex/hooks.json`, preserving unrelated custom
hooks. Because Codex hashes hook definitions for trust, use `/hooks` to review
the updated definition after an install or command-path change.

## License

[Functional Source License, Version 1.1, Apache 2.0 Future License](LICENSE)
(`FSL-1.1-Apache-2.0`).

You can read, fork, modify, and use the code for anything *except* offering
it as a commercial product or service that competes with what we offer using
the same software. Two years after each release, that release auto-converts
to the standard Apache License 2.0 — fully open source.

See [`LICENSE`](LICENSE) for the full text and <https://fsl.software/> for
background on the license shape.

## Contributing

See `docs/CLI.md` for the full CLI reference and `docs/MAINTAINER.md` for the contributor guide to this repo.
