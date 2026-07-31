# Thrivbe Voice as OS Orchestrator — analysis + proposal

Written 2026-07-31. Goal: make the voice agent the main contact point and orchestrator
for ALL system interactions — herdr, thrivbe-os kernel, Bloom, Hermes Tree (Thrivbe-3),
and the service tools — without collapsing the approval gates that already work.

Everything marked **VERIFIED** was checked live on 2026-07-31, not inferred from code.

> A note on proof-of-loop: this very document was produced by a `claude` agent running
> in the herdr lane `voice-voice-orchestrator-analysis` inside the `voice` workspace
> (w14), dispatched through the voice agent's own `delegate` path, under the same
> `VERIFY_HARNESS`/`ORCHESTRATOR_HARNESS` wrapper `tools.py` applies to every
> delegated task. The Mac-local delegation loop works end-to-end at real depth.

---

## 1. Current-state map

### 1.1 The three delegation paths (plus the one that doesn't exist)

| Path | Tool(s) | Where it runs | Mode | Feedback loop to voice | Gating |
|---|---|---|---|---|---|
| **Local coding/research** | `delegate`, `delegate_status`, `continue_task`, `close_finished_tasks` | herdr lane in the `voice` workspace on this Mac (`pi` or `claude`); headless `nohup` fallback when herdr is down | Fire-and-forget with callback | **Closed loop**: agent writes `.out` + touches `.done` sentinel → menubar poller (`_check_tasks`, 2s) auto-wakes a live session and speaks the result. Follow-ups via `continue_task`. | Autonomous (gated only by the `agentic_shell` config toggle; `_own_pane` chokepoint prevents touching foreign lanes) |
| **Business/OS work** | `os_delegate` | thrivbe-os worker on Thrivbe-1 (`POST /delegate` → `startDelegation`) | Fire-and-forget, **no callback** | **Open loop**: kernel replies `202 {runId}` — and `kernel_tools.os_delegate` throws the runId away. Results reach Robin via Telegram approval/summary, never via voice. No status tool, no follow-up tool. | Downstream actions gated by kernel approvals (NOK 50/day budget, `requestApproval`) |
| **Synchronous kernel reads/writes** | 17 `kernel_*`/`bloom_*`/`twenty_*`/`semsearch`/`hybrid_rag`/`cognee`/`graph_*`/`list_inbox_items` tools | Kernel voice-api on Thrivbe-1 via launchd SSH tunnel `127.0.0.1:8790` | Synchronous REST | Immediate spoken result | Reads autonomous. `kernel_decide` = deterministic stage+affirm spoken gate (`_pending_confirmation_outcome`, 120s TTL, regex yes/no incl. ja/nei). Bloom writes file kernel approvals. |
| **Hermes Tree (Thrivbe-3)** | — none — | 10-profile agent fleet coordinated via `/root/.hermes/kanban.db` | n/a | n/a | n/a — voice has zero visibility or reach into this fleet |

