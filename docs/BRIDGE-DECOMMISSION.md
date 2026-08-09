# voice-bridge — decommissioning inventory

**Status: RESEARCH ONLY. Nothing in this document has been executed.** voice-bridge is
still Robin's live phone assistant and every unit named here is still running on
Thrivbe-1 as of 2026-08-09 17:xx CEST. The runbook in §6 is written to be executed
*later*, after the port decisions in §3 land in `voice-agent`.

Written by worker `bridge-inventory`, rounds 1–3, 2026-08-09. Round 2 closed the five rows
that had been left undecided (TLS certs, `voice-router` `:8793`, herdr, the PII pin, and the
Whisper model assets) and added an `os-voice-api` row; see §5 "Round-2 evidence".

**Round 3 corrected two things, both about how the phone reaches the bridge:**

1. **A live externally-reachable surface was missing from the inventory.** The second
   tailnet identity `voice-bridge` (`100.84.255.84`) is not just a daemon — it publishes
   its own HTTPS front door, `https://voice-bridge.tail9908c7.ts.net/ → 127.0.0.1:8766`,
   which returns `200` from any tailnet node today. It is invisible to a plain
   `tailscale serve status` (it lives behind `--socket=/var/run/tailscale-voicebridge.sock`),
   which is why two earlier passes missed it. It now has its own row (23), and row 22's
   open question — "is anything else using node `voice-bridge`?" — is answered: **yes, a
   full HTTPS front door onto the bridge server is.**
2. **The claim that `:8451` is "the path the phone actually uses" is withdrawn.** It was
   unsupported. There are two equivalent front doors and nothing on the box tells them
   apart. Marked UNVERIFIED, raised as O5, and the runbook now removes **both** as its
   last production action instead of taking one down in step 3.

Companion: `../ARCHITECTURE.md` (the new system this ports into).

---

## 1. What voice-bridge actually is, today

A FastAPI app (`server.py`, 2611 lines) on Thrivbe-1 that gives Robin a phone-reachable
voice assistant over Tailscale HTTPS. Two systemd units serve the *same* app:

| Unit | Bind | Reached via | Why both |
|---|---|---|---|
| `voice-bridge.service` | `0.0.0.0:8765` (uvicorn + own TLS) | `https://hetzner.tail9908c7.ts.net:8765` | Direct TLS with a `tailscale cert` keypair. Command Center talks to this one. |
| `voice-bridge-serve.service` | `127.0.0.1:8766` (plain HTTP) | **two** tailnet front doors, see below | Added 2026-08-02 so Android's installed-app detection sees a distinct Tailscale-managed app instead of colliding with Command Center's `:8444`. |

**⚠️ `127.0.0.1:8766` has TWO equivalent HTTPS front doors, not one.** Both are live and
both return `200` on `/ping` from any tailnet node today:

| Front door | Published by | Serve mapping |
|---|---|---|
| `https://hetzner.tail9908c7.ts.net:8451` | the **host** tailscaled (node `hetzner`, `100.114.219.63`) | `/ → http://127.0.0.1:8766` |
| `https://voice-bridge.tail9908c7.ts.net` (port 443) | the **second** tailscaled identity `tailscaled-voicebridge.service` (node `voice-bridge`, `100.84.255.84`), via its own socket `/var/run/tailscale-voicebridge.sock` | `/ → http://127.0.0.1:8766` |

**Which one the installed phone PWA points at is UNVERIFIED and not derivable from the
box.** An earlier draft of this document asserted `:8451` was "the path the phone actually
uses"; that claim is withdrawn — nothing readable supports it. The uvicorn access log
records only the peer IP (`100.110.143.125`, the phone) and never the `Host` header, so it
cannot discriminate between the two paths, and the `voice-bridge` socket emits no per-peer
byte counters to attribute traffic with either. If anything, the stated rationale for
creating a separate identity at all — "so Android's installed-app detection sees a distinct
Tailscale-managed app" — points *toward* the PWA having been installed from
`voice-bridge.tail9908c7.ts.net`, i.e. the opposite of the withdrawn claim.

Two ways to resolve it, both cheap, either one before the runbook reaches step 3:

- **(a) ask Robin** — open the installed app on the phone, read the origin off the
  address bar / app info. One question, definitive.
- **(b) log it** — add `Host` to the uvicorn access-log format (or one line of
  middleware) on `voice-bridge-serve` at the start of the 7-day soak and read it off
  `journalctl -u voice-bridge-serve` afterwards. Requires touching the live unit, so it
  is the fallback if (a) is unavailable.

