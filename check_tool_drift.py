#!/usr/bin/env python3
"""One drift check for every surface Pam runs on.

Merged 2026-08-09 from the two checkers that existed before — this file (Mac) and
`voice-bridge/check_tool_drift.py` (server). Two checkers is *how* the two surfaces
drifted apart: each could only see its own side, so neither could ever report the one
thing that matters — that the Mac and the server no longer agree.

WHAT IT GUARDS

1. **Cross-surface convergence.** Every surface (`mac/`, `server/`, plus anything passed
   with `--surface`) must expose the same tool set, the same tool schemas *and*
   descriptions, the same base system prompt, and the same locally-declared high-stakes
   set. The intended way to satisfy this is to declare **nothing**: a surface that ships
   no `TOOLS` / `LOCAL_HIGH_STAKES` / `LIVE_SYSTEM` of its own inherits `core`'s, and one
   edit in `core/` then lands on both surfaces by construction. The moment a surface
   re-declares one of those symbols it is compared, field by field, and any difference
   is drift.

1b. **Declared capability differences.** Some differences are real and intended: the
   phone surface has no `run_shell` and no `put_text`, so those tools must be ABSENT
   from its tool list rather than present and erroring. A surface says so by declaring
   `SURFACE_PROFILE = "<name>"` at module level, naming a profile in `core`'s `PROFILES`
   (core/capabilities.py). The exclusions in that profile are subtracted before the
   comparison, and printed. So an intended difference is DATA this check reports, and
   any difference that is not in the data is still drift. Once `core` defines `PROFILES`,
   a surface that declares no profile is refused (exit 2) — an unclassified surface is
   the blind spot this whole section exists to close.

   Nothing here is keyed to a profile NAME, and that matters more than it looks: the
   profile set is not fixed. `server` (a voice instance on Thrivbe-1) existed until
   2026-08-10 and is gone; `phone` replaced it when Robin ruled that there is one brain,
   on his Mac, and the phone is its microphone. This checker reads whatever `PROFILES`
   says today — the directory names it walks (`DEFAULT_SURFACES`) are the only fixed
   strings, and those are packages, not capabilities.

2. **The kernel manifest.** Each surface is still checked tool-by-tool against
   `GET /tools` on the voice API (:8790). The manifest's `highStakes` flags are what
   decide which tools need a spoken confirmation gate (`core/live_session.py`), so a
   tool that drifts out of the manifest silently loses its gate. A red run here is a
   safety finding, not schema hygiene.

3. **No double-declared gate.** A tool named in both a surface's `LOCAL_HIGH_STAKES`
   *and* the kernel manifest has two sources of truth for "is this dangerous" — exactly
   how a gate goes missing. Fails.

NO HAND-MAINTAINED TOOL LISTS. The old bridge checker carried
`LOCAL_NAMES = {"get_phone_status", "vibrate_phone", "send_sms"}`, and this file carried
an equivalent set before it was deleted: a list of tool *names* that had to be edited
every time a tool was added, so it rotted, and rotted **silently** — a new tool simply
fell into "informational" and nobody noticed it was unclassified. Nothing below is keyed
by tool name. The only fixed names are the three PROTOCOL symbols, which are a contract
*between surfaces*, not data *about tools*.

EXIT CODES — the contract `deploy.sh` and `reload_app.sh` depend on:

  0  converged
  1  drift — block the deploy
  2  the checker could not do its job — block (a blind check is worse than no check)
  3  the kernel manifest is unreachable (tunnel down) — warn, deploy anyway

Cross-surface comparison runs **first** and needs no kernel, so a down tunnel can never
downgrade a real divergence from "block" to "warn".

USAGE

  ./check_tool_drift.py                       # mac + server in this repo
  ./check_tool_drift.py --root <tree>         # another checkout — it must contain two
                                              #   surfaces, so not an .app bundle (which
                                              #   ships only core/ and mac/; deploy.sh
                                              #   covers the bundle by byte-comparing it
                                              #   against the repo instead)
  ./check_tool_drift.py --surface bridge=/path/to/voice-bridge
                                              # fold an out-of-tree surface into the
                                              #   same comparison (the legacy bridge)

`VOICE_TOOLS_MANIFEST_URL` overrides the manifest endpoint; a `file://` URL is how the
tests feed it a fixture without a kernel.
"""

