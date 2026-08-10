"""Surface capability profiles: what each surface may DO, and that it is data.

The bug these exist to prevent is not "the phone crashed calling put_text". It is
quieter than that: a tool stays in the schema, the model reads its description, offers
it out loud — "I'll paste that into the window for you" — and only then discovers there
is no window. The user heard a promise; the log shows a handled error.

So the assertion is ABSENCE, and it is asserted against the real tool list core ships,
not a copy: `test_the_phone_surface_never_offers_a_desk_tool`.

Since 2026-08-10 the sharpest case of that is the shell. The phone surface has no
`run_shell` AT ALL — not a gated one, not a staged one. There is no shell gate left in
this module to test, because there is no surface with a shell that Robin is not sitting
at. `tests/test_shell_gate.py` holds the other half: that the desk shell still runs free
and that the confirm gate `gmail_send` uses is untouched.
"""
import json

import pytest

from core import capabilities
from core.tools import TOOLS


ALL_TOOL_NAMES = {t["name"] for t in TOOLS}


def test_every_excluded_tool_is_a_real_tool():
    """An exclusion naming a tool that no longer exists is dead data — and worse, it
    hides the moment someone renames an excluded tool and it silently reappears on the
    surface that must not have it."""
    for name, profile in capabilities.PROFILES.items():
        unknown = sorted(set(profile["excluded_tools"]) - ALL_TOOL_NAMES)
        assert not unknown, f"profile {name!r} excludes tools core does not ship: {unknown}"


def test_the_phone_surface_has_no_shell_at_all():
    """Robin's ruling: the phone gets no `run_shell`. Not gated, not staged — absent
    from the schema the model is handed, so there is nothing to classify, allowlist, or
    get wrong. Three rounds of review broke the allowlist that used to sit here."""
    served = {t["name"] for t in capabilities.tools_for("phone", TOOLS)}
    assert "run_shell" not in served
    assert not capabilities.has_shell("phone")


def test_the_phone_surface_never_offers_a_desk_tool():
    """`put_text` pastes into the frontmost window of a Mac Robin is not looking at.
    It must be absent from the phone's tool list, not present and erroring."""
    served = {t["name"] for t in capabilities.tools_for("phone", TOOLS)}
    assert "put_text" not in served
    # …and everything else survives: this is a subtraction, never a hand-copied list.
    assert served == ALL_TOOL_NAMES - capabilities.PHONE_EXCLUDED_TOOLS


def test_the_phone_keeps_the_herdr_lanes():
    """The line that changed with the architecture. The lanes drive tmux panes on THIS
    Mac, and the brain is now this Mac — so delegating from the sofa works, where under
    the old Thrivbe-1 plan it could not. Losing them would take the phone's whole
    reason for existing with it."""
    served = {t["name"] for t in capabilities.tools_for("phone", TOOLS)}
    for lane_tool in ("delegate", "continue_task", "close_finished_tasks", "fleet"):
        assert lane_tool in served, f"{lane_tool} is a local call — it works from the phone"


def test_no_absent_tool_is_NAMED_anywhere_in_the_phone_schema():
    """The assertion that was missing, and the one that caught a real leak.

    Dropping `run_shell` from the tool list is not the whole boundary: a tool that
    SURVIVES the cut carries a description, and a description is prompt text the model
    reads before it decides. `focus` shipped "Once focused, keep using run_shell to read
    deeper files in that folder" inside the phone's own schema — the tool was absent and
    advertised in the same payload. So the assertion is over the JSON actually sent, not
    over the names in it."""
    gone = capabilities.excluded_tools("phone")
    for tool in capabilities.tools_for("phone", TOOLS):
        blob = json.dumps(tool, ensure_ascii=False)
        for absent in gone:
            assert absent not in blob, (
                f"{tool['name']}'s schema names {absent}, which is not on this surface: "
                f"{blob[:400]}")
        assert (tool.get("description") or "").strip(), f"{tool['name']} lost its description"