**Until it is resolved, the runbook treats BOTH front doors as phone-facing and removes
both as the genuinely last action, after the soak** (step 6). This is the whole reason
step 3 no longer disables `tailscaled-voicebridge`.

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
`u0_a634@100.127.4.13` — the address literal itself sits on `attention_push.py:33`, inside
the list that opens on `:25`. That node has been offline 5 days and the *new* node answers
on 8022 but rejects the key:

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
| **Turn-based `/ask` loop** (audio → local Whisper `small` → OmniRoute `:20128` → ElevenLabs TTS, SSE-streamed sentence-by-sentence) | `server.py:2517 ask()`, `conversation_events()`, `transcribe_audio()`, `synthesize_speech()` | **PORT (as opt-in fallback) — carrying row 9's pin as a hard constraint** | This is the *only* cheap path either system has. Pam has no non-realtime mode — `core/backends/` holds exactly `openai_backend.py` and `gemini_backend.py`, both premium speech-to-speech. See §4 for the cost argument. **Constraint (not advice): because this path routes through OmniRoute's provider fan-out, it must pin a single named no-train model for any turn that touched a `PII_TOOLS` tool, and must never use an `auto/*` combo for such a turn. Shipping this row without row 9's pin is a GDPR regression, not a cost saving.** It also depends on the Whisper model cache (row 35). | new `core/backends/turn_backend.py` behind `backends/base.py`'s event vocabulary; STT/TTS as `core.caps` capabilities so a server surface can swap them | EVIDENCE (ran, journal shows full loop 2026-08-07 19:05) |
| **`/ask-text` text turn** | `server.py:2574` | **PORT** | Command Center's system-graph chat and `/api/voice/ask-text` both depend on it; also the cheapest possible path (no STT, no TTS). | same turn backend, text entry point | READ |
| **`/live` Gemini Live WebSocket relay** (browser PCM16 ↔ Gemini, tool calls relayed server-side) | `server.py:1995 live_gemini()`, `backends/gemini_backend.py`, `gemini_client.py`, `index.html:407-468` | **RETIRE** | Pam already speaks Gemini Live natively (`core/backends/gemini_backend.py`) with a normalized event vocabulary, session resumption, stall/idle/loop guards and barge-in that the bridge relay does not have. Re-implementing the relay is strictly worse than reusing `LiveSession`. What is worth keeping is the *browser transport*, not the relay — see the PWA row. | `core/backends/gemini_backend.py` + `core/live_session.py` (already built) | EVIDENCE (socket opened + tool call 2026-08-07 19:06) |
| **PWA client** (`index.html` 1111 lines, `manifest.json`, `sw.js`, icons) — hold-to-talk, barge-in, live console, installable fullscreen | `/opt/voice-bridge/{index.html,manifest.json,sw.js,icon-*.png}` | **PORT — IN FLIGHT** | This is the phone reach. `core/audio_core.py` was explicitly written transport-independent so "a browser mic reuses it whole" (`ARCHITECTURE.md` §3). **As of 2026-08-09 17:5x a parallel worker is filling `voice-agent/server/`** (`app.py`, `audio_ws.py`, `session.py`, `index.html`, PWA shell, `voice-agent.service`) — uncommitted, tests currently red. Read `server/README.md` before acting on any row below; it supersedes this one. | `server/` — FastAPI `/live` WebSocket, `BrowserAudioTransport` mirroring `mac/audio.py`, `BrowserLiveSession` over `core.live_session` | READ (their code read, not exercised) |
| **`X-Voice-Token` per-token auth** | `server.py:247 require_voice_token`, env `VOICE_BRIDGE_TOKEN` | **PORT — DONE (renamed)** | A network surface needs auth; `PRODUCT.md` calls one-token-per-tenant "the natural multi-tenant seam". `server/README.md` keeps the pattern verbatim (`X-Voice-Token` on HTTP, `?token=` on the WS, `hmac.compare_digest`, per-tab UUID, token redaction on both uvicorn loggers) but resolves the secret via `core.secrets` as **`VOICE_AGENT_TOKEN`**, not `VOICE_BRIDGE_TOKEN`. ⚠️ Command Center's proxy still sends the *bridge* token — repointing it (G5) means swapping the secret too, not just the URL. | `server/app.py` + `core/secrets.py` | READ |
| **High-stakes spoken confirmation gate** (kernel `highStakes` manifest ∪ `LOCAL_HIGH_STAKES`, 120 s TTL, Norwegian-aware affirm/deny regex, fail-closed when kernel unreachable) | `server.py:106,363-434,2220 _stage_high_stakes`, `AFFIRM_RE`/`DENY_RE`, `PENDING_ACTION_TTL_SECONDS` | **ALREADY PORTED — verify parity** | `ARCHITECTURE.md` §4 documents the same semantics in `core/live_session.py`, including fail-closed-on-unreachable-kernel. Two bridge-only details to check before retiring: (1) `PENDING_ACTION_TTL_SECONDS = 120` — does Pam's gate expire? (2) the Norwegian tokens `ja` / `kjør` / `nei` in the affirm/deny regexes. | `core/live_session.py` (gate), `check_tool_drift.py` (keeps the manifest honest) | READ — **parity gap #1 and #2 are UNVERIFIED** |
| **Session transcript accumulation + flush + kernel cost metering** (`/session-log` → `runs.cost_nok`, feeds `VOICE_BUDGET_NOK`) | `server.py:506 _post_session_log`, `log_session_cost`, `flush_session`, `SESSION_FLUSH_SECONDS=600`, `SESSION_TURN_LIMIT=50` | **ALREADY PORTED** | `core/kernel_tools.py:314 session_log(source, cost_nok, duration_sec, summary)` posts the same `/session-log`; `tests/test_live_session_cost.py` covers it. The ledger keeps working after the bridge dies. | `core/kernel_tools.py:session_log` + `core/live_session.py` | EVIDENCE (kernel `runs` ledger has 30 days of `process_name='voice'` rows — §4) |
| **`VOICE_BUDGET_NOK` daily cap (NOK 50, suspend-not-spend)** | **NOT in the bridge.** `thrivbe-os/src/os-worker.ts:96-127`, reads `SUM(runs.cost_nok) WHERE process_name IN ('os-delegate','voice')` since Oslo midnight | **UNAFFECTED — no action** | The cap is a kernel control, not a bridge control. Killing the bridge does not weaken it; it only removes one of the two things feeding it. Worth stating explicitly so nobody "ports" it. | `thrivbe-os` (stays where it is) | EVIDENCE (read from the running box) |
| **`PII_TOOLS` → `router-safe` model pinning** (GDPR control) | `server.py:108,2316,2346` — once a turn touches `twenty_search_contacts`, `list_inbox_items`, `android_list_contacts`, `android_get_notifications`, `semsearch_query`, `kernel_recall`, `cognee_ask`, the follow-up LLM call is pinned to the `router-safe` no-train combo | **PORT — conditional on row 1 shipping, and it is a hard constraint on row 1, not an option** | This document recommends porting a cheap fallback (row 1) that routes through OmniRoute `:20128` — the fan-out router this control exists to defend against. So the control cannot be retired on the grounds that "there is no relay path": the same document proposes building one. **Binding rule: if the turn fallback ships, it pins a single named no-train model for any turn that touched a PII tool, and never an `auto/*` combo.** If row 1 is *not* built (Pam stays Realtime/Gemini-only, both direct named sub-processors, no fan-out), then and only then does this row become RETIRE, and the replacement is architectural — one named sub-processor per session — which must be written into `SUBPROCESSORS.md` rather than silently dropped. ⚠️ **Second finding: the control is already broken in production.** Since the 2026-07-30 OmniRoute cutover (`server.py:79-81`) the pinned id `router-safe` no longer exists — it was a `freellmapi`/`unified-router` combo. OmniRoute's 2038-model list contains zero matches and a live probe returns `HTTP 400 "Unable to determine provider for model 'router-safe'"`. `query_router` re-raises rather than falling back (`server.py:2378-2380`), so it **fails closed** — the PII turn dies instead of leaking — but nobody has been protected by a working pin for ~10 days; they have been protected by an error. Do not port the string, port the intent. | if row 1 ships: a named-model pin in `core/backends/turn_backend.py` + the rule written into `voice-agent/SUBPROCESSORS.md`. If row 1 does not ship: `SUBPROCESSORS.md` alone | EVIDENCE (grep confirms absent from `core/`; live 400 from OmniRoute; 0/2038 model matches) |
| **`actions.jsonl` audit journal** — every tool call appended, `send_sms` args redacted to last-4 + body length, `chmod 0o600`, `logrotate` daily × 14 compressed | `server.py:102,462-505 journal_action/_redact_args`, `/etc/logrotate.d/voice-bridge` | **PORT** | This is the only place a *tool action* is recorded independently of the conversation transcript — an accountability record, not a chat log. Pam has `conversations.db` and `live-turns.journal` (both `0o600`, `core/memory.py:41,128`) and an `activity()` UI feed, but **no redacted append-only action journal with a retention policy**. That is a real gap for anything client-facing. | new `core/audit.py` (append-only, redacting, `0o600`) called from `LiveSession._do_tool`; retention via the surface (logrotate on `server/`, size-cap on `mac/`) | EVIDENCE (logrotate config + rotated `.gz` files listed on box) |
| **Log-transcript redaction default-off** (`LOG_TRANSCRIPTS` off ⇒ journald shows char counts only) | `server.py:97` | **PORT (as the default)** | Cheap, and it is the documented posture in `SUBPROCESSORS.md`. The `server/` surface will log to journald the same way. | `server/` logging config | READ |
| **Langfuse tracing** — per-turn traces tagged `voice-bridge`, release-stamped from `RELEASE`, generation spans, `/feedback` → `post_score` | `langfuse_trace.py` (284 lines), `server.py:2368`, `server.py:2559 feedback()` | **PORT** | `grep -rln langfuse core/ mac/` → **no hits**. Pam is currently untraced. Losing the bridge loses fleet LLM observability for voice entirely, and it silently starves the downstream judge (next row). | new `core/tracing.py` (fail-open, same shape as `langfuse_trace.py`), wired in `core/live_session.py` | EVIDENCE (grep confirms absent from `core/` and `mac/`) |
| **`langfuse-judge` nightly LLM-as-judge** (Thrivbe-2, 07:20, pulls 24 h of turns from ClickHouse, judges via OmniRoute, writes scores back) | Thrivbe-2 timer; described in `command-center/src/lib/os/process-descriptions.ts:433` | **DEPENDENT — do not break** | Its input is bridge traces. If tracing is not ported first, this job keeps running against an empty window and quietly reports nothing. Sequence: port tracing → confirm the judge sees Pam turns → then decommission. | (T2 job unchanged; its *source* becomes `core/tracing.py`) | READ |
| **Langfuse-managed tool-guidance prompt** (`voice-bridge-tool-guidance`, label `production`, 600 s TTL, local fallback) + kernel-served persona (`GET /persona`) | `server.py:331-399` | **PARTIAL — persona already ported, prompt registry is not** | `core/live_prompt.py:125` already calls `kernel_tools.kernel_persona()`. The Langfuse *prompt registry* half (edit the tool guidance in a UI, no deploy) has no equivalent — Pam's guidance is source code in `core/live_prompt.py`. Recommend porting: it is how the prompt gets tuned without a `reload_app.sh` cycle. | persona: `core/live_prompt.py` (done) · registry: extend `core/live_prompt.py` with a fetch behind `core/tracing.py`'s credentials | READ |
| **`attention_push.py` + `attention-push.timer`** (every 5 min: kernel `/status` diff → `termux-notification` + `termux-vibrate` over ssh to the phone; Telegram fallback; state file dedupes; Telegram alert after 30 min of kernel-unreachable) | `/opt/voice-bridge/attention_push.py`, `attention-push.{service,timer}`, state `/opt/voice-bridge/.attention_push_state.json`, tests `voice-bridge/tests/test_attention_push.py` | **RETIRE the mechanism, PORT the intent** | The mechanism is broken (`push_fail_count: 5895`, §1) and its intent is already better served: `mac/agent.py:417 _apply_urgent_snapshot` + `core/kernel_tools.py:251 kernel_urgent/urgent_wake_text` poll the same kernel `/status`, filter by priority, dedupe by key, respect quiet hours (`mac/agent.py:64 _in_quiet_hours`) and **speak** the item instead of buzzing it. Covered by `tests/test_urgent_wake.py`. **Residual gap: that only fires when Pam is running on the Mac.** Nothing wakes the phone once the bridge is gone. Decide explicitly (§4, open question O2) — the cheap answer is a kernel-side push (the notifications feed already exists) rather than resurrecting ssh-to-Termux. | `mac/agent.py` + `core/kernel_tools.py` (Mac reach, built) · phone reach = **unowned, needs a decision** | EVIDENCE (state counter + failed ssh, §1) |
| **`tool-drift-check.timer` (Mon 08:00) + `check_tool_drift.py` + `tool-drift-check.sh`** — compares tool schemas against the kernel `/tools` manifest, non-zero exit → Sentry (`thrivbe-ops`) | `/opt/voice-bridge/{check_tool_drift.py,tool-drift-check.sh}`, `tool-drift-check.{service,timer}` | **ALREADY PORTED (improved) — re-point the timer** | `voice-agent/check_tool_drift.py` is the descendant: it checks **both** surfaces (`mac`, `server`), drops the rotted `LOCAL_NAMES` allowlist, and documents that this guards the *confirmation gate*, not just schema hygiene. Ran it for this document: `Surfaces converged: mac, server — one edit lands on all of them. … No drift.` The timer must be repointed at the new script (and its `ENV_PATH` made host-aware) **before** `/opt/voice-bridge` is removed, or the weekly safety check silently dies. | `voice-agent/check_tool_drift.py` (+ a new wrapper `.sh` and a repointed systemd timer) | EVIDENCE (ran it, output in §5) |
| **`send_sms` / `get_phone_status` / `vibrate_phone` / `android_list_contacts` / `android_make_call` / `android_set_clipboard` / `android_get_notifications`** | `server.py:658-1184` (TOOLS), dispatched via `SSH_CMD` | **RETIRE** | All seven are dead today (stale node + stale key, §1) and none is in Pam's 43-tool surface. Restoring them means owning a Termux ssh dependency that has now broken twice. If phone control is wanted again, it should be a deliberate new capability with a stable identity, not a resurrection. `vibrate_phone` is client-side (`navigator.vibrate`) and comes free with the ported PWA. | none (deliberately) | EVIDENCE (ssh failures, §1) |
| **`kernel_*`, `bloom_*`, `graph_*`, `semsearch_query`, `hybrid_rag_search`, `cognee_ask`, `twenty_search_contacts`, `hermes_fleet`, `list_inbox_items`, `os_delegate`** | `server.py:708-1184` | **ALREADY PORTED** | All present in Pam's 43-tool surface (`ARCHITECTURE.md` §4; `check_tool_drift.py` output §5). Pam's set is a strict superset — it adds Notion, Gmail/Front/Calendar/Drive, web search, local memory, shell, focus. **`herdr_delegate` / `herdr_fleet` were previously bundled into this row and do not belong here — they are broken out below.** | `core/tools.py`, `core/kernel_tools.py`, `core/services.py`, `core/web.py` | EVIDENCE (drift check lists all 43) |
| **`herdr_delegate` + `herdr_fleet`** — phone-side spawn/read/steer of Claude Code lanes on Robin's **Mac**, over a restricted SSH shim | bridge: `server.py:139 run_herdr_ssh`, `:162 run_herdr_read`, `:621,835,856,1509`, key `/opt/voice-bridge/.ssh/herdr_mac` (0600, 411 bytes), landing on the Mac's `~/.ssh/authorized_keys` entry `voice-bridge-herdr` (`from="100.114.219.63",command=".../herdr-ssh-shim"`) | **RETIRE phone-side herdr *pane control* — and revoke the key** | **Correcting this document's earlier claim that it was "already ported": it is not.** The bridge reaches herdr *remotely* over SSH; the new stack reaches it *locally* — `core/tools.py:601 HERDR = os.path.expanduser("~/.local/bin/herdr")`, shelled at `:632`. On Thrivbe-1 `which herdr` is empty and `/root/.local/bin/herdr` does not exist, so `_herdr()` returns `None` and `_herdr_up()` (`:641`) is `False` for every herdr-backed tool on a server surface. **What actually degrades (more precisely than "silent no-op"):** `fleet` returns the spoken *"herdr isn't running, so there are no watchable tasks…"* (`:1206`) and `delegate(watch=True)` falls back to `_headless()` (`:1002`) — i.e. **delegating work from the phone survives, headless, via the kernel; watching or steering a pane does not.** Why retire rather than port the SSH shim into `server/`: **the live phone already has its own direct route that never touched the bridge** — Mac `authorized_keys` also carries `termux-to-mac`, `from="100.110.143.125"` (the *current* phone node, the one that answers `tailscale ping`) with the same `command=".../herdr-ssh-shim"`. The bridge's key is a second, redundant path, and porting it would put a private key to Robin's Mac on the server surface for reach the phone already has. If Pam-on-the-server should ever steer panes, the correct port is a `core.caps` **herdr-transport capability** (local-subprocess impl on `mac`, ssh impl on `server`) — not the current hardcoded `HERDR` path. Filed as parity gap #3. | none on the `server/` surface, deliberately · Mac surface unchanged (`core/tools.py`) · **revocation is an action item: delete the `voice-bridge-herdr` line from the Mac's `~/.ssh/authorized_keys` (runbook step 5b)** | EVIDENCE (`grep herdr /opt/voice-bridge/server.py` · `ls -la /opt/voice-bridge/.ssh` · `which herdr` exit 1 + `/root/.local/bin/herdr` exit 2 on T1 · Mac `authorized_keys` lines 4 and 6 · `herdr status --json` running 0.7.1 on the Mac) |
| **`teach_agent_tool` self-learning loop + `dynamic_registry.json`** | Advertised in `README.md`/`ROADMAP.md`; **not in `server.py`** — `grep` finds no `teach_agent_tool`; `dynamic_registry.json` is 3 bytes (`{}`) | **ALREADY RETIRED — delete the docs claim** | Dead feature still described as live in two README sections. Anyone reading the repo to decide what to port would try to port a ghost. | none | EVIDENCE (grep + `ls -la` on box) |
| **HTTPS certs** — `tailscale cert` Let's Encrypt keypair for `hetzner.tail9908c7.ts.net` | `/opt/voice-bridge/certs/{cert.crt,cert.key}` (`cert.key` is `0600`), issued 2026-06-27, `notAfter=Sep 21 08:38:30 2026 GMT` | **RETIRE — do not re-issue; front the new surface with `tailscale serve` instead** | **Decision: option (b).** The new surface binds plain HTTP on `127.0.0.1:8768` and is published as `tailscale serve --https=8452`, exactly the way the phone already reaches the bridge today on `:8451 → 127.0.0.1:8766`. Three reasons this wins over re-issuing into `/opt/voice-agent/certs`: (1) it deletes the file dependency, so `rm -rf /opt/voice-bridge` stops being a foot-gun instead of being a foot-gun someone has to remember; (2) **these files have no renewal timer** — issued by hand 2026-06-27, `notAfter=Sep 21 2026`, so option (a) means also owning a renewal cron nobody has written, and a silent TLS expiry is exactly the kind of failure this fleet keeps having; (3) it matches the transport the phone actually uses — the `:8765` direct-TLS listener exists for Command Center's server-to-server call, which is loopback-reachable anyway once CC is repointed (G5). Cost: uvicorn loses `--ssl-*`, which is one line. **Required edit before step 5: `server/voice-agent.service:35-36` currently hardcodes `--ssl-keyfile /opt/voice-bridge/certs/cert.key` / `--ssl-certfile …/cert.crt`.** Until those two lines are gone, `rm -rf /opt/voice-bridge` takes TLS away from the replacement — see the step-5 foot-gun list. | `server/voice-agent.service` + `tailscale serve` config; **no application code, and no key file** | EVIDENCE (`openssl x509 -noout -enddate`; no renewal timer in `systemctl list-timers --all`; `grep -n ssl server/voice-agent.service` → lines 35-36) |
| **`tailscaled-voicebridge.service`** — a *second* tailscaled identity (`--statedir=/var/lib/tailscale-voicebridge`, userspace networking, node `voice-bridge` = `100.84.255.84`, control socket `/var/run/tailscale-voicebridge.sock`) | `/etc/systemd/system/tailscaled-voicebridge.service` | **RETIRE last — jointly with the next row** | **The earlier "confirm nothing else uses node `voice-bridge`" is now answered: something does.** This daemon is not an idle spare identity — it publishes its own full HTTPS front door onto the bridge (next row), which answers `200` from any tailnet node today. So disabling it is *not* a tidy-up of a leftover daemon; it is one of the two ways the phone assistant can be switched off. It therefore moves out of step 3 and into the final step, together with its serve mapping. Removing it still also requires deleting node `voice-bridge` from the tailnet admin console, or it lingers as a stale device. | none | EVIDENCE (`tailscale --socket=/var/run/tailscale-voicebridge.sock status \| serve status` — §5; `self: voice-bridge ['100.84.255.84', …]`, 8 peers, one serve mapping) |
| **`tailscale serve` on node `voice-bridge`: `https://voice-bridge.tail9908c7.ts.net/ → 127.0.0.1:8766`** (port 443, tailnet-only, no Funnel) | serve config inside `--statedir=/var/lib/tailscale-voicebridge`, reachable only through `--socket=/var/run/tailscale-voicebridge.sock` (it is **invisible to a plain `tailscale serve status`**, which is why earlier passes missed it) | **RETIRE last — candidate phone-facing surface** | A second, complete HTTPS entry point to the same app as `:8451` — same upstream, different tailnet identity and hostname. Live: `curl https://voice-bridge.tail9908c7.ts.net/ping` → `200` from the Mac today. **Either this or `:8451` is what the installed PWA points at, and which one is UNVERIFIED (§1).** Because it is invisible to the default socket, it is the easiest surface in this whole inventory to remove by accident: `systemctl disable --now tailscaled-voicebridge` takes it down silently with no `tailscale serve status` line to warn anyone. Treated as phone-facing until §1's question is answered, and removed only in the final step. Same ordering rationale as `:8451`; `server/README.md` reclaims neither hostname (it takes `:8452`), so the old and new PWAs can coexist through the soak. | `server/` deploy config (new surface takes `tailscale serve --https=8452` on the **host** identity; the second identity is **not** recreated) | EVIDENCE (serve status via the alternate socket + `curl … /ping` → 200, both in §5) |
| **`tailscale serve --https=8451 → 127.0.0.1:8766`** (host identity, `hetzner.tail9908c7.ts.net`) | tailscale serve config (persistent, survives restarts); visible in the default `tailscale serve status` | **RETIRE last — candidate phone-facing surface** | The other of the two equivalent front doors. Live: `curl https://hetzner.tail9908c7.ts.net:8451/ping` → `200`. **Withdrawn from this row: the earlier claim that this is "the phone's actual entry point".** It may be; the previous row may be instead; §1 explains why the box cannot tell them apart. Removing *whichever one the phone uses* is what actually takes the assistant off the phone, so **both** are the last thing removed, after the 7-day soak — not in step 3. Note `server/README.md` picks its own ports (`8767` direct, optional `127.0.0.1:8768` behind `tailscale serve --https=8452`) rather than reclaiming `8451` — the safer choice: the two PWAs can be installed side by side during the soak, and `8451` is freed only once Robin has stopped using the old install. | `server/` deploy config | EVIDENCE (`tailscale serve status` + `curl … /ping` → 200, §5) |
| **Command Center `/api/voice/[action]` proxy** (`ask`, `ask-text`; keeps `VOICE_BRIDGE_TOKEN` server-side, streams SSE through) | `command-center/src/app/api/voice/[action]/route.ts` → `https://hetzner.tail9908c7.ts.net:8765` | **DEPENDENT — must be repointed, not deleted** | Consumed by `VoiceBriefController.tsx:133,189`, rendered by `OverviewDashboard.tsx:150` and `BriefingCard.tsx`. **`/today` itself is a `permanentRedirect("/inbox")`** (`app/today/page.tsx`) — the daily brief now lives on `/inbox`; the route comment naming "/today" is stale. If the bridge stops, the brief's voice button returns HTTP 502 "Voice bridge unreachable". | repoint `BRIDGE_BASE` at the `server/` surface | EVIDENCE (read both files) |
| **Command Center `/api/system-graph/chat`** (voice + text chat over the system map, `/ask` and `/ask-text`) | `command-center/src/app/api/system-graph/chat/route.ts:56,69,141` | **DEPENDENT — must be repointed** | Same bridge, different consumer. Breaks the same way. | repoint at `server/` | EVIDENCE (read) |
| **Command Center health tile** (`GET :8765/ping`, drives the "voice agent" up/down light) | `command-center/src/app/api/stats/route.ts:26` | **DEPENDENT — repoint or remove the tile** | Will show permanently red otherwise. | repoint or delete | EVIDENCE (read) |
| **CC OS-map + process descriptions entries** (`voice-bridge`, `voice-router` nodes) | `command-center/src/lib/os-map/spine.ts:227`, `src/lib/os/process-descriptions.ts:331,337` | **UPDATE** | Per the CC convention, every fleet process needs a `process-descriptions.ts` line; stale entries make the OS map lie. | CC source | EVIDENCE (read) |
| **`voice.sh`** — Termux CLI client (sox record → curl `:8765` → speak) | `/opt/voice-bridge/voice.sh` (and repo) | **RETIRE** | Superseded by the PWA years-of-UX ago; depends on the same broken Termux environment; `BRIDGE_URL` hardcodes `http://100.114.219.63:8765` over plain HTTP. | none | READ |
| **`SUBPROCESSORS.md`** — GDPR Art. 28/30 sub-processor register for *both* voice systems | `voice-bridge/SUBPROCESSORS.md` (last updated 2026-07-11) | **PORT + REWRITE (mandatory)** | It is already **factually wrong about the live system**: it names `freellmapi relays (localhost:8792 → :3004)` as the bridge's LLM path, but `server.py:79-81` has routed to OmniRoute `:20128` since 2026-07-30 and the `freellmapi` container is `not-found` (dead). A register that describes a retired data flow is worse than none. The rewrite must cover: OpenAI Realtime + Gemini Live as the two model sub-processors, ElevenLabs only if the turn fallback ships, OmniRoute's provider fan-out for any routed call, the removal of the free-relay/PII-pinning risk, and the local-storage section (`conversations.db`, `live-turns.journal`, the new action journal). It also carries three open verification items (ElevenLabs DPA, OpenAI zero-retention posture, Art. 13/14 if it ever goes multi-user) that must not be lost. | `voice-agent/SUBPROCESSORS.md` (new file, controller-level, covering Mac + `server/`) | EVIDENCE (compared doc against `server.py` and `fleet services`) |
| **`PRODUCT.md` multi-tenant seam analysis** (per-token auth, kernel-served persona, tool allowlist, PWA, confirm gate = the four properties a product needs) | `voice-bridge/PRODUCT.md` | **PORT (the reasoning, not the file)** | It is the clearest existing statement of why a *server* surface — not the Mac app — is the commercial artifact, and it names the four gaps (tenanting, onboarding, billing attribution, isolation). That belongs with `voice-agent/PRODUCT.md` / `GO-TO-MARKET.md`, otherwise the argument is lost with the repo. | `voice-agent/PRODUCT.md` | READ |
| **`os-ping-fail@<unit>` Sentry wiring** (drop-ins on every voice unit: `OnFailure=os-ping-fail@%n.service`, plus `PYTHONPATH=/usr/local/lib/thrivbe-sentry` + `THRIVBE_SENTRY_SERVICE=` on the Python ones) | `/etc/systemd/system/*.service.d/*.conf` | **PORT the pattern to the new units** | Fleet convention (added 2026-08-08: "a unit that fails silently is a fleet blind spot"), and it satisfies the standing cron rule — errors to Sentry, never Telegram. Any `server/` or repointed drift-check unit must carry the same drop-ins. | `server/` deploy config | EVIDENCE (dumped drop-ins from box) |
| **`unified-router.service` (`:8792`) + `freellm_router_mvp.py`** | Lives in `/opt/voice-bridge/`, but is **not a bridge capability** | **DO NOT TOUCH the unit — but know its backend is already dead** | `~/.zshrc:124 export CLAUDE_ROUTER_URL="http://100.114.219.63:8792"` — this is Robin's **Claude Code** router. It merely shares a working directory with the bridge, so removing `/opt/voice-bridge` without relocating it breaks Claude Code's configured router URL. It is also `systemctl is-enabled` → **disabled** (running, but will not return after a reboot). ⚠️ **New finding: it cannot currently serve a completion.** Its cmdline is `--api-base http://localhost:3004/v1`, and a full `ss -ltnp` shows **no listener on 3004 at all** (`freellmapi` container = `not-found`). `GET /v1/models` still returns 200 because the router answers that from its local `models.allowlist.real.json` — but `POST /v1/chat/completions` returns **HTTP 502 `{"error":{"message":"All compatible models failed: gpt-oss-20b: transient"}}`**. That changes what step 0 has to achieve: "relocate it so Claude Code keeps working" is a **false goal** — relocating a proxy to a dead upstream preserves nothing but the 200 on `/v1/models`. Step 0 must either repoint it at a live upstream (OmniRoute `:20128`) or Robin must unset `CLAUDE_ROUTER_URL`; a straight `cp -a` to `/opt/freellm-router` just moves the corpse. Deciding that is out of this document's scope (it is not a bridge capability) — but do not perform step 0 believing it restores anything. | none (relocate **and repoint**, or retire, on its own ticket) | EVIDENCE (`.zshrc` grep · `is-enabled` · `/proc/1414376/cmdline` · full `ss -ltnp` · live 502) |
| **`voice-router.service` (`:8793`) + `voice_router_mvp.py`, `models.allowlist.real.json`, `voice-router.env`** | `/opt/voice-bridge/`, `127.0.0.1:8793` | **RETIRE — its upstream is gone, so it cannot be serving anyone** | Decided, not deferred. `tr "\0" " " < /proc/1414378/cmdline` shows it running as `voice_router_mvp.py proxy --api-base http://localhost:3004/v1 … --port 8793`, i.e. a pure proxy in front of `freellmapi` on `:3004`. A **full** `ss -ltnp` (not a grep for pre-chosen ports) shows **nothing listens on 3004** on any address, and `fleet services thrivbe-1` reports `freellmapi docker :3004 freellmapi-freellmapi-1=not-found`. A live `curl` to `:3004/v1/models` gets connection-refused (exit 7). It is therefore incapable of answering a completion for any consumer, so the earlier "no consumer found but absence of a grep hit is not proof" objection is moot — even a consumer we failed to find is already getting nothing. It is additionally `is-enabled` → **disabled**, so it dies at the next reboot regardless. Note `:8793` does not even serve `/v1/models` (`{"error":"not found"}`); only `/` answers 200. Remove it with the rest in step 3; its files (`voice_router_mvp.py`, `models.allowlist.real.json`, `voice-router.env`) are captured by the step-4 tarball. **`voice-router.env` contains an API token — it is in the tarball, so the tarball stays `chmod 600` and off-box.** | none (deliberately) | EVIDENCE (`/proc/1414378/cmdline` · full `ss -ltnp` · `fleet services` · curl exit 7 on :3004 · 404 on :8793/v1/models) |
| **Local STT model assets** — `faster-whisper` weights: `medium` 1.5 G, `small` 464 M, `tiny` 75 M (**2.0 G total**) | `/root/.cache/huggingface/hub/models--Systran--faster-whisper-{tiny,small,medium}` — **outside `/opt/voice-bridge`**, so neither the step-4 tarball nor the step-5 `rm -rf` touches it | **KEEP — this is the asset behind row 1, and it must be kept deliberately, not by accident** | Row 1 recommends porting the cheap turn-based path, whose whole cost argument is that STT runs locally on CPU for free. That claim is only true because these weights are already on the box; re-downloading `small` on first use is 464 MB and a cold-start stall in the middle of a budget-cap fallback, which is the worst possible moment. So: **keep the cache, and make the new surface point at it explicitly** rather than relying on `HF_HOME` defaulting to `/root/.cache` under a root-run unit (`server/voice-agent.service` runs as root today; if it is ever de-privileged, the cache moves and the models silently re-download). Add `Environment=HF_HOME=/var/cache/thrivbe-voice/huggingface` (or keep `/root/.cache/huggingface` and say so) to the new unit, and `mv` the cache to match. **If Robin decides row 1 does not ship**, this row flips to RETIRE and step 5 reclaims 2.0 G with `rm -rf /root/.cache/huggingface/hub/models--Systran--faster-whisper-*` — that variant is written into the runbook as step 5c so the decision is not lost. Note the `medium` model (1.5 G of the 2.0 G) is not what the bridge loads: `server.py:438 WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")` and `.env` sets no override (`grep ^WHISPER_MODEL_SIZE /opt/voice-bridge/.env` → exit 1), so `small` is live and `tiny`/`medium` are leftovers — **1.6 G is reclaimable in either case.** | `core/caps` STT capability consumed by `core/backends/turn_backend.py`; cache location pinned in `server/voice-agent.service` | EVIDENCE (`du -sh /root/.cache/huggingface` = 2.0G; per-model `du -sh`; absent from the tar and rm paths) |
| **`os-voice-api.service` (`:8790`)** — the kernel's voice API: `/status`, `/persona`, `/session-log`, `/tools` | `/opt/Thrivbe-AI/projects/thrivbe-os`, `node --import tsx src/voice-api.ts`, `active` **and** `enabled` | **UNAFFECTED — no action, and do not confuse it for a bridge process** | Listed here for the same reason `VOICE_BUDGET_NOK` is: it is kernel-owned, it survives the decommission untouched, and it appeared in this document's earlier `ss -ltnp` evidence only as an unidentified `node` on `0.0.0.0:8790`. It is a *dependency of* the bridge, not a capability of it: `server.py:90 KERNEL_BASE_URL = "http://127.0.0.1:8790"` and `attention_push.py:23 STATUS_URL = "http://127.0.0.1:8790/status"` both point at it, and the new stack keeps using it via `core/kernel_tools.py`. Everything the ported rows rely on — persona, session cost metering, the high-stakes manifest, urgent-wake — comes from this service, so it must stay up through and after the decommission. Do not include `:8790` in any port-reclamation step. | `thrivbe-os` (stays where it is) | EVIDENCE (`systemctl is-active`/`is-enabled` = active/enabled · `/proc/1727991/cwd` → `thrivbe-os` · `ss -ltnp` pid 1727991) |
| **Server-side backup sprawl** — 11 `server.py.bak.*`, 2 `freellm_router_mvp.py.bak-*`, `index.html.bak.pre-pam`, `manifest.json.bak.pre-pam`, `voice_router_mvp.py.bak.pre-pam`, `router_decisions.jsonl` (2.3 MB) | `/opt/voice-bridge/` | **RETIRE (captured by the tar in §6)** | Hand-edited on-box history from before the repo was made canonical (`server.py:79-80` records that a stale deploy once clobbered an on-box fix). The tarball preserves it; nothing needs it live. | none | EVIDENCE (`ls -la`) |
| **Deploy divergence between repo and box** | repo `HEAD = d5aee98`, box `/opt/voice-bridge/RELEASE = d614e4b` | **RESOLVE BEFORE ARCHIVING** | The box is 2 commits behind the repo's release stamp, yet `attention_push.py` on the box is dated Aug 8 (the `d5aee98` Sentry change) — i.e. **partially deployed**. Do not assume the repo is a faithful record of what ran. Diff box↔repo during the archive step and commit any on-box-only change before tagging. | — | EVIDENCE (`cat RELEASE` + `git log`) |

