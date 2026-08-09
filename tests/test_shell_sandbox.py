"""The seatbelt jail, tested as the kernel sees it.

`mac/shell_sandbox.py` exists because a text scan cannot contain a shell: an audit of
the scan-only reverse channel read `~/.ssh/id_rsa` through `$HOME`, read an outside file
through `${HOME}/..`, and followed a symlink planted inside the workspace. So the tests
that matter here are not "the profile string looks right" — they RUN `sandbox-exec` and
try the three escapes for real, because the only authority on what a sandbox contains is
the sandbox.

The profile-shape tests below it guard the properties a live test cannot see: that no
edit ever slips an `(allow default)` in, and that a root of "/" is refused instead of
producing a jail the size of the disk.
"""
import os
import subprocess

import pytest

from mac import shell_sandbox

CANARY = "TOPSECRET-CANARY-9f3a2b"

needs_sandbox = pytest.mark.skipif(not shell_sandbox.available(),
                                   reason="no /usr/bin/sandbox-exec on this machine")


@pytest.fixture
def jail(tmp_path):
    """A workspace with a file in it, an outside directory with a secret, and a symlink
    from the first to the second. The exact shape of the audit that failed."""
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "hello.md").write_text("hi", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / ".ssh").mkdir(parents=True)
    (outside / ".ssh" / "id_rsa").write_text(CANARY, encoding="utf-8")
    (outside / "secret.txt").write_text(CANARY, encoding="utf-8")
    os.symlink(str(outside), str(root / "notes" / "escape"))
    return os.path.realpath(str(root)), os.path.realpath(str(outside))


def run_jailed(root: str, command: str, home: str, scratch: str = "") -> tuple:
    argv = shell_sandbox.wrap(["/bin/zsh", "-lc", command], root, scratch=scratch)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                            cwd=root, env={**os.environ, "HOME": home,
                                           "TMPDIR": scratch or "/dev/null"})
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# --- the three escapes, run for real ------------------------------------------------

@needs_sandbox
@pytest.mark.parametrize("command", [
    "cat $HOME/.ssh/id_rsa",                  # the one the audit actually read
    "cat ${HOME}/secret.txt",                 # braces
    "cat ${HOME}/../outside/secret.txt",      # climb out of an expanded path
    "cat $(echo $HOME)/secret.txt",           # command substitution
    "cat `echo $HOME`/secret.txt",            # …and the old spelling of it
    "cat notes/escape/secret.txt",            # a symlink planted inside the workspace
    "cat notes/escape/.ssh/id_rsa",
    "grep -r TOPSECRET $HOME",
    "cp $HOME/secret.txt notes/stolen.txt",   # stage it inside, then read it legally
])
def test_no_expansion_or_symlink_reaches_a_file_outside_the_root(jail, command):
    root, outside = jail
    _code, output = run_jailed(root, command, home=outside)
    assert CANARY not in output, f"{command!r} read a file outside the sandbox"
    assert not os.path.exists(os.path.join(root, "notes", "stolen.txt"))


@needs_sandbox
@pytest.mark.parametrize("command", [
    "echo PWNED > $HOME/pwned.txt",
    "echo PWNED > notes/escape/pwned.txt",
    "touch ${HOME}/pwned.txt",
    "rm -f $HOME/secret.txt",
])
def test_nothing_outside_the_root_can_be_written_or_deleted(jail, command):
    root, outside = jail
    run_jailed(root, command, home=outside)
    assert not os.path.exists(os.path.join(outside, "pwned.txt"))
    assert os.path.exists(os.path.join(outside, "secret.txt")), "an outside file was deleted"


