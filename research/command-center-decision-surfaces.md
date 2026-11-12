# What Command Center and the kernel already do for things that wait on Robin

Ticket: RobinHoodO/voice-agent#37. Read-only research, 2026-11-12. Row counts are live from Thrivbe-1 (`fleet db`, SELECT only). Anything marked "not checked" was not verified.

Paths: OS = `/Users/robinsverd/Thrivbe-AI/Thrivbe-OS`. The live kernel is the separate repo checked out on Thrivbe-1 at `/opt/Thrivbe-AI/projects/thrivbe-os/src`. The kernel copy under `OS/apps/kernel` is older: it has no `/decisions/raise` route.

## Inventory

| Part | Where | Writes | Reads | In use? |
|---|---|---|---|---|
| `decisions` table | Live schema has 36 columns (task id, via, asked_count, idempotency_key, door, review_at...). The Mac repo `OS/apps/kernel/db/schema.sql:318-330` shows only 12. | `/decisions/raise` (hooks, import script), kernel processes (weekly-compass `createDecision`, content-cocreate) | Telegram `/decisions`, attention-router, approvals-poller expiry | Yes. 701 rows: 457 decided, 164 open, 80 stale. By source: linked:wayfinder 184, linked:notion 177, mac-hook 231 (155 open), weekly-compass 9, content-cocreate 5. 68 mac-hook rows opened in the last day. Oldest open row: 2026-10-14. |
| decisions HTTP routes | Live `src/voice-api.ts:1483-1536` (port 8790), handlers in live `src/decisions.ts`: `raiseDecision:319`, `handleDecisionRaise:417`, `handleDecisionAnswer:513`, `handleDecisionReopen:528`, `handleDecisionKeep:557`, `/decisions/search`, `/decisions/<id>/override` | Bearer `DECISIONS_API_TOKEN` | Hooks, import script | Yes. No "list open rows" route exists. Raise dedupes by question match and bumps `asked_count`. Answer refuses a stale or decided row with 409. Answer takes `via` (pick, override, swipe, batch, sent back) and `answered_by`. |
| decisions lifecycle | `OS/apps/kernel/src/decisions.ts`: `createDecision:70`, `decideDecision:87`, `autoDecideOverdue:116`, `listOpenDecisions:126` (live copy: lines 132, 211, 245, 249) | `approvals-poller.ts:709-713` auto-defaults rows past their deadline and posts to the notifications feed | `attention-router.ts:60-72` turns open rows into attention items with hint `/decide <id> <n>` | Yes. The `via` column shows 76 pick and 79 bulk-close-339. |
| `auto-decide.py` hook | `OS/global/claude/hooks/auto-decide.py` (events at 436-470; raise at 294, 350) | Raises rows with source `mac-hook`: PostToolUse on AskUserQuestion, Stop on "Decided:" lines (regex :54), PreToolUse re-ask gate (4.5 s timeout) | Kernel (`/decisions/raise`, then `/answer`) | Yes. It is the main writer. Never runs in client repos. |
| attention state files | `OS/global/claude/hooks/attention-state.sh` (states waiting-on-you, your-move, working, quiet) writes `~/.claude/attention/*`. A 60 s launchd job syncs them. CC reads them in `OS/apps/command-center/src/lib/attention-state.ts:21` and `/api/attention`. | Hook on PreToolUse(AskUserQuestion), PermissionRequest, Stop | CC dots strip on /agents, /api/brief | Per problem #22 row (`OS/bridge/problem-inventory.md:82`): live since 2026-08-02. Desk only, no phone. Live state-file count not checked. |
| kernel `attention` table | `OS/apps/kernel/db/schema.sql:78`. Filled by `attention-router.ts:548`. CC reads it at `src/app/api/os/attention/route.ts:55-60`, shown by `components/os/AttentionStrip.tsx` and `NeedsRobinPanel.tsx`. | attention-router (collects decisions, approvals, failed runs, stale work) | CC every 10 s | Not on the live db: `fleet db` finds no table named `attention`. So that CC route likely returns an empty list. Cause not checked. |
| `notifications` table and `ctx.notify` | `OS/apps/kernel/db/schema.sql:13-21` (id, created_at, source, level info/urgent, text, read_at). Live `src/notifications.ts:4-37`: `notify()` and `createNotifier(kernel, source)` insert one row. `supervisor.ts:14,74` hands `notify` to every process. | 93 call sites of `sendToRobin`/notify across the kernel (count from live grep, not split by type). Top sources: os-healer 2439, mem-guard 171, fireflies-watch 129, deploy 125, grown-shop 119, finance-autopilot 117, wrap 100. | CC `/api/notifications` (`src/app/api/notifications/route.ts:32`), `/api/notifications/read`, tab on `/agents` (`src/app/agents/page.tsx:212-216, 371`), `components/NotificationBell.tsx` | Yes. 4581 rows, 4244 unread. Nobody clears them, so unread count carries no signal. One-way: no answer path, no push. |
| `approvals` table | `OS/apps/kernel/db/schema.sql:62-75`. Code `OS/apps/kernel/src/approvals.ts`, poller `src/processes/approvals-poller.ts` (Telegram buttons, `/decisions` command at :569, web-decision sync at :1078). CC: `src/app/api/os/approvals/route.ts`, `src/app/api/approvals/[id]/decide/route.ts`, tab "Approvals" (default tab) in `src/app/agents/page.tsx:167,263,352-371`. | Kernel processes when an action needs consent (action_type, preview, idempotency key) | Telegram poller, CC | In use but low volume. 113 executed, 51 rejected, 33 expired, 4 approved, 2 pending. This is for kernel-proposed actions, not agent questions. |
| `incidents` table | `OS/apps/kernel/db/schema.sql:25-39`, `src/incidents.ts`; CC `src/app/api/os/incidents/route.ts`, `components/os/IncidentsPanel.tsx` | systemd, kernel, Sentry via `fileIncident()` | os-healer, CC tab Incidents | Yes. 1691 fixed, 123 gave_up, 38 needs_robin. Off-topic for the inbox except the `needs_robin` state. |
| CC decision/inbox UI | `OS/apps/command-center/src/components/DecisionQueue.tsx` (478 lines), used only by `src/app/inbox/_components/TodayTab.tsx`. It calls `/api/ask`, not the kernel `decisions` table (not fully checked). A swipe/batch UI sets `via` = swipe/batch on decisions (the code was not located). | | | The 164 open decision rows have no CC view that I could find. |
| push / service worker | CC `public/` has no service worker or manifest (only `system-graph-worker.js`). No `web-push` import in CC or kernel src. | | | Does not exist. The kit is `/Users/robinsverd/Thrivbe-AI/.tmp/web-push-kit/` (reference code, not wired). |
| PAM phone page | `/Users/robinsverd/Thrivbe-AI/projects/voice-agent/server/`: `app.py` (routes: `/`, `/manifest.json`, `/sw.js`, `/ping`, `/health`, websocket `/live`), `sw.js` (cache only, no push handler), `index.html`, `manifest.json` | | | Voice shell only. No decisions code, no push. |
| Telegram | `approvals-poller.ts` (`/decisions`, `/decide <id> <n>`, inline approval buttons) | | | Still the only phone path for approvals and decisions. Standing rule: no new Telegram pushes. |

