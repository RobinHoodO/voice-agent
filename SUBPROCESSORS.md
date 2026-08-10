# Thrivbe Voice (Pam) — sub-processor register

Internal Art. 28/30 record for the **one** voice assistant Robin now runs: the macOS
menubar agent on his Mac, plus the phone PWA that is a remote microphone and speaker for
that same process. Robin Thomas Sverd / Thrivbe AS is the controller. This is a
record-keeping document for an operation this size, not a DPIA.

**Last updated 2026-08-10** — rewritten when `voice-bridge` was decommissioned. It
replaces `voice-bridge/SUBPROCESSORS.md`, which described a data flow that had already
stopped existing (it named `freellmapi` relays on `:8792 → :3004` as the LLM path; the
bridge had routed to OmniRoute since 2026-07-30 and `freellmapi` was dead).

## The system this covers, in one paragraph

One brain, on Robin's Mac. A conversation is speech-to-speech with **one** model
provider, chosen per session — there is no router, no fan-out, and no second model in the
path. The phone reaches that same process over Tailscale and contributes microphone audio
and speaker output only; it is not a second agent and stores nothing. **Nothing runs on
Thrivbe-1**: the kernel APIs the agent calls (`os-voice-api :8790`) are Robin's own
infrastructure, not a sub-processor.

## Processors in the request path

| Sub-processor | Purpose | Data reaching it | Transfer basis | Retention / training |
|---|---|---|---|---|
| **OpenAI** (US) | Realtime speech-to-speech backend (`core/backends/openai_backend.py`) — the default (`config.py:89 "backend": "openai"`) | Spoken audio in and out; the system prompt; tool names, arguments and **tool results**; optionally macOS context (see below) | DPF-certified + SCCs | API data not used for training by default; short abuse-retention only. **Verify the org key's zero-retention posture — carried over, still open.** |
| **Google** (US/EU) | Gemini Live speech-to-speech backend (`core/backends/gemini_backend.py`) — the alternative, selected per session | Same as above | SCCs / adequacy | Paid Gemini API: not used to improve models. **Verify for the key actually in use — open.** |
| **Tailscale** (US) | The transport the phone uses to reach the Mac, and the HTTPS certificate for it | Coordination metadata (device identities, endpoints); DERP relays carry WireGuard-encrypted packets only, so **no conversation content** | DPF/SCCs | Content is end-to-end encrypted; Tailscale sees no audio |

**Only one model sub-processor sees any given conversation**, because a session binds one
backend. That is the property the retired bridge did not have, and it is the reason the
old `router-safe` pin is not reproduced as a live control (see below).

### What "tool results" means, stated plainly

The agent's tools reach Robin's CRM, inbox, mail, Notion, Drive, the LinkedIn corpus and
the community graph. Their results are returned **to the model**, so third-party personal
data — other people's names, addresses, messages — reaches whichever provider the session
is on. That is inherent to a voice assistant over a workspace; the control is that the
provider is a single named DPF/SCC-covered processor with a no-training posture, not an
anonymous relay.

`core/privacy.py` names those tools (`PII_TOOLS`) and carries `require_no_train()`, which
**refuses** to send such a turn to any model id that names a router rather than a
sub-processor (`auto/*`, `combo/*`, an empty default). It is a tripwire, not a hot path:
no relay exists today, so nothing calls it. It exists now, ahead of need, because the
bridge's version was written *after* its router and rotted unnoticed for ten days — the
pin `router-safe` had stopped existing at the 2026-07-30 OmniRoute cutover and every PII
turn died on an HTTP 400. Nobody was protected by a working control; they were protected
by an error.

**Binding rule for any future cheap/relay path:** it may not ship without pinning a single
named no-train model for a turn that touched a `PII_TOOLS` tool, and that model id must be
written into this file. `require_no_train` enforces the shape; this sentence is the
policy. Guarded by `tests/test_privacy_pii.py`.

## Sub-processors that were removed on 2026-08-10

Recorded because a register that quietly drops a processor is not a register.

| Removed | Was doing | Why it is gone |
|---|---|---|
| **ElevenLabs** (US) | The bridge's premium TTS (the short pre-`===` summary line only) | The turn-based path is retired; both live backends synthesise speech themselves. The open "confirm the ElevenLabs DPA" item is closed as moot — no data flows there. |
| **freellmapi relays / OmniRoute fan-out** | The bridge's LLM router | Retired with the bridge. No fan-out remains in the voice path, which is what removes the training-relay risk entirely rather than mitigating it. |
| **Local `faster-whisper` STT** on Thrivbe-1 | The bridge's transcription (never a sub-processor — it ran on Robin's own hardware) | Retired with the bridge; the 2.0 G model cache is deleted. |

## Local storage (controller-side, not sub-processors)

All of it on Robin's Mac, in `~/Library/Application Support/ThrivbeVoice/`, which is
owner-only.

- **`conversations.db` + `live-turns.journal`** (`core/memory.py`) — the chat record;
  created `0600`.
- **`actions.jsonl`** (`core/audit.py`) — the append-only accountability record of what
  the agent *did*: one JSON line per tool call with timestamp, **surface** (desk vs
  phone), session, event, tool, redacted arguments and a redacted, truncated result.
  Created `0600` and re-`chmod`'d on rotation. **Retention: 5 MB size cap, one
  generation** (`MAX_BYTES`, `_rotate`) — the bridge's `logrotate` daily×14 does not
  survive it, because there is no longer a server to run logrotate on.
  Redaction is a key-name allowlist: free text a human wrote (bodies, memos, delegated
  instructions) is recorded as a length; identifiers (addresses, phone numbers) keep their
  last four characters; **`run_shell` commands are recorded in full by design**, so treat
  this file as sensitive as shell history.
- **macOS unified log / stdout** — transcript content is not logged by default.

## macOS context: what may be attached to a turn

Off unless switched on, and it is per-machine, not per-seat (`core/config.py`,
`mac/macos_context.py`):

- `privacy.read_cursor_context` — **on**: the focused element's text.
- `privacy.read_window_context` — **off**.
- `privacy.read_window_screenshot` — **off**, and even when on, `SCREENSHOT_DENY_APPS` /
  `SCREENSHOT_DENY_TITLE` refuse banking, health, password and messaging windows.

A phone session never carries any of it — the phone cannot see the Mac's screen, and
`server/session.py` pins that per session rather than flipping the process-wide toggles.

## Open verification items (paperwork, low urgency)

1. Record the zero-retention / no-train posture for the **OpenAI** org key in use.
2. Record the same for the **Google** Gemini API key in use.
3. If this ever becomes multi-user or client-facing, revisit Art. 13/14 transparency —
   data subjects are not notified today, which is acceptable for a solo internal tool and
   not for a product.
4. If a cheap relay path is ever built, the pinned no-train model id goes in the table
   above **before** it ships.
