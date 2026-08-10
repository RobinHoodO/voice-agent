"""Characterization tests for the tools.py + live_prompt.py extractions (Phase 4)."""
from core import kernel_tools
from core import live_prompt
from core import tools


def test_extract_json_plain_and_embedded():
    assert tools._extract_json('{"a": 1}') == {"a": 1}
    assert tools._extract_json('prose before {"b": 2} and after') == {"b": 2}
    assert tools._extract_json("no json here") is None
    assert tools._extract_json("") is None


def test_tools_schema_shape():
    names = {t["name"] for t in tools.TOOLS}
    assert {"run_shell", "remember", "recall", "put_text", "delegate", "set_prompt"} <= names


def test_delegation_line_off_is_empty():
    assert live_prompt._delegation_line({"live": {"delegate": "off"}}) == ""
    assert "pi" in live_prompt._delegation_line({"live": {"delegate": "pi"}})


def test_build_live_instructions_includes_base(tmp_app):
    out = live_prompt._build_live_instructions("on-screen text here")
    assert "hands-free voice agent" in out          # LIVE_SYSTEM base present
    assert "on-screen text here" in out             # the per-turn ctx is appended


def test_build_live_instructions_prepends_kernel_attention(monkeypatch, tmp_app):
    seen = []
    monkeypatch.setattr(
        kernel_tools,
        "kernel_attention_brief",
        lambda timeout: seen.append(timeout) or "KERNEL ATTENTION: 1 approval pending — mention this briefly.",
    )
    monkeypatch.setattr(kernel_tools, "kernel_persona", lambda timeout: "")
    out = live_prompt._build_live_instructions("")
    assert out.startswith("KERNEL ATTENTION:")
    assert seen == [2.5]


# ── the phone surface must not be promised a shell it does not have ──────────
# LIVE_SYSTEM is SHARED by every surface (check_tool_drift.py requires it) and it opens
# by describing a persistent shell. `capabilities.surface_note` contradicts that in
# words. But `_build_live_instructions` also BUILDS blocks of its own — the workspace
# line, the focus line, the herdr digest — and each of those used to end in "…with
# run_shell". A block we wrote ourselves that names a tool this session does not have is
# the same broken promise, just in our handwriting.

def _phone_prompt(monkeypatch, cfg):
    monkeypatch.setattr(kernel_tools, "kernel_attention_brief", lambda timeout: "")
    monkeypatch.setattr(kernel_tools, "kernel_persona", lambda timeout: "")
    return live_prompt._build_live_instructions("", cfg=cfg, profile="phone")


PROMPT_CFG = {"live": {"workspace": "/tmp",
                       "focus": {"name": "Mingle", "dir": "/tmp"},
                       "memory": {"recall_count": 0}}}


def test_the_phone_prompt_never_tells_it_to_use_run_shell(monkeypatch, tmp_app):
    """LIVE_SYSTEM is excluded from this scan on purpose, and that is not a loophole:
    it is ONE shared text across every surface (check_tool_drift.py refuses a
    per-surface base prompt, because two base prompts is two agents), so it cannot be
    made conditional and is contradicted in words instead — see the next test. What IS
    scanned is everything this function assembles itself, where a per-session block can
    be conditional and therefore must be."""
    out = _phone_prompt(monkeypatch, PROMPT_CFG)
    assembled = out.replace(live_prompt.LIVE_SYSTEM, "")
    assert live_prompt.LIVE_SYSTEM in out, "the base prompt moved; re-read this test"

    offenders = [line for line in assembled.splitlines()
                 if "run_shell" in line
                 and "no run_shell in your tool list" not in line
                 and "not in your tool list" not in line]
    assert offenders == [], offenders


def test_the_phone_prompt_says_plainly_that_there_is_no_shell(monkeypatch, tmp_app):
    """Absence has to be STATED, not merely implied by a missing tool: a model that
    infers it from silence infers other things too."""
    out = _phone_prompt(monkeypatch, PROMPT_CFG)
    assert "You have NO shell here" in out
    assert "does not apply on this surface" in out


def test_the_phone_prompt_names_the_way_out(monkeypatch, tmp_app):
    """A wall with no door is where a model starts inventing. The workspace and focus
    blocks have to say who reads the files instead."""
    out = _phone_prompt(monkeypatch, PROMPT_CFG)
    assert "os_delegate" in out
    workspace_line = next(l for l in out.splitlines() if l.startswith("Workspace: "))
    assert "delegate" in workspace_line


def test_the_desk_prompt_still_uses_the_shell(monkeypatch, tmp_app):
    """The other direction: nothing was taken away from the surface Robin sits at."""
    monkeypatch.setattr(kernel_tools, "kernel_attention_brief", lambda timeout: "")
    monkeypatch.setattr(kernel_tools, "kernel_persona", lambda timeout: "")
    out = live_prompt._build_live_instructions("", cfg=PROMPT_CFG, profile="mac")
    assert "Read files there with run_shell" in out
    assert "NO shell" not in out
