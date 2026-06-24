"""Persistent zsh for the agentic shell (run_shell tool).

Extracted from realtime.py: one long-lived non-interactive zsh per live session,
sentinel-delimited output capture, process-group kill on timeout. Spawned with a
secret-stripped env (config.subprocess_env) so model-run commands can't read API keys.
"""
import os
import re
import select
import signal
import subprocess
import time
import uuid

import config

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")   # strip terminal escape codes from shell output


class Shell:
    """One persistent zsh for a live session. cd/env/venvs persist across .run() calls;
    starts in the home folder so the agent is system-wide. Non-interactive (so no prompt
    junk pollutes captured output) but sources ~/.zshrc, so Robin's PATH and shell
    functions (incl. the `claude` wrapper) are available.

    ponytail: sentinel-delimited capture on one shell. A command that never returns
    (opens a REPL) desyncs it — rare; restart live mode to reset. Long jobs: background
    them (`cmd &`) and poll — the prompt tells the model to.
    """

    def __init__(self) -> None:
        self._spawn()

    def _spawn(self) -> None:
        # Start in the configured base folder (live.workspace) so the agent's
        # project skills and files are in reach; fall back to home if unset/missing.
        ws = config.get("live.workspace")
        start = os.path.expanduser(ws) if ws else os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        # start_new_session=True puts the shell + its children in their own process
        # group, so a runaway command can be killed wholesale on timeout (_respawn).
        self.p = subprocess.Popen(
            ["/bin/zsh"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=start, start_new_session=True,
            env=config.subprocess_env())   # strip API keys: model-run commands must not read them
        self.p.stdin.write("source ~/.zshrc 2>/dev/null\n")
        self.p.stdin.flush()
        self._drain(0.6)

    def _respawn(self) -> None:
        """A timed-out command leaves the shell blocked (zsh runs input serially), so
        the next command would queue behind it and desync. Nuke the whole process group
        (kills the runaway child too) and start fresh. Cost: cwd/env reset to home."""
        try:
            os.killpg(os.getpgid(self.p.pid), signal.SIGKILL)
        except Exception:
            pass
        self._spawn()

    def _drain(self, timeout: float) -> None:
        """Discard buffered startup banner / rc noise."""
        while True:
            r, _, _ = select.select([self.p.stdout], [], [], timeout)
            if not r or self.p.stdout.readline() == "":
                break

    def run(self, cmd: str, timeout: float = 20) -> str:
        if not cmd:
            return "(no command)"
        if self.p.poll() is not None:
            self._respawn()
        mark = f"__VA_{uuid.uuid4().hex}__"
        try:
            self.p.stdin.write(f'{cmd}\nprint -r -- "{mark}$?"\n')
            self.p.stdin.flush()
        except Exception as e:
            return f"error: {e}"
        lines, deadline = [], time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._respawn()   # kill the wedged command, reset to a clean shell
                partial = "".join(lines)[:4000]
                return (partial + f"\n(timed out after {int(timeout)}s and was killed. "
                        "Use a faster command — e.g. `mdfind` for files, not `find ~`.)")
            r, _, _ = select.select([self.p.stdout], [], [], remaining)
            if not r:
                continue
            line = self.p.stdout.readline()
            if line == "":
                return "".join(lines)[:6000] or "(shell closed)"
            clean = _ANSI.sub("", line)
            if mark in clean:
                code = clean.split(mark, 1)[1].strip()
                out = "".join(lines)
                if code not in ("0", ""):
                    out += f"\n(exit {code})"
                return out[:6000] or "(no output)"
            lines.append(clean)

    def close(self) -> None:
        # End the interactive shell only — terminate the zsh, NOT its process group.
        # Jobs the agent launched detached (`nohup ... & disown`) are reparented to
        # launchd and keep running after the conversation ends, which is the point:
        # the user can start a long task, hang up, and it finishes in the background.
        try:
            self.p.terminate()
        except Exception:
            pass
