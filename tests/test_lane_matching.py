"""_find_lane: resolving a spoken/minted task name to the right herdr pane."""
from tools import _find_lane


def _lane(label, name=None, pane_id=None):
    return {"_label": label, "name": name, "pane_id": pane_id or label}


def test_exact_and_substring_still_win():
    lanes = [_lane("voice unification plan"), _lane("shell 1 in voice-agent-B")]
    assert _find_lane("voice unification plan", lanes)["_label"] == "voice unification plan"
    assert _find_lane("unification", lanes)["_label"] == "voice unification plan"


def test_minted_task_name_resolves_to_the_original_pane_label():
    """The live failure: the model asked for the name it minted while the pane still
    carried its creation label — neither is a substring of the other, so the lookup
    returned nothing and the follow-up went nowhere."""
    lanes = [_lane("voice unification plan"), _lane("shell 1 in voice-agent-B"),
             _lane("kettlebell deploy")]
    found = _find_lane("voice-unification-execute", lanes)
    assert found is not None and found["_label"] == "voice unification plan"


def test_unrelated_name_still_returns_nothing():
    """Guard the fuzzy fallback: a wrong match sends Robin's feedback into someone
    else's pane, which is worse than admitting we can't find it."""
    lanes = [_lane("voice unification plan"), _lane("kettlebell deploy")]
    assert _find_lane("quarterly invoice chase", lanes) is None


def test_ambiguous_tie_returns_nothing_rather_than_guessing():
    lanes = [_lane("mobile portal execute"), _lane("mobile portal plan")]
    assert _find_lane("mobile portal", lanes) is None
