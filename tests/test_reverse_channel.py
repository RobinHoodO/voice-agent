"""The reverse channel: every way in, and every way it says no.

This is the surface where something that is not on Robin's Mac asks it to act, so the
tests are written the way the module is — refusal-first. Each refusal path has its own
test, named after the thing it refuses, because a fence that silently stops being a
fence is exactly what a "does the happy path work" suite misses.

Most of them drive `reverse_channel.handle()`, which is the whole HTTP policy as a
function: enabled → tailnet peer → token → route → allow-list → scope → gate. The last
section puts a real `ThreadingHTTPServer` on a socket and talks to it, so "it answers
over HTTP" is demonstrated rather than assumed.

Nothing here touches the real Keychain, the real workspace, the real journal
(`isolated_action_journal` is autouse) or the real tailnet.
"""
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from core import audit, capabilities, config, confirm_gate, live_session
from mac import reverse_channel as rc

TOKEN = "reverse-test-token-9a1f"
AUTH = {"x-reverse-token": TOKEN}
PEER = "100.64.9.9"           # a plausible tailnet peer


@pytest.fixture
def workspace(tmp_app, tmp_path, monkeypatch):
    """An enabled channel with a throwaway workspace, and a token in the environment."""
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "hello.md").write_text("hi", encoding="utf-8")
    monkeypatch.setenv("VOICE_AGENT_REVERSE_TOKEN", TOKEN)
    config.set_("reverse_channel.enabled", True)
    config.set_("reverse_channel.workspace", str(root))
    return os.path.realpath(str(root))


class Caller:
    """One authenticated caller against one channel."""

    def __init__(self, channel):
        self.channel = channel

    def post(self, path, body=None, headers=AUTH, peer=PEER, raw=None):
        payload = raw if raw is not None else json.dumps(body or {}).encode()
        return rc.handle(self.channel, "POST", path, dict(headers or {}), payload, peer)

    def get(self, path, headers=AUTH, peer=PEER):
        return rc.handle(self.channel, "GET", path, dict(headers or {}), b"", peer)

    def op(self, name, args=None, **kwargs):
        return self.post("/op", {"op": name, "args": args or {}}, **kwargs)

    def confirm(self, action_id, transcript, **kwargs):
        return self.post("/confirm", {"id": action_id, "transcript": transcript}, **kwargs)


@pytest.fixture
def caller(workspace):
    return Caller(rc.ReverseChannel())


def journal_events():
    return [(entry["event"], entry["tool"]) for entry in audit.read_all()]


# --- 0. off by default ------------------------------------------------------------

def test_the_channel_is_off_in_the_shipped_defaults():
    """Robin opts in. A remote executor that is on out of the box is not a feature."""
    assert config.DEFAULTS["reverse_channel"]["enabled"] is False


def test_serve_refuses_while_disabled(tmp_app, monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_REVERSE_TOKEN", TOKEN)
    config.set_("reverse_channel.enabled", False)
    with pytest.raises(rc.Disabled):
        rc.serve()


def test_serve_refuses_without_a_token(tmp_app, monkeypatch):
    """An unconfigured secret must not mean an open door."""
    monkeypatch.delenv("VOICE_AGENT_REVERSE_TOKEN", raising=False)
    config.set_("reverse_channel.enabled", True)
    with pytest.raises(rc.Disabled):
        rc.serve()


def test_a_disabled_channel_refuses_every_request(caller):
    config.set_("reverse_channel.enabled", False)
    status, payload = caller.op("grab_context")
    assert status == 403
    assert "disabled" in payload["reason"]


# --- 1. the tailnet, and only the tailnet -----------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "::", "192.168.1.20",
                                  "10.0.0.5", "localhost", "100.64.0.0/10"])
def test_bind_refuses_anything_that_is_not_a_tailnet_address(host, monkeypatch):
    """0.0.0.0 is refused by the same rule that refuses 127.0.0.1 — there is no special
    case for it that a later edit could quietly remove."""
    monkeypatch.setattr(rc, "tailscale_addresses", lambda runner=None: ["100.114.219.63"])
    with pytest.raises(rc.Refused):
        rc.resolve_bind_host(host)


