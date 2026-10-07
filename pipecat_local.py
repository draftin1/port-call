#!/usr/bin/env python3
"""Pipecat pipeline for Port Call — a local addition, not part of upstream.

Pipecat cannot join a Google Meet, so the bot stays the bot: browser, join, capture, camera, chat.
What moves here is what happens to the audio once the bot has it. The bot opens one WebSocket per
Meet audio stream and each connection gets its own Pipecat pipeline:

    bot --ws--> transport.input -> Silero VAD -> Whisper (stt_local.py, on this Mac)
                -> Smart Turn (is the speaker done? judged from the sound) -> events back to the bot

Why it is worth the move, measured against the bot's own pipeline (which re-sends a growing window
to Whisper every 2 s and calls text final when two passes agree, 6.3-8.5 s after speech by the
repo's notes): here the voice detector cuts the utterance when the speaker stops, Whisper hears it
once, and the text is final.

The voice leg is teed up, not on. With PIPECAT_VOICE unset nothing is ever sent back but events.
The slot is marked in build(): an LLM and a TTS between the turn detector and transport.output(),
and the serializer already carries audio frames back to the bot as raw PCM.

Wire format, bot -> here: a binary message is 8 bytes of little-endian capture time in ms (the
bot's clock, so transcripts can be matched to who was lit up when) followed by PCM16 mono 16 kHz.
Here -> bot: JSON text events (speech_start, transcript, turn_start, turn_end); binary, later, is
PCM for the bot to play.

The bot does not send silence: its in-page gate drops quiet blocks. So the end of an utterance
usually arrives as audio simply stopping, and VADProcessor's audio_idle_timeout is what closes
it. Blocks are 256 ms, so anything much under 0.4 s would cut speech at an ordinary hiccup.

    .venv-pipecat/bin/python pipecat_local.py          (./pc up does this)
"""
import asyncio, json, os, sys, time
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, WebSocket
from loguru import logger

from pipecat import __version__ as PIPECAT_VERSION
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    DataFrame, Frame, InputAudioRawFrame, OutputAudioRawFrame, OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame, TranscriptionFrame, UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

PORT = int(os.environ.get("PIPECAT_PORT", 8179))
STT_BASE = os.environ.get("PIPECAT_STT_BASE", "http://127.0.0.1:8177/v1")   # stt_local.py
RATE = 16000
VAD_START_SECS, VAD_STOP_SECS = 0.2, 0.2
IDLE_SECS = float(os.environ.get("PIPECAT_IDLE_SECS", 0.45))   # see the docstring: blocks are 256 ms
VOICE = os.environ.get("PIPECAT_VOICE", "") == "1"

# Pipecat logs transcript text at DEBUG. Transcripts are meeting content: keep them out of the log.
logger.remove()
logger.add(sys.stderr, level=os.environ.get("PIPECAT_LOG", "WARNING"))


@dataclass
class Stream:
    """What one connection knows: the bot's capture clock, and the utterance being cut."""
    meeting: str
    channel: str
    capture_ms: int = 0           # capture time of the END of the newest block received
    speech_t0: int = 0            # capture time the current utterance began
    speech_t1: int = 0            # ...and ended
    stopped_wall: float = 0.0     # wall clock at the cut, to time Whisper from
    stats: dict = field(default_factory=lambda: {"utterances": 0, "empty": 0, "stt_ms": 0, "turns": 0})


@dataclass
class Segment(DataFrame):
    """One utterance, cut by the voice detector and heard once by Whisper."""
    text: str = ""
    t0: int = 0
    t1: int = 0
    stt_ms: int = 0
    language: str = "en"


class PortCallSerializer(FrameSerializer):
    def __init__(self, stream: Stream):
        super().__init__()
        self.stream = stream

    async def deserialize(self, data):
        if isinstance(data, (bytes, bytearray)) and len(data) > 8:
            pcm = bytes(data[8:])
            self.stream.capture_ms = int.from_bytes(data[:8], "little") + len(pcm) * 1000 // (2 * RATE)
            return InputAudioRawFrame(audio=pcm, sample_rate=RATE, num_channels=1)
        return None   # no control messages yet

    async def serialize(self, frame: Frame):
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message)
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio   # the voice leg: PCM for the bot to play
        return None


class Clock(FrameProcessor):
    """Stamps each utterance with the bot's capture clock, between the voice detector and Whisper."""

    def __init__(self, stream: Stream):
        super().__init__()
        self.stream = stream

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self.stream.speech_t0 = self.stream.capture_ms - int(VAD_START_SECS * 1000)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self.stream.speech_t1, self.stream.stopped_wall = self.stream.capture_ms, time.monotonic()
        await self.push_frame(frame, direction)


