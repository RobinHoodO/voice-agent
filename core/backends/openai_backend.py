"""OpenAI Realtime backend — the protocol JSON that used to live inline in
live_session.py/audio.py, relocated verbatim. Constants + auth stay in
realtime_client.py.
"""
import base64
import json

from core import caps, config
from core.backends.base import (AGENT_TRANSCRIPT, AUDIO_DELTA, AUDIO_DONE, ERROR, OTHER,
                           RESPONSE_CREATED, RESPONSE_DONE, SPEECH_STARTED,
                           SPEECH_STOPPED, TOOL_CALL, USER_TRANSCRIPT, Backend,
                           NormalizedEvent)
from core.realtime_client import SR, URL, _headers


def _log(msg: str) -> None:
    caps.log(msg)


def _transcription_cfg() -> dict:
    """whisper-1, plus a language pin when the user speaks exactly one language."""
    langs = config.get("live.languages") or []
    cfg = {"model": "whisper-1"}
    if len(langs) == 1:
        cfg["language"] = langs[0].split("-")[0]   # whisper wants ISO-639-1 ("en")
    return cfg


def _turn_detection_cfg() -> dict:
    """When the API decides Robin has finished talking.

    Default is semantic_vad: a model judges whether the utterance is actually
    COMPLETE, instead of assuming any gap means "done". A pure silence timer
    can't tell thinking-mid-sentence from finished, so it fires on every pause —
    which is what made the agent act while he was still speaking.

    `eagerness: low` = wait the longest before responding. Fall back to the old
    server_vad timer with `live.turn_detection.mode = "server"`; both branches keep
    create_response:false so the app can inject cursor context and trigger the
    response itself.
    """
    td = config.get("live.turn_detection") or {}
    if (td.get("mode") or "semantic") == "server":
        # ponytail: silence timer, tunable. semantic mode is the better default —
        # this branch exists so a bad mic day can be fixed without a code change.
        return {"type": "server_vad",
                "threshold": td.get("threshold", 0.6),
                "prefix_padding_ms": td.get("prefix_padding_ms", 300),
                "silence_duration_ms": td.get("silence_ms", 1500),
                "create_response": False}
    return {"type": "semantic_vad",
            "eagerness": td.get("eagerness", "low"),
            "create_response": False}


class OpenAIBackend(Backend):

    def __init__(self):
        self.ws = None
        self._fn_names = {}   # call_id -> tool name (from response.output_item.added)

    async def connect(self):
        import websockets
        self.ws = await websockets.connect(URL, additional_headers=_headers(),
                                           max_size=None)
        return self.ws

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()

    def build_setup(self, instructions: str, tools: list, voice: str,
                    audio_cfg: dict | None = None) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SR},
                        "turn_detection": _turn_detection_cfg(),
                        # whisper's `language` forces ONE language, so only pin it when
                        # exactly one is configured — with several (en + nb) letting it
                        # auto-detect beats transcribing Norwegian as English phonetics.
                        "transcription": _transcription_cfg(),
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": SR}, "voice": voice},
                },
                "tools": tools,
                "tool_choice": "auto",
            },
        }

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm_bytes).decode(),
        }))

    async def send_text_context(self, text: str, image_b64: str | None = None) -> None:
        if image_b64:
            # Separate item so a rejected image never blocks the text context.
            await self.ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_image",
                                      "image_url": f"data:image/jpeg;base64,{image_b64}"}]},
            }))
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        }))

    async def send_tool_result(self, call_id: str, output: str) -> None:
        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output},
        }))

    async def trigger_response(self) -> None:
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def cancel_response(self) -> None:
        await self.ws.send(json.dumps({"type": "response.cancel"}))

    def parse_event(self, ev: dict) -> NormalizedEvent:
        t = ev.get("type", "")
        if t in ("session.updated", "input_audio_buffer.speech_started",
                 "input_audio_buffer.speech_stopped", "response.created",
                 "error"):
            _log(f"ev {t}" + (f" {ev.get('error')}" if t == "error" else ""))
        if t == "input_audio_buffer.speech_started":
            return NormalizedEvent(SPEECH_STARTED)
        if t == "input_audio_buffer.speech_stopped":
            return NormalizedEvent(SPEECH_STOPPED)
        if t == "response.created":
            return NormalizedEvent(RESPONSE_CREATED)
        if t == "response.done":
            return NormalizedEvent(RESPONSE_DONE, detail=ev.get("response") or {})
        if t == "response.output_audio.delta":
            return NormalizedEvent(AUDIO_DELTA, audio=base64.b64decode(ev["delta"]))
        if t == "response.output_audio.done":
            return NormalizedEvent(AUDIO_DONE)
        if t == "response.output_item.added":
            # function_call items carry the tool name + call_id; remember it so the
            # TOOL_CALL event can route (the .arguments.done event may omit the name).
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("call_id"):
                self._fn_names[item["call_id"]] = item.get("name", "")
            return NormalizedEvent(OTHER)
        if t == "response.function_call_arguments.done":
            call_id = ev.get("call_id")
            return NormalizedEvent(TOOL_CALL, call_id=call_id,
                                   name=ev.get("name") or self._fn_names.pop(call_id, ""),
                                   args=ev.get("arguments"))
        if t == "conversation.item.input_audio_transcription.completed":
            return NormalizedEvent(USER_TRANSCRIPT, text=ev.get("transcript", "").strip())
        if t == "response.output_audio_transcript.done":
            return NormalizedEvent(AGENT_TRANSCRIPT, text=(ev.get("transcript") or "").strip())
        if t == "error":
            return NormalizedEvent(ERROR, detail=ev.get("error"))
        return NormalizedEvent(OTHER)