def test_bind_takes_the_tailnet_address_when_one_exists(monkeypatch):
    monkeypatch.setattr(rc, "tailscale_addresses", lambda runner=None: ["100.99.1.2"])
    assert rc.resolve_bind_host() == "100.99.1.2"
    assert rc.resolve_bind_host("100.99.1.2") == "100.99.1.2"


def test_bind_refuses_a_tailnet_address_this_machine_does_not_hold(monkeypatch):
    monkeypatch.setattr(rc, "tailscale_addresses", lambda runner=None: ["100.99.1.2"])
    with pytest.raises(rc.Refused):
        rc.resolve_bind_host("100.64.7.7")


def test_bind_refuses_when_tailscale_is_down(monkeypatch):
    """No fallback. A channel that cannot find the tailnet binds nothing."""
    monkeypatch.setattr(rc, "tailscale_addresses", lambda runner=None: [])
    with pytest.raises(rc.Refused):
        rc.resolve_bind_host()


def test_tailscale_addresses_is_empty_when_the_binary_is_missing():
    def explode(*args, **kwargs):
        raise FileNotFoundError("no tailscale here")

    assert rc.tailscale_addresses(runner=explode) == []


@pytest.mark.parametrize("peer", ["127.0.0.1", "192.168.1.5", "8.8.8.8", ""])
def test_a_request_from_off_the_tailnet_is_refused(caller, peer):
    """Belt and braces: the bind should make this unreachable, so reaching it means an
    assumption broke and the request must still not be served."""
    status, payload = caller.op("grab_context", peer=peer)
    assert status == 403
    assert "tailnet" in payload["reason"]
    assert ("refused_offnet", "-") in journal_events()


# --- 2. the token ------------------------------------------------------------------

def test_no_token_header_is_refused(caller):
    status, _payload = caller.op("grab_context", headers={})
    assert status == 401
    assert ("refused_unauthorized", "-") in journal_events()


def test_a_wrong_token_is_refused(caller):
    status, _payload = caller.op("grab_context", headers={"x-reverse-token": "nope"})
    assert status == 401


def test_an_unset_token_refuses_everything(caller, monkeypatch):
    monkeypatch.delenv("VOICE_AGENT_REVERSE_TOKEN", raising=False)
    assert rc.token_ok("") is False
    assert rc.token_ok(None) is False
    assert caller.op("grab_context")[0] == 401


# --- 3. the closed allow-list -------------------------------------------------------

def test_an_unknown_operation_is_refused_and_never_forwarded(caller):
    status, payload = caller.op("put_text", {"text": "x"})
    assert status == 400
    assert "unknown operation" in payload["reason"]
    assert ("refused_unknown_op", "put_text") in journal_events()


def test_the_allow_list_is_exactly_four_operations():
    """Adding one has to be a decision someone makes on purpose, in this file's sight."""
    assert set(rc.OPERATIONS) == {"run_shell", "open_file", "grab_context", "screenshot"}


def test_an_unknown_endpoint_is_refused(caller):
    assert caller.post("/exec", {})[0] == 404


def test_an_undeclared_argument_is_refused(caller):
    status, payload = caller.op("run_shell", {"command": "ls", "cwd": "/etc"})
    assert status == 400
    assert "does not take cwd" in payload["reason"]


def test_a_missing_argument_is_refused(caller):
    assert caller.op("run_shell", {})[0] == 400


def test_a_non_string_argument_is_refused(caller):
    assert caller.op("run_shell", {"command": ["ls"]})[0] == 400


def test_a_body_that_is_not_json_is_refused(caller):
    assert caller.post("/op", raw=b"not json")[0] == 400


def test_an_oversized_body_is_refused(caller):
    assert caller.post("/op", raw=b"x" * (rc.MAX_BODY_BYTES + 1))[0] == 413


