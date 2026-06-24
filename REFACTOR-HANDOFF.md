# Refactor Handoff — resume Step 4 (god-object split) + Phases 2/3/5

> Written 2026-06-24 at a session boundary. Companion to `OPTIMIZATION-ROADMAP.md` (what)
> and `PROJECT-APPROACH.md` (how). Start a fresh session and follow this.

## State (all on `main`)
- ✅ Step 1 selftest fix · ✅ Step 2 pytest harness · ✅ Step 3 Phase 1 security (5 holes)
- ✅ Step 4 modules **1–2 of 9**: `macos_context.py`, `shell.py` extracted + merged.
- **13 tests green** (`.venv/bin/python -m pytest`). Every module imports headlessly.
- Branch off `main` for the next work: `git checkout -b refactor/split-realtime-2`

## Remaining Step 4 modules (3–9) — extract from `realtime.py`
Do **leaf/low-coupling first**, one module per commit, characterization test each.

| # | Module | Extract | Coupling / risk |
|---|--------|---------|-----------------|
| 3 | `tools.py` | `TOOLS` schema (module-level list) + turn `_do_tool` into a `{name: handler}` registry | MED — `_do_tool` fans to ~20 symbols; keep handlers calling back into the session via a passed `ctx` |
| 4 | `live_prompt.py` | `LIVE_SYSTEM` constant + `_build_live_instructions` | LOW — mostly string building; reads config + memory. Good early win. |
| 5 | `memory.py: MemoryStore` | wrap `_db` + module fns into a class: **one** connection, schema created **once** (also Phase 3.3 perf fix) | MED — many callers (~20, graph-confirmed). Keep module-level fns as thin shims or update callers. |
| 6 | `config` cache | cache `load()` result, invalidate on `set_` (also Phase 4.2) | LOW-MED — watch the concurrent `set_` race (serialize). |
| 7 | `audio.py` | `LiveSession._start_audio/_mic_cb/_player/_pump_mic/_flush_out/_teardown_audio` + device lookup | **HIGH** — heavy `self` state (queues, `_speaking`, `_out_q`). Likely an `AudioIO` collaborator the session owns. CANNOT be tested headlessly (needs PortAudio); rely on import-check + manual `/verify`. |
| 8 | `realtime_client.py` | `_headers` + ws connect/send/recv framing + `_session` loop | **HIGH** — the ws lifecycle. Extract as a client the session drives. |
| 9 | `live_session.py` | what remains of `LiveSession` = orchestration only | **HIGH** — last, after 3–8 thin it out. |

## Then Phases 2/3/5 (roadmap)
- **Phase 2 reliability** (was Step 5): ws reconnect/backoff, mic-open guard+fallback, stored `_pump_mic` task handle, SQLite WAL+busy_timeout, surfaced session-death. TDD where seams allow; `/verify` for the live path. Easier after modules 5/8 exist.
- **Phase 3 latency** (Step 6): bounded `_out_q`, 20Hz→delta-gated pill `set_level`, numpy off the audio callback, schema-once (done if MemoryStore landed), grab timeout. `ecc:performance-optimizer`.
- **Phase 5 cleanup** (Step 7): drop unused `openai` dep (setup.py + requirements.txt), `rm -rf` scrap (`.venv.py314-backup` 91MB, `build.log`, etc.; widen `.gitignore` to `.venv*backup*`), delete `_open_task_log`, collapse `_ENV_FALLBACK`/build scripts. Parallelizable.

## Workflow (per the approved approach)
1. `npx gitnexus impact <symbol> --repo voice-agent` **before** editing a symbol; warn on HIGH/CRITICAL.
2. Write the characterization/regression test first (red), then extract (green). Full TDD.
3. After each module: all modules import (`for m in ...: python -c "import $m"`) + `pytest` green + commit `refactor(split N/9): ...`.
4. Re-`npx gitnexus analyze --force` after a batch so impact stays accurate.
5. Per phase: `superpowers:requesting-code-review` + `ecc:python-reviewer`; merge branch → `main`.

## Gotchas (this environment)
- **GateGuard** fires on the first Edit/Write per call: present the 4 facts, then re-issue the identical call (the retry passes). Repair work can disable it via `ECC_GATEGUARD=off` / `ECC_DISABLED_HOOKS`.
- **GitNexus CLI needs `--repo voice-agent`** (4 repos indexed). Embeddings re-index needs the HF cache env vars (see `~/.claude/skills/gitnexus-cli/SKILL.md`).
- **Headless tests can't exercise live audio/ws** (PortAudio + OpenAI socket). For modules 7–9 lean on import-checks, characterization of pure logic, and manual `/verify` of the running app.
- Pre-commit hook (`.githooks/pre-commit`, enabled via `core.hooksPath`) runs pytest on every commit.
- Attribution disabled in commits (user global rule).
