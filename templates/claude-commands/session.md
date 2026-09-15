---
name: session
description: Manage the orchestrator REPL session — restart it to pick up new harness/CLAUDE.md
---

You have been invoked via the `/session` slash command. The user typed:

```
$ARGUMENTS
```

## Routing

- If `$ARGUMENTS` is empty or starts with `restart`: run the **restart flow** below.
- If `$ARGUMENTS` starts with `status`: run `systemctl --user status metasphere-gateway --no-pager` + `tmux list-sessions | grep metasphere-orchestrator` and report.
- Anything else: print this command's help.

## Restart flow

The orchestrator REPL is wrapped by a `while true; do claude; done` respawn loop in `metasphere-gateway start_session`, so a clean `/exit` is enough to bring up a fresh REPL with the latest baked-in `CLAUDE.md` / settings / slash commands. You don't need to restart the systemd service.

The right way to do this from inside the orchestrator's own REPL:

```bash
metasphere-gateway restart
```

That sends C-c C-c + `/exit` keystrokes to the `metasphere-orchestrator` tmux session and lets the respawn loop revive Claude. It also logs a `supervisor.restart_claude` event so the restart is observable, and writes a restart-pending marker so the watchdog injects a continuation prompt into the new instance. If the session isn't alive it starts one instead of failing.

> There is **no** `metasphere-gateway restart-orchestrator` subcommand — that name was in this doc until 2026-08-27 and never existed. The real one is plain `restart`. It restarts *Claude inside the orchestrator session*, not the gateway daemon; the daemon is a systemd unit and is not what you want here. Dispatch: `metasphere/cli/gateway.py:80` → `restart_session` at `metasphere/gateway/session.py:397` → `restart_agent_session` at `:322`. The CLI logs the reason as `"CLI restart"`; if you need a specific reason in the event log, call `restart_session("<reason>")` in Python instead.

**Steps to take when this slash command fires:**

1. Briefly tell the user what's about to happen ("Restarting REPL — back in a moment with the new harness."). Do this *first*, before triggering the restart, so the message lands on Telegram before the pane dies.
2. Call `metasphere-gateway restart` via the Bash tool. The current Claude process will be killed by the `/exit` it sends to its own pane, the respawn loop will start a new Claude instance, and the new instance will pick up the latest `CLAUDE.md` / hooks / slash commands at boot.
3. Don't try to do additional work after triggering the restart — anything queued won't run because the process is about to die. Get the user-facing message out, fire the restart, done.

## Notes

- This is the canonical way to clear the "harness drift detected" warning that appears at the top of every heartbeat when `~/.metasphere/state/harness_hash_baseline` is stale.
- The new REPL inherits the persistent tmux session, the offset file, the message inbox, and all spawned children. Only the in-memory Claude state (current conversation, working memory) is lost.
- **Auto-continuation:** `restart_session()` writes a `restart_pending.json` marker to `~/.metasphere/state/`. The gateway watchdog detects this marker after an 8-second grace period and injects a continuation prompt into the fresh Claude instance, so the new session picks up automatically without manual intervention.
- If the restart hangs (gateway dead, no respawn loop), the operator may need to start the gateway manually: `systemctl --user restart metasphere-gateway`.
