# voice-bridge — decommissioned 2026-08-10

**Status: DONE. This is a record of what was removed, what survived, and where the backup
is.** Executed by worker `bridge-decommission`, 2026-08-10, on Robin's explicit
authorisation ("then delete bridge for now"). Nothing in this document is a plan any more.

The previous version of this file was a 38-row *research* inventory written against a plan
that no longer exists — **"Thrivbe-1 hosts a voice instance and the phone talks to it."**
Robin killed that plan on 2026-08-10 in favour of the architecture below, and the two are
not compatible: about a third of the old PORT decisions were ports *onto a server surface
that is now never going to be built*. Every row was re-decided against the real system
before anything was touched.

## The architecture this was retired into

**Mac-Pam is the system. One brain, on Robin's Mac.** The phone is a remote microphone and
speaker for that same process — PWA → Mac over Tailscale (`server/`, see
`../server/README.md`). **Thrivbe-1 hosts no voice instance and gets none.** A T1-hosted
system is a later project; `mac/reverse_channel.py` stays committed and off by default for
it.

That single sentence is what turned six PORT rows into RETIRE: there is no server surface
to port a cheap turn loop, an ElevenLabs key, a Whisper cache or a serve mapping *onto*.

---

## 1. What was removed, and the proof

All commands ran through `fleet run thrivbe-1`.

| Removed | Verification |
|---|---|
| `voice-bridge.service` (`:8765`), `voice-bridge-serve.service` (`:8766`), `voice-router.service` (`:8793`), `attention-push.{service,timer}`, `tool-drift-check.{service,timer}` | `systemctl is-active` → `inactive` ×5; `is-enabled` → `disabled` ×5; unit files + `.service.d` drop-ins + `/etc/logrotate.d/voice-bridge` deleted; `systemctl --failed` → none |
| `tailscale serve --https=8451 → 127.0.0.1:8766` (host identity) | `tailscale serve status` no longer lists it; the **other 7 mappings** (`:3000`, `:443` Funnel, `:8443`, `:8444`, `:8446`, `:8450`, `:9119`) all still present and `:8443`/`:8444`/`:8446` verified `200` afterwards |
| `https://voice-bridge.tail9908c7.ts.net/ → 127.0.0.1:8766` (second identity) | turned off through its own socket **before** the daemon died, so it could not survive in the statedir; `serve status` → `No serve config` |
| `tailscaled-voicebridge.service` + node `voice-bridge` `100.84.255.84` + `/var/lib/tailscale-voicebridge` | `tailscale logout` through the alternate socket → `Logged out.`; unit + statedir + socket removed; `systemctl list-units --all \| grep -ci voice-bridge` → **0** |
| `/opt/voice-bridge` (463 M) | `ls -d` → `No such file or directory` |
| `faster-whisper` weights `tiny`+`small`+`medium` (2.0 G) at `/root/.cache/huggingface` | `du -sh` 2.0G → **200K**; disk 117G → 115G used |
| the bridge's SSH key to Robin's Mac | see §3 |

### The tailnet node was a bypass, not a leftover daemon

This is the finding that made removing the node the point of the exercise rather than
tidying up. `tailscaled-voicebridge` ran with `--tun=userspace-networking`, which makes the
node **proxy any tailnet peer to `127.0.0.1:<any port>` on Thrivbe-1**. Not just the bridge
— *any* loopback-bound service on the box, with no serve mapping and nothing in
`tailscale serve status` to reveal it.

Measured from the Mac, before and after (`curl -o /dev/null -w '%{http_code}'`):

