"""Kernel adapter behavior for OS map and persona council read-only tools."""

import pytest

from core import kernel_tools


def test_os_map_search_renders_named_results(monkeypatch):
    calls = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {"spoken": "Two things match voice agent.", "results": [
            {"name": "voice-agent", "kind": "project",
             "snippet": {"text": "Pam's local voice assistant."}},
        ]}

    monkeypatch.setattr(kernel_tools, "_kernel_call", fake_call)

    out = kernel_tools.os_map_search({"q": "voice agent", "limit": 3})

    assert "voice-agent" in out
    assert calls == [(('GET', '/os-map'), {'params': {'q': 'voice agent', 'limit': 3}})]


def test_os_map_overview_returns_spoken_output(monkeypatch):
    monkeypatch.setattr(kernel_tools, "_kernel_call", lambda *args, **kwargs: {
        "spoken": "Your OS map has 81 things across projects and skills.",
    })

    assert "81 things" in kernel_tools.os_map_overview({})


def test_council_list_advisors_returns_spoken_output(monkeypatch):
    monkeypatch.setattr(kernel_tools, "_kernel_call", lambda *args, **kwargs: {
        "spoken": "You can ask Alex Hormozi about offers and Amanda Ripley about conflict.",
        "personas": [{"name": "Alex Hormozi", "tagline": "offers"}],
    })

    assert "Alex Hormozi" in kernel_tools.council_list_advisors({})


def test_council_ask_advisor_returns_spoken_attribution(monkeypatch):
    calls = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {"persona": {"name": "Alex Hormozi"}, "spoken": (
            "Alex Hormozi's view — a simulation, not the real person — is to raise the offer's value."),
        }

    monkeypatch.setattr(kernel_tools, "_kernel_call", fake_call)

    out = kernel_tools.council_ask_advisor({"persona": "Hormozi", "question": "How should I price this?"})

    assert "simulation, not the real person" in out
    assert calls == [(('POST', '/council-ask',
                       {'persona': 'Hormozi', 'question': 'How should I price this?'}), {'timeout': 120})]


@pytest.mark.parametrize(("tool", "args"), [
    (kernel_tools.os_map_search, {"q": "voice agent"}),
    (kernel_tools.os_map_overview, {}),
    (kernel_tools.council_list_advisors, {}),
    (kernel_tools.council_ask_advisor, {"persona": "Hormozi", "question": "How should I price this?"}),
])
def test_os_map_and_council_tools_return_unreachable(monkeypatch, tool, args):
    def unavailable(*_args, **_kwargs):
        raise kernel_tools.KernelUnavailable()

    monkeypatch.setattr(kernel_tools, "_kernel_call", unavailable)

    assert tool(args) == kernel_tools.UNREACHABLE


def test_os_map_search_requires_a_query_without_calling_kernel(monkeypatch):
    def should_not_be_called(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(kernel_tools, "_kernel_call", should_not_be_called)

    assert kernel_tools.os_map_search({}) == "I need something to search for."


def test_council_ask_advisor_requires_persona_and_question_without_calling_kernel(monkeypatch):
    def should_not_be_called(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(kernel_tools, "_kernel_call", should_not_be_called)

    assert kernel_tools.council_ask_advisor({"persona": "Hormozi"}) == \
        "I need which advisor and the actual question."
