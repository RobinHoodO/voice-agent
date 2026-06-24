#!/usr/bin/env python3
"""Floating state ORB for live conversation mode — a bright "Liquid Glass" presence.

A small frosted-glass circle at the bottom-center of the active screen. No text: state
is carried entirely by color + motion (mint = listening, periwinkle = thinking, amber =
speaking), each with a slow breathing glow. Design spec by the Design Director agent.

AppKit is not thread-safe — every method here MUST be called on the main (rumps)
thread. agent.py drives it from its 0.3s Timer tick, never from worker threads.
"""
from AppKit import (
    NSPanel, NSColor, NSView, NSScreen, NSEvent, NSMakeRect, NSAppearance,
    NSVisualEffectView, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSStatusWindowLevel, NSBackingStoreBuffered,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSVisualEffectMaterialHUDWindow, NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
)
from Quartz import CALayer, CAGradientLayer, CABasicAnimation, CAMediaTimingFunction

# state -> (accent rgb, core-center rgb) — mid-saturation, high-value so they glow
# inside white glass without going neon. cool->warm = listening->thinking->speaking.
STATES = {
    "listening": ((0.239, 0.824, 0.753), (0.498, 0.941, 0.886)),   # mint  #3DD2C0
    "thinking":  ((0.486, 0.549, 1.000), (0.651, 0.698, 1.000)),   # periwinkle #7C8CFF
    "speaking":  ((1.000, 0.698, 0.239), (1.000, 0.816, 0.541)),   # amber #FFB23D
}
# per-state breathing: (core_scale_to, glow_radius_lo, glow_radius_hi, glow_op_lo, glow_op_hi, dur)
MOTION = {
    "listening": (1.08, 12.0, 18.0, 0.35, 0.55, 1.3),
    "thinking":  (1.04, 13.0, 15.0, 0.45, 0.58, 0.7),
    "speaking":  (1.05, 14.0, 20.0, 0.45, 0.65, 1.0),
}

PANEL = 84          # clear square panel — extra room for the glow/shadow bloom
DISC = 52           # frosted glass disc
CORE = 18           # glowing accent marble
INSET = (PANEL - DISC) / 2.0
CORE_OFF = (DISC - CORE) / 2.0


def _cg(rgb, a=1.0):
    r, g, b = rgb
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a).CGColor()


def _active_screen():
    m = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= m.x <= f.origin.x + f.size.width and
                f.origin.y <= m.y <= f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()


def _reduce_motion():
    try:
        from AppKit import NSWorkspace
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


