"""Can't-do must escalate to a herdr lane, never degrade into a lesser action.

From the 2026-08-08 incident: Robin asked to move a task to the AAA database.
notion_update_task can't do that, so the model wrote five different statuses to
the page instead of saying so. The tool descriptions are the only place the model
learns this, so pin them.
"""
import tools

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
