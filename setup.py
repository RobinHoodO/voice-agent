from setuptools import setup


APP = ["agent.py"]

# settings.html is the WebView UI loaded at runtime; ship it into Contents/Resources.
DATA_FILES = ["settings.html"]

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
    ],
    "includes": [
        "AppKit",
        "ApplicationServices",
        "Foundation",
        "Quartz",
        "objc",
        "pynput._util.darwin",
        "pynput.keyboard._darwin",
        "urllib.request",
        # live conversation mode — lazily imported, so name them explicitly
        "realtime",          # back-compat shim → live_session
        "live_session",
        "realtime_client",
        "audio",
        "tools",
        "live_prompt",
        "shell",
        "macos_context",     # lazily imported by live_session — name it so py2app bundles it
        "memory",
        "pill",
        "config",
        "asyncio",
        "base64",
        "queue",
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
