"""fleet(detail='topics') gist extraction — the heuristic that turns a coding TUI's
raw pane buffer into 'what this pane is about'."""
from tools import _gist_from_text


def test_prefers_the_typed_prompt_over_current_output():
    text = "\n".join([
        "> unify the voice agent and voice bridge into one service",
        "I'll start by reading the existing implementation files now.",
    ])
    assert _gist_from_text(text) == "unify the voice agent and voice bridge into one service"


def test_strips_tui_chrome_progress_bars_and_token_counters():
    """The pane tail is live chrome; without this the gist was '37% tokens' noise."""
    text = "\n".join([
        "Rewrote the unsubscribe scanner to take the first header it finds.",
        "     4m 33s · ↓ 128.3k tokens",
        "Opus 5 │ voice-agent ███░░░░ 37%",
        "⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])
    assert _gist_from_text(text) == (
        "Rewrote the unsubscribe scanner to take the first header it finds.")


def test_drops_the_identical_delegate_harness_preamble():
    """Every voice-created lane opens with the same harness pointer — it says nothing
    about what THAT lane is doing."""
    text = "\n".join([
        "FIRST: read /path/delegate-harness.md and follow it exactly - "
        "it is your working contract for this task.",
        "Investigating how the voice bridge authenticates mobile clients.",
    ])
    assert _gist_from_text(text) == (
        "Investigating how the voice bridge authenticates mobile clients.")


def test_empty_or_chrome_only_buffer_yields_no_gist():
    assert _gist_from_text("") == ""
    assert _gist_from_text("██░░ 42%\n✴ Brewed for 17m") == ""


def test_gist_is_length_capped():
    assert len(_gist_from_text("word alpha beta gamma delta " * 60, max_chars=80)) <= 80
