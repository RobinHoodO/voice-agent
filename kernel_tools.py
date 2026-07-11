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


def kernel_persona(timeout: float = 3) -> str:
    try:
        data = _kernel_call("GET", "/persona", timeout=timeout)
    except Exception:
        return ""
    persona = data.get("persona") if isinstance(data, dict) else ""
    return persona.strip() if isinstance(persona, str) else ""
