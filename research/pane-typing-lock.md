# What stops lane nudges from pressing Enter on an open question box

Research for [issue 20](https://github.com/RobinHoodO/voice-agent/issues/20), map [issue 8](https://github.com/RobinHoodO/voice-agent/issues/8).
Date: 2026-11-12. Read-only: no keys were sent to any pane. herdr 0.7.1. voice-agent `origin/main` at `4d95845`.
Paths under `Thrivbe-OS/` are relative to `/Users/robinsverd/Thrivbe-AI/`.

## Recommendation

**Guard: one shared send script, `pane-send`, that checks the live screen itself.** Every sender calls it. It is option (a), but its "dialog open" test is herdr's own live state plus the screen text (option b), not the attention flag file. A short per-pane lock (option c) lives inside it only to stop two senders interleaving.

**Typer: a small launchd poller on the Mac** that reads answered rows from the kernel and calls the same `pane-send`. Not inside PAM.

## 1. Who types into panes today

Code that types by itself:

| Sender | Where | Check before typing |
|---|---|---|
| Handover / wayfinder relaunch | `Thrivbe-OS/skills/handover-clipboard/scripts/relaunch.sh:38` (`pane run "/clear"`), `:45-46` (`send-text` + `send-keys Enter`) | status `idle`/`done` (`:24`), input line empty (`:19`), session id changed after `/clear` (`:42`). No dialog check; it relies on `idle`/`done`, and an open dialog reads `blocked`, so it waits. |
| Wayfinder auto-next hook | `Thrivbe-OS/global/claude/hooks/wayfinder-autonext.sh:16,25` | starts `relaunch.sh` detached; same checks. |
| PAM `_lane_send` | `core/tools.py:945-956` on `origin/main` | only "not the protected `orchestrator` name" and "not `/exit`". No screen check. Presses Enter, waits 1.5 s, presses Enter **again** if the lane reads `idle`. Callers: `:1252` (`/clear`), `:1262`, `:1596` (`continue_task`). |
| PAM `pane run` | `core/tools.py:1208`, `:1265` | new or just-cleared lanes only. |

Agents that type because a skill tells them to (prose, run through the Bash tool):

- Lane to orchestrator callback and orchestrator to lane steer: `Thrivbe-OS/skills/herdr/SKILL.md:398-399`, `:424-431`, `:450-456`, and "push answers" `:262`. The callback block is pasted into every lane brief. This is the sender behind both incidents (4 Nov and 11 Nov) in the memory note `no-question-dialog-while-lanes-nudge.md`.
- Pane self-discovery ping: `Thrivbe-OS/skills/herdr/SKILL.md:52-53`.
- `Thrivbe-OS/skills/pam/SKILL.md:87-88`.
- Hermes to Mac over ssh: `Thrivbe-OS/skills/pi-herdr-bridge/SKILL.md:114-116`, `:135`.
- New-pane launch recipes: `Thrivbe-OS/skills/dgx-spark/SKILL.md:703-711`, `Thrivbe-OS/skills/davidondrej-herdr/SKILL.md:37,114`.
- The same skill files are mirrored for Codex under `/Users/robinsverd/.codex/skills/` (herdr, pam, pi-herdr-bridge, dgx-spark, davidondrej-herdr).

Not senders: nothing in `Thrivbe-OS/global/claude/hooks/` other than the wayfinder hook, nothing in `Thrivbe-OS/execution/scripts/`, nothing in `apps/kernel/src`, and no launchd job (`~/Library/LaunchAgents` only has `com.thrivbe.herdr-server`). PAM "hands" (`core/hands.py`, `mac/hands.py`) click and type into the **front macOS window** through Accessibility; they do not address herdr panes.

## 2. What herdr offers

From `herdr agent --help`, `herdr pane --help`, `herdr wait --help`:

- **Pane state: yes.** `herdr pane get <pane>` and `herdr agent list` return `agent_status` (`idle|working|blocked|done|unknown`) and `agent_session.value` (the Claude session id).
- **An open question box reads `blocked`.** Live proof, pane `w22:p19`: `pane get` gave `"agent_status":"blocked"`, and `herdr agent explain w22:p19 --json` gave `matched_rule: live_blocked_form` (priority 980, region `after_last_horizontal_rule`, matched on `esc to cancel`), `visible_blocker: true`. But `blocked` also covers permission prompts (`bash_permission_prompt`, `generic_permission_prompt`) and MCP elicitation, so `blocked` alone does not say "question box".
- **Screen read: yes.** `herdr pane read <pane> --source visible --lines N`. On `w22:p19` it returned the full dialog: tab row (`☐ Merge fix ☐ Come back ☐ Group btn ✔ Submit`), question text, numbered options, and the footer `Enter to select · Tab/Arrow keys to navigate · Esc to cancel`.
- **Wait: yes.** `herdr wait agent-status <pane> --status ...` and `herdr wait output <pane> --match <text>`.
- **Lock: no.** No lock, reserve or exclusive-input command exists. `send-text`, `send-keys`, `agent send` and `pane run` take no "only if" flag. `pane report-metadata --custom-status ... --ttl-ms` could show an advisory label, but nothing reads it before a send.
- The detection rules come from a remote manifest (`manifest_source: remote:.../agent-detection/remote/claude.toml`, version `2026.10.11.1`). A Claude Code UI change can break `blocked` detection until the manifest catches up.

## 3. Existing detection

- `Thrivbe-OS/global/claude/hooks/attention-state.sh` writes `~/.claude/attention/<session>.json` with `state: waiting-on-you` on PreToolUse AskUserQuestion and on PermissionRequest (lines 15-22). Live example: `a7ad900e-....json` = `{"state":"waiting-on-you","ts":1794474712,...}` for the pane above.
- It is **not** a usable "dialog open" flag:
  1. Keyed by session id, not pane. A sender must map pane to session through `herdr pane get` first.
  2. It is never cleared when the dialog is answered. Only UserPromptSubmit, Stop and SessionEnd change it (lines 16-20); there is no PostToolUse mapping and no PostToolUse hook entry for it in `~/.claude/settings.json`. So it stays `waiting-on-you` for the rest of the turn.
  3. It is set even when no dialog appears: the same PreToolUse event runs `auto-decide.py`, which can deny the ask (`auto-decide.py:412-423`).
  4. It also means "permission prompt".
- `auto-decide.py` holds no pane data. Its `flag(session_id)` (`:75`) is the auto-decide on/off switch, not a dialog flag. It records the answer on PostToolUse (`:400-407`) and raises to the kernel with question, options and source only (`raise_body`, `:158-168`); no pane id, no session id in the body.
- `relaunch.sh` gives the reusable checks: `input_empty` (`:19`), `session_id` (`:20`), `agent_status` (`:21`), `turn_finished` accepting `idle|done` (`:24`).

So: no per-pane "question box open" flag exists. herdr's live `blocked` plus the footer text on screen is the only current truth.

## 4. Guard options

| Option | What it does | Races and holes that remain |
|---|---|---|
| (a) shared wrapper + flag file | Refuses when the pane's flag says "dialog open", unless the caller is the inbox with the matching question id. | The flag is wrong in both directions today (section 3): stale after an answer, set with no dialog. Needs a new PostToolUse hook to clear it, and that hook has a gap between "answered" and "cleared". Flag is written by a 5 s hook before the dialog draws, so the inbox can type before the box exists. Senders that skip the wrapper are not stopped. |
| (b) screen check before typing | Read the pane, confirm the footer and the exact question text, type, re-read. | Check-then-type gap: a dialog can open between the read and the key (tens of ms for one key; a full second for today's `send` + `sleep 1` + `Enter` pair). Robin can answer at the desk in the same gap, so keys land in the next prompt. Long questions wrap or scroll off, so the text match can fail (fails safe: no typing). If every sender does its own check, the checks drift apart. |
| (c) lock file per pane | Sender takes `lock/<pane>` before typing. | Purely advisory; herdr does not honour it and neither does Robin's keyboard. Does not know whether a dialog is open. Stale locks after a crash need a timeout. Solves only "two senders interleave". |

**Pick: (a) with (b) inside it.** One script, proposed home `Thrivbe-OS/execution/scripts/pane-send`, two modes:

- `pane-send nudge <pane> "<one line>"`: take the pane lock; refuse unless `agent_status` is `idle|done|working` **and** the visible screen has no `Enter to select` / `Esc to cancel` footer; type; re-check status and footer **again just before Enter**; press Enter; verify. On refuse, exit non-zero so the lane writes to its output file (the rule already in the memory note).
- `pane-send answer <pane> --session <id> --question "<text>" --option <n>`: inbox only. The four checks already set in the issue 11 resolution (session id matches, status `blocked`, footer and exact question text on screen, row still open), then keys one at a time, then re-read.

Enforcement, so the wrapper is not just one more prose rule: a deny rule in the existing PreToolUse Bash hook (`Thrivbe-OS/global/claude/hooks/bash-discipline.sh`) for raw `herdr agent send`, `herdr pane send-keys`, `herdr pane send-text` aimed at another pane, pointing at `pane-send`. PAM's `_lane_send` (`core/tools.py:945`) calls the script instead of `_herdr("agent","send")`. `relaunch.sh` moves its two sends to it.

Races that still remain with the pick:

1. A dialog opens in the few ms between the last check and the Enter. Narrowed (re-check right before Enter), not closed. Only herdr could close it with an atomic "send if state is X".
2. Robin answers at the desk at the same moment the inbox types. The post-type re-read and the PostToolUse capture detect it; they do not prevent it.
3. Senders outside Claude's Bash hook: Codex and pi sessions, Hermes over ssh (`pi-herdr-bridge`), any script calling the herdr binary directly. They follow the wrapper only by skill text.
4. herdr's detection manifest lags a Claude Code UI change: `blocked` is missed. The footer text check is the second net; if both miss, the nudge goes through.
5. `nudge` text itself may act on a dialog that opens mid-typing (digits may pick options). Not tested; see questions.

## 5. Where the Mac-side typer runs

Facts:

- PAM is a GUI app, not a service. `run.sh` does `open -n "Thrivbe Voice.app"`; the repo plist `com.thrivbe.voice-agent.plist` has `RunAtLoad` only, no `KeepAlive`, and is **not installed** in `~/Library/LaunchAgents` (only `com.thrivbe.pam-scorecard` is). It runs now (PID 1269) because it was opened. If it quits or crashes, nothing restarts it.
- PAM's herdr seam hides failure: `_herdr()` returns `None` on any error (`core/tools.py:779-791`), and `_lane_send` ignores the Enter result. A typer needs the opposite: loud failure and a "Not delivered" state for the phone card (issue 16).
- PAM already reaches the kernel at `http://127.0.0.1:8790` through a launchd SSH tunnel (`core/kernel_tools.py:12-13`), so a poller on the Mac can use the same address with `DECISIONS_API_TOKEN`.
- The Mac already runs this shape: `com.thrivbe.attention-sync` is a launchd job with `StartInterval 60` running one shell script.
- Mac asleep: neither choice works; Mac agents are asleep too (issue 11). `pmset -g` shows sleep currently prevented (`sleep 0`, held by Claude and `caffeinate`). launchd runs a missed interval job once on wake, so answers given while asleep are typed on wake, after the full re-check (a stale dialog fails the check and the row is marked not delivered).

**Pick: launchd poller.** One short script: fetch rows that are answered and not yet delivered, call `pane-send answer` for each, post the result back. It runs without PAM, restarts by itself, keeps the guard in one script that every sender shares, and reports failures to Sentry per the cron rule. PAM stays a voice surface that can read the list aloud later.

## Not verified

- No key was sent, so arrow/number/Enter behaviour inside the dialog, the "Type something" option and multi-question tabs are untested (same gap as issue 11).
- Whether plain text typed by `agent send` into an open dialog selects an option (digits) or is ignored.
- That the 8790 tunnel is up and that the answered-rows data can be listed: issue 19 says no "list open rows" route exists.
- Codex and pi hook surfaces for the same deny rule.

## New questions

1. The `decisions` row needs `pane_id`, `session_id`, the question's index inside a multi-question dialog, and a `delivered` / `not delivered` state with a route to set it. Does this go into the field list from issue 16?
2. How fast must a tap reach the pane? A 60 s launchd interval is the existing pattern; 5-10 s or a long-poll costs more wake-ups.
3. Should `attention-state.sh` also map PostToolUse AskUserQuestion to `working`, so the Command Center dot stops showing "waiting on you" after an answer? It is a small fix under problem #22, separate from the guard.
4. Is a hook deny on raw `herdr ... send` acceptable, given it also blocks an agent typing into its own freshly made pane until the skills are updated?
5. Ask the herdr author for an atomic "send only if status is X" flag? It is the only way to close race 1.