import argparse
import ast
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

# --- the protocol -----------------------------------------------------------------
# A surface declares divergence by assigning one of these at module level. Assigning
# nothing means "I expose exactly core's" — which is the point.
TOOLS_SYMBOL = "TOOLS"
HIGH_STAKES_SYMBOL = "LOCAL_HIGH_STAKES"
PROMPT_SYMBOLS = ("LIVE_SYSTEM", "SYSTEM_PROMPT")
# core's table of capability profiles, and the name a surface picks out of it.
PROFILES_SYMBOL = "PROFILES"
PROFILE_SYMBOL = "SURFACE_PROFILE"
EXCLUSIONS_FIELD = "excluded_tools"

DEFAULT_SURFACES = ("mac", "server")
DEFAULT_MANIFEST_URL = "http://127.0.0.1:8790/tools"
ENV_CANDIDATES = (
    "/Users/robinsverd/Thrivbe-AI/.env",     # this Mac
    "/opt/voice-bridge/.env",                # the legacy bridge host
    "/opt/Thrivbe-AI/.env",                  # Thrivbe-1
)

EXIT_OK, EXIT_DRIFT, EXIT_BROKEN, EXIT_NO_KERNEL = 0, 1, 2, 3


def fail(message, code=EXIT_BROKEN):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


# --- reading a surface without importing it -----------------------------------------
# AST, never import: `mac/` needs PyObjC, a built .app's tree is not on sys.path, and an
# out-of-tree surface may not even be installable here. Anything the evaluator cannot
# resolve is reported, never guessed at — see resolve_symbol().

class Unresolved(Exception):
    pass


_CALLABLE_LITERALS = {"frozenset": frozenset, "set": set, "tuple": tuple,
                      "list": list, "dict": dict}


def _literal(node, env):
    """Evaluate a module-level constant expression, resolving earlier module names."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise Unresolved(f"name {node.id!r} is not a module-level constant")
    if isinstance(node, ast.List):
        return [_literal(item, env) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal(item, env) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_literal(item, env) for item in node.elts}
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):      # {**spread}
            raise Unresolved("dict unpacking")
        return {_literal(k, env): _literal(v, env) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None)
        if name in _CALLABLE_LITERALS and not node.keywords and len(node.args) <= 1:
            arg = _literal(node.args[0], env) if node.args else ()
            return _CALLABLE_LITERALS[name](arg)
        raise Unresolved(f"call to {name or 'expression'}()")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal(node.left, env) + _literal(node.right, env)
    raise Unresolved(type(node).__name__)


def module_constants(path):
    """{name: value} for every module-level assignment this file can resolve.

    Returns two dicts: resolved values, and {name: reason} for assignments that exist
    but could not be evaluated — the caller decides whether an unresolved name matters.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (OSError, SyntaxError, ValueError) as error:
        fail(f"could not parse {path}: {error}")
    resolved, unresolved = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            targets, value = [node.target.id], node.value
        else:
            continue
        if not targets:
            continue
        try:
            evaluated = _literal(value, resolved)
        except Unresolved as reason:
            for name in targets:
                unresolved[name] = str(reason)
            continue
        for name in targets:
            resolved[name] = evaluated
            unresolved.pop(name, None)
    return resolved, unresolved


def python_files(directory):
    """Top-level *.py in a surface directory, deterministically ordered."""
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".py"))
    except OSError as error:
        fail(f"could not read surface directory {directory}: {error}")
    return [os.path.join(directory, name) for name in names]