| probe | before | after |
|---|---|---|
| `http://100.84.255.84:8766/` (the bridge) | **200** | **000** |
| `http://100.114.219.63:8766/` (same port, host's own tailnet IP) | 000 (refused) | 000 |
| `http://100.84.255.84:3020/` (**Langfuse web, loopback-only**) | **200** | **000** |
| `http://100.114.219.63:3020/` | 000 (refused) | 000 |
| `http://100.84.255.84:8793/` (voice-router, loopback-only) | **200** | 000 |
| `https://voice-bridge.tail9908c7.ts.net/ping` | 200 | 000 |
| `https://hetzner.tail9908c7.ts.net:8451/ping` | 200 | 000 |

Langfuse is the one that matters: it is bound loopback-only *on purpose* and fronted by
`tailscale serve --https=8443`, and this node handed it to any tailnet peer on plain HTTP.
**Removing the serve mappings did not close it** — `:3020` still answered `200` through the
node after both mappings were off. Only killing the node did.

**One manual step remains, and it is cosmetic:** the device row `voice-bridge
100.84.255.84 … offline, last seen 4m ago` is still listed in the Tailscale admin console.
`logout` invalidated its node key — the bypass is closed and the hostname cannot serve —
but deleting the row is a console action Robin has to click. Until he does, the hostname
stays claimed.

---

## 2. Every capability, re-decided

No row says TBD. Where the earlier document said PORT and this one says RETIRE, the reason
is always the same one sentence — **there is no server surface any more** — so it is stated
once here and not repeated per row.

### Ported, and verified working

| capability | where it lives now | verification |
|---|---|---|
| **`actions.jsonl` audit journal** — the only record of what the agent *did*, independent of what was *said* | `core/audit.py`, called from `core/live_session.py:302` on every tool call | **Live, not just present:** `~/Library/Application Support/ThrivbeVoice/actions.jsonl`, `-rw-------`, 7,647 bytes, last written 2026-08-10 13:16. `tests/test_audit_journal.py` green. |
| **its retention** | 5 MB size cap, one generation, re-`chmod 0600` on rotation (`MAX_BYTES`, `_rotate`) | Deliberately *not* the bridge's `logrotate` daily×14 — there is no server left to run logrotate. Recorded in `SUBPROCESSORS.md`. |
| **`PII_TOOLS` → no-train pin** (GDPR) | `core/privacy.py`; `_pii_touched` tracked at `core/live_session.py:1278` | `tests/test_privacy_pii.py` green. **The intent was ported, not the string** — the bridge pinned `router-safe`, an id that stopped existing at the 2026-07-30 OmniRoute cutover, so for ~10 days every PII turn died on an HTTP 400 and nobody was protected by a working control. `require_no_train()` validates the id it is *given* and refuses a router/`auto/*`/empty default rather than substituting a name that may have rotted. |
| **`SUBPROCESSORS.md`** | `../SUBPROCESSORS.md` (new) | Rewritten, not copied. The bridge's version named `freellmapi` relays as the LLM path — a data flow that had stopped existing ten days before the bridge did. The new one records one named model sub-processor per session, no router, and closes the ElevenLabs DPA item as moot. |
| **`tool-drift-check` — the scheduled half** | `../tool_drift_cron.sh`, run by `~/Library/LaunchAgents/com.thrivbe.tool-drift.plist`, Mon 08:30 | **Ran it:** wrapper exit `0`; `launchctl start com.thrivbe.tool-drift` → `"LastExitStatus" = 0`. Sentry delivery proven separately (`sentry-notify.sh voice-agent-tool-drift …` → exit 0). |

**Why the drift check needed work rather than a re-point.** The Mac already had a weekly
launchd job, but it ran the checker bare and sent its verdict to a log file — so a drift
would have been *silent*, which is the exact failure mode the check exists to prevent (a
tool that drifts out of the kernel manifest loses its spoken-confirmation gate and keeps
working, it just stops asking). The T1 unit had the Sentry reporting; the Mac job had the
right surfaces. This is the two halves joined. Per the house cron rule: failures →
Sentry (`SENTRY_SERVICE=voice-agent-tool-drift`), never Telegram; success silent, which the
rule permits for a job that is not on the kernel host.

### Retired

| capability | decision | reason |
|---|---|---|
| **Turn-based cheap path** (Whisper → OmniRoute → ElevenLabs, SSE) and **`/ask-text`** | **RETIRE** | The whole cost argument was "STT is free because it runs on Thrivbe-1's CPU against 2.0 G of weights already on the box". There is no process on Thrivbe-1 to run it in, and the Mac has neither the weights nor `faster-whisper` in `requirements.txt`. Building a turn backend for the Mac is a feature project, not a decommissioning step. **Consequence, stated rather than hidden:** when the kernel's `VOICE_BUDGET_NOK` cap trips (it did on 2026-07-30 at NOK 65.07), voice now *stops* until Oslo midnight instead of degrading. That was already true of the desk agent; the bridge was an informal escape hatch nobody had chosen deliberately. |
| **ElevenLabs TTS** | **RETIRE** | Only the turn path used it. Both live backends synthesise speech themselves. |
| **`faster-whisper` weights, 2.0 G** | **RETIRE** (deleted) | Follows the row above. The earlier document's "KEEP deliberately" was conditional on the turn path shipping. |
| **`freellmapi` / `voice-router` `:8793`** | **RETIRE** | A pure proxy to `http://localhost:3004/v1`; nothing has listened on `:3004` for weeks (`freellmapi` container = `not-found`), so it could not answer a completion for any consumer. Already `disabled`, i.e. it was going to die at the next reboot anyway. |
| **`/live` Gemini relay** | **RETIRE** | `core/backends/gemini_backend.py` + `core/live_session.py` already speak Gemini Live natively, with session resumption, stall/idle guards and barge-in the relay never had. |
| **PWA, `X-Voice-Token`, HTTPS certs, both serve mappings** | **RETIRE** | Superseded by `server/` on the Mac, which mints its own token (`VOICE_AGENT_TOKEN`, Keychain) and gets HTTPS from `tailscale serve --https=8443` on the Mac's own identity. The 2026-06-27 keypair had **no renewal timer** and expires 2026-09-21; it is in the backup and is not re-issued. |
| **all seven `android_*` / `send_sms` / `get_phone_status` tools, `voice.sh`** | **RETIRE** | Dead for months against a phone node that re-registered (`100.127.4.13` → `robin-t-1 100.110.143.125`) and a Termux username that changed after a reinstall. Under the new architecture the phone is a microphone, not an SSH target. |
| **`herdr_delegate`/`herdr_fleet` over the bridge's SSH shim** | **RETIRE + key revoked** | See §3. |
| **Langfuse tracing (`langfuse_trace.py`) + the `langfuse-judge` feed** | **RETIRE the tracer; the judge loses its input** | Honest statement of a real loss: `grep -rn langfuse core/ mac/ server/` → no hits, so Pam is untraced, and `langfuse-judge` on Thrivbe-2 (07:20) will keep running against an empty window and quietly report nothing. It was not ported because tracing the Mac agent is a feature build with no bearing on whether the bridge can be deleted. **Follow-up, named so it is not lost:** either build `core/tracing.py` or stop the judge. |
| **`teach_agent_tool` / `dynamic_registry.json`** | **ALREADY DEAD** | Advertised in the bridge's README; absent from `server.py`; `dynamic_registry.json` was 3 bytes (`{}`). |
| **on-box `.bak` sprawl** (11 × `server.py.bak.*`, `router_decisions.jsonl`, …) | **RETIRE** | Captured in the backup. |

### `attention_push.py` + `attention-push.timer` — RETIRE, and it *was* delivering

The instruction was to establish whether it delivers today before deciding. It does — but
not to the phone, and not in a way worth keeping.

**Evidence.** `push_fail_count` in `.attention_push_state.json` read **5,895** on 2026-08-09
and **5,902** on 2026-08-10: seven more failed phone pushes in 24 h, against a node
(`100.127.4.13`) that has been offline for six days. Over the same window `approval_ids`
grew 15 → 19 and `attention_keys` 49 → 54. That combination is only reachable through one
branch of `main()`: `push_phone` raised, `send_telegram` **succeeded**, and
`record_delivered` marked the items delivered so they would not be re-sent every five
minutes. So the live behaviour is: **the phone leg has been dead for thousands of
consecutive attempts, and Telegram has silently been the actual channel.**

Retired anyway, for three reasons:

1. **The phone leg cannot be revived under this architecture.** It ssh'd into Termux. The
   phone is now a microphone and speaker for the Mac; there is no shell on it in this design.
2. **Both halves of what it carries are already delivered by something better.**
   *Approvals* — `os-approvals-daemon.service` (running, Telegram long-poll) pages Robin
   with **action buttons**; `attention_push` re-sent the same approvals as plain text with
   nothing to tap. *Attention items* — Command Center renders them at `/api/os/attention`
   (`NeedsRobinPanel.tsx`, priority-ordered), and the urgent ones are **spoken** by Pam
   (`core/kernel_tools.py:251 kernel_urgent` → `mac/agent.py:_apply_urgent_snapshot`,
   quiet-hours aware, `tests/test_urgent_wake.py`).
3. **Its surviving channel breaks the house cron rule** — informational output was going to
   Telegram, which is reserved for things Robin must personally act on.

**Residual gap, accepted rather than papered over:** a *non-urgent* attention item now gets
no push at all when Pam is not running and Robin is not looking at Command Center. It is
visible in both surfaces; nothing nudges. The cheap fix if that ever bites is a kernel-side
insert into the notifications feed (which already exists) — deliberately not built here,
because arming a new scheduled job on T1 is Robin's call and is not needed to retire the
bridge.

### Command Center — what calls the bridge, and what breaks

Grepped `command-center/src`. Four call sites, all pointing at
`https://hetzner.tail9908c7.ts.net:8765`, which no longer answers.

| call site | consumer | what breaks now | decision |
|---|---|---|---|
| `src/app/api/voice/[action]/route.ts` (`ask`, `ask-text`) | `VoiceBriefController.tsx:133,189`, rendered by `OverviewDashboard.tsx:150` and `BriefingCard.tsx` | the daily brief's voice button returns 502 | **RETIRE** — remove the route and the controller |
| `src/app/api/system-graph/chat/route.ts:56,69,141` | `SystemMapChat.tsx:254,286` | **the System Map Chat stops answering, text as well as voice** — the bridge was doing the LLM call, not just the STT | **RETIRE as wired; rebuild the text path in CC** |
| `src/app/api/stats/route.ts:26` (`GET :8765/ping`) | `voiceAgentOnline` in `useStats.ts` | a probe to a dead host every TTL | **RETIRE** — and note that **no component renders `voiceAgentOnline`**; the "health tile" this was said to drive does not exist. Deleting the probe costs nothing visible. |
| `src/lib/os-map/spine.ts:227`, `src/lib/os/process-descriptions.ts:331,337,433` | the OS map | stale nodes, so the map lies | **UPDATE** |

**`/today` is not a page.** `src/app/today/page.tsx` is
`permanentRedirect("/inbox")` — the daily brief lives on `/inbox`, and the comment in the
voice route naming "/today" is stale. So the thing at risk is the brief on `/inbox` and `/`,
not a `/today` route.

**Not executed in this round, and why.** Command Center is a separate repo, on `main`, with
its own deploy path to Thrivbe-1 (`/opt/thrivbe-ops/deploy.sh command-center`). Two of the
four edits are trivial deletions, but the third is not: the System Map Chat needs its LLM
call rebuilt, and the obvious host for it — the `OpenAI`/OpenRouter client already
constructed at the top of that route file — is **dead code today** (`router` and `MODEL` are
declared and never used), so there is no evidence `OPENROUTER_API_KEY` is even set in CC's
production environment. Rebuilding it on an unverified key and deploying it to Robin's
flagship is not something to do blind at the tail of a decommission. Handing over a precise
four-line map is worth more than a half-verified deploy.

### Untouched, named so nobody "cleans them up"

- **`os-voice-api.service` `:8790`** — the kernel's voice API (`/status`, `/persona`,
  `/session-log`, `/tools`). Kernel-owned; everything ported still calls it. `active`.