class LocalWhisper(OpenAISTTService):
    """Whisper through stt_local.py, so the console's model dropdown still decides the model.
    Emits a Segment for every utterance, empty ones included, so a cut that produced no text is
    visible rather than silently missing."""

    def __init__(self, stream: Stream):
        super().__init__(base_url=STT_BASE, api_key="local",
                         settings=OpenAISTTService.Settings(model="whisper-1", language=Language.EN))
        self.stream = stream

    async def run_stt(self, audio: bytes):
        s, text, language = self.stream, "", "en"
        t0, t1, cut = s.speech_t0, s.speech_t1, s.stopped_wall
        async for frame in super().run_stt(audio):
            if isinstance(frame, TranscriptionFrame):
                text += frame.text
                language = str(frame.language.value) if frame.language else language
            yield frame
        ms = round((time.monotonic() - cut) * 1000) if cut else 0
        s.stats["utterances"] += 1
        s.stats["empty"] += 0 if text.strip() else 1
        s.stats["stt_ms"] = ms
        text = " ".join(text.split())      # whisper.cpp wraps its text; a transcript line is one line
        yield Segment(text=text, t0=t0, t1=t1, stt_ms=ms, language=language)


class Events(FrameProcessor):
    """The pipeline's frames, as the JSON events the bot acts on."""

    def __init__(self, stream: Stream):
        super().__init__()
        self.stream = stream

    async def send(self, **message):
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                await self.send(type="speech_start", t=self.stream.speech_t0)
            elif isinstance(frame, Segment):
                await self.send(type="transcript", text=frame.text, t0=frame.t0, t1=frame.t1,
                                language=frame.language, stt_ms=frame.stt_ms)
            elif isinstance(frame, UserStartedSpeakingFrame):
                await self.send(type="turn_start", t=self.stream.speech_t0)
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self.stream.stats["turns"] += 1
                await self.send(type="turn_end", t=self.stream.speech_t1)
        await self.push_frame(frame, direction)


def build(websocket: WebSocket, stream: Stream) -> PipelineWorker:
    transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_in_sample_rate=RATE, audio_in_channels=1, audio_in_passthrough=True,
        audio_out_enabled=VOICE, add_wav_header=False, session_timeout=None,
        serializer=PortCallSerializer(stream)))
    turns = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]))
    pipeline = Pipeline([
        transport.input(),
        VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=RATE, params=VADParams(
            start_secs=VAD_START_SECS, stop_secs=VAD_STOP_SECS)), audio_idle_timeout=IDLE_SECS),
        Clock(stream),
        LocalWhisper(stream),
        turns,
        Events(stream),
        # ── the voice leg goes here when it is wanted ──────────────────────────────────────────
        # A user-turn aggregator, an LLM and a TTS: the turn_end the bot already receives is the
        # frame an LLM would answer, and transport.output() already returns audio to the bot.
        transport.output(),
    ])
    # A stream nobody speaks on for five minutes is normal in a meeting, not a dead session.
    # enable_rtvi=False: by default Pipecat also sends its own client protocol down the socket,
    # which duplicates these events and carries the transcript text a second time.
    task = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=RATE),
                          cancel_on_idle_timeout=False, enable_rtvi=False)

    @transport.event_handler("on_client_disconnected")
    async def gone(_transport, _client):
        await task.cancel()

    return task


app = FastAPI()
STREAMS: dict[str, Stream] = {}


@app.get("/health")
def health():
    return {"status": "ok", "pipecat": PIPECAT_VERSION, "stt": STT_BASE, "voice": VOICE,
            "turn_detector": "smart-turn-v3 (local)", "idle_secs": IDLE_SECS,
            "streams": {k: v.stats for k, v in STREAMS.items()}}


@app.websocket("/ws")
async def ws(websocket: WebSocket, meeting: str = "?", channel: str = "0"):
    await websocket.accept()
    key = f"{meeting}/{channel}"
    stream = STREAMS[key] = Stream(meeting, channel)
    print(f"[pipecat] {time.strftime('%H:%M:%S')} stream {key} connected", flush=True)
    try:
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(build(websocket, stream))
        await runner.run()
    finally:
        STREAMS.pop(key, None)
        print(f"[pipecat] {time.strftime('%H:%M:%S')} stream {key} closed {stream.stats}", flush=True)


if __name__ == "__main__":
    print(f"[pipecat] {PIPECAT_VERSION} on 127.0.0.1:{PORT}, whisper at {STT_BASE}, voice leg "
          f"{'ON' if VOICE else 'off'}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