# --- 4. workspace scope --------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "cat /etc/passwd",
    "ls /",
    "rm -rf /opt/voice-agent",
    "cd ..; ls",
    "cat ../../.ssh/id_rsa",
    "cat ~/Library/Application Support/ThrivbeVoice/config.json",
    "sudo rm -rf notes",
    "ssh hetzner uptime",
    "grep -r secret --include=/etc/shadow",
    "echo hi > /tmp/out",
    "osascript -e 'tell application \"Finder\" to quit'",
])
def test_out_of_scope_commands_are_refused_before_anything_runs(caller, command,
                                                                monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError(f"nothing may execute for {command!r}")

    monkeypatch.setattr(rc.subprocess, "run", fail)
    status, payload = caller.op("run_shell", {"command": command})
    assert status == 400, payload
    assert "out of scope" in payload["reason"]
    assert caller.channel.gate.current is None, "an out-of-scope command was staged"


def test_a_command_with_unbalanced_quotes_is_refused(workspace):
    """The scan cannot see the real tokens, so it refuses instead of guessing."""
    assert rc.scope_violation('echo "unterminated', workspace) is not None


def test_an_empty_command_is_refused(workspace):
    assert rc.scope_violation("   ", workspace) is not None


def test_in_scope_reads_run(caller):
    status, payload = caller.op("run_shell", {"command": "cat notes/hello.md"})
    assert status == 200, payload
    assert payload["status"] == "ok"
    assert "hi" in payload["result"]
    assert ("call", "run_shell") in journal_events()


def test_a_command_runs_at_the_workspace_root_every_time(caller, workspace):
    """No persistent shell: a `cd` in one call cannot move the next one out of scope."""
    _status, payload = caller.op("run_shell", {"command": "pwd"})
    assert os.path.realpath(payload["result"].strip()) == workspace


def test_open_file_outside_the_workspace_is_refused(caller):
    status, payload = caller.op("open_file", {"path": "/etc/hosts"})
    assert status == 400
    assert "out of scope" in payload["reason"]


def test_open_file_that_does_not_exist_is_refused(caller):
    assert caller.op("open_file", {"path": "notes/ghost.md"})[0] == 400


# --- 5. the gate — the SAME gate ------------------------------------------------------

def test_the_reverse_channel_and_the_voice_session_hold_the_same_gate():
    """The structural assertion. A private copy of the confirmation logic would pass
    every behavioural test below and then drift — a different TTL, a wider affirm list,
    a second slot the other surface cannot see."""
    assert isinstance(rc.ReverseChannel().gate, confirm_gate.PendingSlot)
    assert isinstance(live_session.LiveSession.__new__(
        live_session.LiveSession).__class__(), object)   # cheap: the class is importable
    assert live_session.confirm_gate is confirm_gate
    assert live_session._is_short_affirm is confirm_gate.is_short_affirm
    assert live_session._pending_confirmation_outcome is \
        confirm_gate.pending_confirmation_outcome
    assert live_session.PENDING_ACTION_TTL_SECONDS is \
        confirm_gate.PENDING_ACTION_TTL_SECONDS


def test_the_reverse_channel_does_not_decide_consent_itself(caller, workspace,
                                                            monkeypatch):
    """Behavioural proof that the decision is delegated, not merely imported.

    If `core.confirm_gate` says no, a textbook "yes" must not run — which can only be
    true if this surface asks it rather than pattern-matching the transcript on its own.
    """
    monkeypatch.setattr(confirm_gate, "pending_confirmation_outcome",
                        lambda pending, transcript, now=None: "denied")
    action_id = _stage_delete(caller)
    _status, payload = caller.confirm(action_id, "yes")
    assert payload["status"] == "denied"
    assert os.path.exists(os.path.join(workspace, "notes"))


def test_the_affirmation_vocabulary_is_not_duplicated_here():
    """…and the static half: no second phrase list to fall out of sync."""
    source = open(rc.__file__, encoding="utf-8").read()
    assert "AFFIRM" not in source
    assert "is_short_affirm" not in source


def _stage_delete(caller):
    status, payload = caller.op("run_shell", {"command": "rm -rf notes"})
    assert payload["status"] == "staged", (status, payload)
    return payload["id"]


def test_a_destructive_command_is_staged_and_does_not_run(caller, workspace):
    action_id = _stage_delete(caller)
    assert action_id
    assert os.path.exists(os.path.join(workspace, "notes")), "it ran without a yes"
    assert ("staged", "run_shell") in journal_events()


def test_the_stage_response_carries_the_preview_the_caller_must_read_out(caller):
    _status, payload = caller.op("run_shell", {"command": "rm -rf notes"})
    assert "rm -rf notes" in payload["preview"]
    assert "NOTHING HAS RUN" in payload["instruction"]


def test_a_spoken_yes_executes_the_staged_command(caller, workspace):
    action_id = _stage_delete(caller)
    status, payload = caller.confirm(action_id, "yes")
    assert payload["status"] == "confirmed", (status, payload)
    assert not os.path.exists(os.path.join(workspace, "notes"))
    assert ("confirmed", "run_shell") in journal_events()


@pytest.mark.parametrize("utterance", [
    "did you approve that?",       # a question
    "yes but wait",                # a hedge
    "approved",                    # too weak for the strict bar run_shell carries
    "proceed",
    "please confirm",              # a request, not a confirmation
    "sure why not",
    "yes to the email, not that",
])
def test_anything_that_is_not_a_plain_yes_does_not_run_it(caller, workspace, utterance):
    action_id = _stage_delete(caller)
    _status, payload = caller.confirm(action_id, utterance)
    assert payload.get("status") != "confirmed"
    assert os.path.exists(os.path.join(workspace, "notes")), \
        f"{utterance!r} executed a staged delete"


@pytest.mark.parametrize("transcript", ["", "   ", None, 5])
def test_a_confirmation_with_no_words_in_it_is_refused(caller, workspace, transcript):
    action_id = _stage_delete(caller)
    status, _payload = caller.confirm(action_id, transcript)
    assert status == 400
    assert os.path.exists(os.path.join(workspace, "notes"))


def test_a_no_denies_it(caller, workspace):
    action_id = _stage_delete(caller)
    _status, payload = caller.confirm(action_id, "no")
    assert payload["status"] == "denied"
    assert os.path.exists(os.path.join(workspace, "notes"))
    assert ("denied", "run_shell") in journal_events()


def test_a_confirmation_for_the_wrong_action_is_refused_and_keeps_the_slot(caller,
                                                                          workspace):
    """An unbound yes must not land on whatever happens to be staged now."""
    action_id = _stage_delete(caller)
    status, _payload = caller.confirm("deadbeef", "yes")
    assert status == 409
    assert os.path.exists(os.path.join(workspace, "notes"))
    assert caller.channel.gate.current is not None, "the real action lost its slot"
    assert caller.channel.gate.current_id == action_id
    assert ("refused_stale_confirm", "run_shell") in journal_events()


def test_an_expired_stage_does_not_run(caller, workspace):
    action_id = _stage_delete(caller)
    caller.channel.gate.current["ts"] -= confirm_gate.PENDING_ACTION_TTL_SECONDS + 1
    _status, payload = caller.confirm(action_id, "yes")
    assert payload["status"] == "expired"
    assert os.path.exists(os.path.join(workspace, "notes"))


def test_a_second_stage_while_one_is_pending_is_refused(caller):
    _stage_delete(caller)
    status, payload = caller.op("open_file", {"path": "notes/hello.md"})
    assert status == 409
    assert "already awaiting confirmation" in payload["reason"]
    assert caller.channel.gate.current["tool"] == "run_shell", "the slot was swapped"


def test_confirming_with_nothing_staged_is_refused(caller):
    assert caller.confirm("abc", "yes")[0] == 409


def test_open_file_is_treated_as_mutating(caller, monkeypatch):
    launched = []

    def fake_run(command, *args, **kwargs):
        # Other things shell out during a request (the Keychain lookup for the token);
        # only `open` is what this test is about.
        if command and command[0].endswith("/open"):
            launched.append(command)
        return _ok()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    _status, payload = caller.op("open_file", {"path": "notes/hello.md"})
    assert payload["status"] == "staged"
    assert launched == [], "open_file launched something without a confirmation"
    caller.confirm(payload["id"], "yes")
    assert launched, "a confirmed open_file did not run"


def _ok():
    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""
    return _Result()


def test_the_transcript_is_journalled_verbatim_as_the_consent_record(caller):
    action_id = _stage_delete(caller)
    caller.confirm(action_id, "do it")
    confirmed = [e for e in audit.read_all() if e["event"] == "confirmed"]
    assert confirmed and confirmed[-1]["args"]["transcript"] == "do it"


def test_a_narrowed_workspace_is_re_checked_when_the_action_is_confirmed(caller,
                                                                         tmp_path,
                                                                         workspace):
    """The guards run AGAIN at execution time, so an absolute path that was in scope
    when it was staged is refused if the scope moved out from under it."""
    _status, staged = caller.op(
        "run_shell", {"command": f"rm -rf {os.path.join(workspace, 'notes')}"})
    assert staged["status"] == "staged"
    (tmp_path / "elsewhere").mkdir()
    config.set_("reverse_channel.workspace", str(tmp_path / "elsewhere"))
    status, _payload = caller.confirm(staged["id"], "yes")
    assert status == 400
    assert os.path.exists(os.path.join(workspace, "notes"))
    assert ("refused_scope_on_confirm", "run_shell") in journal_events()


def test_a_relative_command_confirmed_after_a_move_runs_in_the_NEW_root(caller,
                                                                       tmp_path,
                                                                       workspace):
    """The honest limit of the re-check: a relative path is relative to whatever the
    workspace is NOW, so it acts in the new root — it cannot reach back into the old
    one, which is the property that matters."""
    action_id = _stage_delete(caller)          # "rm -rf notes", no path in it
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "notes").mkdir(parents=True)
    config.set_("reverse_channel.workspace", str(elsewhere))
    _status, payload = caller.confirm(action_id, "yes")
    assert payload["status"] == "confirmed"
    assert os.path.exists(os.path.join(workspace, "notes")), "it reached the old root"
    assert not os.path.exists(elsewhere / "notes")


