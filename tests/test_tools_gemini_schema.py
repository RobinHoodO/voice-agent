"""to_gemini_schema: the pure OpenAI-flat → Gemini function-declaration reshape."""
from core.tools import TOOLS, to_gemini_schema


def test_top_level_shape():
    out = to_gemini_schema(TOOLS)
    assert isinstance(out, list) and len(out) == 1
    assert set(out[0].keys()) == {"functionDeclarations"}
    assert len(out[0]["functionDeclarations"]) == len(TOOLS)


def test_every_tool_name_survives():
    decls = to_gemini_schema(TOOLS)[0]["functionDeclarations"]
    assert {d["name"] for d in decls} == {t["name"] for t in TOOLS}


def test_type_key_is_dropped():
    decls = to_gemini_schema(TOOLS)[0]["functionDeclarations"]
    assert all("type" not in d for d in decls)


def test_parameters_pass_through_unchanged_and_source_not_mutated():
    decls = {d["name"]: d for d in to_gemini_schema(TOOLS)[0]["functionDeclarations"]}
    src = next(t for t in TOOLS if t["name"] == "run_shell")
    assert decls["run_shell"]["parameters"] == src["parameters"]
    assert decls["run_shell"]["description"] == src["description"]
    assert all(t.get("type") == "function" for t in TOOLS)   # TOOLS itself untouched


def test_json_schema_type_unions_collapse_to_a_single_type():
    """Live-verified 2026-08-02: Gemini's function-declaration Schema rejects
    `"type": ["integer", "string"]` (a valid JSON-Schema union, used by a few tools
    for flexible id fields) with a hard connection close — it wants exactly one
    type. Every such union in TOOLS must collapse to a single string here."""
    decls = {d["name"]: d for d in to_gemini_schema(TOOLS)[0]["functionDeclarations"]}
    src_by_name = {t["name"]: t for t in TOOLS}
    checked = 0
    for name, prop in [("kernel_decide", "approvalId"), ("bloom_create_task", "projectId"),
                       ("bloom_update_task", "id"), ("bloom_comment_task", "id")]:
        src_type = src_by_name[name]["parameters"]["properties"][prop]["type"]
        assert isinstance(src_type, list), f"fixture assumption broke: {name}.{prop} is no longer a union"
        gemini_type = decls[name]["parameters"]["properties"][prop]["type"]
        assert gemini_type == "string"
        checked += 1
    assert checked == 4
    # source TOOLS list itself must stay untouched (same non-mutation guarantee as above)
    assert src_by_name["kernel_decide"]["parameters"]["properties"]["approvalId"]["type"] == ["integer", "string"]
