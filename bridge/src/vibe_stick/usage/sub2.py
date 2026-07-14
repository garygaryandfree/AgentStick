from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://88api.ai/sub2-usage"
PROVIDER_PATHS = {
    "codex": "gpt-plus",
    "claude": "claude",
}


class Sub2UsageError(RuntimeError):
    """Raised when a Sub2-Usage snapshot cannot be fetched or parsed."""


@dataclass(frozen=True)
class Sub2UsageSnapshot:
    provider_id: str
    account_id: int
    account_name: str
    quota_5h_remaining: int
    quota_7d_remaining: int
    quota_updated_at: str


def fetch_sub2_usage(
    provider_id: str,
    account_id: int,
    token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> Sub2UsageSnapshot:
    provider_path = PROVIDER_PATHS.get(provider_id)
    if provider_path is None:
        raise Sub2UsageError(f"unsupported provider: {provider_id}")
    if account_id <= 0:
        raise Sub2UsageError("account id must be positive")
    if not token.strip():
        raise Sub2UsageError("missing Sub2-Usage token")

    url = f"{base_url.rstrip('/')}/{provider_path}/{account_id}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
            "User-Agent": "VibeStick-Bridge/0.1",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Sub2UsageError(f"request failed: {exc.__class__.__name__}") from exc

    return parse_sub2_usage(provider_id, account_id, payload)


def parse_sub2_usage(
    provider_id: str,
    account_id: int,
    payload: Any,
) -> Sub2UsageSnapshot:
    if not isinstance(payload, dict):
        raise Sub2UsageError("response must be a JSON object")
    windows = payload.get("windows")
    if not isinstance(windows, dict):
        raise Sub2UsageError("response is missing windows")

    five_hour = windows.get("five_hour")
    seven_day = windows.get("seven_day")
    if not isinstance(five_hour, dict) or not isinstance(seven_day, dict):
        raise Sub2UsageError("response is missing usage windows")

    response_account_id = _positive_int(payload.get("account_id")) or account_id
    account_name = str(payload.get("account_name") or "").strip()
    return Sub2UsageSnapshot(
        provider_id=provider_id,
        account_id=response_account_id,
        account_name=account_name,
        quota_5h_remaining=_remaining_percent(five_hour.get("remaining_percent"), "five_hour"),
        quota_7d_remaining=_remaining_percent(seven_day.get("remaining_percent"), "seven_day"),
        quota_updated_at=datetime.now().strftime("%H:%M"),
    )


def _remaining_percent(value: Any, field: str) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise Sub2UsageError(f"invalid {field} remaining_percent") from exc
    return max(0, min(100, int(round(parsed))))


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
