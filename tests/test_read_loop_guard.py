"""Guard against a READ tool spun on varying args.

Replays the real 2026-08-10 incident: "what are my active tasks?" fired notion_list_tasks
49 times in 31 seconds, cycling status filters (Focus, Waiting, none, In Progress, …) so
no two calls matched and the exact-args guard never came near its limit. She never spoke.
"""
from core.live_session import (TOOL_NAME_LIMIT, TOOL_REPEAT_LIMIT, _tool_name_count,
                          _tool_repeat_count)

# The real spin: status filters AND title keywords both varied, so 49 calls produced far
# more than 49/3 distinct keys and no exact-args key ever recurred three times in the
# window. `None` = the default all-open listing; the rest are distinct (status, query) pairs.
_STATUSES = ["Focus", "Waiting", "Encountered Challenge", "In Progress", "Next Up",
             "Backlog", "Inbox", None]
_QUERIES = ["", "linkedin", "berlin", "instagram", "bloom", "vr", ""]
INCIDENT = [(i * 0.65, _STATUSES[i % len(_STATUSES)], _QUERIES[i % len(_QUERIES)])
            for i in range(49)]


def _args(status, query=""):
    a = {}
    if status:
        a["status"] = status
    if query:
        a["query"] = query
    return a


def test_exact_args_guard_is_blind_to_the_read_loop():
    """WHY it got through: status AND query both varied, so distinct keys outnumber any
    that could recur — the exact-args counter never reaches its limit across the window."""
    recent, worst = [], 0
    for t, s, q in INCIDENT:
        worst = max(worst, _tool_repeat_count(recent, "notion_list_tasks", _args(s, q), t))
    assert worst < TOOL_REPEAT_LIMIT, (
        f"exact-args key recurred {worst}x — reproduction isn't faithful to the incident")


def test_name_guard_stops_the_read_loop_early():
    """The fix: the ceiling is on the tool NAME, any args. It must bite in the first
    handful of calls, not the 49th."""
    recent, refused_at = [], None
    for i, (t, s, q) in enumerate(INCIDENT):
        _tool_repeat_count(recent, "notion_list_tasks", _args(s, q), t)   # trims + appends
        if _tool_name_count(recent, "notion_list_tasks") >= TOOL_NAME_LIMIT:
            refused_at = i
            break
    assert refused_at == TOOL_NAME_LIMIT - 1, f"refused on call index {refused_at}"
    assert refused_at < 10, "must stop the spin early, not let dozens through"


def test_a_couple_of_honest_follow_ups_are_allowed():
    """'open tasks' then 'just the Focus ones' then 'anything waiting' — three legit calls
    must pass. The ceiling is for a spiral, not for normal narrowing."""
    recent = []
    for i, s in enumerate(["", "Focus", "Waiting"]):
        _tool_repeat_count(recent, "notion_list_tasks", _args(s or None), i * 3.0)
        assert _tool_name_count(recent, "notion_list_tasks") < TOOL_NAME_LIMIT


def test_the_ceiling_is_per_name_not_across_tools():
    """Chaining DIFFERENT tools (search -> read -> search) is encouraged and must not trip
    the per-name ceiling."""
    recent = []
    for i, name in enumerate(["semsearch_query", "notion_read_page", "hybrid_rag_search",
                              "notion_search", "web_search", "read_url", "os_map_search"]):
        _tool_repeat_count(recent, name, {"q": "x"}, i * 1.0)
        assert _tool_name_count(recent, name) == 1
