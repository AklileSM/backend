"""Spoken assistant replies, made by Kokoro on the Ollama machine.

Kokoro-FastAPI serves an OpenAI-style speech endpoint. The browser asks the backend for audio
rather than calling Kokoro itself, so Kokoro only has to be reachable from here. When it is
switched off or can't be reached the browser speaks the reply with its own voice instead.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

AUDIO_TYPE = "audio/mpeg"
# A reply takes about 0.1 s on the GPU; a server that is down should fail fast, not stall the reply.
TIMEOUT = httpx.Timeout(15.0, connect=2.0)


def synthesize(text: str) -> bytes | None:
    """MP3 audio of `text`, or None when Kokoro is not configured or not reachable."""
    settings = get_settings()
    if not settings.assistant_tts_url:
        return None
    try:
        response = httpx.post(
            settings.assistant_tts_url,
            json={
                "model": "kokoro",
                "input": text,
                "voice": settings.assistant_tts_voice,
                "response_format": "mp3",
                "speed": 1.0,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Assistant speech failed: %s", exc)
        return None
    return response.content or None
