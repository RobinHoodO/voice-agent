# server/ — the phone surface

Robin's iPhone as a **remote microphone and speaker for the Mac**. Not a second agent:
this package runs inside the menubar app's own process, and a phone session drives the
same `core.live_session.LiveSession` machinery the double-tap does.

```
iPhone PWA
   │  https / wss  (real Let's Encrypt cert, *.ts.net)
   ▼
tailscale serve  :8443   ── proxies ──▶  127.0.0.1:8767   (uvicorn, loopback only)
                                              │
                                     server/app.py  ── /live ──┐
                                                               ▼
   AudioWorklet mic ──PCM16 binary──▶ bridge.feed_mic → LiveSession._mic_cb
                                          → _mic_q → core.audio_core._pump_mic
                                          → VAD / turn boundaries → backend
   AudioWorklet out ◀──PCM16 binary── player thread ← LiveSession._out_q
   {"type":"interrupted"} ◀────────── BrowserLiveSession._flush_out (barge-in)
```

| File | Role |
|---|---|
| `app.py` | FastAPI: auth boundary, session registry, the floor, the `/live` WebSocket. |
| `audio_ws.py` | `BrowserAudioBridge` + `BrowserAudioTransport` — `mac/audio.py`'s sibling. |
| `session.py` | `BrowserLiveSession`: four surface hooks, plus the two per-session pins. |
| `index.html` | The installable PWA. Two AudioWorklets, no VAD. |
| `manifest.json` / `sw.js` / `icon-*.png` | PWA shell. Distinct id + cache name from voice-bridge's. |
| `../mac/phone_surface.py` | The switch: uvicorn + `tailscale serve`, on and off. |
| `../mac/tailnet.py` | Where it may bind, and how it gets HTTPS. |
| `../core/floor.py` | Who is allowed to be in a conversation right now. |

## Turning it on

Menu bar → **📱 Phone surface: off** → click. Then **Copy phone link + token**, open the
URL on the phone once, paste the token, Add to Home Screen. `phone_surface.enabled` is
persisted, so it comes back after a relaunch.

## The four things that are easy to get wrong

1. **Never gate the mic.** Every captured frame goes on the socket the instant it
   exists — while Pam is talking, and inside the barge-in hold window. `_pump_mic`
   never `continue`s in that window on purpose; a client-side "don't send while she
   speaks" optimisation eats the first word of every interruption.
2. **Sample rate comes from the backend, not from this code.** `BrowserAudioTransport.
   start` reads `s._backend.mic_rate` (Gemini 16 kHz, OpenAI 24 kHz) and sends it as the
   `audio` control frame; the capture worklet configures itself from that frame and
   captures nothing before it arrives. Playback is 24 kHz — `core.realtime_client.SR`.
   Both worklets linear-resample against `AudioContext.sampleRate`, which the phone
   picks (usually 48 kHz) and which nothing here may assume.
3. **Session state is per tab.** One `LiveSession` per session UUID, so the one staged
   high-stakes action lives on that object. A spoken "yes" in another tab reaches a
   different object and cannot confirm it. `tests/test_server_auth.py::
   test_two_tabs_never_cross_confirm` is the regression guard.
4. **Nothing here may rewire `core.caps`.** Both surfaces share one process now, so a
   process-wide `set_profile` / `set_audio_transport` reroutes the *menubar's* audio and
   shell. What differs per seat is pinned on the session class (`PROFILE`,
   `AUDIO_TRANSPORT`); `core.caps` answers only "what machine is this", and the answer is
   always this Mac. `tests/test_phone_does_not_steal_the_mac.py` parses this directory
   for the two banned calls.

## The phone connects while Robin is talking at his desk

One brain, so this is a real case and it needed a decision. **It is refused.** The desk
conversation is not touched, nothing is started (no `LiveSession`, no realtime socket),
and the tab is told who has the floor:

```json
{"type": "busy", "holder": "desk", "since_s": 42.0, "text": "Pam is already in a conversation at your desk…"}
```
followed by close code `4409`. The PWA then shows a **Take over from the desk** button.