Local service tools (Notion ×4, Front ×2, Gmail ×2, Calendar add, Drive search) run
in-process or shell out to workspace scripts. Reads are autonomous ("don't ask
permission for ordinary reads" is already in `LIVE_SYSTEM`); `gmail_send` uses the same
stage+affirm gate as `kernel_decide`; `front_draft` never sends by convention.

### 1.2 Verified live state

- **VERIFIED** kernel tunnel up: ssh listening on 127.0.0.1:8790 (pid 26809, launchd `hetzner-tunnels`).
- **VERIFIED** herdr server running (v0.7.1), two workspaces: `~` (w2) and `voice` (w14, 3 panes, 2 active claude lanes).
- **VERIFIED** `check_tool_drift.py` run against the live manifest: **no drift**, no kernel-backed tools missing from the agent. Manifest version 1, 18 kernel tools, all mirrored in the agent's 37-tool `TOOLS` list.
- **VERIFIED** `check_tool_drift.py` has **no automation on the Mac** (voice-agent's own copy): not in crontab, no LaunchAgent, no hook references it.
- **CORRECTION**: the Mac finding above is not the whole picture. Thrivbe-1 runs a *second*, separately-maintained voice surface — `/opt/voice-bridge` (`voice-bridge.service`, port 8765, the phone-facing PWA bridge — see §6) — which has **its own copy** of `check_tool_drift.py` **and** a systemd `tool-drift-check.timer` (`OnCalendar=Mon 08:00`, `Persistent=true`) that pings Telegram on divergence. So the kernel manifest IS guarded on a weekly cadence today — just not for the Mac app, and via a second hand-maintained copy of the same checker script rather than a shared one. P3 (manifest sync) should target *both* copies, or better, delete the duplication per §6 below.
- **VERIFIED** Hermes kanban.db on Thrivbe-3: `tasks`/`task_runs`/`task_events`/`task_comments`/`kanban_notify_subs` tables; currently 121 done + 61 archived, 0 active.
- **VERIFIED** voice-api route inventory (930 lines, plain `if pathname ===` dispatch, no framework): GET `/health` `/status` `/tools` `/persona` `/inbox` `/graph-node` `/graph-doc` `/bloom-projects` `/bloom-tasks` `/recall` `/twenty-search`; POST `/decide` `/memo` `/remember` `/session-log` `/bloom-write` `/delegate` `/chat` `/semsearch` `/ai-search` `/cognee-ask`. Network-guarded (loopback + Tailscale CGNAT only) before bearer auth.

### 1.3 What's already better than expected

Two things the "make it autonomous" framing tends to miss:

1. **Read-side autonomy already exists** for email, Drive, Notion, CRM, inbox, memory,
   and knowledge search. The agent is instructed not to ask permission for reads. The
   only "I can't check that" left in the daily loop is the **calendar** (see G4).
2. **The confirmation architecture is deterministic, not model-judged.** Staged
   `gmail_send`/`kernel_decide` are resolved by transcript regex against a TTL'd pending
   record — the model can't talk itself into executing. This is the pattern every new
   write path should reuse, and a real product differentiator. Don't weaken it.

---

## 2. Gap analysis

Named and specific. G# referenced by the proposals in §3.

**G1 — os_delegate is a one-way door.** `POST /delegate` returns a `runId`;
`kernel_tools.os_delegate` discards it and returns a canned sentence. There is no
"what happened to the CRM update I delegated an hour ago" tool — `kernel_status` shows
only the top-3 recent runs of *everything*, unlabeled. The kernel even has a follow-up
route (`POST /chat` — threaded delegation follow-ups keyed by task id, built for
command-center) that voice doesn't expose. Contrast: herdr lanes have status, follow-up,
close, AND auto-wake. The OS path has none of the four. This is the single biggest
asymmetry between "tool-calling client" and "orchestrator."

**G2 — voice sees one herdr workspace out of N.** `_voice_lanes()` filters to
`voice-` prefixed lanes; `delegate_status` therefore answers "what's running?" with
only its own children. herdr's actual surface (**VERIFIED** in the skill + live
`workspace list`) includes multi-workspace listing, `agent explain <t>` (built-in
"what is this agent doing" summary), `agent read`, and the standing `orchestrator`
lane. Robin cannot ask by voice "what's running in herdr right now" and get the fleet
answer. Note the ownership chokepoint (`_own_pane`) is a *write* guard — read-only
fleet visibility violates nothing.

**G3 — Hermes Tree is invisible.** No tool checks profile status, kanban task state,
or task history on Thrivbe-3, and no delegation path exists into that fleet. The A2A
proposal (`.tmp/hermes-a2a-protocol-proposal.md`) would formalize exactly the interface
voice needs (AgentCard discovery + Task/TaskState lifecycle), but it's a draft awaiting
the shop's own review — nothing to integrate against yet.

**G4 — calendar is write-only.** `calendar_add` exists; there is no calendar *read*
tool, and (**VERIFIED**) no read script to wrap — `skills/google-workspace/scripts/`
has `calendar_event.py` (create) but no list/agenda script. "What's on my calendar
today?" is currently impossible except via `run_shell` improvisation. This — not
approval gates — is what's actually blocking the "check calendar autonomously"
preference. Gmail/Drive/Notion read autonomy is already met.

**G5 — the agent never initiates.** Auto-wake exists solely for local `.done`
sentinels. `kernel_attention_brief` is injected once at session *start* and explicitly
told to stay silent ("background only"). The kernel's attention-router aggregates a
prioritized "Needs Robin" feed and the notifications table feeds CC — but nothing can
reach Robin *by voice* between sessions. An urgent approval filed at 14:00 waits until
Robin happens to double-tap Control.

**G6 — drift protection is manual and partially blind.** Beyond not being automated
(§1.2), `check_tool_drift.py` has two defects: (a) `LOCAL_NAMES` misclassifies
`os_delegate` as agent-local when it's kernel-backed (harmless today — the intersection
check still covers it — but the "expected local" report lies); (b) agent tools that are
neither kernel-backed nor in `LOCAL_NAMES` (`notion_list_tasks`, `notion_update_task`)
are silently unclassified — a new local tool never surfaces in any report, so the
checker can't catch its own staleness.

