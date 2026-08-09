"""The drift gate has to be provable, not trusted.

check_tool_drift.py is the only thing standing between "I edited core/tools.py" and
"the Mac and the server now disagree about which tools exist and which ones need a
spoken confirmation". These tests build tiny surface trees on disk and assert the exit
codes deploy.sh keys off: 0 converged · 1 drift · 2 blind checker · 3 kernel down.

They never touch the kernel: VOICE_TOOLS_MANIFEST_URL points at a file:// fixture.
"""

import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
CHECKER = REPO / "check_tool_drift.py"

CORE_TOOLS = '''\
LOCAL_HIGH_STAKES = frozenset({"gmail_send"})
NOTION_STATUSES = ["Inbox", "Done"]
TOOLS = [
    {"type": "function", "name": "kernel_status", "description": "kernel health",
     "parameters": {"type": "object", "properties": {"detail": {"type": "string"}},
                    "required": ["detail"]}},
    {"type": "function", "name": "kernel_decide", "description": "decide",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                    "required": ["id"]}},
    {"type": "function", "name": "gmail_send", "description": "send mail",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"},
                                                     "status": {"enum": NOTION_STATUSES}}}},
]
'''

CORE_PROMPT = 'LIVE_SYSTEM = """You are a hands-free voice agent.\nKeep it short."""\n'

MANIFEST = {
    "version": 1,
    "generatedAt": "2026-08-09T00:00:00.000Z",
    "tools": [
        {"name": "kernel_status", "kernel": "os", "highStakes": False,
         "parameters": {"type": "object", "properties": {"detail": {"type": "string"}},
                        "required": ["detail"]}},
        {"name": "kernel_decide", "kernel": "os", "highStakes": True,
         "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"]}},
    ],
}


def build_tree(root, mac="", server="", core_tools=CORE_TOOLS):
    """A minimal core/ + two surfaces. A surface file of "" declares nothing."""
    (root / "core").mkdir(parents=True)
    (root / "core" / "__init__.py").write_text("")
    (root / "core" / "tools.py").write_text(core_tools)
    (root / "core" / "live_prompt.py").write_text(CORE_PROMPT)
    for name, body in (("mac", mac), ("server", server)):
        (root / name).mkdir()
        (root / name / "__init__.py").write_text("")
        if body:
            (root / name / "surface.py").write_text(body)
    return root


def run(root, manifest_url, *extra):
    env = dict(os.environ, VOICE_API_TOKEN="test-token",
               VOICE_TOOLS_MANIFEST_URL=manifest_url)
    return subprocess.run([sys.executable, str(CHECKER), "--root", str(root), *extra],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def manifest_url(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST))
    return path.as_uri()


