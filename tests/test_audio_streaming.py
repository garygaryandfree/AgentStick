import json
import queue
import threading
import time
import unittest
from unittest import mock

from vibe_stick.audio.streaming import (
    FUNASR_AUDIO_CHUNK_BYTES,
    FunASRStreamingSession,
    join_calibrated_segment,
    locked_live_input_target,
    normalize_stream_text,
    spoken_format_command,
)
from vibe_stick.paste.input_injector import PasteResult


class _FakeInjector:
    def __init__(self) -> None:
        self.text = ""
        self.target = 77

    def capture_target(self) -> int:
        return self.target

    def edit(self, delete_count: int, text: str, *, target_window: int = 0) -> PasteResult:
        if target_window != self.target:
            return PasteResult(False, "wrong target")
        if delete_count:
            self.text = self.text[:-delete_count]
        self.text += text
        return PasteResult(True, "ok")


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.messages: queue.Queue[object] = queue.Queue()
        self.closed = threading.Event()

    def send(self, payload: object) -> None:
        self.sent.append(payload)
        if payload == '{"is_speaking": false}':
            self.messages.put(
                json.dumps(
                    {"mode": "2pass-offline", "text": "linux GitHub", "is_final": True}
                )
            )

    def recv(self, timeout: float | None = None):  # noqa: ANN201
        try:
            payload = self.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if payload is None:
            raise RuntimeError("closed")
        return payload

    def close(self) -> None:
        self.closed.set()
        self.messages.put(None)


class AudioStreamingTests(unittest.TestCase):
    def test_pause_segments_gain_punctuation_without_duplicate_commas(self) -> None:
        first = join_calibrated_segment("第一段完成", "第二段开始")
        second = join_calibrated_segment(first + "，", "，第三段开始")

        self.assertEqual(first, "第一段完成，第二段开始")
        self.assertEqual(second, "第一段完成，第二段开始，第三段开始")

    def test_spoken_decimal_time_error_is_repaired_in_recording_context(self) -> None:
        text = normalize_stream_text(
            "21:20:04长录音重放测试松开后退格数为零",
            {},
            sentence_final=True,
        )

        self.assertEqual(text, "21.24 秒长录音重放测试，松开后退格数为零。")

    def test_real_sticks3_segments_restore_words_and_punctuation(self) -> None:
        normalizations = {
            "推格": "退格",
            "分段小整": "分段校正",
            "停顿，后端落": "停顿后的段落",
            "后端落": "段落",
        }
        raw_segments = [
            "21:20:04长录音重放测试松开后推格数为零",
            "，两个问题都已修复并重新刷入",
            "，改为稳定分段小整停顿，后端落会锁定",
        ]

        result = ""
        for raw_segment in raw_segments:
            segment = normalize_stream_text(raw_segment, normalizations)
            result = join_calibrated_segment(result, segment)
        result = normalize_stream_text(result, normalizations, sentence_final=True)

        self.assertEqual(
            result,
            "21.24 秒长录音重放测试，松开后退格数为零，"
            "两个问题都已修复并重新刷入，改为稳定分段校正，停顿后的段落会锁定。",
        )

    def test_real_long_sticks3_technical_errors_are_repaired_in_context(self) -> None:
        raw = (
            "已经修复并重新刷写s三，现在已经退出聆听界面，并恢复在线，"
            "更音是按键松开事件，偶发丢失设备一直上传音频，现在加入了"
            "按键松开，时间正常停止直接读取gpio状态作为兜底松开80ms后自动停止"
            "设备录音最长44秒，防止无线卡住，greatwall断流4s动收回绘画"
            "最长保护50秒事件队列满十记录错误并允许重试"
        )

        result = normalize_stream_text(raw, {}, sentence_final=True)

        self.assertIn("S3", result)
        self.assertIn("根因是按键松开事件", result)
        self.assertIn("按键松开事件正常停止", result)
        self.assertIn("GPIO状态", result)
        self.assertIn("80 ms后自动停止", result)
        self.assertIn("防止无限卡住", result)
        self.assertIn("Gateway断流4 秒自动回收会话", result)
        self.assertIn("事件队列满时记录错误", result)
        self.assertTrue(result.endswith("。"))

    def test_sentence_final_does_not_add_period_after_url(self) -> None:
        self.assertEqual(
            normalize_stream_text("请打开fuzhuniu.net", {}, sentence_final=True),
            "请打开fuzhuniu.net",
        )

    def test_existing_period_is_removed_after_url(self) -> None:
        self.assertEqual(
            normalize_stream_text("请打开fuzhuniu.net。", {}, sentence_final=True),
            "请打开fuzhuniu.net",
        )

    def test_standalone_spoken_newline_becomes_real_newline(self) -> None:
        self.assertEqual(spoken_format_command("换行。"), "\n")
        self.assertEqual(spoken_format_command("另起一行"), "\n")
        self.assertIsNone(spoken_format_command("这里讨论换行规则"))

    def test_technical_prose_gains_reliable_clause_boundaries(self) -> None:
        self.assertEqual(
            normalize_stream_text(
                "不是发音问题已处理根据语音停顿自动补逗号结束时补句号修正退格",
                {},
                sentence_final=True,
            ),
            "不是发音问题。已处理根据语音停顿自动补逗号，结束时补句号。修正退格。",
        )

    def test_explicit_spoken_symbols_are_deterministic(self) -> None:
        self.assertEqual(
            normalize_stream_text("21冒号20冒号04长录音箭头21.24秒", {}, sentence_final=True),
            "21.24 秒长录音→21.24秒。",
        )

    def test_final_calibration_cannot_rewrite_locked_prefix(self) -> None:
        previous = "第一段已经校准。当前尾段有错字"
        corrected = "第一段被离线模型改坏。当前尾段已修正"
        locked_length = len("第一段已经校准。")

        target = locked_live_input_target(previous, corrected, locked_length)

        self.assertTrue(target.startswith("第一段已经校准。"))
        self.assertNotIn("第一段被离线模型改坏", target)

    def test_sticks3_audio_is_entered_before_stream_stop(self) -> None:
        injector = _FakeInjector()
        socket = _FakeSocket()
        session = FunASRStreamingSession(injector)  # type: ignore[arg-type]

        with mock.patch(
            "vibe_stick.audio.streaming._load_asr_config",
            return_value={"provider": "local-funasr", "base_url": "ws://funasr.test:10095"},
        ), mock.patch(
            "vibe_stick.audio.streaming._load_funasr_hotwords",
            return_value=({}, {"linux": "Linux"}),
        ):
            self.assertTrue(session.start("session-1", connector=lambda *_args, **_kwargs: socket))

        loud_block = (2000).to_bytes(2, "little", signed=True) * (FUNASR_AUDIO_CHUNK_BYTES // 2)
        self.assertTrue(session.append(loud_block * 4))
        socket.messages.put(json.dumps({"mode": "2pass-online", "text": "linux"}))
        socket.messages.put(
            json.dumps(
                {"mode": "2pass-offline", "text": "linux GitHub", "is_final": True}
            )
        )

        deadline = time.monotonic() + 1.0
        while injector.text != "Linux GitHub" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(injector.text, "Linux GitHub")

        result = session.stop()
        self.assertTrue(result.success)
        self.assertTrue(result.input_success)
        self.assertEqual(result.text, "Linux GitHub。")
        self.assertEqual(injector.text, "Linux GitHub。")
        self.assertTrue(any(isinstance(payload, bytes) for payload in socket.sent))
        self.assertEqual(socket.sent[-1], '{"is_speaking": false}')


if __name__ == "__main__":
    unittest.main()
