"""Realtime tool schema + the helpers behind the tools.

Extracted from realtime.py: the flat Realtime `TOOLS` schema and the standalone
helpers for delegation (background agent), clipboard/paste, and JSON extraction.
LiveSession._do_tool dispatches to these.
"""
import json
import os
import shlex
import subprocess
import time

import config


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"tools: {msg}")
    except Exception:
        pass


# Tools that exist ONLY on this Mac (no kernel manifest entry) and must still be
# confirmed out loud before they execute. Kernel-backed tools declare this through the
# manifest's highStakes flag instead — never list one in both places; check_tool_drift
# fails on double-declaration precisely because two sources of truth for "is this
# dangerous" is how a gate silently goes missing.
LOCAL_HIGH_STAKES = frozenset({"gmail_send"})


# Realtime tool schema is flat (name/parameters at top level), unlike the
# chat-completions nested {"function": {...}} shape in agent.py.
TOOLS = [
    {
        "type": "function",
        "name": "run_shell",
        "description": "Run a command in the persistent shell (starts in Robin's configured base folder; cd/env persist; can act anywhere on the Mac). Use it to read, search, and act. To hand off a slow coding/research task, ALWAYS use the `delegate` tool (it runs `pi` in the background and auto-wakes with the result) — do NOT shell out to `claude` or `pi` yourself.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Explicitly save one durable fact or preference about the user into your long-term memory (you already learn most things automatically — use this only when the user clearly wants something kept). Don't use it to re-store something you just recalled.",
        "parameters": {"type": "object",
                       "properties": {"note": {"type": "string"}},
                       "required": ["note"]},
    },
    {
        "type": "function",
        "name": "recall",
        "description": "Search your memory — past conversations and the user's wider knowledge — for relevant context. Use it when the user refers to something from before, or when you need background you don't already have in this conversation.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "put_text",
        "description": "Put text into the app the user is in: copies it to the clipboard and (by default) pastes it into the frontmost window with Cmd-V. Use for writing/inserting/pasting into emails, docs, fields.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "paste": {"type": "boolean",
                                                "description": "true (default) = paste into the front window; false = only copy to clipboard"}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "delegate",
        "description": "Hand a slow coding/research task to a background AI agent in its own named herdr lane. Returns immediately and survives the conversation; when it finishes the voice agent automatically comes back and speaks the result. Don't wait or poll. Give it a short task_name so Robin can refer to it later ('continue the routing fix').",
        "parameters": {"type": "object",
                       "properties": {"instruction": {"type": "string"},
                                      "task_name": {"type": "string",
                                                    "description": "Short kebab-case handle, e.g. 'routing-fix'. Optional; derived from the instruction if omitted."}},
                       "required": ["instruction"]},
    },
    {
        "type": "function",
        "name": "delegate_status",
        "description": "List Robin's delegated background tasks: each task's name and whether it is working, blocked, waiting for input, or finished. Use before continue_task if unsure which task Robin means.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "continue_task",
        "description": "Send follow-up feedback into a specific running delegated task by name. If it's ambiguous which task Robin means, check delegate_status and ask him instead of guessing.",
        "parameters": {"type": "object",
                       "properties": {"task_name": {"type": "string"},
                                      "feedback": {"type": "string"}},
                       "required": ["task_name", "feedback"]},
    },
    {
        "type": "function",
        "name": "close_finished_tasks",
        "description": "Close the lanes of finished delegated tasks. Never touches running or blocked ones. Pass task_name to close one specific task explicitly (allowed even if unfinished, when Robin says so).",
        "parameters": {"type": "object",
                       "properties": {"task_name": {"type": "string"}},
                       "required": []},
    },
    {
        "type": "function",
        "name": "os_delegate",
        "description": "Hand BUSINESS/SYSTEM work to Robin's thrivbe-os worker: CRM updates, approvals, follow-ups/chasing, or anything in Robin's operating system. This only waits for the OS to accept the job; Robin gets a Telegram approval or summary later. For plain task capture use notion_create_task instead (instant, no approval loop); for Mac coding/research use the local `delegate` tool.",
        "parameters": {"type": "object",
                       "properties": {"instruction": {"type": "string"}},
                       "required": ["instruction"]},
    },
    {
        "type": "function",
        "name": "notion_create_task",
        "description": "Create a task directly in Robin's Notion Tasks database. Instant — no approval loop. Defaults are applied automatically (assigned to Robin, status Next Up, near-term due date if none given).",
        "parameters": {"type": "object",
                       "properties": {"title": {"type": "string"},
                                      "due": {"type": "string", "description": "YYYY-MM-DD; omit to default near-term"},
                                      "notes": {"type": "string"},
                                      "status": {"type": "string", "description": "Defaults to 'Next Up'"}},
                       "required": ["title"]},
    },
    {
        "type": "function",
        "name": "notion_search",
        "description": "Search Robin's Notion workspace (pages and databases) by keyword; speaks the top hits.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "notion_list_tasks",
        "description": "Read tasks from Robin's Notion Tasks database — filter by status (e.g. Focus, Backlog, Next Up, In Progress, Done) and/or a title keyword. Defaults to everything not Done. Use to answer what's on my list, what's in Focus, or what's overdue-sounding.",
        "parameters": {"type": "object",
                       "properties": {
                           "status": {"type": "string",
                                      "description": "Exact status name, e.g. Focus, Backlog, Next Up, Waiting, In Progress, Done. Omit for all open tasks."},
                           "query": {"type": "string", "description": "Optional title keyword filter."}},
                       "required": []},
    },
    {
        "type": "function",
        "name": "notion_update_task",
        "description": "Update an existing Notion task's status and/or due date, found by its title (e.g. move a task from Focus to Backlog, or push a due date). If the title matches more than one task ambiguously, this asks Robin to say the exact title instead of guessing.",
        "parameters": {"type": "object",
                       "properties": {
                           "title": {"type": "string", "description": "The task's title, as close to exact as possible."},
                           "status": {"type": "string",
                                      "description": "New status, e.g. Focus, Backlog, Next Up, Waiting, In Progress, Done."},
                           "due": {"type": "string", "description": "New due date, YYYY-MM-DD."}},
                       "required": ["title"]},
    },
    {
        "type": "function",
        "name": "front_search",
        "description": "Search Robin's Front email conversations (client communication) by keyword.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "front_draft",
        "description": "Create an email DRAFT in Front (never sends — Robin reviews and sends it there). Use for client/outreach email.",
        "parameters": {"type": "object",
                       "properties": {"to": {"type": "string"},
                                      "subject": {"type": "string"},
                                      "body": {"type": "string", "description": "HTML or plain text body"}},
                       "required": ["to", "subject", "body"]},
    },
    {
        "type": "function",
        "name": "gmail_search",
        "description": "Search Robin's Gmail (robin@thrivbe.com) with normal Gmail search syntax; speaks a short summary of the top mails.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "gmail_send",
        "description": "Send an email from robin@thrivbe.com. This STAGES the send and requires Robin's spoken confirmation before anything goes out — state the recipient and subject, then ask him to confirm.",
        "parameters": {"type": "object",
                       "properties": {"to": {"type": "string"},
                                      "subject": {"type": "string"},
                                      "body": {"type": "string"}},
                       "required": ["to", "subject", "body"]},
    },
    {
        "type": "function",
        "name": "calendar_add",
        "description": "Create a Google Calendar event (Oslo time).",
        "parameters": {"type": "object",
                       "properties": {"title": {"type": "string"},
                                      "date": {"type": "string", "description": "YYYY-MM-DD"},
                                      "time": {"type": "string", "description": "HH:MM 24h"},
                                      "duration": {"type": "integer", "description": "minutes, default 30"},
                                      "attendees": {"type": "string", "description": "comma-separated emails"},
                                      "meet": {"type": "boolean", "description": "add a Google Meet link"}},
                       "required": ["title", "date", "time"]},
    },
    {
        "type": "function",
        "name": "drive_search",
        "description": "Search Robin's Google Drive by file name/content; speaks the top matches.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "set_prompt",
        "description": "Save (overwrite) your persistent custom instructions — your persona and standing context about the user and their workspace. Reloaded at the start of every future conversation. Use when the user asks you to remember how to behave, or after researching their setup to write your own briefing.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "kernel_status",
        "description": "Check what needs Robin's attention right now: pending approvals awaiting his decision, attention items surfaced by the OS, and recent agent runs. Use this for questions like what needs my attention, what's pending, or what has the OS been doing.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "kernel_decide",
        "description": "Approve or reject a pending kernel approval by id. Never call this based on inference. Only after you have read the approval's preview aloud and the user gave an explicit yes or no.",
        "parameters": {"type": "object",
                       "properties": {
                           "approvalId": {"type": ["integer", "string"],
                                          "description": "The approval id from a prior kernel_status call."},
                           "decision": {"type": "string", "enum": ["approve", "reject"],
                                        "description": "Robin's explicit yes/no decision."}},
                       "required": ["approvalId", "decision"]},
    },
    {
        "type": "function",
        "name": "kernel_memo",
        "description": "File a short note into Robin's unified inbox. Use for requests like note that down, remind me, or add this to my inbox.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "description": "The memo text, 1-4000 characters."}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "kernel_remember",
        "description": "Store a durable fact in Robin's ONE memory (shared across voice, Telegram, and the OS). Use for 'remember that...', preferences, decisions, or context worth keeping. Distinct from kernel_memo, which files an inbox item for triage.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "description": "The fact to remember, 1-4000 characters."}},
                       "required": ["text"]},
    },
    {
        "type": "function",
        "name": "kernel_recall",
        "description": "Search Robin's ONE memory (remembered notes first, then CRM/tasks/wiki) for facts or context. Use for 'what did I say about...', 'do you remember...', or any question about stored knowledge.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "bloom_create_task",
        "description": "Request creation of a new task in a Bloom project. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the task is created.",
        "parameters": {"type": "object",
                       "properties": {
                           "projectId": {"type": ["integer", "string"],
                                         "description": "The Bloom project id to add the task to."},
                           "title": {"type": "string", "description": "The task title."},
                           "description": {"type": "string",
                                           "description": "Optional task description."}},
                       "required": ["projectId", "title"]},
    },
    {
        "type": "function",
        "name": "bloom_update_task",
        "description": "Request an update to an existing Bloom task, such as status or title. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the task is updated.",
        "parameters": {"type": "object",
                       "properties": {
                           "id": {"type": ["integer", "string"], "description": "The Bloom task id."},
                           "status": {"type": "string", "enum": ["todo", "in_progress", "done"],
                                      "description": "Optional new task status."},
                           "title": {"type": "string", "description": "Optional new task title."}},
                       "required": ["id"]},
    },
    {
        "type": "function",
        "name": "bloom_comment_task",
        "description": "Request adding a comment to an existing Bloom task. This is gated: it files an approval for Robin and does not take effect until he approves it — tell him that plainly rather than saying the comment is posted.",
        "parameters": {"type": "object",
                       "properties": {
                           "id": {"type": ["integer", "string"], "description": "The Bloom task id."},
                           "body": {"type": "string", "description": "The comment text."}},
                       "required": ["id", "body"]},
    },
    {
        "type": "function",
        "name": "twenty_search_contacts",
        "description": "Search for people by first or last name in Twenty CRM.",
        "parameters": {"type": "object",
                       "properties": {"name_query": {"type": "string",
                                                     "description": "The name or partial name to search for."}},
                       "required": ["name_query"]},
    },
    {
        "type": "function",
        "name": "semsearch_query",
        "description": "Run semantic search against Robin's LinkedIn connections (people), Notion knowledge base (notion), or Obsidian wiki and agent skills (wiki_skills). Use wiki_skills for books, book summaries, abstracts, wiki pages, and skills.",
        "parameters": {"type": "object",
                       "properties": {
                           "query": {"type": "string", "description": "The natural language query."},
                           "corpus": {"type": "string",
                                      "enum": ["people", "notion", "wiki_skills"],
                                      "default": "people",
                                      "description": "Defaults to people."}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "hybrid_rag_search",
        "description": "Hybrid RAG search across system-graph relations and wiki, skills, and codebases. Returns a compact answer with relevant entities.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "cognee_ask",
        "description": "Deep search of the Beeper community knowledge graph — people, groups,"
        " roles, who-knows-whom, open asks across Robin's group chats. SLOW (~20-30s):"
        " tell the user you're looking it up BEFORE calling, then continue when it returns.",
        "parameters": {"type": "object",
                       "properties": {
                           "query": {"type": "string",
                                     "description": "The question, phrased naturally."},
                           "mode": {"type": "string",
                                    "enum": ["GRAPH_COMPLETION", "RAG_COMPLETION", "INSIGHTS",
                                             "CHUNKS", "SUMMARIES", "TEMPORAL"],
                                    "description": "Defaults to GRAPH_COMPLETION."}},
                       "required": ["query"]},
    },
    {
        "type": "function",
        "name": "graph_get_node",
        "description": "Inspect a node and its nearby relationships in the system graph.",
        "parameters": {"type": "object",
                       "properties": {"id": {"type": "string"},
                                      "depth": {"type": "integer"}},
                       "required": ["id"]},
    },
    {
        "type": "function",
        "name": "graph_get_document",
        "description": "Read a markdown document or wiki page from the Command Center catalog by graph node ID.",
        "parameters": {"type": "object",
                       "properties": {"id": {"type": "string"}},
                       "required": ["id"]},
    },
    {
        "type": "function",
        "name": "bloom_list_projects",
        "description": "List active Bloom projects to find project IDs.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "bloom_list_tasks",
        "description": "List tasks in a Bloom project board; use hybrid_rag_search for global search.",
        "parameters": {"type": "object",
                       "properties": {"projectId": {"type": "integer"},
                                      "status": {"type": "string", "enum": ["todo", "in_progress", "done"]},
                                      "q": {"type": "string"}},
                       "required": []},
    },
    {
        "type": "function",
        "name": "list_inbox_items",
        "description": "List inbox items from email, SMS, Beeper, and LinkedIn.",
        "parameters": {"type": "object",
                       "properties": {"triage_status": {"type": "string", "enum": ["unprocessed", "triaged", "snoozed", "archived", "actioned", "all"]},
                                      "search": {"type": "string"},
                                      "limit": {"type": "integer"}},
                       "required": []},
    },
    {
        "type": "function",
        "name": "hermes_fleet",
        "description": "Read-only status of Robin's Hermes agent fleets: what each is running, reviewing, blocked on, or has queued. Omit pod for a summary of every fleet; pass pod for that one's task list. This only REPORTS — it never creates or assigns work, so use os_delegate to hand off a task.",
        # No enum on `pod` — the roster is data (bridge/context/hermes-fleet.json) and
        # the kernel's 400 names the valid ids, so a new pod needs no schema edit.
        "parameters": {"type": "object",
                       "properties": {"pod": {"type": "string",
                                              "description": "Pod id from the fleet summary — currently helm (Helm org), grown (grown shop), pod-01 (Mingle), pod-02 (Ania), pod-03 (grown front desk). Omit for a fleet-wide summary."},
                                      "status": {"type": "string",
                                                 "enum": ["todo", "ready", "running", "review",
                                                          "scheduled", "blocked", "triage", "done", "archived"],
                                                 "description": "Optional filter; open tasks only by default."}},
                       "required": []},
    },
]


# Verify-or-be-honest harness wrapped around EVERY delegated instruction. The
# background agent has file/bash tools, so it can observe the real end-state — this
# forces it to, instead of declaring success off a proxy (a script's "done" echo, a
# tool's own success message). The closing VERIFIED/UNVERIFIED/FAILED tag is what the
# voice agent relays out loud, so an honest "couldn't confirm" reaches the user.
#
# The text lives in delegate-harness.md and is POINTED AT, not inlined: it's ~250 words
# on every delegated call, and having it in a file means it can be edited and iterated
# without touching code. A pointer also works for both `claude` and `pi` delegates —
# both can read a file, whereas a Claude-Code skill would only load for one of them.
HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delegate-harness.md")


def _verify_wrap(instruction: str) -> str:
    """Point the delegate at its working contract, then give it the task.

    Raises if the harness file is missing rather than silently delegating without it —
    an unharnessed agent reports success it never checked, which is the exact failure
    this whole mechanism exists to prevent. Callers turn this into a spoken refusal.
    """
    if not os.path.isfile(HARNESS_PATH):
        raise FileNotFoundError(f"delegation harness missing: {HARNESS_PATH}")
    return (f"FIRST: read {HARNESS_PATH} and follow it exactly — it is your working "
            f"contract for this task.\n\n--- TASK ---\n{instruction}")


# Claude-mode delegation runs Sonnet as an ORCHESTRATOR: it plans and does routine
# work itself, and escalates only the genuinely hard parts to Fable subagents (or
# fans out mechanical sweeps to Haiku). Keeps most voice-delegated tasks fast and
# cheap while hard problems still get the strongest model.
ORCHESTRATOR_HARNESS = """You are the ORCHESTRATOR for this task. Handle planning and routine work yourself. When a subtask genuinely needs deeper reasoning than you can confidently deliver — hard architecture, gnarly debugging, high-stakes writing — spawn a subagent via the Agent tool with model "fable" for it; for large mechanical fan-out (many similar small lookups/edits) use model "haiku" subagents in parallel. Small or straightforward tasks: just do them yourself, no subagents."""


def _orchestrator_wrap(instruction: str, mode: str) -> str:
    """Prepend the orchestrator harness for claude-mode delegation only."""
    if mode != "claude":
        return instruction
    return f"{ORCHESTRATOR_HARNESS}\n\n{instruction}"


def _completion_signal(out: str, done: str) -> str:
    """Instruction appended to WATCHED tasks. An interactive agent never exits, so the
    shell can't drop the .done sentinel — instead the agent self-reports: its last action
    writes the final report (incl. the VERIFIED/… tag) to <out> and touches <done>, which
    the menubar poller already watches to auto-wake and speak. No duplicate run, no stream
    parsing — same wake path as headless."""
    return ("\n\n--- SIGNAL COMPLETION (REQUIRED) ---\n"
            "You are running in a terminal the user is watching live, and your process will "
            "NOT exit on its own — so the voice agent only learns you're done if you tell it. "
            "As your VERY LAST action, after you have finished AND verified, use your shell "
            "tool exactly once to write your final report (a short summary plus the "
            "VERIFIED/UNVERIFIED/FAILED tag line) to the result file and then create the done "
            "marker:\n"
            f"  cat > {shlex.quote(out)} <<'REPORT'\n"
            "  <your final summary and the VERIFIED/UNVERIFIED/FAILED tag>\n"
            "REPORT\n"
            f"  touch {shlex.quote(done)}\n"
            "Without this, the user never hears your result.")


# --- herdr lanes: the registry for delegated background agents ---------------
# Watched delegate tasks run as named lanes in a dedicated `voice` herdr
# workspace instead of anonymous Terminal.app windows. herdr's own registry
# (`agent list`) supplies liveness (working/idle/blocked); our `<tid>.lane`
# sidecar files in TASKS_DIR join tasks to panes; the `.done` sentinel remains
# the ONLY signal of completion (herdr `idle` just means claude finished a turn).

HERDR = os.path.expanduser("~/.local/bin/herdr")
LANE_PREFIX = "voice-"


def _herdr(*args, timeout: int = 10):
    """Run one herdr CLI command; return its parsed `result` dict, or None on any
    failure (server down, timeout, bad JSON). Single seam for all herdr access."""
    try:
        r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout).get("result")
    except Exception:
        return None


