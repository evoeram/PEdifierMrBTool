#!/usr/bin/env python3
"""Edifier MR BLE Control — Web UI server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app_config import AppConfig
from mr_ble import (
    INPUT_SOURCE_NAMES,
    EQPreset,
    MRController,
    PEQBand,
    filter_edifier,
    init_device_state,
    profile_from_name_and_support,
    scan_ble,
    snap_gain,
)

logger = logging.getLogger("edifier.server")
config = AppConfig.from_env()

app = FastAPI(title="Edifier MR BLE Control")
ctrl: MRController | None = None
ctrl_lock = asyncio.Lock()
clients: set[WebSocket] = set()
ble_address: str | None = config.auto_connect_address
static_dir = Path(__file__).parent / "static"

_vol_lock = asyncio.Lock()
_vol_pending: int | None = None
_vol_task: asyncio.Task[None] | None = None
_vol_last_sent: float = 0.0
_VOL_COMMANDS: Final[set[str]] = {
    "setVolume",
    "setVolumePercent",
    "setVolumeImmediate",
    "getVolume",
}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def ok(data: Any = None) -> dict[str, Any]:
    return {"type": "result", "ok": True, "data": data}


def err(message: str) -> dict[str, Any]:
    return {"type": "result", "ok": False, "error": message}


def state_to_dict(controller: MRController) -> dict[str, Any]:
    s = controller.state
    return {
        "connected": controller.ble.connected,
        "model": s.model,
        "name": s.name,
        "address": s.address,
        "firmware": s.firmware,
        "volume": s.current_volume,
        "maxVolume": s.max_volume,
        "volumePercent": s.volume_percent,
        "codec": s.codec_name,
        "activeSpeaker": s.active_speaker,
        "lowCutFreq": s.low_cut_freq,
        "lowCutSlope": s.slope_name,
        "lowCutSlopeRaw": s.low_cut_slope,
        "acousticSpace": s.acoustic_space,
        "desktopMode": s.desktop_mode,
        "promptTone": s.prompt_tone,
        "inputSource": INPUT_SOURCE_NAMES.get(s.input_source, f"0x{s.input_source:02X}"),
        "inputSourceRaw": s.input_source,
        "eqBandCount": s.eq_band_count,
        "eqPresetName": s.eq_preset_name,
        "supportsLdac": controller.profile.supports_ldac,
        "supportsActiveSpeaker": controller.profile.supports_active_speaker,
        "supportsAdvancedAudio": controller.profile.supports_advanced_audio,
        "bands": [band.to_dict() for band in controller.bands],
    }


def presets_to_list(controller: MRController) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "name": preset.name,
            "builtin": is_builtin,
            "description": preset.description,
            "bandCount": len(preset.bands),
        }
        for key, preset, is_builtin in controller.presets.list_all_for_profile(controller.profile)
    ]


async def broadcast(msg: dict[str, Any]) -> None:
    data = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    if dead:
        logger.debug("Removing dead websocket clients", extra={"count": len(dead)})
        clients.difference_update(dead)


async def send_full_state(ws: WebSocket | None = None) -> None:
    if not ctrl:
        return
    msg = {"type": "state", "data": state_to_dict(ctrl)}
    if ws:
        await ws.send_text(json.dumps(msg))
    else:
        await broadcast(msg)


async def send_presets(ws: WebSocket | None = None) -> None:
    if not ctrl:
        return
    msg = {"type": "presets", "data": presets_to_list(ctrl)}
    if ws:
        await ws.send_text(json.dumps(msg))
    else:
        await broadcast(msg)


async def send_volume_update() -> None:
    if not ctrl:
        return
    await broadcast(
        {
            "type": "volumeUpdate",
            "data": {
                "volume": ctrl.state.current_volume,
                "maxVolume": ctrl.state.max_volume,
                "volumePercent": ctrl.state.volume_percent,
            },
        }
    )


def _read_int(
    params: dict[str, Any], key: str, *, min_value: int | None = None, max_value: int | None = None
) -> int:
    if key not in params:
        raise ValueError(f"Missing parameter '{key}'")
    try:
        value = int(params[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter '{key}' must be an integer") from exc
    if min_value is not None and value < min_value:
        raise ValueError(f"Parameter '{key}' must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"Parameter '{key}' must be <= {max_value}")
    return value


def _read_float(params: dict[str, Any], key: str) -> float:
    if key not in params:
        raise ValueError(f"Missing parameter '{key}'")
    try:
        return float(params[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter '{key}' must be a number") from exc


def _read_bool(params: dict[str, Any], key: str) -> bool:
    if key not in params:
        raise ValueError(f"Missing parameter '{key}'")
    value = params[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower().strip()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"Parameter '{key}' must be a boolean")


async def _vol_send_loop() -> None:
    global _vol_pending, _vol_last_sent

    while True:
        async with _vol_lock:
            target = _vol_pending
            _vol_pending = None

        if target is None:
            await asyncio.sleep(0.05)
            continue

        wait = config.volume_min_interval_seconds - (time.monotonic() - _vol_last_sent)
        if wait > 0:
            await asyncio.sleep(wait)

        async with _vol_lock:
            if _vol_pending is not None:
                target = _vol_pending
                _vol_pending = None

        if not ctrl or not ctrl.ble.connected:
            logger.info("Stopping volume queue loop: device disconnected")
            break

        try:
            success = await ctrl.set_volume(target)
            _vol_last_sent = time.monotonic()
            if success:
                await send_volume_update()
            else:
                async with _vol_lock:
                    pending_after_failure = _vol_pending

                failure_context = {
                    "target_volume": target,
                    "cached_current_volume": ctrl.state.current_volume,
                    "cached_max_volume": ctrl.state.max_volume,
                    "pending_volume_after_failure": pending_after_failure,
                    "volume_min_interval_seconds": config.volume_min_interval_seconds,
                    "seconds_since_last_send": time.monotonic() - _vol_last_sent,
                    "ble_connected": bool(ctrl and ctrl.ble.connected),
                }

                logger.warning(
                    "Failed to set volume in queue; attempting state refresh for diagnostics",
                    extra=failure_context,
                )

                try:
                    observed_current, observed_max = await ctrl.get_volume()
                except Exception:
                    logger.exception(
                        "Failed to refresh volume state after queue set failure",
                        extra=failure_context,
                    )
                else:
                    logger.warning(
                        "Volume queue set failure details",
                        extra={
                            **failure_context,
                            "observed_current_volume": observed_current,
                            "observed_max_volume": observed_max,
                        },
                    )
        except Exception:
            logger.exception("Unhandled error in volume queue", extra={"volume": target})


async def enqueue_volume(value: int) -> None:
    global _vol_pending, _vol_task

    async with _vol_lock:
        _vol_pending = value

    if _vol_task is None or _vol_task.done():
        _vol_task = asyncio.create_task(_vol_send_loop())


async def handle_command(ws: WebSocket, msg: dict[str, Any]) -> None:
    cmd = msg.get("cmd")
    params = msg.get("params", {})
    req_id = msg.get("id")

    async def reply(resp: dict[str, Any]) -> None:
        if req_id is not None:
            resp["id"] = req_id
        try:
            await ws.send_text(json.dumps(resp))
        except Exception:
            logger.debug("Reply failed due to websocket disconnect")

    if not isinstance(cmd, str):
        await reply(err("Invalid 'cmd' field"))
        return
    if not isinstance(params, dict):
        await reply(err("Invalid 'params' field"))
        return

    if not ctrl or not ctrl.ble.connected:
        if cmd not in {"connect", "scan", "ping"}:
            await reply(err("Not connected"))
            return

    try:
        async with ctrl_lock:
            result = await _dispatch(cmd, params)
        await reply(result)
    except ValueError as exc:
        await reply(err(str(exc)))
    except Exception:
        logger.exception("Command failed", extra={"cmd": cmd})
        await reply(err(f"Command '{cmd}' failed"))

    if ctrl and ctrl.ble.connected and cmd not in {"ping", "scan", *_VOL_COMMANDS}:
        await send_full_state()


async def _dispatch(cmd: str, p: dict[str, Any]) -> dict[str, Any]:
    global ctrl

    if cmd == "ping":
        return ok("pong")

    if cmd == "scan":
        devices = await scan_ble(config.scan_timeout_seconds)
        return ok(
            [
                {"name": d.name, "address": d.address, "rssi": d.rssi}
                for d in filter_edifier(devices)
            ]
        )

    if cmd == "connect":
        addr = p.get("address", ble_address)
        if not isinstance(addr, str) or not addr.strip():
            return err("No address provided")

        controller = MRController()
        if not await controller.ble.connect(addr):
            return err(f"Failed to connect to {addr}")

        await init_device_state(controller.ble)
        controller.profile = profile_from_name_and_support(
            controller.state.name, controller.state.eq_band_count
        )
        controller.state.model = controller.profile.model
        controller.bands = [
            PEQBand(i, controller.profile.default_freqs[i], 0.0)
            for i in range(controller.profile.eq_num_bands)
        ]
        await controller.query_eq()
        ctrl = controller
        asyncio.create_task(send_presets())
        logger.info(
            "Connected to BLE device", extra={"address": addr, "name": controller.state.name}
        )
        return ok(state_to_dict(controller))

    if not ctrl:
        return err("Not connected")

    if cmd == "disconnect":
        await ctrl.disconnect()
        return ok()

    if cmd == "setVolume":
        volume = _read_int(p, "value", min_value=0, max_value=ctrl.state.max_volume)
        ctrl.state.current_volume = volume
        await enqueue_volume(volume)
        return ok({"volume": volume, "maxVolume": ctrl.state.max_volume})

    if cmd == "setVolumePercent":
        percent = _read_float(p, "percent")
        if not 0 <= percent <= 100:
            raise ValueError("Parameter 'percent' must be between 0 and 100")
        volume = round(percent / 100 * ctrl.state.max_volume)
        ctrl.state.current_volume = volume
        await enqueue_volume(volume)
        return ok({"volume": volume, "maxVolume": ctrl.state.max_volume})

    if cmd == "setVolumeImmediate":
        volume = _read_int(p, "value")
        volume = max(0, min(ctrl.state.max_volume, volume))
        if await ctrl.set_volume(volume):
            await send_volume_update()
            return ok({"volume": ctrl.state.current_volume, "maxVolume": ctrl.state.max_volume})
        return err("Failed to set volume")

    if cmd == "getVolume":
        current, maximum = await ctrl.get_volume()
        await send_volume_update()
        return ok({"volume": current, "max": maximum})

    if cmd == "queryEQ":
        bands = await ctrl.query_eq()
        return ok([b.to_dict() for b in (bands or ctrl.bands)])

    if cmd == "setBand":
        idx = _read_int(p, "index", min_value=0)
        if idx >= len(ctrl.bands):
            raise ValueError(f"Parameter 'index' must be < {len(ctrl.bands)}")
        freq = _read_int(p, "frequency", min_value=20, max_value=20_000)
        gain = _read_float(p, "gain")
        if await ctrl.set_band(idx, freq, snap_gain(gain)):
            return ok(ctrl.bands[idx].to_dict())
        return err("Failed to set band")

    if cmd == "setGain":
        idx = _read_int(p, "index", min_value=0)
        if idx >= len(ctrl.bands):
            raise ValueError(f"Parameter 'index' must be < {len(ctrl.bands)}")
        gain = _read_float(p, "gain")
        ctrl._ensure_full_band_list()
        band = ctrl.bands[idx]
        if await ctrl.set_band(idx, band.frequency, snap_gain(gain)):
            return ok(ctrl.bands[idx].to_dict())
        return err("Failed to set gain")

    if cmd == "resetEQ":
        return ok() if await ctrl.reset_eq() else err("Reset failed")

    if cmd == "flatEQ":
        preset = ctrl.presets.get_for_profile("flat", ctrl.profile)
        return ok() if preset and await ctrl.apply_preset(preset) else err("Flat failed")

    if cmd == "getPresets":
        return ok(presets_to_list(ctrl))

    if cmd == "applyPreset":
        name = p.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Parameter 'name' is required")
        preset = ctrl.presets.get_for_profile(name, ctrl.profile)
        if not preset:
            return err(f"Preset '{name}' not found")
        return ok() if await ctrl.apply_preset(preset) else err("Apply failed")

    if cmd == "savePreset":
        name = p.get("name", "")
        desc = p.get("description", "User preset")
        if not isinstance(name, str) or not name.strip():
            return err("Name required")
        if not isinstance(desc, str):
            raise ValueError("Parameter 'description' must be a string")
        ctrl.presets.save(EQPreset(name.strip(), list(ctrl.bands), desc[:120]))
        await send_presets()
        return ok()

    if cmd == "deletePreset":
        name = p.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Parameter 'name' is required")
        if name.lower() in ctrl.presets.get_builtin_map(ctrl.profile):
            return err("Cannot delete built-in preset")
        if ctrl.presets.delete(name):
            await send_presets()
            return ok()
        return err(f"'{name}' not found")

    if cmd == "setLowCut":
        freq = _read_int(p, "frequency", min_value=20, max_value=20_000)
        slope = _read_int(p, "slope")
        return ok() if await ctrl.set_low_cut(freq, slope) else err("Failed")

    if cmd == "setAcousticSpace":
        value = _read_int(p, "value")
        return ok() if await ctrl.set_acoustic_space(value) else err("Failed")

    if cmd == "setDesktopMode":
        enabled = _read_bool(p, "enabled")
        return ok() if await ctrl.set_desktop_mode(enabled) else err("Failed")

    if cmd == "setActiveSpeaker":
        side = p.get("side", "")
        if side not in {"left", "right", "Left", "Right"}:
            raise ValueError("Parameter 'side' must be 'left' or 'right'")
        return ok() if await ctrl.set_active_speaker(str(side)) else err("Failed")

    if cmd == "setLdac":
        mode = p.get("mode", "")
        if not isinstance(mode, str):
            raise ValueError("Parameter 'mode' must be a string")
        return ok() if await ctrl.set_ldac(mode) else err("Failed")

    if cmd == "setPromptTone":
        enabled = _read_bool(p, "enabled")
        return ok() if await ctrl.set_prompt_tone(enabled) else err("Failed")

    if cmd == "shutdown":
        return ok("Shutdown sent") if await ctrl.shutdown_device() else err("Shutdown failed")

    if cmd == "refreshState":
        await init_device_state(ctrl.ble)
        ctrl.profile = profile_from_name_and_support(ctrl.state.name, ctrl.state.eq_band_count)
        await ctrl.query_eq()
        return ok(state_to_dict(ctrl))

    return err(f"Unknown command: {cmd}")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    logger.info("WebSocket client connected", extra={"clients": len(clients)})

    try:
        if ctrl and ctrl.ble.connected:
            await send_full_state(ws)
            await send_presets(ws)
        else:
            await ws.send_text(json.dumps({"type": "state", "data": {"connected": False}}))

        while True:
            raw = await ws.receive_text()
            if len(raw) > config.ws_max_message_size:
                await ws.send_text(json.dumps(err("Message too large")))
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps(err("Invalid JSON")))
                continue
            if not isinstance(msg, dict):
                await ws.send_text(json.dumps(err("Message must be a JSON object")))
                continue
            await handle_command(ws, msg)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket handler failed")
    finally:
        clients.discard(ws)
        logger.info("WebSocket connection cleanup", extra={"clients": len(clients)})


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def startup() -> None:
    global ctrl
    if not ble_address:
        return

    logger.info("Startup auto-connect requested", extra={"address": ble_address})
    controller = MRController()
    if not await controller.ble.connect(ble_address):
        logger.warning("Auto-connect failed")
        return

    await init_device_state(controller.ble)
    controller.profile = profile_from_name_and_support(
        controller.state.name, controller.state.eq_band_count
    )
    controller.state.model = controller.profile.model
    controller.bands = [
        PEQBand(i, controller.profile.default_freqs[i], 0.0)
        for i in range(controller.profile.eq_num_bands)
    ]
    await controller.query_eq()
    ctrl = controller
    logger.info(
        "Auto-connect successful", extra={"name": ctrl.state.name, "model": ctrl.state.model}
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    global _vol_task
    if _vol_task and not _vol_task.done():
        _vol_task.cancel()
    if ctrl:
        await ctrl.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edifier MR BLE Web UI")
    parser.add_argument(
        "-a",
        "--address",
        type=str,
        default=config.auto_connect_address,
        help="BLE address to auto-connect",
    )
    parser.add_argument("-p", "--port", type=int, default=config.port, help="HTTP port")
    parser.add_argument("--host", type=str, default=config.host, help="Bind host")
    parser.add_argument("--log-level", type=str, default=config.log_level, help="Logging level")
    return parser.parse_args()


def main() -> None:
    global ble_address, config
    args = parse_args()
    ble_address = args.address
    config = AppConfig(
        host=args.host,
        port=args.port,
        log_level=args.log_level.upper(),
        auto_connect_address=ble_address,
        scan_timeout_seconds=config.scan_timeout_seconds,
        ws_max_message_size=config.ws_max_message_size,
        volume_min_interval_seconds=config.volume_min_interval_seconds,
    )
    setup_logging(config.log_level)
    logger.info("Starting server", extra={"host": config.host, "port": config.port})
    uvicorn.run(app, host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
