from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from vibe_stick import __version__ as BRIDGE_VERSION
from vibe_stick.audio.recorder import RecordingController
from vibe_stick.claude.usage import fetch_usage as fetch_claude_usage
from vibe_stick.claude.usage import to_quota_snapshot as claude_usage_to_quota
from vibe_stick.codex.quota import QuotaSnapshot, load_quota, save_quota
from vibe_stick.config.paths import CLAUDE_QUOTA_PATH, QUOTA_PATH, RECORDING_PATH, STATE_PATH, ensure_app_support
from vibe_stick.desktop.hud import hide_hud
from vibe_stick.protocol.state import (
    AlertState,
    AlertType,
    VibeStickState,
    AgentStatus,
    CodexState,
    ProviderState,
    default_state,
    event_id,
    now_time_text,
    state_from_dict,
)
from vibe_stick.providers.base import ProviderObservation
from vibe_stick.providers.claude import observe_claude
from vibe_stick.providers.codex import observe_codex
from vibe_stick.paste.input_relay import InputRelayHub, RelayPasteInjector
from vibe_stick.usage.sub2 import DEFAULT_BASE_URL as SUB2_DEFAULT_BASE_URL
from vibe_stick.usage.sub2 import Sub2UsageSnapshot, fetch_sub2_usage

MANUAL_STATUS_SECONDS = 60
BRIDGE_NAME = "vibestick-bridge"
DEFAULT_MAX_RECORDING_AUDIO_BYTES = 2_000_000
DEFAULT_CLAUDE_USAGE_INTERVAL_SECONDS = 300
MIN_CLAUDE_USAGE_INTERVAL_SECONDS = 30
DEFAULT_RECORDING_IDLE_TIMEOUT_SECONDS = 4.0
DEFAULT_RECORDING_MAX_SECONDS = 125.0
DEFAULT_SUB2_USAGE_INTERVAL_SECONDS = 60
MIN_SUB2_USAGE_INTERVAL_SECONDS = 30
DEFAULT_SUB2_CODEX_ACCOUNT_ID = 52
DEFAULT_SUB2_CLAUDE_ACCOUNT_ID = 29
REMOTE_PROVIDER_STATUS_TTL_SECONDS = 30.0
LOT_DEVICE_TTL_SECONDS = 10.0
LOT_DEVICE_WATCHDOG_INTERVAL_SECONDS = 1.0
LOT_DEVICE_STATUS_TYPE = "device_status"
PLACEHOLDER_BRIDGE_TOKENS = {
    "change-this-shared-token",
    "paste-generated-token-here",
    "changeme",
    "change-me",
}


