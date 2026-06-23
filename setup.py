from setuptools import setup


APP = ["agent.py"]

PLIST = {
    "CFBundleName": "Thrivbe Voice",
    "CFBundleDisplayName": "Thrivbe Voice",
    "CFBundleIdentifier": "com.thrivbe.voice-agent",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1.0",
    "LSUIElement": True,
    "NSMicrophoneUsageDescription": "Thrivbe Voice records only while you hold Right-Option or use Start talking.",
    "NSAppleEventsUsageDescription": "Thrivbe Voice reads the frontmost app, window title, Finder selection, and selected text to answer in context.",
    "NSInputMonitoringUsageDescription": "Thrivbe Voice uses Right-Option as a push-to-talk hotkey.",
}

OPTIONS = {
    "argv_emulation": False,
    "plist": PLIST,
    "packages": [
        "openai",
        "pynput",
        "rumps",
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
    ],
    "excludes": [
        "matplotlib",
        "numpy",
        "pytest",
        "tkinter",
    ],
}

setup(
    app=APP,
    name="Thrivbe Voice",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
