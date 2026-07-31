#!/usr/bin/env python3
"""Live check for drift between agent Realtime tools and the kernel manifest.

This guards a SAFETY property, not just schema hygiene: the manifest's `highStakes`
flags decide which tools require a spoken confirmation gate (see live_session.py).
A tool that drifts out of the manifest loses its gate, so a red run here is not
cosmetic — fix it before trusting the confirm behaviour.

There is deliberately NO allowlist of "expected local" tools. Anything the agent has
that the manifest doesn't is simply reported as agent-local; that way a new tool can
never be *silently* unclassified (the old LOCAL_NAMES set rotted exactly that way).
"""

import ast
import json
import os
import sys
import urllib.error
import urllib.request


ENV_PATH = "/Users/robinsverd/Thrivbe-AI/.env"
MANIFEST_URL = "http://127.0.0.1:8790/tools"


def fail(message, code):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_token():
    token = None
    try:
        with open(ENV_PATH, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip() == "VOICE_API_TOKEN":
                        token = value.strip()
    except FileNotFoundError:
        pass
    return token or os.environ.get("VOICE_API_TOKEN")


def load_agent_tools():
    try:
        import tools
        return tools.TOOLS
    except Exception:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools.py")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "TOOLS" for target in node.targets):
                    return ast.literal_eval(node.value)
        except (OSError, SyntaxError, ValueError, TypeError) as error:
            fail(f"could not load TOOLS from {path}: {error}", 2)
        fail(f"module-level TOOLS assignment not found in {path}", 2)


def fetch_manifest(token):
    if not token:
        fail(f"VOICE_API_TOKEN not found in {ENV_PATH} or environment", 2)
    request = urllib.request.Request(MANIFEST_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            manifest = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as error:
        fail(f"could not fetch kernel manifest from {MANIFEST_URL}: {error}", 3)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list):
        fail("kernel manifest does not contain a tools list", 2)
    return manifest


def schemas(items):
    return {item["name"]: item for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)}


def compare(name, agent_tool, manifest_tool):
    agent_params = agent_tool.get("parameters") or {}
    manifest_params = manifest_tool.get("parameters") or {}
    drift = []
    for label in ("required", "properties"):
        agent_values = set(agent_params.get(label, [] if label == "required" else {}))
        manifest_values = set(manifest_params.get(label, [] if label == "required" else {}))
        if manifest_values - agent_values:
            drift.append(f"missing {label}: {', '.join(sorted(manifest_values - agent_values))}")
        if agent_values - manifest_values:
            drift.append(f"extra {label}: {', '.join(sorted(agent_values - manifest_values))}")
    if drift:
        print(f"DRIFT {name}: " + "; ".join(drift))
    return bool(drift)


def local_high_stakes():
    """The agent's locally-declared confirm-gated tools, or None if unreadable.

    Only works on the import path; under the AST fallback we can't evaluate the module,
    so the cross-check below is skipped rather than guessed at.
    """
    try:
        import tools
        return set(getattr(tools, "LOCAL_HIGH_STAKES", set()))
    except Exception:
        return None


def main():
    agent = schemas(load_agent_tools())
    manifest = fetch_manifest(load_token())
    all_manifest = schemas(manifest["tools"])
    kernel = {name: item for name, item in all_manifest.items() if item.get("kernel") is not None}
    print(f"Kernel manifest: version={manifest.get('version', 'unknown')}")
    drift = any(compare(name, agent[name], kernel[name]) for name in sorted(set(agent) & set(kernel)))
    missing = sorted(set(kernel) - set(agent))
    if missing:
        print("Kernel-backed missing from agent: " + ", ".join(missing))
    agent_only = sorted(set(agent) - set(all_manifest))
    if agent_only:
        print("Agent-local (informational): " + ", ".join(agent_only))

    # Safety surface: report the manifest's gate set, and refuse double-declaration.
    # A tool listed BOTH locally and in the manifest has two sources of truth for
    # whether it's dangerous — that is exactly the rot this check exists to stop.
    gated = sorted(name for name, item in all_manifest.items() if item.get("highStakes"))
    print("Manifest highStakes: " + (", ".join(gated) if gated else "(none)"))
    local_gated = local_high_stakes()
    if local_gated is None:
        print("Local highStakes: (skipped — tools module not importable)")
    else:
        print("Local highStakes: " + (", ".join(sorted(local_gated)) if local_gated else "(none)"))
        double = sorted(local_gated & set(all_manifest))
        if double:
            print("DRIFT double-declared highStakes (move the gate to the manifest): "
                  + ", ".join(double))
            drift = True

    if not drift:
        print("No required/property drift found on the kernel-backed intersection.")
    raise SystemExit(1 if drift else 0)


if __name__ == "__main__":
    main()