---

## 3. Port/retire summary

> **Cross-worker note (2026-08-09 17:5x).** A parallel worker is building `voice-agent/server/`
> in this same working tree (uncommitted: `app.py`, `audio_ws.py`, `session.py`, PWA shell,
> `voice-agent.service`, `tests/test_server_{auth,surface}.py`). Where their `server/README.md`
> and this inventory disagree, **theirs is the live decision on how to build** — this
> document is the decommissioning view. Three places they interlock:
> - **certs** — their `voice-agent.service:35-36` reads `/opt/voice-bridge/certs` in place.
>   Row 21 decides against that (publish via `tailscale serve`), because otherwise the new
>   surface inherits an unrenewed keypair *and* blocks step 5. This is a request to them,
>   gated as **G7**, not a unilateral change to their unit.
> - **auth** — they renamed the secret to `VOICE_AGENT_TOKEN`; Command Center still sends
>   the bridge's. Repointing CC (G5) means swapping the secret, not just the URL.
> - **herdr** — their surface advertises the herdr-backed tools (`delegate`, `fleet`,
>   `continue_task`, `close_finished_tasks`) on `server` as well as `mac`, but `core/tools.py`
>   shells a **local** `herdr` binary that does not exist on Thrivbe-1. Row 19 decides this
>   as RETIRE-with-graceful-degradation rather than porting the bridge's SSH shim; if they
>   want pane control from the phone, it needs a `core.caps` transport seam.

