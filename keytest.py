#!/usr/bin/env python3
"""Diagnostic: log every key press so we can see what the trigger key reports
and whether Accessibility lets us see events at all. Press keys, then Ctrl-C."""
import sys
from pynput import keyboard

log = open("/Users/robinsverd/Thrivbe-AI/projects/voice-agent/keytest.log", "w")
def out(s):
    print(s); log.write(s + "\n"); log.flush(); sys.stdout.flush()

out("Key logger ready. Press RIGHT-OPTION (and any other keys). Ctrl-C to stop.")
out("If you press keys and see NOTHING here, Accessibility is not actually granted.")

def on_press(key):
    out(f"PRESS  {key!r}")
def on_release(key):
    out(f"RELEASE {key!r}")

with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
    l.join()
