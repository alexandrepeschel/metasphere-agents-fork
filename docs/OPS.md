# OPS

Host-level operational procedures for the metasphere harness.
Everything in this file is reproducible on any `systemd --user`-
capable host. Commands assume the repo is checked out at
`<metasphere-agents repo root>`. Find it with:
`pip show metasphere | grep Location`.

## Reaper: `npm root -g` zombies

Claude Code occasionally spawns `npm root -g` which hangs and
accumulates. The reaper is a systemd user oneshot driven by a 60s
timer that kills any `npm root -g` process whose elapsed time
exceeds 60s.

### Files

- `scripts/metasphere-reaper` — the killer (bash)
- `systemd/user/metasphere-reaper.service` — oneshot unit
- `systemd/user/metasphere-reaper.timer` — timer (every 60s)
- `scripts/test_metasphere_reaper.sh` — functional test

### Install

```bash
cd <metasphere-agents repo root>
mkdir -p ~/.config/systemd/user
cp systemd/user/metasphere-reaper.service ~/.config/systemd/user/
cp systemd/user/metasphere-reaper.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now metasphere-reaper.timer
```

Verify:

```bash
systemctl --user list-timers metasphere-reaper.timer
systemctl --user status metasphere-reaper.timer
```

### Observe kills

Each run logs one line with an ISO timestamp and the count of PIDs
killed, to both the user-readable log file and the systemd journal:

- Log file (always readable by the running user):
  `~/.metasphere/logs/reaper.log`
- Journal: `journalctl --user -u metasphere-reaper.service`
  (requires journal read access — on some containers the user is
  not in the `systemd-journal` group, in which case the log file is
  authoritative)

Example line:

```
2026-04-21T13:05:00Z metasphere-reaper killed=2 pids=12345 12390
2026-04-21T13:06:00Z metasphere-reaper killed=0 pids=
```

Per-minute histogram of kill counts:

```bash
awk '/killed=/ {split($0, a, "killed="); split(a[2], b, " ");
     print substr($1,1,16), b[1]}' ~/.metasphere/logs/reaper.log
```

### Run the functional test

The test starts a sleep stub whose argv[0] is `npm root -g`, waits
65 seconds for its elapsed time to exceed the threshold, triggers
the reaper manually (to de-race the timer cadence), and asserts
the stub is dead and a journal line recorded the kill.

```bash
scripts/test_metasphere_reaper.sh
```

### Uninstall

```bash
systemctl --user disable --now metasphere-reaper.timer
rm ~/.config/systemd/user/metasphere-reaper.service
rm ~/.config/systemd/user/metasphere-reaper.timer
systemctl --user daemon-reload
```

### Tuning

`REAPER_THRESHOLD` (seconds) can be overridden via
`Environment=REAPER_THRESHOLD=30` in a drop-in at
`~/.config/systemd/user/metasphere-reaper.service.d/override.conf`.

## Agent session lifecycle

The gateway daemon keeps two hygiene mechanisms running so idle or
completed agents don't leave stale tmux sessions and runtime state
behind.

### Ephemeral !done cleanup

Hook point: `metasphere.messages.send_message` → invokes
`metasphere.agents.on_done_delivered(sender)` whenever the message
`label == "!done"`. This is the narrowest correct point: `!done` is
the single terminal signal in the inter-agent protocol, and the
sender is explicit (`from_agent`).

When the sender is an **ephemeral** agent (no `MISSION.md` in its
identity directory), the hook:

1. Runs `tmux kill-session -t metasphere-<sender>`. Silent no-op when
   the session is absent — ephemerals spawned via headless
   `claude -p` have no tmux pane in the first place.
2. Removes runtime pointers: `pid`, `task_id`. A future
   `metasphere agent spawn <same-name>` re-bootstraps clean.
3. Writes `status = "complete: !done delivered"`. The existing
   `metasphere.posthook.auto_close_finished_task` sweep picks this up
   and archives the backing task.
4. Preserves identity / contract artifacts: `harness.md`,
   `authority`, `responsibility`, `accountability`, `scope`,
   `parent`, `spawned_at`, `task`. These remain available for GC
   logs and later `metasphere agent contract` audits.

When the sender is a **persistent** agent (has `MISSION.md`), the
hook is a strict no-op — persistent lifecycle is governed by the
idle-TTL sweep below, not by individual `!done` messages.

Failures inside the hook are swallowed: the `!done` message is
already written, indexed, and mirrored to telegram before the hook
fires, so a bad tmux probe can never break delivery.

### Persistent idle-TTL dormancy

Hook point: `metasphere.gateway.daemon.run_daemon` loop — calls
`metasphere.agents.reap_dormant(paths, max_idle_seconds)` on the
`dormancy_interval` cadence (default 300s). Separate from the 5s
watchdog tick because sweeping tmux for every agent is more expensive
than the stuck-paste / safety-hooks checks and there is no signal
that would benefit from finer cadence.

`reap_dormant` walks every persistent agent, probes the tmux
`session_activity` timestamp, and for any session idle longer than
`dormancy_max_idle_seconds` (default 86400s = 24h):

