# Voice Agent — Optimization Roadmap

> Source: 6-lens parallel code analysis (Python correctness, security, performance/latency,
> architecture, error-handling/reliability, Ponytail simplification), each driven against the
> GitNexus knowledge graph for `voice-agent`. Generated 2026-06-24.
>
> **Confidence markers:** `[consensus: N]` = flagged independently by N of the 6 lenses (higher = higher
> confidence). `✅ graph-confirmed` = structural claim verified against the rebuilt GitNexus graph
> (callers/callees/blast-radius), not just a source read.
>
> **GitNexus is now live again** (329 nodes, 769 edges, 19 flows, embeddings populated). Run impact
> analysis before editing any symbol: `npx gitnexus impact <symbol> --repo voice-agent`.

---

## Phase 0 — Unblock (minutes) — ✅ partially done

| # | Item | Status | Effort |
|---|------|--------|--------|
| 0.1 | Re-index GitNexus (was v41/v40 mismatch, `embeddings: 0`) | ✅ **DONE** — graph + embeddings rebuilt, verified | — |
| 0.2 | Fix/cut `config.py:200-210 --selftest` — asserts `agentic_shell is True` (default `False`) + reads non-existent `ptt` key → crashes every run `[consensus: 4]` | TODO | S |

---

## Phase 1 — Security ship-blockers (P0)

| # | File:Line | Issue | Fix | Effort |
|---|-----------|-------|-----|--------|
| 1.1 | `settings.py:234` | `openPane` JS-bridge `arg` → unescaped `subprocess.run(["open", ...])`; webview has no CSP | Whitelist `arg` against known pane constants | S |
| 1.2 | `config.py` / `agent.load_env` | OpenAI key promoted into `os.environ` for whole process tree → any model `run_shell` can `echo $OPENAI_API_KEY` | Read key directly, pass only to ws header; don't inherit | M |
| 1.3 | `memory.py:31` | `conversations.db` (transcripts+PII) & `config.json` created 0644 in 0755 dir | `makedirs(...,0o700)` + `chmod 0o600` | S |
| 1.4 | `memory.py:384` | `_recall_command` interpolates model/voice-derived `{query}` into `sh -c` → injection | `shlex.quote`/argv, not string interpolation | S |
| 1.5 | `keytest.py` + `run-keytest.sh` | Committed keystroke logger → 0644 `keytest.log` | Delete from repo (also Phase 5) | S |
| 1.6 | `realtime.py` / `entitlements.plist` | `put_text` auto-paste + arbitrary `run_shell`/`delegate` unconfirmed; over-broad entitlements | Confirmation gate; drop `disable-library-validation`/`allow-unsigned-executable-memory` if unneeded | M–L |

---

## Phase 2 — Reliability: daemon goes silently deaf (P0)

| # | File:Line | Failure mode | Fix | Effort |
|---|-----------|--------------|-----|--------|
| 2.1 | `realtime.py:528` | No websocket reconnect — any drop ends session, daemon dies with one log line | Backoff reconnect loop while `_running` + "reconnecting" pill state | M |
| 2.2 | `realtime.py:795` | Mic stream open unguarded — device loss/permission-denied kills session | try/except + default-device fallback (mirror player) + user-visible error | M |
| 2.3 | `agent.py:535` / `realtime.py:445` | Abnormal session death silently flips pill to idle; user thinks it's listening | Surface notification + failure reason via `on_state` | M |
| 2.4 | `realtime.py:541` | `ensure_future(_pump_mic())` fire-and-forget — exceptions lost, mic stops silently | Store handle + done-callback that logs/surfaces + tears down | S |
| 2.5 | `memory.py:35-64` | SQLite no `busy_timeout`/WAL → concurrent `_learn` thread + main = "database is locked", caught, write silently dropped `[consensus: 3]` | `PRAGMA busy_timeout=5000` + WAL | S |

---

## Phase 3 — Performance / latency hot path (P1)

