"""macOS clipboard + auto-paste — `pbcopy` and a synthetic Cmd-V via osascript.

Lifted verbatim out of `tools._put_text`. This and `mac.settings` are the only places
in the tree that shell out to `osascript`.
"""
import subprocess

from core import caps


class MacClipboard(caps.Clipboard):
    def put_text(self, text: str, paste: bool = True) -> str:
        """Copy text to the clipboard and (by default) paste it into the frontmost window.
        Copy always works; auto-paste needs Accessibility — if it can't, the text is still
        on the clipboard and we tell the user to press Cmd-V."""
        try:
            # ponytail: encode bytes ourselves — text=True uses the locale encoding, which is
            # ASCII in the py2app bundle, so em-dashes/emoji crashed pbcopy with a UnicodeError.
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        except Exception as e:
            return f"couldn't reach the clipboard: {e}"
        if not paste:
            return "copied to the clipboard"
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            capture_output=True, text=True)
        if r.returncode != 0:
            return "copied to the clipboard — press Cmd-V to paste it in (auto-paste needs Accessibility permission)"
        return "pasted it into the front window"


def install() -> None:
    caps.set_clipboard(MacClipboard())