def resolve_symbol(directory, wanted):
    """Find the one module-level declaration of `wanted` under `directory`.

    Returns (value, path) or (None, None) when nothing declares it. An assignment that
    exists but cannot be evaluated is fatal, not ignored: a surface that redeclares
    TOOLS behind a comprehension is a surface this check can no longer vouch for, and a
    check that cannot see is worse than no check (exit 2).
    """
    found = []
    for path in python_files(directory):
        resolved, unresolved = module_constants(path)
        for name in ([wanted] if isinstance(wanted, str) else wanted):
            if name in unresolved:
                fail(f"{path} assigns {name} but this checker cannot evaluate it "
                     f"({unresolved[name]}). Make it a literal, or import it from core "
                     f"instead of rebuilding it here.")
            if name in resolved:
                found.append((resolved[name], path, name))
    if not found:
        return None, None
    if len({path for _, path, _ in found}) > 1:
        places = ", ".join(sorted(f"{path}:{name}" for _, path, name in found))
        fail(f"{os.path.basename(directory)} declares the same surface symbol in more "
             f"than one place ({places}) — there can only be one.")
    value, path, _ = found[0]
    return value, path


def normalize_tools(raw, where):
    """name -> {description, parameters}, accepting both schema shapes.

    Flat (`{"name": ...}`) is the Realtime shape core uses; nested
    (`{"function": {...}}`) is the chat-completions shape the legacy bridge uses. The
    surfaces must agree on the tools, not on which envelope they are wrapped in.
    """
    if not isinstance(raw, list):
        fail(f"TOOLS in {where} is not a list")
    tools = {}
    for entry in raw:
        item = entry.get("function") if isinstance(entry, dict) and "function" in entry else entry
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        parameters = item.get("parameters")
        tools[item["name"]] = {
            "description": (item.get("description") or "").strip(),
            "parameters": parameters if isinstance(parameters, dict) else {},
        }
    if not tools:
        fail(f"TOOLS in {where} contains no named tools")
    return tools


