"""HTTP adapters for the internal sila-voice container."""

from __future__ import annotations

import httpx

from .base import VoiceError


class HttpSpeechToText:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _client(self) -> httpx.AsyncClient:
        # Test seam — tests swap in httpx.MockTransport.
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def transcribe(self, audio: bytes) -> str:
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self._base_url}/stt",
                    content=audio,
                    headers={"Content-Type": "application/octet-stream"},
                )
                resp.raise_for_status()
                return str(resp.json().get("text", ""))
        except httpx.HTTPError as exc:
            raise VoiceError(f"STT failed: {exc}") from exc


class HttpTextToSpeech:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def synthesize(self, text: str) -> bytes:
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._base_url}/tts", json={"text": text})
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            raise VoiceError(f"TTS failed: {exc}") from exc
