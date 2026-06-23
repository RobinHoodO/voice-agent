#!/usr/bin/env python3
"""Floating always-on-top state pill for live conversation mode.

A small borderless NSPanel near the top-center of the screen showing the live
state (listening / thinking / speaking). AppKit is not thread-safe — every
method here MUST be called on the main (rumps) thread. agent.py drives it from
its 0.3s Timer tick, never from the realtime/pynput threads.
"""
from AppKit import (
    NSPanel, NSColor, NSTextField, NSFont, NSScreen, NSEvent, NSMakeRect,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSStatusWindowLevel, NSBackingStoreBuffered, NSTextAlignmentCenter,
)

# state -> (dot color, label) — minimal: a colored dot + one word
STATES = {
    "listening": ((0.20, 0.80, 0.40), "Listening"),
    "thinking": ((1.00, 0.62, 0.04), "Thinking"),
    "speaking": ((0.04, 0.52, 1.00), "Speaking"),
}

W, H = 150, 40


def _active_screen():
    """The screen the mouse cursor is currently on (so the pill lands where
    Robin is looking), falling back to the main screen."""
    m = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= m.x <= f.origin.x + f.size.width and
                f.origin.y <= m.y <= f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()


class Pill:
    def __init__(self):
        self.panel = None
        self.label = None
        self.dot = None

    def _build(self) -> None:
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)  # pure indicator — clicks pass through

        content = panel.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setCornerRadius_(H / 2.0)      # full pill
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.85).CGColor())

        dot = NSTextField.alloc().initWithFrame_(NSMakeRect(20, (H - 16) / 2, 16, 16))
        for f in (dot.setBezeled_, dot.setDrawsBackground_, dot.setEditable_, dot.setSelectable_):
            f(False)
        dot.setStringValue_("●")
        dot.setFont_(NSFont.systemFontOfSize_(14))

        label = NSTextField.alloc().initWithFrame_(NSMakeRect(40, (H - 20) / 2, W - 50, 20))
        for f in (label.setBezeled_, label.setDrawsBackground_, label.setEditable_, label.setSelectable_):
            f(False)
        label.setAlignment_(NSTextAlignmentCenter)
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.systemFontOfSize_(15))

        content.addSubview_(dot)
        content.addSubview_(label)
        self.panel, self.label, self.dot = panel, label, dot

    def show(self, state: str = "listening") -> None:
        if self.panel is None:
            self._build()
        f = _active_screen().frame()
        # bottom-center of the active screen, a little above the Dock
        self.panel.setFrameOrigin_((f.origin.x + (f.size.width - W) / 2,
                                    f.origin.y + 80))
        self.set_state(state)
        self.panel.orderFrontRegardless()

    def hide(self) -> None:
        if self.panel is not None:
            self.panel.orderOut_(None)

    def set_state(self, state: str) -> None:
        color, text = STATES.get(state, ((0.7, 0.7, 0.7), "Live"))
        if self.label is not None:
            self.label.setStringValue_(text)
        if self.dot is not None:
            r, g, b = color
            self.dot.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0))