**G7 — the tool list is a hand-maintained mirror at 37 entries and growing.** Every
kernel tool added means hand-copying schema into `tools.py` + a dispatch arm in
`live_session._do_tool` + a `LOCAL_NAMES` decision. The manifest (`GET /tools`) already
carries name/description/parameters per tool — the agent just doesn't use it as a
source. Each proposal below adds tools; without G7 fixed, G6 gets worse with every one.

**G8 — every deep integration is Robin-hardcoded** (kernel tunnel port, herdr binary
path, workspace scripts, ssh aliases), while PRODUCT.md commits to "no hardcoded user
paths." kernel/herdr tools currently degrade gracefully when absent (good), but there's
no declared config surface saying *which* integrations exist for *this* user.

---

## 3. Enhancement proposals

Ordered cheap-now → real project. Each: what changes, gap closed, safety model, effort.

### P1 — Close the os_delegate loop (G1) — do first

**What:**
1. `kernel_tools.os_delegate` keeps the `runId` from the 202 response and writes an
   `<tid>.osrun` sidecar in `TASKS_DIR` (mirroring the `.lane` pattern).
2. New kernel route `GET /runs?ids=…` (or extend `/status` with a `?runIds=` filter)
   returning status + report for named runs. ~30 lines in voice-api.ts, read-only,
   deployed via `/opt/thrivbe-ops/deploy.sh` — no Mac-side kernel processes, single-writer
   untouched.
3. The existing menubar poller checks `.osrun` sidecars on the same 2s tick (throttled
   to e.g. 60s for the network call) → when a run completes, same idle-only auto-wake
   that speaks herdr results. Same "don't barge in mid-conversation" rule.
4. Extend `delegate_status` to merge OS runs into its answer ("two local tasks working;
   the CRM update you sent to the OS completed 20 minutes ago").
5. Expose the kernel's existing `POST /chat` as an os-task follow-up (the `continue_task`
   analog), keyed off the stored run's task id where present.

**Safety:** all reads; follow-up delegation inherits the kernel's own approval + budget
path. No new gates needed.
**Effort:** small — one kernel route + ~80 lines Python. Highest orchestration value
per line in this document.

### P2 — Calendar read tool (G4) — cheap, closes a standing preference

**What:** write `skills/google-workspace/scripts/list_events.py` (date-range agenda,
same auth as `calendar_event.py`), wrap it as `calendar_list` in `services.py` +
`tools.py` (agent-local, add to `LOCAL_NAMES`). Optionally have `_build_live_instructions`
inject today's agenda as a session-start block the same way `kernel_attention_brief` is
injected — that makes "you have three meetings today" available with zero tool latency.
**Safety:** read-only, autonomous — matches gmail_search's existing tier. `calendar_add`
stays as-is (creating events with attendees emails people; if anything, consider moving
attendee-bearing invites behind the stage+affirm gate — flag for Robin, not decided here).
**Effort:** an afternoon.

### P3 — Manifest-driven kernel tools (G6+G7) — the structural drift fix

