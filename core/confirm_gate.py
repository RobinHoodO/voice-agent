"""The staged-confirm gate — ONE implementation, for every surface that can act.

This is the machinery that used to live inside `core/live_session.py`: the vocabulary
of what counts as a spoken yes, the deterministic outcome for a staged action, and the
ONE pending slot that a second staging attempt is refused from. It moved here unchanged
for one reason: a second surface now needs it.

The reverse channel (`mac/reverse_channel.py`) lets phone-Pam act on Robin's Mac. Its
mutating calls have to clear the same bar as the Mac's own `run_shell`, and the only
way to guarantee "the same bar" is to make it literally the same code. A second gate
implementation is how two gates drift, and a drifted gate is a gate that is weaker
somewhere nobody looked. `tests/test_reverse_channel.py` asserts the identity — both
surfaces hold a `PendingSlot` from this module — so a future copy-paste fails a test
instead of shipping.

WHAT THE GATE IS, in three deliberate parts:

  * `is_short_affirm` — an ALLOWLIST of complete affirmations, not a containment test.
    "did you approve that?", "yes but wait" and "approved yesterday" all contain an
    affirm word and none of them is consent. Which list applies is per-tool DATA
    (`core.capabilities.confirm_strictness`): an irreversible `rm -rf` needs a plainer
    yes than a recoverable email.
  * `pending_confirmation_outcome` — deny beats affirm, and the TTL beats both.
  * `PendingSlot` — exactly one action may wait at a time, and a second stage while one
    is live is REFUSED, never queued and never swapped in. A confirmation is bound to
    the sentence the user was actually read; last-wins would let a model stage an email,
    read it out, then swap in a delete before he answers.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: speak, log, journal, or execute. Those are
per-surface effects and they stay with the surface — this file only decides. That is
what makes it shareable between an asyncio voice session and a synchronous HTTP handler.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

from core import capabilities

# How long a staged action stays confirmable. Long enough for a sentence to be read out
# and answered; short enough that a forgotten stage cannot be executed by an unrelated
# "yes" ten minutes later.
PENDING_ACTION_TTL_SECONDS = 120

DENY_RE = re.compile(r"\b(no|nope|cancel|reject|rejected|stop|abort|don't|nei)\b", re.IGNORECASE)

# --- what counts as a spoken YES -----------------------------------------------------
# Courtesy words that neither add nor remove consent. Stripped from either end before
# the phrase is matched, so "yes please" and "ok, do it now" reduce to "yes"/"do it".
_AFFIRM_COURTESY = frozenset({"now", "then", "ok", "okay", "sir", "pam", "and", "så"})

# "please" is courtesy at the END of an affirmation ("yes please") and a REQUEST at the
# start of one. "please confirm" is Robin asking Pam to confirm, not Robin confirming —
# it used to clear the normal bar because "please" was stripped as courtesy and
# "confirm" is a complete affirmation. A leading request marker is now a disqualifier,
# in the same place as the interrogative openers and for the same reason.
_AFFIRM_COURTESY_TRAILING = _AFFIRM_COURTESY | {"please", "takk"}
_REQUEST_OPENERS = frozenset({"please", "kindly", "vennligst"})

# A question is a request for information, never an instruction. Both the punctuation
# and the opener are checked: transcripts frequently drop the "?".
# ("do" is deliberately absent: "do it" is the affirmation itself. "do you think…" is
#  rejected by the phrase allowlist below, which is the fail-closed half of this pair.)
_INTERROGATIVE = frozenset({
    "did", "does", "is", "are", "was", "were", "why", "what", "when", "where",
    "how", "should", "shall", "can", "could", "would", "will", "who", "which",
    "har", "hva", "hvorfor", "når", "skal", "kan",
})

# A hedge or continuation means he has not finished deciding. "yes but", "yes — wait",
# "ja, men vent" are all mid-thought, and mid-thought is not consent.
_HEDGE = frozenset({
    "but", "wait", "hold", "hang", "actually", "though", "if", "unless", "maybe",
    "perhaps", "first", "before", "after", "later", "men", "vent", "kanskje",
})

# The complete affirmations. Normal bar: recoverable actions (an email, a kernel
# decision). Every entry is a whole utterance, not a substring.
AFFIRM_PHRASES_NORMAL = frozenset({
    "yes", "yeah", "yep", "yup", "yes yes", "yes yeah",
    "yes do it", "yes go ahead", "yes send it", "yes confirm", "yes confirmed",
    "yes proceed", "yes approved",
    "confirm", "confirmed", "confirm it", "confirm that", "i confirm",
    "approve", "approved", "approve it", "approve that", "i approve",
    "go", "go ahead", "go for it", "do it", "do that", "send it", "send that",
    "proceed", "proceed with it",
    "ja", "ja da", "ja gjør det", "gjør det", "gjor det",
    "kjør", "kjor", "kjør det", "kjor det", "ja kjør", "ja kjor", "ja kjør det",
})

# Strict bar: irreversible actions, currently `run_shell`. Narrower vocabulary — the
# weak affirmations ("confirm", "approved", "proceed") are the ones that show up inside
# questions and recollections, so they do not carry a delete. At most three words.
AFFIRM_PHRASES_STRICT = frozenset({
    "yes", "yeah", "yep", "yup", "yes do it", "yes go ahead",
    "go", "go ahead", "do it",
    "ja", "kjør", "kjor", "kjør det", "kjor det", "ja kjør", "ja kjor",
})

AFFIRM_PHRASES = {
    capabilities.AFFIRM_NORMAL: AFFIRM_PHRASES_NORMAL,
    capabilities.AFFIRM_STRICT: AFFIRM_PHRASES_STRICT,
}

# Word budget per bar, on the RAW utterance (before courtesy words are dropped).
AFFIRM_MAX_WORDS = {
    capabilities.AFFIRM_NORMAL: 4,
    capabilities.AFFIRM_STRICT: 3,
}

_WORD_RE = re.compile(r"[^\w'’-]+", re.UNICODE)


def affirm_words(text: str) -> list:
    """Lowercased words, punctuation stripped. `_WORD_RE` splits on anything that is not
    a word character, so "yes, kjør — det" becomes ['yes', 'kjør', 'det']."""
    return [w for w in _WORD_RE.split((text or "").lower().strip()) if w]


def is_short_affirm(text: str,
                    strictness: str = capabilities.DEFAULT_CONFIRM_STRICTNESS) -> bool:
    """True only for a complete, unhedged, non-interrogative affirmation.

    Fail closed in every direction: an utterance that is a question, carries a hedge,
    opens with an interrogative, runs long, or simply is not on the list for this
    strictness is NOT consent.
    """
    raw = (text or "")
    words = affirm_words(raw)
    if not words:
        return False
    if "?" in raw:                                   # (a) it is a question
        return False
    if words[0] in _INTERROGATIVE:                   # (a) …even without the "?"
        return False
    if words[0] in _REQUEST_OPENERS:                 # (a') "please confirm" is a request
        return False
    if any(w in _HEDGE for w in words):              # (c) hedge / continuation
        return False
    trimmed = list(words)
    while trimmed and trimmed[0] in _AFFIRM_COURTESY:
        trimmed.pop(0)
    while trimmed and trimmed[-1] in _AFFIRM_COURTESY_TRAILING:
        trimmed.pop()
    if not trimmed:
        return False
    # The budget is applied AFTER the courtesy words are dropped, so "ok, do it now"
    # costs the same as "do it". It is a cheap early exit; the allowlist below is the
    # actual gate (no phrase on either list is longer than three words anyway).
    if len(trimmed) > AFFIRM_MAX_WORDS.get(strictness, 3):
        return False
    phrases = AFFIRM_PHRASES.get(strictness, AFFIRM_PHRASES_STRICT)
    # (b) the affirmation IS the utterance — it does not merely appear inside one.
    return " ".join(trimmed) in phrases


def is_short_deny(text: str) -> bool:
    return len(text.strip().split()) <= 4 and bool(DENY_RE.search(text))


def confirmation_preview(tool: str, args: dict, host: str = "this machine") -> str:
    """The sentence the model reads back before Robin says yes. Specific beats generic —
    'send an email to X' is checkable by ear; 'run gmail_send' is not."""
    if tool == "run_shell":
        # No classifier verdict in this sentence any more (`core.destructive` is gone —
        # see core/capabilities.py). Nothing decides that a command is "safe enough" to
        # skip the gate, so nothing has to explain why this one did not: the only
        # surface that still stages a command stages EVERY command, and the command
        # itself is the thing Robin has to hear.
        command = (args.get("command") or "").strip()
        return f"about to run this on {host} — the command is: {command}"
    if tool == "gmail_send":
        return (f"about to send an email to {args.get('to')} "
                f"with subject '{args.get('subject')}'")
    if tool == "kernel_decide":
        return (f"about to record decision {args.get('decision')} for approval "
                f"{args.get('approvalId', args.get('approval_id'))}")
    if tool == "close_finished_tasks":
        return f"about to close the foreign pane '{args.get('task_name')}'"
    if tool == "open_file":
        # "about to open X" was the sentence an audit exploited: it reads as opening a
        # document, and the thing being opened was a `.command` that LaunchServices then
        # RAN. The surface now refuses anything runnable and names the viewer itself
        # (`mac.reverse_channel.open_refusal` / `open_argv`), so this sentence can say
        # what actually happens — and it says it out loud, because "shown, not run" is
        # the whole difference Robin is being asked to agree to.
        return (f"about to show {args.get('path')} on {host} in a viewer "
                f"— it is displayed, not run")
    known = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3]) or "no arguments"
    return f"about to run {tool.replace('_', ' ')} with {known}"


def pending_confirmation_outcome(pending: dict, transcript: str,
                                 now: float | None = None) -> str:
    """Return the deterministic disposition for a staged action.

    The affirmation bar depends on WHAT is staged — `capabilities.confirm_strictness`
    — so an irreversible `run_shell` needs a plainer yes than a recoverable email.
    DENY still beats AFFIRM, and the TTL still beats both."""
    now = time.time() if now is None else now
    if now - pending["ts"] >= PENDING_ACTION_TTL_SECONDS:
        return "expired"
    if is_short_deny(transcript):
        return "denied"
    if is_short_affirm(transcript, capabilities.confirm_strictness(pending.get("tool"))):
        return "confirmed"
    return "dropped"


# --- the one slot --------------------------------------------------------------------

STAGED = "staged"
REFUSED = "refused"


@dataclass(frozen=True)
class StageResult:
    """What `PendingSlot.stage` did, with the two actions a caller may need to talk
    about: the one now waiting, and the expired one that was dropped to make room."""

    status: str                     # STAGED | REFUSED
    pending: dict | None            # STAGED: the new action. REFUSED: the one blocking.
    displaced: dict | None = None   # an EXPIRED action that was dropped, if any
    action_id: str | None = None    # handle for the staged action (see PendingSlot._id)

    @property
    def staged(self) -> bool:
        return self.status == STAGED


class PendingSlot:
    """The ONE staged action, and the rules for filling and emptying it.

    Deliberately not thread-safe by itself: the voice session drives it from a single
    asyncio thread, and the reverse channel wraps it in a lock. Making it lock
    internally would hide from the HTTP surface that stage→speak→confirm is a sequence
    that must not interleave, which is the whole point of there being one slot.
    """

    # No per-instance TTL: `pending_confirmation_outcome` reads the module constant, so
    # an instance that could shorten its own expiry would disagree with the function
    # that decides the outcome. One TTL, one place. Tests age an action by moving its
    # `ts` back, which is what the voice suite already does.
    def __init__(self) -> None:
        self.ttl = PENDING_ACTION_TTL_SECONDS
        self._current: dict | None = None
        # The handle a caller quotes back when confirming. Kept BESIDE the action, not
        # inside it: `{tool, args, ts}` is the shape the voice suite pins
        # (test_shell_gate::…reuses_the_high_stakes_machinery…), and that pin is what
        # stops a second, subtly different pending record from appearing. An id is
        # bookkeeping for the HTTP surface, not part of what was staged.
        self._id: str | None = None

    # `current` is a plain attribute-style accessor because the voice session exposes
    # it as `_pending_action` and tests reach into the dict (mutating `ts` to simulate
    # the TTL). Keeping it a dict is part of the contract, not an accident.
    @property
    def current(self) -> dict | None:
        return self._current

    @current.setter
    def current(self, value: dict | None) -> None:
        self._current = value
        self._id = uuid.uuid4().hex if value else None

    @property
    def current_id(self) -> str | None:
        return self._id

    def is_live(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self._current) and (now - self._current["ts"]) < self.ttl

    def stage(self, tool: str, args: dict, *, now: float | None = None) -> StageResult:
        """Occupy the slot, or refuse — never overwrite a LIVE action.

        An EXPIRED action is not live: it is dropped (and handed back as `displaced`
        so the caller can journal it) rather than blocking the gate for the rest of
        the TTL.
        """
        now = time.time() if now is None else now
        busy = self._current
        if busy and (now - busy["ts"]) < self.ttl:
            return StageResult(REFUSED, busy, action_id=self._id)
        displaced = busy if busy else None
        self._current = {"tool": tool, "args": args, "ts": now}
        self._id = uuid.uuid4().hex
        return StageResult(STAGED, self._current, displaced, action_id=self._id)

    def resolve(self, transcript: str, *, now: float | None = None,
                action_id: str | None = None) -> tuple[str, dict | None]:
        """Empty the slot and say what should happen to what was in it.

        Returns `(outcome, pending)`; `("none", None)` when nothing was staged.
        Outcomes: confirmed | denied | dropped | expired | mismatch.

        `action_id`, when given, must match the staged action. That check exists for
        the reverse channel, where the confirmation arrives as a separate HTTP call and
        an unbound "yes" could otherwise land on whatever happens to be staged now
        rather than the action the caller was shown. A mismatch does NOT consume the
        slot: the real pending action is still waiting for its own answer.
        """
        pending = self._current
        if not pending:
            return "none", None
        if action_id is not None and action_id != self._id:
            return "mismatch", pending
        self._current, self._id = None, None
        return pending_confirmation_outcome(pending, transcript, now=now), pending

    def clear(self) -> dict | None:
        was, self._current, self._id = self._current, None, None
        return was
