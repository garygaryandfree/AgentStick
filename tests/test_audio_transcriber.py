import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from vibe_stick.audio import transcriber


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class TranscriberConfigTests(unittest.TestCase):
    def test_local_funasr_config_does_not_require_api_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VIBE_STICK_ASR_PROVIDER": "local-funasr",
                "VIBE_STICK_ASR_BASE_URL": "ws://192.168.31.100:10095",
            },
            clear=True,
        ):
            config = transcriber._load_asr_config()

        self.assertEqual(config["provider"], "local-funasr")
        self.assertEqual(config["base_url"], "ws://192.168.31.100:10095")
        self.assertEqual(config["api_key"], "")

    def test_local_funasr_sends_pcm_and_uses_offline_result(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[object] = []
                self.messages = iter(
                    [
                        '{"mode":"2pass-online","text":"Linux"}',
                        '{"mode":"2pass-offline","text":"linux GitHub","is_final":true}',
                    ]
                )

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def send(self, payload: object) -> None:
                self.sent.append(payload)

            def recv(self, timeout=None):  # noqa: ANN001, ANN201
                return next(self.messages)

        socket = FakeSocket()

        def connector(uri, **kwargs):  # noqa: ANN001, ANN201
            self.assertEqual(uri, "ws://funasr.test:10095")
            self.assertEqual(kwargs["subprotocols"], ["binary"])
            return socket

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "sample.wav"
            with wave.open(str(audio), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x01\x00" * 1920)

            with mock.patch.object(
                transcriber,
                "_load_funasr_hotwords",
                return_value=({"linux": 35}, {"linux": "Linux"}),
            ):
                with mock.patch.object(transcriber, "_funasr_chunk_delay_seconds", return_value=0):
                    result = transcriber._transcribe_funasr_once(
                        audio,
                        {
                            "provider": "local-funasr",
                            "base_url": "ws://funasr.test:10095",
                            "api_key": "",
                            "model": "paraformer-zh-streaming",
                            "language": "zh",
                        },
                        attempt=1,
                        connector=connector,
                    )

        self.assertTrue(result.success)
        self.assertEqual(result.source, "local-funasr")
        self.assertEqual(result.text, "Linux GitHub")
        self.assertIsInstance(socket.sent[0], str)
        self.assertTrue(any(isinstance(item, bytes) for item in socket.sent))
        self.assertEqual(socket.sent[-1], '{"is_speaking": false}')

    def test_load_asr_config_reads_vibestick_asr_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_support = root / "VibeStick"
            app_support.mkdir(parents=True)
            (app_support / "asr.toml").write_text(
                "\n".join(
                    [
                        'asr_provider = "groq"',
                        'groq_api_key = "local-key"',
                        'groq_model = "whisper-large-v3-turbo"',
                        'groq_language = "zh"',
                    ]
                )
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(transcriber, "APP_SUPPORT_DIR", app_support):
                    config = transcriber._load_asr_config()

        self.assertEqual(config["provider"], "groq")
        self.assertEqual(config["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(config["api_key"], "local-key")
        self.assertEqual(config["model"], "whisper-large-v3-turbo")
        self.assertEqual(config["language"], "zh")

    def test_environment_api_key_takes_precedence_over_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_support = root / "VibeStick"
            app_support.mkdir(parents=True)
            (app_support / "config.toml").write_text(
                'asr_provider = "groq"\ngroq_api_key = "local-key"\n'
            )

            with mock.patch.dict(os.environ, {"VIBE_STICK_GROQ_API_KEY": "env-key"}, clear=True):
                with mock.patch.object(transcriber, "APP_SUPPORT_DIR", app_support):
                    config = transcriber._load_asr_config()

        self.assertEqual(config["api_key"], "env-key")

    def test_groq_key_without_generic_provider_uses_groq_preset(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VIBE_STICK_GROQ_API_KEY": "env-key",
                "VIBE_STICK_ASR_PROVIDER": "",
                "VIBE_STICK_ASR_MODEL": transcriber.DEFAULT_ASR_MODEL,
            },
            clear=True,
        ):
            with mock.patch.object(transcriber, "APP_SUPPORT_DIR", Path("/tmp/does-not-exist-vibestick")):
                config = transcriber._load_asr_config()

        self.assertEqual(config["provider"], "groq")
        self.assertEqual(config["api_key"], "env-key")
        self.assertEqual(config["base_url"], transcriber.GROQ_ASR_BASE_URL)

    def test_openai_compatible_toml_config_parses_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_support = root / "VibeStick"
            app_support.mkdir(parents=True)
            (app_support / "asr.toml").write_text(
                "\n".join(
                    [
                        'asr_provider = "openai-compatible"',
                        'base_url = "https://asr.example.test/openai/v1/"',
                        'api_key = "local-key"',
                        'model = "whisper-test"',
                        'language = "en"',
                    ]
                )
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(transcriber, "APP_SUPPORT_DIR", app_support):
                    config = transcriber._load_asr_config()

        self.assertEqual(config["provider"], "openai-compatible")
        self.assertEqual(config["base_url"], "https://asr.example.test/openai/v1/")
        self.assertEqual(config["api_key"], "local-key")
        self.assertEqual(config["model"], "whisper-test")
        self.assertEqual(config["language"], "en")

    def test_openai_compatible_url_joins_trailing_slash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "sample.wav"
            audio.write_bytes(b"RIFFtest")
            seen: dict[str, str] = {}

            def opener(request, timeout=None):  # noqa: ANN001
                seen["url"] = request.full_url
                seen["authorization"] = request.headers.get("Authorization", "")
                return _FakeResponse(b'{"text":"hello"}')

            result = transcriber._transcribe_openai_compatible_once(
                audio,
                {
                    "provider": "openai-compatible",
                    "base_url": "https://asr.example.test/openai/v1/",
                    "api_key": "secret-key",
                    "model": "whisper-test",
                    "language": "en",
                },
                attempt=1,
                opener=opener,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.source, "openai-compatible")
        self.assertEqual(seen["url"], "https://asr.example.test/openai/v1/audio/transcriptions")
        self.assertEqual(seen["authorization"], "Bearer secret-key")

    def test_legacy_groq_config_uses_openai_compatible_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "sample.wav"
            audio.write_bytes(b"RIFFtest")
            seen: dict[str, str] = {}

            def opener(request, timeout=None):  # noqa: ANN001
                seen["url"] = request.full_url
                return _FakeResponse(b'{"text":"hello"}')

            result = transcriber._transcribe_openai_compatible_once(
                audio,
                {
                    "provider": "groq",
                    "base_url": transcriber.GROQ_ASR_BASE_URL,
                    "api_key": "secret-key",
                    "model": "whisper-large-v3-turbo",
                    "language": "zh",
                },
                attempt=1,
                opener=opener,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.source, "groq")
        self.assertEqual(seen["url"], "https://api.groq.com/openai/v1/audio/transcriptions")

    def test_missing_openai_compatible_key_fails_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_support = root / "VibeStick"
            app_support.mkdir(parents=True)
            (app_support / "asr.toml").write_text(
                "\n".join(
                    [
                        'asr_provider = "openai-compatible"',
                        'base_url = "https://asr.example.test/openai/v1"',
                    ]
                )
            )
            audio = root / "sample.wav"
            audio.write_bytes(b"RIFFtest")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(transcriber, "APP_SUPPORT_DIR", app_support):
                    result = transcriber.TranscriptionAdapter().transcribe({"audio_file": str(audio)})

        self.assertFalse(result.success)
        self.assertEqual(result.source, "none")
        self.assertEqual(result.message, "No transcription adapter configured")


if __name__ == "__main__":
    unittest.main()