| # | File:Line | Issue | Fix | Effort |
|---|-----------|-------|-----|--------|
| 3.1 | `agent.py:281,326` | Pill `set_level` 20Hz unconditional ObjC→JS bridge hop even in silence, competes with audio loop | Gate on level delta, drop to ~10-12Hz; remove redundant per-tick `set_state` in `_reconcile_pill` | S |
| 3.2 | `realtime.py:408` | `_out_q` unbounded — model audio outpacing playback grows without bound; expensive barge-in flush `[consensus: 2]` | Bound queue, drop-oldest (same for mic queue) | M |
| 3.3 | `memory.py:35-64` | `_db()` re-runs `CREATE TABLE`/FTS DDL + reconnects on **every** call. ✅ **graph-confirmed: ~20 callers** (record/recall/recent/add_learning/reinforce/top_learnings + session flow). Taxes session-start latency before first reply `[consensus: 4]` | Create schema once at init; reuse one connection (`MemoryStore`) | M |
| 3.4 | `realtime.py:803` | numpy import + RMS on PortAudio callback thread → glitch risk `[consensus: 2]` | Import once at module top; move level calc off the callback | S |
| 3.5 | `realtime.py:555,662` | Synchronous AppleScript/screenshot grab sits in the silent gap between "you stopped" and "model replies" | Pre-warm/cache, or fire `response.create` first with short grab timeout | M |

---

## Phase 4 — Architecture (P2; do after re-index so impact() guides splits)

| # | Scope | Issue | Refactor | Effort |
|---|-------|-------|----------|--------|
| 4.1 | `realtime.py` (961 lines, max 800) / `LiveSession` god-object | ✅ **graph-confirmed: 20+ outgoing methods** — ws protocol + audio threads + `_do_tool` + `_learn` + memory + macOS context in one class; untestable without a live socket | Split: `shell.py`, `tools.py`, `macos_context.py` (breaks `realtime→agent` cycle), `audio.py`, `live_prompt.py`, `realtime_client.py`, `live_session.py` (orchestration only) | L |
| 4.2 | `config.py:130` / `settings.py:147` | `get()`/`load()` re-read+reparse `config.json` on **every** call. ✅ **graph-confirmed: called across webview bridge, `_do_tool`, `grab_context`, `toggle_live`, `recall`, 10+ clusters**; `_state()` ≈25 disk reads/push | Cached config object, invalidate on `set_`; serialize concurrent `set_` to stop last-writer-wins clobber | M |
| 4.3 | `_do_tool` | ✅ **graph-confirmed: fans out to ~20 symbols** — 65-line if/elif mixing arg-parse + side-effects + ws replies | Table-drive: `{name: handler(args, ctx)}` registry colocated with `TOOLS` schema | M |
| 4.4 | `agent.py:90-240` | macOS capture lives in menubar module; `realtime` reaches back via `from agent import grab_context` (the import cycle) | Extract `macos_context.py` both import | M |
| 4.5 | `realtime.py:476` | `_learn` owns continuous-learning policy buried as a private session method; untestable | Move to `memory.py`/`learning.py` as `learn_from(transcript, cfg)` | M |
| 4.6 | 3× modules | Duplicated `_log` shims + duplicated `.env` parsing | One `logging.py`; dedupe env parsing into config | S |

---

## Phase 5 — Cleanup / Ponytail (P2) — ~94MB + ~85 lines, parallelizable anytime

| # | Item | Saving |
|---|------|--------|
| 5.1 | Drop unused `openai` dep (`setup.py` + `requirements.txt`) — app uses raw `websockets` | ~2 lines + bundled dep |
| 5.2 | `rm -rf` dev scrap: `.venv.py314-backup` (91MB, not even gitignored), `build.log` (2.5MB), `mictest.*`, `ff_err.txt`, `*.log`; widen ignore to `.venv*backup*` | ~94MB, 8 files |
| 5.3 | Delete `_open_task_log` (`realtime.py:110`) — Terminal `tail -F` window duplicates the spoken auto-wake result | ~17 lines |
| 5.4 | Collapse `_ENV_FALLBACK` (dict-of-lists for one key), shrink `patch_rumps_status_item`, fold 3 build scripts into one `--release` flag, fix stale PTT copy in `setup.py` plist strings | ~30 lines |

---

## Sequencing

```
0.1 ✅ → 0.2 (quick win) → Phase 1 (security P0) → Phase 2 (reliability P0)
        → Phase 3 (latency P1) → Phase 4 (refactor P2, impact()-guided) → Phase 5 (cleanup, anytime)
```

**Logic:** P0 phases are exploitable or break the running product. Phase 3 is the latency the product
sells on. Phase 4 deliberately follows the re-index so `impact()` can de-risk splitting the god-object.
Phase 5 is safe deletion.

**Workflow per item:** `npx gitnexus impact <symbol> --repo voice-agent` before editing → warn if
HIGH/CRITICAL → edit → `npx gitnexus detect_changes` (or re-`analyze`) before commit.
