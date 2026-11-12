#!/usr/bin/env python3
"""PROTOTYPE, throw away. PAM Inbox thin slice (voice-agent issue 10).

One file: in-memory store + phone page + answer typer (poll loop).
Stands in for the Thrivbe-1 kernel `decisions` table and the Mac launchd poller.
shortcut: no auth, tailnet-only bind, memory only; never ship this.
Run: python3 server.py   (POLL=5 seconds, KEYS=digit|arrows)
"""
import json, os, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
HERDR = os.environ.get("HERDR_BIN", str(Path.home() / ".local/bin/herdr"))
BIND = os.environ.get("BIND", "100.96.126.87")
PORT = int(os.environ.get("PORT", "8799"))
POLL = float(os.environ.get("POLL", "5"))
KEYS = os.environ.get("KEYS", "digit")

ROWS, LOCK = [], threading.Lock()


def herdr(*args):
    return subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=10).stdout


def pane_info(pane):
    p = json.loads(herdr("pane", "get", pane))["result"]["pane"]
    return p.get("agent_status"), (p.get("agent_session") or {}).get("value")


def deliver(row):
    """The guard from issue 20: session matches, pane is blocked, question is on screen."""
    pane, q = row["pane"], row["questions"][0]
    if len(row["questions"]) != 1 or q.get("multiSelect"):
        return "prototype types single pick-one questions only"
    status, session = pane_info(pane)
    if session != row["session_id"]:
        return f"pane now holds another session ({session})"
    if status != "blocked":
        return f"pane is {status}, not blocked"
    screen = herdr("pane", "read", pane, "--source", "visible", "--lines", "200")
    # The box wraps long lines, so compare with all whitespace removed.
    if "".join(q["question"].split())[:60] not in "".join(screen.split()):
        return "question text is not on screen"
    a = row["answer"]
    n_opts = len(q["options"])
    if "option" in a:
        i = a["option"]
        keys = [str(i + 1)] if KEYS == "digit" else ["Down"] * i + ["Enter"]
        herdr("pane", "send-keys", pane, *keys)
    else:  # free text: the box adds "Type something" after the listed options
        keys = [str(n_opts + 1)] if KEYS == "digit" else ["Down"] * n_opts
        herdr("pane", "send-keys", pane, *keys)
        time.sleep(0.4)
        herdr("pane", "send-text", pane, a["text"])
        time.sleep(0.4)
        herdr("pane", "send-keys", pane, "Enter")
    return None


def poll_loop():
    while True:
        time.sleep(POLL)
        with LOCK:
            todo = [r for r in ROWS if r["status"] == "answered"]
            for r in ROWS:  # typed, but the PostToolUse hook never confirmed it
                if r["status"] == "typed" and time.time() - r["typed_ts"] > 15:
                    r["status"], r["note"] = "not_delivered", "typed, but the agent did not confirm in 15 s"
        for r in todo:
            try:
                err = deliver(r)
            except Exception as e:  # noqa: BLE001 prototype: show it on the card
                err = f"error: {e}"
            with LOCK:
                if r["status"] != "answered":
                    continue  # desk answer won the race
                if err:
                    r["status"], r["note"] = "not_delivered", err
                else:
                    r["status"], r["typed_ts"] = "typed", time.time()
                    r["note"] = f"typed {r['typed_ts'] - r['answered_ts']:.1f} s after the tap"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/rows":
            with LOCK:
                self.send({"poll": POLL, "keys": KEYS, "now": time.time(), "rows": ROWS})
        else:
            self.send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        now = time.time()
        with LOCK:
            if self.path == "/raise":
                ROWS.append({**d, "id": len(ROWS) + 1, "raised_ts": now, "status": "open", "note": ""})
            elif self.path == "/resolved":  # PostToolUse: the box closed
                for r in ROWS:
                    if r["session_id"] == d["session_id"] and r["status"] in ("open", "answered", "typed", "not_delivered"):
                        was = r["status"]
                        r["status"] = "delivered" if was == "typed" else "answered_at_desk"
                        r["closed_ts"], r["agent_got"] = now, d.get("answers")
                        if was == "typed":
                            r["note"] += f"; agent confirmed {now - r['answered_ts']:.1f} s after the tap"
            elif self.path in ("/answer", "/retry"):
                for r in ROWS:
                    if r["id"] == d["id"] and r["status"] in ("open", "not_delivered"):
                        if self.path == "/answer":
                            r["answer"] = {k: d[k] for k in ("option", "text") if k in d}
                        r["status"], r["answered_ts"], r["note"] = "answered", now, "waits for the typer"
        self.send({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"PROTOTYPE inbox on http://{BIND}:{PORT}  poll={POLL}s keys={KEYS}", flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