@needs_sandbox
def test_the_workspace_itself_still_reads_and_writes(jail, tmp_path):
    """A jail nobody can work in gets switched off, which is the worst outcome of all."""
    root, outside = jail
    code, output = run_jailed(root, "cat notes/hello.md", home=outside)
    assert (code, output.strip()) == (0, "hi")
    scratch = str(tmp_path / "scratch")
    os.makedirs(scratch)
    code, output = run_jailed(root, "echo written > notes/new.txt && cat notes/new.txt",
                              home=outside, scratch=scratch)
    assert "written" in output
    assert (tmp_path / "ws" / "notes" / "new.txt").exists()


@needs_sandbox
def test_the_system_is_readable_enough_to_be_a_shell(jail):
    """`/etc/passwd` is denied; `/bin/ls` is not. The line between "a command exists"
    and "a command can read Robin's files" is the whole design of the profile."""
    root, outside = jail
    _code, listed = run_jailed(root, "ls notes", home=outside)
    assert "hello.md" in listed
    _code, passwd = run_jailed(root, "cat /etc/passwd", home=outside)
    assert "root:" not in passwd
    assert "not permitted" in passwd.lower()


@needs_sandbox
def test_the_network_is_closed_unless_it_is_switched_on(jail):
    """Not a containment claim about files — a claim about where the workspace's
    contents can be sent. Off by default, and the profile is what says so."""
    root, _outside = jail
    assert "network-outbound" not in shell_sandbox.profile(root)
    assert "(allow network-outbound)" in shell_sandbox.profile(root, network=True)


# --- the profile's shape ----------------------------------------------------------

def test_the_profile_denies_by_default_and_never_allows_default(tmp_path):
    body = shell_sandbox.profile(str(tmp_path))
    assert "(deny default)" in body
    assert "(allow default)" not in body


def test_the_profile_grants_the_root_and_only_the_root(tmp_path):
    root = os.path.realpath(str(tmp_path))
    body = shell_sandbox.profile(root)
    assert f'(allow file-read* file-write* (subpath "{root}"))' in body
    # The system paths are readable, never writable.
    write_rules = [ln for ln in body.splitlines() if "file-write" in ln]
    assert all(root in rule or "/dev/" in rule for rule in write_rules), write_rules


@pytest.mark.parametrize("root", ["/", "", None])
def test_a_root_that_is_the_whole_disk_is_refused(root):
    """A jail whose walls are the edge of the world is not a jail."""
    with pytest.raises(shell_sandbox.Unavailable):
        shell_sandbox.profile(root)


def test_a_root_with_a_quote_in_it_cannot_break_out_of_the_profile(tmp_path):
    """The profile is generated text, so the one injection point is the path."""
    root = str(tmp_path / 'we"ird')
    body = shell_sandbox.profile(root)
    assert '\\"' in body
    assert "(allow default)" not in body


def test_the_scratch_dir_is_writable_and_the_support_dir_is_not(tmp_path, tmp_app):
    scratch = shell_sandbox.scratch_dir()
    body = shell_sandbox.profile(os.path.realpath(str(tmp_path)), scratch=scratch)
    assert f'(subpath "{scratch}")' in body
    # …and not its parent, which holds config.json and actions.jsonl.
    assert f'(subpath "{os.path.dirname(scratch)}")' not in body


def test_wrap_refuses_rather_than_handing_back_a_bare_command(monkeypatch, tmp_path):
    """The failure mode that must not exist: a caller that gets an UNSANDBOXED argv
    back because the sandbox was missing and nobody checked the return value."""
    monkeypatch.setattr(shell_sandbox, "available", lambda: False)
    with pytest.raises(shell_sandbox.Unavailable):
        shell_sandbox.wrap(["/bin/zsh", "-lc", "ls"], str(tmp_path))


def test_wrap_puts_the_profile_in_front_of_the_command(tmp_path):
    argv = shell_sandbox.wrap(["/bin/zsh", "-lc", "ls"], os.path.realpath(str(tmp_path)))
    assert argv[0] == shell_sandbox.SANDBOX_EXEC
    assert argv[1] == "-p"
    assert argv[3:] == ["/bin/zsh", "-lc", "ls"]
