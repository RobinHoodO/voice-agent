"""Any tool writing a constrained field must expose the real values as an enum.

2026-08-08: notion_update_task described statuses as prose examples ("e.g. Focus,
Backlog, Done") that omitted "Archived". Robin said "archive that one" and the model
picked the nearest value it had been shown — Done, which means the opposite. Prose
examples are a suggestion; an enum is a constraint.

This test is generic on purpose: it fails for tools that don't exist yet.
"""
from core import tools

NOTION_TASK_TOOLS = [t for t in tools.TOOLS if t["name"].startswith("notion_")]


def _status_params(tool):
    props = (tool.get("parameters") or {}).get("properties") or {}
    return {n: p for n, p in props.items() if "status" in n.lower()}


def test_every_notion_status_param_is_an_enum():
    gaps = [f"{t['name']}.{n}" for t in NOTION_TASK_TOOLS
            for n, p in _status_params(t).items() if "enum" not in p]
    assert not gaps, f"free-text status params (model will guess a wrong value): {gaps}"


def test_status_enums_all_use_the_single_source_of_truth():
    """Three copies of a list drift. One constant does not."""
    for t in NOTION_TASK_TOOLS:
        for n, p in _status_params(t).items():
            if "enum" in p:
                assert p["enum"] is tools.NOTION_TASK_STATUSES, \
                    f"{t['name']}.{n} has its own copy of the status list"


def test_the_two_statuses_that_actually_got_confused_are_both_present():
    assert "Archived" in tools.NOTION_TASK_STATUSES
    assert "Done" in tools.NOTION_TASK_STATUSES


def test_no_tool_advertises_statuses_as_prose_examples():
    """The failure mode was a description that listed some values and omitted others."""
    for t in NOTION_TASK_TOOLS:
        for n, p in _status_params(t).items():
            d = (p.get("description") or "")
            assert "e.g." not in d.lower(), (
                f"{t['name']}.{n} lists example statuses in prose — the model treats that "
                "as the full set. Put values in enum, not the description.")
