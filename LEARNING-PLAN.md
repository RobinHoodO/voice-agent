# Plan: Continuous Learning for Thrivbe Voice

## Context

Memory recall already shipped (the foundation below). But in a live test the agent re-`remember`ed something it likely already knew and described itself as only "storing facts in note form" — it accumulates, it doesn't *learn*. The ask: **make it always keep learning** — automatically extract durable lessons from every conversation, reinforce the ones that prove right, and self-correct the ones you contradict.

### Foundation already shipped (reuse, don't rebuild)
- `memory.py` — app-owned `conversations.db` (SQLite+FTS5): `record`/`set_summary`/`recent`/`recall`, pluggable provider (`claude-mem` read-only / `command` / `none`), CLI for `pi`.
- `realtime.py` — captures transcript turns, on close stores it and runs a background `pi` summary (`_summarize`); injects "recent conversations" at session start; `recall` tool; delegate gets recalled context.
- `config.py` `live.memory.*` + Settings → Memory section.

### Decisions (confirmed with user)
- **Adaptive**: reinforce useful learnings, supersede on correction, decay unused.
- **Learn**: preferences, facts, and corrections (not procedures — yet).
- **Storage**: app-owned store **and** mirror into claude-mem.

## Architecture — a learning loop on top of the memory store

App-owned store stays source of truth; claude-mem mirror is best-effort and isolated.

**New `learnings` table** in `conversations.db`:
`id, created_at, ts_epoch, type (preference|fact|correction), text, source_conv_id, strength REAL default 1.0, uses INT default 0, last_used_epoch, superseded_by INT null, status (active|superseded)` + a manually-managed FTS5 mirror (same pattern as `conversations_fts`).

**Extraction (the engine) — on conversation close.** Extend `realtime._summarize` into `_learn`: one `pi -p` call (stdin closed, reusing the fixed dispatch) is given the transcript **and the current active learnings**, and returns JSON: `{summary, new_learnings:[{type,text}], supersede:[ids]}`. So the model itself dedups (don't re-add known facts) and flags contradictions. `memory.py` then:
- inserts `new_learnings` (skipping near-duplicates by normalized-text/FTS match → reinforces instead),
- marks `supersede` ids `status=superseded, superseded_by=<new>` (self-correction),
- mirrors each new active learning into claude-mem.

**Reinforcement + decay (adaptive ranking).** Recall ranks active learnings by `strength * recency`. When extraction re-states an existing learning (match found), bump its `strength` and `uses` instead of duplicating — repeated/confirmed facts rise; unused old ones fall and rank out. Corrections supersede, so mistakes are unlearned.

**Recall integration.** `recent()`/`recall()` lead with top active learnings ("What I've learned about you:") ahead of conversation summaries. The flat `memory.log` `remember` path is unified: the `remember` tool now writes a `learning` (type=preference/fact) into this table, so there's one memory surface (fixes the "note form" silo).

**claude-mem mirror (isolated, non-fatal).** `memory._mirror_claude_mem(learning)`: ensure one dedicated `sdk_session` (content/memory_session_id = `thrivbe-voice`, project `Thrivbe Voice`, status active) exists, then insert an `observations` row (`type='discovery'`, `title`=text[:80], `text`, `project`, `created_at[_epoch]`). The `observations_ai` trigger updates claude-mem's FTS automatically. All wrapped in try/except → failure never blocks the app-owned write.

**Agent self-awareness.** Update `LIVE_SYSTEM` + the `remember` tool description so the agent knows it *already* remembers conversations and is continuously learning preferences/facts; use `remember` only for an explicit durable fact, and don't re-note things it just recalled.

## Files to change
- `memory.py` — `learnings` table + `add_learning`/`extract_apply(payload, conv_id)`/`reinforce`/`supersede`/`top_learnings(k)`; fold into `recall`/`recent`; `_mirror_claude_mem`; CLI `learnings`. Self-check `demo()` extended.
- `realtime.py` — `_summarize` → `_learn` (pi returns summary+learnings JSON, given existing learnings); `remember` tool routes to `memory.add_learning`; `_build_live_instructions` leads with learnings; `LIVE_SYSTEM` + tool-desc wording.
- `config.py` — `live.memory.learn` (default true), `live.memory.mirror_claude_mem` (default true on Robin's machine, false in DEFAULTS for the shipped product).
- `settings.html` + `settings.py` — two toggles in the Memory section ("Keep learning", "Mirror to claude-mem"), reusing the existing toggle + `_state` round-trip.

## Reuse (existing, built this session)
- `conversations.db` + FTS pattern and `memory.py` helpers; the background `pi -p </dev/null` path (`_build_delegate_cmd`/`_summarize`); provider isolation pattern; `config.get/set_` dotted API; Settings toggle + `__push`/`_state` plumbing; claude-mem schema (`observations`/`observations_ai` trigger, `sdk_sessions`).

## Verification (end-to-end)
- **Unit (`memory.py demo`, extended):** add a preference → `top_learnings` shows it; re-state it → `strength`/`uses` bump, no duplicate; add a `correction` superseding it → old row `status=superseded`, recall returns only the new one.
- **Mirror:** after a learning is added, `sqlite3 ~/.claude-mem/claude-mem.db "select title from observations where project='Thrivbe Voice' order by id desc limit 3"` shows it, and `memory.py recall` (provider=claude-mem) finds it. Point the DB at a bad path → app-owned write still succeeds (non-fatal).
- **Live:** state a distinctive preference, end session; confirm a `learnings` row + a claude-mem observation. Next session, contradict it; confirm the old learning is superseded and the agent stops asserting the old one. Check `agent.log` shows "What I've learned" injected.
- **Degrade:** with `pi` unavailable, transcript+summary still store; learnings extraction just no-ops. With `learn=false`, loop is off.

## Standalone / sellable notes
- All learning logic lives in the app-owned store; ships working with `provider:none, mirror:false`.
- claude-mem coupling is confined to `_recall_claude_mem` (read) + `_mirror_claude_mem` (write); a version change touches two isolated functions.
- **Out of scope (future):** procedural/how-to learnings; embeddings/semantic recall; cross-conversation consolidation pass; exporting learnings to the LLM wiki via the `command` provider.

## Risks
- Writing claude-mem's third-party schema is fragile → isolated, non-fatal, behind a toggle; app-owned store is source of truth.
- Over-learning noise → model-side dedup + supersede + strength/recency ranking keep the active set tight; cap injected learnings (e.g. top 8).
- Extraction adds a `pi` job per conversation → same fixed background dispatch; transcript/summary unaffected if it fails.
