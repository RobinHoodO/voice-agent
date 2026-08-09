"""Direct service access for the voice agent.

Notion gets a tiny in-app REST client (hot path — task capture must be instant).
Front and Google shell out to the workspace's existing production scripts
(skills/front/, skills/google-workspace/) — no clients are duplicated here.
Every handler returns one terse, speakable string (kernel_tools.py convention).
"""
import datetime
import json
import os
import subprocess
import urllib.error
import urllib.request

from core import caps, config

WORKSPACE = os.path.expanduser("~/Thrivbe-AI")
FRONT_SCRIPTS = os.path.join(WORKSPACE, "skills", "front", "scripts")
GOOGLE_SCRIPTS = os.path.join(WORKSPACE, "skills", "google-workspace", "scripts")

# Robin's Tasks DB — property names verified live against the DB schema 2026-07-30.
NOTION_TASKS_DB = "26e1b78a-1a84-81e2-8a26-cfb6ff6a4250"
ROBIN_NOTION_ID = "56185ad5-49e3-486b-ab56-e235593c9710"   # "Robin T. Sverd"


def _log(msg: str) -> None:
    caps.log(f"services: {msg}")


# --- Notion (direct REST, urllib only) ---------------------------------------

def _notion(method: str, path: str, payload: dict = None):
    key = config.secret("notion")
    if not key:
        raise RuntimeError("no Notion key configured")
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def notion_create_task(args: dict) -> str:
    """Create a task in the Tasks DB with Robin's standing defaults:
    Assignee=Robin, Status=Next Up, Due always set (near-term if unspecified)."""
    title = (args.get("title") or "").strip()
    if not title:
        return "I need a title for the task."
    due = (args.get("due") or "").strip()
    defaulted = False
    if not due:
        due = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        defaulted = True
    props = {
        "Task name": {"title": [{"text": {"content": title[:200]}}]},
        "Assignee": {"people": [{"id": ROBIN_NOTION_ID}]},
        "Status": {"status": {"name": args.get("status") or "Next Up"}},
        "Due": {"date": {"start": due}},
    }
    notes = (args.get("notes") or "").strip()
    children = ([{"object": "block", "type": "paragraph",
                  "paragraph": {"rich_text": [{"text": {"content": notes[:1900]}}]}}]
                if notes else [])
    try:
        page = _notion("POST", "/pages", {
            "parent": {"database_id": NOTION_TASKS_DB},
            "properties": props, **({"children": children} if children else {})})
    except Exception as e:
        _log(f"notion create failed: {e!r}")
        return "I couldn't create the task in Notion."
    if not page.get("id"):
        return "Notion didn't confirm the task."
    day = due if not defaulted else f"{due} — I picked that, adjust if needed"
    return f"Task created: {title}, due {day}, assigned to you, status {props['Status']['status']['name']}."


def _task_title(page: dict) -> str:
    t = (page.get("properties", {}).get("Task name", {}) or {}).get("title", [])
    return "".join(x.get("plain_text", "") for x in t) or "Untitled"


def _find_tasks(title_query: str) -> list:
    """Tasks DB query, title contains match (case-insensitive per Notion)."""
    data = _notion("POST", f"/databases/{NOTION_TASKS_DB}/query", {
        "filter": {"property": "Task name", "title": {"contains": title_query}},
        "page_size": 5})
    return data.get("results", [])


def notion_list_tasks(args: dict) -> str:
    """Read task details: list tasks from the Tasks DB, filtered by status
    (defaults to everything open — not Done, not Archived) and/or a title keyword."""
    status = (args.get("status") or "").strip()
    query = (args.get("query") or "").strip()
    if status:
        filters = [{"property": "Status", "status": {"equals": status}}]
    else:
        filters = [{"property": "Status", "status": {"does_not_equal": "Done"}},
                   {"property": "Status", "status": {"does_not_equal": "Archived"}}]
    if query:
        filters.append({"property": "Task name", "title": {"contains": query}})
    filter_obj = filters[0] if len(filters) == 1 else {"and": filters}
    try:
        data = _notion("POST", f"/databases/{NOTION_TASKS_DB}/query", {
            "filter": filter_obj, "page_size": 8,
            "sorts": [{"property": "Due", "direction": "ascending"}]})
    except Exception as e:
        _log(f"notion list tasks failed: {e!r}")
        return "I couldn't read your Notion tasks."
    results = data.get("results", [])
    if not results:
        return f"No {status or 'open'} tasks found."
    lines = []
    for p in results:
        st = ((p.get("properties", {}).get("Status", {}) or {}).get("status") or {}).get("name", "?")
        due = ((p.get("properties", {}).get("Due", {}) or {}).get("date") or {}).get("start")
        lines.append(f"{_task_title(p)} ({st}{', due ' + due if due else ''})")
    return f"{len(lines)} task{'s' if len(lines) != 1 else ''}: " + "; ".join(lines)


