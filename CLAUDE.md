# Pam — one brain, two surfaces

Restructured 2026-08-10. **`core/` is the brain and is surface-independent** — it imports
cleanly on headless Linux, so nothing in it may reach AppKit, sounddevice, Quartz, osascript
or the Keychain directly. Those live behind capability interfaces (`core/caps.py`,
`core/secrets.py`) with implementations in `mac/`.

| dir | what |
|---|---|
| `core/` | LiveSession, tools, services, backends, memory, VAD/turn-boundary (`audio_core.py`) |
| `mac/` | menubar app, sounddevice audio, screen/AX context, clipboard, herdr lanes |
| `server/` | FastAPI + WebSocket + PWA — **runs on the Mac**, a remote mic+speaker for the same LiveSession |

The phone talks to the **Mac**, over `tailscale serve` (HTTPS is mandatory — `getUserMedia`
refuses a plain-http origin). Thrivbe-1 hosts no voice instance; voice-bridge was deleted
2026-08-10 (`docs/BRIDGE-DECOMMISSION.md`). `mac/reverse_channel.py` exists for a possible
future T1-hosted brain and is **OFF by default** — don't build on it.

## Rules that bite

- **Rebuild ⇒ relaunch, always.** py2app replaces `Contents/Resources/lib/python312.zip`
  wholesale; a process still running against the old one dies on every session start with
  `ZipImportError: bad local file header` and says nothing. Use `reload_app.sh` (refuses
  during a live session, gates on tool-drift, rebuilds AND relaunches). Never split the
  build from the relaunch.
- **The pre-commit hook is at `core.hooksPath=.githooks`**, not `.git/hooks/`. It runs the
  full suite on every commit. Don't conclude "no gate exists" from an empty `.git/hooks/`.
- **Capability profiles are data** (`core/capabilities.py`). An excluded tool is REMOVED
  from the schema sent to the model — never present-and-erroring. Adding a tool means
  `core/tools.py` (schema) **and** `core/live_session.py::_do_tool` (dispatch); miss the
  dispatch and the call silently falls through. Kernel tool names must match
  `thrivbe-os/src/tool-manifest.ts` exactly.
- **The phone has no `run_shell`, by design.** Three adversarial rounds broke every
  "these commands are read-only" allowlist (`env rm -rf`, `git ls-remote --upload-pack=`,
  `uniq a b` writing via its second operand); `core/destructive.py` was deleted rather than
  patched. Don't reintroduce one.
- `check_tool_drift.py` must exit 0 before deploying — it proves both surfaces agree with
  each other and with the kernel manifest.
- Known flakes, not regressions: `test_phone_meets_desk.py::test_a_second_tab_takes_the_
  conversation_over_only_when_asked` (~1 in 3 full-suite runs, async teardown race) and
  `test_memory_store.py::test_concurrent_records_not_dropped`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **voice-agent** (2653 symbols, 5724 relationships, 179 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/voice-agent/context` | Codebase overview, check index freshness |
| `gitnexus://repo/voice-agent/clusters` | All functional areas |
| `gitnexus://repo/voice-agent/processes` | All execution flows |
| `gitnexus://repo/voice-agent/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