- **`VOICE_BUDGET_NOK`** — a kernel control in `thrivbe-os/src/os-worker.ts`, never a bridge
  control. Killing the bridge does not weaken it.
- **`unified-router.service` `:8792`** — Robin's `CLAUDE_ROUTER_URL` (`~/.zshrc:124`). It
  was a *co-tenant* of `/opt/voice-bridge`, not a bridge capability, so it had to move
  before the delete. **Relocated to `/opt/freellm-router`** (script, allowlist, and the env
  file holding an API token, `chmod 600`), unit rewritten, `daemon-reload`, restarted,
  `active`, and **`enable`d** — it had been `disabled`, i.e. it would not have survived a
  reboot. `http://100.114.219.63:8792/v1/models` → `200` from the Mac, same as before.
  ⚠️ **It is still broken for completions and always was:** `--api-base
  http://localhost:3004/v1` points at `freellmapi`, which is gone, so `POST
  /v1/chat/completions` returns `502 "All compatible models failed"`. The `200` on
  `/v1/models` is served from a local JSON file. Relocation preserved the status quo
  exactly; **repointing it at OmniRoute `:20128` or dropping `CLAUDE_ROUTER_URL` is Robin's
  call and its own ticket.**

---

## 3. The SSH key to Robin's Mac — revoked, with proof

