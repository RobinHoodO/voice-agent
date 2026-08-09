"""PII tools and the no-train pin, ported from voice-bridge.

The control the bridge had: once a turn touched a tool returning third-party personal
data, the follow-up model call was pinned so it could not fan out across providers.

The lesson the bridge ALSO taught, and the reason `require_no_train` validates instead
of substituting: the bridge pinned the literal string `router-safe`, that combo stopped
existing at the OmniRoute cutover, and for ten days the protection was an HTTP 400
rather than a pin. A control whose correctness depends on a model id staying spelled
the same is a control that expires silently.
"""
import json

import pytest

from core import live_session, privacy

from test_shell_gate import call_shell, make_session, run


def test_the_pii_tool_list_names_real_tools():
    """A rotted entry protects nothing — and rots silently, because the tool it names
    simply never fires."""
    from core.tools import TOOLS

    names = {t["name"] for t in TOOLS}
    unknown = sorted(privacy.PII_TOOLS - names)
    assert not unknown, f"PII_TOOLS names tools core does not ship: {unknown}"


def test_the_obvious_third_party_readers_are_covered():
    """These are the tools that return someone who is not Robin. Losing one is the
    whole failure mode, so they are named rather than counted."""
    for tool in ("twenty_search_contacts", "list_inbox_items", "semsearch_query",
                 "kernel_recall", "cognee_ask"):
        assert privacy.touches_pii(tool), tool


def test_a_fan_out_route_is_refused_after_a_pii_tool():
    """`auto/*` means "whichever provider is cheapest right now" — which makes
    "who processed this person's data" unanswerable."""
    for model in ("auto/best", "auto:cheap", "router-safe", "combo/grown-fast", "", None):
        with pytest.raises(privacy.PiiRoutingRefused):
            privacy.require_no_train(model, pii_touched=True)


def test_a_named_model_is_allowed_after_a_pii_tool():
    assert privacy.require_no_train("gpt-realtime", pii_touched=True) == "gpt-realtime"
    assert privacy.require_no_train("gemini-live-2.5", pii_touched=True)


def test_a_turn_that_saw_nothing_personal_is_not_constrained():
    """Otherwise the control gets switched off for being wrong most of the time."""
    assert privacy.require_no_train("auto/best", pii_touched=False) == "auto/best"


def test_an_empty_model_is_a_fan_out():
    """"Whatever the router defaults to" is exactly the thing being refused, and an
    empty id is the easiest way to end up there."""
    assert privacy.is_fan_out(None) and privacy.is_fan_out("")


def test_the_session_remembers_that_it_touched_pii(monkeypatch):
    """The flag is what a relay would consult; it has to be set by the dispatch itself,
    not by whoever remembers to."""
    session = make_session("mac", monkeypatch)
    monkeypatch.setattr(live_session.kernel_tools, "kernel_recall",
                        lambda args: "nothing found")

    async def scenario(s):
        assert s.pii_touched is False
        await call_shell(s, "ls /opt")
        assert s.pii_touched is False, "a plain read marked the turn as personal"
        await s._do_tool({"call_id": "c2", "name": "kernel_recall",
                          "arguments": json.dumps({"query": "anna"})})
        assert s.pii_touched is True

    run(lambda: (session, scenario))


def test_the_flag_is_sticky_for_the_rest_of_the_conversation(monkeypatch):
    """Once it is in the context window it is in every later turn's prompt too, so
    clearing it on the next tool call would leak on the turn after."""
    session = make_session("mac", monkeypatch)
    monkeypatch.setattr(live_session.kernel_tools, "kernel_recall", lambda args: "x")

    async def scenario(s):
        await s._do_tool({"call_id": "c1", "name": "kernel_recall",
                          "arguments": json.dumps({"query": "anna"})})
        s._ws.sent.clear()
        await call_shell(s, "ls /opt")
        assert s.pii_touched is True

    run(lambda: (session, scenario))
