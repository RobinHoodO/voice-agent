# voice-bridge — decommissioning inventory

**Status: RESEARCH ONLY. Nothing in this document has been executed.** voice-bridge is
still Robin's live phone assistant and every unit named here is still running on
Thrivbe-1 as of 2026-08-09 17:xx CEST. The runbook in §6 is written to be executed
*later*, after the port decisions in §3 land in `voice-agent`.

Written by worker `bridge-inventory`, round 1, 2026-08-09.
Companion: `../ARCHITECTURE.md` (the new system this ports into).

---

## 1. What voice-bridge actually is, today

A FastAPI app (`server.py`, 2611 lines) on Thrivbe-1 that gives Robin a phone-reachable
voice assistant over Tailscale HTTPS. Two systemd units serve the *same* app:

| Unit | Bind | Reached via | Why both |
|---|---|---|---|
| `voice-bridge.service` | `0.0.0.0:8765` (uvicorn + own TLS) | `https://hetzner.tail9908c7.ts.net:8765` | Direct TLS with a `tailscale cert` keypair. Command Center talks to this one. |
| `voice-bridge-serve.service` | `127.0.0.1:8766` (plain HTTP) | `tailscale serve --https=8451` → `https://hetzner.tail9908c7.ts.net:8451` | Added 2026-08-02 so Android's installed-app detection sees a distinct Tailscale-managed app instead of colliding with Command Center's `:8444`. **This is the path the phone PWA actually uses.** |

Deploy is `./deploy.sh` (scp + `systemctl restart` both units). Secrets and certs live
only on the server. The repo has **no git remote** — it is local-only.

### Live-usage reality check (the reason this is a decommission and not a migration)

```
# journalctl -u voice-bridge      --since "14 days ago" | grep -cE "POST /ask|Whisper|transcribe"
0
# journalctl -u voice-bridge-serve --since "14 days ago" | grep -E "POST /ask|/live|GET / " | day-count
      1 Aug 04
      2 Aug 06
      5 Aug 07
```

Last real conversation: **2026-08-07 19:05–19:06**, one `/ask-text` turn (hybrid_rag_search
→ ElevenLabs) plus one `/live` Gemini socket, from tailnet `100.110.143.125` (the phone).
`actions.jsonl` has been 0 bytes since the 2026-08-08 00:00 logrotate. Usage is ~5 turns
a week, not a daily driver.

### The phone is a different node than the code thinks

```
# tailscale status
100.110.143.125  robin-t-1   robin@  android  -                                   ← live
100.127.4.13     robin-t     robin@  android  idle; offline, last seen 5d ago     ← hardcoded everywhere
```

`server.py:101 SSH_CMD` and `attention_push.py:25 SSH_COMMAND` both hardcode
`u0_a634@100.127.4.13`. That node has been offline 5 days and the *new* node answers on
8022 but rejects the key:

```
# tailscale ping 100.127.4.13     → no reply (2 timeouts)
# ssh -p 8022 ... u0_a634@100.127.4.13   → Connection timed out (exit 255)
# tailscale ping 100.110.143.125  → pong from robin-t-1 in 40ms
# ssh -p 8022 ... u0_a634@100.110.143.125
    Warning: Permanently added '[100.110.143.125]:8022' ...
    u0_a634@100.110.143.125: Permission denied (publickey,password,keyboard-interactive).
```

**This corrects the standing memory note.** Termux sshd **is running today** — it
completed a key exchange and answered with an auth challenge. The blocker is not sshd; it
is (a) a stale IP after the phone re-registered as `robin-t-1`, and (b) a stale
UID-derived username / `authorized_keys` after a Termux reinstall. Consequence: every
`android_*` / `send_sms` / `get_phone_status` tool in the bridge is dead, and
`attention_push.py` has failed its phone push **5895 consecutive times**
(`.attention_push_state.json → push_fail_count: 5895`), silently falling back to Telegram.

---

## 2. Capability inventory

Legend for **verified?**: `EVIDENCE` = confirmed by a command run for this document
(quoted in §1/§5); `READ` = read from source but not exercised; `UNVERIFIED` = asserted,
needs a check before acting on it.