class Descriptor:
    """What a surface exposes, and where each field came from."""

    def __init__(self, tools, high_stakes, prompt, origin, profile=None,
                 excluded=frozenset(), profiles=None):
        self.tools = tools
        self.high_stakes = frozenset(high_stakes)
        self.prompt = prompt
        self.origin = origin
        self.profile = profile           # the capability profile this surface declared
        self.excluded = frozenset(excluded)   # tools that profile removes, by design
        self.profiles = profiles         # core only: the whole profile table

    def signature(self):
        payload = json.dumps(
            {"tools": self.tools, "high_stakes": sorted(self.high_stakes),
             "prompt": self.prompt}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def core_descriptor(core_dir):
    tools_raw, tools_path = resolve_symbol(core_dir, TOOLS_SYMBOL)
    if tools_raw is None:
        fail(f"no module-level {TOOLS_SYMBOL} anywhere in {core_dir} — this is the "
             f"canonical brain; without it there is nothing to compare surfaces against.")
    gated, gated_path = resolve_symbol(core_dir, HIGH_STAKES_SYMBOL)
    prompt, prompt_path = resolve_symbol(core_dir, PROMPT_SYMBOLS)
    if prompt is None:
        fail(f"no module-level {' / '.join(PROMPT_SYMBOLS)} in {core_dir}")
    profiles, _profiles_path = resolve_symbol(core_dir, PROFILES_SYMBOL)
    if profiles is not None and not isinstance(profiles, dict):
        fail(f"{PROFILES_SYMBOL} in {core_dir} is not a table of profiles")
    return Descriptor(
        normalize_tools(tools_raw, tools_path),
        gated or frozenset(),
        prompt,
        {"tools": tools_path, "high_stakes": gated_path, "prompt": prompt_path},
        profiles=profiles,
    )


def profile_exclusions(name, directory, core):
    """(profile name, excluded tools) for one surface.

    A surface names a profile; the profile says which tools it does not have. Both
    halves are refused rather than defaulted — a typo that fell back to "excludes
    nothing" would report convergence for two surfaces that ship different tools.
    """
    declared, declared_path = resolve_symbol(directory, PROFILE_SYMBOL)
    if core.profiles is None:
        return declared, frozenset()
    if declared is None:
        fail(f"{os.path.basename(directory)} declares no module-level {PROFILE_SYMBOL}. "
             f"core defines {PROFILES_SYMBOL}, so every surface has to say which one it "
             f"runs — an unclassified surface is compared as if it offered everything, "
             f"which is exactly the blind spot this check exists to close.")
    if not isinstance(declared, str) or declared not in core.profiles:
        fail(f"{declared_path} declares {PROFILE_SYMBOL} = {declared!r}, which is not a "
             f"profile core defines ({', '.join(sorted(core.profiles))}).")
    return declared, frozenset(core.profiles[declared].get(EXCLUSIONS_FIELD) or ())


def surface_descriptor(name, directory, core):
    """core's descriptor, with any field the surface redeclares for itself, and its
    declared capability exclusions subtracted from the tool list."""
    tools_raw, tools_path = resolve_symbol(directory, TOOLS_SYMBOL)
    gated, gated_path = resolve_symbol(directory, HIGH_STAKES_SYMBOL)
    prompt, prompt_path = resolve_symbol(directory, PROMPT_SYMBOLS)
    origin = dict(core.origin)
    tools = core.tools
    if tools_raw is not None:
        tools, origin["tools"] = normalize_tools(tools_raw, tools_path), tools_path
    high_stakes = core.high_stakes
    if gated is not None:
        high_stakes, origin["high_stakes"] = frozenset(gated), gated_path
    if prompt is not None:
        origin["prompt"] = prompt_path
    else:
        prompt = core.prompt
    profile, excluded = profile_exclusions(name, directory, core)
    tools = {tool: schema for tool, schema in tools.items() if tool not in excluded}
    return Descriptor(tools, high_stakes, prompt, origin, profile=profile,
                      excluded=excluded)


def stale_exclusions(core):
    """Profile exclusions naming a tool core no longer ships.

    Dead data, and dangerous dead data: rename a Mac-only tool and its exclusion stops
    matching, so the renamed tool silently reappears on the surface that cannot run it.
    """
    findings = []
    for name, profile in sorted((core.profiles or {}).items()):
        unknown = sorted(set(profile.get(EXCLUSIONS_FIELD) or ()) - set(core.tools))
        if unknown:
            findings.append(f"profile {name} excludes tools core does not ship: "
                            + ", ".join(unknown))
    return findings


# --- comparison ---------------------------------------------------------------------

def _prompt_diff(left, right):
    left_lines, right_lines = left.splitlines(), right.splitlines()
    for index in range(max(len(left_lines), len(right_lines))):
        a = left_lines[index] if index < len(left_lines) else "(ends)"
        b = right_lines[index] if index < len(right_lines) else "(ends)"
        if a != b:
            return f"line {index + 1}: {a[:70]!r} vs {b[:70]!r}"
    return "trailing whitespace"


def compare_descriptors(left_name, left, right_name, right):
    """Every way two surfaces can disagree, in the order a human wants to read them."""
    differences = []
    # A tool missing from one side is drift UNLESS that side's profile says it is not
    # there. Declared absence is a decision; undeclared absence is a divergence.
    only_left = sorted(set(left.tools) - set(right.tools) - right.excluded)
    only_right = sorted(set(right.tools) - set(left.tools) - left.excluded)
    if only_left:
        differences.append(f"only on {left_name}: {', '.join(only_left)}")
    if only_right:
        differences.append(f"only on {right_name}: {', '.join(only_right)}")
    for name in sorted(set(left.tools) & set(right.tools)):
        a, b = left.tools[name], right.tools[name]
        for label in ("required", "properties"):
            empty = [] if label == "required" else {}
            a_values = set((a["parameters"] or {}).get(label, empty))
            b_values = set((b["parameters"] or {}).get(label, empty))
            if a_values != b_values:
                differences.append(
                    f"{name} {label}: {left_name}={sorted(a_values)} "
                    f"{right_name}={sorted(b_values)}")
        if a["description"] != b["description"]:
            differences.append(f"{name} description differs (the model reads it — it is prompt)")
    if left.high_stakes != right.high_stakes:
        differences.append(
            f"high-stakes: only on {left_name}="
            f"{sorted(left.high_stakes - right.high_stakes) or '-'}, only on {right_name}="
            f"{sorted(right.high_stakes - left.high_stakes) or '-'}")
    if left.prompt != right.prompt:
        differences.append(f"system prompt differs — {_prompt_diff(left.prompt, right.prompt)}")
    return differences


# --- kernel manifest ----------------------------------------------------------------

def load_token(root):
    for path in (os.path.join(root, ".env"), *ENV_CANDIDATES):
        try:
            with open(path, encoding="utf-8") as env_file:
                for raw_line in env_file:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "VOICE_API_TOKEN" and value.strip():
                        return value.strip()
        except OSError:
            continue
    return os.environ.get("VOICE_API_TOKEN")


def fetch_manifest(token, url):
    if not token:
        fail(f"VOICE_API_TOKEN not found in {', '.join(ENV_CANDIDATES)} or the environment")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            manifest = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as error:
        fail(f"could not fetch kernel manifest from {url}: {error}", EXIT_NO_KERNEL)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list):
        fail("kernel manifest does not contain a tools list")
    return manifest