def notion_update_task(args: dict) -> str:
    """Update a task's status and/or due date. Resolved by title (contains
    match); an exact title match wins, otherwise ambiguous matches are
    surfaced for Robin to disambiguate rather than guessed at."""
    title_query = (args.get("title") or "").strip()
    if not title_query:
        return "I need the task's title to find it."
    status, due = (args.get("status") or "").strip(), (args.get("due") or "").strip()
    if not status and not due:
        return "I need a new status or due date to update."
    if status:
        from core import tools
        # Hard enum check BEFORE any network call: an off-list status means the model is
        # improvising ("archive" → Done was the 2026-08-08 incident). Refuse and point at
        # the escalation path instead of writing a wrong-but-valid-looking value.
        canon = {s.lower(): s for s in tools.NOTION_TASK_STATUSES}
        if status.lower() not in canon:
            return (f"'{status}' is not a status in the Tasks database, and I won't guess. "
                    "If Robin asked for something beyond Status or Due, say so and call "
                    "`delegate` with his request verbatim.")
        status = canon[status.lower()]
    try:
        matches = _find_tasks(title_query)
    except Exception as e:
        _log(f"notion find task failed: {e!r}")
        return "I couldn't search your Notion tasks."
    if not matches:
        return f"I couldn't find a task matching {title_query}."
    exact = [p for p in matches if _task_title(p).strip().lower() == title_query.lower()]
    if len(exact) == 1:
        page = exact[0]
    elif len(matches) > 1:
        names = "; ".join(_task_title(p) for p in matches[:5])
        return f"That matched {len(matches)} tasks: {names}. Say the exact title."
    else:
        page = matches[0]
    props = {}
    if status:
        props["Status"] = {"status": {"name": status}}
    if due:
        props["Due"] = {"date": {"start": due}}
    try:
        _notion("PATCH", f"/pages/{page['id']}", {"properties": props})
    except Exception as e:
        _log(f"notion update task failed: {e!r}")
        return "I couldn't update the task in Notion."
    bits = [b for b in (f"status to {status}" if status else "",
                        f"due to {due}" if due else "") if b]
    # Include the page URL (don't read it aloud): if this update wasn't what Robin
    # wanted, an escalated `delegate` can target this exact page instead of re-running
    # the contains-match that once rewrote the wrong row 14 times.
    url = page.get("url") or ""
    return (f"Updated {_task_title(page)}: {' and '.join(bits)}."
            + (f" (page: {url} — for delegation, not for speaking)" if url else ""))


def notion_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "I need something to search Notion for."
    try:
        data = _notion("POST", "/search", {"query": query, "page_size": 5})
    except Exception as e:
        _log(f"notion search failed: {e!r}")
        return "I couldn't search Notion."
    names = []
    for r in data.get("results", []):
        title = ""
        for p in (r.get("properties") or {}).values():
            if p.get("type") == "title" and p.get("title"):
                title = "".join(t.get("plain_text", "") for t in p["title"])
                break
        title = title or "".join(t.get("plain_text", "") for t in (r.get("title") or []))
        if title:
            names.append(title[:80])
    if not names:
        return f"Nothing in Notion for {query}."
    return f"Top Notion hits: {'; '.join(names)}."


# --- Front + Google (shell out to the workspace's production scripts) --------

def _script(cmd: list, timeout: int = 45) -> str:
    """Run one of the workspace's service scripts; return its trimmed stdout or ''."""
    try:
        r = subprocess.run(["python3", *cmd], capture_output=True, text=True,
                           timeout=timeout, cwd=WORKSPACE)
        if r.returncode != 0:
            _log(f"script failed rc={r.returncode}: {cmd[0]}: {r.stderr.strip()[:200]}")
            return ""
        return r.stdout.strip()
    except Exception as e:
        _log(f"script error {cmd[0]}: {e!r}")
        return ""


def _clip(text: str, n: int = 600) -> str:
    return text if len(text) <= n else text[:n] + " …and more."


def front_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "I need something to search Front for."
    out = _script([os.path.join(FRONT_SCRIPTS, "search.py"), "--query", query, "--limit", "5"])
    return _clip(out) if out else f"No Front conversations found for {query}."


