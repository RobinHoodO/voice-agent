"""The macOS surface: menubar app, hotkey, pill, PortAudio, AX/Quartz screen reads.

Everything in here is allowed to import AppKit/Quartz/PyObjC/sounddevice/rumps/pynput
and to shell out to `osascript` and the Keychain `security` CLI. `core` is not.

`mac.caps_install.install()` is what joins the two: it registers this surface's
implementations into `core.caps` / `core.secrets` before any session starts.
"""
