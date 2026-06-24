# Thrivbe Voice — Go-To-Market Strategy

> Companion to `PRODUCT.md` (which covers the editions, signing/notarization, and
> pricing mechanics). This doc focuses on **testing, audience, defensibility**, and
> confirms the **install / auto-update / error-tracking** feasibility. Where this
> overlaps PRODUCT.md it defers to it; it does not restate it.

---

## 0. What we're selling (one paragraph, so the rest makes sense)

A hands-free macOS menubar agent (`com.thrivbe.voice-agent`, PyObjC/`rumps`, py2app)
that you talk to with a push-to-talk hotkey. It is **not** a chatbot: it reads your
on-screen context (frontmost app, window title, selection), acts on the machine via a
persistent shell with read/bash/edit/write tools, and **delegates slow research/coding
jobs to a background agent** (`pi`/deepseek) that survives the conversation and
**wakes you to speak the result** when done. The differentiator vs Siri / ChatGPT
voice is the last two clauses: *it does work and comes back to you.*

---

## 1. How to test it

### The macOS reality that shapes testing
- **No TestFlight.** TestFlight is App-Store-only, and the Pro edition can't be on the
  App Store (shell/Accessibility are sandbox-forbidden — see PRODUCT.md §1). So beta
  distribution = **notarized DMG handed to a private list**, or a **Sparkle "beta"
  appcast channel** (same updater, separate feed) once Sparkle is in.
- **The #1 thing to test is not the AI — it's onboarding.** The app needs three scary
  permission grants (Microphone, Accessibility, Input Monitoring). Every one is a drop-
  off point. If 60% of testers never get past permissions, the AI quality is moot.

### Phased rollout
| Phase | Who | Goal | Gate to next phase |
|-------|-----|------|--------------------|
| **Dogfood** | You + 1–2 trusted | Daily-driver stability, crash floor | A full day with no hard crash |
| **Closed alpha** | 5–15 hand-picked power users | Onboarding funnel + core-loop value | ≥70% complete permission setup; ≥3 say "I'd miss this" |
| **Open beta** | 50–200 via waitlist | Scale, edge-case hardware/OS versions, pricing signal | Crash-free sessions >95%; willingness-to-pay confirmed |

### What each tester run must exercise (the critical paths)
1. **Permission onboarding** — fresh Mac, no prior grants. Time it; count drop-offs.
2. **Core voice loop** — double-tap hotkey → ask → spoken answer, with on-screen
   context actually used.
3. **Delegation + auto-wake** — fire a slow task, confirm it returns and speaks
   unprompted (this is the wow moment; it's also the most fragile — see the recent
   stdin-hang bug class).
4. **Device handling** — connect/disconnect a headset mid-session; mic/speaker pickers
   must reflect reality (already fixed via PortAudio re-init).
5. **Failure modes** — network drop, API key missing/expired, denied permission.

### Instrumentation (so testing produces data, not vibes)
- **Sentry** for automatic crash/exception capture (feasibility confirmed below). This
  is what turns "it broke for someone" into a stack trace you can act on.
- **One lightweight funnel event** per onboarding step (local log or a single
  phone-home ping) so you can *see* where setup dies.
- **An in-app "Report a problem"** that attaches the last N log lines.

### Success metrics to define up front
- Onboarding completion rate (permissions granted ÷ launches).
- Crash-free session rate.
- D1 / D7 retention (did they come back).
- Delegation success rate (dispatched tasks that complete and auto-wake).
- Qualitative: "very disappointed if it went away" ≥ 40% (Sean Ellis PMF test).

---

## 2. How to reach a wider audience

### Positioning (the line everything hangs off)
> *"Talk to your Mac. It sees what you're doing, does the work, and comes back to you
> with the answer."*

Lead with the **delegate-and-walk-away** loop, not "AI voice assistant" (saturated,
sounds like Siri). The shareable demo is: speak a research task → close the lid → get
spoken results later.

### Pick ONE beachhead, not "everyone"
Best fit = people who live in meetings and on a Mac terminal: **independent
consultants, founders, technical operators, sales/CS power users.** Win that niche's
trust, expand later. "Mass market" is the Lite/MAS edition's job, not Pro's.

### Channels, in launch order
1. **Hacker News (Show HN)** — technical, local-agentic-tools crowd; honest demo +
   "here's how it works under the hood" earns this audience. Highest-signal first stop.
2. **X/Twitter demo clips** — the voice→action→return loop is inherently video-native.
   One 20-second screen recording does more than a landing page.
3. **Product Hunt** — once onboarding is smooth; good for a waitlist spike + backlinks.
4. **Niche communities** — r/macapps, r/LocalLLaMA (if/when a local-model option
   exists), indie-hacker and AI-tooling newsletters.
5. **Setapp** (later) — distribution + discovery for Mac power-user apps without
   per-sale billing overhead (PRODUCT.md already flags this).