**What:** at session open (`LiveSession._configure`), fetch `GET /tools` (2s timeout,
cache to disk, fall back to the static list when the tunnel is down) and *generate* the
kernel-backed portion of the Realtime tool list from the manifest. Dispatch generically:
any manifest tool routes through one `_kernel_manifest_call(name, args)` that looks up
`{method, path}` — deleting ~17 hand-written dispatch arms in `_do_tool` and ~17
hand-copied schemas in `tools.py`. `highStakes: true` entries (today: `kernel_decide`)
are refused generic dispatch and must keep bespoke handlers with the stage+affirm gate —
fail closed: a new highStakes manifest tool without a local handler is *dropped*, not
auto-wired.

Meanwhile (one commit, today): fix `LOCAL_NAMES` (remove `os_delegate`, add
`notion_list_tasks`, `notion_update_task`), make unclassified tools a hard failure, and
add a weekly LaunchAgent running `check_tool_drift.py` — **failure pings Telegram
(@kff26_bot), success stays silent** per the cron rule (the Mac can't reach the kernel
notifications DB directly; silent-on-success is the sanctioned fallback).

**Safety:** unchanged for reads; the highStakes carve-out is the load-bearing rule.
**Effort:** the quick fixes are an hour; the manifest-driven refactor is 1–2 days and
should land *before* the tool list grows further. After it lands, new kernel
capabilities become voice-available by editing `tool-manifest.ts` alone — the kernel
becomes the single source of truth it was already designed to be.
**Product note:** genuinely generic — "connect a tool manifest URL" is a sellable
concept; Robin's manifest URL just becomes a config value.

### P4 — herdr fleet visibility (G2)

**What:** one read-only tool, `herdr_fleet_status` (no args, or optional
`workspace`/`task_name`): merges `workspace list` + `agent list` across ALL workspaces,
names each lane's workspace, agent type, and status, and for a named lane calls
`agent explain` for the one-line "what is it doing" summary. Answers "what's running in
herdr right now" fleet-wide, including the standing `orchestrator` lane and lanes other
sessions spawned.
**Safety:** strictly read-only — `_own_pane` continues to gate every mutating call, so
voice still cannot send-to or close anything outside its own lanes. That boundary is
correct and should NOT move: the herdr etiquette rules (shared session, Robin's panes,
Hermes remote lanes) exist precisely because multiple actors share this server.
**Optional second step:** `delegate` gains an optional `workspace` arg so Robin can say
"start it in the bloom workspace" — still creating voice-prefixed, sidecar-tracked lanes
so ownership semantics are preserved wherever the lane lives.
**Effort:** small; all primitives exist in `_herdr()`.

### P5 — Hermes Tree: visibility now, delegation only behind A2A (G3)

**What now (cheap, read-only):** `hermes_status` tool — `ssh thrivbe-3` running a
read-only sqlite query against kanban.db (active/blocked tasks by assignee, last
N completions). Purely observational; no locks touched, no writes, safe against the
dispatcher's `.dispatch.lock` discipline. Speakable answer: "the shop fleet is idle;
last completed task was X, two hours ago."
**What later (delegation):** only once the A2A proposal lands its step 2 ("wrap kanban
as A2A tasks"), voice becomes an ordinary A2A *client*: submit → poll TaskState → same
`.osrun`-style sidecar + idle-only auto-wake as P1. The AgentCard/TaskState lifecycle
gives voice exactly the status/feedback loop that G1 shows matters, without voice
learning kanban's internals.
**Explicitly NOT proposed:** voice writing rows into kanban.db, by ssh or otherwise.
The `force-kanban` hook exists to keep ALL delegation observable through one
choke-point; a second writer path from the Mac would bypass the dispatcher's lock-file
coordination and recreate the exact freeform-delegation problem the hook blocks. If A2A
stalls, the fallback is a tiny HTTP intake endpoint *on Thrivbe-3, owned by the shop*,
that files kanban tasks through the shop's own code path — never direct DB writes from
outside.
**Safety:** read-only now; delegation inherits the shop's human-approval rules (the
CARP guardrails the A2A doc commits to preserving).
**Effort:** status tool ~half a day; A2A client is a real project gated on someone
else's decision — sequence it last.