**38 rows, every one carrying a decision.** No row says "investigate", "conflict" or "TBD".
The counts below sum to 38.

**Port (11):** turn-based cheap path · `/ask-text` · PWA client + `server/` surface (in
flight) · `X-Voice-Token` auth (done, renamed) · **PII no-train pin — conditional on the
turn fallback shipping, and binding on it if it does** · action audit journal + retention ·
log-transcript redaction default-off · Langfuse tracing · `os-ping-fail@` Sentry drop-ins ·
`SUBPROCESSORS.md` rewrite · **`PRODUCT.md`'s reasoning** (port the argument, not the file).

**Already ported, verify parity (5):** high-stakes gate (2 open gaps) · session cost
metering · the kernel/Bloom/search/CRM tool set (herdr no longer in this bundle) · kernel
persona — **ported; its Langfuse prompt-registry half is not, and that half is a port** ·
tool-drift check (repoint the timer).

**Keep, deliberately (1):** the 2.0 G `faster-whisper` cache at `/root/.cache/huggingface`
— it is what makes the cheap path cheap, and it sits outside everything the runbook
deletes. Flips to RETIRE (step 5c) if the turn fallback is not built. 1.6 G of it
(`tiny` + `medium`) is reclaimable either way.

**Retire (12):** `/live` Gemini relay · all seven phone/Termux tools · **phone-side
`herdr_delegate`/`herdr_fleet` pane control** (not ported, contrary to the earlier draft —
and the Mac authorized_keys entry must be revoked) · `attention_push.py` mechanism ·
`voice.sh` · **the TLS keypair — front the new surface with `tailscale serve` instead of
re-issuing** · **the three-part phone-facing bundle, removed LAST and together:
`tailscaled-voicebridge` + its own `voice-bridge.tail9908c7.ts.net` serve mapping + the
`:8451` serve mapping** (two equivalent front doors onto `127.0.0.1:8766`; which one the
installed PWA uses is UNVERIFIED, so both are treated as live phone surfaces) ·
**`voice-router` `:8793`** (its `:3004` upstream is gone) · `teach_agent_tool` remnants ·
on-box `.bak` sprawl.

