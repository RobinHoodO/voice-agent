"""Characterization tests for the tools.py + live_prompt.py extractions (Phase 4)."""
import kernel_tools
import live_prompt
import tools


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