def test_pending_reports_what_is_waiting(caller):
    assert caller.get("/pending")[1]["status"] == "idle"
    action_id = _stage_delete(caller)
    _status, payload = caller.get("/pending")
    assert payload["status"] == "staged"
    assert payload["id"] == action_id
    assert payload["tool"] == "run_shell"


# --- 6. the screenshot denylist (read, not rewritten) ---------------------------------

def _never_capture(*args, **kwargs):
    raise AssertionError("screencapture must not run for a denied window")


def test_a_denylisted_window_refuses_the_screenshot(caller, monkeypatch):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("1Password", ""))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor", lambda: (None, ""))
    monkeypatch.setattr(macos_context.subprocess, "run", _never_capture)
    status, payload = caller.op("screenshot")
    assert status == 403
    assert "denylist" in payload["reason"]


def test_a_denylisted_cursor_window_refuses_the_screenshot(caller, monkeypatch):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("Finder", "Desktop"))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor",
                        lambda: (object(), "Signal chat"))
    monkeypatch.setattr(macos_context.subprocess, "run", _never_capture)
    assert caller.op("screenshot")[0] == 403


def test_an_unidentifiable_app_refuses_the_screenshot(caller, monkeypatch):
    """Fail closed on unknown, exactly as `grab_window_screenshot` does."""
    from mac import macos_context

    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("", ""))
    monkeypatch.setattr(macos_context.subprocess, "run", _never_capture)
    status, payload = caller.op("screenshot")
    assert status == 403
    assert "could not be identified" in payload["reason"]


