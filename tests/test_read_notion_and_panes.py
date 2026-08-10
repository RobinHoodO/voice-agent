"""Reading a Notion page, and reading a herdr pane.

Both answer "what does that actually SAY?" — the question `notion_search` and `fleet`
could not, because both only ever returned titles and one-line gists.
"""
import pytest

from core import services, tools


# --- Notion ------------------------------------------------------------------

def _search_result(title, page_id="p1"):
    return {"id": page_id,
            "properties": {"Name": {"type": "title",
                                    "title": [{"plain_text": title}]}}}


def _para(text):
    return {"type": "paragraph", "has_children": False,
            "paragraph": {"rich_text": [{"plain_text": text}]}}


def _fake_notion(monkeypatch, results, blocks):
    calls = []

    def fake(method, path, payload=None):
        calls.append((method, path))
        if path == "/search":
            return {"results": results}
        return {"results": blocks, "has_more": False, "next_cursor": None}

    monkeypatch.setattr(services, "_notion", fake)
    return calls


def test_it_reads_the_page_body_not_just_the_title(monkeypatch):
    _fake_notion(monkeypatch,
                 [_search_result("Borderland Packing List")],
                 [_para("Bring the tent"), {"type": "to_do", "has_children": False,
                                            "to_do": {"checked": True,
                                                      "rich_text": [{"plain_text": "Water"}]}}])
    out = services.notion_read_page({"query": "Borderland Packing List"})
    assert "Bring the tent" in out
    assert "[x] Water" in out
    assert "Borderland Packing List" in out, "it must say WHICH page it read"


def test_a_single_coincidental_word_is_not_identification(monkeypatch):
    """The regression. Notion's search is weak: asking for the packing page returned
    'Send prep-meeting invite…' and 'go build something that isn't', and one shared word
    was enough to read either out as though it were the page Robin named."""
    _fake_notion(monkeypatch,
                 [_search_result("Send prep-meeting invite: Meet Us At The Edge"),
                  _search_result("Prepare quick proposal for Toniic", "p2")],
                 [_para("should never be reached")])
    out = services.notion_read_page({"query": "Borderland 2025 Packing Prep"})
    assert "not sure which page" in out.lower()
    assert "should never be reached" not in out
    assert "Send prep-meeting invite" in out, "it should offer the candidates it did find"


def test_a_strong_title_match_is_read_without_asking(monkeypatch):
    _fake_notion(monkeypatch,
                 [_search_result("Vagabond Camp Storage Plan")],
                 [_para("Unit 4B holds the kitchen")])
    out = services.notion_read_page({"query": "Vagabond Camp Storage"})
    assert "Unit 4B holds the kitchen" in out
    assert "not sure which page" not in out.lower()


def test_a_page_with_no_prose_says_so_rather_than_going_quiet(monkeypatch):
    _fake_notion(monkeypatch, [_search_result("Some Database")], [])
    out = services.notion_read_page({"query": "Some Database"})
    assert "no readable text" in out


def test_it_asks_when_given_nothing():
    assert "which" in services.notion_read_page({"query": "  "}).lower()


# --- herdr panes -------------------------------------------------------------

PANES = [
    {"pane_id": "w1:pA", "agent": "claude", "focused": False,
     "foreground_cwd": "/Users/robinsverd/Thrivbe-AI/lab/jomoguide", "agent_status": "working"},
    {"pane_id": "w1:pB", "agent": "claude", "focused": True,
     "foreground_cwd": "/Users/robinsverd/Thrivbe-AI/projects/bloom", "agent_status": "idle"},
]


@pytest.fixture
def herdr(monkeypatch):
    seen = []

    def fake_herdr(*args, timeout=10):
        seen.append(args)
        if args[:2] == ("agent", "read"):
            return {"read": {"text": "⏺ Bash(ls)\n  ⎿  the real output line\n"}}
        return None

    monkeypatch.setattr(tools, "_herdr_up", lambda: True)
    monkeypatch.setattr(tools, "_speakable_labels", lambda *a, **k: PANES)
    monkeypatch.setattr(tools, "_herdr", fake_herdr)
    return seen


def test_no_selector_reads_the_pane_on_screen(herdr):
    out = tools.read_pane({})
    assert "w1:pB" in out and "the real output line" in out


def test_it_reads_a_pane_by_the_folder_robin_would_say(herdr):
    """Panes carry no label — 'unnamed pane, unnamed pane, unnamed pane' is what he got
    before. The folder is the handle a person actually has."""
    out = tools.read_pane({"pane": "jomoguide"})
    assert "w1:pA" in out


def test_it_uses_the_json_read_not_the_raw_text_one(herdr):
    """`herdr pane read` prints RAW TEXT, so `_herdr`'s json.loads returns None and the
    pane reads as empty. `agent read` is the JSON form."""
    tools.read_pane({})
    assert any(a[:2] == ("agent", "read") for a in herdr)
    assert not any(a[:2] == ("pane", "read") for a in herdr)


def test_a_missed_name_lists_what_is_actually_open(herdr):
    out = tools.read_pane({"pane": "no-such-pane"})
    assert "don't see a pane" in out
    assert "jomoguide" in out and "bloom" in out


def test_reading_is_not_ownership_gated(herdr):
    """Mutating calls go through `_own_pane` so Pam can never steer Robin's or Hermes'
    lanes. Reading deliberately does not — he asks about panes he owns, and refusing
    would defeat the whole tool."""
    out = tools.read_pane({"pane": "bloom"})
    assert "the real output line" in out