| capability | where it lives | port or retire | reason | owner module in the new system | verified? |
|---|---|---|---|---|---|
| **Turn-based `/ask` loop** (audio → local Whisper `small` → OmniRoute `:20128` → ElevenLabs TTS, SSE-streamed sentence-by-sentence) | `server.py:2517 ask()`, `conversation_events()`, `transcribe_audio()`, `synthesize_speech()` | **PORT (as opt-in fallback)** | This is the *only* cheap path either system has. Pam has no non-realtime mode — `core/backends/` holds exactly `openai_backend.py` and `gemini_backend.py`, both premium speech-to-speech. See §4 for the cost argument. | new `core/backends/turn_backend.py` behind `backends/base.py`'s event vocabulary; STT/TTS as `core.caps` capabilities so a server surface can swap them | EVIDENCE (ran, journal shows full loop 2026-08-07 19:05) |
| **`/ask-text` text turn** | `server.py:2574` | **PORT** | Command Center's system-graph chat and `/api/voice/ask-text` both depend on it; also the cheapest possible path (no STT, no TTS). | same turn backend, text entry point | READ |
| **`/live` Gemini Live WebSocket relay** (browser PCM16 ↔ Gemini, tool calls relayed server-side) | `server.py:1995 live_gemini()`, `backends/gemini_backend.py`, `gemini_client.py`, `index.html:407-468` | **RETIRE** | Pam already speaks Gemini Live natively (`core/backends/gemini_backend.py`) with a normalized event vocabulary, session resumption, stall/idle/loop guards and barge-in that the bridge relay does not have. Re-implementing the relay is strictly worse than reusing `LiveSession`. What is worth keeping is the *browser transport*, not the relay — see the PWA row. | `core/backends/gemini_backend.py` + `core/live_session.py` (already built) | EVIDENCE (socket opened + tool call 2026-08-07 19:06) |
| **PWA client** (`index.html` 1111 lines, `manifest.json`, `sw.js`, icons) — hold-to-talk, barge-in, live console, installable fullscreen | `/opt/voice-bridge/{index.html,manifest.json,sw.js,icon-*.png}` | **PORT — IN FLIGHT** | This is the phone reach. `core/audio_core.py` was explicitly written transport-independent so "a browser mic reuses it whole" (`ARCHITECTURE.md` §3). **As of 2026-08-09 17:5x a parallel worker is filling `voice-agent/server/`** (`app.py`, `audio_ws.py`, `session.py`, `index.html`, PWA shell, `voice-agent.service`) — uncommitted, tests currently red. Read `server/README.md` before acting on any row below; it supersedes this one. | `server/` — FastAPI `/live` WebSocket, `BrowserAudioTransport` mirroring `mac/audio.py`, `BrowserLiveSession` over `core.live_session` | READ (their code read, not exercised) |
| **`X-Voice-Token` per-token auth** | `server.py:247 require_voice_token`, env `VOICE_BRIDGE_TOKEN` | **PORT — DONE (renamed)** | A network surface needs auth; `PRODUCT.md` calls one-token-per-tenant "the natural multi-tenant seam". `server/README.md` keeps the pattern verbatim (`X-Voice-Token` on HTTP, `?token=` on the WS, `hmac.compare_digest`, per-tab UUID, token redaction on both uvicorn loggers) but resolves the secret via `core.secrets` as **`VOICE_AGENT_TOKEN`**, not `VOICE_BRIDGE_TOKEN`. ⚠️ Command Center's proxy still sends the *bridge* token — repointing it (G5) means swapping the secret too, not just the URL. | `server/app.py` + `core/secrets.py` | READ |
| **High-stakes spoken confirmation gate** (kernel `highStakes` manifest ∪ `LOCAL_HIGH_STAKES`, 120 s TTL, Norwegian-aware affirm/deny regex, fail-closed when kernel unreachable) | `server.py:106,363-434,2220 _stage_high_stakes`, `AFFIRM_RE`/`DENY_RE`, `PENDING_ACTION_TTL_SECONDS` | **ALREADY PORTED — verify parity** | `ARCHITECTURE.md` §4 documents the same semantics in `core/live_session.py`, including fail-closed-on-unreachable-kernel. Two bridge-only details to check before retiring: (1) `PENDING_ACTION_TTL_SECONDS = 120` — does Pam's gate expire? (2) the Norwegian tokens `ja` / `kjør` / `nei` in the affirm/deny regexes. | `core/live_session.py` (gate), `check_tool_drift.py` (keeps the manifest honest) | READ — **parity gap #1 and #2 are UNVERIFIED** |
| **Session transcript accumulation + flush + kernel cost metering** (`/session-log` → `runs.cost_nok`, feeds `VOICE_BUDGET_NOK`) | `server.py:506 _post_session_log`, `log_session_cost`, `flush_session`, `SESSION_FLUSH_SECONDS=600`, `SESSION_TURN_LIMIT=50` | **ALREADY PORTED** | `core/kernel_tools.py:314 session_log(source, cost_nok, duration_sec, summary)` posts the same `/session-log`; `tests/test_live_session_cost.py` covers it. The ledger keeps working after the bridge dies. | `core/kernel_tools.py:session_log` + `core/live_session.py` | EVIDENCE (kernel `runs` ledger has 30 days of `process_name='voice'` rows — §4) |
| **`VOICE_BUDGET_NOK` daily cap (NOK 50, suspend-not-spend)** | **NOT in the bridge.** `thrivbe-os/src/os-worker.ts:96-127`, reads `SUM(runs.cost_nok) WHERE process_name IN ('os-delegate','voice')` since Oslo midnight | **UNAFFECTED — no action** | The cap is a kernel control, not a bridge control. Killing the bridge does not weaken it; it only removes one of the two things feeding it. Worth stating explicitly so nobody "ports" it. | `thrivbe-os` (stays where it is) | EVIDENCE (read from the running box) |
| **`PII_TOOLS` → `router-safe` model pinning** (GDPR control) | `server.py:108,2316,2346` — once a turn touches `twenty_search_contacts`, `list_inbox_items`, `android_list_contacts`, `android_get_notifications`, `semsearch_query`, `kernel_recall`, `cognee_ask`, the follow-up LLM call is pinned to the `router-safe` no-train combo | **RETIRE — but only because the risk it mitigates disappears with it** | The control exists because the bridge routed conversation text through free third-party relays that train on prompts (`kilo`, `llm7`, `opencode`). Pam routes to OpenAI Realtime / Gemini Live **directly** — `grep -n "router-safe\|PII" core/*.py` returns nothing, and there is no free-relay path to leak into. The replacement is *architectural*: single named sub-processor per session, no relay fan-out. **This must be written into `SUBPROCESSORS.md`, not just dropped.** ⚠️ If the ported turn-based fallback (row 1) points at OmniRoute `auto/*` combos, the risk comes straight back and the pinning must be ported with it. | new `SUBPROCESSORS.md` entry; if the turn fallback ships, a `router-safe`-equivalent pin in `core/backends/turn_backend.py` | READ + EVIDENCE (grep confirms absent from `core/`) |
| **`actions.jsonl` audit journal** — every tool call appended, `send_sms` args redacted to last-4 + body length, `chmod 0o600`, `logrotate` daily × 14 compressed | `server.py:102,462-505 journal_action/_redact_args`, `/etc/logrotate.d/voice-bridge` | **PORT** | This is the only place a *tool action* is recorded independently of the conversation transcript — an accountability record, not a chat log. Pam has `conversations.db` and `live-turns.journal` (both `0o600`, `core/memory.py:41,128`) and an `activity()` UI feed, but **no redacted append-only action journal with a retention policy**. That is a real gap for anything client-facing. | new `core/audit.py` (append-only, redacting, `0o600`) called from `LiveSession._do_tool`; retention via the surface (logrotate on `server/`, size-cap on `mac/`) | EVIDENCE (logrotate config + rotated `.gz` files listed on box) |
| **Log-transcript redaction default-off** (`LOG_TRANSCRIPTS` off ⇒ journald shows char counts only) | `server.py:97` | **PORT (as the default)** | Cheap, and it is the documented posture in `SUBPROCESSORS.md`. The `server/` surface will log to journald the same way. | `server/` logging config | READ |
| **Langfuse tracing** — per-turn traces tagged `voice-bridge`, release-stamped from `RELEASE`, generation spans, `/feedback` → `post_score` | `langfuse_trace.py` (284 lines), `server.py:2368`, `server.py:2559 feedback()` | **PORT** | `grep -rln langfuse core/ mac/` → **no hits**. Pam is currently untraced. Losing the bridge loses fleet LLM observability for voice entirely, and it silently starves the downstream judge (next row). | new `core/tracing.py` (fail-open, same shape as `langfuse_trace.py`), wired in `core/live_session.py` | EVIDENCE (grep confirms absent from `core/` and `mac/`) |
| **`langfuse-judge` nightly LLM-as-judge** (Thrivbe-2, 07:20, pulls 24 h of turns from ClickHouse, judges via OmniRoute, writes scores back) | Thrivbe-2 timer; described in `command-center/src/lib/os/process-descriptions.ts:433` | **DEPENDENT — do not break** | Its input is bridge traces. If tracing is not ported first, this job keeps running against an empty window and quietly reports nothing. Sequence: port tracing → confirm the judge sees Pam turns → then decommission. | (T2 job unchanged; its *source* becomes `core/tracing.py`) | READ |
| **Langfuse-managed tool-guidance prompt** (`voice-bridge-tool-guidance`, label `production`, 600 s TTL, local fallback) + kernel-served persona (`GET /persona`) | `server.py:331-399` | **PARTIAL — persona already ported, prompt registry is not** | `core/live_prompt.py:125` already calls `kernel_tools.kernel_persona()`. The Langfuse *prompt registry* half (edit the tool guidance in a UI, no deploy) has no equivalent — Pam's guidance is source code in `core/live_prompt.py`. Recommend porting: it is how the prompt gets tuned without a `reload_app.sh` cycle. | persona: `core/live_prompt.py` (done) · registry: extend `core/live_prompt.py` with a fetch behind `core/tracing.py`'s credentials | READ |
| **`attention_push.py` + `attention-push.timer`** (every 5 min: kernel `/status` diff → `termux-notification` + `termux-vibrate` over ssh to the phone; Telegram fallback; state file dedupes; Telegram alert after 30 min of kernel-unreachable) | `/opt/voice-bridge/attention_push.py`, `attention-push.{service,timer}`, state `/opt/voice-bridge/.attention_push_state.json`, tests `voice-bridge/tests/test_attention_push.py` | **RETIRE the mechanism, PORT the intent** | The mechanism is broken (`push_fail_count: 5895`, §1) and its intent is already better served: `mac/agent.py:417 _apply_urgent_snapshot` + `core/kernel_tools.py:251 kernel_urgent/urgent_wake_text` poll the same kernel `/status`, filter by priority, dedupe by key, respect quiet hours (`mac/agent.py:64 _in_quiet_hours`) and **speak** the item instead of buzzing it. Covered by `tests/test_urgent_wake.py`. **Residual gap: that only fires when Pam is running on the Mac.** Nothing wakes the phone once the bridge is gone. Decide explicitly (§4, open question O2) — the cheap answer is a kernel-side push (the notifications feed already exists) rather than resurrecting ssh-to-Termux. | `mac/agent.py` + `core/kernel_tools.py` (Mac reach, built) · phone reach = **unowned, needs a decision** | EVIDENCE (state counter + failed ssh, §1) |
| **`tool-drift-check.timer` (Mon 08:00) + `check_tool_drift.py` + `tool-drift-check.sh`** — compares tool schemas against the kernel `/tools` manifest, non-zero exit → Sentry (`thrivbe-ops`) | `/opt/voice-bridge/{check_tool_drift.py,tool-drift-check.sh}`, `tool-drift-check.{service,timer}` | **ALREADY PORTED (improved) — re-point the timer** | `voice-agent/check_tool_drift.py` is the descendant: it checks **both** surfaces (`mac`, `server`), drops the rotted `LOCAL_NAMES` allowlist, and documents that this guards the *confirmation gate*, not just schema hygiene. Ran it for this document: `Surfaces converged: mac, server — one edit lands on all of them. … No drift.` The timer must be repointed at the new script (and its `ENV_PATH` made host-aware) **before** `/opt/voice-bridge` is removed, or the weekly safety check silently dies. | `voice-agent/check_tool_drift.py` (+ a new wrapper `.sh` and a repointed systemd timer) | EVIDENCE (ran it, output in §5) |
| **`send_sms` / `get_phone_status` / `vibrate_phone` / `android_list_contacts` / `android_make_call` / `android_set_clipboard` / `android_get_notifications`** | `server.py:658-1184` (TOOLS), dispatched via `SSH_CMD` | **RETIRE** | All seven are dead today (stale node + stale key, §1) and none is in Pam's 43-tool surface. Restoring them means owning a Termux ssh dependency that has now broken twice. If phone control is wanted again, it should be a deliberate new capability with a stable identity, not a resurrection. `vibrate_phone` is client-side (`navigator.vibrate`) and comes free with the ported PWA. | none (deliberately) | EVIDENCE (ssh failures, §1) |
| **`kernel_*`, `bloom_*`, `graph_*`, `semsearch_query`, `hybrid_rag_search`, `cognee_ask`, `twenty_search_contacts`, `hermes_fleet`, `list_inbox_items`, `os_delegate`, `herdr_delegate`** | `server.py:708-1184` | **ALREADY PORTED** | All present in Pam's 43-tool surface (`ARCHITECTURE.md` §4; `check_tool_drift.py` output §5). Pam's set is a strict superset — it adds Notion, Gmail/Front/Calendar/Drive, web search, local memory, shell, focus. | `core/tools.py`, `core/kernel_tools.py`, `core/services.py`, `core/web.py` | EVIDENCE (drift check lists all 43) |
| **`teach_agent_tool` self-learning loop + `dynamic_registry.json`** | Advertised in `README.md`/`ROADMAP.md`; **not in `server.py`** — `grep` finds no `teach_agent_tool`; `dynamic_registry.json` is 3 bytes (`{}`) | **ALREADY RETIRED — delete the docs claim** | Dead feature still described as live in two README sections. Anyone reading the repo to decide what to port would try to port a ghost. | none | EVIDENCE (grep + `ls -la` on box) |
| **HTTPS certs** — `tailscale cert` Let's Encrypt keypair for `hetzner.tail9908c7.ts.net` | `/opt/voice-bridge/certs/{cert.crt,cert.key}` (`cert.key` is `0600`), issued 2026-06-27, `notAfter=Sep 21 08:38:30 2026 GMT` | **⚠️ CONFLICT — resolve before step 5** | `server/README.md` says the new surface will "reuse `/opt/voice-bridge/certs/{cert.crt,cert.key}` **in place**". That makes `rm -rf /opt/voice-bridge` (runbook step 5) a foot-gun: it would take TLS away from its replacement. Also note **there is no renewal timer for these files** — they expire 2026-09-21 and were last issued by hand. Recommendation: do not inherit the file dependency. Either (a) re-issue into `/opt/voice-agent/certs` with `tailscale cert` and add a renewal timer, or (b) drop the private key entirely — plain HTTP on loopback behind `tailscale serve --https=<port>`, which is what the phone already uses on `:8451` and what makes the `:8765` cert redundant in the first place. Either way, decide before deleting. | `server/` deploy config, not application code | EVIDENCE (`openssl x509 -noout -enddate`) + READ (`server/README.md:93`) |
| **`tailscaled-voicebridge.service`** — a *second* tailscaled identity (`--statedir=/var/lib/tailscale-voicebridge`, userspace networking, node `voice-bridge` = `100.84.255.84`) | `/etc/systemd/system/tailscaled-voicebridge.service` | **RETIRE — with care** | Created for the bridge's own tailnet identity. Confirm nothing else was hung off node `100.84.255.84` before removing it, and remove the node from the tailnet admin console too, or it lingers as a stale device. | none | READ — **UNVERIFIED that nothing else uses node `voice-bridge`** |
| **`tailscale serve --https=8451 → 127.0.0.1:8766`** | tailscale serve config (persistent, survives restarts) | **RETIRE last** | The serve mapping is the phone's actual entry point; removing it is what actually takes the assistant off the phone, so it is the **last** step in the runbook, not the first. Note `server/README.md` picks its own ports (`8767` direct, optional `127.0.0.1:8768` behind `tailscale serve --https=8452`) rather than reclaiming `8451` — which is the safer choice: the two PWAs can be installed side by side during the soak, and `8451` is freed only once Robin has stopped using the old install. | `server/` deploy config | EVIDENCE (`tailscale serve status`) + READ (`server/README.md:85-91`) |
| **Command Center `/api/voice/[action]` proxy** (`ask`, `ask-text`; keeps `VOICE_BRIDGE_TOKEN` server-side, streams SSE through) | `command-center/src/app/api/voice/[action]/route.ts` → `https://hetzner.tail9908c7.ts.net:8765` | **DEPENDENT — must be repointed, not deleted** | Consumed by `VoiceBriefController.tsx:133,189`, rendered by `OverviewDashboard.tsx:150` and `BriefingCard.tsx`. **`/today` itself is a `permanentRedirect("/inbox")`** (`app/today/page.tsx`) — the daily brief now lives on `/inbox`; the route comment naming "/today" is stale. If the bridge stops, the brief's voice button returns HTTP 502 "Voice bridge unreachable". | repoint `BRIDGE_BASE` at the `server/` surface | EVIDENCE (read both files) |
| **Command Center `/api/system-graph/chat`** (voice + text chat over the system map, `/ask` and `/ask-text`) | `command-center/src/app/api/system-graph/chat/route.ts:56,69,141` | **DEPENDENT — must be repointed** | Same bridge, different consumer. Breaks the same way. | repoint at `server/` | EVIDENCE (read) |
| **Command Center health tile** (`GET :8765/ping`, drives the "voice agent" up/down light) | `command-center/src/app/api/stats/route.ts:26` | **DEPENDENT — repoint or remove the tile** | Will show permanently red otherwise. | repoint or delete | EVIDENCE (read) |
| **CC OS-map + process descriptions entries** (`voice-bridge`, `voice-router` nodes) | `command-center/src/lib/os-map/spine.ts:227`, `src/lib/os/process-descriptions.ts:331,337` | **UPDATE** | Per the CC convention, every fleet process needs a `process-descriptions.ts` line; stale entries make the OS map lie. | CC source | EVIDENCE (read) |
| **`voice.sh`** — Termux CLI client (sox record → curl `:8765` → speak) | `/opt/voice-bridge/voice.sh` (and repo) | **RETIRE** | Superseded by the PWA years-of-UX ago; depends on the same broken Termux environment; `BRIDGE_URL` hardcodes `http://100.114.219.63:8765` over plain HTTP. | none | READ |
| **`SUBPROCESSORS.md`** — GDPR Art. 28/30 sub-processor register for *both* voice systems | `voice-bridge/SUBPROCESSORS.md` (last updated 2026-07-11) | **PORT + REWRITE (mandatory)** | It is already **factually wrong about the live system**: it names `freellmapi relays (localhost:8792 → :3004)` as the bridge's LLM path, but `server.py:79-81` has routed to OmniRoute `:20128` since 2026-07-30 and the `freellmapi` container is `not-found` (dead). A register that describes a retired data flow is worse than none. The rewrite must cover: OpenAI Realtime + Gemini Live as the two model sub-processors, ElevenLabs only if the turn fallback ships, OmniRoute's provider fan-out for any routed call, the removal of the free-relay/PII-pinning risk, and the local-storage section (`conversations.db`, `live-turns.journal`, the new action journal). It also carries three open verification items (ElevenLabs DPA, OpenAI zero-retention posture, Art. 13/14 if it ever goes multi-user) that must not be lost. | `voice-agent/SUBPROCESSORS.md` (new file, controller-level, covering Mac + `server/`) | EVIDENCE (compared doc against `server.py` and `fleet services`) |
| **`PRODUCT.md` multi-tenant seam analysis** (per-token auth, kernel-served persona, tool allowlist, PWA, confirm gate = the four properties a product needs) | `voice-bridge/PRODUCT.md` | **PORT (the reasoning, not the file)** | It is the clearest existing statement of why a *server* surface — not the Mac app — is the commercial artifact, and it names the four gaps (tenanting, onboarding, billing attribution, isolation). That belongs with `voice-agent/PRODUCT.md` / `GO-TO-MARKET.md`, otherwise the argument is lost with the repo. | `voice-agent/PRODUCT.md` | READ |
| **`os-ping-fail@<unit>` Sentry wiring** (drop-ins on every voice unit: `OnFailure=os-ping-fail@%n.service`, plus `PYTHONPATH=/usr/local/lib/thrivbe-sentry` + `THRIVBE_SENTRY_SERVICE=` on the Python ones) | `/etc/systemd/system/*.service.d/*.conf` | **PORT the pattern to the new units** | Fleet convention (added 2026-08-08: "a unit that fails silently is a fleet blind spot"), and it satisfies the standing cron rule — errors to Sentry, never Telegram. Any `server/` or repointed drift-check unit must carry the same drop-ins. | `server/` deploy config | EVIDENCE (dumped drop-ins from box) |
| **`unified-router.service` (`:8792`) + `freellm_router_mvp.py`** | Lives in `/opt/voice-bridge/`, but is **not a bridge capability** | **DO NOT TOUCH — separate lifecycle** | `~/.zshrc:124 export CLAUDE_ROUTER_URL="http://100.114.219.63:8792"` — this is Robin's **Claude Code** router. It merely shares a working directory with the bridge. Removing `/opt/voice-bridge` without relocating it breaks Claude Code. It is also `systemctl is-enabled` → **disabled** (running, but will not return after a reboot) — a pre-existing fragility worth fixing separately. | none (relocate to its own prefix) | EVIDENCE (`.zshrc` grep + `is-enabled`) |
| **`voice-router.service` (`:8793`) + `voice_router_mvp.py`, `models.allowlist.real.json`, `voice-router.env`** | `/opt/voice-bridge/`, `127.0.0.1:8793` | **INVESTIGATE before removing** | Also `is-enabled` → **disabled** (running only until the next reboot). CC describes it as "voice/Telegram intake to agents (agent.delegate intake)" — a different concern from the bridge. No consumer found on Thrivbe-1 or the Mac, but absence of a grep hit is not proof. | none identified | **UNVERIFIED — no consumer found, not proven unused** |
| **Server-side backup sprawl** — 11 `server.py.bak.*`, 2 `freellm_router_mvp.py.bak-*`, `index.html.bak.pre-pam`, `manifest.json.bak.pre-pam`, `voice_router_mvp.py.bak.pre-pam`, `router_decisions.jsonl` (2.3 MB) | `/opt/voice-bridge/` | **RETIRE (captured by the tar in §6)** | Hand-edited on-box history from before the repo was made canonical (`server.py:79-80` records that a stale deploy once clobbered an on-box fix). The tarball preserves it; nothing needs it live. | none | EVIDENCE (`ls -la`) |
| **Deploy divergence between repo and box** | repo `HEAD = d5aee98`, box `/opt/voice-bridge/RELEASE = d614e4b` | **RESOLVE BEFORE ARCHIVING** | The box is 2 commits behind the repo's release stamp, yet `attention_push.py` on the box is dated Aug 8 (the `d5aee98` Sentry change) — i.e. **partially deployed**. Do not assume the repo is a faithful record of what ran. Diff box↔repo during the archive step and commit any on-box-only change before tagging. | — | EVIDENCE (`cat RELEASE` + `git log`) |

