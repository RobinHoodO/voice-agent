"""Gemini Live backend — provider wire protocol; constants + auth stay in gemini_client.py."""
import base64
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from backends.base import (AGENT_TRANSCRIPT, AUDIO_DELTA, AUDIO_DONE, OTHER,
                           SPEECH_STARTED, TOOL_CALL, USER_TRANSCRIPT, Backend,
                           NormalizedEvent)
from gemini_client import MODEL, HOST, _url
from tools import to_gemini_schema


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(msg)
    except Exception:
        pass


_VOICE_MAP = {
    "alloy": "Puck", "echo": "Charon", "shimmer": "Aoede", "ash": "Fenrir",
    "ballad": "Kore", "coral": "Leda", "sage": "Orus", "verse": "Zephyr",
}


class GeminiBackend(Backend):
    mic_rate = 16000
    manual_vad = True

    def __init__(self):
        self.ws = None
        self._pending_extra: list[NormalizedEvent] = []
        self._out_transcript_buf = ""
        self._in_transcript_buf = ""

    async def connect(self):
        import websockets
        self.ws = await websockets.connect(_url(), max_size=None)
        return self.ws

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()

    def build_setup(self, instructions: str, tools: list, voice: str,
                    audio_cfg: dict | None = None) -> dict:
        return {
            "setup": {
                "model": MODEL,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                        "voiceName": _VOICE_MAP.get(voice, "Puck")}}},
                },
                "systemInstruction": {"parts": [{"text": instructions}]},
                "tools": to_gemini_schema(tools),
                "realtimeInputConfig": {
                    "automaticActivityDetection": {"disabled": True},
                    "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                },
                "outputAudioTranscription": {},
                # Without a hint Gemini re-detects the language every utterance, so
                # short ones ("what about now?") came back as Spanish. The field is
                # languageCodes (plural list) — singular languageCode is rejected.
                "inputAudioTranscription": {
                    "languageCodes": config.get("live.languages") or ["en-US"]},
            },
        }

    async def send_setup(self, instructions: str, tools: list, voice: str,
                         audio_cfg: dict | None = None) -> None:
        await self.ws.send(json.dumps(self.build_setup(instructions, tools, voice, audio_cfg)))
        while True:
            ev = json.loads(await self.ws.recv())
            if "error" in ev:
                raise RuntimeError(ev["error"])
            if "setupComplete" in ev:
                _log("ev setupComplete")
                return

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        await self.ws.send(json.dumps({
            "realtimeInput": {"audio": {
                "data": base64.b64encode(pcm_bytes).decode(),
                "mimeType": f"audio/pcm;rate={self.mic_rate}",
            }},
        }))

    async def send_activity_start(self) -> None:
        await self.ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))

    async def send_activity_end(self) -> None:
        # No transcript flush here: Gemini's ASR of the utterance streams in AFTER this
        # point, so flushing now would strand it (see parse_event's flush rule).
        await self.ws.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))

    async def send_text_context(self, text: str, image_b64: str | None = None) -> None:
        if image_b64:
            await self.ws.send(json.dumps({"clientContent": {
                "turns": [{"role": "user", "parts": [{"inlineData": {
                    "mimeType": "image/jpeg", "data": image_b64}}]}],
                "turnComplete": False,
            }}))
        await self.ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}],
            "turnComplete": False,
        }}))

    async def send_tool_result(self, call_id: str, output: str) -> None:
        await self.ws.send(json.dumps({"toolResponse": {"functionResponses": [{
            "id": call_id, "response": {"result": output},
        }]}}))

    async def trigger_response(self) -> None:
        await self.ws.send(json.dumps({"clientContent": {
            "turns": [], "turnComplete": True,
        }}))

    async def cancel_response(self) -> None:
        # activityStart carries Gemini's interruption control.
        pass

    def drain_extra_events(self) -> list[NormalizedEvent]:
        pending, self._pending_extra = self._pending_extra, []
        return pending

    def parse_event(self, ev: dict) -> NormalizedEvent:
        if "toolCall" in ev:
            calls = ev["toolCall"]["functionCalls"]
            for call in calls[1:]:
                self._pending_extra.append(NormalizedEvent(
                    TOOL_CALL, call_id=call["id"], name=call["name"],
                    args=json.dumps(call.get("args") or {})))
            call = calls[0]
            return NormalizedEvent(TOOL_CALL, call_id=call["id"], name=call["name"],
                                   args=json.dumps(call.get("args") or {}))
        if "setupComplete" in ev:
            _log("ev setupComplete")
            return NormalizedEvent(OTHER)
        if "serverContent" in ev:
            sc = ev["serverContent"]
            if out := (sc.get("outputTranscription") or {}).get("text"):
                self._out_transcript_buf += out
            if incoming := (sc.get("inputTranscription") or {}).get("text"):
                self._in_transcript_buf += incoming

            audio = [NormalizedEvent(AUDIO_DELTA, audio=base64.b64decode(inline["data"]))
                     for part in (sc.get("modelTurn") or {}).get("parts") or []
                     if (inline := part.get("inlineData") or {}).get("data")]
            complete = bool(sc.get("turnComplete") or sc.get("generationComplete"))
            interrupted = bool(sc.get("interrupted"))

            # Built in DISPLAY order, then emitted head-first with the rest queued.
            out_events = []
            # Gemini streams its ASR of the user's utterance only AFTER local VAD has
            # already fired activityEnd, so the buffer can only be flushed once the model
            # starts replying — by then that utterance is fully transcribed. Flushing it
            # any earlier splits one sentence across several "you: …" lines; flushing it
            # at the NEXT activityEnd (the original design) stranded it a full round late,
            # which also delayed spoken yes/no reaching the confirmation gate.
            if audio or complete or interrupted:
                heard, self._in_transcript_buf = self._in_transcript_buf.strip(), ""
                if heard:
                    out_events.append(NormalizedEvent(USER_TRANSCRIPT, text=heard))
            if interrupted:
                out_events.append(NormalizedEvent(SPEECH_STARTED))
            else:
                out_events.extend(audio)
                if complete:
                    # AUDIO_DONE must always fire (it's what flips _speaking back off) —
                    # Gemini commonly bundles the last audio delta, turnComplete AND the
                    # transcript into one message, and an earlier version dropped it
                    # whenever a transcript was present.
                    out_events.append(NormalizedEvent(AUDIO_DONE))
                    said, self._out_transcript_buf = self._out_transcript_buf.strip(), ""
                    if said:
                        out_events.append(NormalizedEvent(AGENT_TRANSCRIPT, text=said))
            if out_events:
                self._pending_extra.extend(out_events[1:])
                return out_events[0]
        return NormalizedEvent(OTHER)