`/opt/voice-bridge/.ssh/herdr_mac` was a private key that authenticated **into Robin's
Mac**, matched by a `voice-bridge-herdr` line in `~/.ssh/authorized_keys`. Deleting the
server would have destroyed the private half while leaving the public half authorised —
an authorised key with no known holder. So the Mac was revoked **first**:

```
# before (from Thrivbe-1, using the bridge's key):
ssh -i /opt/voice-bridge/.ssh/herdr_mac robinsverd@100.96.126.87 "status"
  → denied: only 'herdr ...' is allowed over this key      ← the shim answered: key ACCEPTED

# revoke on the Mac (backup kept at ~/.ssh/authorized_keys.bak-20260810):
grep -v " voice-bridge-herdr$" ~/.ssh/authorized_keys > … && chmod 600 …
  8 lines → 7 lines

# after:
  → Permission denied (publickey,password,keyboard-interactive)   exit 255
```

The two herdr paths that are **not** the bridge's were left intact and verified present:
`termux-to-mac` (`from="100.110.143.125"`, the live phone) and `hermes-herdr-mac`
(`from="100.114.219.63"`, Hermes). Keep `~/.ssh/authorized_keys.bak-20260810` until the
dust settles, then delete it — it still contains the revoked public key.

**Noted, not acted on (out of scope):** line 3 is `phone-termux-herdr`, pinned to
`from="100.127.4.13"` — the phone's *old*, six-days-offline address. It is stale and
authorises nothing reachable, but it is not the bridge's key and removing it is Robin's
call.