### P6 — Proactive contact: urgent-only wake (G5)

**What:** extend the existing tunnel-probe/tasks-poller pattern in `agent.py` with a
third poll (60s, background thread, results applied on `_tick`): `GET /status`,
filtered to items that qualify as *urgent* under the existing routine/urgent approval
split. On a NEW urgent item while the app is idle → the established `_wake_and_speak`
path: "There's an urgent approval waiting: <preview>. Approve, reject, or ignore?" —
which lands directly in the already-built `kernel_decide` stage+affirm gate.

Guardrails, all load-bearing:
- **Idle-only** — never barge into a live conversation (same rule `_check_tasks`
  already enforces; ponytail says "idle-only auto-wake, no queueing" — keep it).
- **Urgent tier only, once per item** (announced-set, like `_announced`), with a
  config toggle (`live.proactive_wake`, default OFF) and quiet hours.
- **Client-side polling only.** This is a kernel *client* on the Mac, not a kernel
  process — the single-writer/single-poller rule on Thrivbe-1 is untouched.
- Routine approvals and info notifications stay in Telegram/CC where they were
  deliberately routed (2026-07-17 noise decision). Voice-wake is a *third, narrower*
  channel, not a replacement — otherwise we recreate the Telegram noise problem in
  audio, which is worse.

**Optional companion:** a `list_notifications` read tool needs a small kernel
`GET /notifications` route (the table exists; no route does — verified against the
route inventory). Pull-only: "anything from the fleet today?"
**Effort:** poller ~a day; notifications route ~30 lines kernel-side.

### P7 — Integrations config surface (G8)

**What:** a `config.json` `integrations` block naming what exists on this machine:

```jsonc
"integrations": {
  "kernel":  { "url": "http://127.0.0.1:8790", "manifest": true },
  "herdr":   { "bin": "~/.local/bin/herdr" },
  "scripts": { "google": "~/Thrivbe-AI/skills/google-workspace/scripts",
               "front":  "~/Thrivbe-AI/skills/front/scripts" },
  "hermes":  { "ssh_host": "thrivbe-3", "kanban": "/root/.hermes/kanban.db" }
}
```

Absent block → tool absent from the session's tool list (the schema itself, not just a
runtime "unreachable" reply — fewer tools in the Realtime prompt is also a token/
misrouting win). This is the mechanism that keeps every Robin-specific proposal above
(P1 kernel, P4 herdr, P5 hermes ssh) compatible with the PRODUCT.md direction: the
product ships with the block empty; Robin's dev config fills it. `services.py`'s
hardcoded `WORKSPACE`/script paths fold into it, finishing the §2.1 de-hardcoding item
PRODUCT.md lists as done-except-services.
**Effort:** 1–2 days, mostly mechanical; do it alongside whichever of P1/P4/P5 lands
first so the pattern is set early.

### Sequencing

| Order | Item | Size | Depends on |
|---|---|---|---|
| 1 | P3 quick part: fix LOCAL_NAMES + unclassified-fails + weekly LaunchAgent w/ Telegram-on-failure | hours | — |
| 2 | P2 calendar read | half day | — |
| 3 | P1 os_delegate loop (runId sidecar + `/runs` route + auto-wake + status merge) | 2–3 days | — |
| 4 | P4 herdr fleet status | half day | — |
| 5 | P5a hermes_status (read-only) | half day | — |
| 6 | P7 integrations config | 1–2 days | with 3–5 |
| 7 | P3 full: manifest-driven kernel tools | 1–2 days | best before more kernel tools land |
| 8 | P6 urgent-only proactive wake (+ /notifications route) | 1–2 days | 3 (reuses run-wake plumbing) |
| 9 | P5b Hermes A2A client | project | Hermes A2A proposal step 2 shipping |

---

## 4. Non-goals and advise-against

