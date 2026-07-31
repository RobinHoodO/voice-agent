# Pam: full herdr pane awareness, reuse, and control by voice

## Context

Pam can only see and touch herdr panes **she created herself**. The guard is
`_own_pane()` (`tools.py:540`), which requires a pane to be BOTH named `voice-*` AND
have a `.lane` sidecar Pam wrote. Everything else — Robin's own shells, an orchestrator
lane, a pane a previous session left behind — is invisible and untouchable. So she
cannot reuse a running claude process, cannot report what's actually going on, and
`delegate` always spawns yet another pane (say "fix routing" twice and you get
`voice-fix-routing` and `voice-fix-routing-2`).

**The walls are entirely in Pam's own code.** herdr already exposes everything needed:
`pane list` spans all workspaces and includes plain shells, `pane process-info` gives
full argv, `agent read` works on any pane (verified against a bare shell), and
`agent rename` means a foreign pane can be *adopted*.

Outcome: Pam tracks every pane on the Mac, prefers continuing the pane already on-topic,
can recycle an idle pane for new work, and can close things — governed by the herdr
skill she reads and extends, with a small hard floor kept in code.

## Decisions taken (Robin, 2026-07-31)

| Decision | Choice |
|---|---|
| Where the rules live | **The herdr skill, not hardcoded Python.** Pam reads `~/.claude/skills/herdr/SKILL.md` and appends to a `## Learned by Pam` section as she learns |
| Default behaviour | **Find and continue the pane relevant to the task.** Spawn new only when nothing matches |
| Reusing a pane for an *unrelated* task | **`/clear` first**, then rename, then send |
| Voice addressing | Spoken lane names; unnamed shells get auto-labels ("shell one in home"). Never pane ids |
| Adjacent defects | Fix both: results dropped mid-conversation, and leaking sidecar files |

## Verified ground truth

- `pane list` / `agent list` / `workspace list` are **global across workspaces**. Plain
  shells appear in `pane list` with no `agent` key and `agent_status:"unknown"`.
- `agent read <pane>` returns JSON and **works on a bare shell** (tested live on `w2:pV`).
  `pane read` prints RAW TEXT, not JSON — a footgun; always use `agent read`.
- `agent_status` ∈ `idle|working|blocked|done|unknown`, **screen-scraped**. `idle` means
  "sitting at its prompt", NOT "finished", and no installed manifest ever emits `done`
  for claude. **The `.done` sentinel file remains the only completion signal.**
- `agent rename <target> <name>` exists → adoption is possible.
- claude's `/clear` genuinely resets context; **pi's `/clear` is fake** (needs pid kill +
  relaunch).
- Live now: 6 panes — `w2:pV`, `w2:pY`, `w14:p1` bare shells; `w14:p9`, `w14:p8`,
  `w14:pC` claude lanes named `voice-*`. Workspaces `w2` ("~") and `w14` ("voice").
- **94 leaked files** already in `~/Library/Application Support/ThrivbeVoice/tasks/`,
  oldest 2026-06-25. Nothing ever deletes them.

---

## 1. The gate: provenance, not scraped status

My first instinct was "confirm before touching a pane that is `working`." **That is
unsound and I'm dropping it.** `agent_status` is screen-scraped, and its worst failure
is a stale `idle` on a pane that is genuinely working — which would sail straight
through such a gate and destroy live work. A trip-wire whose false-negative is exactly
the catastrophe it guards against is not a floor.

**Provenance is deterministic** and already in the codebase: a pane either has a `.lane`
sidecar Pam wrote, or it doesn't. No scraping, no staleness.

| Action | Pane Pam created or adopted | Any other pane |
|---|---|---|
| list / read | free | free |
| send text | free | free — herdr *queues* text for a busy agent, the process is never destroyed, and it's visible in the pane. Recoverable. Logged to the activity feed |
| close | free if `.done` exists; else Robin must name it | **spoken confirmation, always** |

Scraped status still appears in the confirmation preview ("currently working") as
*decoration* — if it's wrong there, it costs nothing.

