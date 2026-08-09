"""Can't-do must escalate to a herdr lane, never degrade into a lesser action.

From the 2026-08-08 incident: Robin asked to move a task to the AAA database.
notion_update_task can't do that, so the model wrote five different statuses to
the page instead of saying so. The tool descriptions are the only place the model
learns this, so pin them.
"""
from core import tools

BY_NAME = {t["name"]: t for t in tools.TOOLS}


def test_update_task_forbids_the_consolation_prize():
    d = BY_NAME["notion_update_task"]["description"].lower()
    assert "do not substitute a status change" in d
    assert "different notion database" in d
    assert "delegate" in d, "must name the escalation tool, not just refuse"


def test_update_task_status_enum_separates_archived_from_done():
    st = BY_NAME["notion_update_task"]["parameters"]["properties"]["status"]
    assert "Archived" in st["enum"] and "Done" in st["enum"]
    assert "never 'done'" in st["description"].lower()


def test_delegate_advertises_itself_as_the_escalation_path():
    d = BY_NAME["delegate"]["description"].lower()
    assert "escalation path" in d
    assert "verbatim" in d, "Robin's own words must reach the lane unsummarised"


def test_delegate_still_requires_a_named_lane():
    """Escalation must not create anonymous panes Robin can't address by voice."""
    p = BY_NAME["delegate"]["parameters"]
    assert set(p["required"]) == {"instruction", "task_name"}


def test_escalation_target_exists():
    assert "delegate" in BY_NAME, "notion_update_task points at a tool that must be registered"


# --- the code layer: descriptions are advice, these paths are deterministic ----------

def test_thrash_refusal_names_the_escalation_tool():
    """The thrash guard fires exactly when a shallow tool substitutes for the real ask —
    its refusal must point at `delegate`, not just say stop."""
    from core import live_session
    hint = live_session._ESCALATION_HINT["notion_update_task"]
    assert "delegate" in hint and "verbatim" in hint
    # A hint for an untracked tool would never be appended to anything.
    assert set(live_session._ESCALATION_HINT) <= set(live_session._TOOL_TARGET_FIELD)


def test_invalid_status_refuses_without_a_network_call(monkeypatch):
    """An off-list status means the model is improvising ('archive' -> Done). The tool
    must refuse BEFORE any Notion call, and the refusal must name the escalation path."""
    from core import services

    def _boom(*a, **k):
        raise AssertionError("network call made for an invalid status")

    monkeypatch.setattr(services, "_notion", _boom)
    out = services.notion_update_task({"title": "anything", "status": "Archive it please"})
    assert "delegate" in out and "won't guess" in out


def test_valid_status_is_canonicalized_case_insensitively(monkeypatch):
    """'done' spoken lowercase must reach Notion as 'Done', not be refused."""
    from core import services
    calls = []

    def _fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":   # _find_tasks query
            return {"results": [{"id": "p1", "url": "https://notion.so/p1",
                                 "properties": {"Task name":
                                                {"title": [{"plain_text": "anything"}]}}}]}
        return {}

    monkeypatch.setattr(services, "_notion", _fake)
    out = services.notion_update_task({"title": "anything", "status": "done"})
    patched = [b for (m, _p, b) in calls if m == "PATCH"][0]
    assert patched["properties"]["Status"]["status"]["name"] == "Done"
    assert "Updated" in out and "https://notion.so/p1" in out
