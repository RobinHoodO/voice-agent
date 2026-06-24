# Thrivbe Voice — install guide (beta)

A little 🎙 in your menu bar you can talk to. Hold a key and ask it things, or
double-tap Control for a hands-free conversation. ~3 minutes to set up.

> **You'll need your own OpenAI API key.** This app sends your voice to OpenAI
> using *your* key — nothing goes to us. Get a key at
> https://platform.openai.com/api-keys (pay-as-you-go, usually cents per session).

---

## 1. Install

1. Open **ThrivbeVoice-beta.dmg** and drag **Thrivbe Voice** into **Applications**.
2. This beta isn't signed by Apple yet, so the first launch needs one extra step:
   **right-click** (or Control-click) the app in Applications → **Open** →
   **Open** again in the dialog. (Double-clicking will just say "unidentified
   developer" — right-click → Open is the way past it. You only do this once.)

If macOS says the app is "damaged", run this once in Terminal:
```
xattr -dr com.apple.quarantine "/Applications/Thrivbe Voice.app"
```

## 2. First-run setup

On first launch it walks you through:

1. **Paste your OpenAI API key** (stored securely in your macOS Keychain — never
   on disk, never sent to us).
2. **Grant 3 permissions** — it opens the right System Settings panes for you:
   - **Microphone** — so it can hear you.
   - **Accessibility** — so it can see what's under your cursor (to answer in context).
   - **Input Monitoring** — so the hotkeys work.
3. After granting them, **quit and relaunch** the app (System Settings permissions
   only take effect on restart).

## 3. Use it

- **Hold Right-Option**, ask a question, release → it answers out loud.
- **Double-tap Control** → hands-free live conversation. Double-tap again to stop.
- The menu-bar 🎙 shows state: idle · 🔴 listening · 💭 thinking · 🗣 speaking.

### Tips
- **Don't use a Bluetooth headset as your mic** — macOS drops it into low-quality
  "call mode" and playback gets quiet. Use the built-in mic (or AirPods just for output).
- **"Agentic shell" is OFF by default.** Turn it on in the menu only if you want the
  live agent to actually *run commands* on your Mac (powerful — it acts as you).

## Uninstall
Drag the app to the Trash. To remove everything:
```
rm -rf ~/Library/Application\ Support/ThrivbeVoice ~/Library/Logs/ThrivbeVoice
security delete-generic-password -s ThrivbeVoice   # removes the stored API keys
```

## Trouble?
- **Hotkey does nothing** → re-check **Input Monitoring** for "Thrivbe Voice", then relaunch.
- **It hears but won't talk** → switch system output off a Bluetooth headset (see Tips).
- Logs: `~/Library/Logs/ThrivbeVoice/agent.log`.