---

## 3. Port/retire summary

> **Cross-worker note (2026-08-09 17:5x).** A parallel worker is building `voice-agent/server/`
> in this same working tree (uncommitted: `app.py`, `audio_ws.py`, `session.py`, PWA shell,
> `voice-agent.service`, `tests/test_server_{auth,surface}.py`). Where their `server/README.md`
> and this inventory disagree, **theirs is the live decision** — this document is the
> decommissioning view, not the build plan. Two places they interlock: the certs row (they
> plan to read `/opt/voice-bridge/certs` in place) and the auth row (they renamed the secret
> to `VOICE_AGENT_TOKEN`). Both are flagged inline.

**Port (7):** turn-based cheap path · `/ask-text` · PWA client + `server/` surface (in flight) ·
`X-Voice-Token` auth (done, renamed) · action audit journal + retention · Langfuse tracing
(+ prompt registry) · `SUBPROCESSORS.md` rewrite.

**Already ported, verify parity (5):** high-stakes gate (2 open gaps) · session cost
metering · the kernel/Bloom/search/CRM tool set · kernel persona · tool-drift check
(repoint the timer).

**Retire (8):** `/live` Gemini relay · all seven phone/Termux tools · `attention_push.py`
mechanism · `voice.sh` · `tailscaled-voicebridge` · `:8451` serve mapping (last) ·
`teach_agent_tool` remnants · on-box `.bak` sprawl.

