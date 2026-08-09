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


def hermetic_env(**overrides):
    """os.environ minus every ambient GIT_* variable.

    The pre-commit hook runs this suite from inside a `git commit`, which exports
    GIT_DIR and GIT_INDEX_FILE. Inherited, they point the sandbox repos below — and
    deploy.sh's own `git status` — at *this* repository's index, so the dirty-tree
    case reads the wrong tree and the sandbox `git commit` fails outright. A test
    that shells out to git has to carry no ambient git context at all.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(overrides)
    return env


def reloaded_marker(tree):
    """Written by the stubbed reload_app.sh — i.e. "the menubar app was rebuilt"."""
    return tree.parent / f"{tree.name}.reloaded"


def make_tree(root, *, extra_file=None):
    """A deploy.sh sandbox: the real script, stubbed guards, two tiny packages.

    reload_app.sh is the one stub that leaves a trace. It is the script that quits,
    rebuilds and relaunches Robin's live menubar app, so "did it run?" is the whole
    question when we are asserting that a refusal happened *before* the Mac was touched.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "deploy.sh", root / "deploy.sh")
    for stub in ("drift_gate.sh", "live_session_guard.sh"):
        (root / stub).write_text(STUB)
        (root / stub).chmod(0o755)
    # The marker lives outside the tree so that "the app was rebuilt" can never be
    # confused with "the working tree is dirty".
    (root / "reload_app.sh").write_text(
        f'#!/bin/bash\ntouch "{reloaded_marker(root)}"\nexit 0\n')
    (root / "reload_app.sh").chmod(0o755)
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
    environment = hermetic_env(DEPLOY_PROBE="0")
    if app is not None:
        environment["THRIVBE_VOICE_APP"] = str(app)
    environment.update(env or {})
    return subprocess.run([str(tree / "deploy.sh"), *args],
                          capture_output=True, text=True, env=environment, cwd=str(tree))


def git(tree, *args):
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True,
                   env=hermetic_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
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


@pytest.mark.parametrize("leg", ["server", "all"])
def test_a_dirty_tree_blocks_before_either_surface_is_touched(tmp_path, leg):
    """A dirty tree is Robin's normal mid-edit state, and `./deploy.sh` with no argument
    means `all`. If this refusal ran inside the server leg, the default invocation would
    quit and rebuild the live menubar app on code the server was about to refuse — the
    exact divergence the script exists to prevent, produced by the script itself.

    Deliberately NOT a dry run: a dry run never invokes reload_app.sh, so the marker
    below would prove nothing about the ordering.
    """
    tree = make_tree(tmp_path / f"repo-{leg}", extra_file="scratch.txt")
    app = make_bundle(tmp_path / f"App-{leg}", tree)
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "deploy.sh", "core", "mac")
    git(tree, "commit", "-qm", "base")
    result = deploy(tree, leg, app=app)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "uncommitted changes" in result.stderr
    assert "scratch.txt" in result.stderr
    assert not reloaded_marker(tree).exists(), "the app was rebuilt despite the refusal"
    assert "mac surface verified" not in result.stdout
    assert "server surface" not in result.stdout


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
                         capture_output=True, text=True,
                         env=hermetic_env()).stdout.strip()
    assert f"/opt/thrivbe-ops/deploy.sh voice-agent {sha}" in result.stdout
    assert "DRY RUN would: git" in result.stdout and "push origin main" in result.stdout


@pytest.mark.parametrize("form", [["--ref", "v1.2.3"], ["--ref=v1.2.3"]])
def test_both_ref_spellings_are_accepted(tmp_path, form):
    """The header documents the space form; the parser used to accept only `--ref=`."""
    tree = make_tree(tmp_path / f"repo-{len(form)}")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "base")
    git(tree, "remote", "add", "origin", "https://example.invalid/voice-agent.git")
    result = deploy(tree, "server", "--dry-run", *form)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/opt/thrivbe-ops/deploy.sh voice-agent v1.2.3" in result.stdout


def test_a_ref_carrying_shell_syntax_is_refused_not_quoted(tmp_path):
    """--ref is interpolated into a command that reaches a remote shell, and it is the
    thing that makes "the server runs the commit I just built" true. A value that runs
    local commands, or that survives as a truncated ref while the script exits 0, breaks
    both. Refuse it outright rather than trusting quoting."""
    tree = make_tree(tmp_path / "repo")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "base")
    git(tree, "remote", "add", "origin", "https://example.invalid/voice-agent.git")
    pwned = tmp_path / "PWNED"
    result = deploy(tree, "server", f"--ref=v1'; touch {pwned}; echo '")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is not a git ref" in result.stderr
    assert not pwned.exists(), "a --ref value executed a local command"
    assert "server surface" not in result.stdout


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
    assert not reloaded_marker(tree).exists(), "the app was rebuilt despite the refusal"


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
    assert not reloaded_marker(tree).exists(), "the app was rebuilt despite the refusal"