def manifest_schemas(items):
    return {item["name"]: item for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)}


def compare_against_manifest(label, descriptor, all_manifest, kernel_backed):
    """The old per-surface check, unchanged in spirit: required/properties on the
    kernel-backed intersection, plus the double-declared-gate refusal."""
    drift = False
    for name in sorted(set(descriptor.tools) & set(kernel_backed)):
        surface_params = descriptor.tools[name]["parameters"] or {}
        manifest_params = kernel_backed[name].get("parameters") or {}
        differences = []
        for field in ("required", "properties"):
            empty = [] if field == "required" else {}
            surface_values = set(surface_params.get(field, empty))
            manifest_values = set(manifest_params.get(field, empty))
            if manifest_values - surface_values:
                differences.append(
                    f"missing {field}: {', '.join(sorted(manifest_values - surface_values))}")
            if surface_values - manifest_values:
                differences.append(
                    f"extra {field}: {', '.join(sorted(surface_values - manifest_values))}")
        if differences:
            print(f"DRIFT [{label}] {name}: " + "; ".join(differences))
            drift = True
    missing = sorted(set(kernel_backed) - set(descriptor.tools))
    if missing:
        print(f"[{label}] kernel-backed missing from the surface: " + ", ".join(missing))
    surface_only = sorted(set(descriptor.tools) - set(all_manifest))
    if surface_only:
        print(f"[{label}] surface-local (informational): " + ", ".join(surface_only))
    double = sorted(descriptor.high_stakes & set(all_manifest))
    if double:
        print(f"DRIFT [{label}] double-declared highStakes (move the gate to the "
              f"manifest): " + ", ".join(double))
        drift = True
    return drift


# --- entry point --------------------------------------------------------------------

