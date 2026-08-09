"""Surface capability profiles: what each surface may DO, and that it is data.

The bug these exist to prevent is not "the server crashed calling put_text". It is
quieter than that: a tool stays in the schema, the model reads its description, offers
it out loud — "I'll paste that into the window for you" — and only then discovers there
is no window. The user heard a promise; the log shows a handled error.

So the assertion is ABSENCE, and it is asserted against the real tool list core ships,
not a copy: `test_the_server_surface_never_offers_a_mac_tool`.
"""
import pytest

from core import capabilities
from core.tools import TOOLS


ALL_TOOL_NAMES = {t["name"] for t in TOOLS}


def test_every_excluded_tool_is_a_real_tool():
    """An exclusion naming a tool that no longer exists is dead data — and worse, it
    hides the moment someone renames a Mac-only tool and it silently reappears on the
    server under its new name."""
    for name, profile in capabilities.PROFILES.items():
        unknown = sorted(set(profile["excluded_tools"]) - ALL_TOOL_NAMES)
        assert not unknown, f"profile {name!r} excludes tools core does not ship: {unknown}"


def test_the_server_surface_never_offers_a_mac_tool():
    """The clipboard and the herdr lanes are Mac hardware and a Mac tmux server. They
    must be absent from the server's tool list, not present and erroring."""
    served = {t["name"] for t in capabilities.tools_for("server", TOOLS)}
    assert "put_text" not in served
    for lane_tool in ("delegate", "continue_task", "close_finished_tasks", "fleet"):
        assert lane_tool not in served, f"{lane_tool} would be offered on Thrivbe-1"
    # …and everything else survives: this is a subtraction, never a hand-copied list.
    assert served == ALL_TOOL_NAMES - capabilities.MAC_ONLY_TOOLS


def test_the_mac_surface_keeps_every_tool():
    """The Mac is the surface all of these were built for; the profile must not quietly
    take something away from it."""
    assert [t["name"] for t in capabilities.tools_for("mac", TOOLS)] == \
        [t["name"] for t in TOOLS]


def test_os_delegate_survives_on_both_surfaces():
    """`delegate` is gone from the server, so the hand-off path had better not be.
    Losing both would leave the phone surface with no way to escalate at all."""
    for name in ("mac", "server"):
        served = {t["name"] for t in capabilities.tools_for(name, TOOLS)}
        assert "os_delegate" in served


def test_an_unregistered_surface_gets_the_strictest_shell_gate():
    """The fail-closed direction. A surface that forgets to register must not inherit
    the Mac's free shell — a missing `install()` should cost a confirmation prompt, not
    an unstaged `rm` on a server."""
    assert capabilities.shell_gate(None) == capabilities.SHELL_STAGE_DESTRUCTIVE
    assert capabilities.get(None) is capabilities.PROFILES[capabilities.FALLBACK_PROFILE]


def test_an_unknown_profile_name_raises_rather_than_defaulting():
    """Silently falling back on a typo would hand a server the Mac's profile."""
    with pytest.raises(capabilities.UnknownProfile):
        capabilities.get("phone")


def test_every_profile_declares_every_field():
    """A profile missing a key would KeyError at session start, in production, on the
    surface that has no screen to show the traceback on."""
    fields = set(capabilities.PROFILES["mac"])
    for name, profile in capabilities.PROFILES.items():
        assert set(profile) == fields, f"profile {name!r} fields differ: {set(profile) ^ fields}"
        assert profile["shell_gate"] in (capabilities.SHELL_FREE,
                                         capabilities.SHELL_STAGE_DESTRUCTIVE)
        assert profile["shell_binaries"], name


def test_the_surface_note_tells_the_model_what_is_missing():
    """LIVE_SYSTEM is shared by both surfaces (the drift checker requires it), so the
    per-machine truth has to be assembled at runtime — or the server's model reads
    'running on the user's Mac' and offers the clipboard."""
    note = capabilities.surface_note("server", TOOLS)
    assert "Thrivbe-1" in note
    assert "put_text" in note and "delegate" in note
    assert "no screen" in note
    assert "staged" in note              # the shell gate is announced, not discovered

    mac_note = capabilities.surface_note("mac", TOOLS)
    assert "this Mac" in mac_note
    assert "put_text" not in mac_note    # nothing is missing there, so nothing is said
    assert "staged" not in mac_note


def test_the_surface_note_cannot_rot_out_of_sync_with_the_exclusions():
    """The note names the absent tools by reading the profile, never a second list."""
    note = capabilities.surface_note("server", TOOLS)
    for name in capabilities.excluded_tools("server"):
        assert name in note


def test_the_registered_profiles_are_the_ones_the_surfaces_declare():
    """`mac/caps_install.py` and `server/session.py` each name a profile at module
    level; the name has to exist here or the surface dies at startup."""
    from mac import caps_install
    from server import session as server_session

    assert caps_install.SURFACE_PROFILE in capabilities.PROFILES
    assert server_session.SURFACE_PROFILE in capabilities.PROFILES
    assert caps_install.SURFACE_PROFILE != server_session.SURFACE_PROFILE
    # The server's session class pins it structurally, so a session built before
    # install_capabilities() finishes is still gated as the server.
    assert server_session.BrowserLiveSession.PROFILE == server_session.SURFACE_PROFILE
