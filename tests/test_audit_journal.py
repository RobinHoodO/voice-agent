"""`actions.jsonl` — the record of what was DONE, on both surfaces.

The bridge had this and Pam did not, which is the gap that mattered most in
`docs/BRIDGE-DECOMMISSION.md`: `conversations.db` records what was SAID. Nothing
recorded that an email went out, that a kernel approval was decided, or — now — that a
shell command ran on Thrivbe-1, independently of a transcript that may be deleted for
exactly the privacy reasons that make it deletable.

The tests that matter here are the two that are easy to lose:

  * `test_the_journal_never_stores_a_message_body` — a journal that copies the email it
    is recording is a second transcript wearing a compliance hat;
  * `test_both_surfaces_journal` — the writer lives in `core`, so a new surface cannot
    ship without it. Asserted by running the same call on both profiles.
"""
import asyncio
import json
import os
import stat

import pytest

from core import audit, live_session

from test_shell_gate import FakeShell, Ws, call_shell, make_session, run


def entries():
    return audit.read_all()


# ── the record itself ────────────────────────────────────────────────────────
def test_a_tool_call_is_recorded(monkeypatch):
    session = make_session("mac", monkeypatch)

    async def scenario(s):
        await call_shell(s, "ls /opt")

    run(lambda: (session, scenario))
    (line,) = [e for e in entries() if e["tool"] == "run_shell"]
    assert line["event"] == "call"
    assert line["surface"] == "mac"
    assert line["args"]["command"] == "ls /opt"
    assert "ran: ls /opt" in line["result"]
    assert line["ts"]


def test_the_file_is_owner_only():
    """It holds who was contacted and what was run. 0600 on creation, and again after
    a rotation — the bridge's `chmod` was on the create path only."""
    audit.record("call", "kernel_status", {}, "ok")
    mode = stat.S_IMODE(os.stat(audit.path()).st_mode)
    assert mode == 0o600, oct(mode)


def test_the_journal_never_stores_a_message_body():
    """Free text is recorded as a LENGTH. An accountability record that quotes the mail
    it is recording has become the thing it was supposed to be separate from."""
    audit.record("confirmed", "gmail_send",
                 {"to": "someone@example.com", "subject": "Invoice 42",
                  "body": "Hi Anna, here are the bank details you asked for."},
                 "sent")
    line = entries()[-1]
    assert "bank details" not in json.dumps(line)
    assert line["args"]["body"] == "<redacted 49 chars>"
    # …and the subject survives, because "which mail was it" has to be answerable.
    assert line["args"]["subject"] == "Invoice 42"


def test_recipients_are_recognisable_but_not_readable():
    audit.record("confirmed", "gmail_send", {"to": "anna@example.com"}, "sent")
    masked = entries()[-1]["args"]["to"]
    assert masked.endswith(".com") and set(masked[:-4]) == {"*"}
    assert "anna" not in masked and "example" not in masked
    assert len(masked) == len("anna@example.com")   # length is not itself a secret


def test_redaction_reaches_nested_arguments():
    """Args nest — a body one level down is still a body."""
    audit.record("call", "front_draft",
                 {"draft": {"to": "anna@example.com", "body": "secret words"}}, "ok")
    nested = entries()[-1]["args"]["draft"]
    assert "secret words" not in json.dumps(nested)
    assert nested["body"].startswith("<redacted")


def test_the_result_does_not_re_leak_what_the_args_redacted():
    """The masking in `args` is cosmetic if the same value is spelled out in `result`.

    A staged action's result IS the confirmation sentence, and that sentence
    interpolates the arguments — so the recipient was masked in one field of the line
    and printed in the next. Same line, same value, one of them readable.
    """
    audit.record("staged", "gmail_send",
                 {"to": "anna@example.com", "subject": "Q3 layoffs",
                  "body": "the whole memo"},
                 "CONFIRMATION REQUIRED: about to send an email to anna@example.com "
                 "with subject 'Q3 layoffs'. Ask the user to confirm out loud.")
    line = entries()[-1]
    blob = json.dumps(line)
    assert "anna@example.com" not in blob
    assert "the whole memo" not in blob
    # …and the line is still readable as a record: the tail identifies the recipient.
    assert "************.com" in line["result"]
    assert "CONFIRMATION REQUIRED" in line["result"]


def test_the_scrub_runs_before_truncation():
    """Truncation is not redaction. A recipient sitting past the 300th character of a
    long preview would survive a scrub applied to the truncated text."""
    audit.record("staged", "gmail_send",
                 {"to": "anna@example.com"},
                 "x" * (audit.RESULT_CHARS - 5) + " anna@example.com")
    assert "anna@example.com" not in json.dumps(entries()[-1])


