# Voice Agent — Watch/Speak Rebuild + Hook Fixes (handover)

You are picking up work in **`~/Thrivbe-AI/projects/voice-agent`** (standalone macOS
menubar voice agent; double-tap Control → OpenAI Realtime convo; built as a signed
`.app` via `./build_app.sh`). Branch: **`fix/bundle-settings-html`** (not yet merged
to main).

Guiding discipline this whole thread: **verify, don't trust proxies.** Probe real
CLI behavior empirically before building on it. Don't declare done off a self-report.

---

## What already shipped (committed on this branch, app rebuilt + running)

1. **Verify-or-be-honest harness** (`tools.py`): `VERIFY_HARNESS` + `_verify_wrap()` is
   now prepended to EVERY delegated instruction inside `_build_delegate_cmd`. It forces
   the background agent to restate the goal as an observable end-state → act → VERIFY by
   independent observation (not a script's "done" echo) → iterate → end with exactly one
   tag line: `VERIFIED:` / `UNVERIFIED:` / `FAILED:`. `live_session.py:_speak_announcement`
   was updated to tell the waking voice agent to relay that tag honestly.
   Self-check: `.venv/bin/python tools.py`.
2. **Watch-mode trust fix** (`tools.py`): the watch-mode Terminal now `cd`s into the
   configured workspace (`~/Thrivbe-AI`, already a trusted folder) before launching the
   agent, so Claude/pi never shows the "trust this folder?" dialog (a new Terminal opens
   in `$HOME`, which is untrusted). Headless mode was never affected (`-p` + redirected
   output skips the dialog).

Verified live: a watched Claude delegate ran the "arrange desktop icons" task, found
that `arrangeBy=kind` (Finder renders it) works where scripted x/y positions don't
(macOS won't render them), and emitted a real `VERIFIED:` tag. The harness works.

---

## THE TASK: unify watch + speak (design is decided)

**User's model (decided — implement this):**
- Spoken results should ALWAYS happen, invisibly, in the background — no matter what.
- The "task terminal" is a SEPARATE, OPTIONAL *view*. Turning it on shows pi/Claude
  working in a real Terminal; turning it off changes nothing about the spoken result.

**Why this is the right architecture:** it decouples "produce the result" from "watch
the work." Today the toggle *replaces* the headless path with an interactive one, and
interactive agents never exit → no `.done` sentinel → `_check_tasks` never auto-wakes →
no spoken result. Decoupling kills that problem: the always-on headless run produces the
spoken result; the Terminal is just a window onto it.

**Chosen fork: ONE run, tailed (NOT two runs).**
- Always: headless `<agent> -p --verbose` → capture to `out`, `touch done` on exit →
  `_check_tasks` auto-wakes and speaks the tag. Runs regardless of the toggle.
- If `show_task_terminals` is on: ALSO open a Terminal that `tail -f`s the SAME run's
  `out` file. One agent, one set of actions, live view.
- REJECTED: a second interactive agent for the pretty TUI — it runs the task twice
  (two agents both arranging icons = a race that fights itself). Never do duplicate
  actions. The live text stream (`--verbose`) is the acceptable trade for the boxed UI.

### Must-verify BEFORE writing code (the load-bearing assumption)
`-p --verbose` must stream progress INCREMENTALLY into the redirected `out` file (so the
`tail -f` viewer is live, not blank-until-the-end). Verify for BOTH `claude` and `pi`.
- Known: `pi -p` WITHOUT `--verbose` writes only the final answer (handover lore: a pipe
  strips the TTY → looks frozen). `--verbose` is supposed to fix this.
- A prior probe of `script -q <file> claude -p --verbose ...` FAILED (0-byte log, claude
  never showed alive) — inconclusive, likely PATH/syntax in a non-interactive shell. Do
  NOT trust that null result. Re-probe cleanly: run in a normal login shell, confirm the
  out file grows over time (not just at the end), and confirm the process exits.
- If `--verbose`-to-file does NOT stream live, fall back to: viewer tails the final
  output (less live but correct) — still ONE run, never duplicate the action.

### Two bugs to fix in the same pass (both block "always spoken results")
1. **Spoken result reads the WRONG end.** `live_session.py:_speak_announcement` does
   `txt = (self._announce or "")[:2500]` — the FIRST 2500 chars. The `VERIFIED/UNVERIFIED/
   FAILED` tag is the LAST line. On a `--verbose` stream the tag gets truncated away, so
   voice never speaks the verdict. Fix: read the TAIL of the output and explicitly extract
   the status-tag line. (`agent.py:_read_task_out` returns the full ANSI-stripped text;
   either fix there or in `_speak_announcement`.)
2. **`--verbose` noise.** verbose/tail logs are full of ANSI + repaints. `_read_task_out`
   already strips `\x1b[...]` escapes; make the tag extraction robust to the noise.

---

## Code anchors

- `tools.py`
  - `VERIFY_HARNESS`, `_verify_wrap()` — the harness (done, don't break).
  - `_build_delegate_cmd(instruction, cfg)` — THE function to rework. Current branches:
    - headless (`show_task_terminals` falsy): `nohup sh -c '<agent -p> "$(cat pf)"
      </dev/null > out 2>&1; touch done' >/dev/null 2>&1 & disown`
    - watch (`show_task_terminals` truthy): `osascript -e 'do script "cd <ws> &&
      <interactive agent> \"$(cat pf)\""'` — NO capture, NO done. (This branch is what
      changes: keep the headless run always; make watch ADD a `tail -f out` window.)
  - Returns `(cmd, out_path)`. The `out_path` is already returned for exactly a log window.
  - `__main__` self-check asserts the harness wraps every task + watch cmd cds to workspace.
    Add a check that the headless capture+done path runs even when the toggle is on.
- `live_session.py`
  - `_do_tool` `elif name == "delegate":` (~405) — builds + runs the cmd, prepends memory
    context, returns the spoken "started it" line.
  - `_speak_announcement` (~319) — bug #1 lives here (first-2500-chars truncation).
- `agent.py`
  - `_check_tasks` (~236) — polls `config.TASKS_DIR` for `.done` every 2s; SKIPS auto-wake
    if `self.live_on` (mid-conversation — by design, don't change).
  - `_read_task_out` (~259) — reads `<id>.out`, strips ANSI. Bug #2 area.
  - `_wake_and_speak` (~270) — opens a LiveSession with `announce=text`.
- `config.py` — `TASKS_DIR`; settings keys: `live.delegate` (pi|claude|off),
  `live.show_task_terminals` (bool), `live.workspace` (`~/Thrivbe-AI`),
  `live.pi_model` (default `deepseek-v4-flash`).

Current config: `delegate: claude`, `show_task_terminals: True`, `workspace: ~/Thrivbe-AI`.

> The two misfiring Stop hooks (claude-mem summarize id-mismatch + broken-Python
> `encodings` crash) that ate 6m15s are split out into **`HANDOVER_stop_hooks.md`** —
> a separate, independent task. Not part of this watch/speak work.

---

## Build / run / gotchas

- No system `python` — use `.venv/bin/python` or `python3`.
- Self-test: `.venv/bin/python tools.py` (harness + watch-cd asserts).
- Rebuild: `./build_app.sh` (signs with stable `Thrivbe Voice Dev` identity → TCC grants
  survive rebuilds). Python changes REQUIRE a full rebuild; HTML-only can be `cp`'d into
  `Thrivbe Voice.app/Contents/Resources/`.
- Relaunch: `pkill -f "Thrivbe Voice.app/Contents/MacOS"; open "Thrivbe Voice.app"`.
- Flaky: `tests/test_memory_store.py::test_concurrent_records_not_dropped` fails under
  full-suite load (SQLite schema-init race), passes alone — just retry the commit.
- Logs: `~/Library/Logs/ThrivbeVoice/agent.log` (+ `activity.log`). Tasks:
  `config.TASKS_DIR` (`<HHMMSS>.prompt` / `.out` / `.done`).
- Canonical test task: "arrange my desktop icons in a grid grouped by type" — exercises
  the verify harness (proxy success vs observed render) AND the watch/speak paths.

## Definition of done
- Toggle OFF: delegate runs invisibly, voice wakes and speaks the `VERIFIED/…` tag.
- Toggle ON: same spoken result PLUS a live Terminal tailing the same run.
- Exactly ONE agent run per delegate (no duplicate actions).
- Spoken result includes the status tag (tail/extraction fix landed).
- `--verbose` streaming-to-file verified empirically for claude AND pi before shipping.
