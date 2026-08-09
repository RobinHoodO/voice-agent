from setuptools import setup


# NOT "agent.py": modulegraph keys modules by basename, so a boot script named
# agent.py gets aliased over mac/agent.py — py2app then ships neither and the app
# dies at launch with ModuleNotFoundError: mac. Any name that is not a submodule
# name works; this one matches the bundle.
APP = ["thrivbe_voice.py"]

# settings.html is the WebView UI loaded at runtime; ship it into Contents/Resources.
# delegate-harness.md is not decoration: tools._verify_wrap refuses to delegate at all
# if it's missing, so a bundle without it silently loses the delegate feature.
DATA_FILES = ["settings.html", "delegate-harness.md"]

PLIST = {
    "CFBundleName": "Thrivbe Voice",
    "CFBundleDisplayName": "Thrivbe Voice",
    "CFBundleIdentifier": "com.thrivbe.voice-agent",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1.0",
    "LSUIElement": True,
    "NSMicrophoneUsageDescription": "Thrivbe Voice records your voice during a live conversation (started by double-tapping Control or from the menu).",
    "NSAppleEventsUsageDescription": "Thrivbe Voice reads the frontmost app, window title, Finder selection, and selected text to answer in context.",
    "NSInputMonitoringUsageDescription": "Thrivbe Voice listens for a double-tap of the Control key to start a conversation.",
}

OPTIONS = {
    "argv_emulation": False,
    "plist": PLIST,
    "packages": [
        "pynput",
        "rumps",
        "sounddevice",
        "websockets",
        "numpy",
        # ships libportaudio.dylib as package data — sounddevice loads it at
        # runtime; as a "package" py2app copies the dir verbatim (incl. the dylib)
        # instead of zipping just the .py, which would drop the binary.
        "_sounddevice_data",
        # `mac` MUST be here and cannot be discovered by the import scan:
        # modulegraph.find_modules._PLATFORM_MODULES hard-excludes a top-level module
        # named "mac" (a Python-2-era platform module), so `from mac.agent import main`
        # in the boot script resolves to nothing and the built app dies with
        # "No module named mac". Listing it as a package copies the directory verbatim.
        # The cost: py2app does NOT scan a package's imports, so every third-party
        # module `mac/*.py` touches has to be named in `includes` below.
        # tests/test_bundle_deps.py fails the build if that list falls behind.
        "mac",
    ],
    "includes": [
        # PyObjC frameworks. Cocoa + WebKit are here because mac/settings.py and
        # mac/pill.py import them and `mac` is an unscanned package (see above) —
        # without these two the app launches but the settings window and the wave
        # pill fail at import.
        "AppKit",
        "ApplicationServices",
        "Cocoa",
        "Foundation",
        "Quartz",
        "WebKit",
        "objc",
        "pynput._util.darwin",
        "pynput.keyboard._darwin",
        "urllib.request",
        "asyncio",
        "base64",
        "queue",
        # The app: `core` is the surface-independent brain, `mac` is this surface.
        # These are the entries py2app's import scan CANNOT reach on its own, because
        # they are imported lazily inside functions (see mac/agent.py, core/tools.py,
        # core/live_session.py). Naming them here is not belt-and-braces: it is what
        # makes py2app follow THEIR imports too. `mac.*` is deliberately absent —
        # modulegraph excludes the name, so mac ships via `packages` instead.
        "realtime",                    # back-compat shim → core.live_session
        "core.live_session",
        "core.realtime_client",
        "core.audio_core",
        "core.tools",
        "core.services",               # Notion/Front/Google direct-access handlers
        "core.live_prompt",
        "core.kernel_tools",
        "core.shell",
        "core.focus",                  # imported inside _do_tool, invisible to the scan
        "core.web",                    # ditto
        "core.memory",
        "core.settings",
        "core.config",
        "core.caps",
        "core.secrets",
        "core.paths",
        "core.backends.openai_backend",
        "core.backends.gemini_backend",
    ],
    "excludes": [
        "matplotlib",
        "pytest",
        "tkinter",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    name="Thrivbe Voice",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