## Overlaps with the PAM Inbox

1. The store is already there and already holds the agent questions. 155 of the 164 open rows are `mac-hook` rows from `auto-decide.py`. The inbox does not need a new table, only more fields (planned in #10 and #16).
2. The open list is full of old and low-value rows (oldest 2026-10-14). A phone list of "open rows" would show all 164 on day one. A cleanup or filter rule is needed before the first push.
3. Problem #22 attention dots already say "waiting on you" at the desk. The inbox is the phone half of the same signal. Same hooks, `attention-state.sh` and `auto-decide.py`.
4. `/decisions/<id>/answer` and the `via` field already exist. Telegram `/decide` and the approval buttons are a second answer path into the same rows.
5. The `notifications` feed is one-way and has 4244 unread rows. It is the wrong place for questions; use it only for "inbox pushed N questions" style info.
6. `approvals` overlaps in meaning (things that wait on Robin) but not in source: they are kernel-proposed actions with an executor behind them. Keep them separate.

## Plug-in points

- Write: keep `auto-decide.py` + `/decisions/raise`. Add the new fields there (live `src/decisions.ts` `RaiseInput:285`, `createDecision:132`).
- Read: add a "list open rows" route next to the other `/decisions/*` routes in live `src/voice-api.ts:1483-1536`. The PAM page (or a Thrivbe-1 service) polls it. Not built: #19 confirmed none exists.
- Answer: reuse `handleDecisionAnswer:513` with `via` and `answered_by`.
- Push: new on Thrivbe-1 (web-push kit, #12). Nothing to reuse in CC. Log each push to the `notifications` feed with `ctx.notify` source `inbox`, per the standing cron rule.
- Desk view: CC could read the same `decisions` rows. Today it has no view of them.

## Not checked

- Whether the CC `/api/os/attention` route works live (no `attention` table found by `fleet db`; CC may read a different db file, `lib/kernel-db.ts:10`).
- Live count of `~/.claude/attention` state files and whether the 60 s sync still runs.
- What `DecisionQueue.tsx` shows for kernel decision rows, and where the swipe UI lives.
- How many of the 155 open `mac-hook` rows are real waiting questions and how many are stale, duplicates or already decided at the desk.
- Whether the Telegram `/decisions` command is used (no usage counts).
- Codex or BB-box sources of decision rows (none seen in the source breakdown).
