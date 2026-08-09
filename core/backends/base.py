"""Backend interface for a live speech-to-speech session.

LiveSession orchestrates (state machine, tools, UI); a Backend owns the wire
protocol: the websocket, the setup payload, and mapping raw provider events into
the small NormalizedEvent vocabulary LiveSession branches on.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

# NormalizedEvent kinds — exactly the cases LiveSession._handle acts on.
SPEECH_STARTED = "speech_started"
SPEECH_STOPPED = "speech_stopped"
AUDIO_DELTA = "audio_delta"
AUDIO_DONE = "audio_done"
USER_TRANSCRIPT = "user_transcript"
AGENT_TRANSCRIPT = "agent_transcript"
TOOL_CALL = "tool_call"
RESPONSE_CREATED = "response_created"
RESPONSE_DONE = "response_done"
ERROR = "error"
OTHER = "other"   # anything not mapped above — ignored, never crashes the session


@dataclass
class NormalizedEvent:
    kind: str
    audio: bytes = b""            # AUDIO_DELTA
    text: str = ""                # USER_TRANSCRIPT / AGENT_TRANSCRIPT
    call_id: str = ""             # TOOL_CALL
    name: str = ""                # TOOL_CALL
    args: str | None = None       # TOOL_CALL — raw JSON string of arguments
    detail: object = None         # ERROR payload / RESPONSE_DONE response dict


class Backend(ABC):
    """One live provider connection. Implementations set self.ws in connect()."""

    ws = None
    mic_rate: int = 24000
    manual_vad: bool = False
    resume_handle: str | None = None   # providers that can resume a dropped session

    @abstractmethod
    async def connect(self):
        """Open the websocket, store it as self.ws, and return it."""

    @abstractmethod
    async def close(self) -> None:
        """Close the websocket if open."""

    @abstractmethod
    def build_setup(self, instructions: str, tools: list, voice: str,
                    audio_cfg: dict | None = None) -> dict:
        """The provider's session-setup payload (sent first on every connection)."""

    async def send_setup(self, instructions: str, tools: list, voice: str,
                         audio_cfg: dict | None = None) -> None:
        await self.ws.send(json.dumps(self.build_setup(instructions, tools, voice, audio_cfg)))

    async def send_activity_start(self) -> None:
        pass

    async def send_activity_end(self) -> None:
        pass

    @abstractmethod
    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Stream one chunk of raw mic PCM to the provider."""

    @abstractmethod
    async def send_text_context(self, text: str, image_b64: str | None = None) -> None:
        """Inject a user-role context message (optionally with a screenshot)."""

    @abstractmethod
    async def send_tool_result(self, call_id: str, output: str) -> None:
        """Return a tool call's output to the model."""

    @abstractmethod
    async def trigger_response(self) -> None:
        """Ask the model to respond now (we drive turns ourselves, not the server)."""

    @abstractmethod
    async def cancel_response(self) -> None:
        """Barge-in: stop the in-flight response."""

    def drain_extra_events(self) -> list[NormalizedEvent]:
        return []

    @abstractmethod
    def parse_event(self, ev: dict) -> NormalizedEvent:
        """Map one raw wire event into the NormalizedEvent vocabulary."""