**Blocked on a decision (1):** the TLS certs — the replacement surface currently plans to
read them in place.

**Do not touch (3):** `VOICE_BUDGET_NOK` (kernel-owned) · `unified-router` `:8792`
(Claude Code) · `langfuse-judge` on T2 (feed it before you cut its source).

**Must be repointed, not deleted (4 CC surfaces):** `/api/voice/[action]` ·
`/api/system-graph/chat` · `/api/stats` health tile · OS-map + process-descriptions
entries.

---

## 4. The cheap-fallback recommendation

**Recommendation: yes — port the turn-based path, as an explicit fallback mode, and wire
it to the budget the kernel already enforces.**

The argument is not hypothetical. The kernel ledger (`runs.cost_nok`, `process_name='voice'`)
over the last 30 days:

```
2026-07-30 | voice | 27 sessions | NOK 65.07   ← OVER the NOK 50/day cap
2026-07-31 | voice | 32 sessions | NOK 47.73   ← 95% of cap
2026-08-02 | voice | 26 sessions | NOK 37.50
2026-08-04 | voice | 13 sessions | NOK 20.02
2026-08-08 | voice | 18 sessions | NOK 25.18
```

Three facts follow:

1. **The cap trips in practice.** NOK 65.07 on 2026-07-30 exceeded `VOICE_BUDGET_NOK=50`.
   The kernel's rule is *suspend, not spend* (`os-worker.ts:119`) — so on a heavy day
   Robin's voice assistant and `os_delegate` both stop until Oslo midnight. Today the
   bridge is the informal escape hatch; after decommissioning, there is none.