def test_a_scrubbed_description_still_says_what_to_do_instead():
    """A wall with no door is where a model starts inventing. Removing the sentence that
    said "keep using run_shell" leaves "and then what?" unanswered, so the profile names
    the shell-less alternative and `tools_for` puts it there."""
    served = {t["name"]: t for t in capabilities.tools_for("phone", TOOLS)}
    description = served["focus"]["description"]
    assert "os_delegate" in description
    assert "semsearch_query" in description
    # the rest of the description is untouched — this is a sentence swap, not a rewrite
    assert "Point yourself at ONE client or project folder" in description


def test_a_tool_that_is_only_about_an_absent_tool_is_dropped_entirely():
    """The end of the same rule. If scrubbing leaves a tool with no description at all,
    the tool WAS the promise — ship it and the model reads a nameless entry and guesses
    at it. Synthetic input on purpose: core ships no such tool today, and this test is
    what keeps the branch honest if one ever arrives."""
    fake = [{"name": "shell_helper", "description": "Runs run_shell for you."},
            {"name": "keeper", "description": "Unrelated. Also unrelated."}]
    assert [t["name"] for t in capabilities.tools_for("phone", fake)] == ["keeper"]


def test_the_desk_schema_is_core_s_own_list_object_for_object():
    """The other direction, and the one Robin was explicit about: the desk is unchanged.
    Not "equal after a rewrite" — the same objects, so a scrub can never touch it."""
    served = capabilities.tools_for("mac", TOOLS)
    assert len(served) == len(TOOLS)
    assert all(a is b for a, b in zip(served, TOOLS))
    assert "run_shell" in json.dumps(served, ensure_ascii=False)


def test_the_mac_surface_keeps_every_tool():
    """The Mac desk is the surface all of these were built for; the profile must not
    quietly take something away from it. Robin's words: it works like it works now."""
    assert [t["name"] for t in capabilities.tools_for("mac", TOOLS)] == \
        [t["name"] for t in TOOLS]
    assert capabilities.has_shell("mac")


def test_the_escalation_paths_survive_on_both_surfaces():
    """The phone has no shell, so the ways OUT of "I can't do that here" had better
    both be there. Losing them would leave it with no way to act on the filesystem at
    all, which is when a model starts inventing."""
    for name in ("mac", "phone"):
        served = {t["name"] for t in capabilities.tools_for(name, TOOLS)}
        assert "os_delegate" in served
        assert "delegate" in served


def test_an_unregistered_surface_gets_the_strictest_profile():
    """The fail-closed direction. A surface that forgets to register must not inherit
    the desk's free shell — a missing `install()` should cost a missing tool, not an
    unwatched shell on a surface nobody classified."""
    assert not capabilities.has_shell(None)
    assert capabilities.excluded_tools(None) == capabilities.PHONE_EXCLUDED_TOOLS
    assert capabilities.get(None) is capabilities.PROFILES[capabilities.FALLBACK_PROFILE]


def test_an_unknown_profile_name_raises_rather_than_defaulting():
    """Silently falling back on a typo would hand a phone the desk's profile."""
    with pytest.raises(capabilities.UnknownProfile):
        capabilities.get("server")          # deleted 2026-08-10; must not resolve


def test_every_profile_declares_every_field():
    """A profile missing a key would KeyError at session start, in production, on the
    surface that has no screen to show the traceback on."""
    fields = set(capabilities.PROFILES["mac"])
    for name, profile in capabilities.PROFILES.items():
        assert set(profile) == fields, f"profile {name!r} fields differ: {set(profile) ^ fields}"
        assert profile["shell_binaries"], name
        assert profile["host"], name


def test_no_shell_gate_machinery_came_back():
    """The staged-shell gate and its command classifier were deleted, not disabled.

    A `shell_gate` field would mean someone re-introduced "this command looks safe, run
    it" — the exact design three rounds of adversarial review broke (env as an exec
    wrapper, `git ls-remote --upload-pack`, uniq's second operand writing a file). If a
    surface ever needs one again it needs a new review, not a revived constant.
    """
    assert not hasattr(capabilities, "shell_gate")
    assert not hasattr(capabilities, "SHELL_STAGE_DESTRUCTIVE")
    for name, profile in capabilities.PROFILES.items():
        assert "shell_gate" not in profile, name
    with pytest.raises(ImportError):
        from core import destructive          # noqa: F401


