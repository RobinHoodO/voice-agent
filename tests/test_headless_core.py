"""`core` must import and run on a headless Linux box with none of the Mac stack installed.

This is the whole point of the core/mac split, so it is enforced rather than documented.
Two checks:

  1. **Import guard** — a meta-path finder makes AppKit, Quartz, PyObjC, sounddevice,
     rumps, pynput, Cocoa and WebKit raise ImportError exactly as they would on Linux,
     then every module under `core/` is imported in a fresh subprocess. Any module that
     reaches for the Mac at import time fails here.
  2. **Source scan** — no module under `core/` may shell out to `osascript`, `pbcopy`
     or the Keychain `security` CLI. Those are capabilities (`core.caps`,
     `core.secrets`), and a grep is the only way to catch a call site that is merely
     never exercised by a test.

The mac implementations of those capabilities live in `mac/` and are tested through
their own suites (test_macos_context, test_reliability's teardown cases, test_security).
"""
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CORE = REPO / "core"

# Everything a headless Linux host would not have. `objc`/Foundation/ApplicationServices
# are the PyObjC bridge; `_sounddevice_data` ships PortAudio.
BLOCKED = ["AppKit", "Cocoa", "WebKit", "Quartz", "objc", "Foundation",
           "ApplicationServices", "sounddevice", "_sounddevice_data", "rumps", "pynput"]


def _core_modules() -> list[str]:
    mods = []
    for p in sorted(CORE.rglob("*.py")):
        rel = p.relative_to(REPO).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        mods.append(".".join(parts))
    return mods


_GUARD = '''
import importlib, sys

BLOCKED = set(%(blocked)r)

class _Blocker:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ImportError("No module named %%r (blocked: not present on Linux)" %% name)
        return None

sys.meta_path.insert(0, _Blocker())
for m in list(sys.modules):
    if m.split(".")[0] in BLOCKED:
        del sys.modules[m]

failed = []
for mod in %(mods)r:
    try:
        importlib.import_module(mod)
    except Exception as e:
        failed.append("%%s: %%r" %% (mod, e))
if failed:
    print("\\n".join(failed))
    raise SystemExit(1)
print("ALL CORE MODULES IMPORTED HEADLESS: %%d" %% len(%(mods)r))
'''


def test_core_imports_with_the_mac_stack_unavailable():
    mods = _core_modules()
    assert len(mods) >= 15, mods
    script = _GUARD % {"blocked": BLOCKED, "mods": mods}
    env = dict(os.environ, PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       env=env, cwd=str(REPO))
    assert r.returncode == 0, f"core is not surface-independent:\n{r.stdout}\n{r.stderr}"
    assert "ALL CORE MODULES IMPORTED HEADLESS" in r.stdout


def test_core_never_shells_out_to_the_mac():
    """osascript / pbcopy / the Keychain `security` CLI belong to `mac`, not `core`."""
    offenders = []
    for p in sorted(CORE.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        for needle in ('"osascript"', "'osascript'", '"pbcopy"', "'pbcopy'",
                       '"security"', "'security'"):
            if needle in src:
                offenders.append(f"{p.relative_to(REPO)} contains {needle}")
    assert not offenders, "\n".join(offenders)


def test_core_never_imports_the_mac_package():
    """`mac` may import `core`; the reverse would reintroduce the coupling. The one
    exception is inside a `__main__`/selftest block, which never runs on a server."""
    offenders = []
    for p in sorted(CORE.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import mac", "from mac ", "from mac.")):
                if line.startswith(" " * 8):   # nested inside the __main__ selftest
                    continue
                offenders.append(f"{p.relative_to(REPO)}:{i}: {stripped}")
    assert not offenders, "\n".join(offenders)