def _selftest() -> None:
    backend = GeminiBackend()
    tools = [{"type": "function", "name": "foo", "description": "d",
              "parameters": {"type": "object", "properties": {}}}]
    setup = backend.build_setup("instr", tools, "alloy")
    assert setup["setup"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert (setup["setup"]["generationConfig"]["speechConfig"]["voiceConfig"]
            ["prebuiltVoiceConfig"]["voiceName"] == "Puck")
    assert (setup["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
            ["disabled"] is True)
    assert setup["setup"]["tools"][0]["functionDeclarations"][0]["name"] == "foo"
    assert backend.parse_event({"setupComplete": {}}).kind == OTHER
    assert backend.parse_event({"serverContent": {"interrupted": True}}).kind == SPEECH_STARTED
    audio = backend.parse_event({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
        "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(b"abc").decode(),
    }}]}}})
    assert audio.kind == AUDIO_DELTA and audio.audio == b"abc"
    call = backend.parse_event({"toolCall": {"functionCalls": [{
        "id": "c1", "name": "foo", "args": {"x": 1},
    }]}})
    assert (call.kind, call.call_id, call.name, json.loads(call.args)) == (
        TOOL_CALL, "c1", "foo", {"x": 1})
    first = backend.parse_event({"toolCall": {"functionCalls": [
        {"id": "c1", "name": "foo", "args": {}},
        {"id": "c2", "name": "bar", "args": {"y": 2}},
    ]}})
    extra = backend.drain_extra_events()
    assert first.call_id == "c1" and len(extra) == 1 and extra[0].call_id == "c2"
    assert backend.drain_extra_events() == []
    assert "functionDeclarations" in to_gemini_schema(tools)[0]
    print("GEMINI BACKEND OK")


if __name__ == "__main__" and "--selftest" in sys.argv:
    _selftest()
