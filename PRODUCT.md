# Thrivbe Voice → a sellable product

How to take the personal menubar agent and turn it into something other people can
buy, install, and set up themselves. This is the plan; the code is being refactored
to match it (see "Build status" at the end).

---

## 1. The distribution reality (read this first)

The product's whole value is the **agentic terminal**: it runs shell commands,
reads files anywhere, drives other apps via Accessibility, and spawns a persistent
`zsh` / delegates to `pi`. **Every one of those is forbidden by the Mac App Store
sandbox.** Concretely, MAS requires the `com.apple.security.app-sandbox`
entitlement, which blocks:

- arbitrary `subprocess`/shell execution and spawning `zsh`/`pi`,
- reading files outside user-granted scopes (no system-wide file access),
- the Accessibility API against *other* processes (reading the element under the
  cursor in other apps),
- a global push-to-talk hotkey via Input Monitoring.

So there are two honest options:

| Edition | Distribution | Capabilities | Who it's for |
|---------|-------------|--------------|--------------|
| **Pro (the real product)** | **Developer ID + notarized**, sold direct (own site / Gumroad / Paddle / Setapp) | Full agentic terminal, system-wide, hotkeys | Power users, developers, operators |
| **Lite** (optional, later) | Mac App Store, sandboxed | Voice Q&A + dictation only; **no** shell, no cross-app reading | Mass market, discoverability |

**Recommendation:** ship **Pro via Developer ID notarization** first. It's the
faster path (no App Review, no sandbox surgery), it's where the value and the
willingness-to-pay are, and it's how comparable tools ship (Raycast, cleanshot,
many menubar AI tools sell direct or via Setapp, not MAS). Treat MAS-Lite as a
later funnel, not the launch vehicle.

### What "App-Store-ready structure" actually means here

Since MAS is off the table for Pro, "store-ready" translates to the things a
notarized commercial app needs anyway, and which a future sandboxed Lite could reuse:

- A real **Apple Developer ID** signing + **notarization** pipeline (stapled,
  hardened runtime).
- **No hardcoded user paths** — everything per-user and discoverable at runtime.
- **Secrets in the Keychain**, not a workspace `.env`.
- A **first-run onboarding** that gets a stranger from download → working in <3 min.
- A **Settings UI** for keys, devices, modes, and the workspace.
- **Licensing/activation** + **auto-update**.
- Clean **uninstall** and **privacy** story.

---

## 2. Architecture: personal tool → product

### 2.1 Current coupling to remove

The MVP hardcodes Robin's machine. The product cannot. Concretely:

- `WORKSPACE = "/Users/robinsverd/Thrivbe-AI"` and `MEMORY = ".../projects/voice-agent/.voice-memory.log"` → per-user paths.
- Keys loaded from `~/Thrivbe-AI/.env` → Keychain, entered in onboarding.
- `LIVE_SYSTEM` hardwires Thrivbe context (CLAUDE.md, skills dirs) → optional,
  user-configurable "workspace" concept.
- Delegation hardwired to `pi`/`claude` → a configurable "power tool" the user opts into.
- Device names `MacBook`/`OpenComm` → user-selected in Settings.

### 2.2 Target module layout

```
voice-agent/
  agent.py        # menubar app + push-to-talk (orchestration only)
  realtime.py     # live speech-to-speech session + agentic shell
  pill.py         # floating state indicator
  config.py       # NEW — per-user config dir, settings model, Keychain secrets
  onboarding.py   # NEW — first-run setup flow
  settings.py     # NEW — preferences UI (keys, devices, modes, workspace)
  licensing.py    # LATER — license key validation / trial
  updater.py      # LATER — Sparkle bridge / update check
```

Config + secrets live under:

```
~/Library/Application Support/ThrivbeVoice/config.json   # non-secret settings
Keychain (service "ThrivbeVoice")                        # API keys
~/Library/Application Support/ThrivbeVoice/memory.log    # cross-session memory
~/Library/Logs/ThrivbeVoice/agent.log                    # logs (today: /tmp)
```

### 2.3 Tiers of user (what unlocks with which key)

