"""OpenAI Realtime API protocol layer: connection constants, auth headers, and a
text-only self-test round-trip. Extracted from realtime.py; LiveSession uses these.
"""
import asyncio
import json

import config

MODEL = "gpt-realtime"   # GA Realtime model (the old beta shape is disabled on this account)
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
SR = 24000          # Realtime PCM16 sample rate (fixed by the API)
VOICE = "alloy"     # default OpenAI voice (overridable via config live.voice)


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


def _headers() -> dict:
    # GA Realtime: plain bearer auth, no OpenAI-Beta header. Key from Keychain
    # (config), falling back to env in dev.
    return {"Authorization": f"Bearer {config.secret('openai') or ''}"}


async def _selftest() -> str:
    """Text-only round-trip — proves auth + model + protocol with no audio/GUI."""
    import websockets
    async with websockets.connect(URL, additional_headers=_headers(), max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update",
                                  "session": {"type": "realtime", "output_modalities": ["text"],
                                              "instructions": "Reply in one short sentence."}}))
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Say hello to Robin."}]}}))
        await ws.send(json.dumps({"type": "response.create"}))
        text = ""
        async for raw in ws:
            ev = json.loads(raw)
            tp = ev.get("type", "")
            if tp == "response.output_text.delta":
                text += ev.get("delta", "")
            elif tp == "response.output_text.done":
                text = ev.get("text", text)
            elif tp == "response.done":
                break
            elif tp == "error":
                raise RuntimeError(ev.get("error"))
        return text
