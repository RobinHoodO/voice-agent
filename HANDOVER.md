# Thrivbe Voice Agent — Session Handover (2026-06-25)

Standalone macOS menubar voice agent. Repo: `~/Thrivbe-AI/projects/voice-agent`
(private GitHub `RobinHoodO/voice-agent`). Double-tap **Control** → live OpenAI
Realtime conversation. Built as a signed `.app` via `./build_app.sh`.

## Branch state
- Working branch: **`fix/bundle-settings-html`** — 12 commits, all pushed to origin.
- **PR into `main` is NOT opened/merged yet.** (Open with `gh pr create --base main`.)
- App is rebuilt + running from this branch.

## What shipped this session (in order)
1. **fix: bundle `settings.html`** — Settings window silently failed in the packaged
   app (`NotADirectoryError`: `settings.html` wasn't a py2app data_file, and the path
   resolved inside `lib/python312.zip`). Now shipped via `data_files` + resolved via
   `RESOURCEPATH`.
2. **feat: two-layer memory UI** — `settings.html` Memory section rebuilt to show the
   on-device store (always-on, live conversation/learning counts + recent-learning
   chips) vs. optional wider-memory provider. New `memory.panel_stats()` +
   `settings._mem_stats()`.
3. **Sidebar nav** made real (was decorative `nav dim`) — Settings/Memory/Account/System
   scroll-nav via `go()`.
4. **feat: delegate-by-default** — `live_prompt.py` LIVE_SYSTEM rewritten: any "do
   something" request → `delegate` (background agent); `run_shell` is look-ups only.
   Plus a learn-when-stuck instruction.
5. **feat: task terminals** — `live.show_task_terminals` (Settings toggle). When on,
   `delegate` opens **interactive pi in its own Terminal** (real TTY, full live UI) —
   NOT a `tail -f` and NOT `pi -p | tee` (a pipe strips the TTY → pi only prints the
   final answer, which looked frozen). Trade-off: interactive pi stays open → no
   auto-captured result to speak (watching IS the result). Headless mode (toggle off)
   keeps nohup + auto-wake + spoken result.
6. **feat: delegate target picker** — Settings "Delegate tasks to": pi / Claude / off
   (`live.delegate`, backend already supported it).
7. **fix: UTF-8 agentic shell** — `shell.py` Popen had no `encoding=`; in the py2app
   bundle the locale is ASCII so non-ASCII output (`≤`, smart quotes, emoji) crashed
   the readline loop. Added `encoding="utf-8", errors="replace"`. **This was the real
   blocker** for the desktop-icons task.
8. **fix: cursor-following context** — `macos_context.py` window-text + screenshot now
   follow the window UNDER THE CURSOR (multi-monitor), not the frontmost window.
   New helpers `_cursor_loc`, `_ax_window_under_cursor`, `_window_under_cursor_bounds`.
9. **build: STABLE self-signed signing identity** — `make_signing_cert.sh` creates
   `Thrivbe Voice Dev` cert in login keychain; `build_app.sh` signs with it instead of
   ad-hoc. **This ends the re-grant treadmill**: ad-hoc signing changed the cdhash every
   build → TCC (Input Monitoring/Accessibility) grants dropped each rebuild. Stable
   identity → constant designated requirement → grants survive rebuilds.

## Key gotchas (carry forward)
- **Rebuild no longer breaks permissions** (stable signing). If grants ever do reset:
  `tccutil reset All com.thrivbe.voice-agent`, relaunch, re-grant Input Monitoring +
  Accessibility. `accessibility_trusted=False` in the log is a RED HERRING — the hotkey
  uses Input Monitoring, not `AXIsProcessTrusted`.
- **p12 import gotcha** (in `make_signing_cert.sh`): Apple's `security import` rejects
  OpenSSL-3's default SHA-256 MAC AND empty-password p12 → must use
  `-legacy -macalg sha1` + a non-empty password.
- **Flaky test**: `tests/test_memory_store.py::test_concurrent_records_not_dropped`
  fails under full-suite load (SQLite schema-init race), passes alone. Blocks the
  pre-commit hook intermittently — just retry the commit. `--no-verify` is hook-blocked.
- **Logs**: `~/Library/Logs/ThrivbeVoice/agent.log` (+ `activity.log`). Config + keys:
  `~/Library/Application Support/ThrivbeVoice/` (keys in macOS Keychain svc "ThrivbeVoice").
- **Deploy without rebuild** (preserves TCC): `cp settings.html "Thrivbe Voice.app/Contents/Resources/"` + relaunch (HTML is a sealed resource but the main-exec cdhash is unchanged, so grants hold). Python changes need a full `./build_app.sh`.

## OPEN / next
- **PR + merge** `fix/bundle-settings-html` → `main`.
- **The big one — "solve-anything" process fix (not yet built):** the desktop-icons task
  exposed that the agent declares success off a PROXY (script's "done" echo) instead of
  verifying the real end-state. Desktop icons: Finder *reports* the scripted positions
  but macOS (Sonoma/Sequoia) doesn't render scripted desktop-icon moves reliably —
  arrangeBy is already `none`, automation works, so it's a macOS rendering limitation,
  not a script bug. Proposed fix: wrap every `delegate` instruction in a
  **verify-or-be-honest loop** — restate goal as an observable end-state, act, VERIFY
  via independent observation (not the tool's self-report), iterate on failure, and
  report honestly when it can't be confirmed. Highest-leverage change; Robin agreed it's
  the real product. NOT yet implemented.
- Optional: have the delegate prompt nudge pi to compute screen geometry dynamically
  (the saved `~/.claude/scripts/arrange_desktop_icons.applescript` hardcodes 2056×1329).
- Fix the flaky concurrency test (guard schema init with a lock).

## Build / run
- Rebuild: `./build_app.sh` (signs with `Thrivbe Voice Dev`; run `./make_signing_cert.sh`
  once on a fresh machine first).
- Relaunch: `pkill -f "Thrivbe Voice.app/Contents/MacOS"; open "Thrivbe Voice.app"`.
- Self-tests: `.venv/bin/python memory.py demo`; `pytest tests/`.