def test_the_surface_note_tells_the_model_what_is_missing():
    """LIVE_SYSTEM is shared by both surfaces (the drift checker requires it), so the
    per-seat truth has to be assembled at runtime — or the phone's model reads
    'you have a PERSISTENT shell' and offers to go read a file."""
    note = capabilities.surface_note("phone", TOOLS)
    assert "Robin's Mac" in note
    assert "put_text" in note and "run_shell" in note
    assert "NO shell" in note
    assert "os_delegate" in note             # the way out is named, not just the wall
    assert "screen" in note
    assert "staged" not in note              # nothing stages a command anywhere now

    mac_note = capabilities.surface_note("mac", TOOLS)
    assert "this Mac" in mac_note
    assert "put_text" not in mac_note        # nothing is missing there, so nothing is said
    assert "NO shell" not in mac_note


def test_the_surface_note_cannot_rot_out_of_sync_with_the_exclusions():
    """The note names the absent tools by reading the profile, never a second list."""
    note = capabilities.surface_note("phone", TOOLS)
    for name in capabilities.excluded_tools("phone"):
        assert name in note


def test_the_schema_actually_sent_to_the_model_is_the_filtered_one(monkeypatch):
    """End to end, through `_configure`. Every assertion above is about the data; this
    one is about the wire — the schema the model is handed at session setup is where
    "absent" either happens or does not."""
    import asyncio

    from core import kernel_tools, live_session

    captured = {}

    class Backend:
        async def send_setup(self, instructions, tools, voice):
            captured["tools"] = [t["name"] for t in tools]
            captured["instructions"] = instructions

    monkeypatch.setattr(kernel_tools, "kernel_high_stakes", lambda: ["gmail_send"])
    monkeypatch.setattr(live_session, "_grab_context", lambda: "(no context)")
    monkeypatch.setattr(live_session, "_build_live_instructions",
                        lambda ctx, cfg=None, profile=None:
                        capabilities.surface_note(profile, TOOLS))

    class Session(live_session.LiveSession):
        PROFILE = "phone"

    session = Session()
    session._backend = Backend()
    session._cfg = {"live": {"agentic_shell": True}}

    async def scenario():
        session._loop = asyncio.get_running_loop()
        await session._configure()

    asyncio.run(scenario())

    # agentic_shell is ON, so the toggle is not what removed run_shell — the profile is.
    assert "run_shell" not in captured["tools"]
    assert "put_text" not in captured["tools"]
    assert "delegate" in captured["tools"]            # the lanes are local calls
    assert "os_delegate" in captured["tools"]
    # …and the instructions it is set up with say which seat Robin is in.
    assert "Robin's Mac" in captured["instructions"]


def test_the_shared_base_prompt_is_contradicted_where_it_is_wrong():
    """LIVE_SYSTEM opens by describing a persistent shell and offers the clipboard. It
    cannot say otherwise without becoming a second prompt, so the surface block has to
    override it in words — not leave the model choosing between two claims."""
    from core.live_prompt import LIVE_SYSTEM

    assert "run_shell" in LIVE_SYSTEM, "the premise of this test moved; re-read the override"
    assert "clipboard" in LIVE_SYSTEM
    note = capabilities.surface_note("phone", TOOLS)
    assert "does not apply" in note
    assert "paste" in note


def test_the_registered_profiles_are_the_ones_the_surfaces_declare():
    """`mac/caps_install.py` and `server/session.py` each name a profile at module
    level; the name has to exist here or the surface dies at startup."""
    from mac import caps_install
    from server import session as browser_session

    assert caps_install.SURFACE_PROFILE in capabilities.PROFILES
    assert browser_session.SURFACE_PROFILE in capabilities.PROFILES
    assert caps_install.SURFACE_PROFILE != browser_session.SURFACE_PROFILE
    # The browser session class pins it structurally, so a session built before
    # install_capabilities() finishes is still narrowed as the phone.
    assert browser_session.BrowserLiveSession.PROFILE == browser_session.SURFACE_PROFILE
