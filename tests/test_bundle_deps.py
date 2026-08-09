"""The .app must keep shipping every module `mac/` imports.

`mac` is listed in setup.py's `packages`, not `includes`, and it has to be: modulegraph
hard-excludes a top-level module named "mac" (`_PLATFORM_MODULES`, a Python-2 leftover),
so the import scan silently drops it and the built app dies at launch with
"No module named mac".

The price of `packages` is that py2app copies the directory verbatim **without scanning
its imports**. So a new `from SomeFramework import X` in mac/*.py builds fine, tests
fine from source, and then fails only in the shipped bundle — the exact class of bug
that took a rebuild-and-relaunch to notice. This test closes that gap statically.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MAC = REPO / "mac"

# Names that need no declaration: the stdlib, and the app's own packages.
_OWN = {"core", "mac", "server"}


def _declared() -> set[str]:
    """Top-level module names setup.py promises the bundle will contain."""
    tree = ast.parse((REPO / "setup.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("packages", "includes"):
                    for item in v.elts:
                        if isinstance(item, ast.Constant):
                            names.add(item.value.split(".")[0])
    return names


def _imported_by_mac() -> dict[str, str]:
    """{top-level module: "file:line"} for every import under mac/, lazy ones included."""
    found: dict[str, str] = {}
    for p in sorted(MAC.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for m in mods:
                root = m.split(".")[0]
                found.setdefault(root, f"{p.relative_to(REPO)}:{node.lineno}")
    return found


def test_every_third_party_import_under_mac_is_declared_in_setup_py():
    declared = _declared()
    stdlib = sys.stdlib_module_names
    missing = {
        root: where for root, where in _imported_by_mac().items()
        if root not in stdlib and root not in _OWN and root not in declared
    }
    assert not missing, (
        "mac/ imports these but setup.py does not ship them — the built .app will "
        "raise ImportError at runtime:\n"
        + "\n".join(f"  {m}  (first seen at {w})" for m, w in sorted(missing.items())))


def test_mac_is_shipped_as_a_package_not_an_include():
    """If someone 'tidies' this into `includes`, the app stops launching entirely."""
    src = (REPO / "setup.py").read_text(encoding="utf-8")
    packages = src.split('"packages": [', 1)[1].split("],", 1)[0]
    assert '"mac",' in packages, "mac must stay in `packages` — modulegraph excludes it"


def test_boot_script_is_not_named_agent_py():
    """modulegraph keys modules by basename: a boot script called agent.py is registered
    as an alias for mac/agent.py and neither is bundled."""
    src = (REPO / "setup.py").read_text(encoding="utf-8")
    app = src.split("APP = [", 1)[1].split("]", 1)[0]
    assert "agent.py" not in app, app
    assert (REPO / app.strip().strip('"').strip("'")).is_file()
