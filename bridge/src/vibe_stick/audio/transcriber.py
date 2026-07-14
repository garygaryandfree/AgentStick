from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from vibe_stick.config.paths import APP_SUPPORT_DIR

GROQ_ASR_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_ASR_MODEL = "whisper-large-v3-turbo"
DEFAULT_ASR_LANGUAGE = "zh"
DEFAULT_FUNASR_BASE_URL = "ws://127.0.0.1:10095"
FUNASR_AUDIO_CHUNK_BYTES = 1920

_hotword_cache: tuple[float, dict[str, int], dict[str, str]] | None = None


@dataclass
class TranscriptionResult:
    text: str = ""
    success: bool = False
    message: str = ""
    source: str = "none"


class TranscriptionAdapter:
    """project-owned boundary for speech-to-text providers.

    V1 does not bake any cloud ASR provider or secret into the bridge. A local
    command can be configured with VIBE_STICK_TRANSCRIBE_CMD and should print
    the final transcript to stdout.
    """

    def transcribe(
        self,
        session_payload: dict[str, Any],
        explicit_text: str = "",
    ) -> TranscriptionResult:
        explicit_text = explicit_text.strip()
        if explicit_text:
            return TranscriptionResult(
                text=explicit_text,
                success=True,
                message="Transcript supplied by request",
                source="request",
            )

        configured_text = os.environ.get("VIBE_STICK_TRANSCRIPT_TEXT", "").strip()
        if configured_text:
            return TranscriptionResult(
                text=configured_text,
                success=True,
                message="Transcript supplied by local development override",
                source="env",
            )

        command = os.environ.get("VIBE_STICK_TRANSCRIBE_CMD", "").strip()
        if not command:
            return self._transcribe_with_configured_asr(session_payload)

        try:
            result = subprocess.run(
                command,
                input=json.dumps(session_payload),
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=_command_timeout_seconds(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return TranscriptionResult(
                success=False,
                message=f"Transcription command failed: {exc}",
                source="command",
            )

        transcript = result.stdout.strip()
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Transcription command failed").strip()
            return TranscriptionResult(success=False, message=message, source="command")
        if not transcript:
            return TranscriptionResult(success=False, message="Transcription command returned no text", source="command")
        return TranscriptionResult(
            text=transcript,
            success=True,
            message="Transcript supplied by local command",
            source="command",
        )

    def _transcribe_with_configured_asr(self, session_payload: dict[str, Any]) -> TranscriptionResult:
        audio_file_raw = str(session_payload.get("audio_file") or "").strip()
        if not audio_file_raw:
            return TranscriptionResult(
                success=False,
                message="No audio file available for transcription",
                source="none",
            )
        audio_file = Path(audio_file_raw)
        if not audio_file.is_file():
            return TranscriptionResult(
                success=False,
                message="No audio file available for transcription",
                source="none",
            )

        config = _load_asr_config()
        provider = config.get("provider")
        if provider == "local-funasr" and config.get("base_url"):
            return _transcribe_funasr(audio_file, config)
        if provider not in {"groq", "openai-compatible"} or not config.get("api_key"):
            return TranscriptionResult(
                success=False,
                message="No transcription adapter configured",
                source="none",
            )
        return _transcribe_openai_compatible(audio_file, config)


def _command_timeout_seconds() -> int:
    raw = os.environ.get("VIBE_STICK_TRANSCRIBE_TIMEOUT_SECONDS", "120")
    try:
        value = int(raw)
    except ValueError:
        return 120
    return max(5, min(600, value))


def _asr_timeout_seconds() -> int:
    raw = (
        os.environ.get("VIBE_STICK_ASR_TIMEOUT_SECONDS")
        or os.environ.get("VIBE_STICK_GROQ_TIMEOUT_SECONDS")
        or "15"
    )
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(3, min(60, value))


def _asr_attempt_count() -> int:
    raw = (
        os.environ.get("VIBE_STICK_ASR_ATTEMPTS")
        or os.environ.get("VIBE_STICK_GROQ_ATTEMPTS")
        or "2"
    )
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(5, value))


def _load_asr_config() -> dict[str, str]:
    generic_env = _config_from_generic_env()
    if generic_env:
        return generic_env

    env_key = os.environ.get("VIBE_STICK_GROQ_API_KEY", "").strip()
    if env_key:
        return {
            "provider": "groq",
            "base_url": GROQ_ASR_BASE_URL,
            "api_key": env_key,
            "model": os.environ.get("VIBE_STICK_GROQ_MODEL", DEFAULT_ASR_MODEL).strip(),
            "language": os.environ.get("VIBE_STICK_GROQ_LANGUAGE", DEFAULT_ASR_LANGUAGE).strip(),
        }

    for path in _asr_config_paths():
        try:
            data = tomllib.loads(path.read_text())
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
            continue
        config = _config_from_toml(data)
        if config:
            return config
    return {}


def _config_from_generic_env() -> dict[str, str]:
    provider = _normalize_asr_provider(os.environ.get("VIBE_STICK_ASR_PROVIDER", ""))
    api_key = os.environ.get("VIBE_STICK_ASR_API_KEY", "").strip()
    base_url = os.environ.get("VIBE_STICK_ASR_BASE_URL", "").strip()
    model = os.environ.get("VIBE_STICK_ASR_MODEL", "").strip()
    language = os.environ.get("VIBE_STICK_ASR_LANGUAGE", "").strip()
    if not any((provider, api_key, base_url)):
        return {}
    if not provider:
        provider = "openai-compatible"
    if provider == "groq":
        api_key = api_key or os.environ.get("VIBE_STICK_GROQ_API_KEY", "").strip()
        base_url = base_url or GROQ_ASR_BASE_URL
        model = model or os.environ.get("VIBE_STICK_GROQ_MODEL", DEFAULT_ASR_MODEL).strip()
        language = language or os.environ.get("VIBE_STICK_GROQ_LANGUAGE", DEFAULT_ASR_LANGUAGE).strip()
    elif provider == "local-funasr":
        base_url = base_url or DEFAULT_FUNASR_BASE_URL
        model = model or "paraformer-zh-streaming"
        language = language or DEFAULT_ASR_LANGUAGE
    else:
        model = model or DEFAULT_ASR_MODEL
        language = language or DEFAULT_ASR_LANGUAGE
    return _asr_config(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        language=language,
    )


def _config_from_toml(data: dict[str, Any]) -> dict[str, str]:
    provider = _normalize_asr_provider(data.get("asr_provider") or data.get("provider") or "")
    api_key = str(data.get("api_key") or "").strip()
    base_url = str(data.get("base_url") or "").strip()
    model = str(data.get("model") or "").strip()
    language = str(data.get("language") or "").strip()
    groq_api_key = str(data.get("groq_api_key") or "").strip()
    if not provider and (api_key or base_url):
        provider = "openai-compatible"
    if provider == "groq":
        api_key = groq_api_key or api_key
        base_url = base_url or GROQ_ASR_BASE_URL
        model = str(data.get("groq_model") or model or DEFAULT_ASR_MODEL).strip()
        language = str(data.get("groq_language") or language or DEFAULT_ASR_LANGUAGE).strip()
    elif provider == "local-funasr":
        base_url = base_url or DEFAULT_FUNASR_BASE_URL
        model = model or "paraformer-zh-streaming"
        language = language or DEFAULT_ASR_LANGUAGE
    elif provider == "openai-compatible":
        model = model or DEFAULT_ASR_MODEL
        language = language or DEFAULT_ASR_LANGUAGE
    else:
        return {}
    return _asr_config(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        language=language,
    )


def _asr_config(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    language: str,
) -> dict[str, str]:
    return {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "language": language,
    }


def _normalize_asr_provider(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value in {"funasr", "local-funasr", "local_funasr"}:
        return "local-funasr"
    if value in {"groq", "openai-compatible"}:
        return value
    return ""


def _asr_config_paths() -> list[Path]:
    return [
        APP_SUPPORT_DIR / "asr.toml",
        APP_SUPPORT_DIR / "config.toml",
    ]


def _transcribe_openai_compatible(audio_file: Path, config: dict[str, str]) -> TranscriptionResult:
    source = config.get("provider") or "openai-compatible"
    label = _asr_label(source)
    if not config.get("api_key") or not config.get("base_url"):
        return TranscriptionResult(success=False, message="No transcription adapter configured", source="none")
    last_result = TranscriptionResult(success=False, message=f"{label} transcription failed", source=source)
    attempts = _asr_attempt_count()
    for attempt in range(1, attempts + 1):
        result = _transcribe_openai_compatible_once(audio_file, config, attempt)
        if result.success:
            return result
        last_result = result
        if attempt >= attempts or not _is_retryable_asr_error(result.message):
            return result
        time.sleep(min(2.0, 0.4 * attempt))
    return last_result


def _transcribe_openai_compatible_once(
    audio_file: Path,
    config: dict[str, str],
    attempt: int,
    opener=urllib.request.urlopen,  # noqa: ANN001
) -> TranscriptionResult:
    source = config.get("provider") or "openai-compatible"
    label = _asr_label(source)
    boundary = f"VibeStickASR-{uuid4().hex}"
    try:
        body = _multipart_body(
            boundary=boundary,
            audio_file=audio_file,
            model=config.get("model") or DEFAULT_ASR_MODEL,
            language=config.get("language") or DEFAULT_ASR_LANGUAGE,
        )
    except OSError as exc:
        return TranscriptionResult(success=False, message=f"Could not read audio file: {exc}", source=source)

    request = urllib.request.Request(
        _transcription_url(config.get("base_url", "")),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "VibeStick/0.1",
            "Connection": "close",
        },
    )
    try:
        with opener(request, timeout=_asr_timeout_seconds()) as response:
            response_data = response.read()
    except urllib.error.HTTPError as exc:
        _discard_http_error_body(exc)
        return TranscriptionResult(
            success=False,
            message=f"{label} transcription failed on attempt {attempt}: HTTP {exc.code}",
            source=source,
        )
    except (OSError, TimeoutError) as exc:
        return TranscriptionResult(
            success=False,
            message=f"{label} transcription failed on attempt {attempt}: {exc}",
            source=source,
        )

    try:
        payload = json.loads(response_data.decode("utf-8"))
    except json.JSONDecodeError:
        return TranscriptionResult(success=False, message=f"{label} returned unreadable JSON", source=source)
    text = str(payload.get("text") or "").strip()
    if not text:
        return TranscriptionResult(success=False, message=f"{label} returned no transcript", source=source)
    return TranscriptionResult(
        text=text,
        success=True,
        message=f"Transcript supplied by {label} ASR",
        source=source,
    )


def _transcription_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/audio/transcriptions"


def _asr_label(provider: str) -> str:
    if provider == "groq":
        return "Groq"
    if provider == "local-funasr":
        return "Local FunASR"
    return "OpenAI-compatible"


def _transcribe_funasr(audio_file: Path, config: dict[str, str]) -> TranscriptionResult:
    last_result = TranscriptionResult(
        success=False,
        message="Local FunASR transcription failed",
        source="local-funasr",
    )
    for attempt in range(1, _asr_attempt_count() + 1):
        result = _transcribe_funasr_once(audio_file, config, attempt)
        if result.success:
            return result
        last_result = result
        if attempt < _asr_attempt_count():
            time.sleep(min(0.5, 0.15 * attempt))
    return last_result


def _transcribe_funasr_once(
    audio_file: Path,
    config: dict[str, str],
    attempt: int,
    connector=None,  # noqa: ANN001
) -> TranscriptionResult:
    try:
        with wave.open(str(audio_file), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            compression = wav_file.getcomptype()
            audio = wav_file.readframes(wav_file.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        return TranscriptionResult(
            success=False,
            message=f"Could not read WAV audio file: {exc}",
            source="local-funasr",
        )
    if channels != 1 or sample_width != 2 or compression != "NONE":
        return TranscriptionResult(
            success=False,
            message="Local FunASR requires mono PCM16 WAV audio",
            source="local-funasr",
        )
    if not audio:
        return TranscriptionResult(
            success=False,
            message="Local FunASR received an empty recording",
            source="local-funasr",
        )

    if connector is None:
        try:
            from websockets.sync.client import connect as connector
        except ImportError:
            return TranscriptionResult(
                success=False,
                message="Local FunASR requires the 'websockets' package",
                source="local-funasr",
            )

    hotwords, normalizations = _load_funasr_hotwords()
    start_message = {
        "mode": "2pass",
        "chunk_size": [5, 10, 5],
        "chunk_interval": 10,
        "encoder_chunk_look_back": 4,
        "decoder_chunk_look_back": 0,
        "audio_fs": sample_rate,
        "wav_name": audio_file.name,
        "wav_format": "pcm",
        "is_speaking": True,
        "hotwords": json.dumps(hotwords, ensure_ascii=False) if hotwords else "",
        "itn": True,
    }
    timeout = _asr_timeout_seconds()
    chunk_delay = _funasr_chunk_delay_seconds()
    messages: list[dict[str, Any]] = []
    try:
        with connector(
            config["base_url"],
            subprotocols=["binary"],
            ping_interval=None,
            max_size=None,
            open_timeout=min(8, timeout),
            close_timeout=3,
        ) as websocket:
            websocket.send(json.dumps(start_message, ensure_ascii=False))
            for offset in range(0, len(audio), FUNASR_AUDIO_CHUNK_BYTES):
                websocket.send(audio[offset : offset + FUNASR_AUDIO_CHUNK_BYTES])
                if chunk_delay:
                    time.sleep(chunk_delay)
            websocket.send(json.dumps({"is_speaking": False}))

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw_message = websocket.recv(timeout=max(0.05, deadline - time.monotonic()))
                payload = json.loads(raw_message)
                if isinstance(payload, dict):
                    messages.append(payload)
                    if payload.get("is_final"):
                        break
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return TranscriptionResult(
            success=False,
            message=f"Local FunASR transcription failed on attempt {attempt}: {exc}",
            source="local-funasr",
        )

    transcript = _funasr_final_text(messages)
    transcript = _apply_funasr_normalizations(transcript, normalizations)
    if not transcript:
        return TranscriptionResult(
            success=False,
            message="Local FunASR returned no transcript",
            source="local-funasr",
        )
    return TranscriptionResult(
        text=transcript,
        success=True,
        message="Transcript supplied by Local FunASR ASR",
        source="local-funasr",
    )


def _funasr_final_text(messages: list[dict[str, Any]]) -> str:
    offline = ""
    online = ""
    for message in messages:
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        if message.get("mode") == "2pass-offline":
            offline = _merge_funasr_text(offline, text)
        elif message.get("mode") == "2pass-online":
            online += text
    return (offline or online).strip()


def _merge_funasr_text(previous: str, current: str) -> str:
    if not previous or current.startswith(previous):
        return current
    if previous.startswith(current):
        return previous
    max_overlap = min(len(previous), len(current))
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == current[:size]:
            return previous + current[size:]
    return previous + current


def _load_funasr_hotwords() -> tuple[dict[str, int], dict[str, str]]:
    global _hotword_cache
    url = os.environ.get("VIBE_STICK_FUNASR_HOTWORDS_URL", "").strip()
    if not url:
        return {}, {}
    now = time.monotonic()
    if _hotword_cache and now - _hotword_cache[0] < 300:
        return _hotword_cache[1], _hotword_cache[2]
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        hotwords = {
            str(word): int(weight)
            for word, weight in dict(payload.get("hotwords") or {}).items()
        }
        normalizations = {
            str(source): str(target)
            for source, target in dict(payload.get("normalizations") or {}).items()
        }
        _hotword_cache = (now, hotwords, normalizations)
        return hotwords, normalizations
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return {}, {}


def _apply_funasr_normalizations(text: str, normalizations: dict[str, str]) -> str:
    for source in sorted(normalizations, key=len, reverse=True):
        text = text.replace(source, normalizations[source])
        text = text.replace(source.lower(), normalizations[source])
    return text


def _funasr_chunk_delay_seconds() -> float:
    raw = os.environ.get("VIBE_STICK_FUNASR_CHUNK_DELAY_MS", "2")
    try:
        delay_ms = float(raw)
    except ValueError:
        delay_ms = 2.0
    return max(0.0, min(60.0, delay_ms)) / 1000.0


def _discard_http_error_body(exc: urllib.error.HTTPError) -> None:
    try:
        exc.read()
    except OSError:
        pass


def _is_retryable_asr_error(message: str) -> bool:
    retryable_fragments = (
        "HTTP 408",
        "HTTP 409",
        "HTTP 425",
        "HTTP 429",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
        "UNEXPECTED_EOF",
        "EOF occurred",
        "Remote end closed",
        "Connection reset",
        "Temporary failure",
        "timed out",
        "timeout",
        "SSL",
    )
    return any(fragment in message for fragment in retryable_fragments)


def _multipart_body(boundary: str, audio_file: Path, model: str, language: str) -> bytes:
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    add_field("model", model)
    add_field("response_format", "json")
    add_field("temperature", "0")
    if language:
        add_field("language", language)

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{audio_file.name}"\r\n'.encode()
    )
    body.extend(f"Content-Type: {_content_type(audio_file)}\r\n\r\n".encode())
    body.extend(audio_file.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body)


def _content_type(audio_file: Path) -> str:
    suffix = audio_file.suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".mp3":
        return "audio/mpeg"
    return "audio/mp4"