2. **The two paths are an order of magnitude apart.** Realtime sessions in that ledger run
   NOK 0.2–5.0 each (median ≈ 0.8, one at 4.99). A bridge turn is local Whisper (CPU, free)
   + an OmniRoute free-tier completion (free) + ElevenLabs at the system's own configured
   estimate of **NOK 2.3 per 1000 characters** (`server.py:96`). The real 2026-08-07 turn
   synthesised 155 + 70 = 225 chars ⇒ ≈ **NOK 0.52**, and that is the *whole* turn cost.
   Text-only (`/ask-text`, no TTS) is ≈ NOK 0.
3. **Degrading is better than stopping.** "Latency goes up and the voice changes" beats
   "the assistant is off until midnight" — especially for the read-only work (status,
   search, recall, brief) that is most of what gets asked when the budget is already spent.

**Shape of the port.** Not a second app: a third backend behind the existing
`core/backends/base.py` event vocabulary, so `LiveSession` keeps its guards, its gate, its
journal and its metering unchanged. Selection rule: manual mode switch **or** automatic
when `todaysVoiceSpend()` crosses a threshold below the hard cap (e.g. 80%, where the
kernel already sends its warning) — so the fallback engages *before* the wall, not after.

**Two conditions attached.**
- **Cost telemetry must be per-mode.** Today `runs.source_meta` is just
  `{"intention":"human:voice"}` for every session — you cannot tell a bridge turn from a
  Realtime session in the ledger. `session_log(source, …)` already takes a `source`
  argument; make it reach the ledger, or the fallback's savings are unmeasurable.
