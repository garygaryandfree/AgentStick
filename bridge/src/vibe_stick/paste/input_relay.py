from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from vibe_stick.paste.input_injector import PasteResult


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class InputRelayHub:
    """Thread-safe bridge between the Unraid service and one Windows client."""

    def __init__(self, token: str) -> None:
        self.token = token
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._connection: Any = None
        self._pending: dict[str, _PendingRequest] = {}
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._provider_status_handler: Callable[[dict[str, Any]], None] | None = None
        self._device_status_provider: Callable[[], list[dict[str, Any]]] | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connection is not None

    def start(self, host: str, port: int) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._serve,
            args=(host, port),
            name="vibestick-input-relay",
            daemon=True,
        )
        self._thread.start()

    def set_provider_status_handler(
        self,
        handler: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Accept authenticated task status reports from the Windows client."""

        with self._lock:
            self._provider_status_handler = handler

    def set_device_status_provider(
        self,
        provider: Callable[[], list[dict[str, Any]]] | None,
    ) -> None:
        """Expose current Lot device status to newly connected Windows clients."""

        with self._lock:
            self._device_status_provider = provider

    def _serve(self, host: str, port: int) -> None:
        from websockets.sync.server import serve

        with serve(
            self._handle_connection,
            host,
            port,
            ping_interval=20,
            ping_timeout=20,
            max_size=256_000,
        ) as server:
            self._server = server
            print(f"VibeStick input relay listening on ws://{host}:{port}", flush=True)
            server.serve_forever()

    def _handle_connection(self, connection: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            raw = connection.recv(timeout=5)
            hello = json.loads(raw) if isinstance(raw, str) else {}
            if (
                not isinstance(hello, dict)
                or hello.get("type") != "hello"
                or hello.get("role") != "windows"
                or str(hello.get("token") or "") != self.token
            ):
                connection.close(code=1008, reason="unauthorized")
                return
            with self._lock:
                previous = self._connection
                self._connection = connection
            if previous and previous is not connection:
                try:
                    previous.close(code=1012, reason="replaced")
                except OSError:
                    pass
            with self._lock:
                device_status_provider = self._device_status_provider
            devices = device_status_provider() if device_status_provider is not None else []
            connection.send(json.dumps({"type": "hello_ack", "ok": True, "devices": devices}))
            try:
                for message in connection:
                    if not isinstance(message, str):
                        continue
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "provider_status":
                        with self._lock:
                            handler = self._provider_status_handler
                        if handler is not None:
                            try:
                                handler(payload)
                            except Exception as exc:
                                print(
                                    f"Windows provider status rejected error={exc.__class__.__name__}",
                                    flush=True,
                                )
                        continue
                    if payload.get("type") != "ack":
                        continue
                    request_id = str(payload.get("request_id") or "")
                    with self._lock:
                        pending = self._pending.get(request_id)
                        if pending:
                            pending.response = payload
                            pending.event.set()
            except (ConnectionClosed, OSError):
                # Client restarts, sleep and network changes are routine. The
                # outer listener accepts the reconnect; don't emit a traceback.
                pass
        finally:
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                pending_requests = list(self._pending.values())
            for pending in pending_requests:
                pending.event.set()

    def request(self, event_type: str, *, timeout: float = 1.5, **payload: Any) -> PasteResult:
        request_id = uuid.uuid4().hex
        pending = _PendingRequest()
        with self._lock:
            connection = self._connection
            if connection is None:
                return PasteResult(False, "Windows input client is not connected")
            self._pending[request_id] = pending
        message = {"type": event_type, "request_id": request_id, **payload}
        try:
            with self._send_lock:
                connection.send(json.dumps(message, ensure_ascii=False))
        except (OSError, RuntimeError) as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            return PasteResult(False, f"Windows input relay send failed: {exc}")
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            return PasteResult(False, "Windows input client did not acknowledge the request")
        with self._lock:
            self._pending.pop(request_id, None)
        response = pending.response or {}
        return PasteResult(
            bool(response.get("success")),
            str(response.get("message") or "Windows input request failed"),
        )

    def send_event(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            connection = self._connection
        if connection is None:
            return False
        try:
            with self._send_lock:
                connection.send(json.dumps(payload, ensure_ascii=False))
            return True
        except (OSError, RuntimeError):
            return False


class RelayPasteInjector:
    def __init__(self, hub: InputRelayHub) -> None:
        self.hub = hub
        self.session_id = ""

    def begin_session(self, session_id: str) -> PasteResult:
        result = self.hub.request("session_start", session_id=session_id, timeout=2.0)
        if result.success:
            self.session_id = session_id
        return result

    def end_session(self, session_id: str = "") -> PasteResult:
        active_session = session_id or self.session_id
        if not active_session:
            return PasteResult(True, "No relay session was active")
        result = self.hub.request("session_end", session_id=active_session, timeout=1.0)
        if active_session == self.session_id:
            self.session_id = ""
        return result

    def capture_target(self) -> int:
        return 1 if self.session_id else 0

    def edit(self, delete_count: int, text: str, *, target_window: int = 0) -> PasteResult:
        del target_window
        if not self.session_id:
            return PasteResult(False, "No Windows relay session is active")
        return self.hub.request(
            "edit",
            session_id=self.session_id,
            delete_count=max(0, int(delete_count)),
            text=text,
        )

    def press_enter(self) -> PasteResult:
        return self.hub.request("press_enter")

    def paste(self, text: str, press_enter: bool = False) -> PasteResult:
        session_id = uuid.uuid4().hex
        started = self.begin_session(session_id)
        if not started.success:
            return started
        try:
            result = self.edit(0, text)
            if result.success and press_enter:
                result = self.press_enter()
            return result
        finally:
            self.end_session(session_id)