### Content engine
- A **library of 15–30s demo clips**, one task each (summarize this call, research X
  while I'm away, rename these files, draft this reply). Each clip = one channel post.
- A **"how it actually works" technical writeup** for the HN/dev audience — transparency
  is a moat with this crowd, not a leak (the value is execution, not the recipe).

---

## 3. "Make sure nobody just takes the idea and sets it up"

Straight answer first: **you cannot protect the idea.** Ideas aren't defensible, and
the architecture (menubar app + realtime voice + delegate to a headless agent) is
visible the moment you demo it. Anyone competent could rebuild the concept. The Python
bundle is also **trivially decompilable** — py2app ships `.pyc`, so code obfuscation
buys you days, not protection. Plan around that, not against it.

What you *can* actually defend, in order of strength:

1. **Server-side moat (the real one).** Move the valuable, copy-prone logic — prompt
   orchestration, delegation routing, the system prompts in `realtime.py`, any tuned
   pipelines — **behind your own API**, keyed per user. Ship a **thin client**. A
   copycat who clones the app gets a shell that does nothing without your backend.
   This is the single highest-leverage protection and it doubles as your billing point.
2. **Licensing + activation gating updates.** Per-purchase license key (signed,
   offline-friendly blob + periodic phone-home; PRODUCT.md §5). **Tie the Sparkle
   update feed to a valid license** — an unlicensed copy still runs but rots: no
   updates, no new features, no server access.
3. **Brand + trademark.** Trademark the name ("Thrivbe Voice") and own the identity.
   You can't trademark the idea, but you can stop someone shipping *your* product under
   *your* name, and the brand is what users actually search for and trust.
4. **Speed and community as the durable moat.** First-mover polish, a tight feedback
   loop with the beachhead niche, and shipping faster than a copycat can catch up. For
   small tools this beats any legal/technical barrier in practice.

**Net recommendation:** thin client + server-held secret sauce + license-gated updates.
That combination makes copying the binary worthless and makes the subscription the
thing they're actually buying. Don't spend effort on code obfuscation.

---

## 4. Can it be a self-updating installable app? (Yes — all three asks are feasible)

### 4a. Installable on other people's Macs
**Feasible, and the path is already scoped in PRODUCT.md §5.** Summary so it's in one
place:
- Keep **py2app** (already in `setup.py`).
- Sign with **Developer ID Application** + **hardened runtime** + entitlements
  (mic, Apple Events, audio-input).
- **Notarize** (`xcrun notarytool submit` → `xcrun stapler staple`). Mandatory — without
  it Gatekeeper blocks the download on other Macs ("unidentified developer" / damaged).
- **Distribute** a **DMG** (drag to /Applications). Host on your site / Gumroad / Paddle,
  or Setapp later.
- *Not* the Mac App Store for Pro — the shell/Accessibility capabilities are
  sandbox-forbidden (PRODUCT.md §1).

### 4b. "Connected to the app, they update on their side to the newest version"
**Feasible via Sparkle** — the standard auto-update framework for non-App-Store Mac apps.
- Sparkle reads a hosted **appcast XML feed** describing the latest version + download URL.
- Updates are verified with an **EdDSA signature** (you sign each build; the app holds
  the public key) so nobody can push a malicious update.
- The app already uses **Cocoa/PyObjC** (`settings.py` imports `Cocoa`/WKWebView), so
  bridging Sparkle from Python is straightforward — wire it into the planned
  `updater.py` (already a placeholder in the PRODUCT.md structure) and the existing
  "Check for updates…" menu item.
- This gives both **manual** ("Check for updates…") and **automatic** (background check
  + install on next launch) update flows.
- **Tie the appcast to licensing** (§3.2): valid license → gets the feed; unlicensed →
  doesn't. One mechanism serves both "stay current" and "stop freeloaders."

### 4c. Automatic error tracking (Sentry)
**Feasible and cheap.** Sentry has a first-class **Python SDK**:
- `sentry_sdk.init(dsn=..., release="thrivbe-voice@<version>", traces_sample_rate=...)`
  early in `agent.py`.
- Install a **global `sys.excepthook`** (and wrap the asyncio/realtime loop) so the
  silent-failure paths that currently only hit the local `LOG()` also report to Sentry
  with a stack trace.
- Works headless — no UI needed; it's the right tool for a background menubar app where
  the user never sees a traceback.
- **Privacy note (critical for a voice/screen-reading app):** scrub before send. Do
  **not** ship transcript text, audio, file contents, or screen context to Sentry —
  strip them in a `before_send` hook. Send the error and minimal metadata only. This is
  both an ethics and a trust/marketing point.
- "Automatically updates it" = Sparkle's automatic channel (4b). Sentry tells you *what*
  to fix; Sparkle ships the fix. Together: detect → release → auto-deliver, no user action.

---

## 5. Suggested sequencing

1. **Harden + instrument** — add Sentry (scrubbed), fix the known fragile paths
   (delegation/stdin class of bugs), tighten the permission onboarding funnel.
2. **Sign + notarize + DMG** — get a real installable on a stranger's Mac.
3. **Closed alpha (5–15)** — measure the onboarding funnel and core-loop value.
4. **Sparkle auto-update + license gating** — so beta→GA upgrades are one mechanism and
   freeloaders self-expire.
5. **Thin-client refactor** — move secret-sauce orchestration server-side (the real
   moat + billing point) before any public launch.
6. **Public launch** — Show HN + demo clips → Product Hunt → niche communities → Setapp.

---

## 6. Honest risks

- **Permission friction** is the biggest conversion killer — more than AI quality.
- **API cost per user** (realtime voice + delegation) can sink unit economics; meter it
  and price for it (PRODUCT.md leans subscription $8–15/mo — correct instinct).
- **Trust**: an app that reads your screen and runs shell is a hard sell. Privacy posture
  (local-first where possible, scrubbed telemetry, transparent docs) is a *feature*, and
  the recent prompt-injection-to-shell finding shows why a human-in-the-loop confirmation
  for any transcript-derived action must be in place before wide release.
- **Copycats**: real but secondary; out-execute via the server moat + speed, don't
  litigate.