1. Writes `status = "dormant: idle <N>s (auto-ttl at <utc>)"` to the
   agent's identity directory so `metasphere status` and a human
   reader see why the session went away.
2. Runs `tmux kill-session -t <session>`.
3. Emits an `agent.dormant` event with the idle duration and session
   name.

Persona and contract files (`MISSION.md`, `SOUL.md`,
`LEARNINGS.md`, `HEARTBEAT.md`, `authority`, `responsibility`,
`accountability`, `harness.md`) are preserved — a subsequent
`metasphere agent wake <name>` restarts cleanly from them.

### Tuning

Both cadences are injection points on `run_daemon`; override at
daemon start time:

- `dormancy_interval` — how often `reap_dormant` is called (seconds)
- `dormancy_max_idle_seconds` — per-agent idle threshold (seconds)

For testing, `reap_dormant_fn` can be injected directly (see the
`test_run_daemon_reap_dormant_fires_on_interval` gateway test).

## Host-health monitoring

The gateway exposes three host-health counters through
`metasphere gateway status` and surfaces a single-line ALERT at the
top of every per-turn context injection when any counter exceeds a
threshold. The probes live in `metasphere.gateway.monitoring` and are
pure-python (no subprocess for zombies / pid headroom; tmux counters
shell out to `tmux list-sessions`).

### Counters

- **zombies** — total procfs state=`Z` processes on the host, plus a
  dedicated `npm_root_g` breakdown because Claude Code's orphaned
  `npm root -g` children are the dominant historical source. Read
  from `/proc/<pid>/status` + `/proc/<pid>/cmdline` + `/proc/<pid>/comm`.
- **tmux** — live tmux sessions split into `persistent` vs
  `ephemeral`. A session counts as persistent when the matching agent
  directory contains `MISSION.md`. Works for both global agents
  (`metasphere-<name>` → `~/.metasphere/agents/@<name>`) and
  project-scoped ones (`metasphere-<project>-<name>` →
  `~/.metasphere/projects/<project>/agents/@<name>`).
- **pid_headroom** — configured PID limit, current process count,
  and percent of slots still available. Cgroup `pids.max` wins when
  it is a finite number (real container ceiling); falls back to
  `/proc/sys/kernel/pid_max`. `source=` in the status output records
  which file was authoritative (`cgroup` / `kernel` / `unknown`).

### Thresholds

A single-line `## ALERT: ...` block is prepended to the per-turn
context when ANY condition is tripped (conditions are joined with
`; `, and the block stays absent when nothing trips — zero-impact on
normal turns):

| Counter                | Trip condition          |
|------------------------|-------------------------|
| `zombies.total`        | > 20                    |
| `tmux.total`           | > 10                    |
| `pids.free_pct`        | < 20 %                  |

Boundaries are intentionally strict (`>`, `<`): a counter sitting
exactly at the threshold is silent. Change the constants in
`metasphere/gateway/monitoring.py` (`ZOMBIE_THRESHOLD`,
`TMUX_THRESHOLD`, `PID_HEADROOM_PCT_THRESHOLD`) to retune.

### Observing

Inspect live counters:

```bash
metasphere gateway status
```

Example output:

```
session=metasphere-orchestrator alive=True idle=0s
zombies total=0 npm_root_g=0
tmux total=5 persistent=2 ephemeral=3
pid_headroom limit=4194304 current=312 free_pct=100.0 source=kernel
```

### Reproducible alert demo

Set `METASPHERE_MONITORING_OVERRIDE` to force the ALERT renderer with
synthetic numbers — useful for verifying the injection path without
actually overloading the host:

```bash
METASPHERE_MONITORING_OVERRIDE='zombies=50,tmux=3,pid_pct=99.0' \
  metasphere hooks context
```

The override takes the form `zombies=N,tmux=M,pid_pct=P`. Unparseable
values fall through to the live probes.

### Failure mode

Every probe is wrapped in try/except and falls back to an empty
ALERT. A broken `/proc` walk or a missing `tmux` binary must never
break context assembly. The `test_render_alert_swallows_probe_exceptions`
test pins this invariant.

## Posthook fail-closed (context-hook breadcrumb)

The `UserPromptSubmit` context hook (`metasphere hooks context`)
writes a per-turn **success breadcrumb** that the `Stop` posthook
(`metasphere hooks posthook`) reads before deciding whether to
forward the assistant's final reply to Telegram. If the breadcrumb is
missing or marked failed, the posthook *fail-closes*: the auto-forward
is suppressed, an entry is written to a local log, and a single `!info`
is queued in `@orchestrator`'s inbox so the degraded-context turn
surfaces.

This guards the case where the context hook crashed (out-of-memory,
file-lock contention, regex blow-up, etc.) and the agent generated its
reply against a context block missing messages, tasks, the voice
capsule, or the host-health ALERT. Sending such a reply to Telegram
makes the agent look amnesic; suppressing it costs one turn but keeps
trust in the channel.