def test_the_denylist_predicate_is_the_one_grab_window_screenshot_uses():
    """The policy is read from `macos_context`, never copied into this surface."""
    from mac import macos_context

    assert macos_context.screenshot_denied("Nordea", "")
    assert macos_context.screenshot_denied("Finder", "1Password — Login")
    assert macos_context.screenshot_denied("Finder", "Desktop", "WhatsApp")
    assert not macos_context.screenshot_denied("Finder", "Desktop")
    source = open(rc.__file__, encoding="utf-8").read().lower()
    for term in ("nordea", "1password", "bitwarden", "helsenorge", "whatsapp"):
        assert term not in source, \
            f"{term} is hard-coded in the reverse channel — the denylist was copied"


def test_a_permitted_window_captures(caller, monkeypatch, tmp_path):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "_frontmost_app_and_title", lambda: ("Finder", "Desktop"))
    monkeypatch.setattr(macos_context, "_ax_window_under_cursor", lambda: (None, ""))
    monkeypatch.setattr(macos_context, "_focused_window_region", lambda: "0,0,10,10")
    monkeypatch.setattr(macos_context.tempfile, "gettempdir", lambda: str(tmp_path))

    def capture(command, **kwargs):
        if command[0] == "screencapture":
            with open(command[-1], "wb") as handle:
                handle.write(b"test")

    monkeypatch.setattr(macos_context.subprocess, "run", capture)
    assert caller.op("screenshot")[1]["result"] == "dGVzdA=="


def test_grab_context_is_a_read_and_runs_free(caller, monkeypatch):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "grab_context", lambda: "App: Finder")
    status, payload = caller.op("grab_context")
    assert status == 200
    assert payload == {"status": "ok", "result": "App: Finder"}


# --- 7. everything journals ------------------------------------------------------------