- **If the fallback routes through OmniRoute `auto/*` combos, the PII pinning comes back
  with it** (see the `PII_TOOLS` row). A cheap path that fans out to training relays is
  not a saving, it is a GDPR regression.

### Open questions for Robin (blocking full decommission)

- **O1 — Does the phone keep a voice assistant?** If yes, `server/` is a prerequisite for
  decommissioning, not a follow-up. If no, the PWA/auth/`:8451` rows collapse to RETIRE and
  this becomes a much smaller job.
- **O2 — Does anything still push to the phone?** `attention_push` is dead and Pam's
  urgent-wake only reaches the Mac. Cheapest replacement is kernel-side (the notifications
  feed + Telegram for the genuinely urgent), not ssh-to-Termux.
- **O3 — Is the `voice-router` `:8793` service anyone's dependency?** No consumer found; not
  proven unused.

---

## 5. Evidence log

Commands run for this document (all read-only; nothing was stopped, disabled or removed).

```
$ fleet services thrivbe-1
voice-bridge   systemd  :8765   voice-bridge.service=active running
unified-router systemd          unified-router.service=active running
…

$ fleet run thrivbe-1 'systemctl list-timers --all | grep -iE "voice|attention|drift"'
Sun 2026-08-09 17:40:00 CEST  attention-push.timer     → attention-push.service   (last 17:35:01)
Mon 2026-08-10 08:00:00 CEST  tool-drift-check.timer   → tool-drift-check.service (last Mon 2026-08-03 08:00:01)

$ fleet run thrivbe-1 'systemctl is-enabled voice-bridge voice-bridge-serve voice-router \
      unified-router attention-push.timer tool-drift-check.timer tailscaled-voicebridge'
enabled / enabled / disabled / disabled / enabled / enabled / enabled

$ fleet run thrivbe-1 'python3 -c "...json.load(.attention_push_state.json)..."'
{'approval_ids': 15, 'attention_keys': 49, 'unreachable_count': 0, 'push_fail_count': 5895}

$ fleet run thrivbe-1 'tailscale ping -c 2 100.127.4.13'
ping "100.127.4.13" timed out ×2 — no reply
$ fleet run thrivbe-1 'ssh -p 8022 … u0_a634@100.127.4.13 echo PHONE_SSH_OK'
ssh: connect to host 100.127.4.13 port 8022: Connection timed out     (exit 255)
$ fleet run thrivbe-1 'tailscale ping -c 2 100.110.143.125'
pong from robin-t-1 (100.110.143.125) via 46.15.63.89:24619 in 40ms
$ fleet run thrivbe-1 'ssh -p 8022 … u0_a634@100.110.143.125 echo PHONE_SSH_OK'
u0_a634@100.110.143.125: Permission denied (publickey,password,keyboard-interactive).   (exit 255)
    → sshd IS listening and completed key exchange; the identity is stale, not the daemon.

$ fleet run thrivbe-1 'openssl x509 -in /opt/voice-bridge/certs/cert.crt -noout -subject -enddate'
subject=CN = hetzner.tail9908c7.ts.net
notAfter=Sep 21 08:38:30 2026 GMT

$ fleet run thrivbe-1 'ss -ltnp | grep -E "8765|8766|8790|8792|8793|20128"'
0.0.0.0:8765 uvicorn · 127.0.0.1:8766 uvicorn · 0.0.0.0:8790 node · 0.0.0.0:8792 python3
127.0.0.1:8793 python3 · 127.0.0.1:20128 + 100.114.219.63:20128 docker-proxy

$ fleet run thrivbe-1 'tailscale serve status'
https://hetzner.tail9908c7.ts.net:8451 (tailnet only) |-- / proxy http://127.0.0.1:8766

$ fleet run thrivbe-1 'cat /etc/logrotate.d/voice-bridge'
/opt/voice-bridge/actions.jsonl { daily rotate 14 missingok notempty compress delaycompress copytruncate su root root }
$ fleet run thrivbe-1 'ls -la /opt/voice-bridge/actions.jsonl*'
-rw------- actions.jsonl (0 bytes, Aug 8 00:00) + .1 + .2.gz … .5.gz     ← chmod 600 confirmed

$ fleet run thrivbe-1 'du -sh /opt/voice-bridge'
463M    /opt/voice-bridge          (459M of it is lib/ — the venv)

$ fleet run thrivbe-1 'cat /opt/voice-bridge/RELEASE'      → d614e4b
$ git -C projects/voice-bridge log --oneline -1            → d5aee98
$ git -C projects/voice-bridge remote -v                   → (none)

$ fleet db "SELECT date(started_at), process_name, COUNT(*), ROUND(SUM(cost_nok),2)
            FROM runs WHERE process_name IN ('voice','os-delegate')
            AND started_at >= date('now','-30 days') GROUP BY 1,2 ORDER BY 1 DESC"
2026-08-09|voice|2|1.52   2026-08-08|voice|18|25.18   2026-08-07|voice|8|6.18
2026-08-04|voice|13|20.02 2026-08-02|voice|26|37.5    2026-07-31|voice|32|47.73
2026-07-30|voice|27|65.07                                    ← over the NOK 50 cap

$ .venv/bin/python check_tool_drift.py            # the PORTED checker, run from voice-agent
surface mac:    43 tools, 1 local high-stakes, sig=a66d87de7a37 (inherits core)
surface server: 43 tools, 1 local high-stakes, sig=a66d87de7a37 (inherits core)
Surfaces converged: mac, server — one edit lands on all of them.
Kernel manifest: version=1 generatedAt=2026-08-09T15:43:53.135Z
Manifest highStakes: kernel_decide
No drift: surfaces agree with each other and with the kernel manifest.

$ .venv/bin/python -m pytest --rootdir <voice-agent> tests -q ; echo $?
196 passed        exit 0
    (one earlier run failed test_memory_store.py::test_concurrent_records_not_dropped —
     "a write was dropped: [0,1,2,3,4]" — and passed on re-run. Flaky, pre-existing,
     unrelated to this document, which changes no code. Worth its own ticket.)
```

