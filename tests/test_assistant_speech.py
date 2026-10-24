import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.config import Settings
from app.main import app
from app.models import User
from app.services import assistant_speech

KOKORO_URL = "http://kokoro.test:8880/v1/audio/speech"


def kokoro_settings(url: str = KOKORO_URL) -> Settings:
    return Settings(assistant_tts_url=url, assistant_tts_voice="af_heart")


def kokoro_reply(status: int = 200, content: bytes = b"ID3audio") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("POST", KOKORO_URL))


class AssistantSpeechTests(unittest.TestCase):
    """Spoken replies: audio from Kokoro, or a 503 that sends the browser to its own voice."""

    def setUp(self) -> None:
        app.dependency_overrides[get_current_user] = lambda: User(username="operator", password_hash="x")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def speak(self, text: str = "The robot is on its way to the kitchen."):
        return self.client.post("/api/robot/assistant/speech", json={"text": text})

    def test_returns_kokoro_audio(self) -> None:
        with mock.patch.object(assistant_speech, "get_settings", return_value=kokoro_settings()), \
                mock.patch.object(assistant_speech.httpx, "post", return_value=kokoro_reply()) as post:
            response = self.speak()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.content, b"ID3audio")
        self.assertEqual(post.call_args.args[0], KOKORO_URL)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["input"], "The robot is on its way to the kitchen.")
        self.assertEqual(body["voice"], "af_heart")
        self.assertEqual(body["response_format"], "mp3")

    def test_unreachable_kokoro_is_503(self) -> None:
        with mock.patch.object(assistant_speech, "get_settings", return_value=kokoro_settings()), \
                mock.patch.object(assistant_speech.httpx, "post", side_effect=httpx.ConnectError("refused")):
            response = self.speak()

        self.assertEqual(response.status_code, 503)

    def test_kokoro_error_is_503(self) -> None:
        with mock.patch.object(assistant_speech, "get_settings", return_value=kokoro_settings()), \
                mock.patch.object(assistant_speech.httpx, "post", return_value=kokoro_reply(400, b'{"detail":"bad voice"}')):
            response = self.speak()

        self.assertEqual(response.status_code, 503)

    def test_switched_off_skips_kokoro(self) -> None:
        with mock.patch.object(assistant_speech, "get_settings", return_value=kokoro_settings(url="")), \
                mock.patch.object(assistant_speech.httpx, "post") as post:
            response = self.speak()

        self.assertEqual(response.status_code, 503)
        post.assert_not_called()

    def test_rejects_empty_and_overlong_text(self) -> None:
        with mock.patch.object(assistant_speech.httpx, "post") as post:
            self.assertEqual(self.speak("").status_code, 422)
            self.assertEqual(self.speak("x" * 2001).status_code, 422)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
