"""New herdr lanes are named by PURPOSE, so Robin can say the name back later."""
from tools import _purpose_slug


def test_model_supplied_task_name_wins():
    assert _purpose_slug("voice-bridge-unify", "some rambling instruction") == "voice-bridge-unify"


def test_spoken_filler_is_not_the_name():
    """The live failure: naming from the first words of a spoken request produced
    'can-you-spin', which Robin could never say back to find the pane."""
    spoken = ("Can you spin up a new agent that are going to look at the voice agent "
              "and voice bridge and figure out how to unify the system")
    name = _purpose_slug("", spoken)
    assert name != "can-you-spin"
    assert "voice" in name


def test_stopwords_dropped_from_derived_name():
    assert _purpose_slug("", "unify the mobile portal with the voice bridge") == \
        "unify-mobile-portal"


def test_empty_input_still_yields_a_usable_name():
    assert _purpose_slug("", "") == "task"
    assert _purpose_slug("   ", "the a an and") == "task"


def test_name_is_kebab_and_bounded():
    name = _purpose_slug("", "refactor the authentication middleware layer completely now")
    assert name.count("-") == 2 and name.islower() and " " not in name