def parse_surfaces(root, extra):
    surfaces = {}
    for name in DEFAULT_SURFACES:
        directory = os.path.join(root, name)
        if os.path.isdir(directory):
            surfaces[name] = directory
    for spec in extra or []:
        if "=" not in spec:
            fail(f"--surface expects NAME=PATH, got {spec!r}")
        name, path = spec.split("=", 1)
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            fail(f"--surface {name}: {path} is not a directory")
        surfaces[name.strip()] = path
    if len(surfaces) < 2:
        fail(f"need at least two surfaces to compare; found {sorted(surfaces) or 'none'} "
             f"under {root}. This check exists to compare surfaces — running it against "
             f"one is the blind spot the two old checkers had.")
    return surfaces


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)),
                        help="tree holding core/ and the surface packages")
    parser.add_argument("--surface", action="append", metavar="NAME=PATH",
                        help="fold an out-of-tree surface into the same comparison")
    args = parser.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.root))
    core_dir = os.path.join(root, "core")
    if not os.path.isdir(core_dir):
        fail(f"no core/ under {root}")
    core = core_descriptor(core_dir)
    surfaces = parse_surfaces(root, args.surface)

    descriptors = {name: surface_descriptor(name, path, core)
                   for name, path in sorted(surfaces.items())}
    for name, descriptor in descriptors.items():
        inherited = [field for field, path in descriptor.origin.items()
                     if path == core.origin[field]]
        own = {field: path for field, path in descriptor.origin.items()
               if path != core.origin[field]}
        note = "inherits core" if len(inherited) == 3 else \
            "own: " + ", ".join(f"{f}={os.path.relpath(p, root)}" for f, p in sorted(own.items()))
        profile = f", profile={descriptor.profile}" if descriptor.profile else ""
        print(f"surface {name}: {len(descriptor.tools)} tools, "
              f"{len(descriptor.high_stakes)} local high-stakes, sig={descriptor.signature()} "
              f"({note}{profile})")
        if descriptor.excluded:
            # Printed, not silent: the intended difference has to be as visible as an
            # unintended one, or "converged" stops meaning anything.
            print(f"  {name} does not have (by profile): "
                  + ", ".join(sorted(descriptor.excluded)))

    # 1. Cross-surface FIRST: it needs no kernel, so a down tunnel can never turn a real
    #    divergence into a warning.
    drift = False
    for finding in stale_exclusions(core):
        print(f"DRIFT {finding}")
        drift = True
    names = sorted(descriptors)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            differences = compare_descriptors(left, descriptors[left], right, descriptors[right])
            for difference in differences:
                print(f"DRIFT {left} vs {right}: {difference}")
            drift = drift or bool(differences)
    if not drift:
        print(f"Surfaces converged: {', '.join(names)} — one edit lands on all of them.")

    # 2. Both surfaces against the kernel manifest. Identical surfaces are checked once.
    token = load_token(root)
    url = os.environ.get("VOICE_TOOLS_MANIFEST_URL", DEFAULT_MANIFEST_URL)
    try:
        manifest = fetch_manifest(token, url)
    except SystemExit as exit_signal:
        if exit_signal.code == EXIT_NO_KERNEL and drift:
            # Never let an unreachable kernel downgrade a real divergence to a warning.
            raise SystemExit(EXIT_DRIFT)
        raise
    all_manifest = manifest_schemas(manifest["tools"])
    kernel_backed = {name: item for name, item in all_manifest.items()
                     if item.get("kernel") is not None}
    print(f"Kernel manifest: version={manifest.get('version', 'unknown')} "
          f"generatedAt={manifest.get('generatedAt', 'unknown')}")
    gated = sorted(name for name, item in all_manifest.items() if item.get("highStakes"))
    print("Manifest highStakes: " + (", ".join(gated) if gated else "(none)"))

    groups = {}
    for name in names:
        groups.setdefault(descriptors[name].signature(), []).append(name)
    for members in groups.values():
        label = ", ".join(members)
        drift = compare_against_manifest(label, descriptors[members[0]],
                                         all_manifest, kernel_backed) or drift

    if not drift:
        print("No drift: surfaces agree with each other and with the kernel manifest.")
    raise SystemExit(EXIT_DRIFT if drift else EXIT_OK)


if __name__ == "__main__":
    main()
