import json
import socket
import threading
import time
import unittest

from websockets.sync.client import connect

from vibe_stick.paste.input_relay import InputRelayHub, RelayPasteInjector


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class InputRelayTests(unittest.TestCase):
    def test_authenticated_windows_client_can_report_provider_status(self) -> None:
        port = _free_port()
        hub = InputRelayHub("test-token")
        received: list[dict] = []
        reported = threading.Event()

        def handler(payload: dict) -> None:
            received.append(payload)
            reported.set()

        hub.set_provider_status_handler(handler)
        hub.start("127.0.0.1", port)
        deadline = time.monotonic() + 2
        while True:
            try:
                connection = connect(f"ws://127.0.0.1:{port}", open_timeout=0.2)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        with connection:
            connection.send(json.dumps({"type": "hello", "role": "windows", "token": "test-token"}))
            self.assertTrue(json.loads(connection.recv())["ok"])
            connection.send(
                json.dumps(
                    {
                        "type": "provider_status",
                        "provider": "codex",
                        "status": "DONE",
                        "alert_type": "DONE",
                        "alert_event_id": "evt-1",
                    }
                )
            )
            self.assertTrue(reported.wait(2))

        self.assertEqual(received[0]["alert_event_id"], "evt-1")

    def test_windows_client_acknowledges_session_edits(self) -> None:
        port = _free_port()
        hub = InputRelayHub("test-token")
        hub.start("127.0.0.1", port)
        received: list[dict] = []
        ready = threading.Event()

        def client() -> None:
            deadline = time.monotonic() + 2
            while True:
                try:
                    connection = connect(f"ws://127.0.0.1:{port}", open_timeout=0.2)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            with connection:
                connection.send(
                    json.dumps(
                        {"type": "hello", "role": "windows", "token": "test-token"}
                    )
                )
                hello = json.loads(connection.recv())
                self.assertTrue(hello["ok"])
                ready.set()
                for raw in connection:
                    event = json.loads(raw)
                    received.append(event)
                    connection.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "request_id": event["request_id"],
                                "success": True,
                                "message": "ok",
                            }
                        )
                    )
                    if event["type"] == "session_end":
                        return

        worker = threading.Thread(target=client, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(2))

        injector = RelayPasteInjector(hub)
        self.assertTrue(injector.begin_session("session-1").success)
        self.assertTrue(injector.edit(0, "你好").success)
        self.assertTrue(injector.edit(1, "们").success)
        self.assertTrue(injector.press_enter().success)
        self.assertTrue(injector.end_session().success)
        worker.join(timeout=2)

        self.assertEqual(
            [event["type"] for event in received],
            ["session_start", "edit", "edit", "press_enter", "session_end"],
        )


if __name__ == "__main__":
    unittest.main()
