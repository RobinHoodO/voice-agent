"""The Mac side of the phone surface: where it listens, how it gets HTTPS, and the switch.

Two things here are worth more than the rest.

**The `tailscale serve` invocation is asserted argument by argument.** Not because argv
is interesting, but because it was verified once against the installed binary
(Tailscale 1.98.10) and there is no way to notice it drifting: a wrong flag fails at
runtime, on Robin's phone, as a microphone that never opens. The transcript of that
verification is in `mac/tailnet.py`'s docstring.

**Port 443 is refused.** This Mac already serves 443 on the tailnet to trustmux
(`robins-macbook-pro.tail9908c7.ts.net:443` → `127.0.0.1:7432`, observed 2026-08-10).
`serve --https=443` REPLACES that handler rather than erroring, so the failure mode of
getting this wrong is breaking an unrelated service silently.
"""
import socket

import pytest

from mac import tailnet


class FakeRun:
    """Stands in for `subprocess.run`, recording argv and replaying canned results."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def __init__(self, results=None):
        self.calls: list[list] = []
        self._results = results or {}

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        for needle, result in self._results.items():
            if needle in " ".join(argv):
                return result
        return self.Result(0, "")

    def argv_containing(self, needle: str) -> list:
        for call in self.calls:
            if needle in " ".join(call):
                return call
        raise AssertionError(f"no call containing {needle!r} in {self.calls}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


# ── where it listens ─────────────────────────────────────────────────────────
def test_loopback_is_the_default_bind():
    assert tailnet.resolve_bind_host(None) == "127.0.0.1"
    assert tailnet.resolve_bind_host("") == "127.0.0.1"
    assert tailnet.resolve_bind_host("127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize("candidate", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5",
                                       "8.8.8.8", "not-an-ip"])
def test_anything_that_is_not_loopback_or_the_tailnet_is_refused(candidate):
    """0.0.0.0 is refused by the RULE — loopback or a tailnet address — rather than by
    a special case someone could delete while tidying."""
    with pytest.raises(tailnet.TailnetError):
        tailnet.resolve_bind_host(candidate, runner=FakeRun())


def test_a_tailnet_address_this_mac_holds_is_allowed():
    runner = FakeRun({"ip -4": FakeRun.Result(0, "100.96.126.87\n")})
    assert tailnet.resolve_bind_host("100.96.126.87", runner=runner) == "100.96.126.87"


def test_a_tailnet_address_this_mac_does_not_hold_is_refused():
    runner = FakeRun({"ip -4": FakeRun.Result(0, "100.96.126.87\n")})
    with pytest.raises(tailnet.TailnetError):
        tailnet.resolve_bind_host("100.64.9.9", runner=runner)


# ── how it gets HTTPS ────────────────────────────────────────────────────────
def test_serve_start_uses_the_invocation_verified_against_the_installed_binary():
    runner = FakeRun({
        "ip -4": FakeRun.Result(0, "100.96.126.87\n"),
        "status --json": FakeRun.Result(
            0, '{"Self": {"DNSName": "robins-macbook-pro.tail9908c7.ts.net."}}'),
    })
    url = tailnet.serve_start(8443, 8767, runner=runner)

    argv = runner.argv_containing("serve")
    assert argv[1:] == ["serve", "--bg", "--yes", "--https=8443",
                        "http://127.0.0.1:8767"]
    assert argv[0] in (*tailnet.TAILSCALE_BINARIES, "tailscale")
    # The trailing dot on DNSName is stripped — a URL with one does not resolve in Safari.
    assert url == "https://robins-macbook-pro.tail9908c7.ts.net:8443/"


def test_serve_start_refuses_port_443():
    """443 is this Mac's existing tailnet front door. `serve --https=443` would replace
    it without complaining, which is why this refusal is in code and not in a comment."""
    with pytest.raises(tailnet.TailnetError, match="front door"):
        tailnet.serve_start(443, 8767, runner=FakeRun())
    assert tailnet.RESERVED_TLS_PORTS == frozenset({443})


def test_serve_start_refuses_when_tailscale_is_down():
    """No tailnet address means no certificate and no reachable name. Failing here is
    much better than a URL that times out on the phone."""
    runner = FakeRun({"ip -4": FakeRun.Result(1, "")})
    with pytest.raises(tailnet.TailnetError, match="not up"):
        tailnet.serve_start(8443, 8767, runner=runner)


def test_serve_stop_uses_the_off_target_and_tolerates_already_off():
    """`tailscale serve --https=PORT off` exits 1 with "handler does not exist" when
    there is nothing to remove. That is the desired end state, not an error."""
    runner = FakeRun({"off": FakeRun.Result(0, "")})
    assert tailnet.serve_stop(8443, runner=runner) is True
    assert runner.argv_containing("off")[1:] == ["serve", "--https=8443", "off"]

    already = FakeRun({"off": FakeRun.Result(
        1, "", "error: failed to remove web serve: handler does not exist\n")})
    assert tailnet.serve_stop(8443, runner=already) is True

    broken = FakeRun({"off": FakeRun.Result(1, "", "error: something else entirely")})
    assert tailnet.serve_stop(8443, runner=broken) is False


def test_serve_ports_reads_the_live_config():
    runner = FakeRun({"serve status": FakeRun.Result(
        0, '{"TCP": {"443": {"HTTPS": true}, "8443": {"HTTPS": true}}}')})
    assert tailnet.serve_ports(runner=runner) == {443, 8443}


# ── the switch ───────────────────────────────────────────────────────────────
@pytest.fixture
def surface(tmp_app, monkeypatch):
    """A PhoneSurface with a real uvicorn but a scripted Tailscale.

    Tailscale is scripted rather than run: publishing for real would touch the tailnet
    config of the machine the suite is running on, which a test has no business doing.
    The invocation itself is covered above, against the flags verified on the binary.
    """
    from core import caps, config
    from mac import phone_surface

    monkeypatch.setenv("VOICE_AGENT_TOKEN", "test-token-phone-surface")
    # Inside the menubar app the profile is already registered, so the app's lifespan
    # does not go looking for PyObjC. Mirror that.
    monkeypatch.setattr(caps, "_profile", "mac", raising=False)

    port = _free_port()
    config.set_("phone_surface.port", port)
    config.set_("phone_surface.tls_port", 8443)
    config.set_("phone_surface.bind", "127.0.0.1")

    events: list[tuple[str, bool]] = []

    def fake_serve_start(tls_port, target_port, runner=None):
        # Record whether the HTTP listener was already answering when we published.
        events.append(("publish", _answers(target_port)))
        return f"https://fake.ts.net:{tls_port}/"

    def fake_serve_stop(tls_port, runner=None):
        events.append(("unpublish", _answers(port)))
        return True

    monkeypatch.setattr(phone_surface.tailnet, "serve_start", fake_serve_start)
    monkeypatch.setattr(phone_surface.tailnet, "serve_stop", fake_serve_stop)

    s = phone_surface.PhoneSurface()
    try:
        yield s, port, events
    finally:
        s.stop()


def test_it_serves_on_loopback_and_never_on_a_wider_interface(surface):
    s, port, _events = surface
    s.start()
    assert s.is_on
    assert _answers(port, "127.0.0.1"), "nothing is listening on loopback"
    assert s.status()["bind"] == "127.0.0.1"
    # The listener must not be reachable on this machine's routable addresses. A
    # 0.0.0.0 bind would answer here; a loopback bind refuses.
    lan = socket.gethostbyname(socket.gethostname())
    if lan and not lan.startswith("127."):
        assert not _answers(port, lan), f"the surface answered on {lan} — it bound wide"


def test_it_publishes_only_once_the_port_answers_and_unpublishes_before_closing(surface):
    """Order, in both directions. A serve handler in front of a port that is not up
    gives the phone a bare 502; leaving one in front of a closed port leaves the tailnet
    advertising something dead, which Safari caches hard."""
    s, _port, events = surface
    s.start()
    s.stop()
    assert events == [("publish", True), ("unpublish", True)], events


def test_the_menubar_can_see_it_is_on_and_turn_it_off(surface):
    """Requirement: a way to see the phone surface is on, and a way to turn it off."""
    from core import config

    s, _port, _events = surface
    assert s.status()["on"] is False

    s.toggle()
    status = s.status()
    assert status["on"] is True
    assert status["url"].startswith("https://")
    assert status["tls_port"] == 8443
    assert config.get("phone_surface.enabled") is True

    s.toggle()
    assert s.status()["on"] is False
    assert s.status()["url"] is None
    assert config.get("phone_surface.enabled") is False


def test_a_failure_to_start_leaves_nothing_listening_and_a_readable_reason(
        tmp_app, monkeypatch):
    """The publish step failing must not leave an orphan HTTP listener behind — that is
    a port Robin cannot reclaim without quitting the app."""
    from core import caps, config
    from mac import phone_surface

    monkeypatch.setenv("VOICE_AGENT_TOKEN", "test-token-phone-surface")
    monkeypatch.setattr(caps, "_profile", "mac", raising=False)
    port = _free_port()
    config.set_("phone_surface.port", port)

    def boom(tls_port, target_port, runner=None):
        raise tailnet.TailnetError("Tailscale is not up on this Mac.")

    monkeypatch.setattr(phone_surface.tailnet, "serve_start", boom)
    monkeypatch.setattr(phone_surface.tailnet, "serve_stop", lambda *a, **k: True)

    s = phone_surface.PhoneSurface()
    with pytest.raises(tailnet.TailnetError):
        s.start()
    assert s.is_on is False
    assert not _answers(port), "the HTTP listener survived a failed start"

    # …and through the menu, the same failure is a message rather than a crash.
    status = s.toggle()
    assert status["on"] is False
    assert "Tailscale is not up" in (status["error"] or "")


def test_the_token_is_minted_once_and_reused(tmp_app, monkeypatch):
    """Robin has to be able to get the token; a surface that listens with no way to
    authenticate to it is a surface he cannot use."""
    from core import secrets
    from mac import phone_surface

    monkeypatch.delenv("VOICE_AGENT_TOKEN", raising=False)
    stored: dict = {}

    class MemoryStore(secrets.SecretStore):
        def get(self, name):
            return stored.get(name)

        def set(self, name, value):
            stored[name] = value
            return True

    monkeypatch.setattr(secrets, "_store", MemoryStore(), raising=False)

    first = phone_surface.ensure_token()
    assert len(first) >= 24
    assert phone_surface.ensure_token() == first, "a second call minted a new token"
    assert stored[phone_surface.TOKEN_NAME] == first
    # It has to be the name the server actually checks.
    assert phone_surface.TOKEN_NAME == "voice_agent_token"