**Unaffected — named so nobody "ports" or reclaims them (2):** `VOICE_BUDGET_NOK`
(kernel-owned cap) · `os-voice-api` `:8790` (the kernel voice API every ported row still
calls).

**Do not touch (2):** `unified-router` `:8792` — Robin's `CLAUDE_ROUTER_URL`, **but its
`:3004` backend is also already dead, so step 0 must repoint it, not just move it** ·
`langfuse-judge` on T2 (feed it before you cut its source).

**Must be repointed, not deleted (4 CC surfaces):** `/api/voice/[action]` ·
`/api/system-graph/chat` · `/api/stats` health tile · OS-map + process-descriptions
entries.

**Resolve before archiving (1):** the repo↔box deploy divergence (box `RELEASE=d614e4b`,
repo `HEAD=d5aee98`, partially deployed) — step 4a.

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
- **The PII no-train pin ships with it — this is a condition of the recommendation, not a
  caveat on it.** This recommendation routes through OmniRoute `:20128`, which is a
  provider fan-out. So row 9 is decided **PORT, conditional on this row shipping**: any
  turn that touched a `PII_TOOLS` tool must be pinned to a single named no-train model,
  never an `auto/*` combo. A cheap path that fans out to training relays is not a saving,
  it is a GDPR regression. Do **not** port the literal string `router-safe` — that id
  belonged to the retired `freellmapi`/`unified-router` stack and OmniRoute rejects it
  today with `HTTP 400` (see row 9); pick a named model that OmniRoute actually serves and
  write it into `SUBPROCESSORS.md`.
- **The local STT weights are a prerequisite, not an implementation detail.** The "free"
  half of the cost argument is `faster-whisper small` running on Thrivbe-1's CPU, which is
  only free because 2.0 G of weights already sit in `/root/.cache/huggingface` — outside
  `/opt/voice-bridge` and outside every step of the runbook. Row 35 keeps them and pins
  the cache path in the new unit.

### Open questions for Robin (blocking full decommission)

- **O1 — Does the phone keep a voice assistant?** If yes, `server/` is a prerequisite for
  decommissioning, not a follow-up. If no, the PWA/auth/serve-mapping rows (23, 24)
  collapse to RETIRE and this becomes a much smaller job.
- **O5 — Which URL is the installed PWA on the phone actually pointing at?** One question,
  ten seconds: open the app and read the origin. It is either
  `https://voice-bridge.tail9908c7.ts.net` (row 23) or
  `https://hetzner.tail9908c7.ts.net:8451` (row 24) — both are live and both proxy to the
  same `127.0.0.1:8766`, and **nothing readable on the box distinguishes them** (§1, §5:
  uvicorn logs the peer IP, not the `Host`). This is not idle curiosity: it decides which
  surface the replacement PWA must be installed from, and whether deleting the
  `voice-bridge` tailnet node in step 6e is a cleanup or a forced reinstall. If Robin is
  unavailable, the fallback is to log the `Host` header on `voice-bridge-serve` for the
  duration of the soak. Until answered, **both** are treated as phone-facing and the
  runbook removes them together, last. An earlier draft asserted `:8451` was the phone's
  path; that assertion had no evidence behind it and has been withdrawn.
- **O2 — Does anything still push to the phone?** `attention_push` is dead and Pam's
  urgent-wake only reaches the Mac. Cheapest replacement is kernel-side (the notifications
  feed + Telegram for the genuinely urgent), not ssh-to-Termux.
- **O3 — CLOSED.** *Was:* "is `voice-router` `:8793` anyone's dependency?" It cannot be:
  it is a proxy to `localhost:3004`, nothing listens on 3004, and `freellmapi` is
  `not-found`. Decided RETIRE in row 34 without needing a consumer search. **A new
  question falls out of it:** `unified-router` `:8792` — Robin's `CLAUDE_ROUTER_URL` —
  proxies to the *same* dead upstream and returns `502` on completions. Step 0 must
  repoint or retire it; "relocate it so Claude Code keeps working" is not achievable by
  relocation alone. That is a separate ticket, not a decommissioning blocker.
- **O4 — Does the phone still need herdr pane control?** Row 19 retires the bridge's SSH
  shim on the grounds that the live phone node already has its own direct
  `termux-to-mac` → `herdr-ssh-shim` entry in the Mac's `authorized_keys`. Confirm Robin
  actually uses that path before the bridge's key is revoked; if he does not, phone-side
  pane control ends here and only headless kernel delegation remains.

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
    (…plus :3000, :443 Funnel, :8443, :8444, :8446, :8450, :9119 — none of them the bridge)

# ── rows 22-23: THE SECOND FRONT DOOR. The command above does NOT show it. ──
# `tailscaled-voicebridge` keeps its own state and its own serve config behind a separate
# control socket, so a plain `tailscale serve status` is blind to it. Use --socket:
$ fleet run thrivbe-1 'tailscale --socket=/var/run/tailscale-voicebridge.sock status; \
                       echo "=== SERVE(vb) ==="; \
                       tailscale --socket=/var/run/tailscale-voicebridge.sock serve status'
