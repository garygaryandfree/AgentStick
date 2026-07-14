from __future__ import annotations

import json
import math
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

from vibe_stick.audio.transcriber import (
    FUNASR_AUDIO_CHUNK_BYTES,
    _apply_funasr_normalizations,
    _load_asr_config,
    _load_funasr_hotwords,
    _merge_funasr_text,
)
from vibe_stick.paste.input_injector import PasteInjector

SPEECH_RMS_THRESHOLD = 900.0
SPEECH_START_BLOCKS = 2
MINIMUM_SPEECH_BLOCKS = 3
PRE_ROLL_BLOCKS = 5
LIVE_FINAL_MIN_SECONDS = 0.16
LIVE_FINAL_QUIET_SECONDS = 0.10
_PUNCTUATION = "，。！？；：,.!?;:"
_SPOKEN_DECIMAL_AS_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,3}):([1-5]0):0?(\d)(?:秒)?(?=(?:长|的)?(?:录音|语音|测试))"
)
_URL_AT_END_RE = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?$",
    re.IGNORECASE,
)
_SPOKEN_FORMAT_COMMANDS = {
    "换行": "\n",
    "另起一行": "\n",
    "下一行": "\n",
    "句号": "。",
    "逗号": "，",
    "问号": "？",
    "感叹号": "！",
}

# Context-bound repairs for recurring Paraformer errors in technical dictation.
# Keep these longer than a single ambiguous word so ordinary Chinese remains
# untouched (for example, never replace every occurrence of "无线").
_TECHNICAL_PHRASE_REPAIRS = (
    (re.compile(r"(?i)(?<![A-Za-z0-9])s\s*[三3](?![A-Za-z0-9])"), "S3"),
    (re.compile(r"(?i)great\s*wall"), "Gateway"),
    (re.compile(r"(?i)g\s*p\s*i\s*o"), "GPIO"),
    (re.compile(r"更音是按键松开事件"), "根因是按键松开事件"),
    (re.compile(r"按键松开[，,]?时间正常停止"), "按键松开事件正常停止"),
    (re.compile(r"防止无线卡住"), "防止无限卡住"),
    (re.compile(r"(?:自动)?收回绘画"), "自动回收会话"),
    (re.compile(r"事件队列满十(?=记录错误)"), "事件队列满时"),
)


@dataclass(frozen=True)
class StreamingResult:
    text: str
    audio: bytes
    success: bool
    input_success: bool
    message: str


def live_input_edit(previous: str, current: str) -> tuple[int, str]:
    if current == previous:
        return 0, ""
    common = 0
    for old_character, new_character in zip(previous, current):
        if old_character != new_character:
            break
        common += 1
    return len(previous) - common, current[common:]


def locked_live_input_target(previous: str, current: str, locked_length: int) -> str:
    """Preserve calibrated segments while allowing only the active tail to change."""
    locked_length = min(max(0, locked_length), len(previous))
    if locked_length == 0 or previous[:locked_length] == current[:locked_length]:
        return current
    return previous[:locked_length] + current[min(locked_length, len(current)) :]


def committed_segment_delta(previous: str, current: str, fallback: str) -> str:
    if current.startswith(previous):
        return current[len(previous) :]
    return fallback.strip()


def spoken_format_command(text: str) -> str | None:
    """Return a deterministic edit for a standalone spoken format command."""
    command = text.strip(" \t\r\n，。！？；：,.!?;:")
    return _SPOKEN_FORMAT_COMMANDS.get(command)


def strip_trailing_url_period(text: str) -> str:
    """URLs are identifiers, so remove sentence periods emitted by ASR."""
    stripped = text.rstrip()
    if not stripped.endswith(("。", ".")):
        return text
    without_period = stripped[:-1]
    return without_period if _URL_AT_END_RE.search(without_period) else text