**Do not weaken the gates to feel "more autonomous."** The friction Robin experiences
is almost entirely *missing read tools and missing feedback loops* (G1, G4), not the
confirmation gates. Every gap above closes without touching `kernel_decide`'s spoken
yes/no, `gmail_send`'s staging, Bloom's approval filing, or `front_draft`'s never-send
convention. If gated flows still feel slow after P1–P4, tune the kernel's standing-trust
tiers (the `bloom-write` path already auto-executes when standing trust exists —
"Already executed — Robin has standing trust for this action") — that's the designed
dial for earned autonomy, and it lives kernel-side where it's audited.

**Do not give voice mutating access to herdr lanes it doesn't own.** The `_own_pane`
chokepoint plus the shared-session etiquette is what makes fleet-wide *read* access
safe to add at all.

**Do not write to Hermes kanban.db from the Mac.** Covered in P5 — it bypasses the
dispatcher locks and the force-kanban discipline, and it would be the second bespoke
integration into a system that's about to get a standard interface.

**Do not run kernel logic on the Mac.** All proposals here are HTTP/ssh clients of
Thrivbe-1/Thrivbe-3. The single-SQLite-writer, single-Telegram-poller invariant stays.

**Do not make proactive voice chatty.** P6 is deliberately urgent-only, off-by-default,
once-per-item. The 2026-07-17 decision moved success/info OUT of Robin's synchronous
channels; an eager voice assistant announcing routine completions would be a regression
of that decision with higher interruption cost than Telegram ever had.

**Deferred, deliberately:** exposing `run_shell`-over-ssh to servers (voice already
reaches them via delegate/os_delegate with harnesses and gates; a raw remote shell adds
risk with no orchestration value), and any Realtime tool-count optimization beyond P7's
conditional inclusion — measure misrouting first before inventing a router.

---

## 5. What "main contact point" looks like when this lands

Robin, in one live session, can then: hear what needs attention across kernel approvals,
inbox, calendar, and fleet; ask "what's running?" and get herdr (all workspaces) + OS
delegation runs + Hermes shop state in one spoken answer; delegate to the right lane —
Mac coding (herdr), business ops (kernel worker), shop work (A2A, later) — with every
path reporting back by voice when done; and approve/decide anything pending through one
deterministic spoken gate. The voice agent stops being a client of one system with a
side-door to another, and becomes the one place all four report to — while every write
still passes the gate it always did.

---

## 6. Addendum (2026-07-31, same session): voice-bridge vs. Thrivbe Voice — replace or converge?

Robin asked whether Thrivbe Voice (this Mac app) should replace `voice-bridge` on
Thrivbe-1. Checked live (`ssh hetzner`, `systemctl`, read `server.py` in full):

**They are not the same product — different physical surface, not competing
implementations.** `voice-bridge.service` (`/opt/voice-bridge/server.py`, 1911 lines,
FastAPI on :8765) is a **phone-facing PWA**: records audio on Robin's Android phone,
transcribes with Whisper, calls the LLM over OmniRoute directly (`:20128`,
`auto/best-fast` — not through `voice_router_mvp.py`/FreeLLMAPI, which looks like dead
code left over from the 2026-07-30 OmniRoute cutover and is worth a separate cleanup
pass), synthesizes with ElevenLabs, and — critically — **SSHes into the phone itself**
(`u0_a634@100.127.4.13:8022`, Termux) to make calls, read SMS/contacts/notifications.
None of that is reachable from a Mac menubar app: Thrivbe Voice needs Robin physically
at his Mac (push-to-talk hotkey, Accessibility API, local shell). voice-bridge needs
only a phone and a browser. Replacing one with the other would delete a capability
(mobile access + Android control) neither can absorb into the other's runtime.

**What genuinely should converge — the two surfaces have drifted into hand-duplicated
logic that talks to the same kernel:**
- Both have their own `check_tool_drift.py` (Mac vs. `/opt/voice-bridge/`) — one file,
  or the P3 auto-sync, should serve both instead of two copies going stale independently.
