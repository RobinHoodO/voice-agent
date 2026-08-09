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
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MAC = REPO / "mac"

# Names that need no declaration: the stdlib, and the app's own packages.
_OWN = {"core", "mac", "server"}


def _listed_in(key: str) -> list[str]:
    """The literal strings under OPTIONS[key] in setup.py, in source order."""
    tree = ast.parse((REPO / "setup.py").read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    out += [i.value for i in v.elts if isinstance(i, ast.Constant)]
    return out


def _declared() -> set[str]:
    """Top-level module names setup.py promises the bundle will contain."""
    return {n.split(".")[0] for n in _listed_in("packages") + _listed_in("includes")}


def _imports_of(path: pathlib.Path) -> dict[str, int]:
    """{top-level module: first line} for every absolute import in one .py file."""
    found: dict[str, int] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module] if node.level == 0 and node.module else []
        else:
            continue
        for m in mods:
            found.setdefault(m.split(".")[0], node.lineno)
    return found


def _imported_by_mac() -> dict[str, str]:
    """{top-level module: "file:line"} for every import under mac/, lazy ones included."""
    found: dict[str, str] = {}
    for p in sorted(MAC.rglob("*.py")):
        for root, line in _imports_of(p).items():
            found.setdefault(root, f"{p.relative_to(REPO)}:{line}")
    return found


def _plain_module_source(name: str) -> pathlib.Path | None:
    """Path to `name`'s source if it is a single .py top-level module — i.e. the case
    where py2app copies one file verbatim and its own imports are never followed.
    None for packages (copied whole, dir included) and for C extensions (nothing to parse).
    Resolution only, no import: find_spec does not execute the module."""
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        return None
    if spec is None or spec.submodule_search_locations is not None:
        return None
    origin = spec.origin
    if not origin or not origin.endswith(".py"):
        return None
    return pathlib.Path(origin)


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


def test_plain_modules_listed_in_packages_have_their_own_imports_declared():
    """`packages` is unscanned in BOTH directions.

    The test above covers what mac/ imports. It does not cover what those
    dependencies themselves import — and for a name in `packages` that is really a
    single .py module (sounddevice, not a directory), py2app copies that one file and
    follows nothing, so its own `from _sounddevice import ffi` never ships. The .app
    then builds, signs, launches, and raises ModuleNotFoundError the moment the mic
    layer is touched. That happened; this is the guard.

    A plain module belongs in `includes`, where modulegraph parses it and pulls its
    imports in automatically — which is also why this test is normally vacuous.
    """
    declared = _declared()
    stdlib = sys.stdlib_module_names
    missing: dict[str, str] = {}
    for name in _listed_in("packages"):
        if name in _OWN:
            continue
        src = _plain_module_source(name)
        if src is None:
            continue    # a real package: py2app copies the whole directory
        for root, line in _imports_of(src).items():
            if root == name or root in stdlib or root in declared:
                continue
            missing[f"{name} -> {root}"] = f"{src}:{line}"
    assert not missing, (
        "setup.py lists these in `packages`, which py2app copies without scanning, so "
        "the modules they import are missing from the .app — it will raise "
        "ModuleNotFoundError at runtime. Move the plain module to `includes` so "
        "modulegraph follows its imports:\n"
        + "\n".join(f"  {k}  ({v})" for k, v in sorted(missing.items())))


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