def test_the_scrub_reaches_nested_and_long_values_only():
    """It masks what `redact` masked — nested args included — and does not chew up a
    result over a one-character argument."""
    assert "secret words" not in audit.scrub(
        "the note said secret words", {"draft": {"body": "secret words"}})
    assert audit.scrub("a b c", {"to": "b"}) == "a b c"


def test_a_refusal_does_not_journal_the_other_actions_arguments(monkeypatch):
    """The gate-busy refusal SPEAKS a preview of the action holding the gate. That
    sentence carries the other tool's recipient into a record whose own `args` are a
    shell command — where this module's redaction cannot see it."""
    session = make_session("server", monkeypatch)
    monkeypatch.setattr(live_session.services, "gmail_send", lambda args: "sent")

    async def scenario(s):
        await s._do_tool({"call_id": "c1", "name": "gmail_send",
                          "arguments": json.dumps({"to": "anna@example.com",
                                                   "subject": "Q3", "body": "b"})})
        s._ws.sent.clear()
        await call_shell(s, "rm -rf /opt/a")     # refused: the gate is occupied

    run(lambda: (session, scenario))
    refusal = [e for e in entries() if e["event"] == "refused_gate_busy"][-1]
    assert "anna@example.com" not in json.dumps(refusal)
    assert "gmail_send" in refusal["result"]     # it still says WHAT held the gate


def test_a_long_result_is_truncated_not_stored_whole():
    audit.record("call", "run_shell", {"command": "ls"}, "x" * 5000)
    assert len(entries()[-1]["result"]) <= audit.RESULT_CHARS


def test_journalling_never_raises(monkeypatch):
    """It sits in the hot path of a live conversation. A journal that can throw is a
    journal that gets deleted from the hot path after the first outage."""
    monkeypatch.setattr(audit, "path", lambda: "/nonexistent-dir-x/actions.jsonl")
    audit.record("call", "run_shell", {"command": "ls"}, "ok")   # must not raise


def test_rotation_keeps_the_permissions(monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 200)
    for _ in range(20):
        audit.record("call", "kernel_status", {}, "ok" * 40)
    rotated = audit.path() + ".1"
    assert os.path.exists(rotated)
    assert stat.S_IMODE(os.stat(rotated).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(audit.path()).st_mode) == 0o600


# ── gate events, not just calls ─────────────────────────────────────────────
def test_every_gate_event_is_recorded(monkeypatch):
    """Staged, denied, confirmed. "Nothing happened" has to be as provable as
    "something happened" — otherwise a missing line reads as a missing gate."""
    session = make_session("server", monkeypatch)

    async def scenario(s):
        await call_shell(s, "rm -rf /opt/a")
        await s._resolve_pending_action("no")
        s._ws.sent.clear()
        await call_shell(s, "rm -rf /opt/b")
        await s._resolve_pending_action("yes")

    run(lambda: (session, scenario))
    events = [(e["event"], e["args"].get("command")) for e in entries()]
    assert ("staged", "rm -rf /opt/a") in events
    assert ("denied", "rm -rf /opt/a") in events
    assert ("staged", "rm -rf /opt/b") in events
    assert ("confirmed", "rm -rf /opt/b") in events


def test_a_guard_refusal_is_recorded(monkeypatch):
    """The loop guard refusing 40 calls in 12s is the kind of thing you want to find
    afterwards without a transcript."""
    session = make_session("mac", monkeypatch)

    async def scenario(s):
        for _ in range(live_session.TOOL_REPEAT_LIMIT + 1):
            s._ws.sent.clear()
            await call_shell(s, "ls /opt")

    run(lambda: (session, scenario))
    assert any(e["event"] == "refused_loop_guard" for e in entries())


@pytest.mark.parametrize("profile", ["mac", "server"])
def test_both_surfaces_journal(monkeypatch, profile):
    """The writer is in core, so it cannot be surface-specific. Proven by running the
    same read on both profiles and finding a line tagged with each."""
    session = make_session(profile, monkeypatch)

    async def scenario(s):
        await call_shell(s, "ls /opt")

    run(lambda: (session, scenario))
    assert [e for e in entries() if e["surface"] == profile]


def test_a_pii_tool_call_is_flagged(monkeypatch):
    """Which lines involved other people's data is the first question a data-subject
    request asks."""
    session = make_session("mac", monkeypatch)
    monkeypatch.setattr(live_session.kernel_tools, "twenty_search_contacts",
                        lambda args: "no matches")

    async def scenario(s):
        await s._do_tool({"call_id": "c1", "name": "twenty_search_contacts",
                          "arguments": json.dumps({"name_query": "anna"})})

    run(lambda: (session, scenario))
    line = [e for e in entries() if e["tool"] == "twenty_search_contacts"][-1]
    assert line["pii"] is True
    assert line["args"]["name_query"].startswith("<redacted")
