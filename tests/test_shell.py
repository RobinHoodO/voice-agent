"""Characterization test for the shell.py extraction (Phase 4).

Drives the real persistent zsh to pin run()/exit-code behavior after the move.
"""
from shell import Shell


def test_run_captures_output():
    sh = Shell()
    try:
        out = sh.run("echo hello-from-shell")
        assert "hello-from-shell" in out
    finally:
        sh.close()


def test_run_reports_nonzero_exit():
    sh = Shell()
    try:
        out = sh.run("false")
        assert "exit 1" in out
    finally:
        sh.close()