def normalize_stream_text(
    text: str,
    normalizations: dict[str, str],
    *,
    sentence_final: bool = False,
) -> str:
    text = _apply_funasr_normalizations(text, normalizations).strip()
    for pattern, replacement in _TECHNICAL_PHRASE_REPAIRS:
        text = pattern.sub(replacement, text)
    text = re.sub(
        r"断流(\d+)\s*s动(?:自动)?回收会话",
        r"断流\1 秒自动回收会话",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<=\d)\s*(?:ms|毫秒)(?=\D|$)", " ms", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*s(?=(?:自动|断流|后|内|$))", " 秒", text, flags=re.IGNORECASE)
    # Explicit symbol words are unambiguous. Requiring the user to say the
    # symbol name avoids guessing whether an ordinary “到” means an arrow.
    text = text.replace("箭头", "→")
    text = re.sub(r"(?<=\d)冒号(?=\d)", ":", text)
    text = _SPOKEN_DECIMAL_AS_TIME_RE.sub(
        lambda match: f"{match.group(1)}.{int(match.group(2)) + int(match.group(3))} 秒",
        text,
    )
    # These clause starters are reliable punctuation cues in dictated
    # technical prose. Do not touch them at the beginning of a sentence.
    text = re.sub(
        r"(?<=[\u4e00-\u9fffA-Za-z0-9])(?=(?:松开后|按下后|录音结束后|识别完成后|停顿后))",
        "，",
        text,
    )
    text = re.sub(r"(刷入|完成|退出)(?=改为)", r"\1。", text)
    text = re.sub(r"(不是[^，。！？\n]{1,16}问题)(?=已(?:处理|修复|解决|完成))", r"\1。", text)
    text = re.sub(r"(自动补逗号)(?=结束时)", r"\1，", text)
    text = re.sub(r"(补句号)(?=修正)", r"\1。", text)
    text = strip_trailing_url_period(text)
    if sentence_final and text and text[-1] not in _PUNCTUATION and not _URL_AT_END_RE.search(text):
        text += "。"
    return text


def join_calibrated_segment(base: str, segment: str) -> str:
    segment = segment.strip()
    if not base:
        return segment
    if not segment:
        return base
    if base[-1] in _PUNCTUATION and segment[0] in _PUNCTUATION:
        segment = segment.lstrip("，,。.")
        return base if not segment else base + segment
    if base[-1] not in _PUNCTUATION and segment[0] not in _PUNCTUATION:
        return base + "，" + segment
    return _join_text(base, segment)


def novel_stream_suffix(existing: str, incoming: str, overlap_limit: int = 96) -> str:
    if not existing or not incoming:
        return incoming
    for overlap in range(min(len(existing), len(incoming), overlap_limit), 0, -1):
        if existing.endswith(incoming[:overlap]):
            return incoming[overlap:]
    return incoming


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    needs_space = left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum()
    return left + (" " if needs_space else "") + right


def _pcm16_rms(block: bytes) -> float:
    usable = len(block) - (len(block) % 2)
    if usable <= 0:
        return 0.0
    samples = struct.unpack(f"<{usable // 2}h", block[:usable])
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