Claude reads this hook from the runtime/project
`.claude/settings.local.json` files and receives the existing plain-text
output. Codex reads one user-level definition from `~/.codex/hooks.json` and
receives structured `hookSpecificOutput.additionalContext`; its event's
`prompt` field is passed directly into recall. USER and IDENTITY baseline
sections render before verbose SOUL and have independent byte budgets. The
Codex handler's 12,000-token transport limit is intentionally above the
builder's bounded section output so stable profile facts remain inline.

Codex user hooks are non-managed hooks. Do not add
`--dangerously-bypass-hook-trust` to project-agent launch paths: review and
trust the generated absolute command via `/hooks`. This avoids implicitly
trusting arbitrary project-local hooks. Updates remove only Metasphere-owned
legacy matcher groups from runtime/source `.codex/hooks.json` files and retain
operator-defined groups.

### Files

- `metasphere/breadcrumbs.py` — breadcrumb read/write/evaluate helpers
- `metasphere/cli/context.py` — UserPromptSubmit hook (writes the
  breadcrumb at end of `build_context()`, or a `failed` marker on
  exception)
- `metasphere/posthook.py::run_posthook` — Stop hook (calls
  `breadcrumbs.evaluate()` before `route_to_telegram`)
- `metasphere/tests/test_breadcrumbs.py`,
  `metasphere/tests/test_cli_context.py`,
  `metasphere/tests/test_posthook.py` — unit tests for the three
  scenarios + count-mismatch defense-in-depth

### Breadcrumb location

```
~/.metasphere/state/context-breadcrumbs/<session_id>.json
```

One file per claude-code `session_id`, overwritten at the end of every
UserPromptSubmit hook in that session. Pruning happens opportunistically
on each successful write — files older than `BREADCRUMB_MAX_AGE_SECONDS`
(7 days) are deleted so orphaned sessions don't accumulate forever.

Schema:

```json
{
  "session_id": "<uuid>",
  "user_msg_count": 12,
  "status": "success" | "failed",
  "agent": "@orchestrator",
  "timestamp": "2026-04-21T14:30:00Z",
  "reason": "<set only on failed: e.g. 'OSError: [Errno 11] EAGAIN'>"
}
```

The posthook's correlation rule: a breadcrumb matches the current turn
when its `session_id` equals the Stop payload's `session_id` AND its
`user_msg_count` equals the count of `type=="user"` records in the
transcript at Stop time. This catches the case where the context hook
crashed *before* writing — the breadcrumb is then stale (count from the
previous turn) and `evaluate()` returns `count-mismatch`.

### Suppression log location

```
~/.metasphere/logs/posthook-suppressions.log
```

One line per suppression. Format:

```
[2026-04-21T14:18:58Z] suppressed forward agent=@orchestrator session=demo-failed reason=context-hook-failed
```

`reason` is one of: `no-session-id`, `breadcrumb-missing`,
`context-hook-failed`, `count-mismatch`, `session-mismatch`.

For postmortems: tail the suppression log to see whether the silence
on Telegram is a fail-closed event or a genuine silent tick. Cross-
reference the timestamp with `~/.metasphere/state/posthook_telegram_errors.log`
to distinguish "context hook died" from "telegram API rejected the
send".

### Reproducible demo

Force a context-hook failure and observe the suppression:

```bash
python - <<'PY'
import os, json, sys
from unittest import mock
from metasphere.cli import context as cli_context
from metasphere import posthook, breadcrumbs as _bc
from metasphere.paths import resolve

paths = resolve()
session_id = "demo-failclose"
transcript = paths.root / "demo.jsonl"
transcript.write_text(json.dumps({"type": "user"}) + "\n")

class FakeStdin:
    def __init__(self, p): self.buffer = type("B", (), {"read": lambda s: p})()
    def isatty(self): return False

# 1) Force the context hook to crash → writes a 'failed' breadcrumb.
sys.stdin = FakeStdin(json.dumps({
    "session_id": session_id, "transcript_path": str(transcript),
    "hook_event_name": "UserPromptSubmit", "prompt": "x",
}).encode())
with mock.patch("metasphere.cli.context.build_context",
                side_effect=RuntimeError("simulated")):
    cli_context.main([])

print("breadcrumb:", _bc.read_breadcrumb(paths, session_id))

# 2) Run the Stop hook → must NOT call telegram-send.
stop_payload = json.dumps({
    "session_id": session_id, "transcript_path": str(transcript),
    "stop_hook_active": False,
}).encode()
with mock.patch("metasphere.telegram.api.send_message") as m:
    posthook.run_posthook(stop_payload, paths)
print("send_message called:", m.called, "(expected False)")
PY

tail -1 ~/.metasphere/logs/posthook-suppressions.log
```

### Failure mode

The breadcrumb itself can fail to write (disk full, permissions,
EAGAIN). In that case, the next posthook tick will see no breadcrumb
and fail-close — that's the *correct* default: when in doubt, don't
forward. The cost of a false positive (one suppressed turn the user
didn't see) is much lower than a false negative (the user gets a reply
generated against an incomplete context and the agent looks broken).

The whole module is wrapped in try/except so a breadcrumb glitch can
never break the host turn — the worst case is "turn doesn't reach
Telegram + entry in suppressions log".