---

## 6. Decommission runbook — **TO BE EXECUTED LATER, NOT NOW**

Do not start until **all** of these hold:

- **G1** — O1 answered. If the phone keeps an assistant, `server/` is live on `:8451` and
  Robin has used it for a week without falling back to the bridge.
- **G2** — Langfuse tracing ships in `core/`, and `langfuse-judge` on Thrivbe-2 has scored
  at least one night of Pam turns (otherwise the judge goes blind at step 3).
- **G3** — `tool-drift-check.timer` is repointed at `voice-agent/check_tool_drift.py` and
  has run green **once** from its new location.
- **G4** — The action audit journal exists in `core/` with retention, and
  `voice-agent/SUBPROCESSORS.md` is written and accurate.
- **G5** — Command Center's three call sites are repointed (or removed) and deployed:
  `/api/voice/[action]`, `/api/system-graph/chat`, `/api/stats`.
- **G6** — High-stakes parity gaps closed: confirmation TTL, and Norwegian `ja`/`kjør`/`nei`
  in the affirm/deny matching.

Every step below is run through `fleet` (`fleet run thrivbe-1 '<cmd>'`), never a
hand-composed ssh pipeline.

### Step 0 — Relocate the co-tenants (MUST be first; breaks Claude Code otherwise)

```
# 1. unified-router (:8792) — Robin's CLAUDE_ROUTER_URL. Move it out of /opt/voice-bridge.
fleet run thrivbe-1 'mkdir -p /opt/freellm-router && \
  cp -a /opt/voice-bridge/freellm_router_mvp.py /opt/voice-bridge/models.allowlist.real.json \
        /opt/voice-bridge/voice-router.env /opt/freellm-router/'
# 2. Rewrite unified-router.service WorkingDirectory/ExecStart/EnvironmentFile to /opt/freellm-router
# 3. systemctl daemon-reload && systemctl enable --now unified-router   ← also fixes is-enabled=disabled
# 4. VERIFY (do not proceed until green):
fleet run thrivbe-1 'systemctl is-active unified-router && curl -s -m 5 http://127.0.0.1:8792/v1/models | head -c 200'
# 5. From the Mac, prove Claude Code's router still answers on the tailnet address in ~/.zshrc:
curl -s -m 5 http://100.114.219.63:8792/v1/models | head -c 200
# 6. Same treatment for voice-router (:8793) ONLY IF O3 says it is still wanted;
#    if O3 says unused, leave it in place for now and remove it in step 3 with the rest.
```

### Step 1 — Announce and freeze

```
# Post to the kernel notifications feed (success/info goes here, not Telegram):
fleet run thrivbe-1 'sqlite3 /opt/Thrivbe-AI/projects/thrivbe-os/thrivbe-os.db \
  "INSERT INTO notifications (source, text) VALUES (\"voice-bridge\", \"voice-bridge decommission starting — see docs/BRIDGE-DECOMMISSION.md\")"'
# No further deploys to /opt/voice-bridge from this point.
```

### Step 2 — Stop, in dependency order (reversible: nothing is deleted yet)

```
fleet restart … is not what we want here; use explicit stops via fleet run:
fleet run thrivbe-1 'systemctl stop attention-push.timer tool-drift-check.timer'
fleet run thrivbe-1 'systemctl stop voice-bridge-serve voice-bridge'
fleet run thrivbe-1 'systemctl is-active voice-bridge voice-bridge-serve attention-push.timer tool-drift-check.timer'
   # expect: inactive ×4
```