- `server.py` re-implements its own `HIGH_STAKES = {kernel_decide, send_sms,
  android_make_call}` set, its own `AFFIRM_RE`/`DENY_RE` yes/no confirmation regex, and
  its own inlined copy of the verify harness (`OS_DELEGATE_VERIFY_HARNESS`, a near-exact
  duplicate of `tools.py`'s `VERIFY_HARNESS` — same content, hand-copied, will drift the
  first time either gets edited without the other). Same problem as §6's harness
  question below, at the kernel-facing-tool layer instead of the delegation layer.
- Both are independent clients of the same `kernel_tools` surface (:8790) with separately
  maintained tool lists — a second thing P3's manifest-sync should cover in one shot
  rather than fixing the Mac side only.

**Recommendation:** don't replace; extract the shared logic (harness text, high-stakes
set, confirm/deny detection, kernel tool-manifest sync) into one place both import or
read from, and let the two apps stay separate front-ends (desktop-agentic vs.
mobile-PWA) over that shared core. This is the same pattern as the skill-extraction
below — worth doing both in the same pass since they're the same root cause
(hand-duplicated instructional/config text across two Python codebases that both talk
to thrivbe-os).

## 7. Addendum: extract the delegation harness into an editable file instead of inlining it

Robin's observation: every `delegate`/`os_delegate` call currently gets `VERIFY_HARNESS`
(~250 words) and, in `claude` mode, `ORCHESTRATOR_HARNESS` (~60 words) **inlined in
full** into the prompt from Python string constants in `tools.py` (lines 385–414) —
and, per §6, `server.py` hand-copies its own near-duplicate. Iterating on the harness
today means editing Python string literals in two files and re-deploying both. Robin's
proposed fix — pull the harness into one general, editable file the prompt *points to*
instead of inlines — is correct and should happen.

**Design:** a plain markdown file (not necessarily needing Claude Code's skill YAML
frontmatter, since `pi`-mode delegation must read it too and `pi` has no skill-loading
convention) at a fixed path — e.g. `~/.claude/skills/voice-delegate-harness/SKILL.md`,
which is *also* a valid interactive skill for on-demand use, but is really just a
markdown file any agent can `cat`/Read. Both `_verify_wrap` and `_orchestrator_wrap` in
`tools.py`, and the equivalent in `server.py`, collapse from "paste ~300 words" to one
line: `"Read <path> and follow it exactly before starting."` The per-call dynamic bits
that genuinely can't live in a static file — `_completion_signal`'s `out`/`done` file
paths, which differ every task — stay inlined as data, appended after the pointer line.

**Why this is worth doing, concretely, not just for editability:**
- **Token cost**: every delegated call currently pays for the full harness text in the
  prompt; a one-line pointer plus the delegate reading a cached/short file is cheaper,
  especially at herdr-lane scale where multiple lanes may run concurrently.
- **Single source of truth**: fixes the exact duplication problem found in §6
  (`server.py`'s `OS_DELEGATE_VERIFY_HARNESS` hand-copy) for free — both surfaces point
  at the same file instead of each carrying their own text.
- **Mode-agnostic**: "read this file and follow it" works identically for `claude` and
  `pi` delegates (no dependency on Claude Code's Skill-tool trigger-matching, which
  would only work reliably for `claude` mode and adds indirection where a direct Read
  is simpler and deterministic).

**Trade-off to flag:** an extra Read round-trip before the delegate starts real work
(negligible latency vs. the multi-minute lanes this gates) and — if the file doesn't
exist yet the first time (e.g. `pi`/`claude` binary not yet updated, path typo) — the
delegate silently proceeds without the harness. Mitigate by having `_verify_wrap` fail
loud in Python if the file is missing at prompt-build time (check `os.path.exists`
before emitting the pointer) rather than only discovering it at runtime inside the
delegate's own session.

**Not implemented in this pass** — this is Robin's idea being confirmed as sound, not
executed. Next step if he wants it built: extract the harness text verbatim to the new
file, point `tools.py` and `server.py` at it, delete the inline duplicates, and update
`check_tool_drift.py`'s `if __name__` self-check in `tools.py` (asserts on
`VERIFY_HARNESS` being a prefix of the written prompt) to assert on the pointer line
instead.