**Why refusal, and not automatic takeover.** The rule is that a *connection* never ends
a conversation — only Robin, or the idle/max watchdogs core already runs. Automatic
takeover is the option that can lose a turn, because at the server the two cases are
identical: "Robin walked to the sofa and opened the app" and "the phone woke up in his
pocket and the service worker revived the socket". iOS restores PWAs on its own, tunnels
heal on their own, tabs wake on their own. Any of those, under automatic takeover, ends
the sentence he is in the middle of at his desk. Under refusal, the worst case is one
extra tap.

**Why a takeover is still allowed.** From his phone Robin cannot reach the desk session
at all — he is not there. So the phone offers an explicit second act. `?takeover=1` is
set in exactly one place in `index.html`, inside the button's `onclick`, and cleared the
moment it is spent, so no automatic reconnect can carry it. A takeover then ends the
other side by calling its own `LiveSession.stop()` — the same call a spoken sign-off
makes — so the transcript is persisted by the path that already persists transcripts.
Nothing is discarded.

**The symmetric case** (double-tapping Control at the desk while the phone is live) is a
takeover for the same reason: a keypress on this keyboard is unambiguously Robin, here,
deliberately. It evicts the phone through the identical path.

**Scope.** The floor arbitrates *seats*, not sockets. The phone holds one claim however
many tabs it has open — that is one human in one room — and per-tab confirmation
isolation (point 3 above) is a separate mechanism that is unchanged.

**One more consequence, and it is a feature:** while the phone holds the floor,
`mac/agent.py` routes a finished background job to the phone conversation instead of
waking the Mac to speak at an empty room, and the proactive/urgent wake stays quiet.

## Auth

voice-bridge's pattern, kept because it has survived a year on Robin's phone:

* shared token — `X-Voice-Token` on HTTP, `?token=` on the WebSocket (browsers cannot
  set custom headers on a WS handshake), compared with `hmac.compare_digest`. It lives
  in the Keychain as `voice_agent_token` and is minted on first use
  (`mac.phone_surface.ensure_token`);
* per-tab session UUID, rejected unless it parses as a UUID;
* `_RedactTokenFilter` on `uvicorn.access` **and** `uvicorn.error` — the WebSocket
  "accepted" line comes from the error logger, and the token is in the path.

The listener binds **loopback**. `mac.tailnet.resolve_bind_host` accepts loopback or a
tailnet address this Mac actually holds and refuses everything else — `0.0.0.0` by the
rule, not by a special case.

## HTTPS is mandatory, and `tailscale serve` provides it

`getUserMedia` and `AudioWorklet` are secure-context-only: Safari will not hand over the
microphone to a plain-http origin, with no prompt and nothing to click through. So TLS
is the difference between a working microphone and a dead one.

Verified against the installed binary (Tailscale **1.98.10**):

```
$ /Applications/Tailscale.app/Contents/MacOS/Tailscale \
      serve --bg --yes --https=8443 http://127.0.0.1:8767
Available within your tailnet:
https://robins-macbook-pro.tail9908c7.ts.net:8443/
|-- proxy http://127.0.0.1:8767
To disable the proxy, run: tailscale serve --https=8443 off
```

**Not port 443.** This Mac already serves `…ts.net:443` → `127.0.0.1:7432` (trustmux).
`serve --https=443` would *replace* that handler without erroring, so `443` is in
`tailnet.RESERVED_TLS_PORTS` and `serve_start` refuses it.

Teardown is the `off` target the tool itself prints. It exits 1 with "handler does not
exist" when there is nothing to remove; that is the desired end state and counts as
success.

## Running it without the menubar (development)

```bash
VOICE_AGENT_TOKEN=dev-token \
  .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8767
```

`http://127.0.0.1:8767/` — localhost counts as a secure context, so `getUserMedia` works
without a certificate. Any other host needs the `tailscale serve` front end above.

In that mode `ensure_capabilities()` installs the **Mac's** capabilities, because that is
the machine this process is on. Inside the menubar app it is a no-op.

## Not deployed anywhere, and that is the design

There is no systemd unit, no TLS keypair on disk and no Thrivbe-1 install. The brain is
this Mac (Robin, 2026-08-10); a T1-hosted system is a later project. `mac/reverse_channel.py`
— the HTTP surface a T1 brain would have called back into — stays committed and **off by
default** for that project. Nothing here turns it on or depends on it.
