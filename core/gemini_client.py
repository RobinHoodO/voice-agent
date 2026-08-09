"""Gemini Live API protocol layer: connection constants and an audio self-test
round-trip. Sibling of realtime_client.py — NOT wired into the live session yet.

Verified against the real API 2026-08-02. Protocol notes: unlike OpenAI, Gemini
requires waiting for setupComplete before sending anything else, the key rides in
the URL query string rather than a header, and gemini-3.1-flash-live-preview only
supports AUDIO responseModalities (TEXT is rejected with a 1007 close).
"""
import asyncio
import json

from core import caps, config

MODEL = "models/gemini-3.1-flash-live-preview"
HOST = ("wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


def _log(msg: str) -> None:
    caps.log(msg)


def _url() -> str:
    # No "gemini" env fallback is registered in config yet — secret() returns None
    # until a key is provisioned (Keychain) in the later config/UI step.
    return f"{HOST}?key={config.secret('gemini') or ''}"


async def _selftest() -> int:
    """Audio round-trip — proves auth + model + protocol; returns bytes of audio received."""
    import websockets
    async with websockets.connect(_url(), max_size=None) as ws:
        await ws.send(json.dumps({"setup": {
            "model": MODEL,
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "systemInstruction": {"parts": [{"text": "Reply in one short sentence."}]},
        }}))
        # Gemini demands the setup handshake completes before any client content.
        async for raw in ws:
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(msg["error"])
            if "setupComplete" in msg:
                break
        await ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": "Say hello to Robin."}]}],
            "turnComplete": True,
        }}))
        audio_bytes = 0
        async for raw in ws:
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(msg["error"])
            sc = msg.get("serverContent") or {}
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                inline = part.get("inlineData") or {}
                if inline.get("data"):
                    audio_bytes += len(inline["data"])
            if sc.get("turnComplete"):
                break
        return audio_bytes


if __name__ == "__main__":
    if not config.secret("gemini"):
        raise SystemExit("no Gemini key (config.secret('gemini') is empty) — cannot self-test")
    print("SELFTEST AUDIO BYTES (base64):", asyncio.run(_selftest()))
