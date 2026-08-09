"""Screen context capability backed by `mac.macos_context` (AX / Quartz / osascript).

Imports are deferred to call time exactly as `live_session._grab_context` used to do
them, so a machine with the Accessibility grant missing degrades to "(no context)"
instead of failing at startup.
"""
from core import caps


class MacScreenContext(caps.ScreenContext):
    def grab_context(self) -> str:
        from mac.macos_context import grab_context
        return grab_context()

    def grab_window_screenshot(self) -> str:
        from mac.macos_context import grab_window_screenshot
        return grab_window_screenshot()


def install() -> None:
    caps.set_screen(MacScreenContext())
