"""Reliable tmux paste+submit for claude TUI sessions.

Bypasses bracketed-paste entirely by using ``tmux send-keys -l`` (literal
mode) to type the message character-by-character. Newlines within the
message are sent as ``C-j`` (newline-in-buffer, does not submit); final
submit is a single ``Enter``.

Belt-and-suspenders: after submitting, captures the pane and checks for
a stuck ``[Pasted text #`` placeholder. If found, retries Enter up to
3 times.

Confirmed-submit: the single submit ``C-m`` can be *eaten* when it fires
while Claude Code's Ink/React TUI is still settling the bracketed-paste
``\\e[201~`` end-marker — the paste content then lands **inline** in the
input box with NO ``[Pasted text #`` placeholder. The gateway watchdog
only force-submits *marker-prefixed* payloads (``[wake]``/``[task]``/…),
so an unmarked **human** message stranded this way is never recovered
(this is why operator-authored Telegram-relayed messages stick). The
post-submit poll below re-fires ``C-m`` when byte-stable inline content
proves the submit was eaten — reliable submission at the source, no
marker needed.

Never raises — returns False on failure.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
import time


#: Confirmed-submit tuning. After the submit ``C-m``, the post-submit poll
#: watches the input box. If inline content (NOT a ``[Pasted text #``
#: placeholder) sits **byte-identical** across this many consecutive polls,
#: the submit was eaten (an *accepted* submit clears an inline box within a
#: render frame or two) — so we re-fire ``C-m``. The window must sit safely
#: past normal render lag; ~4 polls ≈ 2s+ of a frozen box is unambiguous.
_SUBMIT_CONFIRM_TICKS = 4
#: Max confirmed-submit ``C-m`` re-fires before giving up to the watchdog.
_SUBMIT_CONFIRM_RETRIES = 3


def _confirmed_submit_disabled() -> bool:
    """Fail-open kill-switch for the confirmed-submit retry (core-loop
    behavioral change). Set ``METASPHERE_SUBMIT_RETRY_DISABLED=1`` to revert
    to patient-poll-only submission without editing this live-loaded module.
    Any truthy value disables it."""
    return bool(os.environ.get("METASPHERE_SUBMIT_RETRY_DISABLED"))


# Sessions for which we already logged a "defer: input has typing"
# line. The heartbeat fires every 5 minutes; without this gate, a single
# multi-hour typing session in the orchestrator pane produces hundreds
# of identical defer lines and drowns the heartbeat log (~50% noise was
# the steady state). We log on the transition into the deferred state
# and stay silent until a successful submit clears the flag — so the
# next deferred heartbeat after the user stops typing logs again.
_deferring_sessions: set[str] = set()

# Monotonic counter for unique tmux paste-buffer names. Every submit used to
# share the buffer ``_metasphere_submit``; a heartbeat inject (cron-spawned
# process) and a telegram inject (gateway daemon) firing concurrently would
# then ``load-buffer`` over each other, so one paste pulled the other's content
# (garbled/partial submit or a stuck placeholder). A per-call name — pid keyed
# so cross-process injects can't collide, counter keyed for same-process
# safety — makes each paste self-contained. ``paste-buffer -d`` still deletes
# the buffer after use, so these don't accumulate.
_buffer_counter = itertools.count()

#: Tail of the last payload pasted into each session, persisted to disk.
#:
#: Why a file and not a module global: heartbeat injects are cron-spawned
#: *processes* while telegram injects run in the gateway daemon (see the
#: ``_buffer_counter`` note above). The submit that leaves a residual tail
#: and the submit that trips over it are routinely in different processes,
#: so in-memory state would miss exactly the cross-process case this guards.
#: Only a bounded tail is stored — a leftover fragment is short, and keeping
#: whole payloads on disk would be both pointless and a privacy footgun.
_LAST_PASTE_TAIL_CHARS = 4000

#: Above this length, a box whose content appears anywhere inside the last
#: payload is treated as our own leftover rather than as operator typing.
#: Chosen to be far past what a human types by hand that also happens to be a
#: verbatim slice of the previous heartbeat — 24 characters of exact match is
#: already not a coincidence. Below it, the stricter suffix test still applies.
_MIN_RESIDUAL_TAIL_CHARS = 24


def _last_paste_path(session: str) -> "pathlib.Path":
    import pathlib
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    d = pathlib.Path.home() / ".metasphere" / "state"
    return d / f"last_paste.{safe}"


def _record_last_paste(session: str, message: str) -> None:
    """Remember what we just pasted so the next submit can recognise its tail."""
    try:
        p = _last_paste_path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(message.split())[-_LAST_PASTE_TAIL_CHARS:])
    except OSError:
        pass  # best-effort; a missing hint only costs us the old behaviour


def _read_last_paste(session: str) -> str:
    try:
        return _last_paste_path(session).read_text()
    except OSError:
        return ""


#: How recently we must have pasted for an unreadable box to count as ours.
#: A residual tail lands within seconds of its own paste, so this only has to
#: cover one submit cycle plus the TUI's render lag. Kept short because the
#: cost of being wrong is discarding an operator's large paste.
_HINT_FRESH_SECONDS = 90


def _hint_is_fresh(session: str) -> bool:
    """True if we pasted into *session* within the last few seconds.

    The content-based tests cannot see through a ``[Pasted text #N]``
    placeholder, so provenance has to stand in for content: if we pasted
    moments ago and something unreadable is now sitting in the box, it is
    overwhelmingly likely to be the tail of what we pasted.
    """
    try:
        import time as _t
        age = _t.time() - _last_paste_path(session).stat().st_mtime
        return 0 <= age <= _HINT_FRESH_SECONDS
    except OSError:
        return False

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


#: Returned by tmux discovery when a pytest run must not reach the host
#: server but the discovery contract requires a str (``_tmux_bin`` in
#: agents.py / gateway modules). The path cannot exist, so any exec of
#: it raises FileNotFoundError — which every call site treats as
#: tmux-absent and degrades gracefully.
PYTEST_TMUX_SENTINEL = "/nonexistent/tmux-blocked-under-pytest"


def tmux_sandboxed() -> bool:
    """True when tmux side-effects must be blocked: under pytest
    (``PYTEST_CURRENT_TEST`` is set) without the explicit
    ``METASPHERE_ALLOW_TMUX_IN_TESTS=1`` opt-in.

    2026-07-05: sandboxed ``mark_done`` tests (tmp ``paths=``) reached
    the real ``wake_recipient_if_live`` → ``submit_to_tmux``, injecting
    ``[wake] new !done from @worker: all green`` into the production
    orchestrator pane on every suite run — file writes and log_event
    respect the paths sandbox, tmux does not. The same class of leak
    exists at every tmux discovery site (agents cold-start/kill-session,
    gateway watchdog, cli restart/failsafe), so they all key off this
    single predicate.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get(
        "METASPHERE_ALLOW_TMUX_IN_TESTS"
    )


def _find_tmux() -> str | None:
    """Locate the tmux binary.

    Under pytest this returns None so that every side-effecting path in
    this module — ``submit_to_tmux``, ``submit_watchdog`` — degrades to
    its graceful no-tmux failure mode instead of typing into LIVE panes
    (see ``tmux_sandboxed``).

    Tests that deliberately exercise the submit machinery monkeypatch
    this function (see test_tmux.py). To intentionally reach a real
    tmux server from a test, set ``METASPHERE_ALLOW_TMUX_IN_TESTS=1``.
    """
    if tmux_sandboxed():
        return None
    return shutil.which("tmux")


def _has_session(tmux: str, session: str) -> bool:
    """Return True if a tmux session exists."""
    try:
        r = subprocess.run(
            [tmux, "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except OSError:
        return False


def _is_box_border(stripped: str) -> bool:
    """A Claude Code input-box border line is a run of ``─`` (U+2500)
    characters, optionally with leading/trailing spaces. Some terminals
    render it as ASCII ``-`` — accept both."""
    if not stripped:
        return False
    # Must be predominantly border chars (allow a few stray spaces).
    border_chars = sum(1 for c in stripped if c in "─-")
    return border_chars >= 10 and border_chars >= len(stripped) * 0.8


def _codex_prompt_content(styled_pane: str) -> str | None:
    for raw in reversed(styled_pane.splitlines()[-10:]):
        plain = _ANSI_RE.sub("", raw).strip()
        if not plain.startswith("›"):
            continue
        content = plain[1:].strip()
        if not content or "\x1b[2m" in raw:
            return None
        return content
    return None


def _input_line_has_typing(tmux: str, session: str) -> bool:
    """Inspect the pane and return True if the input box shows
    user-typed content (mid-typing human, or mid-inject residue).

    2026-04-16: an operator was typing into the attached orchestrator pane
    when a heartbeat fired ``submit_to_tmux``; its send-keys interleaved
    with his keystrokes and submitted the garbled mess. This guard
    inspects the Claude TUI input box BEFORE firing any send-keys; if
    the prompt shows typed content that isn't a known paste placeholder,
    auto-injectors defer.

    Heuristic: find Claude Code's input box by its ``─────`` border
    lines (U+2500). Input box content lives between the last two
    border lines in the visible pane. If any line between them has
    content beyond the ``❯`` prompt marker and whitespace, someone is
    typing.

    The earlier version walked only the last 10 lines looking for a
    line starting with ``❯`` — which missed wrapped multi-line input,
    because Claude Code pushes the ``❯``-line off the window when the
    user types a long message. Heartbeats then fired mid-typing and
    interleaved with keystrokes. The border-based detection handles
    wrapped input correctly (all wrapped content sits between the
    same two borders).

    Fails open on any error (returns False) — better to occasionally
    interleave than to silently drop every heartbeat on tmux quirks.
    """
    try:
        codex_runtime = os.environ.get("METASPHERE_AGENT_RUNTIME", "").lower() == "codex"
        argv = [tmux, "capture-pane", "-p"]
        if codex_runtime:
            argv.append("-e")
        argv.extend(["-t", session])
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return False
        if codex_runtime:
            return _codex_prompt_content(r.stdout) is not None
        inner_lines = _input_box_inner_lines(r.stdout.splitlines())
        if inner_lines is None:
            return False  # no input box found — fail open
        for line in inner_lines:
            inner = _clean_input_inner(line)
            if not inner:
                continue
            # A lingering paste placeholder is not typing — submit_watchdog
            # handles those asynchronously.
            if "[Pasted text #" in inner:
                continue
            return True
        return False
    except OSError:
        return False


def _input_box_inner_lines(pane_lines: list[str]) -> list[str] | None:
    """Return the raw lines that sit *between* Claude Code's input-box
    borders (the two nearest ``─────`` lines at the bottom of the pane),
    or ``None`` when a full box isn't visible.

    Walks up from the end for the two border lines that bracket the box —
    the same detection ``_input_line_has_typing`` used inline before it was
    extracted here, so wrapped multi-line input is handled correctly (all
    wrapped content sits between the same two borders). Requiring *both*
    borders means a box whose top border has scrolled off returns ``None``
    (callers fail their own way — the typing-guard fails open, the
    stuck-input recovery fails closed).
    """
    bottom_idx: int | None = None
    top_idx: int | None = None
    for i in range(len(pane_lines) - 1, -1, -1):
        if _is_box_border(pane_lines[i].strip()):
            if bottom_idx is None:
                bottom_idx = i
            else:
                top_idx = i
                break
    if bottom_idx is None or top_idx is None:
        return None
    return pane_lines[top_idx + 1:bottom_idx]


def _clean_input_inner(line: str) -> str:
    """Strip box side-chars and the ``❯``/``>`` prompt marker from one
    input-box line, returning the bare typed content (possibly empty)."""
    inner = line.strip().lstrip("│|").rstrip("│|").strip()
    if inner.startswith("❯"):
        inner = inner[len("❯"):].strip()
    elif inner.startswith(">"):
        inner = inner[1:].strip()
    return inner


def input_box_content(session: str) -> str | None:
    """Return the text currently typed into *session*'s Claude Code input
    box (inner lines joined by ``\\n``, box chrome and prompt marker
    stripped), or ``None`` when no input box is visible / on any error.

    Unlike :func:`_input_line_has_typing` this returns the content verbatim
    — including a lone ``[Pasted text #`` placeholder — so callers decide
    what to do with it. The gateway watchdog uses it to recognise an
    auto-inject that stuck *inline* in the box (no placeholder for
    ``check_stuck_paste`` to catch) and force its submit. Never raises.
    """
    try:
        tmux = _find_tmux()
        if not tmux:
            return None
        codex_runtime = os.environ.get("METASPHERE_AGENT_RUNTIME", "").lower() == "codex"
        argv = [tmux, "capture-pane", "-p"]
        if codex_runtime:
            argv.append("-e")
        argv.extend(["-t", session])
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return None
        if codex_runtime:
            return _codex_prompt_content(r.stdout)
        inner_lines = _input_box_inner_lines(r.stdout.splitlines())
        if inner_lines is None:
            return None
        parts = [_clean_input_inner(line) for line in inner_lines]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    except OSError:
        return None


def _has_pending_paste(tmux: str, session: str) -> bool:
    """Return True if a ``[Pasted text #`` placeholder is visible in the
    last few lines of the session pane."""
    try:
        r = subprocess.run(
            [tmux, "capture-pane", "-p", "-t", session],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return False
        # Check last 5 lines, matching the bash script's `tail -5`
        lines = r.stdout.splitlines()
        for line in lines[-5:]:
            if "[Pasted text #" in line:
                return True
        return False
    except OSError:
        return False


def _is_residual_paste_tail(message: str, inline: str) -> bool:
    """True if ``inline`` looks like the leftover *tail* of ``message`` rather
    than a message that never got submitted.

    Why this exists. The confirmed-submit retry below re-fires ``C-m`` when
    inline content sits byte-stable in the input box, on the theory that the
    submit was eaten. There is a second way to get a stable inline box, and it
    is the opposite situation: the submit was **accepted**, but the paste was
    still streaming, so the last few characters landed in the now-empty box
    afterwards. Re-firing ``C-m`` then does not recover a lost message — it
    *submits a fragment* as its own turn, which arrives at the agent looking
    like a user instruction that starts mid-word.

    Observed three times, 2026-09-01 and twice on 2026-09-02, always as the
    tail of the preceding heartbeat payload, e.g.
    ``" ext and hits turn limits faster.**How to apply:**- Code analysis"``.
    The cut moved by one character between occurrences, which is the signature
    of a timing race rather than a fixed truncation budget.

    The discriminator: a proper *substring* means the head already went
    through. If the whole message is sitting there, it is a genuine eaten
    submit and the retry is correct — so equality must NOT match. Whitespace
    is stripped from both sides because the pane render wraps and pads, so
    only the character sequence is comparable.

    **2026-09-15 — it was a strict-suffix test and that was too narrow.** The
    input box only renders as many lines as fit; on a long leftover the pane
    shows a *middle slice* of it, so the visible text is a substring that does
    not reach the end of the payload and the suffix test returns False. The
    fragment then takes the retry path and gets submitted. Measured on the
    occurrence that prompted this: the visible text sat at index 3574 of a
    4000-character hint, i.e. 426 characters short of being a suffix. Short
    tails had always happened to be fully visible, which is why the narrower
    test looked correct for two weeks.

    ``_MIN_RESIDUAL_TAIL_CHARS`` is what keeps this safe. Widening from suffix
    to substring means operator typing could in principle match, so a fragment
    must also be long enough that a human reproducing it verbatim out of the
    previous payload is not a real scenario. Below that floor we fall back to
    requiring a true suffix, which is the old behaviour.
    """
    if not message or not inline:
        return False
    msg = "".join(message.split())
    tail = "".join(inline.split())
    if not tail or len(tail) >= len(msg):
        # Equal or longer: the whole payload is stuck, or the box holds
        # something we did not paste. Both belong to the retry path.
        return False
    if len(tail) >= _MIN_RESIDUAL_TAIL_CHARS:
        return tail in msg
    return msg.endswith(tail)


def submit_to_tmux(
    session: str, message: str, *,
    defer_if_busy: bool = False,
    escape_prefix: bool = True,
) -> bool:
    """Deliver *message* to a claude TUI in tmux session *session*.

    Strategy: split on newlines, send each line via ``tmux send-keys -l``
    (literal mode). Between lines send ``C-j`` (newline in buffer, does
    not submit). After the last line, brief settle then ``Enter`` to
    submit. Verifies no stuck ``[Pasted text #`` placeholder remains;
    retries Enter up to 3 times if one is found.

    Returns True on success, False on any failure. Never raises.

    If *defer_if_busy* is True, abort (returning False, no send-keys
    fired) when the input box shows typed content (see
    :func:`_input_line_has_typing`). Auto-injectors (heartbeat,
    agent-to-agent wakes, posthook deferred-cmd, restart-wake) opt in;
    manual CLI paths and user-inbound telegram leave it off so the send
    still goes through. Importantly, this does NOT check for client
    attachment — operators keep panes attached for monitoring and
    attach-alone isn't evidence of typing; we guard on actual
    input-buffer content instead.

    If *escape_prefix* is True (default), fire ``Escape × 2`` before
    typing to clear any stuck paste placeholder AND to interrupt any
    in-flight Claude Code turn — so the pasted message becomes a new
    user-turn rather than queuing behind a running tool. Auto-injectors
    (heartbeat, agent-to-agent wakes, posthook deferred-cmd,
    restart-wake) set this to False: they must never interrupt a
    running tool call, only user-inbound telegram and manual CLI sends
    should. 2026-04-16: the always-on Escape was eating the operator's
    telegram inbound AND their own typing whenever a heartbeat fired
    during a mid-tool-call; separating "interrupt intent" from "paste
    intent" closes that race "once and for all" (their framing).
    """
    try:
        tmux = _find_tmux()
        if not tmux:
            return False

        if not _has_session(tmux, session):
            return False

        if defer_if_busy and _input_line_has_typing(tmux, session):
            if session not in _deferring_sessions:
                print(
                    f"[tmux.submit] defer: input has typing in {session}",
                    file=sys.stderr,
                )
                _deferring_sessions.add(session)
            return False

        # Pre-flush C-m: if the input box has legit pending content
        # from a prior wake that didn't fully commit (rare now that
        # escape_prefix=False on wakes, but possible on daemon restart
        # or interrupt race), submit it as its own user-turn rather
        # than clobbering it. Claude Code queues user-turns during an
        # active turn and processes them in order, so this is safe.
        # On clean empty input, C-m is a no-op (no spurious turn).
        # 2026-04-20: the operator's suggestion — pre-C-m preserves legit
        # queued content instead of overwriting it.
        #
        # 2026-09-15: *unless what is sitting there is our own leftover.*
        # The pre-flush assumes box content is legit pending content. A
        # residual paste tail is the opposite — the previous submit was
        # accepted and the last characters of that payload landed in the
        # now-empty box afterwards. C-m then submits the fragment as its
        # own turn, and it reaches the agent as a user message starting
        # mid-word. This is the SECOND route to the symptom that
        # ``_is_residual_paste_tail`` was written for: that guard sits in
        # the post-submit retry loop below, so it never sees this path,
        # which is why occurrences continued after it shipped. Observed
        # three times on 09-15 alone, all tails of the heartbeat payload,
        # all arriving at the *next* heartbeat's submit.
        #
        # C-u (kill line) rather than C-m: the fragment is ours and was
        # already delivered as part of the head, so there is nothing to
        # preserve. Falls through to the normal C-m when the box holds
        # anything we do not recognise — an unknown box still belongs to
        # the operator and must not be discarded.
        # 2026-09-15, FOURTH occurrence and the last patch to this guard.
        # Everything above compares the box's *text* against the hint. When the
        # leftover is long the TUI does not show text at all — it shows a
        # ``[Pasted text #N]`` placeholder, which is a substring of nothing, so
        # every text-based test returns False and the pre-flush `C-m` submits a
        # payload it cannot read. Three fixes in one day all missed this because
        # all three asked "is this string ours", a question the pane refuses to
        # answer in exactly the case that matters.
        #
        # So stop asking about the string. A placeholder we did not just create
        # ourselves, sitting in the box while a hint written seconds ago says we
        # pasted something, is our leftover — identified by provenance and
        # timing rather than by content. The operator pasting a large block in
        # the same few seconds is the false positive, and it is rare enough
        # against a bleed that has now fired four times in one day; the window
        # is kept tight to bound it.
        _preflush_key = "C-m"
        _box = input_box_content(session)
        if _box and "[Pasted text #" in _box and _hint_is_fresh(session):
            _preflush_key = "C-u"
            print(
                f"[tmux.submit] dropping unreadable residual paste in {session} "
                f"(placeholder + fresh paste hint)",
                file=sys.stderr,
            )
        elif _is_residual_paste_tail(_read_last_paste(session), _box or ""):
            _preflush_key = "C-u"
            print(
                f"[tmux.submit] dropping residual paste tail in {session}",
                file=sys.stderr,
            )
        subprocess.run(
            [tmux, "send-keys", "-t", session, _preflush_key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(0.2)

        # Pre-emptive Escape × 2: (a) clears any ``[Pasted text #N``
        # placeholder left over from a prior wake that didn't fully
        # commit (stacking pattern that caused the 2026-04-16
        # research-* pane outages); (b) interrupts any in-flight
        # Claude Code turn so the pasted text becomes a NEW user-turn
        # rather than queueing behind a running tool.
        #
        # Gated on *escape_prefix* so auto-injectors can paste without
        # interrupting a running tool. When False we rely on Claude
        # Code's keystroke queue: characters typed during a tool call
        # are buffered and processed when the tool completes. The
        # downside is that if a prior inject left a stale paste
        # placeholder, the auto-path can't clean it up — but the
        # submit_watchdog daemon handles that asynchronously.
        if escape_prefix:
            # SINGLE Escape = interrupt running turn (operator-confirmed
            # against the Claude Code keybinding reference, 2026-04-16). Esc Esc
            # opens the Rewind/Undo menu — we were typing into THAT
            # menu's filter the whole time, which explains the
            # "list of messages flashing" symptom. Never Escape×2
            # here.
            subprocess.run(
                [tmux, "send-keys", "-t", session, "Escape"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            # 800ms gives the TUI time to finish its post-interrupt
            # "What should Claude do instead?" transition before we
            # type. Shorter settles eat the first 1-2 chars.
            time.sleep(0.8)
        else:
            # No Escape prefix, but we still need a modest settle
            # before typing. Without it, the first 6-7 characters
            # get eaten when the pane is in a post-turn transition
            # state (e.g., TUI still rendering the previous reply
            # / ack). 2026-04-20 repro: cam-lead pane, wake with
            # "[task] TEST ..." payload, "[task] " (7 chars)
            # consistently lost; 0.5s pre-type settle eliminates it.
            time.sleep(0.5)

        # Deliver message via tmux paste-buffer (single atomic paste)
        # instead of per-line send-keys -l + C-j. Two advantages:
        #
        # 1. The TUI sees a proper bracketed-paste event, not a rapid
        #    char-stream that gets heuristically flagged as "suspicious
        #    paste." Both paths eventually converge on a [Pasted text #N]
        #    placeholder for long content, but paste-buffer's placeholder
        #    commits reliably on C-m; send-keys -l char bursts left the
        #    TUI in a mid-detection state that ate the C-m submit.
        # 2. One subprocess call instead of N (N = line_count × 2),
        #    reducing latency + the window where a heartbeat could
        #    interleave mid-paste.
        #
        # 2026-04-20 repro: 79-line wake via send-keys -l → buffered
        # for >15s (C-m eaten); 60-line via load-buffer + paste-buffer →
        # committed in <8s on single C-m. Root-cause fix.
        # Unique per-call buffer name so a concurrent inject (e.g. a cron
        # heartbeat process racing the daemon's telegram inject) can't clobber
        # this paste's content between load and paste. ``-d`` deletes it after.
        buf = f"_metasphere_submit_{os.getpid()}_{next(_buffer_counter)}"
        subprocess.run(
            [tmux, "load-buffer", "-b", buf, "-"],
            input=message,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            [tmux, "paste-buffer", "-b", buf,
             "-d", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        # ``paste-buffer -d`` deletes the buffer on success. If the paste
        # failed (e.g. the session vanished in the race window after the
        # _has_session check), the named buffer would leak — and unlike the
        # old fixed name (bounded to 1, overwritten next call), per-call names
        # would accumulate. Best-effort delete closes that; a no-op / harmless
        # error when -d already removed it.
        subprocess.run(
            [tmux, "delete-buffer", "-b", buf],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        # Record the hint HERE, not at the success return below.
        #
        # 2026-09-15: the hint was originally written only when the
        # post-submit poll came back clean — which a heartbeat injected
        # into a *busy* agent can never do. Claude Code queues the paste
        # behind the running turn, the ``[Pasted text #N]`` placeholder
        # stays on screen past the 12s poll, and the function returns
        # False even though delivery succeeded. So the one payload whose
        # tail actually bleeds was the one payload never recorded, and
        # the pre-flush guard above compared the leftover against a
        # stale hint from hours earlier and let the C-m through.
        #
        # What the hint claims is "this is what we last pushed at the
        # pane", which is true the moment the buffer is pasted. Whether
        # the submit was later confirmed is a different question and not
        # one the tail discriminator asks.
        _record_last_paste(session, message)

        # Settle, then wait for the paste to actually land in the input
        # box before firing the submit C-m. The Claude Code TUI
        # (Ink/React) processes the bracketed-paste event asynchronously:
        # paste-buffer dumps the content to the TTY immediately, but
        # the TUI's render pass that surfaces the chars (inline for
        # short payloads, ``[Pasted text #N]`` placeholder for long
        # ones) can lag 0.5–4s on a busy host.
        #
        # If C-m fires before the paste has landed, the TUI submits an
        # empty input (no-op) and the paste content arrives moments
        # later — sitting in the input box forever with no
        # ``[Pasted text #`` placeholder for ``submit_watchdog`` to
        # recover from. This is the smoking gun for the "input stuck
        # in pane, not submitted" outage: heartbeats then ``defer`` on
        # the leftover typing signal, perpetuating the stuck state.
        #
        # Poll up to ~5s for paste-landed. Most pastes are visible in
        # <0.5s. If the poll times out we still fire C-m anyway —
        # better to attempt the submit than silently drop the message;
        # the post-submit poll below will return False if it didn't
        # take, surfacing the failure to the caller.
        time.sleep(0.3)
        for _ in range(10):
            if (_has_pending_paste(tmux, session)
                    or _input_line_has_typing(tmux, session)):
                break
            time.sleep(0.5)
        subprocess.run(
            # C-m (ASCII 0x0D) raw byte. tmux 3.3a's 'Enter' keysym
            # doesn't trigger submit in Claude Code's TUI (Ink/React)
            # in all states; raw C-m does.
            [tmux, "send-keys", "-t", session, "C-m"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        # Poll for clean input box, up to 12s. Fast path returns in
        # <1s on the common case; patient on slow TUI processing
        # (large payloads can take 5-8s from paste → commit → render
        # clean).
        #
        # Confirmed-submit: if the submit C-m was EATEN (fired while the
        # TUI was still settling the bracketed-paste end-marker), the
        # payload sits INLINE in the box with no ``[Pasted text #``
        # placeholder — the exact stranded-human-message state the
        # marker-keyed watchdog can't recover. We detect it by inline
        # content sitting BYTE-IDENTICAL across ``_SUBMIT_CONFIRM_TICKS``
        # polls: an *accepted* inline submit clears the box within a
        # render frame or two, so a frozen inline box past ~2s means the
        # Enter was dropped, not that the TUI is still rendering. Re-fire
        # C-m, bounded.
        #
        # Scoped to INLINE content only. A ``[Pasted text #`` placeholder
        # (large payload) keeps the patient-poll-only path + watchdog
        # backstop, so we don't re-introduce the 2026-04-20 stacking
        # regression (retry C-m mid-commit on a large payload). The
        # kill-switch (_confirmed_submit_disabled) also gates it off
        # entirely — fail-open to today's behavior.
        retry_on = not _confirmed_submit_disabled()
        prev_inline: str | None = None
        stable = 0
        retries_left = _SUBMIT_CONFIRM_RETRIES
        for _ in range(24):
            time.sleep(0.5)
            if (not _has_pending_paste(tmux, session)
                    and not _input_line_has_typing(tmux, session)):
                _deferring_sessions.discard(session)
                return True
            if not retry_on:
                continue
            # Dirty. Only INLINE content (no placeholder) is a
            # confirmed-submit candidate; read it to test byte-stability.
            inline = input_box_content(session)
            if inline is None or "[Pasted text #" in inline:
                # Placeholder path, or a transient empty read — leave to
                # the patient poll / watchdog; reset the stability window.
                prev_inline = None
                stable = 0
                continue
            if _is_residual_paste_tail(message, inline):
                # Accepted submit + late-landing paste tail. Re-firing C-m
                # here is what turns a harmless leftover into a spurious
                # user turn — see _is_residual_paste_tail. Leave it for the
                # next paste to overwrite; do not retry, do not count it
                # toward stability.
                prev_inline = None
                stable = 0
                continue
            if inline == prev_inline:
                stable += 1
            else:
                prev_inline = inline
                stable = 0
            if stable >= _SUBMIT_CONFIRM_TICKS and retries_left > 0:
                # Eaten submit — re-fire C-m (raw 0x0D; the Enter keysym
                # is unreliable in Claude Code's TUI). Bare C-m never
                # interrupts a running tool, so this is safe regardless
                # of escape_prefix.
                print(
                    f"[tmux.submit] confirmed-submit re-fire C-m in {session} "
                    f"(inline content stalled, submit eaten)",
                    file=sys.stderr,
                )
                subprocess.run(
                    [tmux, "send-keys", "-t", session, "C-m"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                retries_left -= 1
                stable = 0
                prev_inline = None

        # 12s elapsed and the input is still dirty. Don't fire a further
        # recovery C-m here — leave the buffered content for
        # submit_watchdog to handle on its next daemon tick, OR for the
        # next intentional caller to overwrite.
        return False

    except Exception:
        return False


def submit_watchdog(session: str) -> bool:
    """Scan *session* for a stale ``[Pasted text #`` placeholder and force
    Enter if found.

    Intended to run periodically from the gateway daemon. Returns True if
    no action was needed or recovery succeeded; False on hard failure.
    Never raises.
    """
    try:
        tmux = _find_tmux()
        if not tmux:
            return False

        if not _has_session(tmux, session):
            return True  # no session = nothing to fix

        if not _has_pending_paste(tmux, session):
            return True  # clean

        # Force submit, up to 2 attempts
        for _ in range(2):
            subprocess.run(
                # C-m (ASCII 0x0D) instead of the 'Enter' keysym. tmux 3.3a's
                # 'Enter' keysym doesn't trigger submit in Claude Code's
                # TUI (Ink/React), but raw C-m does. Root cause of the
                # 2026-04-20 wake-Enter race: text typed but Enter keysym
                # silently dropped by the TUI input handler.
                [tmux, "send-keys", "-t", session, "C-m"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            time.sleep(0.5)
            if not _has_pending_paste(tmux, session):
                return True

        return False

    except Exception:
        return False