def front_draft(args: dict) -> str:
    """Create a draft — per Robin's Front convention the draft IS the deliverable;
    nothing is ever sent from here."""
    to, subject = (args.get("to") or "").strip(), (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not (to and subject and body):
        return "I need a recipient, a subject, and a body for the draft."
    out = _script([os.path.join(FRONT_SCRIPTS, "create_draft.py"),
                   "--to", to, "--subject", subject, "--body", body])
    if not out:
        return "I couldn't create the Front draft."
    return f"Draft to {to} is in Front — review and send it there."


def gmail_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "I need a Gmail search query."
    out = _script([os.path.join(GOOGLE_SCRIPTS, "fetch_email.py"),
                   "--query", query, "--max", "5", "--format", "summary"])
    return _clip(out) if out else f"No mail found for {query}."


def gmail_send(args: dict) -> str:
    """Actually send — only ever called AFTER the live session's spoken
    stage+affirm confirmation gate (mirrors kernel_decide)."""
    to, subject = (args.get("to") or "").strip(), (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not (to and subject and body):
        return "I need a recipient, a subject, and a body."
    out = _script([os.path.join(GOOGLE_SCRIPTS, "send_email.py"),
                   "--to", to, "--subject", subject, "--body", body])
    if not out:
        return "The send failed — nothing went out."
    return f"Sent to {to}."


def calendar_add(args: dict) -> str:
    title = (args.get("title") or "").strip()
    date, start = (args.get("date") or "").strip(), (args.get("time") or "").strip()
    if not (title and date and start):
        return "I need a title, a date, and a start time."
    cmd = [os.path.join(GOOGLE_SCRIPTS, "calendar_event.py"),
           "--title", title, "--date", date, "--time", start,
           "--duration", str(int(args.get("duration") or 30))]
    if args.get("attendees"):
        cmd += ["--attendees", str(args["attendees"])]
    if args.get("meet"):
        cmd += ["--meet"]
    out = _script(cmd)
    if not out:
        return "I couldn't create the event."
    return f"On the calendar: {title}, {date} at {start}."


def calendar_list(args: dict) -> str:
    """Read-only agenda — the answer to 'what's on my calendar'. Defaults to today."""
    cmd = [os.path.join(GOOGLE_SCRIPTS, "calendar_list.py")]
    days = args.get("days")
    if days:
        cmd += ["--days", str(int(days))]
    if (args.get("from") or "").strip():
        cmd += ["--from", str(args["from"]).strip()]
    if (args.get("to") or "").strip():
        cmd += ["--to", str(args["to"]).strip()]
    out = _script(cmd)
    return _clip(out) if out else "I couldn't read the calendar."


def drive_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "I need something to search Drive for."
    out = _script([os.path.join(GOOGLE_SCRIPTS, "search_drive.py"), query, "--limit", "5"])
    return _clip(out) if out else f"Nothing in Drive for {query}."


if __name__ == "__main__":
    # Self-check: validation paths (no network), and the Notion payload shape.
    assert "title" in notion_create_task({})
    assert "search Notion" in notion_search({})
    assert "title" in notion_update_task({})
    assert "status or due date" in notion_update_task({"title": "x"})
    assert "recipient" in front_draft({"to": "x@y.z"})
    assert "recipient" in gmail_send({})
    assert "title" in calendar_add({"date": "2026-01-01"})
    assert "Drive" in drive_search({})
    # payload shape: capture what notion_create_task would POST
    sent = {}
    def fake_notion(method, path, payload=None):
        sent.update({"method": method, "path": path, "payload": payload})
        return {"id": "fake-page"}
    globals()["_notion"] = fake_notion
    out = notion_create_task({"title": "Test task"})
    p = sent["payload"]["properties"]
    assert sent["path"] == "/pages" and sent["payload"]["parent"]["database_id"] == NOTION_TASKS_DB
    assert p["Assignee"]["people"][0]["id"] == ROBIN_NOTION_ID, "Assignee default missing"
    assert p["Status"]["status"]["name"] == "Next Up", "Status default missing"
    assert p["Due"]["date"]["start"], "Due default missing"
    assert "I picked that" in out, "must say when the due date was defaulted"

    # notion_list_tasks: query filter shape + speakable formatting
    def fake_query(method, path, payload=None):
        sent.update({"method": method, "path": path, "payload": payload})
        return {"results": [{
            "id": "page-1",
            "properties": {
                "Task name": {"title": [{"plain_text": "Ship the thing"}]},
                "Status": {"status": {"name": "Focus"}},
                "Due": {"date": {"start": "2026-08-01"}}}}]}
    globals()["_notion"] = fake_query
    out = notion_list_tasks({"status": "Focus"})
    assert sent["path"] == f"/databases/{NOTION_TASKS_DB}/query"
    assert sent["payload"]["filter"] == {"property": "Status", "status": {"equals": "Focus"}}
    assert "Ship the thing" in out and "Focus" in out and "2026-08-01" in out

    # notion_update_task: resolve-by-title then PATCH status + due
    def fake_update(method, path, payload=None):
        sent.update({"method": method, "path": path, "payload": payload})
        if method == "POST":
            return {"results": [{"id": "page-2", "properties": {
                "Task name": {"title": [{"plain_text": "Ship the thing"}]}}}]}
        return {"id": "page-2"}
    globals()["_notion"] = fake_update
    out = notion_update_task({"title": "Ship the thing", "status": "Backlog", "due": "2026-09-01"})
    assert sent["method"] == "PATCH" and sent["path"] == "/pages/page-2"
    props = sent["payload"]["properties"]
    assert props["Status"]["status"]["name"] == "Backlog"
    assert props["Due"]["date"]["start"] == "2026-09-01"
    assert "Ship the thing" in out and "Backlog" in out and "2026-09-01" in out

    print("services self-check OK — validation + Robin's Notion task defaults + read/update wired")
