# Voice Agent — Execution Approach

> Composed via `/coding-strategist` (2026-06-24). Companion to `OPTIMIZATION-ROADMAP.md`
> (the *what*); this is the *how*. Decisions: **all 6 phases · full TDD / 80% coverage ·
> superpowers-lite process + ECC reviewers**.

## Spine
superpowers discipline, run **once per roadmap phase**, ECC reviewers at each gate, GitNexus on
every edit, full TDD as the non-negotiable core. (Catalog archetype: *feature on existing complex
repo*, looped.) No GSD file artifacts.

## Cross-cutting (always on)
- **ponytail** — smallest working diff, nothing speculative.
- **gitnexus** — `npx gitnexus impact <symbol> --repo voice-agent` before editing any symbol;
  `detect_changes` (or re-`analyze`) before each commit. Warn on HIGH/CRITICAL blast radius.
- **claude-mem** — multi-session continuity (this is multi-session work).
- **codex:codex-rescue** — escape hatch if stuck 3+ fixes, esp. the Phase 4 extraction.
- **grill-me** — one gate only: the Phase 4 module-split layout.

## Why the order differs from the roadmap
Full-TDD + all-phases flips two things vs the roadmap's P1-first ordering:
1. Can't TDD a zero-test repo with an untestable god-object → **bootstrap tests + pull seam
   extraction (Phase 4) earlier**.
2. Latency fixes before the refactor = writing them twice → **refactor before perf**.

## Steps (check off as you go)

- [ ] **1. Roadmap 0.2 — fix `config.py:200-210 --selftest`.** Inline + ponytail. Restores the dead
      smoke test (asserts stale `agentic_shell`/`ptt` keys). _Verify:_ `python config.py --selftest` passes.
- [ ] **2. Bootstrap test harness (TDD prereq).** `pytest` + `tests/` + `conftest` fakes (fake
      websocket, fake `sounddevice`, temp sqlite) + `/setup-pre-commit` to gate commits.
      _Without this, "full TDD" is impossible._
- [ ] **3. Roadmap Phase 1 — security P0.** `/tdd` per fix (red→green): openPane whitelist, 0600
      perms, `shlex.quote` recall cmd, drop key from inherited env, delete keylogger. Gate:
      `ecc:security-scan` + `ecc:security-reviewer`.
- [ ] **4. Roadmap Phase 4 — architecture (pulled earlier).** `superpowers:writing-plans` on the
      split → **`grill-me` gate on the layout** → `gitnexus-refactoring` + `rename` →
      characterization tests first, then extract: `shell.py`, `tools.py`, `macos_context.py`
      (breaks the `realtime→agent` cycle), `audio.py`, `MemoryStore`, cached config. These are the
      test seams the rest needs. `codex:codex-rescue` if extraction stalls.
- [ ] **5. Roadmap Phase 2 — reliability.** `/tdd` reconnect/backoff, mic-open guard, stored task
      handle, SQLite WAL+busy_timeout, surfaced session-death. `/verify` (run the app).
- [ ] **6. Roadmap Phase 3 — latency.** `/tdd` + `ecc:performance-optimizer`: bounded `_out_q`,
      level-delta gating, numpy off the audio callback, schema-once `MemoryStore`, grab timeout.
      `/verify`. Lands once, in clean modules.
- [ ] **7. Roadmap Phase 5 — cleanup.** `superpowers:dispatching-parallel-agents` (independent
      deletions), optionally in a `git-worktree`: drop `openai` dep, `rm -rf` scrap (~94MB), delete
      `_open_task_log`, collapse `_ENV_FALLBACK`/build scripts.

**Per IMPLEMENT step:** close with `superpowers:requesting-code-review` + `ecc:python-reviewer`;
`ecc:test-coverage` to hold 80%.
**Per phase SHIP:** `/github-flow` — branch-per-phase → PR → merge.
**At session boundaries:** `/handoff` + claude-mem. **At end:** `/ponytail-debt`.

## Skipped (deliberately)
- DESIGN/UI phase — pill/settings only de-jittered, not redesigned.
- `gsd-ai-integration-phase` — realtime+learning AI already exists; hardening, not designing.
- Full GSD phases / `gsd-secure-phase` — chose lite; `ecc:security-scan` covers the P0 need.
- `llm-council` — `grill-me` reserved for the one real fork (module split); rest is execution.
