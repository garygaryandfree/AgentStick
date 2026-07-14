from __future__ import annotations

import os
import platform
from pathlib import Path


def _app_support_dir() -> Path:
    override = os.environ.get("VIBE_STICK_APP_SUPPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "VibeStick" if base else Path.home() / "AppData" / "Local" / "VibeStick"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "VibeStick"
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share") / "VibeStick"


APP_SUPPORT_DIR = _app_support_dir()
STATE_PATH = APP_SUPPORT_DIR / "state.json"
QUOTA_PATH = APP_SUPPORT_DIR / "quota.json"
CLAUDE_QUOTA_PATH = APP_SUPPORT_DIR / "claude-quota.json"
RECORDING_PATH = APP_SUPPORT_DIR / "recording.json"
HUD_STATE_PATH = APP_SUPPORT_DIR / "hud-state.json"
RECORDINGS_DIR = APP_SUPPORT_DIR / "Recordings"


def ensure_app_support() -> Path:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    return APP_SUPPORT_DIR
