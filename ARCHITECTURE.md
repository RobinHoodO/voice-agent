# Thrivbe Voice (Pam) — System Concept

The single map of what Pam is, what she can touch, and how the pieces connect.
Written 2026-08-04 after the crash post-mortem below.

Companions: `README.md` (install + usage) · `PRODUCT.md` (what it's for, commercially)
· `CLAUDE.md` / `AGENTS.md` (working agreements) · `OS-ORCHESTRATOR-PROPOSAL.md` (where
this is heading). This file is the **structural** one — the skeleton.

---

## 1. What it is, in one paragraph

Pam is a macOS menubar app (`rumps`) that holds a **continuous, full-duplex voice
conversation** with a realtime speech model over a WebSocket. Double-tap `Control` and
she's listening. She is not a dictation tool and not a chatbot with a voice skin: mid-
conversation she can read your screen, run shell commands, search your knowledge bases,
file tasks, and hand long work to background coding agents — then wake you up later and
speak the result. Everything runs locally except the model itself and the services she
calls.

**The one-line mental model:** *a duplex audio pipe into a model that holds 41 tools,
wrapped in a process that survives you walking away.*

---

## 2. The map

Two views. **A** is one turn — the loop that runs every time you speak. **B** is the
system — the layers and everything they reach. Sources in `docs/*.mmd`; re-render with
`npx -y @mermaid-js/mermaid-cli@11 -i docs/<name>.mmd -o docs/<name>.png -b "#0d1017" -s 2`.

### A · One turn

![One turn through Pam](docs/turn-loop.png)

### B · The system

![Thrivbe Voice system map](docs/architecture.png)

<details>
<summary>Mermaid source for B (inline, if you'd rather read it as text)</summary>

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: "ui-sans-serif, -apple-system, Helvetica, sans-serif"
    fontSize: 15px
    lineColor: "#8494ad"
  flowchart:
    rankSpacing: 55
    nodeSpacing: 35
---
flowchart TB

    subgraph L1["① SURFACE · what you touch"]
        direction LR
        HOT["⌨️ double-tap Control<br/><i>agent.py · pynput</i>"]
        PILL["🔵 wave pill + menubar icon<br/><i>pill.py</i>"]
        SET["⚙️ settings<br/><i>settings.py · settings.html</i>"]
    end

    subgraph L2["② EDGE · audio + screen"]
        direction LR
        AUD["🎧 <b>audio.py</b><br/>mic · local VAD · barge-in<br/>playback thread"]
        CTX["👁 <b>macos_context.py</b><br/>cursor text · AX window text<br/>window screenshot"]
        FOC["📌 <b>focus.py</b><br/>pin ONE client/project folder"]
    end

    CORE["⚙️ <b>LiveSession · live_session.py</b><br/>events · tool dispatch · watchdogs · persistence<br/><i>1043 lines — the only orchestrator</i>"]

    subgraph L3["③ MODEL · one active at a time"]
        direction LR
        OA["<b>OpenAI Realtime</b><br/><i>backends/openai_backend.py</i>"]
        GEM["<b>Gemini Live</b><br/><i>backends/gemini_backend.py</i>"]
        BASE["<b>backends/base.py</b><br/>one normalized<br/>event vocabulary"]
    end

    subgraph L4["④ GUARDS · all fail closed"]
        direction LR
        G1["⛔ loop guard<br/><i>3 identical / 45s</i>"]
        G2["⏳ stall watchdog<br/><i>6s · 15s · 35s</i>"]
        G3["💤 idle watchdog<br/><i>90s · 300s</i>"]
        G4["🔒 high-stakes gate<br/><i>kernel manifest ∪ gmail_send</i>"]
        G5["🚫 shell off ⇒ run_shell<br/>+ delegate off the schema"]
    end

    subgraph L5["⑤ REACH · 41 tools"]
        direction LR
        K["🧠 <b>kernel — Thrivbe-1 tailnet</b><br/><i>kernel_tools.py</i><br/>Bloom · ONE memory<br/>semsearch · hybrid RAG · cognee<br/>Twenty CRM · Hermes · inbox"]
        D["🔗 <b>direct services</b><br/><i>services.py · web.py</i><br/>Notion · Front<br/>Gmail 🔒 · Calendar · Drive<br/>web search · read URL"]
        BG["🤖 <b>background agents</b><br/><i>tools.py</i><br/>herdr panes: pi · claude<br/>→ tasks/*.done sentinel"]
        SHELL["🐚 <b>persistent zsh</b><br/><i>shell.py — cd sticks</i>"]
    end

    subgraph L6["⑥ STATE · local, on this Mac"]
        direction LR
        JRN["📓 <b>live-turns.journal</b><br/><i>fsync per turn · crash-safe</i>"]
        DB[("💾 <b>conversations.db</b><br/>transcripts · FTS5 · learnings")]
        CFG[("⚙️ config.json")]
        LOG["📄 agent.log · activity.log"]
    end

    HOT --> CORE
    AUD --> CORE
    CTX --> CORE
    FOC --> CORE
    SET --> CFG
    CFG --> CORE

    CORE <--> BASE
    OA --> BASE
    GEM --> BASE

    CORE --> G4
    G4 -- "approved" --> K
    G4 --> D
    G4 --> BG
    G4 --> SHELL

    CORE -- "every turn" --> JRN
    JRN -. "recovered at launch" .-> DB
    CORE -- "on close" --> DB
    DB -. "recall + learnings seed the prompt" .-> CORE
    BG -. "poller wakes her to SPEAK the result" .-> CORE

    classDef fix fill:#1e4620,stroke:#4ade80,stroke-width:2px,color:#eafff0
    classDef core fill:#3b2f14,stroke:#f0b429,stroke-width:3px,color:#fff6e0
    class G1,G2,JRN fix
    class CORE core
```

</details>

> Green = the reliability guards added 2026-08-04.

---

## 3. The files

| File | Lines | Role |
|---|---:|---|
| `agent.py` | 567 | The rumps menubar app. Hotkey, icon state, task poller, launch. **Entry point.** |
| `live_session.py` | 1043 | One conversation: event loop, tool dispatch, gates, watchdogs, persistence. **The core.** |
| `tools.py` | 1521 | The 41-tool schema + the helpers behind delegation, herdr panes, `put_text`. |
| `audio.py` | 308 | Mic capture → WS pump; model audio → speaker. Local VAD, barge-in, teardown. |
| `backends/` | 463 | Wire protocol per provider. `base.py` normalizes both into one event vocabulary. |
| `kernel_tools.py` | 593 | Stdlib client for the thrivbe-os kernel voice API on Thrivbe-1. |
| `services.py` | 338 | Direct (non-kernel) service calls: Notion, Front, Gmail, Calendar, Drive. |
| `memory.py` | 582 | Conversation store (SQLite + FTS5), durable learnings, crash journal, recall. |
| `macos_context.py` | 311 | Screen context: cursor text, AX window text, window screenshot. |
| `focus.py` | 298 | Pin one client/project folder as the subject; loads its text into context. |
| `live_prompt.py` | 174 | Assembles the system prompt: persona + context + learnings + custom prompt. |
| `config.py` | 268 | `config.json`, defaults, `activity()` feed. |
| `settings.py` / `settings.html` | 327 | The settings UI. |
| `pill.py` | 293 | The floating wave pill. |
| `shell.py` | 121 | Persistent zsh so `cd` sticks across commands. |
| `web.py` | 191 | Web search + URL reading. |

---

## 4. The 41 tools

**Gated (high-stakes).** Staged, read back to you out loud, executed only on a spoken
"yes" within 120s. Membership = the kernel's `highStakes` manifest ∪ `LOCAL_HIGH_STAKES`,
so adding a gate is a manifest edit, not a code change. **Fails closed:** if the kernel is
unreachable, *every* kernel-backed tool is treated as gated.

| Group | Tools |
|---|---|
| 🐚 **Shell & screen** | `run_shell` · `put_text` · `focus` · `set_prompt` |
| 🤖 **Delegation** | `delegate` · `continue_task` · `fleet` · `close_finished_tasks` · `os_delegate` |
| 🧠 **Kernel memory** | `kernel_remember` · `kernel_recall` · `kernel_memo` · `kernel_status` · `kernel_decide` |
| 🔍 **Search** | `semsearch_query` · `hybrid_rag_search` · `cognee_ask` · `graph_get_node` · `graph_get_document` · `web_search` · `read_url` |
| 🌱 **Bloom** | `bloom_create_task` · `bloom_update_task` · `bloom_comment_task` · `bloom_list_projects` · `bloom_list_tasks` |
| 📇 **CRM / fleet / inbox** | `twenty_search_contacts` · `hermes_fleet` · `list_inbox_items` |
| 📝 **Notion** | `notion_create_task` · `notion_search` · `notion_list_tasks` · `notion_update_task` |
| ✉️ **Comms** | `gmail_search` · **`gmail_send`** 🔒 · `front_search` · `front_draft` · `calendar_add` · `drive_search` |
| 💾 **Local memory** | `remember` · `recall` |

`run_shell` and `delegate` are **removed from the schema entirely** unless
`live.agentic_shell` is on — fail-closed, not prompt-enforced.

---

## 5. How a turn actually runs

1. **You speak.** PortAudio callback copies bytes (and does nothing else — heavy work on
   that thread risks input glitches) onto the session's asyncio queue.
2. **VAD decides the turn boundary.** OpenAI does it server-side; on Gemini a local VAD
   with a raised bar while *she's* talking, so an open-ear headset leaking her own voice
   doesn't read as a barge-in.
3. **Speech stops → context is injected.** Screen text (+ optional screenshot) grabbed
   concurrently, hard-capped at 2s so a slow AppleScript can't stall the reply.
4. **Model responds** — audio deltas, and/or tool calls.
5. **Tool call → `_do_tool`.** Loop guard → verbatim-words wrapper for delegating tools →
   high-stakes gate → dispatch → result back → trigger the next response.
6. **Turn recorded** to `_turns` *and* fsync'd to the journal.
7. **Session closes** → transcript to SQLite → background `_learn` pass extracts durable
   learnings (and supersedes contradicted ones) → journal cleared.

**Delegation is the interesting one.** `delegate` hands work to a herdr pane running `pi`
or `claude`, prefixed with *your verbatim ASR words* — ground truth independent of how
faithfully the model paraphrased you into the tool argument. The pane writes a report +
a `.done` sentinel; `agent.py`'s poller sees it, reopens a session, and **speaks the
result at you**, honouring quiet hours.

---

## 6. Failure modes (and what now guards them)

The 2026-08-04 crash, which is why this document exists. Four independent bugs stacked:

| # | Failure | Guard | Test |
|---|---|---|---|
| 1 | Model fired `focus`+`semsearch_query` with identical args **16× in 21s**. Nothing bounded it — every tool result triggers a response that can call the same tool again. Silence to you; enough churn that the server aborted (`1008`). | **Loop guard** — the 3rd identical `(tool, args)` in 45s is refused with an explanation that tells her to answer or ask. Cleared on each new spoken turn. | `test_reliability_2026_08_04.py` ×3 |
| 2 | **72 seconds** of you talking into a session that had stopped answering, with no signal. | **Stall watchdog** — 6s soft tone, 15s "say one sentence out loud", 35s drop the socket and reconnect. Tunable via `live.stall_*`. | ×5 |
| 3 | `_start_audio` called `sd._terminate()` (frees PortAudio's **global** state) while the previous attempt's player thread and streams were still alive → use-after-free on CoreAudio's IO thread → **SIGSEGV**. Reconnect was a coin flip. | Per-attempt `_audio_stop` event; the player owns and closes its own stream; teardown **joins** it; `_audio_dirty` blocks the re-init if it can't prove the join. | ×3 |
| 4 | Transcripts only persisted in `_run`'s `finally` — **a segfault doesn't run `finally`**, so the whole conversation was lost. | Every turn fsync'd to `live-turns.journal`; recovered into SQLite at next launch; cleared on clean save. | ×2 |

Pre-existing guards worth knowing: idle watchdog (90s quiet / 300s cap) so a forgotten-on
mic doesn't answer ambient speech · fail-closed high-stakes set when the kernel is
unreachable · bounded playback queue (drop oldest, not unbounded latency) · `run_shell`/
`delegate` stripped from the schema when the agentic shell is off · herdr argv denylist.

---

## 7. Boundaries

- **Kernel-dependent, not kernel-blocked.** If Thrivbe-1 is unreachable the kernel tools
  fail and the gate set stays maximally conservative — she keeps talking.
- **One writer.** SQLite in WAL; the `_learn` thread and the main thread both write.
- **Privacy is opt-in per layer.** Cursor text on; window text and screenshots off by
  default (`privacy.*`). All three off → she answers from speech alone.
- **Deploy = `./reload_app.sh`** (quit → `build_app.sh` → relaunch). It signs with the
  stable *Thrivbe Voice Dev* identity so Accessibility/Input-Monitoring grants survive.
  Editing the source dir alone changes nothing — modules load from
  `Contents/Resources/lib/python312.zip`. Verify a deploy by the zip's mtime.
- **Use `.venv/bin/python` (3.12).** System `python3` is 3.9 and dies on PEP 604 unions.
