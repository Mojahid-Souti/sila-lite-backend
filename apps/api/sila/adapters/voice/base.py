"""Voice adapter interfaces — Rule 5 swap point.

The concierge consumes these Protocols; the current implementation calls the
internal sila-voice container (faster-whisper + Piper). A LiveKit/GPU
pipeline later implements the same two methods and nothing upstream changes.
"""

from __future__ import annotations

from typing import Protocol


class VoiceError(Exception):
    """STT/TTS failed — callers degrade to text-only, never crash the session."""


class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes) -> str:
        """One utterance of compressed audio (webm/opus/wav) → English text.

        Raises VoiceError on decode/transport failure.
        """
        ...


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> bytes:
        """Text → complete WAV bytes. Raises VoiceError on failure."""
        ...