100.84.255.84    voice-bridge        robin@          linux    -
100.114.219.63   hetzner             tagged-devices  linux    -
100.110.143.125  robin-t-1           robin@          android  -
100.127.4.13     robin-t             robin@          android  offline, last seen 5d ago
…
=== SERVE(vb) ===
https://voice-bridge.tail9908c7.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:8766

$ fleet run thrivbe-1 'tailscale --socket=/var/run/tailscale-voicebridge.sock status --json | …'
self: voice-bridge ['100.84.255.84', 'fd7a:115c:a1e0::2a3b:ff55']    peers: 8

    ⇒ This answers the question row 22 previously left open ("is anything else using node
      voice-bridge?"). Yes: the node publishes a complete HTTPS front door onto the bridge
      app. `systemctl disable --now tailscaled-voicebridge` therefore removes a live phone
      surface, not just an idle daemon — which is why it left step 3 for step 6.

# ── both front doors are live RIGHT NOW; run from the Mac, on the tailnet ──
$ curl -s -m 10 -o /dev/null -w "vb-node:%{http_code}\n" https://voice-bridge.tail9908c7.ts.net/ping
vb-node:200      exit=0
$ curl -s -m 10 -o /dev/null -w "8451:%{http_code}\n" https://hetzner.tail9908c7.ts.net:8451/ping
8451:200         exit=0

# ── what CANNOT be determined from the box: which door the phone uses ──
$ fleet run thrivbe-1 'journalctl -u voice-bridge-serve --since "7 days ago" | grep -E "GET|POST" | tail'
Aug 07 18:50:27 uvicorn[2290470]: INFO: 100.110.143.125:0 - "GET / HTTP/1.1" 200 OK
Aug 07 19:05:19 uvicorn[2290470]: INFO: 100.110.143.125:0 - "POST /ask-text HTTP/1.1" 200 OK
    ⇒ peer IP only, no Host header ⇒ does not discriminate between the two serve paths.
      The `voice-bridge` socket emits no per-peer byte counters either, so traffic
      attribution is not decidable from counters. Resolve it per §1 (a) ask Robin or
      (b) log the Host header during the soak. Marked UNVERIFIED until then.

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
```

### Round-2 evidence (the five decisions that were previously deferred)

```
# ── row 34 / row 33: both routers proxy to an upstream that no longer exists ──
$ fleet run thrivbe-1 'for p in $(pgrep -f "router_mvp.py"); do tr "\0" " " < /proc/$p/cmdline; echo; done'
/usr/bin/python3 /opt/voice-bridge/freellm_router_mvp.py proxy --host 0.0.0.0 \
    --api-base http://localhost:3004/v1 --allowlist …/models.allowlist.real.json --port 8792 …
/usr/bin/python3 /opt/voice-bridge/voice_router_mvp.py   proxy \
    --api-base http://localhost:3004/v1 --allowlist …/models.allowlist.real.json --port 8793 …
    (--api-token values redacted here; they are real credentials)

$ fleet run thrivbe-1 'ss -ltnp'        # FULL listing, 49 lines, not a grep for chosen ports
    → 8765 uvicorn · 8766 uvicorn · 8790 node · 8792 python3 · 8793 python3 · 20128 docker-proxy
    → **no listener on 3004 on any address**
$ fleet services thrivbe-1 | grep freellmapi
freellmapi  docker  :3004  freellmapi-freellmapi-1=not-found

$ fleet run thrivbe-1 'curl -s -m5 -o /dev/null -w "%{http_code}\n" http://localhost:3004/v1/models'
000   (curl exit 7 — connection refused)
$ … POST http://127.0.0.1:8792/v1/chat/completions  (model=auto)
{"error":{"message":"All compatible models failed: gpt-oss-20b: transient","type":"router_error"}}   HTTP=502
$ … GET  http://127.0.0.1:8792/v1/models            → 200  (served from the local allowlist FILE)
$ … GET  http://127.0.0.1:8793/v1/models            → {"error":"not found"}   (404)
$ … GET  http://127.0.0.1:8793/                     → 200
    ⇒ row 34 RETIRE (dead upstream, cannot serve anyone) · row 33 keeps DO-NOT-TOUCH on the
      unit but records that relocation alone restores nothing.

# ── row 9: the PII pin is already broken in production ──
$ fleet run thrivbe-1 'sed -n "2343,2349p" /opt/voice-bridge/server.py'
    if pii_seen: model = "router-safe"   … print("[PRIVACY] PII tool used -> routing follow-up to router-safe")
$ fleet run thrivbe-1 'curl … http://localhost:20128/v1/models > /tmp/om.json; wc -c; grep -c router-safe'
742803 bytes, 2038 model ids, **0 matches for "router-safe"**
$ fleet run thrivbe-1 'curl … -X POST :20128/v1/chat/completions -d {"model":"router-safe",…}'
{"error":{"message":"Unable to determine provider for model 'router-safe'. …","code":"bad_request"}}  HTTP=400
$ fleet run thrivbe-1 'sed -n "2378,2380p" /opt/voice-bridge/server.py'
            except Exception:  tracer.end_span(generation_span);  raise      ← no model fallback
    ⇒ fails CLOSED (the turn dies, nothing leaks) but has protected nobody since the
      2026-07-30 OmniRoute cutover. Port the intent, never the string.

# ── row 19: herdr is NOT ported ──
$ fleet run thrivbe-1 'grep -n herdr /opt/voice-bridge/server.py | head'
139:def run_herdr_ssh(...)   145: "ssh","-i","/opt/voice-bridge/.ssh/herdr_mac", …
$ fleet run thrivbe-1 'ls -la /opt/voice-bridge/.ssh'
-rw------- 1 root root 411 Aug  2 18:15 herdr_mac          ← private key to Robin's Mac
$ grep -n "^HERDR\|def _herdr_up" voice-agent/core/tools.py
601:HERDR = os.path.expanduser("~/.local/bin/herdr")     641:def _herdr_up() -> bool:
$ fleet run thrivbe-1 'which herdr; ls -la /root/.local/bin/herdr'
(no output, exit 1)   ls: cannot access …: No such file or directory (exit 2)
$ grep -n " voice-bridge-herdr$\| termux-to-mac$" ~/.ssh/authorized_keys      # on the Mac
4: from="100.114.219.63",command=".../herdr-ssh-shim",… voice-bridge-herdr    ← the bridge's key
6: from="100.110.143.125",command=".../herdr-ssh-shim",… termux-to-mac        ← the LIVE phone, direct
$ ~/.local/bin/herdr status --json | head -c 120
{"client":{"version":"0.7.1",…},"server":{"status":"running","running":true,…
    ⇒ the phone already reaches herdr without the bridge; retire the bridge's shim + revoke key.

# ── row 21: TLS — no renewal timer anywhere, and the new unit hardcodes the old path ──
$ fleet run thrivbe-1 'systemctl list-timers --all --no-pager | grep -icE "cert|renew|acme|letsencrypt"'
0        (out of 80 timer lines)
$ fleet run thrivbe-1 'grep -rn "tailscale cert" /etc/cron* /etc/systemd/system'   → no output
$ grep -n ssl voice-agent/server/voice-agent.service
35:    --ssl-keyfile /opt/voice-bridge/certs/cert.key \
36:    --ssl-certfile /opt/voice-bridge/certs/cert.crt \
    ⇒ RETIRE the keypair; publish via `tailscale serve` (option b) and delete lines 35-36.

# ── row 35: the STT assets behind the cheap path ──
$ fleet run thrivbe-1 'du -sh /root/.cache/huggingface; du -sh /root/.cache/huggingface/hub/*'
2.0G  /root/.cache/huggingface
1.5G  models--Systran--faster-whisper-medium   464M  …-small   75M  …-tiny
$ fleet run thrivbe-1 'grep -n WHISPER_MODEL_SIZE /opt/voice-bridge/server.py'
438:WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
$ fleet run thrivbe-1 'grep "^WHISPER_MODEL_SIZE" /opt/voice-bridge/.env'   → exit 1 (no override)
    ⇒ `small` is live; tiny+medium (1.6G) are reclaimable either way.

# ── row 36: os-voice-api is the kernel's, and it is the dependency everything else has ──
$ fleet run thrivbe-1 'systemctl is-active os-voice-api; systemctl is-enabled os-voice-api'
active
enabled
$ fleet run thrivbe-1 'ls -l /proc/1727991/cwd; tr "\0" " " < /proc/1727991/cmdline'
/proc/1727991/cwd -> /opt/Thrivbe-AI/projects/thrivbe-os
/usr/bin/node --import tsx src/voice-api.ts
$ fleet run thrivbe-1 'sed -n "20,33p" /opt/voice-bridge/attention_push.py'
23:STATUS_URL = "http://127.0.0.1:8790/status"      25:SSH_COMMAND = [ …  33:"u0_a634@100.127.4.13",
    ⇒ UNAFFECTED row; also fixes this document's earlier `attention_push.py:25` citation —
      the list opens at :25, the stale phone address literal is at :33.
```

**Production untouched in round 2 as well.** Every command above is a read, a `curl`, or a
`/proc` inspection. No `systemctl stop|disable|restart`, no writes under `/opt`, no
`tailscale serve` changes. The one live `POST` sent was a 5-token completion to two routers
(one 502, one 400) — no side effects and no tool execution.

```
$ .venv/bin/python -m pytest --rootdir <voice-agent> tests -q > out.txt 2>&1 ; echo "PYTEST_EXIT=$?"
PYTEST_EXIT=0
    243 collected, 243 dots, 0 failures (round 2, after the parallel worker's server/ landed;
    round 1 saw 196 — the delta is their new tests, not anything this document touched).
    pytest.ini sets `addopts = -q`, which suppresses the summary line; the count comes from
    `--collect-only -q` summed per file. This document changes no code, so the run is a
    regression guard, not evidence for any claim in the table.
    (One round-1 run failed test_memory_store.py::test_concurrent_records_not_dropped —
     "a write was dropped: [0,1,2,3,4]" — and passed on re-run. Flaky, pre-existing,
     unrelated to this document. Worth its own ticket. It did not recur in round 2.)
```

---

## 6. Decommission runbook — **TO BE EXECUTED LATER, NOT NOW**

Do not start until **all** of these hold:

- **G1** — O1 answered. If the phone keeps an assistant, `server/` is live on its **own**
  surface — `tailscale serve --https=8452 → 127.0.0.1:8768` per `server/README.md`, *not*
  a reuse of `:8451` — and Robin has used it for a week without falling back to the
  bridge. Running side by side is the point: it is what makes the old front doors safe to
  leave up until step 6.
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
- **G7** — `server/voice-agent.service` no longer references `/opt/voice-bridge` at all.
  Per row 21 that means the two `--ssl-*` lines (35-36) are gone and the surface is
  published via `tailscale serve --https=8452 → 127.0.0.1:8768`. Verify with
  `fleet run thrivbe-1 'grep -c /opt/voice-bridge /etc/systemd/system/voice-agent.service'`
  → **0**, and `curl -sk https://hetzner.tail9908c7.ts.net:8452/ping` from the tailnet.
- **G8** — Row 35 answered: if the turn fallback ships, the Whisper cache path is pinned in
  `voice-agent.service` and a cold `POST /ask` has been shown to transcribe **without**
  downloading (`journalctl -u voice-agent | grep -i "Loading Whisper"` present, no
  huggingface download lines). If it does not ship, step 5c is authorised.
- **G9** — O5 answered, or the `Host`-header log is running. Step 6 removes **both**
  tailnet front doors, so an unanswered O5 does not block reaching step 6 — but it does
  block 6e (deleting the `voice-bridge` node from the admin console), because that is the
  one action that cannot be undone if the PWA turns out to have used that hostname.
  Verify the log is capturing it, if that is the route taken:
  `fleet run thrivbe-1 'journalctl -u voice-bridge-serve --since "1 hour ago" | grep -c Host='`
  → non-zero once the phone has been used.

Every step below is run through `fleet` (`fleet run thrivbe-1 '<cmd>'`), never a
hand-composed ssh pipeline.

### Step 0 — Relocate the co-tenants (MUST be first; breaks Claude Code otherwise)

⚠️ **Read row 33 first: `unified-router` is not merely misplaced, it is broken.** Its
`--api-base http://localhost:3004/v1` upstream (`freellmapi`) is gone, so a `cp -a` to a new
prefix preserves a 200 on `/v1/models` (served from the local allowlist file) and nothing
else. Relocation is necessary but **not sufficient** — decide with Robin whether to repoint
it at OmniRoute `:20128` or drop `CLAUDE_ROUTER_URL` from `~/.zshrc` entirely.

```
# 0a. PROVE the current state before touching anything (this is what makes the decision):
fleet run thrivbe-1 'tr "\0" " " < /proc/$(pgrep -f freellm_router_mvp.py)/cmdline; echo'
fleet run thrivbe-1 'ss -ltnp | grep -c ":3004 "'          # expect 0 — upstream is dead
fleet run thrivbe-1 'curl -s -m 15 -o /dev/null -w "%{http_code}\n" -X POST \
    http://127.0.0.1:8792/v1/chat/completions -H "Content-Type: application/json" \
    -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}"'
    # observed 2026-08-09: 502 "All compatible models failed"

# 0b. Move it out of /opt/voice-bridge (necessary):
fleet run thrivbe-1 'mkdir -p /opt/freellm-router && \
  cp -a /opt/voice-bridge/freellm_router_mvp.py /opt/voice-bridge/models.allowlist.real.json \
        /opt/voice-bridge/voice-router.env /opt/freellm-router/'
fleet run thrivbe-1 'chmod 600 /opt/freellm-router/voice-router.env'   # it holds an API token
# 0c. Rewrite unified-router.service WorkingDirectory/ExecStart/EnvironmentFile to /opt/freellm-router
#     AND, per the decision above, its --api-base. Then:
# 0d. systemctl daemon-reload && systemctl enable --now unified-router  ← also fixes is-enabled=disabled

# 0e. VERIFY the thing that actually matters — a COMPLETION, not /v1/models (do not proceed
#     until this is 200; a 200 on /v1/models proves only that a JSON file is readable):
fleet run thrivbe-1 'curl -s -m 30 -o /dev/null -w "%{http_code}\n" -X POST \
    http://127.0.0.1:8792/v1/chat/completions -H "Content-Type: application/json" \
    -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}"'
# 0f. From the Mac, same probe against the tailnet address in ~/.zshrc:124:
curl -s -m 30 -o /dev/null -w "%{http_code}\n" -X POST \
    http://100.114.219.63:8792/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model":"auto","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'

# 0g. voice-router (:8793) needs NO relocation — row 34 retires it (dead upstream, disabled,
#     no reachable consumer). Leave it running; it is removed in step 3 with the rest.
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
fleet run thrivbe-1 'systemctl stop voice-router'      # row 34 — dead upstream, already disabled
fleet run thrivbe-1 'systemctl is-active voice-bridge voice-bridge-serve voice-router \
                                          attention-push.timer tool-drift-check.timer'
   # expect: inactive ×5
   # NB: unified-router (:8792) is NOT in this list — step 0 moved it out. Confirm:
fleet run thrivbe-1 'systemctl is-active unified-router'    # expect: active
```

**Soak here for 7 days.** Stopped-but-present is the cheap rollback: `systemctl start
voice-bridge voice-bridge-serve` restores service in seconds. Watch for: Command Center
502s, a missing Monday 08:00 drift check, anything asking for the phone assistant.

### Step 3 — Disable (survives reboot). **The phone-facing surfaces are NOT touched here.**

```
fleet run thrivbe-1 'systemctl disable --now attention-push.timer attention-push.service \
                                            tool-drift-check.timer tool-drift-check.service \
                                            voice-bridge voice-bridge-serve voice-router'
fleet run thrivbe-1 'systemctl is-enabled voice-bridge voice-bridge-serve voice-router \
                                          attention-push.timer tool-drift-check.timer'
   # expect: disabled ×5
# GATE — prove the phone surfaces are still intact after this step:
fleet run thrivbe-1 'systemctl is-active tailscaled-voicebridge'     # expect: active
fleet run thrivbe-1 'tailscale serve status | grep 8451'             # expect: still mapped
fleet run thrivbe-1 'tailscale --socket=/var/run/tailscale-voicebridge.sock serve status'
   # expect: https://voice-bridge.tail9908c7.ts.net → 127.0.0.1:8766 still mapped
```

⚠️ **An earlier draft of this runbook ran `tailscale serve --https=8451 off` and
`systemctl disable --now tailscaled-voicebridge` inside this step. Both have moved to
step 6.** The reason: there are **two** equivalent tailnet front doors onto
`127.0.0.1:8766` (§1) and it is UNVERIFIED which one the installed PWA points at.
Removing either here would take the phone assistant down one step earlier than this
document claims, and — for the `voice-bridge` node — silently, since its mapping does not
appear in a default `tailscale serve status`. Both are treated as live phone surfaces and
are the last thing removed.

⚠️ Note the rollback boundary honestly: step 2 already stopped the app, so the phone is
without an assistant from step 2 onward. What steps 3-5 must not do is make that
**irreversible** for whichever door the phone actually uses — the front doors and the
second tailnet identity stay recreatable-for-free until step 6.

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
fleet run thrivbe-1 'chmod 600 /root/voice-bridge-decommission-*.tar.gz'
#     ⚠️ chmod 600 is not optional: this tarball contains `.env`, `voice-router.env` (API
#     token), `certs/cert.key`, and `.ssh/herdr_mac` (a private key to Robin's Mac).
#     Confirm the two keys made it in — they are the things nothing else preserves:
fleet run thrivbe-1 'tar -tzf /root/voice-bridge-decommission-*.tar.gz | grep -E "certs/cert.key|\.ssh/herdr_mac|\.env$"'
#     The 2.0 G huggingface cache is deliberately NOT in the tar (rebuildable from the Hub,
#     and row 35 keeps it in place rather than archiving it).
# 4c. Copy off-box to the offsite backup target used by os-db-offsite-backup, and verify
#     the checksum matches on both ends before step 5.
```

### Step 5 — Remove from the box

```
# NOTE: tailscaled-voicebridge.service is deliberately ABSENT from these three lines —
# it still publishes a live phone-facing front door (§1) and is removed in step 6.
fleet run thrivbe-1 'rm -f /etc/systemd/system/{voice-bridge,voice-bridge-serve,voice-router,attention-push,tool-drift-check}.service \
                           /etc/systemd/system/{attention-push,tool-drift-check}.timer'
fleet run thrivbe-1 'rm -rf /etc/systemd/system/{voice-bridge,voice-bridge-serve,voice-router,attention-push,tool-drift-check}.service.d'
fleet run thrivbe-1 'rm -f /etc/logrotate.d/voice-bridge'
fleet run thrivbe-1 'systemctl daemon-reload && systemctl reset-failed'
fleet run thrivbe-1 'systemctl is-active tailscaled-voicebridge'   # expect: active — still up
```

**⚠️ `rm -rf /opt/voice-bridge` deletes THREE in-place dependencies that live nowhere else.**
Each is a file other things point at, not application code the repo can regenerate. Clear
all three before the `rm`, and prove it with the gate below.

| in-place dependency | who points at it | cleared by |
|---|---|---|
| `/opt/voice-bridge/certs/{cert.crt,cert.key}` | `server/voice-agent.service:35-36` (`--ssl-keyfile`/`--ssl-certfile`) | **G7** — row 21 drops TLS from uvicorn in favour of `tailscale serve --https=8452` |
| `/opt/voice-bridge/.ssh/herdr_mac` | the bridge's SSH shim to Robin's Mac; the matching `voice-bridge-herdr` line in the Mac's `~/.ssh/authorized_keys` **outlives the delete and stays authorised** | **step 5b** — revoke on the Mac, then delete on the server |
| `/opt/voice-bridge/{freellm_router_mvp.py,models.allowlist.real.json,voice-router.env}` | `unified-router.service` (`:8792`, Robin's `CLAUDE_ROUTER_URL`) | **step 0b/0c** — relocated to `/opt/freellm-router` |

```
# 5a. GATE — nothing outside /opt/voice-bridge may still reference it:
fleet run thrivbe-1 'grep -rln "/opt/voice-bridge" /etc/systemd/system /etc/logrotate.d /opt/freellm-router 2>/dev/null'
#     expect: NO output (exit 1). If voice-agent.service still appears, G7 is not met — stop.

# 5b. Revoke the bridge's key to Robin's Mac BEFORE deleting the private half, so the
#     window where an authorised key has no known holder never opens.
#     On the Mac (not via fleet — this is Robin's laptop):
cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-$(date +%Y%m%d)
grep -v " voice-bridge-herdr$" ~/.ssh/authorized_keys > /tmp/ak && mv /tmp/ak ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
grep -c " voice-bridge-herdr$" ~/.ssh/authorized_keys        # expect 0 (exit 1)
#     Prove the revocation from the server BEFORE the rm (expect Permission denied / exit 255):
fleet run thrivbe-1 'ssh -o BatchMode=yes -o StrictHostKeyChecking=no \
    -i /opt/voice-bridge/.ssh/herdr_mac robinsverd@<mac-tailscale-ip> "agent list"; echo "exit=$?"'
#     Also confirm the phone's own path is untouched if O4 says Robin uses it:
grep -c " termux-to-mac$" ~/.ssh/authorized_keys              # expect 1

# 5c. Model cache — branch on row 35 / G8:
#     If the turn fallback SHIPPED: keep the cache, and drop only the unused sizes (1.6 G):
fleet run thrivbe-1 'rm -rf /root/.cache/huggingface/hub/models--Systran--faster-whisper-tiny \
                            /root/.cache/huggingface/hub/models--Systran--faster-whisper-medium'
#     If the turn fallback did NOT ship: reclaim all 2.0 G:
fleet run thrivbe-1 'rm -rf /root/.cache/huggingface/hub/models--Systran--faster-whisper-*'
fleet run thrivbe-1 'du -sh /root/.cache/huggingface'

# 5d. Only now:
#     NOTE: /var/lib/tailscale-voicebridge is deliberately NOT deleted here — it is the
#     second identity's statedir and holds its serve config. It goes in step 6.
fleet run thrivbe-1 'rm -rf /opt/voice-bridge'
# VERIFY:
fleet run thrivbe-1 'ls /opt/voice-bridge 2>&1; systemctl --failed --no-pager'
   # expect: "No such file or directory", no failed units
   # (`systemctl list-units | grep -ci voice-bridge` is NOT 0 yet — tailscaled-voicebridge
   #  is still up on purpose. It reaches 0 after step 6.)
fleet run thrivbe-1 'systemctl is-active unified-router os-voice-api'   # expect: active active
fleet status thrivbe-1
```

### Step 6 — **LAST production action:** remove BOTH phone-facing front doors

Nothing before this point is irreversible for the phone: until now, `systemctl start
voice-bridge-serve` + the untouched serve mappings put the assistant back. This step is
the point of no return for phone reach, so it runs **only after** the 7-day soak from
step 2 **and** after G1 (`server/` owns the replacement surface) **and** after §1's
open question is answered.

**Pre-flight — answer §1 first.** Which door the installed PWA uses is UNVERIFIED. Do not
guess; either (a) ask Robin to open the app and read the origin, or (b) read it off the
Host header logged during the soak. If it is still unknown when this step is reached, that
is fine — this step removes **both**, so the ordering is safe either way; what is *not*
safe is removing one of them earlier, in the hope that it was the unused one.

```
# 6a. The host identity's mapping (hetzner.tail9908c7.ts.net:8451):
fleet run thrivbe-1 'tailscale serve --https=8451 off'
fleet run thrivbe-1 'tailscale serve status | grep 8451; echo "exit=$?"'   # expect: no match, exit 1
#     Everything else in the default serve config must be untouched — 8 other mappings:
fleet run thrivbe-1 'tailscale serve status'
   # expect: :3000, :443 Funnel, :8443, :8444, :8446, :8450, :9119 all still present

# 6b. The second identity's own mapping (voice-bridge.tail9908c7.ts.net) — invisible to the
#     command above; it MUST be turned off through its own socket, before the daemon dies,
#     or it survives in the statedir and comes back if anyone ever restarts the unit:
fleet run thrivbe-1 'tailscale --socket=/var/run/tailscale-voicebridge.sock serve --https=443 off'
fleet run thrivbe-1 'tailscale --socket=/var/run/tailscale-voicebridge.sock serve status'
   # expect: empty

# 6c. Prove BOTH doors are shut from the tailnet (run from the Mac, not via fleet):
curl -s -m 10 -o /dev/null -w "vb-node:%{http_code}\n" https://voice-bridge.tail9908c7.ts.net/ping
curl -s -m 10 -o /dev/null -w "8451:%{http_code}\n"    https://hetzner.tail9908c7.ts.net:8451/ping
   # expect: connection failure / non-200 on both (they were 200/200 before decommissioning)

# 6d. Only now the daemon, its unit, and its statedir:
fleet run thrivbe-1 'systemctl disable --now tailscaled-voicebridge'
fleet run thrivbe-1 'rm -f /etc/systemd/system/tailscaled-voicebridge.service'
fleet run thrivbe-1 'rm -rf /etc/systemd/system/tailscaled-voicebridge.service.d \
                            /var/lib/tailscale-voicebridge /var/run/tailscale-voicebridge.sock'
fleet run thrivbe-1 'systemctl daemon-reload && systemctl reset-failed'
fleet run thrivbe-1 'systemctl list-units --all | grep -ci voice-bridge'   # expect: 0
fleet run thrivbe-1 'systemctl --failed --no-pager'                        # expect: none

# 6e. Delete node "voice-bridge" (100.84.255.84) from the tailnet admin console — a removed
#     daemon still leaves a stale device, and the hostname stays claimed. Then:
fleet run thrivbe-1 'tailscale status | grep -c voice-bridge'   # expect: 0
#     Sanity: the host identity itself must be unaffected.
fleet run thrivbe-1 'tailscale status | head -3'
fleet status thrivbe-1
```

**Rollback for this step only** (if Robin finds the phone still needed it): re-create the
mapping on the host identity — `tailscale serve --https=8451 http://127.0.0.1:8766` — and
`systemctl start voice-bridge-serve`. The `voice-bridge` node and hostname are **not**
recoverable once deleted from the admin console; a new identity would have to be created,
and the PWA reinstalled from the new origin. That asymmetry is the reason 6e is last.

### Step 7 — Archive the repo

```
# The repo has NO remote — archiving means creating one, or it exists only on this Mac.
git -C /Users/robinsverd/Thrivbe-AI/projects/voice-bridge tag -a decommissioned-$(date +%Y%m%d) \
    -m "voice-bridge decommissioned; capabilities ported per voice-agent/docs/BRIDGE-DECOMMISSION.md"
gh repo create thrivbe/voice-bridge --private --source=. --push   # from the repo dir
gh repo archive thrivbe/voice-bridge --yes
# Then move the working copy out of the active projects tree:
mv /Users/robinsverd/Thrivbe-AI/projects/voice-bridge /Users/robinsverd/Thrivbe-AI/lab/archive/voice-bridge
```

### Step 8 — Update the record

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
- Mac: keep `~/.ssh/authorized_keys.bak-*` from step 5b until the soak is over, then delete
  it — it still contains the revoked `voice-bridge-herdr` public key.
- If the turn fallback shipped, record the pinned Whisper cache path and the pinned no-train
  model id in `voice-agent/SUBPROCESSORS.md` — those two lines are the whole GDPR argument
  for the cheap path.
- Close the loop publicly per the workspace habit: draft the labs entry at
  `projects/thrivbe-clone/content/labs/`.

### Rollback

- Before step 3: `systemctl start voice-bridge voice-bridge-serve` — seconds. Both front
  doors are still mapped, so whichever one the phone uses comes straight back.
- Before step 5: `systemctl enable --now …` + `systemctl start voice-bridge-serve` —
  minutes. **No `tailscale serve` command is needed**: step 3 no longer removes either
  mapping, so both are still in place. (An earlier draft told you to re-add `:8451` here,
  which was the tell that step 3 had taken it away too early.)
- Before step 6: the box is stripped but the phone path is still *recreatable* — restore
  per the next bullet, then `systemctl start voice-bridge-serve`; the mappings and the
  `voice-bridge` node were never touched.
- After step 6: the `:8451` mapping is re-creatable in one command
  (`tailscale serve --https=8451 http://127.0.0.1:8766`), but the `voice-bridge` tailnet
  node deleted in 6e is **not** — a new identity must be created and the PWA reinstalled
  from the new origin. If the PWA turns out to have used that hostname, this is a
  reinstall, not a rollback.
- After step 5: restore the step-4 tarball to `/opt/voice-bridge`, recreate the venv from
  `requirements.txt`, reinstall the unit files — ~30 minutes, and only if the tarball was
  verified off-box. **Three things the tarball alone does not restore:** (1) the
  `voice-bridge-herdr` line in the Mac's `~/.ssh/authorized_keys` — re-add it from
  `~/.ssh/authorized_keys.bak-*` (step 5b), or the restored bridge's herdr tools stay dead;
  (2) the Whisper weights, if step 5c deleted them — first `/ask` re-downloads 464 MB and
  stalls; (3) the certs are restorable from the tar but expire **2026-09-21** with no
  renewal timer, so a rollback after that date needs `tailscale cert hetzner.tail9908c7.ts.net`
  re-run by hand.
