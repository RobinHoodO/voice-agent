"""Small stdlib client for the local thrivbe-os kernel voice API."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import config


KERNEL_BASE_URL = "http://127.0.0.1:8790"
UNREACHABLE = "The kernel isn't reachable right now — the SSH tunnel to Thrivbe-1 (launchd, local port 8790) may be down."


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"kernel_tools: {msg}")
    except Exception:
        pass


class KernelUnavailable(Exception):
    pass


def _token() -> str | None:
    try:
        secret = getattr(config, "secret", None)
        if secret:
            value = secret("VOICE_API_TOKEN")
            if value:
                return value
    except Exception as e:
        _log(f"secret lookup failed: {e!r}")
    return os.environ.get("VOICE_API_TOKEN")


def _kernel_call(method: str, path: str, body=None, params=None, timeout: float = 10):
    token = _token()
    if not token:
        _log("VOICE_API_TOKEN missing")
        raise KernelUnavailable("missing token")

    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        KERNEL_BASE_URL + path + query,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError:
        raise
    except (TimeoutError, OSError, urllib.error.URLError) as e:
        _log(f"{method} {path} failed: {e!r}")
        raise KernelUnavailable(str(e))

    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"text": text}


def _error_message(data) -> str:
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("error")
        if detail:
            return str(detail)
    return "request failed"


def _short(text, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def kernel_status() -> str:
    try:
        data = _kernel_call("GET", "/status")
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected status: {e.code}."

    approvals = data.get("approvals", []) if isinstance(data, dict) else []
    attention = data.get("attention", []) if isinstance(data, dict) else []
    runs = data.get("runs", []) if isinstance(data, dict) else []

    lines = []
    if approvals:
        lines.append(f"{len(approvals)} pending approval{'s' if len(approvals) != 1 else ''}:")
        for a in approvals[:5]:
            lines.append(f"[{a.get('id')}] {a.get('actionType', 'action')}: {_short(a.get('preview'))}")
    if attention:
        lines.append(f"{len(attention)} attention item{'s' if len(attention) != 1 else ''}:")
        for item in attention[:5]:
            detail = f": {_short(item.get('detail'), 80)}" if item.get("detail") else ""
            lines.append(f"{item.get('title', '(untitled)')}{detail}")
    if runs:
        recent = ", ".join(f"{r.get('process_name', 'run')} {r.get('status', '')}".strip()
                           for r in runs[:3])
        lines.append(f"Recent runs: {recent}.")
    return "\n".join(lines) if lines else "Nothing needs your attention right now."


def kernel_attention_brief(timeout: float = 2.5) -> str:
    """One compact, best-effort status line for the live-session prompt."""
    try:
        data = _kernel_call("GET", "/status", timeout=timeout)
    except (KernelUnavailable, urllib.error.HTTPError):
        return ""
    approvals = data.get("approvals", []) if isinstance(data, dict) else []
    attention = data.get("attention", []) if isinstance(data, dict) else []
    if not approvals and not attention:
        return ""
    parts = []
    if approvals:
        parts.append(f"{len(approvals)} approval{'s' if len(approvals) != 1 else ''} pending")
    if attention:
        parts.append(f"{len(attention)} attention item{'s' if len(attention) != 1 else ''}")
    return "KERNEL ATTENTION: " + ", ".join(parts) + " — mention this to the user at the first natural opening, briefly."


def kernel_decide(args: dict) -> str:
    approval_id = args.get("approvalId", args.get("approval_id"))
    decision = args.get("decision")
    mapped = {"approve": "approved", "reject": "rejected",
              "approved": "approved", "rejected": "rejected"}.get(decision)
    try:
        approval_id = int(approval_id)
    except Exception:
        return "I need the approval id before I can decide it."
    if mapped is None:
        return "Say approve or reject for that approval."
    try:
        data = _kernel_call("POST", "/decide", {"approvalId": approval_id, "decision": mapped})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected that decision: {e.code}."
    if data.get("changed"):
        return f"Done - approval {approval_id} marked {mapped}."
    return "That approval was already decided, or it doesn't exist."


def kernel_memo(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "I need some text to file as a memo."
    try:
        data = _kernel_call("POST", "/memo", {"text": text})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected that memo: {e.code}."
    return f"Noted - filed to your inbox ({data.get('id', 'saved')})."


def kernel_remember(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "I need something to remember."
    try:
        _kernel_call("POST", "/remember", {"text": text})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected that memory: {e.code}."
    return "Remembered."


def kernel_recall(args: dict) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return "What should I recall?"
    try:
        data = _kernel_call("GET", "/recall", params={"q": q})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Recall failed: {e.code}."
    hits = data.get("hits", [])
    if not hits:
        return f"Nothing recalled for {q}."
    return "\n".join(_short(h.get("text"), 200) for h in hits[:4])


def os_delegate(args: dict) -> str:
    instruction = (args.get("instruction") or "").strip()
    if not instruction:
        return "I need an instruction to hand to the OS worker."
    try:
        _kernel_call("POST", "/delegate", {"instruction": instruction, "source": "voice-agent"})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected that delegation: {e.code}."
    return "Handed to the OS worker — Robin will get an approval or a summary on Telegram."


def session_log(source: str, cost_nok: float, duration_sec: float | None, summary: str) -> str:
    try:
        _kernel_call("POST", "/session-log", {
            "source": source,
            "costNok": cost_nok,
            "durationSec": duration_sec,
            "summary": summary,
        })
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"The kernel rejected that session log: {e.code}."
    return "Session cost logged."


def bloom_create_task(args: dict) -> str:
    payload = {"op": "create", "projectId": args.get("projectId"), "title": args.get("title")}
    if args.get("description"):
        payload["description"] = args.get("description")
    return _bloom_write(payload)


def bloom_update_task(args: dict) -> str:
    payload = {"op": "update", "id": args.get("id")}
    if args.get("status"):
        payload["status"] = args.get("status")
    if args.get("title"):
        payload["title"] = args.get("title")
    return _bloom_write(payload)


def bloom_comment_task(args: dict) -> str:
    return _bloom_write({"op": "comment", "id": args.get("id"), "body": args.get("body")})


def _bloom_write(payload: dict) -> str:
    try:
        data = _kernel_call("POST", "/bloom-write", payload)
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            return f"Bloom write was rejected: {_error_message(detail)}."
        except Exception:
            return f"Bloom write was rejected: {e.code}."
    return data.get("message") or f"Filed approval #{data.get('approvalId')} - Robin must approve before this executes."


def twenty_search_contacts(args: dict) -> str:
    q = (args.get("name_query") or "").strip()
    if not q:
        return "I need a name to search for."
    try:
        data = _kernel_call("GET", "/twenty-search", params={"q": q})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"CRM search failed: {e.code}."
    results = data.get("results", [])
    if not results:
        return f"No contacts found in CRM matching {q}."
    lines = [f"I found {len(results)} contact{'s' if len(results) != 1 else ''} matching {q}:"]
    for p in results[:5]:
        name = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip() or "Unnamed"
        job = p.get("jobTitle") or "Contact"
        email = p.get("email") or "no email"
        lines.append(f"{name} - {job}, {email}")
    return "\n".join(lines)


def semsearch_query(args: dict) -> str:
    q = (args.get("query") or "").strip()
    corpus = args.get("corpus") or "people"
    if not q:
        return "I need a query to search."
    try:
        data = _kernel_call("POST", "/semsearch", {"query": q, "corpus": corpus})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Semantic search failed: {e.code}."
    results = data.get("results", [])
    if not results:
        return f"No semantic search results in {corpus}."
    lines = [f"I found {len(results)} result{'s' if len(results) != 1 else ''} in {corpus}:"]
    for res in results[:5]:
        name = res.get("name") or res.get("title") or "Match"
        headline = res.get("headline") or res.get("snippet") or ""
        if not headline and res.get("path"):
            headline = f"{res.get('path')} chunk {res.get('chunk_index', 0)}"
        company = f" at {res.get('company')}" if res.get("company") else ""
        lines.append(f"{name}{company}: {_short(headline)}")
    return "\n".join(lines)


def _items(data, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def hybrid_rag_search(args: dict) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return "I need a query to search."
    try:
        data = _kernel_call("POST", "/ai-search", {"query": q})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Hybrid search failed: {e.code}."
    if not isinstance(data, dict):
        return "Hybrid search returned no usable result."
    answer = _short(data.get("answer"), 360)
    entities = _items(data, "entities", "resolvedEntities")
    lines = [answer] if answer else []
    if entities:
        names = []
        for entity in entities[:3]:
            if isinstance(entity, dict):
                name = entity.get("title") or entity.get("name") or entity.get("id")
                kind = entity.get("type")
                if name:
                    names.append(f"{_short(name, 80)}{f' ({kind})' if kind else ''}")
        if names:
            lines.append("Related: " + ", ".join(names) + ".")
    return "\n".join(lines) or f"No hybrid search result for {q}."


def graph_get_node(args: dict) -> str:
    node_id = (args.get("id") or "").strip()
    if not node_id:
        return "I need a graph node id."
    params = {"id": node_id}
    if args.get("depth") is not None:
        params["depth"] = args["depth"]
    try:
        data = _kernel_call("GET", "/graph-node", params=params)
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Graph lookup failed: {e.code}."
    if not isinstance(data, dict):
        return f"No graph details found for {node_id}."
    nodes = _items(data, "nodes")
    root = next((node for node in nodes if isinstance(node, dict) and node.get("id") == node_id), None)
    root = root or (nodes[0] if nodes and isinstance(nodes[0], dict) else {})
    title = root.get("name") or root.get("title") or node_id
    kind = root.get("type") or root.get("kind")
    related = [node for node in nodes if isinstance(node, dict) and node.get("id") != node_id]
    lines = [f"{_short(title, 100)}{f' ({kind})' if kind else ''}: {len(related)} related node{'s' if len(related) != 1 else ''}."]
    names = [_short(node.get("name") or node.get("title") or node.get("id"), 80)
             for node in related[:4] if node.get("name") or node.get("title") or node.get("id")]
    if names:
        lines.append("Top related: " + ", ".join(names) + ".")
    return "\n".join(lines)


def graph_get_document(args: dict) -> str:
    document_id = (args.get("id") or "").strip()
    if not document_id:
        return "I need a graph document id."
    try:
        data = _kernel_call("GET", "/graph-doc", params={"id": document_id})
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Graph document lookup failed: {e.code}."
    if not isinstance(data, dict):
        return f"No readable graph document found for {document_id}."
    title = data.get("title") or data.get("name") or data.get("path") or document_id
    content = data.get("content") or data.get("text")
    if not content:
        return f"{_short(title, 120)} has no readable content."
    return f"{_short(title, 120)}: {_short(content, 500)}"


def bloom_list_projects(args: dict) -> str:
    try:
        data = _kernel_call("GET", "/bloom-projects")
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Bloom project lookup failed: {e.code}."
    projects = _items(data, "projects", "items", "results")
    if not projects:
        return "No Bloom projects found."
    lines = [f"I found {len(projects)} Bloom project{'s' if len(projects) != 1 else ''}:"]
    for project in projects[:5]:
        if isinstance(project, dict):
            name = project.get("name") or project.get("title") or "Untitled project"
            project_id = project.get("id")
            lines.append(f"[{project_id}] {_short(name, 100)}" if project_id is not None else _short(name, 100))
    return "\n".join(lines)


def bloom_list_tasks(args: dict) -> str:
    params = {key: args[key] for key in ("projectId", "status", "q") if args.get(key) is not None}
    try:
        data = _kernel_call("GET", "/bloom-tasks", params=params)
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Bloom task lookup failed: {e.code}."
    tasks = _items(data, "tasks", "items", "results")
    if not tasks:
        return "No Bloom tasks found."
    lines = [f"I found {len(tasks)} Bloom task{'s' if len(tasks) != 1 else ''}:"]
    for task in tasks[:5]:
        if isinstance(task, dict):
            title = task.get("title") or task.get("name") or "Untitled task"
            task_id = task.get("id")
            status = task.get("status")
            prefix = f"[{task_id}] " if task_id is not None else ""
            lines.append(prefix + _short(title, 100) + (f" ({status})" if status else ""))
    return "\n".join(lines)


def list_inbox_items(args: dict) -> str:
    params = {key: args[key] for key in ("triage_status", "search", "limit") if args.get(key) is not None}
    try:
        data = _kernel_call("GET", "/inbox", params=params)
    except KernelUnavailable:
        return UNREACHABLE
    except urllib.error.HTTPError as e:
        return f"Inbox lookup failed: {e.code}."
    items = _items(data, "items", "results")
    if not items:
        return "No inbox items found."
    lines = [f"I found {len(items)} inbox item{'s' if len(items) != 1 else ''}:"]
    for item in items[:5]:
        if isinstance(item, dict):
            subject = item.get("subject") or item.get("snippet") or "Untitled item"
            sender = item.get("sender")
            detail = f" from {_short(sender, 60)}" if sender else ""
            lines.append(f"{_short(subject, 120)}{detail}")
    return "\n".join(lines)


def kernel_persona(timeout: float = 3) -> str:
    try:
        data = _kernel_call("GET", "/persona", timeout=timeout)
    except Exception:
        return ""
    persona = data.get("persona") if isinstance(data, dict) else ""
    return persona.strip() if isinstance(persona, str) else ""
