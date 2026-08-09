"""deploy.sh's own refusals, exercised rather than assumed.

Two of them decide whether "one edit lands on both surfaces" is true or merely claimed:

  * the Mac leg proves the build landed, by diffing every core/ and mac/ .py against the
    copy inside Thrivbe Voice.app. Editing the source dir alone changes nothing — the
    .app runs its own copy — so a build that silently skipped a file is a deploy that
    reported success while the running app stayed on the old code.
  * the server leg refuses on a dirty tree, because the Mac deploys from the working
    tree and the server deploys from git. Shipping with uncommitted changes puts
    different code on the two surfaces, which is the exact failure the script exists
    to prevent.

The drift gate and the live-session guard are stubbed here — they have their own tests
(test_tool_drift.py) and their own scripts — so these cases test deploy.sh itself.
"""

import os
import pathlib
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
STUB = "#!/bin/bash\nexit 0\n"


def make_tree(root, *, extra_file=None):
    """A deploy.sh sandbox: the real script, stubbed guards, two tiny packages."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "deploy.sh", root / "deploy.sh")
    for stub in ("drift_gate.sh", "live_session_guard.sh", "reload_app.sh"):
        (root / stub).write_text(STUB)
        (root / stub).chmod(0o755)
    (root / "deploy.sh").chmod(0o755)
    for package in ("core", "mac"):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text(f"# {package}\n")
        (root / package / "thing.py").write_text(f"VALUE = '{package}'\n")
    if extra_file:
        (root / extra_file).write_text("untracked\n")
    return root


def make_bundle(root, source, *, tamper=None, drop=None):
    """The .app's private copy of the source tree, optionally out of date."""
    lib = root / "Contents" / "Resources" / "lib" / "python3.12"
    for package in ("core", "mac"):
        shutil.copytree(source / package, lib / package)
    if tamper:
        (lib / tamper).write_text("VALUE = 'stale build'\n")
    if drop:
        (lib / drop).unlink()
    return root


def deploy(tree, *args, app=None, env=None):
    environment = dict(os.environ, DEPLOY_PROBE="0")
    if app is not None:
        environment["THRIVBE_VOICE_APP"] = str(app)
    environment.update(env or {})
    return subprocess.run([str(tree / "deploy.sh"), *args],
                          capture_output=True, text=True, env=environment, cwd=str(tree))


def git(tree, *args):
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def test_mac_leg_passes_when_the_bundle_matches_the_source(tmp_path):
    tree = make_tree(tmp_path / "repo")
    app = make_bundle(tmp_path / "App", tree)
    result = deploy(tree, "mac", app=app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mac surface verified" in result.stdout


def test_mac_leg_blocks_when_the_build_did_not_land(tmp_path):
    """The source changed, the .app still holds the old bytes — the classic silent
    non-deploy: everything reports success and the running app is unchanged."""
    tree = make_tree(tmp_path / "repo")
    app = make_bundle(tmp_path / "App", tree, tamper="core/thing.py")
    result = deploy(tree, "mac", app=app)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STALE: core/thing.py" in result.stderr
    assert "the running app is not this source tree" in result.stderr


def test_mac_leg_blocks_when_a_file_never_made_it_into_the_bundle(tmp_path):
    """py2app ships what it can reach. A new module nothing imports yet is simply
    absent from the .app, and the surface quietly does not have it."""
    tree = make_tree(tmp_path / "repo")
    app = make_bundle(tmp_path / "App", tree, drop="mac/thing.py")
    result = deploy(tree, "mac", app=app)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT SHIPPED: mac/thing.py" in result.stderr


def test_server_leg_blocks_on_a_dirty_tree(tmp_path):
    tree = make_tree(tmp_path / "repo", extra_file="scratch.txt")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "deploy.sh", "core", "mac")
    git(tree, "commit", "-qm", "base")
    result = deploy(tree, "server", "--dry-run")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "uncommitted changes" in result.stderr
    assert "scratch.txt" in result.stderr


def test_server_leg_dry_run_pins_the_pushed_sha(tmp_path):
    """The house script's default channel is main. Pinning the SHA is what makes 'the
    server runs the commit I just built on the Mac' true rather than approximately true."""
    tree = make_tree(tmp_path / "repo")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "base")
    git(tree, "remote", "add", "origin", "https://example.invalid/voice-agent.git")
    result = deploy(tree, "server", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    sha = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    assert f"/opt/thrivbe-ops/deploy.sh voice-agent {sha}" in result.stdout
    assert "DRY RUN would: git" in result.stdout and "push origin 'main'" in result.stdout


def test_the_server_leg_never_copies_files_by_name():
    """The bridge shipped nine files by name over scp, so anything off that list stayed
    behind on the Mac — one of the ways the two surfaces drifted apart. The server leg
    goes through git; no scp invocation may creep back in."""
    for number, line in enumerate((REPO / "deploy.sh").read_text().splitlines(), 1):
        code = line.split("#", 1)[0].strip()
        assert not code.startswith("scp") and " scp " not in f" {code} ", \
            f"deploy.sh:{number} invokes scp: {line.strip()}"


@pytest.mark.parametrize("leg", ["mac", "server", "all"])
def test_drift_gate_refusal_stops_every_leg(tmp_path, leg):
    """A red gate must stop the deploy before either surface is touched."""
    tree = make_tree(tmp_path / f"repo-{leg}")
    (tree / "drift_gate.sh").write_text("#!/bin/bash\necho 'REFUSED: tool drift' >&2\nexit 1\n")
    (tree / "drift_gate.sh").chmod(0o755)
    app = make_bundle(tmp_path / f"App-{leg}", tree)
    result = deploy(tree, leg, app=app)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "mac surface" not in result.stdout
    assert "server surface" not in result.stdout


def test_live_session_refusal_stops_the_deploy_before_it_pushes(tmp_path):
    """Order matters: the Mac leg runs first so a mid-conversation refusal happens
    before anything reaches GitHub."""
    tree = make_tree(tmp_path / "repo")
    (tree / "live_session_guard.sh").write_text(
        "#!/bin/bash\necho 'REFUSED: a live voice session looks active' >&2\nexit 1\n")
    (tree / "live_session_guard.sh").chmod(0o755)
    app = make_bundle(tmp_path / "App", tree)
    result = deploy(tree, "all", "--dry-run", app=app)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "live voice session" in result.stderr
    assert "server surface" not in result.stdout