**Hard floor — code-enforced, cannot be talked out of even by a confirmed yes:**

1. **Destructive-globals denylist at the `_herdr()` seam** (`tools.py:474`) — one ~5-line
   predicate refusing `server stop`, `session stop`, `update`, and any argv containing
   `--takeover`. Every herdr call in the codebase funnels through `_herdr`, so placing it
   there means no future tool, no prompt injection, and no confirmed action can ever emit
   these.
2. **The `orchestrator` agent is untouchable** — close refuses it even when confirmed;
   `/exit`-style text is refused too. Matches the skill's PROTECTED section, mechanically.
3. **Foreign close requires the spoken gate** (table above).
4. **Skill self-edits are append-only by convention** — `>>` only, into the last
   section of the file. *(Post-review honesty note, 2026-07-31: unlike floors 1–3
   this has no code chokepoint — the append runs through `run_shell`, so it is
   doctrine the model is instructed to follow, not a mechanical guarantee. Adding
   a dedicated append tool would break the 38-tool ceiling in §2; accepted as-is.)*

Everything else — which pane to pick, reuse vs spawn, `--split right`/`--no-focus`, the
Enter-after-paste gotcha, `/clear` semantics, supervision cadence — is **soft doctrine in
the skill**, which Pam reads and iterates. That is Robin's decision, honoured: doctrine in
the skill, four invariants in Python.

## 2. Tool surface — stays at 38 tools, zero added

Pam already ships 38 tools / ~15.8k chars per session, with three delegation-ish verbs
competing for "do this task" utterances. Adding a `herdr_*` family would be a real
regression, so the four existing herdr verbs are **reshaped, not multiplied**. Anything
exotic (workspace rename, `agent explain`, notifications) goes through the existing
`run_shell`, guided by the skill.