def test_converged_surfaces_pass(tmp_path, manifest_url):
    """Neither surface declares anything, so both are core — the intended end state."""
    root = build_tree(tmp_path / "tree")
    result = run(root, manifest_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Surfaces converged: mac, server" in result.stdout
    assert "inherits core" in result.stdout


def test_divergent_tool_set_blocks(tmp_path, manifest_url):
    """The server drops a tool the Mac has — the exact failure the merge exists for."""
    root = build_tree(tmp_path / "tree", server='''\
TOOLS = [
    {"type": "function", "name": "kernel_status", "description": "kernel health",
     "parameters": {"type": "object", "properties": {"detail": {"type": "string"}},
                    "required": ["detail"]}},
    {"type": "function", "name": "kernel_decide", "description": "decide",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                    "required": ["id"]}},
]
''')
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT mac vs server: only on mac: gmail_send" in result.stdout


def test_divergent_tool_schema_blocks(tmp_path, manifest_url):
    """Same tool names, one changed argument — silent until someone calls it."""
    root = build_tree(tmp_path / "tree", server=CORE_TOOLS.replace(
        '"required": ["detail"]', '"required": []'))
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "kernel_status required" in result.stdout


def test_divergent_tool_description_blocks(tmp_path, manifest_url):
    """A tool description IS prompt — the model reads it to decide when to call."""
    root = build_tree(tmp_path / "tree", server=CORE_TOOLS.replace(
        '"description": "kernel health"', '"description": "check the box"'))
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "kernel_status description differs" in result.stdout


def test_divergent_prompt_blocks(tmp_path, manifest_url):
    root = build_tree(tmp_path / "tree",
                      server='SYSTEM_PROMPT = "You are a phone assistant."\n')
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "system prompt differs" in result.stdout


def test_divergent_high_stakes_blocks(tmp_path, manifest_url):
    """One surface gates gmail_send, the other doesn't — a gate that exists on one
    machine and not the other is worse than no gate: it teaches false confidence."""
    root = build_tree(tmp_path / "tree",
                      server='LOCAL_HIGH_STAKES = frozenset()\n')
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "high-stakes: only on mac=['gmail_send']" in result.stdout


def test_double_declared_high_stakes_blocks(tmp_path, manifest_url):
    """Declared dangerous in BOTH the manifest and the surface = two sources of truth."""
    root = build_tree(tmp_path / "tree", core_tools=CORE_TOOLS.replace(
        'frozenset({"gmail_send"})', 'frozenset({"gmail_send", "kernel_decide"})'))
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "double-declared highStakes" in result.stdout
    assert "kernel_decide" in result.stdout


def test_manifest_schema_drift_blocks(tmp_path, manifest_url):
    """The old per-surface check, still doing its job after the merge."""
    root = build_tree(tmp_path / "tree", core_tools=CORE_TOOLS.replace(
        '"properties": {"id": {"type": "string"}}', '"properties": {"identifier": {"type": "string"}}'))
    result = run(root, manifest_url)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "kernel_decide" in result.stdout
    assert "missing properties: id" in result.stdout


def test_unreachable_kernel_warns_not_blocks(tmp_path):
    """Tunnel down on a converged tree is exit 3 — reload_app.sh/deploy.sh warn."""
    root = build_tree(tmp_path / "tree")
    result = run(root, "http://127.0.0.1:1/tools")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "Surfaces converged" in result.stdout


def test_unreachable_kernel_cannot_downgrade_real_drift(tmp_path):
    """The trap the old split checkers left open: with the kernel unreachable, a
    checker that only talks to the kernel reports 'warn' while the surfaces are already
    divergent. Cross-surface runs first precisely so this stays a block."""
    root = build_tree(tmp_path / "tree",
                      server='SYSTEM_PROMPT = "You are a phone assistant."\n')
    result = run(root, "http://127.0.0.1:1/tools")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "system prompt differs" in result.stdout


def test_unevaluable_declaration_is_fatal(tmp_path, manifest_url):
    """A surface that rebuilds TOOLS behind a comprehension is a surface this check
    cannot vouch for. Refuse (2) rather than silently treat it as 'inherits core'."""
    root = build_tree(tmp_path / "tree", server='''\
from core.tools import TOOLS as CORE_TOOLS
TOOLS = [t for t in CORE_TOOLS if t["name"] != "gmail_send"]
''')
    result = run(root, manifest_url)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot evaluate it" in result.stderr


def test_one_surface_is_not_a_comparison(tmp_path, manifest_url):
    """Running against a single surface is the blind spot the two old checkers had."""
    root = build_tree(tmp_path / "tree")
    (root / "server" / "__init__.py").unlink()
    (root / "server").rmdir()
    result = run(root, manifest_url)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "at least two surfaces" in result.stderr


def test_real_repo_surfaces_converge(manifest_url):
    """The live tree: mac/ and server/ must both still be pure core.

    Fails the moment someone hand-copies a tool list, a prompt, or a high-stakes set
    into either surface — which is the whole point of the convergence.
    """
    result = run(REPO, manifest_url)
    assert "Surfaces converged: mac, server" in result.stdout, result.stdout + result.stderr
    assert "surface mac:" in result.stdout and "inherits core" in result.stdout


def _load_checker():
    """Import check_tool_drift.py as a module (it guards its own __main__)."""
    spec = importlib.util.spec_from_file_location("_check_tool_drift", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_hand_maintained_tool_name_list_came_back():
    """LOCAL_NAMES rotted silently: a new tool just fell into 'informational'. The
    merged checker is keyed on surface symbols, never on tool names — keep it that way.

    Greping for the old variable name would only stop the old variable name; the same
    rot returns as SURFACE_LOCAL or KNOWN_TOOLS. So the forbidden words are derived from
    the tools core actually ships: no module-level constant in the checker may mention
    any of them, whatever it is called and whatever shape it takes.
    """
    real_tools = set(_load_checker().core_descriptor(REPO / "core").tools)
    assert real_tools, "core ships no tools — this guard would be vacuous"

    offenders = []
    for node in ast.parse(CHECKER.read_text()).body:
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if value is None:
            continue
        label = ", ".join(t.id for t in targets if isinstance(t, ast.Name)) or "<assign>"
        named = {n.value for n in ast.walk(value)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)} & real_tools
        if named:
            offenders.append(f"check_tool_drift.py:{node.lineno} {label} = ... {sorted(named)}")
    assert not offenders, (
        "a module-level constant in the checker is keyed by tool name — that list has to "
        "be edited every time a tool is added, and it rots silently:\n  "
        + "\n  ".join(offenders))
