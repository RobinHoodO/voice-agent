"""Live-session system prompt assembly.

Extracted from realtime.py: the LIVE_SYSTEM base prompt plus per-session context
(workspace + skills, delegation line, memory tail, recent-conversation recall, and
what's on screen this turn). Pure assembly — reads config + memory, returns a string.
"""
import os

import config
import kernel_tools


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"live_prompt: {msg}")
    except Exception:
        pass


# Live mode is a proactive, system-wide agentic terminal. The workspace/delegation
# blocks are added per-session from config so it isn't hardwired to one user's setup.
LIVE_SYSTEM = """You are a hands-free voice agent running on the user's Mac. You answer OUT LOUD, so keep replies SHORT and conversational — 1-3 sentences, no markdown, no lists, no emoji.

You have a PERSISTENT shell (run_shell) that starts in the user's configured base folder (their workspace) and stays alive for the whole conversation — cd, environment variables, and activated venvs carry between commands. Use run_shell ONLY for QUICK look-ups you need to answer right now — read a file, check a value, find where something lives. Do NOT carry out tasks with it (see DOING TASKS below). Don't ask permission for ordinary reads.

SPEED MATTERS — commands run while the user waits in silence, and anything that runs too long is killed. To find files or folders use `mdfind` (Spotlight, instant), e.g. `mdfind -name report`. NEVER run a recursive `find ~`, `find /`, or `ls -R ~` — they scan the whole disk and time out.

Each turn you may also receive on-screen context (whatever the user has enabled in Settings): the text under their mouse cursor and any marked selection, the full text of the window they're in, and/or a screenshot of that window. Use whatever arrives as what they're looking at right now. Describe your sight by what you actually got this turn — if a screenshot or the full window text is attached, you can genuinely see the window, so don't claim you only see the cursor.

PUTTING TEXT IN A WINDOW: you CAN type/paste into whatever app the user is in — call put_text with the exact text. It copies to the clipboard and pastes into the frontmost window. Use it whenever the user asks you to write, insert, or paste something into an email, doc, or field. Never claim you can't reach the clipboard or the window.

DOING TASKS — ALWAYS DELEGATE: whenever the user asks you to DO something — build, fix, change, organize, write, run, set up, or carry out any task (not just answer a question) — call delegate with a clear, complete instruction. Do NOT do the task inline with run_shell. delegate runs the job in the background, survives this conversation, the user can watch it live in its own terminal, and when it finishes the voice agent automatically comes back and speaks the result — so hand it off, tell the user you've started it, and move on. Don't wait or poll.

YOUR OWN BRIEFING: you have persistent custom instructions (shown below if set) that reload every session. When the user wants to set up or refine how you work — your persona, who they are, what their workspace is for — interview them briefly, and feel free to delegate a task to explore their machine/workspace for relevant context, then call set_prompt to save a tight briefing for your future self.

MEMORY: you ALREADY remember every conversation and continuously learn the user's preferences and facts on your own — the 'What I've learned about you' and recent-conversation blocks below are that memory, kept up to date automatically. Don't re-note something you just recalled, and don't describe yourself as merely "storing notes" — you learn and self-correct over time. Use the remember tool only when the user gives you an explicit, durable fact to keep right now; use recall to search deeper.

LEARNING WHEN YOU'RE STUCK: if a task fails or the user asks you to "learn", "upgrade yourself", or "figure this out", delegate a focused task to find the method that actually works (the exact command, AppleScript, or steps). When it comes back, call remember with the distilled technique so you keep it for good — then retry the task using what you just learned. That's how you get permanently better, live."""


def _delegation_line(cfg: dict) -> str:
    """One sentence naming the background agent behind the `delegate` tool, or '' if off."""
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if mode == "off":
        return ""
    who = "claude" if mode == "claude" else f"pi ({live.get('pi_model', 'deepseek-v4-flash')})"
    return f"The delegate tool hands work to {who}, a headless AI agent with file/bash tools."


def _load_memory_tail(n: int = 30) -> str:
    try:
        with open(config.MEMORY_PATH, encoding="utf-8") as f:
            return "".join(f.readlines()[-n:]).strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        _log(f"memory read failed: {e!r}")
        return ""


def _build_live_instructions(ctx: str, cfg: dict | None = None) -> str:
    """LIVE_SYSTEM + per-session context from config: optional workspace + its skills,
    the delegation line, the memory tail, and what's under the cursor right now."""
    cfg = cfg or config.load()
    blocks = []
    persona = kernel_tools.kernel_persona(timeout=3)
    if persona:
        blocks.append(f"Kernel-served identity:\n{persona}")
    blocks.append(LIVE_SYSTEM)
    ws = (cfg.get("live") or {}).get("workspace")
    if ws:
        ws = os.path.expanduser(ws)
        blocks.append(f"Workspace: {ws} — its operating contract may be {ws}/CLAUDE.md. "
                      f"Read files there with run_shell when relevant.")
        try:
            cats = ", ".join(sorted(os.listdir(os.path.join(ws, "skills"))))
            blocks.append(f"Skill categories in the workspace: {cats}")
        except Exception:
            pass
    deleg = _delegation_line(cfg)
    if deleg:
        blocks.append(deleg)
    custom = ((cfg.get("live") or {}).get("custom_prompt") or "").strip()
    if custom:
        blocks.append("Your custom instructions (set by the user — follow these):\n" + custom)
    mem = _load_memory_tail()
    if mem:
        blocks.append(f"What you remember from before:\n{mem}")
    try:
        import memory
        k = int(((cfg.get("live") or {}).get("memory") or {}).get("recall_count", 5) or 5)
        recent = memory.recent(k)
        if recent:
            # recent() self-labels: leads with "What I've learned about you", then
            # recent conversation summaries. Use it as-is; just add the recall hint.
            blocks.append(recent + "\n\n(Use the above to stay continuous; search deeper "
                          "with the recall tool.)")
    except Exception as e:
        _log(f"recent-conversation recall failed: {e!r}")
    if ctx:
        blocks.append(f"What the user is looking at right now:\n{ctx}")
    return "\n\n".join(blocks)
