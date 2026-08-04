"""Offline coverage for Gemini Live wire payloads and normalized events."""
import base64
import json

from backends.base import (AGENT_TRANSCRIPT, AUDIO_DELTA, AUDIO_DONE, OTHER,
                           SPEECH_STARTED, TOOL_CALL, USER_TRANSCRIPT)
from backends.gemini_backend import GeminiBackend
from tools import to_gemini_schema


def _tools():
    return [{"type": "function", "name": "foo", "description": "d",
             "parameters": {"type": "object", "properties": {}}}]


def test_build_setup_uses_audio_manual_vad_and_gemini_tools():
    setup = GeminiBackend().build_setup("instr", _tools(), "alloy")
    assert setup["setup"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert (setup["setup"]["generationConfig"]["speechConfig"]["voiceConfig"]
            ["prebuiltVoiceConfig"]["voiceName"] == "Puck")
    assert (setup["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
            ["disabled"] is True)
    assert setup["setup"]["tools"][0]["functionDeclarations"][0]["name"] == "foo"


def test_parse_setup_complete_is_ignored():
    assert GeminiBackend().parse_event({"setupComplete": {}}).kind == OTHER


def test_setup_asks_for_resumption_handles_when_it_has_none():
    assert GeminiBackend().build_setup("i", _tools(), "alloy")["setup"]["sessionResumption"] == {}


def test_setup_replays_a_stored_handle():
    backend = GeminiBackend()
    backend.resume_handle = "h-123"
    setup = backend.build_setup("i", _tools(), "alloy")
    assert setup["setup"]["sessionResumption"] == {"handle": "h-123"}


def test_resumable_handle_is_stored_and_non_resumable_never_clobbers_it():
    backend = GeminiBackend()
    assert backend.parse_event({"sessionResumptionUpdate": {
        "newHandle": "h-1", "resumable": True}}).kind == OTHER
    assert backend.resume_handle == "h-1"
    backend.parse_event({"sessionResumptionUpdate": {"newHandle": "", "resumable": False}})
    assert backend.resume_handle == "h-1"
    backend.parse_event({"sessionResumptionUpdate": {"newHandle": "h-2", "resumable": True}})
    assert backend.resume_handle == "h-2"


def test_go_away_is_survivable():
    assert GeminiBackend().parse_event({"goAway": {"timeLeft": "5s"}}).kind == OTHER


def test_parse_interrupted_as_speech_started():
    assert GeminiBackend().parse_event({"serverContent": {"interrupted": True}}).kind == SPEECH_STARTED


def test_parse_audio_delta():
    event = GeminiBackend().parse_event({"serverContent": {"modelTurn": {"parts": [{
        "inlineData": {"mimeType": "audio/pcm;rate=24000",
                       "data": base64.b64encode(b"abc").decode()},
    }]}}})
    assert event.kind == AUDIO_DELTA
    assert event.audio == b"abc"


def test_parse_tool_call_uses_json_arguments():
    event = GeminiBackend().parse_event({"toolCall": {"functionCalls": [{
        "id": "c1", "name": "foo", "args": {"x": 1},
    }]}})
    assert event.kind == TOOL_CALL
    assert event.call_id == "c1"
    assert event.name == "foo"
    assert event.args == json.dumps({"x": 1})


def test_parse_multiple_tool_calls_drains_extra_events():
    backend = GeminiBackend()
    first = backend.parse_event({"toolCall": {"functionCalls": [
        {"id": "c1", "name": "foo", "args": {}},
        {"id": "c2", "name": "bar", "args": {"y": 2}},
    ]}})
    extra = backend.drain_extra_events()
    assert first.call_id == "c1"
    assert len(extra) == 1
    assert (extra[0].call_id, extra[0].name, extra[0].args) == (
        "c2", "bar", json.dumps({"y": 2}))
    assert backend.drain_extra_events() == []


def test_to_gemini_schema_uses_camel_case_function_declarations():
    assert "functionDeclarations" in to_gemini_schema(_tools())[0]


def test_parse_final_audio_chunk_with_transcript_still_emits_audio_done():
    """Gemini commonly bundles the last audio delta + turnComplete + transcript into
    ONE serverContent message — AUDIO_DONE (which flips _speaking off) must still fire
    even though the primary/first returned event is the audio delta itself."""
    backend = GeminiBackend()
    primary = backend.parse_event({"serverContent": {
        "modelTurn": {"parts": [{"inlineData": {
            "mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(b"xyz").decode()}}]},
        "outputTranscription": {"text": "hello there"},
        "turnComplete": True, "generationComplete": True,
    }})
    assert primary.kind == AUDIO_DELTA
    extra = backend.drain_extra_events()
    kinds = [e.kind for e in extra]
    assert AUDIO_DONE in kinds
    assert AGENT_TRANSCRIPT in kinds
    transcript = next(e for e in extra if e.kind == AGENT_TRANSCRIPT)
    assert transcript.text == "hello there"


def _audio_part(payload=b"xyz"):
    return {"inlineData": {"mimeType": "audio/pcm;rate=24000",
                           "data": base64.b64encode(payload).decode()}}


def test_input_transcript_accumulates_without_emitting_partials():
    """ASR chunks alone must NOT emit — flushing per chunk would split one spoken
    sentence across several "you: …" lines."""
    backend = GeminiBackend()
    for chunk in ("What ", "about ", "now?"):
        ev = backend.parse_event({"serverContent": {"inputTranscription": {"text": chunk}}})
        assert ev.kind == OTHER
        assert backend.drain_extra_events() == []


def test_input_transcript_flushes_when_model_starts_replying_and_leads_the_reply():
    """Regression: ASR streams in after activityEnd, so the old flush-at-activityEnd
    stranded it until the NEXT turn ended — "you: …" printed a full round late and a
    spoken yes/no reached the confirmation gate a turn too late. It must land on the
    same turn, and BEFORE the reply audio so the feed reads you -> agent."""
    backend = GeminiBackend()
    for chunk in ("What ", "about ", "now?"):
        backend.parse_event({"serverContent": {"inputTranscription": {"text": chunk}}})
    primary = backend.parse_event({"serverContent": {"modelTurn": {"parts": [_audio_part()]}}})
    assert primary.kind == USER_TRANSCRIPT
    assert primary.text == "What about now?"
    extra = backend.drain_extra_events()
    assert [e.kind for e in extra] == [AUDIO_DELTA]
    # buffer is consumed — the next turn must not replay it
    nxt = backend.parse_event({"serverContent": {"modelTurn": {"parts": [_audio_part(b"abc")]}}})
    assert nxt.kind == AUDIO_DELTA
    assert backend.drain_extra_events() == []


def test_input_transcript_flushes_on_a_bare_turn_complete():
    """A turn that produced no audio (e.g. a tool-only turn) must still surface what
    the user said rather than holding it for the next turn."""
    backend = GeminiBackend()
    backend.parse_event({"serverContent": {"inputTranscription": {"text": "yes"}}})
    primary = backend.parse_event({"serverContent": {"turnComplete": True}})
    assert primary.kind == USER_TRANSCRIPT and primary.text == "yes"
    assert [e.kind for e in backend.drain_extra_events()] == [AUDIO_DONE]