---

## 4. The backup

```
/root/voice-bridge-decommission-20260810.tar.gz          on Thrivbe-1
-rw------- root root  492,876 bytes
sha256  7cad6124f381ce99d33b731de1ec6bb167407e8ac97a324cf0fa02ec6e32b97a
88 entries
```

Verified, not merely created:

- `gzip -t` → OK.
- Extracted `opt/voice-bridge/server.py` from the archive and re-hashed it:
  `330bbb159904c8d3f0fa138c9ecd6af7b1c6c2f77b5c353751663807a5fe52fa` — **identical** to the
  live file's hash taken before the delete.
- Contains the four things nothing else preserves: `opt/voice-bridge/.env`,
  `certs/cert.key`, `.ssh/herdr_mac`, `voice-router.env`.
- Also contains **all 25 systemd artefacts** (`root/vb-units/…`: the 9 unit files, every
  `.service.d` drop-in including the `os-ping-fail@` Sentry wiring, and the logrotate
  config) — those live in `/etc`, so a tar of `/opt/voice-bridge` alone would have lost them.
- `chmod 600` and it stays that way: it holds two private keys and two API tokens.
- The virtualenv is excluded (459 M of the original 463 M; rebuildable from
  `requirements.txt`).

**Repo↔box divergence: resolved before archiving, and it was the harmless direction.**
`sha256sum` of ten files, box vs `projects/voice-bridge`: nine identical. The tenth,
`check_tool_drift.py`, differs because the *repo* is ahead (HEAD `df249fa`, "the bridge's
drift checker becomes a shim over the merged one"). Nothing existed only on the box.

**The repo stays.** `/Users/robinsverd/Thrivbe-AI/projects/voice-bridge` is untouched and
still in git — it is the code half of the record; the tarball is the secrets/state half,
and those two deliberately do not live in the same place.

---

## 5. Follow-ups, owned by nobody yet

1. **Delete the `voice-bridge` device row** in the Tailscale admin console (console-only).
2. **Command Center**: the four edits in §2, including the System Map Chat rebuild.
3. **Langfuse**: build `core/tracing.py` or stop `langfuse-judge` on Thrivbe-2 — it is
   currently judging an empty window.
4. **`unified-router` `:8792`**: repoint at OmniRoute or drop `CLAUDE_ROUTER_URL`. It is
   relocated and enabled, and it still cannot serve a completion.
5. **Workspace record**: drop `voice-bridge` from `Thrivbe-AI/CLAUDE.md` + `projects.json`;
   free ports 8765/8766/8451; update `bridge/problem-inventory.md`; replace the stale memory
   note claiming "attention-push blocked on Termux sshd not running" — sshd runs, the
   identity was stale, and the job is gone either way.
6. **Labs entry** per the workspace habit: `projects/thrivbe-clone/content/labs/`.