def test_every_call_leaves_a_line(caller, monkeypatch):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "grab_context", lambda: "ctx")
    caller.op("grab_context")
    caller.op("nope")
    caller.op("grab_context", headers={})
    events = [event for event, _tool in journal_events()]
    assert "call" in events
    assert "refused_unknown_op" in events
    assert "refused_unauthorized" in events


def test_the_journal_records_the_reverse_surface_by_name(caller, monkeypatch):
    from mac import macos_context

    monkeypatch.setattr(macos_context, "grab_context", lambda: "ctx")
    caller.op("grab_context")
    assert {entry["surface"] for entry in audit.read_all()} == {rc.REVERSE_PROFILE}


# --- 8. the profile ----------------------------------------------------------------------

def test_the_reverse_profile_stages_destructive_commands():
    """The Mac's own profile runs the shell free because Robin is at the keyboard.
    Over the reverse channel he is not, so this profile must not inherit that."""
    assert capabilities.shell_gate("mac") == capabilities.SHELL_FREE
    assert capabilities.shell_gate(rc.REVERSE_PROFILE) == \
        capabilities.SHELL_STAGE_DESTRUCTIVE


def test_run_shell_keeps_the_strict_affirmation_bar_here_too():
    assert capabilities.confirm_strictness("run_shell") == capabilities.AFFIRM_STRICT


# --- 9. one command kills it --------------------------------------------------------------

def test_kill_signals_the_recorded_pid(tmp_app):
    rc.write_pidfile()
    signals = []

    def signaller(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    message = rc.kill(sleeper=lambda _s: None, signaller=signaller)
    assert signals[0] == (os.getpid(), 15)
    assert "stopped" in message
    assert not os.path.exists(rc.pidfile_path())


def test_kill_with_nothing_running_is_not_an_error(tmp_app):
    assert "nothing to kill" in rc.kill(sleeper=lambda _s: None)


def test_kill_escalates_when_sigterm_is_ignored(tmp_app):
    rc.write_pidfile()
    signals = []
    rc.kill(sleeper=lambda _s: None, signaller=lambda pid, sig: signals.append(sig))
    assert 9 in signals, "a process that ignores SIGTERM must still die"
    assert not os.path.exists(rc.pidfile_path())


def test_status_reports_off_when_disabled(tmp_app):
    config.set_("reverse_channel.enabled", False)
    assert "off" in rc.status()


# --- 10. over a real socket ---------------------------------------------------------------

@pytest.fixture
def live_socket(workspace, monkeypatch):
    """A real ThreadingHTTPServer on loopback.

    The peer check is stubbed for this fixture ONLY, and only because loopback is by
    definition not a tailnet address — the check itself has its own tests above
    (`test_a_request_from_off_the_tailnet_is_refused`) and `resolve_bind_host` is what
    decides where the production process actually listens.
    """
    monkeypatch.setattr(rc, "is_tailnet_address",
                        lambda addr: addr in ("127.0.0.1", "100.64.9.9"))
    server = rc.build_server(rc.ReverseChannel(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _http(url, body=None, headers=AUTH, method=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_over_http_a_read_answers_and_a_delete_stages(live_socket, workspace):
    status, payload = _http(f"{live_socket}/op",
                            {"op": "run_shell", "args": {"command": "cat notes/hello.md"}})
    assert (status, payload["result"].strip()) == (200, "hi")

    status, payload = _http(f"{live_socket}/op",
                            {"op": "run_shell", "args": {"command": "rm -rf notes"}})
    assert payload["status"] == "staged"
    assert os.path.exists(os.path.join(workspace, "notes"))

    status, denied = _http(f"{live_socket}/confirm",
                           {"id": payload["id"], "transcript": "what does that delete?"})
    assert denied["status"] == "dropped"
    assert os.path.exists(os.path.join(workspace, "notes"))


def test_over_http_an_unauthenticated_request_is_refused(live_socket):
    status, payload = _http(f"{live_socket}/health", headers={}, method="GET")
    assert status == 401
    assert payload["reason"] == "unauthorized"


def test_over_http_health_reports_the_scope(live_socket, workspace):
    status, payload = _http(f"{live_socket}/health", method="GET")
    assert status == 200
    assert payload["workspace"] == workspace
    assert payload["operations"] == sorted(rc.OPERATIONS)