class BridgeStateStore:
    def __init__(self, relay_hub: InputRelayHub | None = None) -> None:
        ensure_app_support()
        self._lock = threading.RLock()
        self._recording_lock = threading.RLock()
        self._project_root = _resolve_project_root()
        self._manual_status_until = 0.0
        self._state = self._load_state()
        self._last_active_provider = self._state.active_provider or "codex"
        self._claude_quota = load_quota(CLAUDE_QUOTA_PATH)
        if not _has_quota(self._claude_quota):
            self._claude_quota = _claude_quota_from_state(self._state)
        self._claude_usage_last_attempt = 0.0
        self._claude_usage_last_success = 0.0
        self._sub2_usage_token = os.environ.get("VIBE_STICK_SUB2_USAGE_TOKEN", "").strip()
        self._sub2_usage_base_url = (
            os.environ.get("VIBE_STICK_SUB2_USAGE_BASE_URL", "").strip() or SUB2_DEFAULT_BASE_URL
        )
        self._sub2_usage_interval = _sub2_usage_interval_seconds()
        self._sub2_usage_accounts = {
            "codex": _sub2_account_id("VIBE_STICK_SUB2_CODEX_ACCOUNT_ID", DEFAULT_SUB2_CODEX_ACCOUNT_ID),
            "claude": _sub2_account_id("VIBE_STICK_SUB2_CLAUDE_ACCOUNT_ID", DEFAULT_SUB2_CLAUDE_ACCOUNT_ID),
        }
        self._sub2_usage_snapshots: dict[str, Sub2UsageSnapshot] = {}
        self._sub2_usage_snapshot_times: dict[str, float] = {}
        self._sub2_usage_refresh_event = threading.Event()
        self._remote_provider_observations: dict[str, ProviderObservation] = {}
        self._remote_provider_seen_at: dict[str, float] = {}
        self._relay_hub = relay_hub
        self._lot_devices: dict[str, dict[str, Any]] = {}
        self._lot_device_last_signature = ""
        quota = load_quota(QUOTA_PATH)
        self._state.codex.quota_5h_remaining = quota.quota_5h_remaining
        self._state.codex.quota_7d_remaining = quota.quota_7d_remaining
        self._state.codex.quota_updated_at = quota.quota_updated_at
        self._state.codex.quota_stale = quota.quota_stale
        relay_injector = RelayPasteInjector(relay_hub) if relay_hub else None
        self.recording = RecordingController(RECORDING_PATH, paste_injector=relay_injector)
        if self.recording.session.active:
            self.recording.abort_stale_stream("Gateway restarted during an active recording")
        self._recording_started_monotonic = 0.0
        self._recording_last_activity_monotonic = 0.0
        self._recording_idle_timeout_seconds = _positive_float_env(
            "VIBE_STICK_RECORDING_IDLE_TIMEOUT_SECONDS",
            DEFAULT_RECORDING_IDLE_TIMEOUT_SECONDS,
            minimum=2.0,
            maximum=30.0,
        )
        self._recording_max_seconds = _positive_float_env(
            "VIBE_STICK_RECORDING_MAX_SECONDS",
            DEFAULT_RECORDING_MAX_SECONDS,
            minimum=10.0,
            maximum=300.0,
        )
        threading.Thread(
            target=self._recording_watchdog_loop,
            name="vibestick-recording-watchdog",
            daemon=True,
        ).start()
        if self._sub2_usage_token:
            threading.Thread(
                target=self._sub2_usage_loop,
                name="vibestick-sub2-usage",
                daemon=True,
            ).start()
        if relay_hub is not None:
            relay_hub.set_provider_status_handler(self.update_provider_status)
            relay_hub.set_device_status_provider(self.current_lot_devices)
            threading.Thread(
                target=self._lot_device_watchdog_loop,
                name="vibestick-lot-device-watchdog",
                daemon=True,
            ).start()
        hide_hud()

    def get_state(self) -> VibeStickState:
        with self._lock:
            self._refresh_providers_locked()
            self._state.time = now_time_text()
            self._save_state_locked()
            return self._state

    def update_from_event(self, event: dict[str, Any]) -> VibeStickState:
        with self._lock:
            event_name = str(event.get("event") or "")
            requested_status = event.get("codex_status") or event.get("status")
            if requested_status:
                self._set_codex_status(str(requested_status), str(event.get("message") or ""))
                self._manual_status_until = time.monotonic() + MANUAL_STATUS_SECONDS
            elif event_name == "button_double":
                self.refresh_quota_locked()
            elif event_name == "button_short":
                self.recording.paste_injector.press_enter()
                self._state.alert = AlertState(event_id="", type=AlertType.NONE, message="")
            self._save_state_locked()
            return self._state

    def refresh_quota(self) -> VibeStickState:
        with self._lock:
            self.refresh_quota_locked()
            self._save_state_locked()
            return self._state

    def refresh_quota_locked(self) -> None:
        if self._sub2_usage_token:
            self._sub2_usage_refresh_event.set()
        if self._state.active_provider == "claude":
            self._refresh_claude_usage_locked(force=True)
            self._state.provider = _provider_state_from_observation(
                self._apply_claude_quota(observe_claude(self._project_root))
            )
            return

        codex_observation = observe_codex(self._project_root)
        self._apply_codex_quota(codex_observation, force_stale=True)
        self._state.codex = _codex_state_from_observation(codex_observation)
        if self._state.active_provider == "codex":
            self._state.provider = _provider_state_from_observation(codex_observation)

    def update_provider_status(self, payload: dict[str, Any]) -> None:
        provider_id = str(payload.get("provider") or "").strip().lower()
        if provider_id not in {"codex", "claude"}:
            raise ValueError("Unsupported provider status")
        try:
            status = AgentStatus(str(payload.get("status") or "UNKNOWN").upper())
        except ValueError:
            status = AgentStatus.UNKNOWN
        alert_type = str(payload.get("alert_type") or "NONE").upper()
        if alert_type not in {item.value for item in AlertType}:
            alert_type = AlertType.NONE.value
        observation = ProviderObservation(
            provider_id=provider_id,
            display_name="Codex" if provider_id == "codex" else "Claude",
            online=True,
            status=status,
            project=str(payload.get("project") or "vibestick")[:128],
            quota_5h_remaining=None,
            quota_7d_remaining=None,
            quota_updated_at="",
            quota_stale=False,
            alert_type=alert_type,
            alert_message=str(payload.get("alert_message") or "")[:512],
            alert_event_id=str(payload.get("alert_event_id") or "")[:160],
        )
        with self._lock:
            self._remote_provider_observations[provider_id] = observation
            self._remote_provider_seen_at[provider_id] = time.monotonic()

    def mark_lot_device_seen(self, headers: Any, remote_addr: str = "") -> None:
        firmware_name = str(headers.get("X-Vibe-Stick-Firmware-Name", "") or "").strip()
        firmware_version = str(headers.get("X-Vibe-Stick-Firmware-Version", "") or "").strip()
        transport = str(headers.get("X-Vibe-Stick-Firmware-Transport", "") or "").strip()
        if not firmware_name and not firmware_version and not transport:
            return
        device_id = _lot_device_id(firmware_name, remote_addr)
        display_name = _lot_device_display_name(firmware_name)
        now = time.monotonic()
        with self._lock:
            self._lot_devices[device_id] = {
                "id": device_id,
                "name": display_name,
                "firmware_name": firmware_name,
                "firmware_version": firmware_version,
                "transport": transport,
                "remote_addr": remote_addr,
                "online": True,
                "last_seen": now,
            }
        self._publish_lot_device_status_if_changed()

    def current_lot_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._online_lot_devices_locked(time.monotonic())

    def _lot_device_watchdog_loop(self) -> None:
        while True:
            time.sleep(LOT_DEVICE_WATCHDOG_INTERVAL_SECONDS)
            self._publish_lot_device_status_if_changed()

    def _online_lot_devices_locked(self, now: float) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        stale_ids: list[str] = []
        for device_id, device in self._lot_devices.items():
            last_seen = float(device.get("last_seen") or 0.0)
            if now - last_seen <= LOT_DEVICE_TTL_SECONDS:
                devices.append(
                    {
                        "id": device_id,
                        "name": str(device.get("name") or "Stick S3"),
                        "online": True,
                        "firmware_name": str(device.get("firmware_name") or ""),
                        "firmware_version": str(device.get("firmware_version") or ""),
                        "transport": str(device.get("transport") or ""),
                        "last_seen_age_seconds": round(max(0.0, now - last_seen), 2),
                    }
                )
            else:
                stale_ids.append(device_id)
        for device_id in stale_ids:
            self._lot_devices.pop(device_id, None)
        devices.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
        return devices

    def _publish_lot_device_status_if_changed(self) -> None:
        relay_hub = self._relay_hub
        if relay_hub is None:
            return
        with self._lock:
            devices = self._online_lot_devices_locked(time.monotonic())
            signature = json.dumps(
                [(device.get("id"), device.get("name")) for device in devices],
                ensure_ascii=False,
                sort_keys=True,
            )
            if signature == self._lot_device_last_signature:
                return
            self._lot_device_last_signature = signature
        relay_hub.send_event({"type": LOT_DEVICE_STATUS_TYPE, "devices": devices})

    def start_recording(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._recording_lock:
            session = self.recording.start(request)
            now = time.monotonic()
            if session.active and session.audio_source == "sticks3_pcm_stream":
                self._recording_started_monotonic = now
                self._recording_last_activity_monotonic = now
            else:
                self._clear_recording_watchdog_locked()
        with self._lock:
            self._state.alert = AlertState(
                event_id="",
                type=AlertType.NONE,
                message="",
            )
            self._save_state_locked()
        # Provider state is polled separately by the StickS3. Keeping it off
        # the start path lets the first audio frame leave immediately.
        return {"recording": session.to_jsonable()}

    def stop_recording(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._recording_lock:
            session = self.recording.stop(request)
            self._clear_recording_watchdog_locked()
        # Streaming text has already been injected; provider observation here
        # only makes button release feel slower.
        return {"recording": session.to_jsonable()}

    def upload_recording_audio(
        self,
        pcm: bytes,
        *,
        session_id: str = "",
        sample_rate: int = 16000,
        channels: int = 1,
        bits_per_sample: int = 16,
    ) -> dict[str, Any]:
        with self._recording_lock:
            session = self.recording.attach_pcm(
                pcm,
                session_id=session_id,
                sample_rate=sample_rate,
                channels=channels,
                bits_per_sample=bits_per_sample,
            )
            self._recording_last_activity_monotonic = time.monotonic()
        return {"recording": session.to_jsonable()}

    def upload_recording_chunk(self, pcm: bytes, *, session_id: str = "") -> dict[str, Any]:
        with self._recording_lock:
            session = self.recording.attach_pcm_chunk(pcm, session_id=session_id)
            if session.active and session.status == "recording":
                self._recording_last_activity_monotonic = time.monotonic()
        # This endpoint runs several times per second. Provider observation is
        # unrelated to audio ingestion and can add enough latency to break the
        # live-input cadence, so return only the recording acknowledgement.
        return {"recording": session.to_jsonable()}

    def _clear_recording_watchdog_locked(self) -> None:
        self._recording_started_monotonic = 0.0
        self._recording_last_activity_monotonic = 0.0

    def _recording_watchdog_loop(self) -> None:
        while True:
            time.sleep(0.5)
            with self._recording_lock:
                session = self.recording.session
                if not session.active or session.audio_source != "sticks3_pcm_stream":
                    continue
                now = time.monotonic()
                idle_seconds = now - self._recording_last_activity_monotonic
                age_seconds = now - self._recording_started_monotonic
                if self._recording_started_monotonic <= 0:
                    continue
                if age_seconds >= self._recording_max_seconds:
                    reason = f"Recording exceeded {self._recording_max_seconds:.0f} seconds"
                elif idle_seconds >= self._recording_idle_timeout_seconds:
                    reason = f"StickS3 audio stopped for {idle_seconds:.1f} seconds"
                else:
                    continue
                print(f"recording watchdog abort session={session.session_id} reason={reason}", flush=True)
                self.recording.abort_stale_stream(reason)
                self._clear_recording_watchdog_locked()

    def _sub2_usage_loop(self) -> None:
        while True:
            refreshed: dict[str, Sub2UsageSnapshot] = {}
            for provider_id, account_id in self._sub2_usage_accounts.items():
                try:
                    refreshed[provider_id] = fetch_sub2_usage(
                        provider_id,
                        account_id,
                        self._sub2_usage_token,
                        base_url=self._sub2_usage_base_url,
                        timeout=10.0,
                    )
                except Exception as exc:
                    print(
                        f"Sub2-Usage refresh failed provider={provider_id} error={exc.__class__.__name__}",
                        flush=True,
                    )
            if refreshed:
                refreshed_at = time.monotonic()
                with self._lock:
                    self._sub2_usage_snapshots.update(refreshed)
                    for provider_id in refreshed:
                        self._sub2_usage_snapshot_times[provider_id] = refreshed_at
            self._sub2_usage_refresh_event.wait(self._sub2_usage_interval)
            self._sub2_usage_refresh_event.clear()

    def _refresh_providers_locked(self) -> None:
        codex_observation = observe_codex(self._project_root)
        claude_observation = observe_claude(self._project_root)
        codex_observation = self._remote_provider_observation("codex", codex_observation)
        claude_observation = self._remote_provider_observation("claude", claude_observation)
        self._apply_codex_quota(codex_observation)

        if time.monotonic() < self._manual_status_until:
            _apply_manual_codex_state(codex_observation, self._state)

        active_provider = _select_active_provider(
            _configured_provider(),
            self._last_active_provider,
            codex_observation,
            claude_observation,
        )
        self._last_active_provider = active_provider
        self._state.active_provider = active_provider

        if active_provider == "claude":
            self._refresh_claude_usage_locked(force=False)
        claude_observation = self._apply_claude_quota(claude_observation)
        self._apply_sub2_usage(codex_observation)
        self._apply_sub2_usage(claude_observation)
        observations = {
            "codex": codex_observation,
            "claude": claude_observation,
        }
        active_observation = observations[active_provider]

        self._state.codex = _codex_state_from_observation(codex_observation)
        self._state.provider = _provider_state_from_observation(active_observation)
        self._state.providers = {
            provider_id: _provider_state_from_observation(observation)
            for provider_id, observation in observations.items()
        }
        self._apply_alert_from_observation(
            _select_alert_observation(active_observation, codex_observation, claude_observation)
        )

    def _remote_provider_observation(
        self,
        provider_id: str,
        fallback: ProviderObservation,
    ) -> ProviderObservation:
        seen_at = self._remote_provider_seen_at.get(provider_id, 0.0)
        observation = self._remote_provider_observations.get(provider_id)
        if (
            observation is None
            or seen_at <= 0
            or time.monotonic() - seen_at > REMOTE_PROVIDER_STATUS_TTL_SECONDS
        ):
            return fallback
        return observation

    def _apply_alert_from_observation(self, observation: ProviderObservation) -> None:
        try:
            alert_type = AlertType(observation.alert_type)
        except ValueError:
            alert_type = AlertType.NONE
        if alert_type in {AlertType.DONE, AlertType.APPROVAL, AlertType.ERROR} and observation.alert_event_id:
            self._state.alert = AlertState(
                event_id=observation.alert_event_id,
                type=alert_type,
                message=observation.alert_message,
            )
        else:
            self._state.alert = AlertState(event_id="", type=AlertType.NONE, message="")

    def _apply_codex_quota(self, observation: ProviderObservation, *, force_stale: bool = False) -> None:
        if observation.quota_5h_remaining is not None or observation.quota_7d_remaining is not None:
            refreshed = QuotaSnapshot(
                quota_5h_remaining=observation.quota_5h_remaining,
                quota_7d_remaining=observation.quota_7d_remaining,
                quota_updated_at=observation.quota_updated_at,
                quota_stale=observation.quota_stale,
            )
            save_quota(QUOTA_PATH, refreshed)
        else:
            existing = QuotaSnapshot(
                quota_5h_remaining=self._state.codex.quota_5h_remaining,
                quota_7d_remaining=self._state.codex.quota_7d_remaining,
                quota_updated_at=self._state.codex.quota_updated_at,
                quota_stale=self._state.codex.quota_stale,
            )
            if existing.quota_5h_remaining is None and existing.quota_7d_remaining is None:
                refreshed = existing
            else:
                refreshed = _stale_quota(existing)
            if force_stale:
                save_quota(QUOTA_PATH, refreshed)

        observation.quota_5h_remaining = refreshed.quota_5h_remaining
        observation.quota_7d_remaining = refreshed.quota_7d_remaining
        observation.quota_updated_at = refreshed.quota_updated_at
        observation.quota_stale = refreshed.quota_stale

    def _refresh_claude_usage_locked(self, *, force: bool) -> None:
        now = time.monotonic()
        interval = _claude_usage_interval_seconds()
        if not force and now - self._claude_usage_last_attempt < interval:
            return
        self._claude_usage_last_attempt = now

        usage = fetch_claude_usage()
        if usage is None:
            if _has_quota(self._claude_quota):
                self._claude_quota = _stale_quota(self._claude_quota)
                save_quota(CLAUDE_QUOTA_PATH, self._claude_quota)
            else:
                self._claude_quota = QuotaSnapshot()
            return

        self._claude_quota = claude_usage_to_quota(usage)
        save_quota(CLAUDE_QUOTA_PATH, self._claude_quota)
        self._claude_usage_last_success = now

    def _apply_claude_quota(self, observation: ProviderObservation) -> ProviderObservation:
        quota = self._current_claude_quota()
        observation.quota_5h_remaining = quota.quota_5h_remaining
        observation.quota_7d_remaining = quota.quota_7d_remaining
        observation.quota_updated_at = quota.quota_updated_at
        observation.quota_stale = quota.quota_stale
        return observation

    def _apply_sub2_usage(self, observation: ProviderObservation) -> None:
        snapshot = self._sub2_usage_snapshots.get(observation.provider_id)
        if snapshot is None:
            return
        observation.account_name = snapshot.account_name or f"{snapshot.account_id}#"
        observation.quota_5h_remaining = snapshot.quota_5h_remaining
        observation.quota_7d_remaining = snapshot.quota_7d_remaining
        observation.quota_updated_at = snapshot.quota_updated_at
        refreshed_at = self._sub2_usage_snapshot_times.get(observation.provider_id, 0.0)
        observation.quota_stale = (
            refreshed_at <= 0
            or time.monotonic() - refreshed_at > self._sub2_usage_interval * 3
        )

    def _current_claude_quota(self) -> QuotaSnapshot:
        if (
            self._claude_quota.quota_5h_remaining is None
            and self._claude_quota.quota_7d_remaining is None
        ):
            return self._claude_quota
        if self._claude_usage_last_success and time.monotonic() - self._claude_usage_last_success > 30 * 60:
            return _stale_quota(self._claude_quota)
        return self._claude_quota

    def _set_codex_status(self, raw_status: str, message: str) -> None:
        try:
            status = AgentStatus(raw_status.upper())
        except ValueError:
            status = AgentStatus.UNKNOWN
        self._state.codex.status = status
        if self._state.active_provider == "codex":
            self._state.provider.status = status
        if status == AgentStatus.DONE:
            self._state.alert = AlertState(event_id("done"), AlertType.DONE, message or "Codex task completed")
        elif status == AgentStatus.APPROVAL:
            self._state.alert = AlertState(
                event_id("approval"),
                AlertType.APPROVAL,
                message or "Codex is waiting for approval",
            )
        elif status == AgentStatus.ERROR:
            self._state.alert = AlertState(event_id("error"), AlertType.ERROR, message or "Codex needs attention")
        else:
            self._state.alert = AlertState(event_id="", type=AlertType.NONE, message="")

    def _load_state(self) -> VibeStickState:
        try:
            return state_from_dict(json.loads(STATE_PATH.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return default_state()

    def _save_state_locked(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self._state.to_jsonable(), indent=2) + "\n")


def make_handler(store: BridgeStateStore) -> type[BaseHTTPRequestHandler]:
    class VibeStickHandler(BaseHTTPRequestHandler):
        server_version = "VibeStick/0.1"

        def do_GET(self) -> None:
            if self.path == "/state":
                store.mark_lot_device_seen(self.headers, str(self.client_address[0]))
                self._send_json(_with_bridge_metadata(store.get_state().to_jsonable()))
            elif self.path == "/health":
                self._send_json(
                    {
                        "ok": True,
                        "bridge_name": BRIDGE_NAME,
                        "bridge_version": BRIDGE_VERSION,
                    }
                )
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in _protected_paths() and not self._is_authorized():
                self._send_error(HTTPStatus.UNAUTHORIZED, "Unauthorized")
                return
            if parsed.path in _protected_paths():
                store.mark_lot_device_seen(self.headers, str(self.client_address[0]))

            if parsed.path == "/event":
                body = self._read_json_body()
                self._send_json(store.update_from_event(body).to_jsonable())
            elif parsed.path == "/quota/refresh":
                state = store.refresh_quota()
                self._send_json({"refreshed": True, "state": state.to_jsonable()})
            elif parsed.path == "/recording/start":
                body = self._read_json_body()
                self._send_json(store.start_recording(body))
            elif parsed.path == "/recording/audio":
                query = parse_qs(parsed.query)
                content_length = self._content_length()
                max_audio_bytes = _max_recording_audio_bytes()
                if content_length > max_audio_bytes:
                    self._send_error(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"Recording audio exceeds {max_audio_bytes} bytes",
                    )
                    return
                pcm = self._read_raw_body(content_length)
                self._send_json(
                    store.upload_recording_audio(
                        pcm,
                        session_id=_first(query, "session_id"),
                        sample_rate=_int_header(self.headers.get("X-Vibe-Stick-Sample-Rate"), 16000),
                        channels=_int_header(self.headers.get("X-Vibe-Stick-Channels"), 1),
                        bits_per_sample=_int_header(self.headers.get("X-Vibe-Stick-Bits-Per-Sample"), 16),
                    )
                )
            elif parsed.path == "/recording/chunk":
                query = parse_qs(parsed.query)
                content_length = self._content_length()
                if content_length > 32_000:
                    self._send_error(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        "Streaming audio chunk exceeds 32000 bytes",
                    )
                    return
                pcm = self._read_raw_body(content_length)
                self._send_json(
                    store.upload_recording_chunk(
                        pcm,
                        session_id=_first(query, "session_id"),
                    )
                )
            elif parsed.path == "/recording/stop":
                body = self._read_json_body()
                self._send_json(store.stop_recording(body))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        def log_message(self, fmt: str, *args: object) -> None:
            firmware_name = self.headers.get("X-Vibe-Stick-Firmware-Name", "-")
            firmware_version = self.headers.get("X-Vibe-Stick-Firmware-Version", "-")
            firmware_transport = self.headers.get("X-Vibe-Stick-Firmware-Transport", "-")
            print(
                f"{self.address_string()} - {fmt % args} "
                f"firmware={firmware_name}/{firmware_version} transport={firmware_transport}",
                flush=True,
            )

        def _read_json_body(self) -> dict[str, Any]:
            length = self._content_length()
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def _read_raw_body(self, length: int) -> bytes:
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _content_length(self) -> int:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                return 0
            return max(0, length)

        def _is_authorized(self) -> bool:
            expected = _bridge_token()
            if not expected:
                return True
            supplied = self.headers.get("X-Vibe-Stick-Token", "")
            return hmac.compare_digest(supplied, expected)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status=status)

    return VibeStickHandler


def run_server(host: str, port: int) -> None:
    _enforce_bind_security(host)
    relay_port = _input_relay_port()
    relay_hub = InputRelayHub(_bridge_token()) if relay_port else None
    store = BridgeStateStore(relay_hub)
    if relay_hub:
        relay_hub.start(host, relay_port)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    if not _bridge_token():
        print(
            "WARNING: VIBE_STICK_BRIDGE_TOKEN is not set; POST endpoints are unauthenticated on loopback only.",
            flush=True,
        )
    print(f"VibeStick Bridge listening on http://{host}:{port}", flush=True)
    server.serve_forever()


def _protected_paths() -> set[str]:
    return {
        "/event",
        "/quota/refresh",
        "/recording/start",
        "/recording/audio",
        "/recording/chunk",
        "/recording/stop",
    }


def _lot_device_display_name(firmware_name: str) -> str:
    normalized = "".join(ch for ch in firmware_name.lower() if ch.isalnum())
    if not normalized or "vibestick" in normalized or "sticks3" in normalized:
        return "Stick S3"
    return firmware_name.strip() or "Stick S3"


def _lot_device_id(firmware_name: str, remote_addr: str) -> str:
    normalized = "".join(ch for ch in firmware_name.lower() if ch.isalnum())
    if "vibestick" in normalized or "sticks3" in normalized:
        return "sticks3"
    if normalized:
        return normalized
    return f"device-{remote_addr}"


def _bridge_token() -> str:
    token = os.environ.get("VIBE_STICK_BRIDGE_TOKEN", "").strip()
    if token.lower() in PLACEHOLDER_BRIDGE_TOKENS:
        return ""
    return token


def _enforce_bind_security(host: str) -> None:
    if _host_requires_token(host) and not _bridge_token():
        raise SystemExit(
            "Refusing to bind VibeStick Bridge outside loopback without "
            "VIBE_STICK_BRIDGE_TOKEN. Set a strong shared token or use --host 127.0.0.1."
        )


def _host_requires_token(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return False
    if not normalized:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return not address.is_loopback


def _max_recording_audio_bytes() -> int:
    raw = os.environ.get("VIBE_STICK_MAX_RECORDING_AUDIO_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_RECORDING_AUDIO_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_RECORDING_AUDIO_BYTES
    return max(256_000, min(8_000_000, value))


def _input_relay_port() -> int:
    raw = os.environ.get("VIBE_STICK_INPUT_RELAY_PORT", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 1 <= value <= 65535 else 0


def _resolve_project_root() -> Path:
    configured = os.environ.get("VIBE_STICK_PROJECT_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd()
    if root.name in {"bridge", "firmware", "app", "scripts"} and (root.parent / "README.md").exists():
        root = root.parent
    return root.resolve()


def _stale_quota(existing: QuotaSnapshot) -> QuotaSnapshot:
    return QuotaSnapshot(
        quota_5h_remaining=existing.quota_5h_remaining,
        quota_7d_remaining=existing.quota_7d_remaining,
        quota_updated_at=existing.quota_updated_at,
        quota_stale=True,
    )


def _has_quota(snapshot: QuotaSnapshot) -> bool:
    return snapshot.quota_5h_remaining is not None or snapshot.quota_7d_remaining is not None


def _claude_quota_from_state(state: VibeStickState) -> QuotaSnapshot:
    provider = state.provider
    if provider.id != "claude":
        return QuotaSnapshot()
    snapshot = QuotaSnapshot(
        quota_5h_remaining=provider.quota_5h_remaining,
        quota_7d_remaining=provider.quota_7d_remaining,
        quota_updated_at=provider.quota_updated_at,
        quota_stale=True,
    )
    return snapshot if _has_quota(snapshot) else QuotaSnapshot()


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def _with_bridge_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    payload["bridge_name"] = BRIDGE_NAME
    payload["bridge_version"] = BRIDGE_VERSION
    return payload


def _configured_provider() -> str:
    value = os.environ.get("VIBE_STICK_PROVIDER", "auto").strip().lower()
    return value if value in {"codex", "claude", "auto"} else "auto"


def _sub2_usage_interval_seconds() -> int:
    try:
        value = int(os.environ.get("VIBE_STICK_SUB2_USAGE_INTERVAL_SECONDS", ""))
    except ValueError:
        value = DEFAULT_SUB2_USAGE_INTERVAL_SECONDS
    if value <= 0:
        value = DEFAULT_SUB2_USAGE_INTERVAL_SECONDS
    return max(MIN_SUB2_USAGE_INTERVAL_SECONDS, value)


def _sub2_account_id(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        value = default
    return value if value > 0 else default


def _select_active_provider(
    configured: str,
    last_active: str,
    codex_observation: ProviderObservation,
    claude_observation: ProviderObservation,
) -> str:
    if configured in {"codex", "claude"}:
        return configured

    if codex_observation.online and not claude_observation.online:
        return "codex"
    if claude_observation.online and not codex_observation.online:
        return "claude"
    if codex_observation.online and claude_observation.online:
        codex_time = codex_observation.latest_event_timestamp
        claude_time = claude_observation.latest_event_timestamp
        if codex_time is not None and claude_time is not None:
            return "claude" if claude_time > codex_time else "codex"
        if claude_time is not None:
            return "claude"
        if codex_time is not None:
            return "codex"
        return last_active if last_active in {"codex", "claude"} else "codex"

    return last_active if last_active in {"codex", "claude"} else "codex"


def _select_alert_observation(
    active_observation: ProviderObservation,
    *observations: ProviderObservation,
) -> ProviderObservation:
    if _observation_has_alert(active_observation):
        return active_observation
    for observation in observations:
        if observation is active_observation:
            continue
        if _observation_has_alert(observation):
            return observation
    return active_observation


def _observation_has_alert(observation: ProviderObservation) -> bool:
    try:
        alert_type = AlertType(observation.alert_type)
    except ValueError:
        return False
    return alert_type in {AlertType.DONE, AlertType.APPROVAL, AlertType.ERROR} and bool(observation.alert_event_id)


def _claude_usage_interval_seconds() -> int:
    try:
        value = int(os.environ.get("VIBE_STICK_CLAUDE_USAGE_INTERVAL_SECONDS", ""))
    except ValueError:
        value = DEFAULT_CLAUDE_USAGE_INTERVAL_SECONDS
    if value <= 0:
        value = DEFAULT_CLAUDE_USAGE_INTERVAL_SECONDS
    return max(MIN_CLAUDE_USAGE_INTERVAL_SECONDS, value)


def _positive_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        value = default
    if value <= 0:
        value = default
    return max(minimum, min(maximum, value))


def _codex_state_from_observation(observation: ProviderObservation) -> CodexState:
    return CodexState(
        status=observation.status,
        project=observation.project,
        quota_5h_remaining=observation.quota_5h_remaining,
        quota_7d_remaining=observation.quota_7d_remaining,
        quota_updated_at=observation.quota_updated_at,
        quota_stale=observation.quota_stale,
        account_name=observation.account_name,
    )


def _provider_state_from_observation(observation: ProviderObservation) -> ProviderState:
    return ProviderState(
        id=observation.provider_id,
        display_name=observation.display_name,
        implemented=True,
        status=observation.status,
        project=observation.project,
        quota_5h_remaining=observation.quota_5h_remaining,
        quota_7d_remaining=observation.quota_7d_remaining,
        quota_updated_at=observation.quota_updated_at,
        quota_stale=observation.quota_stale,
        account_name=observation.account_name,
    )


def _apply_manual_codex_state(observation: ProviderObservation, state: VibeStickState) -> None:
    observation.status = state.codex.status
    observation.alert_type = state.alert.type.value
    observation.alert_message = state.alert.message
    observation.alert_event_id = state.alert.event_id


def _int_header(raw: str | None, default: int) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        return default
    return value if value > 0 else default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VibeStick Bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run_server(args.host, args.port)
