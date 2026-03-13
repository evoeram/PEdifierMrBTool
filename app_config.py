from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    auto_connect_address: str | None = None
    scan_timeout_seconds: float = 8.0
    ws_max_message_size: int = 16_384
    volume_min_interval_seconds: float = 0.35

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            host=os.getenv("EDIFIER_HOST", "0.0.0.0"),
            port=_as_int("EDIFIER_PORT", 8000),
            log_level=os.getenv("EDIFIER_LOG_LEVEL", "INFO").upper(),
            auto_connect_address=os.getenv("EDIFIER_BLE_ADDRESS") or None,
            scan_timeout_seconds=_as_float("EDIFIER_SCAN_TIMEOUT_SECONDS", 8.0),
            ws_max_message_size=_as_int("EDIFIER_WS_MAX_MESSAGE_SIZE", 16_384),
            volume_min_interval_seconds=_as_float("EDIFIER_VOLUME_MIN_INTERVAL_SECONDS", 0.35),
        )


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
