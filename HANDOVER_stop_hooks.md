# Fix two misfiring Claude Code Stop hooks (handover)

These are **global / plugin Claude Code hooks** on this machine (`robinsverd`), NOT
voice-agent code. They were discovered inside a delegate session spawned by the voice
agent (`~/Thrivbe-AI/projects/voice-agent`), but they fire on EVERY `claude` session and
should be fixed at the hook level. A delegate Claude burned **6m15s** thrashing on these
*after* it had already finished its real task.

`claude` reports "**Ran 11 stop hooks**" — global `~/.claude/settings.json` Stop has only
`session-stop-reminder.sh`, so the other ~10 come from installed plugins.

---

## Bug 1 — claude-mem "summarize" Stop hook: transcript id mismatch

**Symptom:** every turn the Stop hook errors with an escalating chain — missing transcript
file → empty file → unparseable → `No message found for role 'assistant'`. The agent
keeps "fixing" each by hand-authoring a stub `.jsonl`, which only surfaces the next field
the summarizer demands. Infinite band-aid loop (worse under `--dangerously-skip-permissions`,
which delegate sessions use).

**Root cause (diagnosed by the trapped agent, looks correct):** the harness wrote the
session transcript under one session id (e.g. `fb8ade7d…`) but the claude-mem summarize
hook looks for a different id (e.g. `9a0bdb07…`) at
`~/.claude/projects/<encoded-cwd>/<id>.jsonl`. The hook never finds the real transcript
and fails forever.

**Where:** `~/.claude/plugins/cache/thedotmack/claude-mem/9.0.17/hooks/hooks.json`
(defines the Stop hook(s)). Trace the Stop entry → the script it runs → how it derives the
transcript path / session id.

**Fix directions (pick after confirming root cause):**
- Make the hook resolve the transcript by the ACTUAL current session id (the value Claude
  passes the hook), not a stale/derived one — i.e. the bug is path/id derivation in the
  hook script.
- OR, if it's a claude-mem-vs-harness id contract mismatch, report/patch claude-mem.
- Stopgap: disable the claude-mem **summarize** Stop hook (env `ECC_DISABLED_HOOKS`, or
  remove it from the plugin's `hooks.json` / disable the plugin's Stop hook) so sessions
  end cleanly. Do NOT leave hand-authored stub transcripts as the "fix."

---

## Bug 2 — a Stop hook crashes with a broken Python

**Symptom:**
```
Stop hook error: Failed with non-blocking status code: Fatal Python error:
Failed to import encodings module
Python runtime state: core initialized
ModuleNotFoundError: No module named 'encodings'
```
This is a Python interpreter started without its standard library — classic broken
`PYTHONHOME`/`PYTHONPATH`, or a hook shebang/command pointing at an interpreter that
isn't a complete Python (e.g. a venv/py2app `python` invoked outside its bundle).

**Investigate:**
- Enumerate all Stop hooks: `grep -rl '"Stop"' ~/.claude/plugins/cache/**/hooks*.json`
  and `~/.claude/settings.json`; list each Stop command.
- Find the one that runs `python`/`python3`. Check its shebang / resolved interpreter
  (`head -1 <script>`, `command -v python3`).
- Check for a poisoned env: is `PYTHONHOME` or `PYTHONPATH` set in the login shell
  (`~/.zshrc`, `~/.zprofile`) or exported into the session? The voice agent is a py2app
  bundle with its own Python — confirm it isn't exporting `PYTHONHOME`/`PYTHONPATH` that
  leaks into shells/Terminals it spawns. (`do script` opens a fresh login shell, so this
  is a hypothesis to verify, not a certainty.)

**Fix directions:**
- Point the offending hook at a real interpreter (absolute path to a working
  `python3`), or clear the bad `PYTHONHOME`/`PYTHONPATH` for the hook command.
- If it's a leaked env from the voice-agent bundle, strip those vars before it spawns
  Terminals.
- Stopgap: disable that specific Stop hook via `ECC_DISABLED_HOOKS`.

---

## Useful commands
```sh
# All Stop hooks across global settings + plugins
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('hooks',{}).get('Stop'))"
ls ~/.claude/plugins/cache/*/*/*/hooks/hooks.json 2>/dev/null
grep -rl '"Stop"' ~/.claude/plugins/cache 2>/dev/null

# claude-mem hook config
python3 -m json.tool ~/.claude/plugins/cache/thedotmack/claude-mem/9.0.17/hooks/hooks.json

# Reproduce: run any short claude session and watch the Stop-hook output
claude -p "say hi" --verbose
```

## Definition of done
- A normal `claude` session ends with **zero** Stop-hook errors (no missing-transcript
  loop, no `encodings` crash).
- Root cause fixed at the hook/env level (not stub transcripts, not blanket-disabling
  everything) — disabling a specific broken hook is acceptable if its owner is filed/known.
- Verified by running a real session start→stop and confirming clean exit.
