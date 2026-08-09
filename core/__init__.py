"""Surface-independent brain.

Nothing in this package may import AppKit, Quartz, PyObjC, sounddevice, rumps,
pynput, or shell out to `osascript` / the Keychain `security` CLI. Everything a
surface (mac menubar app, headless server) has to provide is declared as a
capability in `core.caps` and registered by that surface at startup.

`tests/test_headless_core.py` enforces this: it blocks the macOS-only modules at
import time and imports every module in here.
"""