The app is **bring-your-own-key (BYOK)** at launch — simplest legally and
operationally (no proxying users' audio/cost through us). Keys required:

| Capability | Provider / key | Required? |
|-----------|----------------|-----------|
| Live voice conversation | **OpenAI** API key (Realtime API) | **Required** — core feature |
| Push-to-talk transcription | OpenAI (same key) | Required for PTT |
| Push-to-talk brain | OpenRouter key (or OpenAI) | Optional — defaults to OpenAI |
| Premium TTS voice (PTT) | ElevenLabs key | Optional — falls back to macOS `say` |
| Agentic delegation | `claude` / `pi` installed locally | Optional power feature |

> **OpenAI is the one mandatory login.** Everything else degrades gracefully.
> Future "managed" tier (we hold the keys, bill subscription) is a v2 business
> decision — see §6.

---

## 3. Onboarding (download → working in under 3 minutes)

First launch detects `onboarding_complete == false` and runs a guided flow. Each
step is skippable-where-safe and re-runnable later from the menu.

**Step 0 — Welcome.** One screen: what it does, the two ways to talk to it
(hold-Option / double-tap-Control), and "you'll need an OpenAI API key."

**Step 1 — OpenAI key.** Text field + "Paste your key" + a link to
`platform.openai.com/api-keys`. On entry we **validate it live** (a cheap
`/models` call) and store it in the Keychain. Red/green inline feedback. This is
the only hard gate.

**Step 2 — Permissions.** Three buttons that deep-link straight to the exact
panes (using `x-apple.systempreferences:` URLs):
- **Microphone** → `...Privacy_Microphone`
- **Accessibility** (read what's under your cursor) → `...Privacy_Accessibility`
- **Input Monitoring** (the hotkey) → `...Privacy_ListenEvent`

Each shows a live ✓/✗ status (we can poll `AXIsProcessTrusted()` for Accessibility;
for the others, "I've enabled it" + re-check). Explain *why* each is needed in one
line — permission anxiety kills activation.

**Step 3 — Audio devices.** Dropdowns for input mic and output, pre-filled with
sensible defaults (built-in mic in, system default out — with the "don't use a BT
headset as mic" guidance baked in as a tooltip). "Test" button speaks a line.

**Step 4 — Optional power-ups.** Toggles for: ElevenLabs voice (key field),
agentic delegation (detects whether `claude`/`pi` exist), and a "workspace folder"
the agent should treat as home/context. All optional, clearly labelled.

**Step 5 — Done.** "Double-tap Control and say hello." Sets
`onboarding_complete = true`.

> Implementation note: the MVP builds this with `rumps.Window`/alerts (fast,
> low-code) and deep-link buttons; a polished SwiftUI/AppKit wizard is a later
> upgrade once the flow is validated.

---

## 4. Menu bar + configuration

### 4.1 Menu structure

```
🎙  (status icon: idle / listening / thinking / speaking)
├── 🎧 Live conversation        ⌃⌃ (double-tap Control)     [toggles]
├── 🔴 Start talking            (hold Right-Option)          [PTT fallback]
├── ──────────
├── Mode
│   ├── Voice: ElevenLabs / macOS say
│   └── Agentic shell: on / off          ← gates run_shell in live mode
├── Settings…                            ← opens preferences
├── Run setup again…                     ← re-runs onboarding
├── ──────────
├── View memory…                         ← opens memory.log
├── Reset conversation
├── ──────────
├── Check for updates…
├── Help / docs
└── Quit
```

### 4.2 Settings (preferences) — the full config surface

- **Accounts / keys:** OpenAI (required, shows validated ✓), OpenRouter,
  ElevenLabs. Each: paste / validate / clear. Stored in Keychain.
- **Voice & audio:** live-mode voice (alloy/echo/shimmer/…), PTT voice
  (ElevenLabs voice id or `say`), input device, output device, test button.
- **Hotkeys:** live toggle (default double-tap Control), PTT (default
  Right-Option) — with conflict warnings (e.g. macOS Dictation on double-Control).
- **Live agent behaviour:** agentic shell on/off, command timeout, delegation
  tool (`pi` model / `claude` / off), "workspace" folder for context.
- **Privacy:** what's sent where (audio → OpenAI; cursor context → OpenAI),
  toggle cursor-context reading, clear memory, open logs.
- **About:** version, license status, check for updates, uninstall helper.

### 4.3 Config schema (`config.json`)

```jsonc
{
  "onboarding_complete": false,
  "live": { "voice": "alloy", "agentic_shell": true, "shell_timeout": 20,
            "delegate": "pi", "pi_model": "deepseek-v4-flash", "workspace": null },
  "ptt":  { "voice_engine": "elevenlabs", "eleven_voice": "21m00Tcm4TlvDq8ikWAM",
            "brain_model": "openai/gpt-4o-mini" },
  "audio": { "input_device": null, "output_device": null },   // null = system default
  "hotkeys": { "live": "double_ctrl", "ptt": "alt_r" },
  "privacy": { "read_cursor_context": true }
}
```
Secrets are **not** in this file — they're in the Keychain.

---

## 5. Packaging, signing, distribution

1. **Bundle:** keep `py2app`, but move from ad-hoc signing to **Developer ID
   Application** signing with the **hardened runtime** + entitlements
   (`com.apple.security.cs.allow-jit`, disable-library-validation only if needed
   for the embedded Python).
2. **Notarize:** `xcrun notarytool submit` → `xcrun stapler staple`. Required or
   Gatekeeper blocks download installs on other Macs.
3. **Distribute:** DMG with drag-to-Applications. Host on own site / Gumroad /
   Paddle (Paddle handles VAT/MoR — worth it for EU). Consider **Setapp** for
   distribution + discovery without per-sale billing overhead.
4. **Auto-update:** **Sparkle** (the standard for non-MAS Mac apps) with a signed
   appcast feed.
5. **Licensing:** license key per purchase, validated offline-friendly (signed
   license blob) with a periodic phone-home; 7-day trial. Keep it simple — license
   gating in `licensing.py`, fail-open on network errors so paying users never get
   locked out.

---

## 6. Monetization

- **Model:** BYOK one-time purchase **or** subscription. Given ongoing model/API
  churn and updates, a **subscription** ($8–15/mo) or **annual + 1 yr updates** is
  more defensible than pure one-time. Setapp gives recurring revenue without
  running billing.
- **Why BYOK first:** users pay OpenAI directly → we carry no inference cost or
  liability for their audio/data, and pricing is pure software margin.
- **v2 "Managed" tier:** we proxy a hosted key, bill a higher subscription, abstract
  away the API-key step entirely (huge onboarding win) — but adds cost, abuse,
  and data-handling responsibility. Decide after launch traction.

---

## 7. Legal / privacy / trust

- **Privacy policy + clear in-app disclosure:** audio goes to OpenAI; cursor
  context (text under the mouse) goes to OpenAI; nothing is sent to us in BYOK mode.
- **Agentic-shell consent:** running arbitrary shell as the user is powerful and
  dangerous. Ship with agentic shell **off by default** for sold copies, an explicit
  "I understand this can run commands on my Mac" gate, and consider a
  confirmation/allowlist for destructive ops.
- **Data residency:** memory.log stays local. Say so.

---

## 8. Roadmap (build order)

1. **Foundation (done):** `config.py` (config dir + Keychain + settings), de-hardcoded
   `agent.py`/`realtime.py`, secrets from Keychain w/ `.env` dev fallback.
2. **Onboarding + Settings (done):** first-run flow, key validation, permission
   deep-links, device pickers, preferences window.
3. **Memory + perception (done):** app-owned conversation store (SQLite+FTS) with a
   continuous-learning loop — extracts durable preferences/facts/corrections on each
   close, reinforces what recurs, supersedes on contradiction; optional claude-mem
   mirror. On-screen context the user can mix-and-match: cursor/marked text, full
   focused-window text, and a focused-window screenshot (vision). See `LEARNING-PLAN.md`.
4. **Hardening:** agentic-shell off-by-default + consent gate, command allowlist
   option, privacy toggles (cursor/window/screenshot done; allowlist next).
5. **Packaging:** Developer ID signing + notarization + DMG + Sparkle auto-update.
6. **Commerce:** trial + license activation; pick Gumroad/Paddle/Setapp.
7. **Lite/MAS (optional):** sandboxed voice-Q&A-only edition for App Store funnel.

---

## Build status

- [x] Personal MVP: push-to-talk + live agentic mode working
- [x] `config.py` foundation (per-user config + Keychain)
- [x] de-hardcode paths/keys/devices
- [x] onboarding flow
- [x] settings window
- [x] conversation memory + continuous learning (reinforce / supersede / decay)
- [x] selectable screen context: cursor text · full-window text · window screenshot (vision)
- [ ] command allowlist + kill-switch (hardening)
- [ ] signing/notarization pipeline
- [ ] licensing + auto-update