**Soak here for 7 days.** Stopped-but-present is the cheap rollback: `systemctl start
voice-bridge voice-bridge-serve` restores service in seconds. Watch for: Command Center
502s, a missing Monday 08:00 drift check, anything asking for the phone assistant.

### Step 3 — Disable (survives reboot) and remove the serve mapping

```
fleet run thrivbe-1 'systemctl disable --now attention-push.timer attention-push.service \
                                            tool-drift-check.timer tool-drift-check.service \
                                            voice-bridge voice-bridge-serve'
# Only after server/ owns the port (G1) — repoint; if the phone gets no assistant, remove:
fleet run thrivbe-1 'tailscale serve --https=8451 off'
fleet run thrivbe-1 'tailscale serve status'          # 8451 gone or repointed
fleet run thrivbe-1 'systemctl disable --now tailscaled-voicebridge'
# Then remove node "voice-bridge" (100.84.255.84) from the tailnet admin console — a
# disabled daemon still leaves a stale device. Verify with: fleet run thrivbe-1 'tailscale status'
```

### Step 4 — Reconcile the repo with the box, then tar

```
# 4a. Prove the repo is a faithful record BEFORE archiving (box RELEASE d614e4b != repo d5aee98):
fleet run thrivbe-1 'cd /opt/voice-bridge && sha256sum server.py attention_push.py check_tool_drift.py \
                     langfuse_trace.py index.html gemini_client.py gemini_schema.py'
#     compare against the repo; commit any on-box-only change to the repo first.

# 4b. Full tarball INCLUDING secrets and certs — this is the point of no return for .env.
fleet run thrivbe-1 'tar --exclude=lib --exclude=include --exclude=bin --exclude=__pycache__ \
    -czf /root/voice-bridge-decommission-$(date +%Y%m%d).tar.gz /opt/voice-bridge'
#     (venv excluded: 459M of the 463M is lib/ and is rebuildable from requirements.txt)
fleet run thrivbe-1 'ls -la /root/voice-bridge-decommission-*.tar.gz && \
                     tar -tzf /root/voice-bridge-decommission-*.tar.gz | wc -l'
fleet run thrivbe-1 'chmod 600 /root/voice-bridge-decommission-*.tar.gz'   # it contains .env
# 4c. Copy off-box to the offsite backup target used by os-db-offsite-backup, and verify
#     the checksum matches on both ends before step 5.
```

### Step 5 — Remove from the box

```
fleet run thrivbe-1 'rm -f /etc/systemd/system/{voice-bridge,voice-bridge-serve,attention-push,tool-drift-check,tailscaled-voicebridge}.service \
                           /etc/systemd/system/{attention-push,tool-drift-check}.timer'
fleet run thrivbe-1 'rm -rf /etc/systemd/system/{voice-bridge,voice-bridge-serve,attention-push,tool-drift-check,tailscaled-voicebridge}.service.d'
fleet run thrivbe-1 'rm -f /etc/logrotate.d/voice-bridge'
fleet run thrivbe-1 'systemctl daemon-reload && systemctl reset-failed'

# ⚠️ BLOCKER: server/README.md plans to read /opt/voice-bridge/certs IN PLACE.
#    Do not run the rm until the certs question in §2 is resolved and the new surface
#    no longer points at this directory. Prove it first:
fleet run thrivbe-1 'grep -rn "/opt/voice-bridge" /etc/systemd/system/voice-agent.service /opt/voice-agent 2>/dev/null'
#    expect: no matches. Only then:
fleet run thrivbe-1 'rm -rf /opt/voice-bridge /var/lib/tailscale-voicebridge'
# VERIFY:
fleet run thrivbe-1 'ls /opt/voice-bridge 2>&1; systemctl list-units --all | grep -ci voice-bridge; \
                     systemctl --failed --no-pager'
   # expect: "No such file or directory", 0 matches, no failed units
fleet status thrivbe-1
```

### Step 6 — Archive the repo

```
# The repo has NO remote — archiving means creating one, or it exists only on this Mac.
git -C /Users/robinsverd/Thrivbe-AI/projects/voice-bridge tag -a decommissioned-$(date +%Y%m%d) \
    -m "voice-bridge decommissioned; capabilities ported per voice-agent/docs/BRIDGE-DECOMMISSION.md"
gh repo create thrivbe/voice-bridge --private --source=. --push   # from the repo dir
gh repo archive thrivbe/voice-bridge --yes
# Then move the working copy out of the active projects tree:
mv /Users/robinsverd/Thrivbe-AI/projects/voice-bridge /Users/robinsverd/Thrivbe-AI/lab/archive/voice-bridge
```

### Step 7 — Update the record

- `Thrivbe-AI/CLAUDE.md` — remove voice-bridge from the project map; `projects.json` — drop
  the entry; free ports 8765/8766/8451 in the port-allocation list.
- `command-center/src/lib/os/process-descriptions.ts` + `src/lib/os-map/spine.ts` — remove or
  repoint the `voice-bridge` (and `voice-router`, if removed) nodes.
- `bridge/problem-inventory.md` — note which problems the bridge was solving and where they
  now live.
- `voice-agent/SUBPROCESSORS.md` — final pass: the bridge's processors (ElevenLabs,
  freellmapi relays) are gone unless the turn fallback shipped.
- Memory: replace the stale "attention-push BLOCKED on Termux sshd not running" note with
  the finding in §1 (sshd runs; the identity is stale).
- Close the loop publicly per the workspace habit: draft the labs entry at
  `projects/thrivbe-clone/content/labs/`.

### Rollback

- Before step 3: `systemctl start voice-bridge voice-bridge-serve` — seconds.
- Before step 5: `systemctl enable --now …` + `tailscale serve --https=8451 http://127.0.0.1:8766` — minutes.
- After step 5: restore the step-4 tarball to `/opt/voice-bridge`, recreate the venv from
  `requirements.txt`, reinstall the unit files — ~30 minutes, and only if the tarball was
  verified off-box.
