"""actions.jsonl — the append-only, redacted record of what the agent actually DID.

Ported from voice-bridge (`server.py:journal_action/_redact_args`), which is the only
place in either system where a tool ACTION is recorded independently of the
conversation. That distinction is the whole point:

  * `conversations.db` and `live-turns.journal` (core/memory.py) are a chat log — what
    was said. Deleting them is a privacy improvement.
  * this is an accountability record — what was done, when, on whose word, and whether
    a gate stood in the way. Deleting it is evidence destruction.

Robin's assistant sends mail as him, decides kernel approvals, and runs shell commands
on his Mac. Every one of those needs to be answerable weeks later without replaying a
transcript — and so does the surface it happened FROM, which is why every line carries
one: an action taken while he was at the keyboard and one taken from his phone are not
the same event.

WHAT IS WRITTEN: one JSON object per line — timestamp, surface, session, event, tool,
redacted args, truncated result. Nothing else, and never the raw body of anything.

WHAT IS REDACTED, and why it is DATA: `REDACT_WHOLE` are fields that are free text a
human wrote or the agent generated (an email body, a memo, a delegated instruction) —
recorded as a length, never content. `REDACT_TAIL` are identifiers you need to
recognise but not to read (an address, a phone number) — last four characters only.
Redaction is recursive, because args nest.

THE RESULT IS REDACTED THROUGH THE SAME PATH. It has to be: the result of a STAGED
action is the confirmation sentence that was read out loud, and that sentence
interpolates the very arguments the `args` redaction just masked ("about to send an
email to anna@example.com…"). A blind review found exactly this on 2026-08-09 — the
recipient was masked in `args` and printed in full in `result`, which made the masking
cosmetic. So every value that was redacted out of `args` is masked out of the result
text as well, by the same replacement it got in `args`, before truncation. It is not a
pattern matcher and does not try to be: it removes the strings this record itself
decided were not for reading.

WHAT IS *NOT* REDACTED, stated plainly because it is a deliberate choice and not an
oversight: redaction is a key-name ALLOWLIST, so anything whose key is not in the two
sets above is written verbatim. Two consequences worth knowing before you read this
file out loud or ship it anywhere:

  * `run_shell` commands are recorded IN FULL, by design. The command is the whole
    point of the record — "something was deleted" is not an answer to
    "what did it delete?". A command that inlines a secret (`PGPASSWORD=… psql …`) puts
    that secret in this file. Treat actions.jsonl as sensitive as the shell history it
    describes: 0600, owner-only directory, never pasted into a ticket.
  * a future tool that declares a credential-shaped argument is covered only if its
    field name is in `REDACT_WHOLE`. The credential names below are pre-registered as a
    tripwire — no tool schema declares them today (`core/tools.py` has none), so they
    cost nothing now and catch the one that does later.

PERMISSIONS: the file is created 0600 and re-chmod'd on every rotation. It sits in the
same owner-only directory as the conversation store.

RETENTION: a size cap here, so both surfaces get one; `logrotate` on the server takes
over from it (the bridge kept daily × 14 compressed) without conflicting — a rotation
that finds the file gone simply starts a new one.
"""
from __future__ import annotations

import json
import os
import time

from core import config

# Free-text fields: recorded as a length, never as content. The second line is the
# credential tripwire described above — pre-registered, not currently reachable.
REDACT_WHOLE = frozenset({
    "body", "text", "message", "note", "notes", "instruction", "feedback",
    "custom_prompt", "prompt", "query", "name_query", "description", "comment",
    "password", "passphrase", "token", "api_key", "apikey", "secret", "credential",
    "credentials", "authorization", "auth", "access_token", "refresh_token",
    "private_key", "session_key",
})

# Identifiers: keep the tail so a line is recognisable, drop the rest.
REDACT_TAIL = frozenset({"to", "to_number", "phone_number", "email", "recipient", "cc", "bcc"})

TAIL_KEPT = 4

# How much of a tool's return value is worth keeping. Enough to tell success from
# failure; not enough to become a second transcript.
RESULT_CHARS = 300

