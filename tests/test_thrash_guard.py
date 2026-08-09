"""Guard against thrashing one record with different values.

Replays the real 2026-08-08 incident: "AI Agents: A Practical Guide" was written
Done -> Backlog -> Focus -> Done -> Next Up in 42 seconds while Robin was still
mid-sentence. The exact-args loop guard never fired because no two calls matched.
"""
from core.live_session import (TOOL_REPEAT_LIMIT, TOOL_TARGET_LIMIT, _tool_repeat_count,
                          _tool_target_count)

TASK = "AI Agents: A Practical Guide"
# (offset_seconds, status) exactly as they appear in agent.log
INCIDENT = [(0.0, "Done"), (5.0, "Backlog"), (33.0, "Focus"), (36.0, "Done"), (42.0, "Next Up")]


def _args(status):
    return {"title": TASK, "status": status}


def test_exact_args_guard_is_blind_to_the_incident():
    """Documents WHY the bug got through: statuses were nearly all distinct, so the
    exact-args counter never came close to TOOL_REPEAT_LIMIT. ('Done' recurs once,
    which is why this is < limit rather than == 0.)"""
    recent = []
    counts = [_tool_repeat_count(recent, "notion_update_task", _args(s), t)
              for t, s in INCIDENT]
    assert max(counts) < TOOL_REPEAT_LIMIT


def test_thrash_guard_stops_the_incident():
    recent = []
    refused_at = None
    for i, (t, s) in enumerate(INCIDENT):
        if _tool_target_count(recent, "notion_update_task", _args(s), t) >= TOOL_TARGET_LIMIT:
            refused_at = i
            break
    assert refused_at == 3, f"expected refusal on the 4th write, got {refused_at}"


def test_an_honest_change_of_mind_is_allowed():
    """Backlog, then 'actually, Next Up' must still go through."""
    recent = []
    for i, s in enumerate(("Backlog", "Next Up")):
        assert _tool_target_count(recent, "notion_update_task", _args(s), float(i)) < TOOL_TARGET_LIMIT


def test_different_tasks_do_not_count_against_each_other():
    recent = []
    for i in range(5):
        n = _tool_target_count(recent, "notion_update_task",
                               {"title": f"task {i}", "status": "Done"}, float(i))
        assert n == 0


def test_untracked_tools_are_unaffected():
    recent = []
    assert _tool_target_count(recent, "notion_search", {"query": "x"}, 0.0) == 0
    assert recent == []


def test_missing_title_is_not_treated_as_a_target():
    recent = []
    assert _tool_target_count(recent, "notion_update_task", {"status": "Done"}, 0.0) == 0


# --- the 12:00 spiral: 3 Clarissa rows written ~40x in 45s, ZERO guard fires ---------
# Root cause: _on_speech_started() cleared the guard window, and Robin was speaking
# throughout (trying to stop it). Protesting reset the guard. Thrash history must
# therefore live in its own list that a turn boundary does NOT clear.

CLARISSA = ["send clarissa: boardy ai platform + katapult future fest links",
            "follow up and connect with the metabolic community (per clarissa's suggestion)",
            "send clarissa magalhaes a written summary of insights + next steps from the call"]


def test_speech_clearing_the_shared_list_is_what_defeated_the_guard():
    """Reproduces the old behaviour: shared list + a clear each turn = never fires."""
    shared = []
    fired = False
    for i in range(40):
        title = CLARISSA[i % 3]
        if _tool_target_count(shared, "notion_update_task",
                              {"title": title, "status": "Done"}, i * 1.1) >= TOOL_TARGET_LIMIT:
            fired = True
        if i % 3 == 2:
            shared.clear()          # Robin speaks -> old code wiped the window
    assert not fired, "if this fires, the reproduction is wrong"


def test_dedicated_target_list_stops_the_spiral_despite_speech():
    """New behaviour: the write history is not cleared by a spoken turn."""
    targets = []
    writes_allowed = 0
    for i in range(40):
        title = CLARISSA[i % 3]
        if _tool_target_count(targets, "notion_update_task",
                              {"title": title, "status": "Done"}, i * 1.1) >= TOOL_TARGET_LIMIT:
            break
        writes_allowed += 1
    assert writes_allowed <= 9, f"should stop within ~3 writes per row, allowed {writes_allowed}"


def test_target_history_still_expires_by_time():
    """It owns its list now, so it must prune itself or it would never forget."""
    targets = []
    a = {"title": "x", "status": "Done"}
    for t in (0.0, 1.0, 2.0):
        _tool_target_count(targets, "notion_update_task", a, t)
    assert _tool_target_count(targets, "notion_update_task", a, 500.0) == 0
