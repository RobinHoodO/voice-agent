"""Register the macOS implementations of every core capability. Idempotent.

Called once from `mac.agent` at import, before anything opens a session. Each provider
module is imported lazily so a partial environment (no PortAudio, no Accessibility
grant) still gets the capabilities that do work — the same fail-soft posture the
inline `try/except` blocks had before they were hoisted into `core.caps`.
"""
from core import caps

# Which capability profile this surface runs (see core/capabilities.py). Declared at
# module level because `check_tool_drift.py` reads it without importing this file —
# importing it needs PyObjC, and the checker has to work from any checkout.
SURFACE_PROFILE = "mac"

_installed = False


def install(log_sink=None) -> None:
    global _installed
    if log_sink is not None:
        caps.set_log_sink(log_sink)
    caps.set_profile(SURFACE_PROFILE)
    if _installed:
        return

    def _rumps_notify(msg: str) -> None:
        import rumps
        rumps.notification("Thrivbe Voice", "", msg)

    caps.set_notifier(_rumps_notify)

    for mod in ("mac.keychain", "mac.clipboard", "mac.screen", "mac.audio"):
        try:
            __import__(mod, fromlist=["install"]).install()
        except Exception as e:      # noqa: BLE001 — a missing capability degrades, never fatal
            caps.log(f"caps: {mod} unavailable: {e!r}")
    _installed = True
