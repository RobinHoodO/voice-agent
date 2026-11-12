#!/usr/bin/env python3
"""PROTOTYPE, throw away. AskUserQuestion hook for the inbox thin slice (issue 10).

PreToolUse  -> raise the question (the box still opens at the desk; we print nothing).
PostToolUse -> tell the inbox the box closed.
Fails open: any error is swallowed so the box always shows.
"""
import json, os, sys, urllib.request

URL = os.environ.get("INBOX_URL", "http://100.96.126.87:8799")


def post(path, body):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2)


try:
    d = json.load(sys.stdin)
    if d.get("hook_event_name") == "PreToolUse":
        post("/raise", {
            "session_id": d.get("session_id"),
            "pane": os.environ.get("HERDR_PANE_ID"),
            "cwd": d.get("cwd"),
            "questions": d.get("tool_input", {}).get("questions", []),
        })
    else:
        post("/resolved", {"session_id": d.get("session_id"), "answers": d.get("tool_response")})
except Exception:  # noqa: BLE001 fail open, the desk box must always show
    pass
