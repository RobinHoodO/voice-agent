#!/usr/bin/env python3
"""Live check for drift between agent Realtime tools and the kernel manifest."""

import ast
import json
import os
import sys
import urllib.error
import urllib.request


ENV_PATH = "/Users/robinsverd/Thrivbe-AI/.env"
MANIFEST_URL = "http://127.0.0.1:8790/tools"
LOCAL_NAMES = {"run_shell", "remember", "recall", "put_text", "delegate", "set_prompt", "os_delegate"}


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


def main():
    agent = schemas(load_agent_tools())
    manifest = fetch_manifest(load_token())
    kernel = {name: item for name, item in schemas(manifest["tools"]).items() if item.get("kernel") is not None}
    print(f"Kernel manifest: version={manifest.get('version', 'unknown')}")
    drift = any(compare(name, agent[name], kernel[name]) for name in sorted(set(agent) & set(kernel)))
    missing = sorted(set(kernel) - set(agent))
    if missing:
        print("Kernel-backed missing from agent: " + ", ".join(missing))
    local = sorted(name for name in agent if name in LOCAL_NAMES)
    if local:
        print("Agent-local (expected): " + ", ".join(local))
    if not drift:
        print("No required/property drift found on the kernel-backed intersection.")
    raise SystemExit(1 if drift else 0)


if __name__ == "__main__":
    main()
