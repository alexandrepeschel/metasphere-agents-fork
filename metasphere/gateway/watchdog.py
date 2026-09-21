"""Stuck-prompt recovery for the orchestrator session.

Five failure modes handled:

1. **Stuck pasted-text placeholder.** Bracketed-paste race occasionally
   leaves ``[Pasted text #N +M lines]`` in the pane with the Enter
   eaten. After 15s of the placeholder lingering we force an Enter.
1b. **Stuck inline inject.** The same bracketed-paste race on a *short*
   payload lands the content inline in the input box with NO
   ``[Pasted text #`` placeholder (the submit ``C-m`` fired before the
   paste landed, so the empty box submitted as a no-op and the text
   arrived just after). ``check_stuck_paste`` can't see it, and because
   auto-wakes ``defer_if_busy`` on the leftover typing signal, every
   subsequent wake logs ``[submit failed]`` and the pane stays stranded.
   When byte-stable content prefixed with a known harness marker
   (``[wake]``/``[task]``/…) has lingered past the threshold we force a
   submit — safe because a human never types those markers.
2. **Safety-hooks confirmation prompt.** Plugins occasionally prompt
   ``Do you want to proceed?`` with a numbered ``1. Yes`` option. We
   auto-send ``1`` + Enter, rate-limited to once every 10s so we never
   spam.
3. **Context limit reached.** When Claude's context window fills up,
   the TUI shows ``Context limit reached · /compact or /clear to
   continue`` and stops processing messages silently. We inject
   ``/compact`` to compress the conversation into a summary and resume,
   rate-limited to once every 10 minutes to avoid loops.
4. **Stuck interactive select widget** (``AskUserQuestion`` /
   ``ExitPlanMode``). A gateway session has no keyboard, so an
   interactive select TUI can never receive input and hangs forever.
   The PreToolUse hook (``metasphere.cli.pretool``) normally denies
   those tools *before* they render, so this is a **defense-in-depth
   backstop** for the cases that slip past it: the hook silently
   un-wired (e.g. an ``update.py`` hooks-block drift deletes it) or a
   *new* interactive tool the hook's matcher doesn't yet know. After a
   widget sits **untouched** (byte-stable) for the linger threshold we
   send a single ``Escape`` to cancel it (never ``Escape Escape`` —
   that opens the Rewind menu) and nudge the agent to its async idiom.

All checks are pure functions of capture-pane output + filesystem
state. ``run_watchdog`` composes them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from typing import Optional

from ..events import log_event
from ..io import atomic_write_text
from ..paths import Paths, resolve
from ..session import list_sessions
from .session import SESSION_NAME, session_alive
from ..tmux import input_box_content, submit_to_tmux

_PASTE_RE = re.compile(r"\[Pasted text #\d+")
# Require BOTH a confirm-class line AND a "1. Yes" option line so prose
# listing alone (e.g. an enumeration the agent typed into chat) doesn't
# trip the watchdog.
_SAFETY_HOOKS_PROMPT_RE = re.compile(
    r"(Do you want to proceed\?|\[plugin:safety-hooks\])",
)
_SAFETY_HOOKS_OPTION_RE = re.compile(r"^\s*1\.\s+Yes\b", re.MULTILINE)
# Anchor the context-limit match to the FULL banner on a single line —
# both the ``Context limit reached`` head and the ``/compact or /clear``
# tail — so the bare phrase appearing in scrollback (e.g. an earlier
# banner that has since been compacted away, or the phrase echoed in
# normal output) does not re-fire the check. Mirrors the two-correlated-
# signals discipline of the safety-hooks match above.
_CONTEXT_LIMIT_RE = re.compile(
    r"Context limit reached\b.*?/compact\s+or\s+/clear",
)
# Only inspect the live banner region: the prompt/banner sits at the
# bottom of the pane, so we restrict the match to the last few lines.
_CONTEXT_LIMIT_TAIL_LINES = 8

_STUCK_PASTE_THRESHOLD_S = 15
_SAFETY_HOOKS_RATE_LIMIT_S = 10
_CONTEXT_LIMIT_RATE_LIMIT_S = 600  # 10 minutes

# Auto-inject markers — the prefixes the HARNESS pastes into an agent's input
# box via submit_to_tmux (agent-to-agent wakes, task dispatch, heartbeats; see
# that function's docstring). Any input-box content STARTING with one of these
# is un-committed harness residue: a paste whose submit ``C-m`` fired before
# the bracketed paste landed, leaving the payload inline with NO
# ``[Pasted text #`` placeholder for ``check_stuck_paste`` to recover. A human
# never types these prefixes, so matching on them lets the recovery force a
# submit that can't clobber an operator's half-written message. Deliberately
# NOT included: ``[idle]``/``[silent]``/``[info]`` — those are agent-EMITTED
# sigils, never pasted INTO an input box, so they can't be stuck residue and
# admitting them would only widen the human-typed-lookalike surface.
# ``\b`` after the name matches the ``[task:123]`` / ``[task.consolidate]``
# dispatch variants too.
_INJECT_MARKER_RE = re.compile(r"^\[(wake|task|heartbeat)\b")
# Byte-stable linger before recovering: a human editing changes the buffer
# (resetting the timer); stuck residue doesn't. Also lets a genuinely in-flight
# submit (paste still landing / C-m queued behind a running turn) complete on
# its own first.
_STUCK_INPUT_THRESHOLD_S = 30
# After a recovery C-m, back off before firing again so content that's merely
# queued behind a long-running turn doesn't draw a fresh (benign, but noisy)
# empty C-m every threshold-window.
_STUCK_INPUT_RATE_LIMIT_S = 90

# Interactive select-widget signatures. These are literal footer/option
# strings lifted from the installed Claude Code bundle (v2.1.x), NOT guesses —
# matching the verified-string + two-correlated-signals discipline of the
# checks above rather than puppeteering a guessed pane shape.
#
# Multi-select (``AskUserQuestion``): the toggle-footer is unique enough to
# match alone — it does not appear in normal agent output.
_INTERACTIVE_MULTISELECT_RE = re.compile(r"Space to toggle,\s*Enter to confirm")
# Plan approval (``ExitPlanMode``): require BOTH the question head AND the
# distinctive ``No, keep planning`` option line, so prose merely echoing either
# phrase can't trip the check.
_INTERACTIVE_PLAN_HEAD_RE = re.compile(r"Would you like to proceed\?|Ready to code\?")
_INTERACTIVE_PLAN_OPTION_RE = re.compile(r"No, keep planning")
# The widget sits at the bottom of the pane; only inspect the live region.
_INTERACTIVE_TAIL_LINES = 12
# Escape is the most disruptive recovery (it cancels a tool call), so wait
# longer than the other checks before acting. A genuinely hung widget waits
# forever, so a generous threshold costs nothing while sharply cutting the
# chance of cancelling a human who has attached to answer by hand.
_STUCK_INTERACTIVE_THRESHOLD_S = 45

_INTERACTIVE_REDIRECT_NOTE = (
    "[gateway] An interactive prompt (AskUserQuestion/plan-approval) was "
    "auto-cancelled — this session has no keyboard to answer one and it would "
    "hang you. Ask via your async channel instead: metasphere telegram send "
    "(if you are @orchestrator) or metasphere msg send to your parent/lead "
    "(any other agent), then await the reply in your message inbox, which you "
    "read at the start of every turn. Do not retry the interactive tool."
)


def _tmux_bin() -> str:
    # Pytest sentinel: see metasphere.tmux.tmux_sandboxed — a sandboxed
    # test must never capture from or type into live panes.
    from ..tmux import PYTEST_TMUX_SENTINEL, tmux_sandboxed
    if tmux_sandboxed():
        return PYTEST_TMUX_SENTINEL
    return shutil.which("tmux") or "tmux"


def _capture_pane(session: str) -> str:
    try:
        r = subprocess.run(
            [_tmux_bin(), "capture-pane", "-t", session, "-p", "-S", "-50"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    return r.stdout if r.returncode == 0 else ""


def _send_keys(session: str, *keys: str) -> None:
    try:
        subprocess.run(
            [_tmux_bin(), "send-keys", "-t", session, *keys],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        pass


def _session_state_file(paths: Paths, name: str, session_name: str):
    """Return a per-session linger/rate-limit marker path.

    ``run_watchdog`` fans every check out over *all* live ``metasphere-*``
    tmux sessions, so a marker keyed only by ``name`` is shared across
    sessions: one session's tick clobbers (or unlinks) another's timer,
    silently disabling recovery whenever ≥2 sessions are live. Keying the
    marker by ``session_name`` isolates each session's state. tmux session
    names (``metasphere-<...>``) are filename-safe. Mirrors the per-agent
    ``restart_pending.@<agent>.json`` convention already used below.
    """
    return paths.state / f"{name}.{session_name}"


def _read_int(path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _write_int(path, value: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))
    except OSError:
        pass


def check_stuck_paste(
    session_name: str = SESSION_NAME,
    paths: Optional[Paths] = None,
    *,
    now: Optional[int] = None,
) -> bool:
    """Detect a lingering ``[Pasted text #N`` placeholder; force Enter
    if it has been there ≥15s. Returns True if Enter was sent.

    Handles the stuck-paste branch of prompt recovery.
    """
    paths = paths or resolve()
    if not session_alive(session_name):
        return False
    pane = _capture_pane(session_name)
    state_file = _session_state_file(paths, "stuck_paste_seen", session_name)
    if not _PASTE_RE.search(pane):
        # No placeholder — clear the timer.
        try:
            if state_file.exists():
                state_file.unlink()
        except OSError:
            pass
        return False
    now = now if now is not None else int(time.time())
    first = _read_int(state_file)
    if first == 0:
        _write_int(state_file, now)
        return False
    if now - first < _STUCK_PASTE_THRESHOLD_S:
        return False
    # Stuck long enough — force Enter.
    try:
        log_event(
            "supervisor.force_enter",
            "Stuck pasted-text placeholder cleared",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass
    _send_keys(session_name, "Enter")
    try:
        state_file.unlink()
    except OSError:
        pass
    return True


def check_stuck_input(
    session_name: str = SESSION_NAME,
    paths: Optional[Paths] = None,
    *,
    now: Optional[int] = None,
) -> bool:
    """Recover an auto-inject that stuck INLINE in the input box.

    The bracketed-paste race in :func:`metasphere.tmux.submit_to_tmux` can
    fire the submit ``C-m`` before a short paste has landed: the empty input
    submits as a no-op and the payload arrives moments later, sitting in the
    input box with no ``[Pasted text #`` placeholder. Because auto-wakes use
    ``defer_if_busy=True``, every subsequent wake then DEFERS on the leftover
    typing signal (logged ``[submit failed]``) and the pane stays stranded —
    the exact perpetuating-stuck-state the ``submit_to_tmux`` comment flags
    with "no placeholder for submit_watchdog to recover from". This is that
    recovery path; :func:`check_stuck_paste` handles the placeholder variant.

    Safe by construction. Fires ONLY when the lingering content starts with a
    known harness inject marker (``[wake]``/``[task]``/``[heartbeat]``/…),
    which a human never types — so a force-submit can never clobber an
    operator's half-written message. Gated additionally on a byte-stable
    linger (a human editing changes the buffer, resetting the timer; stuck
    residue doesn't) so an in-flight submit is given time to complete on its
    own, and rate-limited so content merely queued behind a long-running turn
    doesn't draw a fresh empty ``C-m`` every window. In that residual
    queued-behind-a-turn case the extra ``C-m`` is benign: it queues behind
    the original submit, so the content delivers once and any extra ``C-m``
    submits an empty prompt (a no-op).

    Returns True iff a recovery ``C-m`` was fired.
    """
    paths = paths or resolve()
    if not session_alive(session_name):
        return False
    content = input_box_content(session_name)
    state_file = _session_state_file(paths, "stuck_input_seen", session_name)
    # Not our stuck residue: empty box / placeholder (check_stuck_paste's job)
    # / anything not prefixed with a harness marker (incl. real human typing).
    # Clear the linger timer so a later genuine episode starts fresh.
    if (
        not content
        or _PASTE_RE.search(content)
        or not _INJECT_MARKER_RE.match(content)
    ):
        try:
            if state_file.exists():
                state_file.unlink()
        except OSError:
            pass
        return False

    now = now if now is not None else int(time.time())
    rate_file = _session_state_file(paths, "stuck_input_last_submit", session_name)
    if now - _read_int(rate_file) < _STUCK_INPUT_RATE_LIMIT_S:
        return False

    sig = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()[:16]
    first, prev_sig = _read_seen_state(state_file)
    if first == 0 or sig != prev_sig:
        # First sighting, or the content changed (someone is editing / a new
        # inject) — (re)start the linger timer from now.
        _write_seen_state(state_file, now, sig)
        return False
    if now - first < _STUCK_INPUT_THRESHOLD_S:
        return False

    # Byte-stable harness residue past the threshold — submit it as its own
    # user-turn with a raw ``C-m`` (the tmux ``Enter`` keysym doesn't reliably
    # submit in Claude Code's Ink/React TUI — see metasphere.tmux).
    try:
        log_event(
            "supervisor.force_submit",
            f"[watchdog] stuck inline inject in {session_name} "
            f"({content.splitlines()[0][:60]!r}) — forcing submit",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass
    _send_keys(session_name, "C-m")
    _write_int(rate_file, now)
    try:
        state_file.unlink()
    except OSError:
        pass
    return True


def check_safety_hooks_confirmation(
    session_name: str = SESSION_NAME,
    paths: Optional[Paths] = None,
    *,
    now: Optional[int] = None,
) -> bool:
    """Detect a stuck safety-hooks confirmation prompt and auto-approve.

    Rate-limited to once every 10s via a state file marker so we never
    spam ``1`` Enter into the pane. Returns True if a key was sent.
    """
    paths = paths or resolve()
    if not session_alive(session_name):
        return False
    pane = _capture_pane(session_name)
    if not (_SAFETY_HOOKS_PROMPT_RE.search(pane) and _SAFETY_HOOKS_OPTION_RE.search(pane)):
        return False
    marker = _session_state_file(paths, "last_safety_hook_intervention", session_name)
    now = now if now is not None else int(time.time())
    last = _read_int(marker)
    if now - last < _SAFETY_HOOKS_RATE_LIMIT_S:
        return False
    try:
        log_event(
            "supervisor.auto_approve",
            "Safety-hooks confirmation auto-approved",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass
    _send_keys(session_name, "1")
    time.sleep(0.2)
    _send_keys(session_name, "Enter")
    _write_int(marker, now)
    return True


def check_context_limit(
    session_name: str = SESSION_NAME,
    paths: Optional[Paths] = None,
    *,
    now: Optional[int] = None,
) -> bool:
    """Detect the ``Context limit reached`` banner and auto-compact.

    When Claude's context window is full the TUI displays::

        Context limit reached · /compact or /clear to continue

    and stops processing new messages silently. This check injects
    ``/compact`` (which summarises the conversation and resumes) rather
    than doing a hard restart, so no conversational context is lost.

    Rate-limited to once every 10 minutes via a state-file marker to
    prevent rapid-fire loops if compaction itself fails. Returns True
    if ``/compact`` was injected.
    """
    paths = paths or resolve()
    if not session_alive(session_name):
        return False
    pane = _capture_pane(session_name)
    # Match the full banner (head + ``/compact or /clear`` tail) and only
    # in the live banner region at the bottom of the pane, so stale
    # scrollback can't re-fire after a successful compact.
    tail = "\n".join(pane.splitlines()[-_CONTEXT_LIMIT_TAIL_LINES:])
    if not _CONTEXT_LIMIT_RE.search(tail):
        return False
    marker = _session_state_file(paths, "last_context_limit_compact", session_name)
    now = now if now is not None else int(time.time())
    last = _read_int(marker)
    if now - last < _CONTEXT_LIMIT_RATE_LIMIT_S:
        return False
    # Route through the guarded submit path (defer_if_busy=True) so we
    # never interleave the ``/compact`` command with a human mid-typing
    # in the pane, and escape_prefix=False so we don't interrupt a
    # running tool. submit_to_tmux types the command and submits with a
    # raw ``C-m`` byte — the tmux ``Enter`` keysym does NOT reliably
    # submit in Claude Code's Ink/React TUI (see metasphere.tmux), so a
    # bare ``Enter`` would type ``/compact`` but never send it while the
    # rate-limit marker still advanced, freezing the session for the full
    # rate-limit window per attempt.
    submitted = submit_to_tmux(
        session_name,
        "/compact",
        defer_if_busy=True,
        escape_prefix=False,
    )
    if not submitted:
        # Deferred (human typing / busy pane) or submit failed — do NOT
        # write the marker, so the next tick retries once the pane frees.
        return False
    try:
        log_event(
            "supervisor.context_limit_compact",
            f"[watchdog] context limit detected in {session_name}, injecting /compact",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass
    _write_int(marker, now)
    return True


def _interactive_widget_tail(pane: str) -> Optional[str]:
    """Return the live pane-tail region IFF it shows an interactive select
    widget that would hang a gateway session, else ``None``.

    Matches the multi-select toggle-footer alone (unique), or the plan-approval
    head + ``No, keep planning`` option together (two correlated signals).
    """
    tail = "\n".join(pane.splitlines()[-_INTERACTIVE_TAIL_LINES:])
    if _INTERACTIVE_MULTISELECT_RE.search(tail):
        return tail
    if _INTERACTIVE_PLAN_HEAD_RE.search(tail) and _INTERACTIVE_PLAN_OPTION_RE.search(tail):
        return tail
    return None


def _read_seen_state(path) -> tuple[int, str]:
    """Read a ``<first_ts>\\t<signature>`` linger-state file → ``(ts, sig)``."""
    try:
        ts_str, _, sig = path.read_text().strip().partition("\t")
        return int(ts_str), sig
    except (OSError, ValueError):
        return 0, ""


def _write_seen_state(path, ts: int, sig: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{ts}\t{sig}")
    except OSError:
        pass


def check_stuck_interactive_prompt(
    session_name: str = SESSION_NAME,
    paths: Optional[Paths] = None,
    *,
    now: Optional[int] = None,
) -> bool:
    """Backstop: cancel an interactive select widget hanging a gateway session.

    A gateway pane has no keyboard, so ``AskUserQuestion`` / ``ExitPlanMode``
    (and any future interactive select tool) can never receive input and hang
    forever. The PreToolUse hook normally denies those *before* they render;
    this catches the residue — a silently un-wired hook, or a new tool the
    hook's matcher misses.

    Only fires when the widget has sat **untouched** for the linger threshold:
    we fingerprint the widget tail and reset the timer whenever it changes, so
    a human who has attached and is actively navigating the menu (which moves
    the selection highlight, changing the capture) is never cancelled out from
    under. On fire we send a single ``Escape`` (never ``Escape Escape`` — that
    opens the Rewind menu) to cancel, then inject an async-channel nudge.
    Returns True iff a cancel was issued.
    """
    paths = paths or resolve()
    if not session_alive(session_name):
        return False
    pane = _capture_pane(session_name)
    state_file = _session_state_file(paths, "stuck_interactive_seen", session_name)

    tail = _interactive_widget_tail(pane)
    if tail is None:
        # No widget — clear the linger timer.
        try:
            if state_file.exists():
                state_file.unlink()
        except OSError:
            pass
        return False

    sig = hashlib.sha1(tail.encode("utf-8", "replace")).hexdigest()[:16]
    now = now if now is not None else int(time.time())
    first, prev_sig = _read_seen_state(state_file)

    if first == 0 or sig != prev_sig:
        # First sighting, OR the widget changed (someone is interacting /
        # a different prompt) — (re)start the linger timer from now.
        _write_seen_state(state_file, now, sig)
        return False
    if now - first < _STUCK_INTERACTIVE_THRESHOLD_S:
        return False

    # Untouched past the threshold — cancel with a SINGLE Escape and nudge.
    try:
        log_event(
            "supervisor.interactive_prompt_cancel",
            f"[watchdog] stuck interactive widget in {session_name}, "
            "sending Escape + async-channel nudge",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass
    _send_keys(session_name, "Escape")
    time.sleep(0.3)  # let the TUI settle back to the prompt before injecting
    # escape_prefix=False: we already cancelled; do NOT fire a second Escape
    # (Escape Escape opens Rewind). defer_if_busy=True: never fight a human
    # mid-type. The note is best-effort — the cancel is the load-bearing fix —
    # so we clear the timer regardless of whether the nudge landed.
    submit_to_tmux(
        session_name,
        _INTERACTIVE_REDIRECT_NOTE,
        defer_if_busy=True,
        escape_prefix=False,
    )
    try:
        state_file.unlink()
    except OSError:
        pass
    return True


# Grace period after restart before injecting the continuation prompt.
# Claude Code needs a few seconds to start up, load CLAUDE.md, and
# display the initial prompt.
_RESTART_GRACE_S = 8
# If the marker is older than this, something went wrong — clear it
# rather than injecting into a session that's been running for ages.
_RESTART_STALE_S = 120


def _check_restart_marker(
    marker: "Path",
    paths: Paths,
    *,
    now: Optional[int] = None,
) -> bool:
    """Process a single restart-pending marker file. Injects a wake-up
    message into the agent's session if the grace period has elapsed.

    Returns True if a wake-up was injected.
    """
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            marker.unlink()
        except OSError:
            pass
        return False

    ts = data.get("timestamp", 0)
    reason = data.get("reason", "unknown")
    agent = data.get("agent", "@orchestrator")
    now = now if now is not None else int(time.time())
    age = now - ts

    if age > _RESTART_STALE_S:
        try:
            marker.unlink()
        except OSError:
            pass
        return False

    if age < _RESTART_GRACE_S:
        return False

    # Resolve the session name for this agent. ``_resolve_session``
    # already special-cases @orchestrator → SESSION_NAME and walks the
    # agent registry for project-scoped agents (so agents living in
    # ``metasphere-<project>-<agent>`` sessions are found, not silently
    # missed by the bare ``session_name_for`` form).
    from ..session import _resolve_session

    target_session = _resolve_session(agent)

    if not session_alive(target_session):
        return False

    from ..telegram.inject import submit_to_tmux as _submit

    wake_msg = (
        f"[session restarted] agent: {agent}, reason: {reason}. "
        "Check messages and tasks, resume where you left off."
    )
    # defer_if_busy=True: post-restart wake is auto-fired. If a human
    # is at the freshly-respawned pane and typing, retain the marker and
    # retry after another grace window rather than interleaving with them.
    # escape_prefix=False: post-restart wake is an auto-injector and
    # must not interrupt whatever the respawned pane may already be
    # doing — queue the wake text; claude-code will process it when
    # idle.
    success = _submit(
        "system",
        wake_msg,
        session=target_session,
        defer_if_busy=True,
        escape_prefix=False,
    )

    if success:
        try:
            marker.unlink()
        except OSError:
            pass
    else:
        # A startup selector, a busy human input, or a transient tmux failure
        # must not consume the only continuation attempt. Refresh the marker's
        # retry clock so it remains durable beyond the normal stale cutoff and
        # the next attempt still observes the startup grace period.
        data["timestamp"] = now
        data["attempts"] = int(data.get("attempts", 0)) + 1
        try:
            atomic_write_text(marker, json.dumps(data) + "\n")
        except OSError:
            pass

    try:
        outcome = "Injected" if success else "Deferred"
        log_event(
            "supervisor.restart_wake",
            f"{outcome} continuation prompt for {agent} ({reason})",
            agent="@daemon-supervisor",
            paths=paths,
        )
    except Exception:
        pass

    return success


def check_all_restart_pending(paths: Optional[Paths] = None) -> int:
    """Scan for all per-agent restart markers and process them.

    Returns the number of wake-up messages injected.
    """
    paths = paths or resolve()
    if not paths.state.is_dir():
        return 0
    count = 0
    for marker in paths.state.glob("restart_pending.@*.json"):
        try:
            if _check_restart_marker(marker, paths):
                count += 1
        except Exception as e:
            try:
                log_event(
                    "supervisor.watchdog_error",
                    f"check_restart_marker({marker.name}): {e}",
                    agent="@daemon-supervisor",
                    paths=paths,
                )
            except Exception:
                pass
    return count


def _all_session_names() -> list[str]:
    """Return names of all live metasphere-* tmux sessions."""
    return [s.name for s in list_sessions()]


def run_watchdog(paths: Optional[Paths] = None) -> None:
    """Run all stuck-prompt checks across ALL active agent sessions.

    Enumerates all ``metasphere-*`` tmux sessions and runs per-session
    checks (stuck paste, stuck inline inject, safety hooks, context limit,
    stuck interactive prompt). Then scans for per-agent restart markers
    independently.

    Failures of one check do not abort the others. This is the only
    watchdog entry point the daemon calls.
    """
    paths = paths or resolve()

    # Per-session checks: run against every live metasphere-* session.
    sessions = _all_session_names()
    for session_name in sessions:
        for fn in (
            check_stuck_paste,
            check_stuck_input,
            check_safety_hooks_confirmation,
            check_context_limit,
            check_stuck_interactive_prompt,
        ):
            try:
                fn(session_name, paths)
            except Exception as e:  # pragma: no cover - defensive
                try:
                    log_event(
                        "supervisor.watchdog_error",
                        f"{fn.__name__}({session_name}): {e}",
                        agent="@daemon-supervisor",
                        paths=paths,
                    )
                except Exception:
                    pass

    # Restart-pending: scan markers (independent of session enumeration).
    try:
        check_all_restart_pending(paths)
    except Exception as e:  # pragma: no cover - defensive
        try:
            log_event(
                "supervisor.watchdog_error",
                f"check_all_restart_pending: {e}",
                agent="@daemon-supervisor",
                paths=paths,
            )
        except Exception:
            pass

    # Failed addressed inbound is persisted separately from the transport
    # archive. Retry it until tmux confirms delivery; selector blocks and
    # transient session failures therefore cannot silently lose the message.
    try:
        from .pending import retry_pending_inbound

        retry_pending_inbound(paths)
    except Exception as e:  # pragma: no cover - defensive
        try:
            log_event(
                "supervisor.watchdog_error",
                f"retry_pending_inbound: {e}",
                agent="@daemon-supervisor",
                paths=paths,
            )
        except Exception:
            pass