class Pill:
    def __init__(self):
        self.panel = None
        self.glow = None
        self.core = None

    def _build(self) -> None:
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL, PANEL), style, NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)

        content = panel.contentView()
        content.setWantsLayer_(True)
        host = content.layer()

        disc = NSMakeRect(INSET, INSET, DISC, DISC)

        # 1. ambient lift: white disc behind everything, cool soft shadow (body hidden by glass)
        base = CALayer.layer()
        base.setFrame_(disc)
        base.setCornerRadius_(DISC / 2.0)
        base.setBackgroundColor_(_cg((1, 1, 1), 1.0))
        base.setShadowColor_(_cg((0.078, 0.094, 0.157), 1.0))
        base.setShadowOpacity_(0.18)
        base.setShadowRadius_(16.0)
        base.setShadowOffset_((0, -6))
        base.setMasksToBounds_(False)
        host.insertSublayer_atIndex_(base, 0)

        # 2. state glow: accent bloom (its shadow is the halo; body hidden by glass)
        glow = CALayer.layer()
        glow.setFrame_(disc)
        glow.setCornerRadius_(DISC / 2.0)
        glow.setShadowOffset_((0, 0))
        glow.setMasksToBounds_(False)
        host.insertSublayer_atIndex_(glow, 1)
        self.glow = glow

        # 3. frosted glass — forced bright (vibrantLight stays luminous in Dark Mode)
        fx = NSVisualEffectView.alloc().initWithFrame_(disc)
        fx.setMaterial_(NSVisualEffectMaterialHUDWindow)
        fx.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        fx.setState_(NSVisualEffectStateActive)
        try:
            fx.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameVibrantLight"))
        except Exception:
            pass
        fx.setWantsLayer_(True)
        fxl = fx.layer()
        fxl.setCornerRadius_(DISC / 2.0)
        fxl.setMasksToBounds_(True)
        content.addSubview_(fx)

        # 4. luminance tint — pushes the glass brighter/cooler than raw vibrancy
        tint = CALayer.layer()
        tint.setFrame_(NSMakeRect(0, 0, DISC, DISC))
        tint.setCornerRadius_(DISC / 2.0)
        tint.setBackgroundColor_(_cg((1, 1, 1), 0.18))
        fxl.addSublayer_(tint)

        # 5. hairline glass rim (lit meniscus)
        rim = CALayer.layer()
        rim.setFrame_(NSMakeRect(0.5, 0.5, DISC - 1, DISC - 1))
        rim.setCornerRadius_((DISC - 1) / 2.0)
        rim.setBorderWidth_(1.0)
        rim.setBorderColor_(_cg((1, 1, 1), 0.6))
        fxl.addSublayer_(rim)

        # 6. glowing accent core — a lit radial-gradient marble
        core = CAGradientLayer.layer()
        core.setFrame_(NSMakeRect(CORE_OFF, CORE_OFF, CORE, CORE))
        core.setCornerRadius_(CORE / 2.0)
        core.setType_("radial")
        core.setStartPoint_((0.5, 0.5))
        core.setEndPoint_((1.0, 1.0))
        core.setLocations_([0.0, 1.0])
        fxl.addSublayer_(core)
        self.core = core

        self.panel = panel

    @staticmethod
    def _anim(keypath, frm, to, dur):
        a = CABasicAnimation.animationWithKeyPath_(keypath)
        a.setFromValue_(frm)
        a.setToValue_(to)
        a.setDuration_(dur)
        a.setAutoreverses_(True)
        a.setRepeatCount_(1e9)
        a.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
        a.setRemovedOnCompletion_(False)
        return a

    def set_state(self, state: str) -> None:
        if self.core is None:
            return
        accent, center = STATES.get(state, ((0.7, 0.7, 0.7), (0.85, 0.85, 0.85)))
        scale, rlo, rhi, olo, ohi, dur = MOTION.get(state, (1.05, 13, 16, 0.4, 0.55, 1.2))
        # colors
        self.core.setColors_([_cg(center, 1.0), _cg(accent, 1.0)])
        self.glow.setShadowColor_(_cg(accent, 1.0))
        self.glow.setShadowRadius_((rlo + rhi) / 2.0)
        self.glow.setShadowOpacity_((olo + ohi) / 2.0)
        # motion (or static if reduce-motion)
        self.core.removeAllAnimations()
        self.glow.removeAllAnimations()
        if _reduce_motion():
            return
        self.core.addAnimation_forKey_(self._anim("transform.scale", 1.0, scale, dur), "breathe")
        self.glow.addAnimation_forKey_(self._anim("shadowRadius", rlo, rhi, dur), "halo")
        self.glow.addAnimation_forKey_(self._anim("shadowOpacity", olo, ohi, dur), "pulse")

    def show(self, state: str = "listening") -> None:
        if self.panel is None:
            self._build()
        vf = _active_screen().visibleFrame()
        # bottom-center, 28pt above the Dock/safe area
        self.panel.setFrameOrigin_((vf.origin.x + (vf.size.width - PANEL) / 2.0,
                                    vf.origin.y + 28))
        self.set_state(state)
        self.panel.orderFrontRegardless()

    def hide(self) -> None:
        if self.panel is not None:
            self.panel.orderOut_(None)
