from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AgentStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    DONE = "DONE"
    APPROVAL = "APPROVAL"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class AlertType(StrEnum):
    NONE = "NONE"
    DONE = "DONE"
    APPROVAL = "APPROVAL"
    ERROR = "ERROR"


@dataclass
class CodexState:
    status: AgentStatus = AgentStatus.IDLE
    project: str = "vibestick"
    quota_5h_remaining: int | None = None
    quota_7d_remaining: int | None = None
    quota_updated_at: str = ""
    quota_stale: bool = False
    account_name: str = ""


@dataclass
class ProviderState:
    id: str = "codex"
    display_name: str = "Codex"
    implemented: bool = True
    status: AgentStatus = AgentStatus.IDLE
    project: str = "vibestick"
    quota_5h_remaining: int | None = None
    quota_7d_remaining: int | None = None
    quota_updated_at: str = ""
    quota_stale: bool = False
    account_name: str = ""


@dataclass
class AlertState:
    event_id: str = ""
    type: AlertType = AlertType.NONE
    message: str = ""


@dataclass
class VibeStickState:
    time: str
    wifi: bool
    ble: bool
    battery: int | None
    active_provider: str
    provider: ProviderState
    codex: CodexState
    alert: AlertState
    providers: dict[str, ProviderState] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        data["battery"] = None
        data["provider"]["status"] = self.provider.status.value
        data["codex"]["status"] = self.codex.status.value
        data["alert"]["type"] = self.alert.type.value
        for provider_id, provider in self.providers.items():
            data["providers"][provider_id]["status"] = provider.status.value
        return data


def now_time_text() -> str:
    return datetime.now().strftime("%H:%M")


def event_id(prefix: str) -> str:
    return f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{prefix}"


def state_from_dict(data: dict[str, Any]) -> VibeStickState:
    provider_data = data.get("provider", {})
    providers_data = data.get("providers", {})
    codex_data = data.get("codex", {})
    codex_data = codex_data if isinstance(codex_data, dict) else {}
    alert_data = data.get("alert", {})
    alert_data = alert_data if isinstance(alert_data, dict) else {}
    provider_state = _provider_state_from_dict(provider_data if isinstance(provider_data, dict) else {}, codex_data)
    return VibeStickState(
        time=now_time_text(),
        wifi=bool(data.get("wifi", True)),
        ble=bool(data.get("ble", False)),
        battery=data.get("battery"),
        active_provider=str(data.get("active_provider") or provider_state.id),
        provider=provider_state,
        codex=CodexState(
            status=AgentStatus(codex_data.get("status", AgentStatus.IDLE.value)),
            project=str(codex_data.get("project") or "vibestick"),
            quota_5h_remaining=codex_data.get("quota_5h_remaining"),
            quota_7d_remaining=codex_data.get("quota_7d_remaining"),
            quota_updated_at=str(codex_data.get("quota_updated_at") or ""),
            quota_stale=bool(codex_data.get("quota_stale", False)),
            account_name=str(codex_data.get("account_name") or ""),
        ),
        alert=AlertState(
            event_id=str(alert_data.get("event_id") or ""),
            type=AlertType(alert_data.get("type", AlertType.NONE.value)),
            message=str(alert_data.get("message") or ""),
        ),
        providers=_providers_from_dict(
            providers_data if isinstance(providers_data, dict) else {},
            provider_state,
            codex_data,
        ),
    )


def _provider_state_from_dict(provider_data: dict[str, Any], codex_data: dict[str, Any]) -> ProviderState:
    if provider_data:
        return ProviderState(
            id=str(provider_data.get("id") or "codex"),
            display_name=str(provider_data.get("display_name") or "Codex"),
            implemented=bool(provider_data.get("implemented", True)),
            status=AgentStatus(provider_data.get("status", AgentStatus.IDLE.value)),
            project=str(provider_data.get("project") or "vibestick"),
            quota_5h_remaining=provider_data.get("quota_5h_remaining"),
            quota_7d_remaining=provider_data.get("quota_7d_remaining"),
            quota_updated_at=str(provider_data.get("quota_updated_at") or ""),
            quota_stale=bool(provider_data.get("quota_stale", False)),
            account_name=str(provider_data.get("account_name") or ""),
        )

    return ProviderState(
        id="codex",
        display_name="Codex",
        implemented=True,
        status=AgentStatus(codex_data.get("status", AgentStatus.IDLE.value)),
        project=str(codex_data.get("project") or "vibestick"),
        quota_5h_remaining=codex_data.get("quota_5h_remaining"),
        quota_7d_remaining=codex_data.get("quota_7d_remaining"),
        quota_updated_at=str(codex_data.get("quota_updated_at") or ""),
        quota_stale=bool(codex_data.get("quota_stale", False)),
        account_name=str(codex_data.get("account_name") or ""),
    )


def _providers_from_dict(
    providers_data: dict[str, Any],
    active_provider: ProviderState,
    codex_data: dict[str, Any],
) -> dict[str, ProviderState]:
    providers: dict[str, ProviderState] = {}
    for provider_id, raw_provider in providers_data.items():
        if not isinstance(raw_provider, dict):
            continue
        normalized = dict(raw_provider)
        normalized.setdefault("id", str(provider_id))
        providers[str(provider_id)] = _provider_state_from_dict(normalized, codex_data)
    providers.setdefault(active_provider.id, active_provider)
    if "codex" not in providers and codex_data:
        providers["codex"] = _provider_state_from_dict({}, codex_data)
    return providers


def default_state() -> VibeStickState:
    codex = CodexState(
        status=AgentStatus.RUNNING,
        project="vibestick",
        quota_5h_remaining=None,
        quota_7d_remaining=None,
        quota_updated_at="",
        quota_stale=False,
        account_name="",
    )
    return VibeStickState(
        time=now_time_text(),
        wifi=True,
        ble=False,
        battery=None,
        active_provider="codex",
        provider=ProviderState(
            id="codex",
            display_name="Codex",
            implemented=True,
            status=codex.status,
            project=codex.project,
            quota_5h_remaining=codex.quota_5h_remaining,
            quota_7d_remaining=codex.quota_7d_remaining,
            quota_updated_at=codex.quota_updated_at,
            quota_stale=codex.quota_stale,
            account_name=codex.account_name,
        ),
        codex=codex,
        alert=AlertState(event_id="", type=AlertType.NONE, message=""),
        providers={},
    )