class FunASRStreamingSession:
    """One StickS3 push-to-talk stream backed by a persistent FunASR socket."""

    def __init__(self, injector: PasteInjector) -> None:
        self.injector = injector
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._final_event = threading.Event()
        self._receiver: threading.Thread | None = None
        self._websocket: Any = None
        self._session_id = ""
        self._target_window = 0
        self._audio = bytearray()
        self._pending = bytearray()
        self._pre_roll: list[bytes] = []
        self._speech_started = False
        self._consecutive_speech = 0
        self._speech_blocks = 0
        self._committed_text = ""
        self._online_text = ""
        self._display_text = ""
        self._display_segment_start = 0
        self._locked_prefix_length = 0
        self._applied_text = ""
        self._dedupe_next_online = False
        self._dedupe_reference = ""
        self._normalizations: dict[str, str] = {}
        self._input_success = True
        self._error = ""
        self._stop_sent_at = 0.0
        self._last_post_stop_result_at = 0.0

    @property
    def active(self) -> bool:
        return bool(self._websocket and self._session_id and not self._stop_event.is_set())

    def start(self, session_id: str, *, connector=None) -> bool:  # noqa: ANN001
        self.abort(rollback=True)
        self._error = ""
        config = _load_asr_config()
        if config.get("provider") != "local-funasr" or not config.get("base_url"):
            self._error = "Streaming requires the local FunASR provider"
            return False
        if connector is None:
            try:
                from websockets.sync.client import connect as connector
            except ImportError:
                self._error = "The websockets package is unavailable"
                return False

        hotwords, self._normalizations = _load_funasr_hotwords()
        begin_session = getattr(self.injector, "begin_session", None)
        if begin_session:
            relay_start = begin_session(session_id)
            if not relay_start.success:
                self._error = relay_start.message
                return False
        try:
            websocket = connector(
                config["base_url"],
                subprotocols=["binary"],
                ping_interval=None,
                max_size=None,
                open_timeout=4,
                close_timeout=2,
            )
            websocket.send(
                json.dumps(
                    {
                        "mode": "2pass",
                        "chunk_size": [5, 10, 5],
                        "chunk_interval": 10,
                        "encoder_chunk_look_back": 4,
                        "decoder_chunk_look_back": 0,
                        "audio_fs": 16000,
                        "wav_name": f"sticks3-{session_id}",
                        "wav_format": "pcm",
                        "is_speaking": True,
                        "hotwords": json.dumps(hotwords, ensure_ascii=False) if hotwords else "",
                        "itn": True,
                    },
                    ensure_ascii=False,
                )
            )
        except (OSError, TimeoutError, ValueError) as exc:
            end_session = getattr(self.injector, "end_session", None)
            if end_session:
                end_session(session_id)
            self._error = f"Could not connect to FunASR: {exc}"
            return False

        with self._lock:
            self._websocket = websocket
            self._session_id = session_id
            self._target_window = self.injector.capture_target()
            self._stop_event.clear()
            self._final_event.clear()
            self._receiver = threading.Thread(
                target=self._receive_loop,
                name="vibestick-funasr-stream",
                daemon=True,
            )
            self._receiver.start()
        return True

    def append(self, pcm: bytes) -> bool:
        if not pcm or not self.active:
            return False
        with self._lock:
            self._audio.extend(pcm)
            self._pending.extend(pcm)
            blocks: list[bytes] = []
            while len(self._pending) >= FUNASR_AUDIO_CHUNK_BYTES:
                blocks.append(bytes(self._pending[:FUNASR_AUDIO_CHUNK_BYTES]))
                del self._pending[:FUNASR_AUDIO_CHUNK_BYTES]
        try:
            for block in blocks:
                self._feed_audio_block(block)
            return True
        except (OSError, TimeoutError, RuntimeError) as exc:
            self._error = f"FunASR audio send failed: {exc}"
            return False

    def stop(self) -> StreamingResult:
        websocket = self._websocket
        if not websocket:
            return StreamingResult("", bytes(self._audio), False, False, self._error or "Stream is inactive")

        try:
            with self._lock:
                tail = bytes(self._pending)
                self._pending.clear()
            if tail and self._speech_started:
                self._send(tail)
            with self._lock:
                self._stop_sent_at = time.monotonic()
                # FunASR can mark completed phrase segments final while the
                # speaker is still talking. Only a result after release should
                # satisfy the final-calibration wait.
                self._final_event.clear()
            self._send(json.dumps({"is_speaking": False}))
            self._wait_for_live_final()
        except (OSError, TimeoutError, RuntimeError) as exc:
            self._error = f"FunASR stop failed: {exc}"

        self._stop_event.set()
        try:
            websocket.close()
        except (OSError, RuntimeError):
            pass
        receiver = self._receiver
        if receiver and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)

        with self._lock:
            text = self._display_text or _join_text(self._committed_text, self._online_text)
            audio = bytes(self._audio)
            enough_speech = self._speech_blocks >= MINIMUM_SPEECH_BLOCKS
            input_success = self._input_success
        if enough_speech and text:
            format_edit = spoken_format_command(text)
            text = (
                format_edit
                if format_edit is not None
                else normalize_stream_text(text, self._normalizations, sentence_final=True)
            )
            if text != self._applied_text:
                final_edit = self._publish(text)
                input_success = input_success and final_edit
        if not enough_speech:
            if self._applied_text:
                removal = self.injector.edit(
                    len(self._applied_text),
                    "",
                    target_window=self._target_window,
                )
                input_success = input_success and removal.success
            text = ""
        success = bool(text and enough_speech and input_success)
        message = self._error or (
            "Streaming transcript entered" if success else "No clear streaming transcript"
        )
        result = StreamingResult(text, audio, success, input_success, message)
        end_session = getattr(self.injector, "end_session", None)
        if end_session:
            end_session(self._session_id)
        self._reset_runtime()
        return result

    def abort(self, *, rollback: bool = False) -> None:
        websocket = self._websocket
        session_id = self._session_id
        self._stop_event.set()
        if rollback and self._applied_text:
            self.injector.edit(
                len(self._applied_text),
                "",
                target_window=self._target_window,
            )
        if websocket:
            try:
                websocket.close()
            except (OSError, RuntimeError):
                pass
        receiver = self._receiver
        if receiver and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)
        end_session = getattr(self.injector, "end_session", None)
        if session_id and end_session:
            end_session(session_id)
        self._reset_runtime()

    def _feed_audio_block(self, block: bytes) -> None:
        level = _pcm16_rms(block)
        is_speech = level >= SPEECH_RMS_THRESHOLD
        if is_speech:
            self._speech_blocks += 1
        if self._speech_started:
            self._send(block)
            return

        self._pre_roll.append(block)
        if len(self._pre_roll) > PRE_ROLL_BLOCKS:
            del self._pre_roll[0]
        self._consecutive_speech = self._consecutive_speech + 1 if is_speech else 0
        if self._consecutive_speech < SPEECH_START_BLOCKS:
            return
        self._speech_started = True
        for ready_block in self._pre_roll:
            self._send(ready_block)
        self._pre_roll.clear()

    def _send(self, payload: object) -> None:
        websocket = self._websocket
        if not websocket:
            raise RuntimeError("Stream is inactive")
        with self._send_lock:
            websocket.send(payload)

    def _receive_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw = self._websocket.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception as exc:  # The websocket library has version-specific close exceptions.
                if not self._stop_event.is_set():
                    self._error = f"FunASR receive failed: {exc}"
                break
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            self._handle_result(payload)
            if payload.get("is_final"):
                self._final_event.set()

    def _handle_result(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        mode = str(payload.get("mode") or "")
        with self._lock:
            if self._stop_sent_at:
                self._last_post_stop_result_at = time.monotonic()
            if mode == "2pass-online":
                self._online_text += text
                starts_new_segment = self._dedupe_next_online
                visible_suffix = (
                    novel_stream_suffix(self._dedupe_reference, text)
                    if starts_new_segment
                    else text
                )
                if starts_new_segment and visible_suffix == text:
                    visible_suffix = novel_stream_suffix(self._display_text, text)
                self._dedupe_next_online = False
                self._dedupe_reference = ""
                if not visible_suffix:
                    return
                target = (
                    _join_text(self._display_text, visible_suffix)
                    if starts_new_segment
                    else self._display_text + visible_suffix
                )
                target = normalize_stream_text(target, self._normalizations)
                self._display_text = target
            else:
                previous_committed = self._committed_text
                self._committed_text = _merge_funasr_text(previous_committed, text)
                self._online_text = ""
                is_release_final = bool(self._stop_sent_at and payload.get("is_final"))
                if is_release_final:
                    corrected = normalize_stream_text(
                        self._committed_text,
                        self._normalizations,
                    )
                    target = locked_live_input_target(
                        self._applied_text,
                        corrected,
                        self._locked_prefix_length,
                    )
                    self._display_text = target
                else:
                    corrected_segment = committed_segment_delta(
                        previous_committed,
                        self._committed_text,
                        text,
                    )
                    if not corrected_segment:
                        return
                    target = join_calibrated_segment(
                        self._display_text[: self._display_segment_start],
                        corrected_segment,
                    )
                    target = normalize_stream_text(
                        target,
                        self._normalizations,
                    )
                    self._display_text = target
                    self._display_segment_start = len(target)
                    self._locked_prefix_length = len(target)
                    self._dedupe_next_online = True
                    self._dedupe_reference = text
        self._publish(target)

    def _publish(self, target: str) -> bool:
        delete_count, suffix = live_input_edit(self._applied_text, target)
        if not delete_count and not suffix:
            return True
        result = self.injector.edit(
            delete_count,
            suffix,
            target_window=self._target_window,
        )
        with self._lock:
            self._input_success = self._input_success and result.success
            if result.success:
                self._applied_text = target
            elif not self._error:
                self._error = result.message
        return result.success

    def _reset_runtime(self) -> None:
        self._stop_event.set()
        self._websocket = None
        self._receiver = None
        self._session_id = ""
        self._target_window = 0
        self._audio.clear()
        self._pending.clear()
        self._pre_roll.clear()
        self._speech_started = False
        self._consecutive_speech = 0
        self._speech_blocks = 0
        self._committed_text = ""
        self._online_text = ""
        self._display_text = ""
        self._display_segment_start = 0
        self._locked_prefix_length = 0
        self._applied_text = ""
        self._dedupe_next_online = False
        self._dedupe_reference = ""
        self._normalizations = {}
        self._input_success = True
        self._stop_sent_at = 0.0
        self._last_post_stop_result_at = 0.0

    def _wait_for_live_final(self) -> None:
        with self._lock:
            stop_sent_at = self._stop_sent_at
        deadline = stop_sent_at + _final_wait_seconds()
        minimum_finish = stop_sent_at + LIVE_FINAL_MIN_SECONDS
        while not self._final_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            with self._lock:
                last_result = self._last_post_stop_result_at
            if (
                now >= minimum_finish
                and last_result >= stop_sent_at
                and now - last_result >= LIVE_FINAL_QUIET_SECONDS
            ):
                return
            self._final_event.wait(timeout=min(0.04, max(0.0, deadline - now)))


def _final_wait_seconds() -> float:
    raw = os.environ.get("VIBE_STICK_STREAM_FINAL_WAIT_SECONDS", "0.55")
    try:
        value = float(raw)
    except ValueError:
        value = 0.55
    return max(LIVE_FINAL_MIN_SECONDS, min(3.0, value))