| Tool | Change |
|---|---|
| `delegate_status` → **`fleet`** | Now the full inventory: every pane, every workspace, agent lanes *and* bare shells. Optional `pane` arg zooms in — `agent read --source recent` + `process-info` — answering "what is that pane actually doing". Absorbs what would otherwise be a separate read tool |
| **`delegate`** | Gains optional `reuse_pane`. Unset → spawn new (today's path). Set → recycle that pane for a NEW task: `/clear` → adopt/rename → harness + fresh sentinel |
| **`continue_task`** | Widened to resolve **any** pane by spoken name, adopting foreign ones on first touch. Keeps its mint-a-new-tid trick so auto-wake refires |
| **`close_finished_tasks`** | Widened to close any pane by name, through the provenance gate above |

**Why keep four verbs instead of one passthrough:** a tool that merely shells `herdr …`
**cannot mint a `.done` sentinel**, so any work started through it would run silently and
Robin would never hear the result — the worst failure mode in this system. `delegate` and
`continue_task` exist precisely to own the harness + sentinel + auto-wake plumbing. A
generic `herdr_run(argv)` would also move the close gate from a typed chokepoint into
argv string-parsing; one quoting variant and the floor is gone. **Advise against it.**

## 3. Adoption — the controlled door

Rather than loosening `_own_pane`, add a door into it. New `_adopt_pane(pane_id, name)`:
refuses `PROTECTED_AGENTS = {"orchestrator"}`, issues `agent rename <pane> voice-<slug>`,
writes a `.lane` sidecar. After adoption the pane satisfies the *existing, unchanged*
chokepoint, so status/close/continue all work on it for free.

The invariant shifts from "never touch foreign panes" to **"mutate only panes explicitly
created or adopted, and never the orchestrator"** — still mechanical, still auditable,
and adoption only ever fires at the moment Robin has just asked for work to go there.

## 4. The find-and-continue flow

Relevance is judged by the model (code can't do semantic matching); `fleet` gives it
names, states and workspaces, which is enough. Pam **says which pane she chose**:

1. Task utterance → `fleet()` if not already fresh this conversation.
2. **Related to an existing lane** → `continue_task` → *"continuing that in the routing
   fix lane."* Keeps context; no `/clear`.
3. **Unrelated, but a suitable idle lane or shell exists** → `delegate(…, reuse_pane=…)`
   → *"clearing the api docs lane and reusing it for X."*
4. **Nothing suitable** → `delegate(…)` → *"nothing matches — starting a new lane called X."*

**Reuse sequence:** refuse if protected or `working` (fall back to spawn, say so) →
branch on agent type: **claude** = `/clear` + Enter, poll to idle; **bare shell** =
nothing to clear; **pi = not reusable in v1** (fake `/clear`; spawn fresh and say so) →
`_adopt_pane` → mint tid + full wrapped prompt file → deliver. Delivery avoids the
multi-line Enter gotcha: a shell gets `pane run … "$(cat <promptfile>)"` (never typed);
a cleared claude lane gets a **one-line pointer** — *"Read `<promptfile>` and execute it
exactly"* — with the file carrying harness and sentinel.

**Every path mints its sentinel in code.** No route starts work that can't wake the menubar.

## 5. Auto-labels and the spoken inventory

Join `workspace list` + `agent list` + `pane list` + sidecars. Bare shells get
`"shell {n} in {workspace label}"`, n = 1-based position among that workspace's shells
sorted by pane_id (stable for a pane's lifetime; the moment Pam actually uses one she
adopts it, freezing a real name). Label computation is shared by `fleet` and the
resolver, so what she says is always what she can target. Cap at ~6 sentences: beyond 8
panes, name only non-idle agents and finished lanes, summarise the rest as counts.

> "Two workspaces. In voice: routing fix is working, api docs is finished, cleanup is
> paused waiting for input. In home: shell one and shell two are sitting at a prompt."

## 6. How the skill is loaded and extended

- **One-time:** append to `~/.claude/skills/herdr/SKILL.md`:
  `## Learned by Pam` + an append-only HTML comment, kept as the **last** section.
- **Load:** `_build_live_instructions` injects a compact doctrine digest (the PROTECTED
  + etiquette essentials) plus the `## Learned by Pam` tail, capped ~1.5k chars, with a
  pointer to read the full skill via `run_shell` before non-routine moves. *Not* the
  whole 216-line skill — inlining it every session would blow the very budget §2 protects.
- **Append:** `run_shell` with `printf -- '- %s (%s)\n' "<lesson>" "$(date +%F)" >> <skill>`.
  Append-only holds by construction: `>>` cannot rewrite, and her section is last.

## 7. The two defects

**Results dropped mid-conversation** (`agent.py:282-303`). Today the tid is added to
`_announced` *before* the `if self.live_on` check, so a task finishing while Robin talks
is marked announced and then skipped — **never spoken, even after the session ends**.

Fix: `LiveSession.offer_task(tid, text)` queues it (idempotent); drain in
`_inject_context_and_respond` **after** the context item and **before** the turn's own
`response.create` — so results land at a natural turn boundary with no mid-sentence
interruption and no extra response race. `_check_tasks` marks `_announced` **only after
the result was actually spoken**; anything undrained when the session closes is still
unannounced, so the normal wake path speaks it on the next 2s tick. Several finishing at
once drain together behind a one-line "N background tasks finished" preamble.

**Sidecars leak.** Two small mechanisms: (a) age-prune >7d at app launch, *before*
`_announced` seeding — the only safe moment (single-threaded, and every `.done` present at
launch is already condemned to silence by the seed, so the new queue can't want it); this
also kills a latent tid-recycling bug, since tids are `HHMMSS` with no date. (b) delete a
pane's `.lane` sidecars on successful close, severing them exactly when herdr may recycle
the id. Rule: **lane sidecars die with their pane; everything else dies at 7 days, at launch.**

## 8. File-by-file

| File | Change |
|---|---|
| `voice-agent/tools.py` | `_forbidden()` denylist inside `_herdr` (:474); `PROTECTED_AGENTS` beside `LANE_PREFIX` (:471); `_all_panes()`/`_workspaces()`/`_speakable_labels()` after `_voice_lanes` (:512); `_adopt_pane()` after `_own_pane` (**:540 unchanged**); extract `_mint_task()` from `_build_delegate_cmd` (:596); `reuse_pane` branch in `delegate_task` (:637); `_lane_state` tolerant of sidecar-less lanes (:705); `delegate_status`→`fleet` (:720); `_find_lane` over labels + substring fallback (:737); `continue_task` adopts foreign (:748); `close_finished_tasks` + `close_gate()` (:774); `.lane` delete on close; **self-check rewrite (:926-930)** |
| `voice-agent/live_session.py` | import `fleet`; `delegate` passes `reuse_pane`; `fleet` dispatch arm takes args; conditional close staging **above** the generic `elif name in self._high_stakes`, reusing `_pending_action`/`_resolve_pending_action`/`HIGH_STAKES_EXECUTORS`/`_confirmation_preview`; `offer_task` + drain in `_inject_context_and_respond` |
| `voice-agent/agent.py` | `_check_tasks` mark-after-spoken; launch age-prune before `_announced` seeding |
| `voice-agent/live_prompt.py` | Rewrite the DOING TASKS paragraph (:35) for find-and-continue + announce-the-pane + protected/denylist rules; inject doctrine digest + Learned tail in `_build_live_instructions` |
| `~/.claude/skills/herdr/SKILL.md` | Add the append-only `## Learned by Pam` section |

Note `close_finished_tasks` is Mac-local, so it needs **no** kernel manifest entry — and
must **not** go in `LOCAL_HIGH_STAKES` (the gate is conditional per-argument, not
per-tool), keeping `check_tool_drift` clean.

## 9. Verification

**Offline:** `.venv/bin/python tools.py` (fake herdr seam + tmp TASKS_DIR),
`check_tool_drift.py` green. Rewritten self-check must prove, with a negative test per
invariant: denylist refuses the four globals but passes `agent list`/`pane close`;
a **confirmed** close still refuses the orchestrator and issues no herdr call; an
unconfirmed foreign close issues **no** `pane close`; a foreign send now succeeds (the
relaxation, proven); own finished lanes stay ungated; closing deletes the `.lane`.

**Read-only live:** `herdr status --json`, `agent list`, `fleet()` output eyeballed for
≤6 sentences including the bare shells.

**Gate, on a throwaway only:** create `herdr workspace create --cwd /tmp --label pam-test
--no-focus` + a scratch shell lane; by voice "close the pam test shell" → expect preview +
confirmation demand → "yes" → gone. Negatives: "run herdr server stop" and "close the
orchestrator" must refuse, with `herdr status --json` proving the server never blinked.
**Never point tests at the live working voice lanes.**

**Queue:** mid-conversation, drop `<tid>.out` + `<tid>.done` into TASKS_DIR (app scratch,
not a herdr mutation); say any sentence → the result should be woven into the reply. Then
the regression case: drop a `.done`, end the session **without speaking** → it must be
spoken within ~4s by the wake path. Exactly one announcement per tid in the log.

**Reuse, end to end:** ask for a task, then an unrelated one naming the same lane;
confirm `/clear` ran, the lane was renamed, and the result still speaks itself.

## 10. Non-goals / advise against

- **No generic `herdr_run` passthrough** (§2) — it can't mint sentinels and it dissolves
  the close gate into argv parsing.
- **No pi-lane reuse in v1** — fake `/clear` needs pid-kill choreography for a rare case;
  spawn fresh and say so.
- **Foreign *send* stays ungated** by choice — it queues, is visible, and is
  countermandable. Mitigation is the activity log plus doctrine (read the pane, name the
  target aloud). If it ever bites, the conditional-gate pattern extends in ~5 lines;
  don't pre-build it.
- **Don't try to make `agent_status` reliable** — polling or double-reads just rebuilds
  the scraper's problem one layer up. Provenance sidesteps it.
- **Don't inline the full skill** into every session, and **never** trust `idle` as
  completion anywhere.