def _herdr_up() -> bool:
    # NOT via _herdr(): `status --json` has a different shape (no `result` wrapper)
    # and exits 0 even when the server is down — liveness is in server.running.
    try:
        r = subprocess.run([HERDR, "status", "--json"],
                           capture_output=True, text=True, timeout=3)
        return bool(json.loads(r.stdout).get("server", {}).get("running"))
    except Exception:
        return False


def _voice_workspace(ws_dir: str):
    """Workspace id of the `voice` herdr workspace, creating it if missing.
    Discovered by label each time — stateless across app restarts."""
    listed = _herdr("workspace", "list")
    for w in (listed or {}).get("workspaces", []):
        if w.get("label") == "voice":
            return w.get("workspace_id")
    created = _herdr("workspace", "create", "--cwd", ws_dir, "--label", "voice", "--no-focus")
    return ((created or {}).get("workspace") or {}).get("workspace_id")


def _voice_lanes() -> list:
    """Live voice-owned lanes from herdr's registry."""
    listed = _herdr("agent", "list")
    return [a for a in (listed or {}).get("agents", [])
            if (a.get("name") or "").startswith(LANE_PREFIX)]


def _lane_sidecars() -> dict:
    """pane_id -> {'name','pane_id','tid','mtime'} from the newest sidecar per pane.
    Sidecars are `<tid>.lane` files; the newest one for a pane names the tid whose
    .done sentinel decides whether that lane's task is finished."""
    out = {}
    try:
        for f in os.listdir(config.TASKS_DIR):
            if not f.endswith(".lane"):
                continue
            p = os.path.join(config.TASKS_DIR, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    rec = json.load(fh)
                rec["tid"] = f[:-5]
                rec["mtime"] = os.path.getmtime(p)
            except Exception:
                continue
            prev = out.get(rec.get("pane_id"))
            if prev is None or rec["mtime"] > prev["mtime"]:
                out[rec["pane_id"]] = rec
    except FileNotFoundError:
        pass
    return out


def _own_pane(pane_id: str, lanes=None) -> bool:
    """Ownership chokepoint: True only if this pane is a live voice-prefixed lane
    AND one of our sidecars points at it. Every mutating herdr call must pass
    through this — it is the mechanical 'never touch foreign/active work' guarantee."""
    if not pane_id or pane_id not in _lane_sidecars():
        return False
    for a in (lanes if lanes is not None else _voice_lanes()):
        if a.get("pane_id") == pane_id and (a.get("name") or "").startswith(LANE_PREFIX):
            return True
    return False


def _lane_send(pane_id: str, text: str, lanes=None) -> bool:
    if not _own_pane(pane_id, lanes):
        return False
    if _herdr("agent", "send", pane_id, text) is None:
        return False
    _herdr("pane", "send-keys", pane_id, "enter")
    # Documented gotcha: a multi-line paste may need a second Enter to submit.
    time.sleep(1.5)
    for a in _voice_lanes():
        if a.get("pane_id") == pane_id and a.get("agent_status") == "idle":
            _herdr("pane", "send-keys", pane_id, "enter")
    return True


def _lane_close(pane_id: str, lanes=None) -> bool:
    if not _own_pane(pane_id, lanes):
        return False
    return _herdr("pane", "close", pane_id) is not None


def _slug(text: str) -> str:
    words = [w.strip(".,:;!?\"'").lower() for w in (text or "").split()[:3]]
    return "-".join(w for w in words if w) or "task"


def _lane_done(rec) -> bool:
    """A lane's task is finished iff its newest tid's .done sentinel exists."""
    return bool(rec) and os.path.exists(os.path.join(config.TASKS_DIR, f"{rec['tid']}.done"))


def _age_min(mtime: float) -> int:
    return max(0, int((time.time() - mtime) / 60))


def _task_paths():
    """Fresh (tid, prompt, out, done) paths; de-collides same-second builds."""
    config.ensure_dirs()
    tid = time.strftime("%H%M%S")
    while os.path.exists(os.path.join(config.TASKS_DIR, f"{tid}.prompt")):
        tid += "b"
    j = lambda ext: os.path.join(config.TASKS_DIR, f"{tid}{ext}")
    return tid, j(".prompt"), j(".out"), j(".done")


def _build_delegate_cmd(instruction: str, cfg: dict):
    """HEADLESS fallback: write the instruction to a prompt file and return
    (shell command, out_path) that runs the background agent DETACHED, capturing
    output to TASKS_DIR/<id>.out and dropping a .done sentinel on completion (the
    menubar watcher polls for it to auto-wake and speak the result). Returns None
    if delegation is off or empty. Watched delegation goes via delegate_task."""
    instruction = (instruction or "").strip()
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if not instruction or mode == "off":
        return None
    if mode == "claude":
        agent_cmd = f"claude -p --model {live.get('claude_model', 'sonnet')} --permission-mode acceptEdits"
    else:
        agent_cmd = f"pi -p --model {live.get('pi_model', 'deepseek-v4-flash')}"
    try:
        # Inside the try: a missing harness file must degrade to "couldn't start it",
        # never to an unharnessed delegate.
        instruction = _verify_wrap(_orchestrator_wrap(instruction, mode))
        _tid, pf, out, done = _task_paths()
        with open(pf, "w", encoding="utf-8") as f:
            f.write(instruction)
        # $(cat prompt) avoids any shell-injection from the instruction text itself.
        # </dev/null is essential: detached under the live shell, the agent would
        # otherwise inherit an open stdin that never EOFs and block forever (0% CPU,
        # no output, no .done — so the auto-wake never fires). cd into the workspace
        # first — the live shell's cwd drifts with the conversation, and the agent
        # must always start in the trusted workspace (loads CLAUDE.md, no trust prompt).
        ws = os.path.expanduser(live.get("workspace") or "~")
        runner = (f'cd {shlex.quote(ws)} && '
                  f'{agent_cmd} "$(cat {shlex.quote(pf)})" </dev/null > {shlex.quote(out)} 2>&1; '
                  f'touch {shlex.quote(done)}')
        cmd = f"nohup sh -c {shlex.quote(runner)} >/dev/null 2>&1 & disown"
        return (cmd, out)
    except Exception as e:
        _log(f"delegate build failed: {e!r}")
        return None


def delegate_task(instruction: str, cfg: dict, task_name: str = "", run_shell=None) -> str:
    """Launch a watched delegate as a named herdr lane in the `voice` workspace.
    Falls back to the headless path (via run_shell) when herdr is down or watching
    is disabled. Returns the spoken confirmation string."""
    instruction = (instruction or "").strip()
    live = cfg.get("live") or {}
    mode = live.get("delegate", "pi")
    if not instruction or mode == "off":
        return "couldn't start it (delegation is off or the instruction was empty)"
    watch = bool(live.get("show_task_terminals"))

    def _headless(reason: str = "") -> str:
        built = _build_delegate_cmd(instruction, cfg)
        if not built:
            return ("couldn't start it (delegation is off, the instruction was empty, "
                    "or the delegation harness file is missing — check the log)")
        cmd, _out = built
        if run_shell:
            run_shell(cmd)
        else:
            subprocess.Popen(["sh", "-c", cmd])
        base = "Started it in the background — I'll come back with the result when it's done."
        return f"{reason} {base}".strip()

    if not watch:
        return _headless()
    if not _herdr_up():
        return _headless("herdr isn't running, so you can't watch this one —")

    name = LANE_PREFIX + _slug(task_name or instruction)
    taken = {a.get("name") for a in _voice_lanes()}
    n, i = name, 2
    while n in taken:
        n, i = f"{name}-{i}", i + 1
    name = n

    try:
        wrapped = _verify_wrap(_orchestrator_wrap(instruction, mode))
        _tid, pf, out, done = _task_paths()
        with open(pf, "w", encoding="utf-8") as f:
            f.write(wrapped + _completion_signal(out, done))
        ws = os.path.expanduser(live.get("workspace") or "~")
        wsid = _voice_workspace(ws)
        if not wsid:
            return _headless("I couldn't reach the voice workspace, so")
        # Shell first, never the raw binary: the `claude` zsh function (with bypass
        # permissions baked in) only resolves through zsh — the raw binary would
        # silently hang lanes on permission prompts nobody answers.
        started = _herdr("agent", "start", name, "--cwd", ws, "--workspace", wsid,
                         "--split", "right", "--no-focus", "--", "zsh")
        pane_id = ((started or {}).get("agent") or {}).get("pane_id")
        if not pane_id:
            return _headless("I couldn't open a lane, so")
        if mode == "claude":
            run_cmd = f"claude --model {live.get('claude_model', 'sonnet')}"
        else:
            run_cmd = f"pi --model {live.get('pi_model', 'deepseek-v4-flash')}"
        # pane run = text + Enter atomically; the pane's shell expands $(cat …), so
        # the multi-KB prompt never gets typed and the Enter gotcha never applies.
        _herdr("pane", "run", pane_id, f'{run_cmd} "$(cat {shlex.quote(pf)})"')
        with open(os.path.join(config.TASKS_DIR, f"{_tid}.lane"), "w", encoding="utf-8") as f:
            json.dump({"name": name, "pane_id": pane_id}, f)
        spoken = name[len(LANE_PREFIX):].replace("-", " ")
        return (f"Started it as '{spoken}' in your voice workspace — "
                "I'll come back with the result when it's done.")
    except Exception as e:
        _log(f"lane launch failed: {e!r}")
        return _headless("the lane launch failed, so")


def _lane_state(a, sidecars) -> str:
    """One word of speakable state for a live lane."""
    rec = sidecars.get(a.get("pane_id"))
    if _lane_done(rec):
        return "finished"
    st = a.get("agent_status")
    if st == "working":
        return "working"
    if st == "blocked":
        return "blocked, probably on a permission prompt"
    if st == "unknown":
        return "starting up"
    return "paused, probably waiting for your input"


def delegate_status(args: dict = None) -> str:
    if not _herdr_up():
        return "herdr isn't running, so there are no watchable tasks. Headless ones still announce themselves when done."
    lanes = _voice_lanes()
    if not lanes:
        return "No delegated tasks in the voice workspace right now."
    sidecars = _lane_sidecars()
    parts = []
    for a in lanes:
        spoken = (a.get("name") or "")[len(LANE_PREFIX):].replace("-", " ")
        rec = sidecars.get(a.get("pane_id"))
        age = f", started {_age_min(rec['mtime'])} minutes ago" if rec else ""
        parts.append(f"{spoken}: {_lane_state(a, sidecars)}{age}")
    n = len(lanes)
    return f"{n} task{'s' if n > 1 else ''} — " + "; ".join(parts) + "."


def _find_lane(task_name: str, lanes):
    """Resolve a spoken/kebab name to a live lane, forgiving the voice- prefix."""
    want = _slug(task_name) if " " in (task_name or "") else (task_name or "").lower().strip()
    want = want[len(LANE_PREFIX):] if want.startswith(LANE_PREFIX) else want
    want = want.replace(" ", "-")
    for a in lanes:
        if (a.get("name") or "")[len(LANE_PREFIX):] == want:
            return a
    return None


def continue_task(args: dict) -> str:
    task_name = (args.get("task_name") or "").strip()
    feedback = (args.get("feedback") or "").strip()
    if not task_name or not feedback:
        return "I need both the task name and the feedback."
    if not _herdr_up():
        return "herdr isn't running — I can't reach that task's lane."
    lanes = _voice_lanes()
    lane = _find_lane(task_name, lanes)
    if lane is None:
        return f"I don't see a task called {task_name}. Ask me for the task list."
    # New tid + sentinel so the auto-wake fires again for this follow-up. Keep the
    # message single-line: multi-line pastes need extra Enters to submit.
    _tid, _pf, out, done = _task_paths()
    open(_pf, "w", encoding="utf-8").write(feedback)  # de-collision marker + audit trail
    msg = (" ".join(feedback.split())
           + f" — when this follow-up is done, write your updated summary to {out} "
           f"and then run: touch {shlex.quote(done)}")
    if not _lane_send(lane["pane_id"], msg, lanes):
        return "That lane didn't accept input — it may have just closed."
    with open(os.path.join(config.TASKS_DIR, f"{_tid}.lane"), "w", encoding="utf-8") as f:
        json.dump({"name": lane["name"], "pane_id": lane["pane_id"]}, f)
    spoken = lane["name"][len(LANE_PREFIX):].replace("-", " ")
    return f"Passed that on to {spoken} — I'll speak up when it reports back."


def close_finished_tasks(args: dict = None) -> str:
    task_name = ((args or {}).get("task_name") or "").strip()
    if not _herdr_up():
        return "herdr isn't running — nothing to close."
    lanes = _voice_lanes()
    sidecars = _lane_sidecars()
    if task_name:
        lane = _find_lane(task_name, lanes)
        if lane is None:
            return f"I don't see a task called {task_name}."
        spoken = lane["name"][len(LANE_PREFIX):].replace("-", " ")
        if _lane_close(lane["pane_id"], lanes):
            return f"Closed {spoken}."
        return f"I couldn't close {spoken} — it isn't a lane I own."
    closed, kept = [], []
    for a in lanes:
        spoken = (a.get("name") or "")[len(LANE_PREFIX):].replace("-", " ")
        if _lane_done(sidecars.get(a.get("pane_id"))):
            (closed if _lane_close(a["pane_id"], lanes) else kept).append(spoken)
        else:
            kept.append(f"{spoken} ({_lane_state(a, sidecars)})")
    if not closed:
        return "Nothing was finished, so I closed nothing. " + (
            f"Still open: {'; '.join(kept)}." if kept else "")
    res = f"Closed {len(closed)}: {', '.join(closed)}."
    if kept:
        res += f" Left open: {'; '.join(kept)}."
    return res


def _extract_json(text: str):
    """Best-effort: pull the first JSON object out of a model's output (it may wrap it
    in prose or ```json fences). Returns the parsed dict, or None if there's no valid
    object — callers degrade to treating the raw text as a plain summary."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _put_text(text: str, paste: bool = True) -> str:
    """Copy text to the clipboard and (by default) paste it into the frontmost window.
    Copy always works; auto-paste needs Accessibility — if it can't, the text is still
    on the clipboard and we tell the user to press Cmd-V."""
    text = text or ""
    if not text.strip():
        return "nothing to put"
    try:
        # ponytail: encode bytes ourselves — text=True uses the locale encoding, which is
        # ASCII in the py2app bundle, so em-dashes/emoji crashed pbcopy with a UnicodeError.
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    except Exception as e:
        return f"couldn't reach the clipboard: {e}"
    if not paste:
        return "copied to the clipboard"
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, text=True)
    if r.returncode != 0:
        return "copied to the clipboard — press Cmd-V to paste it in (auto-paste needs Accessibility permission)"
    return "pasted it into the front window"


if __name__ == "__main__":
    # Self-check: delegate prompts must carry the verify harness; herdr lanes must be
    # launched, continued, and closed ONLY through the voice- ownership chokepoint;
    # herdr-down must fall back headless. herdr itself is faked — no server needed.
    import tempfile
    cfg = {"live": {"delegate": "pi", "show_task_terminals": False}}
    orig_tasks = config.TASKS_DIR
    config.TASKS_DIR = tempfile.mkdtemp()
    try:
        # --- headless path -------------------------------------------------
        cmd, out = _build_delegate_cmd("organize my desktop icons", cfg)
        pf = os.path.join(config.TASKS_DIR,
                          [f for f in os.listdir(config.TASKS_DIR) if f.endswith(".prompt")][0])
        written = open(pf, encoding="utf-8").read()
        assert written.startswith("FIRST: read "), "harness pointer missing from delegated prompt"
        assert HARNESS_PATH in written, "harness pointer does not name the harness file"
        # The pointer is only as good as the file it points at.
        assert os.path.isfile(HARNESS_PATH), "harness file missing"
        assert "VERIFIED:" in open(HARNESS_PATH, encoding="utf-8").read(), \
            "harness file lost its VERIFIED/UNVERIFIED/FAILED tag contract"
        assert "--- TASK ---" in written and "desktop icons" in written
        assert "SIGNAL COMPLETION" not in written, "headless prompt must not carry the completion signal"
        assert "ORCHESTRATOR" not in written, "pi mode must not get the orchestrator harness"
        assert _build_delegate_cmd("anything", {"live": {"delegate": "off"}}) is None
        assert _build_delegate_cmd("", cfg) is None
        ccmd, cout = _build_delegate_cmd("z", {"live": {"delegate": "claude",
                                                        "workspace": "~/Thrivbe-AI"}})
        assert "--model sonnet" in ccmd, "claude mode must pin the sonnet orchestrator"
        assert "Thrivbe-AI" in ccmd, "headless claude cmd must cd into the workspace"
        ctext = open(cout[:-4] + ".prompt", encoding="utf-8").read()
        assert "ORCHESTRATOR" in ctext, "claude prompt must carry the orchestrator harness"
        # same-second builds must NOT share a tid anymore
        c2cmd, c2out = _build_delegate_cmd("z2", {"live": {"delegate": "claude"}})
        assert cout != c2out, "tid de-collision failed"

        # Fail LOUD, not silent: no harness file => refuse to launch. An unharnessed
        # delegate reports success it never verified, which is worse than not running.
        _real_harness = HARNESS_PATH
        globals()["HARNESS_PATH"] = "/nonexistent/delegate-harness.md"
        try:
            assert _build_delegate_cmd("x", cfg) is None, "must refuse to delegate without the harness"
        finally:
            globals()["HARNESS_PATH"] = _real_harness

        # --- fake herdr ----------------------------------------------------
        calls = []
        FAKE = {"agents": [], "workspaces": [{"label": "voice", "workspace_id": "w9"}]}

        def fake_herdr(*args, timeout=10):
            calls.append(args)
            if args[:2] == ("workspace", "list"):
                return {"workspaces": FAKE["workspaces"]}
            if args[:2] == ("agent", "list"):
                return {"agents": FAKE["agents"]}
            if args[:2] == ("agent", "start"):
                a = {"name": args[2], "pane_id": "w9:p7", "agent_status": "working"}
                FAKE["agents"].append(a)
                return {"agent": a}
            return {}
        globals()["_herdr"] = fake_herdr
        globals()["_herdr_up"] = lambda: FAKE.get("up", True)

        wcfg = {"live": {"delegate": "claude", "show_task_terminals": True,
                         "workspace": "~/Thrivbe-AI"}}
        spoken = delegate_task("fix the routing bug", wcfg, task_name="routing-fix")
        assert "routing fix" in spoken, spoken
        lanes = [f for f in os.listdir(config.TASKS_DIR) if f.endswith(".lane")]
        assert len(lanes) == 1, "lane sidecar not written"
        rec = json.load(open(os.path.join(config.TASKS_DIR, lanes[0])))
        assert rec == {"name": "voice-routing-fix", "pane_id": "w9:p7"}
        wtext = next(open(os.path.join(config.TASKS_DIR, f), encoding="utf-8").read()
                     for f in os.listdir(config.TASKS_DIR)
                     if f.endswith(".prompt")
                     and "SIGNAL COMPLETION" in open(os.path.join(config.TASKS_DIR, f)).read())
        assert "ORCHESTRATOR" in wtext, "watched claude prompt must carry harness + signal"
        run_calls = [c for c in calls if c[:2] == ("pane", "run")]
        assert run_calls and "claude --model sonnet" in run_calls[0][3], "lane must run claude via the zsh function"

        # ownership chokepoint: foreign panes are untouchable even if live
        FAKE["agents"].append({"name": "orchestrator", "pane_id": "w2:p1", "agent_status": "idle"})
        assert not _lane_close("w2:p1"), "must refuse to close a non-voice lane"
        assert not _lane_send("w2:p1", "hi"), "must refuse to send into a non-voice lane"
        assert not _lane_close("w9:p99"), "must refuse a pane with no sidecar"

        # status + continue + close
        st = delegate_status()
        assert "routing fix" in st and ("working" in st), st
        globals()["time"].sleep = lambda s: None  # skip the 1.5s re-enter wait
        cont = continue_task({"task_name": "routing fix", "feedback": "also check the fallback"})
        assert "routing fix" in cont, cont
        assert any(c[:2] == ("agent", "send") and c[2] == "w9:p7" for c in calls), "feedback must land in the named lane"
        res = close_finished_tasks({})
        assert "closed nothing" in res.lower(), "must not close an unfinished lane"
        # mark the newest tid done -> now closable
        newest = max((f for f in os.listdir(config.TASKS_DIR) if f.endswith(".lane")),
                     key=lambda f: os.path.getmtime(os.path.join(config.TASKS_DIR, f)))
        open(os.path.join(config.TASKS_DIR, newest[:-5] + ".done"), "w").close()
        res = close_finished_tasks({})
        assert "routing fix" in res and "Closed 1" in res, res
        # herdr down -> headless fallback that still says so
        FAKE["up"] = False
        spoken = delegate_task("quick job", wcfg, run_shell=lambda c: None)
        assert "herdr isn't running" in spoken, spoken
        print("tools self-check OK — harness + herdr lanes + ownership chokepoint + headless fallback wired")
    finally:
        config.TASKS_DIR = orig_tasks