# Rotate at this size. One generation, because this is a tripwire and a record, not an
# archive — the server's logrotate keeps the long tail.
MAX_BYTES = 5 * 1024 * 1024


def path() -> str:
    """Where the journal lives. `VOICE_AGENT_ACTIONS_LOG` overrides, which is how the
    server unit points it at a logrotate-managed path outside the app support dir."""
    return os.getenv("VOICE_AGENT_ACTIONS_LOG") or os.path.join(
        config.SUPPORT_DIR, "actions.jsonl")


def redact(args):
    """Recursively redact one args structure. Unknown keys pass through: the field
    names above are the ones that carry content, and inventing a blanket rule would
    make the journal useless for answering "which task did it change?"."""
    if isinstance(args, dict):
        out = {}
        for key, value in args.items():
            if key in REDACT_WHOLE and isinstance(value, str):
                out[key] = f"<redacted {len(value)} chars>"
            elif key in REDACT_TAIL and isinstance(value, str):
                out[key] = ("*" * max(0, len(value) - TAIL_KEPT)) + value[-TAIL_KEPT:]
            else:
                out[key] = redact(value)
        return out
    if isinstance(args, (list, tuple)):
        return [redact(item) for item in args]
    return args


# Below this length a "sensitive" value is not worth substituting out of a result: a
# one- or two-character recipient would blank out half the sentence and tell nobody
# anything. Four characters is the same tail `REDACT_TAIL` keeps.
MIN_SCRUB_LEN = 4


def _sensitive_pairs(args, out=None):
    """[(raw value, its redacted form)] for every arg this module would redact.

    Walks the same structure `redact` walks, so a field that gains redaction there is
    scrubbed out of the result here without a second list to keep in sync.
    """
    out = [] if out is None else out
    if isinstance(args, dict):
        for key, value in args.items():
            if isinstance(value, str) and len(value) >= MIN_SCRUB_LEN:
                if key in REDACT_WHOLE:
                    out.append((value, f"<redacted {len(value)} chars>"))
                elif key in REDACT_TAIL:
                    out.append((value, ("*" * max(0, len(value) - TAIL_KEPT))
                                + value[-TAIL_KEPT:]))
            _sensitive_pairs(value, out)
    elif isinstance(args, (list, tuple)):
        for item in args:
            _sensitive_pairs(item, out)
    return out


def scrub(result, args) -> str:
    """The result with every value redacted out of `args` masked the same way.

    Longest first, so a value that contains another (a body quoting the subject) does
    not get half-replaced and leave the tail readable.
    """
    text = str(result or "")
    for raw, masked in sorted(_sensitive_pairs(args), key=lambda p: -len(p[0])):
        if raw in text:
            text = text.replace(raw, masked)
    return text


def _truncate(value) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS - 1] + "…"


def _rotate(target: str) -> None:
    try:
        if os.path.getsize(target) < MAX_BYTES:
            return
        os.replace(target, target + ".1")
        os.chmod(target + ".1", 0o600)
    except OSError:
        pass


def record(event: str, tool: str, args=None, result=None, *, surface=None,
           session=None, pii=False) -> None:
    """Append one line. Never raises.

    A journal that can kill a live conversation is a journal that gets removed from the
    hot path, so every failure here is swallowed — the same posture as `config.activity`.
    The cost of a missed line is a gap in the record; the cost of a raised exception is
    a dropped call mid-sentence.
    """
    try:
        config.ensure_dirs()
        target = path()
        _rotate(target)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "surface": surface or "unknown",
            "session": (session or "")[:8] or None,
            "event": event,
            "tool": tool,
            "args": redact(args if isinstance(args, dict) else {}),
            # Scrub BEFORE truncating: the recipient can sit past the 300th character
            # of a long preview and truncation is not redaction.
            "result": _truncate(scrub(result, args if isinstance(args, dict) else {})),
        }
        if pii:
            entry["pii"] = True
        fresh = not os.path.exists(target)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if fresh:
            os.chmod(target, 0o600)
    except Exception:
        pass


def read_all() -> list:
    """Every entry, oldest first. For tests and for answering a question about what
    happened — nothing in the hot path reads this back."""
    try:
        with open(path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
