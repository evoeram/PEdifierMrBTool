#!/usr/bin/env python3
"""
Edifier MR BLE Control Tool v2.3

Supports:
- MR5BT: 9-band EQ, advanced audio, active speaker, LDAC
- MR3BT: 6-band EQ, SBC-only, no active speaker selection, no LDAC
- Unknown models: limited/safer behavior using reported capabilities where possible

Usage:
    python mr_ble.py                    # Auto-scan & connect
    python mr_ble.py -a XX:XX:XX:XX     # Connect by address
    python mr_ble.py --scan             # Scan only
    python mr_ble.py --verify           # Verify protocol

Requirements:
    pip install bleak
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
import traceback
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    print("ERROR: 'bleak' required. Install: pip install bleak")
    sys.exit(1)


# ============================================================
# Constants
# ============================================================

HEADER_TX = 0xAA
HEADER_RX = 0xBB
APP_CODE = 0xEC

BLE_UUID_WRITE = "48090002-1a48-11e9-ab14-d663bd873d93"
BLE_UUID_NOTIFY = "48090001-1a48-11e9-ab14-d663bd873d93"

PRESETS_DIR = Path.home() / ".edifier_mr_ble"

EQ_GAIN_MIN = -3.0
EQ_GAIN_MAX = 3.0
EQ_GAIN_STEP = 0.5
EQ_GAIN_OFFSET = 6

MR5_DEFAULT_FREQS = [62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
MR3_DEFAULT_FREQS = [125, 250, 500, 1000, 4000, 8000]

CODEC_NAMES = {
    0: "SBC",
    1: "AAC",
    2: "aptX",
    3: "aptX-HD",
    4: "LDAC",
    5: "LHDC",
    13: "LDAC",
}
INPUT_SOURCE_NAMES = {0: "Bluetooth", 1: "USB", 2: "AUX", 3: "Optical"}
SLOPE_TO_BYTE = {6: 0, 12: 1, 18: 2, 24: 3}
SLOPE_FROM_BYTE = {0: "6dB", 1: "12dB", 2: "18dB", 3: "24dB"}
LDAC_MODES = {"off": 0, "48k": 1, "96k": 2}

EDIFIER_KEYWORDS = ("edifier", "mr5", "mr3")

logger = logging.getLogger("edifier.ble")


class Cmd(IntEnum):
    """BLE command codes."""

    EQ_SET = 0xC4
    EQ_QUERY = 0xC5
    SINGLE_EQ_SET = 0x44
    CUSTOM_EQ_GET = 0x43
    CUSTOM_EQ_RESET = 0x45
    CUSTOM_EQ_CHANGE = 0x46
    EQ_NAME_SAVE = 0x47
    VOLUME_QUERY = 0x66
    VOLUME_SET = 0x67
    DEVICE_NAME_QUERY = 0xC9
    VERSION_QUERY = 0xC6
    SUPPORT_FUNC = 0xD8
    DEVICE_STATE = 0xC3
    HEADSET_STATE = 0xC0
    AUDIO_CODEC = 0x68
    ADVANCED_QUERY = 0xD5
    AUTO_SHUTDOWN_QUERY = 0xD7
    AUTO_SHUTDOWN_SET = 0xD6
    RESET_DEVICE = 0x07
    SHUTDOWN = 0xCE
    INPUT_SOURCE_QUERY = 0x61
    INPUT_SOURCE_SET = 0x62
    LDAC_QUERY = 0x48
    LDAC_CFG_SET = 0x92
    LDAC_SET = 0x49
    ACTIVE_SPEAKER_QUERY = 0xBB
    ACTIVE_SPEAKER_SET = 0xBC
    PROMPT_TONE_QUERY = 0x86
    PROMPT_TONE_SET = 0x87


# ============================================================
# Utility Functions
# ============================================================


def snap_gain(gain: float) -> float:
    """Clamp and round gain to nearest 0.5 dB step."""
    return max(EQ_GAIN_MIN, min(EQ_GAIN_MAX, round(gain * 2) / 2))


def gain_to_byte(gain: float) -> int:
    """Encode dB gain to protocol byte: -3.0→0, 0→6, +3.0→12."""
    return max(0, min(12, round(gain / EQ_GAIN_STEP) + EQ_GAIN_OFFSET))


def byte_to_gain(b: int) -> float:
    """Decode protocol byte to dB gain."""
    return (b - EQ_GAIN_OFFSET) * EQ_GAIN_STEP


def format_freq(freq: int) -> str:
    """Format frequency for display."""
    return f"{freq / 1000:.1f}k" if freq >= 1000 else str(freq)


def format_hex(data: bytes) -> str:
    """Format bytes as hex string."""
    return " ".join(f"{b:02X}" for b in data)


def is_edifier_device(name: str) -> bool:
    """Check if device name matches known Edifier patterns."""
    lower = (name or "").lower()
    return any(kw in lower for kw in EDIFIER_KEYWORDS)


async def prompt_input(message: str) -> str:
    """Non-blocking input prompt."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(message).strip())


# ============================================================
# Device Profiles
# ============================================================


@dataclass
class DeviceProfile:
    model: str
    eq_num_bands: int
    default_freqs: List[int]
    supports_codec_query: bool = True
    supports_ldac: bool = False
    supports_active_speaker: bool = False
    supports_prompt_tone: bool = True
    supports_advanced_audio: bool = True
    is_known: bool = True


MR5BT_PROFILE = DeviceProfile(
    model="MR5BT",
    eq_num_bands=9,
    default_freqs=MR5_DEFAULT_FREQS,
    supports_codec_query=True,
    supports_ldac=True,
    supports_active_speaker=True,
    supports_prompt_tone=True,
    supports_advanced_audio=True,
    is_known=True,
)

MR3BT_PROFILE = DeviceProfile(
    model="MR3BT",
    eq_num_bands=6,
    default_freqs=MR3_DEFAULT_FREQS,
    supports_codec_query=False,
    supports_ldac=False,
    supports_active_speaker=False,
    supports_prompt_tone=True,
    supports_advanced_audio=True,
    is_known=True,
)

UNKNOWN_PROFILE = DeviceProfile(
    model="Unknown",
    eq_num_bands=9,
    default_freqs=MR5_DEFAULT_FREQS,
    supports_codec_query=False,
    supports_ldac=False,
    supports_active_speaker=False,
    supports_prompt_tone=True,
    supports_advanced_audio=False,
    is_known=False,
)


def detect_device_profile(name: str) -> DeviceProfile:
    lower = (name or "").lower()
    if "mr3" in lower:
        return MR3BT_PROFILE
    if "mr5" in lower:
        return MR5BT_PROFILE
    return UNKNOWN_PROFILE


def profile_from_name_and_support(name: str, support_eq_band_count: int) -> DeviceProfile:
    detected = detect_device_profile(name)
    if detected.is_known:
        return detected

    if support_eq_band_count == 6:
        return DeviceProfile(
            model="Unknown(6-band)",
            eq_num_bands=6,
            default_freqs=MR3_DEFAULT_FREQS,
            supports_codec_query=False,
            supports_ldac=False,
            supports_active_speaker=False,
            supports_prompt_tone=True,
            supports_advanced_audio=False,
            is_known=False,
        )
    if support_eq_band_count == 9:
        return DeviceProfile(
            model="Unknown(9-band)",
            eq_num_bands=9,
            default_freqs=MR5_DEFAULT_FREQS,
            supports_codec_query=False,
            supports_ldac=False,
            supports_active_speaker=False,
            supports_prompt_tone=True,
            supports_advanced_audio=False,
            is_known=False,
        )
    return UNKNOWN_PROFILE


# ============================================================
# Data Models
# ============================================================


@dataclass
class PEQBand:
    """A single parametric EQ band."""

    index: int
    frequency: int
    gain: float

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "frequency": self.frequency, "gain": self.gain}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PEQBand":
        return cls(d["index"], d["frequency"], float(d["gain"]))

    def __repr__(self) -> str:
        freq_label = (
            f"{self.frequency / 1000:.1f}kHz" if self.frequency >= 1000 else f"{self.frequency}Hz"
        )
        return f"Band[{self.index}] {freq_label:>7} {self.gain:+4.1f}dB"


@dataclass
class EQPreset:
    """Named collection of EQ bands."""

    name: str
    bands: List[PEQBand]
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "bands": [b.to_dict() for b in self.bands],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EQPreset":
        return cls(
            d["name"],
            [PEQBand.from_dict(b) for b in d["bands"]],
            d.get("description", ""),
        )


@dataclass
class DeviceState:
    """Complete device state."""

    name: str = ""
    firmware: str = ""
    address: str = ""
    connected: bool = False
    model: str = "Unknown"

    max_volume: int = 30
    current_volume: int = 0
    audio_codec: int = 0
    device_state: int = 0

    eq_band_count: int = 0
    eq_type: int = 0
    eq_preset_name: str = ""

    low_cut_freq: int = 60
    low_cut_slope: int = 0
    acoustic_space: int = 0
    desktop_mode: bool = False
    active_speaker: str = "Unknown"
    prompt_tone: bool = True
    input_source: int = 0

    @property
    def volume_percent(self) -> int:
        return round(self.current_volume / self.max_volume * 100) if self.max_volume else 0

    @property
    def codec_name(self) -> str:
        return CODEC_NAMES.get(self.audio_codec, f"0x{self.audio_codec:02X}")

    @property
    def slope_name(self) -> str:
        return SLOPE_FROM_BYTE.get(self.low_cut_slope, f"Unk({self.low_cut_slope})")

    @property
    def space_label(self) -> str:
        return f"-{self.acoustic_space}dB" if self.acoustic_space > 0 else "0dB"


# ============================================================
# Packet Protocol
# ============================================================


class Packet:
    """Build and parse BLE protocol packets."""

    @staticmethod
    def checksum(data: bytes) -> int:
        return sum(data) & 0xFF

    @staticmethod
    def build(cmd: int, payload: bytes = b"") -> bytes:
        length = len(payload)
        header = bytes(
            [
                HEADER_TX,
                APP_CODE,
                cmd & 0xFF,
                (length >> 8) & 0xFF,
                length & 0xFF,
            ]
        )
        pkt = header + payload
        return pkt + bytes([Packet.checksum(pkt)])

    @staticmethod
    def parse(data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) < 6 or data[0] != HEADER_RX:
            return None

        cmd = data[2]
        length = (data[3] << 8) | data[4]
        expected_len = 5 + length + 1

        if len(data) != expected_len:
            return {
                "cmd": cmd,
                "length": length,
                "payload": data[5:-1] if len(data) > 6 else b"",
                "crc_ok": False,
                "raw": data,
                "length_ok": False,
            }

        payload = data[5 : 5 + length]
        crc_ok = data[-1] == Packet.checksum(data[:-1])
        return {
            "cmd": cmd,
            "length": length,
            "payload": payload,
            "crc_ok": crc_ok,
            "raw": data,
            "length_ok": True,
        }


# ============================================================
# EQ Response Parser
# ============================================================


def parse_eq_response(payload: bytes) -> Tuple[List[PEQBand], str, int]:
    """Parse EQ query response into bands, preset name, and EQ type."""
    if len(payload) < 2:
        return [], "", 0

    eq_type = payload[0]
    num_bands = payload[1]
    bands = []
    offset = 2

    for i in range(num_bands):
        if offset >= len(payload):
            break

        idx = payload[offset]
        offset += 1

        if i == 0:
            if offset + 3 > len(payload):
                break
            offset += 1  # observed first-band padding on captured traffic

        if offset + 2 > len(payload):
            break
        freq = (payload[offset] << 8) | payload[offset + 1]
        offset += 2

        if offset >= len(payload):
            break
        gain_db = byte_to_gain(payload[offset])
        offset += 1

        bands.append(PEQBand(idx, freq, gain_db))

    name = _extract_ascii_name(payload[offset:]) if offset < len(payload) else ""
    return bands, name, eq_type


def _extract_ascii_name(data: bytes) -> str:
    """Extract the first plausible ASCII string (≥3 chars) from raw bytes."""
    ascii_start = -1
    for j, byte in enumerate(data):
        if 32 <= byte < 127:
            if ascii_start < 0:
                ascii_start = j
        else:
            if ascii_start >= 0 and j - ascii_start >= 3:
                break
            ascii_start = -1

    if ascii_start < 0:
        return ""

    name_bytes = data[ascii_start:]
    end = next((j for j, b in enumerate(name_bytes) if b < 32 or b >= 127), len(name_bytes))
    return name_bytes[:end].decode("ascii", errors="replace")


# ============================================================
# BLE Scanner
# ============================================================


@dataclass
class ScanResult:
    """Wraps a discovered BLE device."""

    device: BLEDevice
    adv_data: Optional[AdvertisementData] = None

    @property
    def name(self) -> str:
        return (
            self.device.name or (self.adv_data.local_name if self.adv_data else None) or "Unknown"
        )

    @property
    def address(self) -> str:
        return self.device.address

    @property
    def rssi(self) -> int:
        if self.adv_data:
            return self.adv_data.rssi
        return getattr(self.device, "rssi", -999) or -999

    @property
    def rssi_label(self) -> str:
        return f"{self.rssi}dBm" if self.rssi != -999 else "?"


async def scan_ble(timeout: float = 10.0) -> List[ScanResult]:
    """Scan for BLE devices, returning unique results."""
    results: List[ScanResult] = []
    seen: set[str] = set()

    def on_detected(dev: BLEDevice, adv: AdvertisementData):
        if dev.address not in seen:
            seen.add(dev.address)
            results.append(ScanResult(dev, adv))

    scanner = BleakScanner(detection_callback=on_detected)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return results


def filter_edifier(devices: List[ScanResult]) -> List[ScanResult]:
    """Filter scan results to Edifier devices only."""
    return [d for d in devices if is_edifier_device(d.name)]


# ============================================================
# BLE Connection
# ============================================================


class BLEConnection:
    """Manages BLE connection, notifications, and command/response."""

    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.state = DeviceState()
        self._response: Optional[bytes] = None
        self._response_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def connect(self, address: str) -> bool:
        try:
            self.client = BleakClient(address)
            await self.client.connect()
            if self.client.is_connected:
                self.state.connected = True
                self.state.address = address
                await self.client.start_notify(BLE_UUID_NOTIFY, self._on_notification)
                return True
        except Exception as e:
            logger.error("Connect failed: %s", e)
        return False

    async def disconnect(self):
        if not self.client:
            return
        for action in [
            lambda: self.client.stop_notify(BLE_UUID_NOTIFY),
            lambda: self.client.disconnect(),
        ]:
            try:
                await action()
            except Exception:
                pass
        self.state.connected = False
        self.client = None

    def _on_notification(self, _sender: Any, data: bytearray):
        self._response = bytes(data)
        self._response_event.set()

    async def send(
        self,
        cmd: int,
        payload: bytes = b"",
        timeout: float = 3.0,
        expected_cmd: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._write_and_wait(
            Packet.build(cmd, payload),
            timeout=timeout,
            expected_cmd=(cmd if expected_cmd is None else expected_cmd),
        )

    async def send_raw(
        self, raw_packet: bytes, timeout: float = 3.0, expected_cmd: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        return await self._write_and_wait(raw_packet, timeout=timeout, expected_cmd=expected_cmd)

    async def _write_and_wait(
        self,
        data: bytes,
        timeout: float,
        expected_cmd: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.connected:
            return None

        async with self._send_lock:
            self._response_event.clear()
            self._response = None

            client = self.client
            if client is None:
                return None

            try:
                await client.write_gatt_char(BLE_UUID_WRITE, data)
            except Exception as e:
                logger.error("Write failed: %s", e)
                return None

            deadline = asyncio.get_running_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None

                try:
                    await asyncio.wait_for(self._response_event.wait(), remaining)
                except asyncio.TimeoutError:
                    return None

                raw = self._response
                self._response = None
                self._response_event.clear()

                if not raw:
                    continue

                parsed = Packet.parse(raw)
                if not parsed:
                    continue

                if expected_cmd is not None and parsed["cmd"] != (expected_cmd & 0xFF):
                    continue

                return parsed

    async def query(self, cmd: int, min_payload: int = 0) -> Optional[bytes]:
        r = await self.send(cmd)
        if r and r.get("crc_ok") and r.get("length_ok", True) and len(r["payload"]) >= min_payload:
            return r["payload"]
        return None


# ============================================================
# Device Initializer
# ============================================================


async def init_device_state(ble: BLEConnection):
    """Query available device properties and populate state."""
    state = ble.state

    support_eq_band_count = 0

    p = await ble.query(Cmd.SUPPORT_FUNC, 14)
    if p:
        if len(p) > 12:
            support_eq_band_count = p[12]
        if len(p) > 13:
            state.max_volume = p[13]
    await asyncio.sleep(0.05)

    p = await ble.query(Cmd.DEVICE_NAME_QUERY)
    if p:
        state.name = p.decode("utf-8", errors="replace")
    await asyncio.sleep(0.05)

    p = await ble.query(Cmd.VERSION_QUERY, 3)
    if p:
        state.firmware = f"{p[0]}.{p[1]}.{p[2]}"
    await asyncio.sleep(0.05)

    profile = profile_from_name_and_support(state.name, support_eq_band_count)
    state.model = profile.model

    if support_eq_band_count > 0:
        state.eq_band_count = support_eq_band_count
    else:
        state.eq_band_count = profile.eq_num_bands

    p = await ble.query(Cmd.VOLUME_QUERY, 2)
    if p:
        state.max_volume = p[0]
        state.current_volume = p[1]
    await asyncio.sleep(0.05)

    if profile.supports_codec_query:
        p = await ble.query(Cmd.AUDIO_CODEC, 1)
        if p:
            state.audio_codec = p[0]
    else:
        state.audio_codec = 0
    await asyncio.sleep(0.05)

    p = await ble.query(Cmd.DEVICE_STATE, 1)
    if p:
        state.device_state = p[0]
    await asyncio.sleep(0.05)

    if profile.supports_active_speaker:
        p = await ble.query(Cmd.ACTIVE_SPEAKER_QUERY, 2)
        if p:
            state.active_speaker = "Left" if p[1] == 2 else "Right"
    else:
        state.active_speaker = "N/A"
    await asyncio.sleep(0.05)

    if profile.supports_prompt_tone:
        p = await ble.query(Cmd.PROMPT_TONE_QUERY, 2)
        if p:
            state.prompt_tone = p[1] == 1
    await asyncio.sleep(0.05)

    if profile.supports_advanced_audio:
        p = await ble.query(Cmd.ADVANCED_QUERY, 7)
        if p and len(p) >= 7:
            state.low_cut_freq = (p[2] << 8) | p[3]
            state.low_cut_slope = p[4]
            state.acoustic_space = p[5]
            state.desktop_mode = p[6] == 1
    await asyncio.sleep(0.05)

    p = await ble.query(Cmd.INPUT_SOURCE_QUERY, 1)
    if p:
        state.input_source = p[0]
    await asyncio.sleep(0.05)


# ============================================================
# Built-in Presets
# ============================================================


def _make_preset(name: str, freqs: List[int], gains: List[float], desc: str = "") -> EQPreset:
    bands = [PEQBand(i, freqs[i], gains[i]) for i in range(len(freqs))]
    return EQPreset(name, bands, desc)


BUILTIN_PRESETS_MR5: Dict[str, EQPreset] = {
    "flat": _make_preset("Flat", MR5_DEFAULT_FREQS, [0] * 9, "All bands 0 dB"),
    "bass_boost": _make_preset(
        "Bass Boost", MR5_DEFAULT_FREQS, [3, 2.5, 1.5, 0.5, 0, 0, 0, 0, 0], "Enhanced bass"
    ),
    "treble_boost": _make_preset(
        "Treble Boost", MR5_DEFAULT_FREQS, [0, 0, 0, 0, 0.5, 1, 1.5, 2.5, 3], "Enhanced treble"
    ),
    "vocal": _make_preset(
        "Vocal", MR5_DEFAULT_FREQS, [-1, -0.5, 0, 1, 1.5, 1.5, 1, 0.5, 0], "Vocal clarity"
    ),
    "v_shape": _make_preset(
        "V-Shape", MR5_DEFAULT_FREQS, [2.5, 2, 1, 0, -1, -0.5, 0.5, 2, 2.5], "V-shaped curve"
    ),
    "warm": _make_preset(
        "Warm", MR5_DEFAULT_FREQS, [2, 1.5, 1, 0.5, 0, -0.5, -0.5, -1, -1], "Warm relaxed sound"
    ),
    "bright": _make_preset(
        "Bright",
        MR5_DEFAULT_FREQS,
        [-0.5, -0.5, 0, 0, 0.5, 1, 1.5, 1, 0.5],
        "Bright detailed sound",
    ),
    "studio": _make_preset(
        "Studio", MR5_DEFAULT_FREQS, [-0.5, 0, 0, 0, 0, 0.5, 0, -0.5, -1], "Neutral studio monitor"
    ),
    "loudness": _make_preset(
        "Loudness", MR5_DEFAULT_FREQS, [3, 2, 1, 0, -0.5, 0.5, 1.5, 2, 3], "Loudness compensation"
    ),
    "mid_scoop": _make_preset(
        "Mid Scoop", MR5_DEFAULT_FREQS, [1.5, 1, 0.5, -0.5, -1.5, -0.5, 0.5, 1, 1.5], "Scooped mids"
    ),
    "podcast": _make_preset(
        "Podcast", MR5_DEFAULT_FREQS, [-1.5, -1, 0, 1, 1.5, 1.5, 0.5, 0, -0.5], "Speech clarity"
    ),
    "sub_boost": _make_preset(
        "Sub Boost", MR5_DEFAULT_FREQS, [3, 3, 1.5, 0.5, 0, 0, 0, 0, 0], "Maximum sub-bass"
    ),
    "monitor": _make_preset(
        "Monitor", MR5_DEFAULT_FREQS, [-0.5, 0, 0, 0, 0, 0.5, 0, -0.5, -1], "Neutral studio monitor"
    ),
}

BUILTIN_PRESETS_MR3: Dict[str, EQPreset] = {
    "flat": _make_preset("Flat", MR3_DEFAULT_FREQS, [0, 0, 0, 0, 0, 0], "All bands 0 dB"),
    "bass_boost": _make_preset(
        "Bass Boost", MR3_DEFAULT_FREQS, [3, 2, 1, 0, 0, 0], "Enhanced bass"
    ),
    "treble_boost": _make_preset(
        "Treble Boost", MR3_DEFAULT_FREQS, [0, 0, 0, 0.5, 2, 3], "Enhanced treble"
    ),
    "vocal": _make_preset(
        "Vocal", MR3_DEFAULT_FREQS, [-1, -0.5, 0.5, 1.5, 1.5, 0.5], "Vocal clarity"
    ),
    "warm": _make_preset("Warm", MR3_DEFAULT_FREQS, [2, 1, 0.5, 0, -0.5, -1], "Warm relaxed sound"),
    "bright": _make_preset(
        "Bright", MR3_DEFAULT_FREQS, [-0.5, 0, 0, 0.5, 1.5, 1.5], "Bright detailed sound"
    ),
    "studio": _make_preset("Studio", MR3_DEFAULT_FREQS, [0, 0, 0, 0, -0.5, -1], "Neutral response"),
    "loudness": _make_preset(
        "Loudness", MR3_DEFAULT_FREQS, [3, 2, 0.5, 0, 1.5, 2.5], "Loudness compensation"
    ),
    "podcast": _make_preset(
        "Podcast", MR3_DEFAULT_FREQS, [-1.5, -0.5, 0.5, 1.5, 1.0, 0], "Speech clarity"
    ),
    "monitor": _make_preset(
        "Monitor", MR3_DEFAULT_FREQS, [0, 0, 0, 0, -0.5, -1], "Neutral monitor"
    ),
}


# ============================================================
# Preset Manager
# ============================================================


class PresetManager:
    """Manages built-in and user presets with file persistence."""

    def __init__(self):
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        self.user_presets: Dict[str, EQPreset] = {}
        self._load_all()

    def _load_all(self):
        for filepath in PRESETS_DIR.glob("*.json"):
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                preset = EQPreset.from_dict(data)
                self.user_presets[preset.name.lower()] = preset
            except Exception as e:
                print(f"  Warning loading {filepath.name}: {e}")

    def save(self, preset: EQPreset):
        key = preset.name.lower().replace(" ", "_")
        path = PRESETS_DIR / f"{key}.json"
        path.write_text(
            json.dumps(preset.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.user_presets[preset.name.lower()] = preset
        print(f"  Saved '{preset.name}' → {path}")

    def delete(self, name: str) -> bool:
        key = name.lower()
        if key not in self.user_presets:
            return False
        filepath = PRESETS_DIR / f"{key.replace(' ', '_')}.json"
        if filepath.exists():
            filepath.unlink()
        del self.user_presets[key]
        return True

    def get_builtin_map(self, profile: DeviceProfile) -> Dict[str, EQPreset]:
        return BUILTIN_PRESETS_MR3 if profile.eq_num_bands == 6 else BUILTIN_PRESETS_MR5

    def get_for_profile(self, name: str, profile: DeviceProfile) -> Optional[EQPreset]:
        key = name.lower()
        return self.get_builtin_map(profile).get(key) or self.user_presets.get(key)

    def list_all_for_profile(self, profile: DeviceProfile) -> List[Tuple[str, EQPreset, bool]]:
        builtin_map = self.get_builtin_map(profile)
        builtin = [(k, p, True) for k, p in builtin_map.items()]
        user = [
            (k, p, False)
            for k, p in self.user_presets.items()
            if len(p.bands) == profile.eq_num_bands
        ]
        return builtin + user

    def fuzzy_match_for_profile(self, query: str, profile: DeviceProfile) -> List[str]:
        all_keys = {k for k, _, _ in self.list_all_for_profile(profile)}
        return [k for k in all_keys if query.lower() in k]

    def export_file(self, name: str, path: str, profile: DeviceProfile) -> bool:
        preset = self.get_for_profile(name, profile)
        if not preset:
            return False
        Path(path).write_text(
            json.dumps(preset.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True

    def import_file(self, path: str) -> Optional[EQPreset]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            preset = EQPreset.from_dict(data)
            self.save(preset)
            return preset
        except Exception as e:
            print(f"  Import failed: {e}")
            return None


# ============================================================
# Display Utilities
# ============================================================


class Display:
    """Text-based UI rendering for EQ curves, volume bars, etc."""

    @staticmethod
    def device_info(state: DeviceState):
        vol_str = f"{state.current_volume}/{state.max_volume} ({state.volume_percent}%)"
        rows = [
            ("Model", state.model),
            ("Name", state.name),
            ("Address", state.address),
            ("Firmware", state.firmware),
            ("Volume", vol_str),
            ("Codec", state.codec_name),
            ("Speaker", state.active_speaker),
            ("Low Cut", f"{state.low_cut_freq}Hz / {state.slope_name}"),
            ("Space", state.space_label),
            ("Desktop", "On" if state.desktop_mode else "Off"),
            ("Prompt", "On" if state.prompt_tone else "Off"),
            ("Source", INPUT_SOURCE_NAMES.get(state.input_source, f"0x{state.input_source:02X}")),
            ("EQ", f"{state.eq_band_count}-band, -3..+3 dB, step 0.5"),
        ]
        if state.eq_preset_name:
            rows.append(("Preset", state.eq_preset_name))

        print()
        print("  +-- Device Info ---------------------------------+")
        for label, value in rows:
            text = str(value)
            if len(text) > 38:
                text = text[:35] + "..."
            print(f"  | {label + ':':<11}{text:<38}|")
        print("  +------------------------------------------------+")
        print()

    @staticmethod
    def eq_curve(bands: List[PEQBand]):
        if not bands:
            print("  No EQ data")
            return

        sorted_bands = sorted(bands, key=lambda b: b.index)
        col_width = 7

        print()
        print("  +-- EQ Curve " + "-" * (len(sorted_bands) * col_width + 2) + "+")

        for half_step in range(6, -7, -1):
            db = half_step * 0.5
            label = f"  |{db:+4.1f}|"
            cells = []
            for b in sorted_bands:
                g_steps = round(b.gain / 0.5)
                if half_step == 0:
                    cells.append("---O---" if abs(b.gain) < 1e-9 else "-------")
                elif (half_step > 0 and g_steps >= half_step) or (
                    half_step < 0 and g_steps <= half_step
                ):
                    cells.append("  ###  ")
                else:
                    cells.append("       ")
            print(f"{label}{''.join(cells)}|")

        print("  |    +" + "-" * (len(sorted_bands) * col_width) + "+")
        print("  | Hz:" + "".join(f" {format_freq(b.frequency):>5} " for b in sorted_bands))
        print("  | dB:" + "".join(f" {b.gain:+4.1f} " for b in sorted_bands))
        print("  +" + "-" * (len(sorted_bands) * col_width + 6) + "+")
        print()

    @staticmethod
    def volume_bar(current: int, maximum: int):
        if maximum <= 0:
            return
        pct = current / maximum
        filled = round(pct * 30)
        bar = "#" * filled + "." * (30 - filled)
        print(f"  Volume: [{bar}] {current}/{maximum} ({round(pct * 100)}%)")

    @staticmethod
    def band_detail(band: PEQBand):
        center = 6
        pos = max(0, min(12, center + round(band.gain * 2)))
        line = list(".............")
        line[center] = "|"
        line[pos] = "O"

        fill_range = range(center + 1, pos) if pos > center else range(pos + 1, center)
        for i in fill_range:
            line[i] = "="

        freq_str = (
            f"{band.frequency / 1000:.1f}kHz" if band.frequency >= 1000 else f"{band.frequency}Hz"
        )
        print(
            f"  Band {band.index}: {freq_str:>8} "
            f"[{''.join(line)}] {band.gain:+4.1f}dB (0x{gain_to_byte(band.gain):02X})"
        )


# ============================================================
# Controller
# ============================================================


class MRController:
    """High-level controller for the Edifier MR series speakers."""

    def __init__(self):
        self.ble = BLEConnection()
        self.presets = PresetManager()
        self.profile: DeviceProfile = UNKNOWN_PROFILE
        self.bands = [
            PEQBand(i, self.profile.default_freqs[i], 0.0) for i in range(self.profile.eq_num_bands)
        ]

    @property
    def state(self) -> DeviceState:
        return self.ble.state

    async def connect(self, address: Optional[str] = None) -> bool:
        if address:
            return await self._connect_by_address(address)
        return await self._connect_by_scan()

    async def _connect_by_address(self, address: str) -> bool:
        print(f"  Connecting to {address}...")
        if await self.ble.connect(address):
            await self._post_connect()
            return True
        return False

    async def _connect_by_scan(self) -> bool:
        print("  Scanning (10s)...")
        all_devices = await scan_ble(10.0)
        edifier_devices = filter_edifier(all_devices)

        if not edifier_devices:
            print("  No Edifier devices found!")
            self._print_all_devices(all_devices)
            return False

        device = await self._select_device(edifier_devices)
        print(f"\n  Connecting to {device.name}...")
        if await self.ble.connect(device.address):
            await self._post_connect()
            return True
        return False

    async def _post_connect(self):
        print("  Connected! Reading device info...")
        await init_device_state(self.ble)

        self.profile = profile_from_name_and_support(self.state.name, self.state.eq_band_count)
        self.state.model = self.profile.model

        self.bands = [
            PEQBand(i, self.profile.default_freqs[i], 0.0) for i in range(self.profile.eq_num_bands)
        ]

        Display.device_info(self.state)
        await self.query_eq()

    @staticmethod
    def _print_all_devices(devices: List[ScanResult]):
        named = [d for d in devices if d.name != "Unknown"]
        if named:
            print("  All BLE devices:")
            for d in named:
                print(f"    {d.name} [{d.address}] RSSI={d.rssi_label}")

    @staticmethod
    async def _select_device(devices: List[ScanResult]) -> ScanResult:
        print()
        for i, d in enumerate(devices):
            print(f"  [{i}] {d.name} [{d.address}] RSSI={d.rssi_label}")

        if len(devices) == 1:
            return devices[0]

        try:
            choice = await prompt_input(f"  Select [0-{len(devices) - 1}]: ")
            return devices[int(choice) if choice else 0]
        except (ValueError, IndexError):
            return devices[0]

    async def disconnect(self):
        await self.ble.disconnect()
        print("  Disconnected")

    # ---- Helpers ----

    def _make_default_bands(self) -> List[PEQBand]:
        return [
            PEQBand(i, self.profile.default_freqs[i], 0.0) for i in range(self.profile.eq_num_bands)
        ]

    def _ensure_full_band_list(self):
        if len(self.bands) == self.profile.eq_num_bands:
            return

        full = self._make_default_bands()
        for b in self.bands:
            if 0 <= b.index < self.profile.eq_num_bands:
                full[b.index] = b
        self.bands = full

    # ---- Volume ----

    async def get_volume(self) -> Tuple[int, int]:
        p = await self.ble.query(Cmd.VOLUME_QUERY, 2)
        if p:
            self.state.max_volume = p[0]
            self.state.current_volume = p[1]
        return self.state.current_volume, self.state.max_volume

    async def set_volume(self, vol: int) -> bool:
        vol = max(0, min(self.state.max_volume, vol))
        r = await self.ble.send(Cmd.VOLUME_SET, bytes([vol]))
        if r and r.get("crc_ok") and r.get("length_ok", True):
            self.state.current_volume = vol
            return True
        return False

    async def set_volume_percent(self, pct: float) -> bool:
        return await self.set_volume(round(pct / 100 * self.state.max_volume))

    # ---- EQ ----

    def _ensure_band_exists(self, index: int):
        while len(self.bands) <= index:
            freq = (
                self.profile.default_freqs[len(self.bands)]
                if len(self.bands) < len(self.profile.default_freqs)
                else 1000
            )
            self.bands.append(PEQBand(len(self.bands), freq, 0.0))

    async def set_band(self, index: int, freq: int, gain_db: float) -> bool:
        if not (0 <= index < self.profile.eq_num_bands):
            print(f"  Invalid band index for {self.profile.model}: {index}")
            return False

        freq = max(20, min(20000, freq))
        gain_db = snap_gain(gain_db)
        payload = bytes(
            [
                0x00,
                index & 0xFF,
                (freq >> 8) & 0xFF,
                freq & 0xFF,
                gain_to_byte(gain_db) & 0xFF,
            ]
        )
        r = await self.ble.send(Cmd.SINGLE_EQ_SET, payload)
        if r and r.get("crc_ok") and r.get("length_ok", True):
            self._ensure_band_exists(index)
            self.bands[index] = PEQBand(index, freq, gain_db)
            return True
        return False

    async def apply_preset(self, preset: EQPreset, delay: float = 0.12) -> bool:
        if len(preset.bands) != self.profile.eq_num_bands:
            print(
                f"  Preset '{preset.name}' has {len(preset.bands)} bands, device requires {self.profile.eq_num_bands}"
            )
            return False

        print(f"  Applying '{preset.name}'...")
        for band in preset.bands:
            if not await self.set_band(band.index, band.frequency, band.gain):
                print(f"  Failed at band {band.index}")
                return False
            await asyncio.sleep(delay)
        self.bands = list(preset.bands)
        return True

    async def reset_eq(self) -> bool:
        r = await self.ble.send(Cmd.CUSTOM_EQ_RESET)
        if r and r.get("crc_ok") and r.get("length_ok", True):
            bands = await self.query_eq()
            if not bands:
                self.bands = self._make_default_bands()
            return True
        return False

    async def query_eq(self) -> Optional[List[PEQBand]]:
        r = await self.ble.send(Cmd.CUSTOM_EQ_GET)
        if r and r.get("crc_ok") and r.get("length_ok", True):
            bands, name, eq_type = parse_eq_response(r["payload"])
            if bands:
                full = self._make_default_bands()
                seen = set()
                for b in bands:
                    if 0 <= b.index < self.profile.eq_num_bands and b.index not in seen:
                        full[b.index] = b
                        seen.add(b.index)
                self.bands = full
                self.state.eq_type = eq_type
                self.state.eq_preset_name = name
                self.state.eq_band_count = self.profile.eq_num_bands
                return full
        return None

    # ---- Advanced Audio ----

    async def _write_advanced_settings(self) -> bool:
        if not self.profile.supports_advanced_audio:
            print("  Not supported on this model")
            return False

        s = self.state
        payload = bytes(
            [
                0x00,
                0x02,
                (s.low_cut_freq >> 8) & 0xFF,
                s.low_cut_freq & 0xFF,
                s.low_cut_slope,
                s.acoustic_space,
                1 if s.desktop_mode else 0,
            ]
        )
        r = await self.ble.send(Cmd.EQ_SET, payload)
        return bool(r and r.get("crc_ok") and r.get("length_ok", True))

    async def set_low_cut(self, freq: int, slope_db: int) -> bool:
        if slope_db not in SLOPE_TO_BYTE:
            print(f"  Invalid slope. Use one of: {list(SLOPE_TO_BYTE.keys())}")
            return False
        if not (20 <= freq <= 20000):
            print("  Invalid frequency. Use 20..20000 Hz")
            return False
        self.state.low_cut_freq = freq
        self.state.low_cut_slope = SLOPE_TO_BYTE[slope_db]
        if await self._write_advanced_settings():
            print(f"  Low Cut set to {freq}Hz {slope_db}dB/oct.")
            return True
        print("  Failed.")
        return False

    async def set_acoustic_space(self, db: int) -> bool:
        allowed = {0, 2, 4, 6}
        value = abs(db)
        if value not in allowed:
            print(
                f"  Invalid acoustic space value. Use one of: {sorted([-x for x in allowed])} or {sorted(allowed)}"
            )
            return False
        self.state.acoustic_space = value
        if await self._write_advanced_settings():
            print(f"  Acoustic Space set to -{value}dB.")
            return True
        print("  Failed.")
        return False

    async def set_desktop_mode(self, enabled: bool) -> bool:
        self.state.desktop_mode = enabled
        if await self._write_advanced_settings():
            print(f"  Desktop Mode {'On' if enabled else 'Off'}.")
            return True
        print("  Failed.")
        return False

    async def set_active_speaker(self, side: str) -> bool:
        if not self.profile.supports_active_speaker:
            print("  Active speaker selection is not supported on this model")
            return False

        side_l = side.lower()
        if side_l not in ("left", "right"):
            print("  Invalid side. Use: left | right")
            return False

        val = 2 if side_l == "left" else 1
        r = await self.ble.send(Cmd.ACTIVE_SPEAKER_SET, bytes([0x04, val]))
        if r and r.get("crc_ok") and r.get("length_ok", True):
            self.state.active_speaker = "Left" if val == 2 else "Right"
            print(f"  Active speaker set to: {self.state.active_speaker}")
            return True
        return False

    async def set_ldac(self, mode_str: str) -> bool:
        if not self.profile.supports_ldac:
            print("  LDAC is not supported on this model")
            return False

        mode_l = mode_str.lower()
        if mode_l not in LDAC_MODES:
            print(f"  Invalid LDAC mode. Use one of: {', '.join(LDAC_MODES.keys())}")
            return False

        val = LDAC_MODES[mode_l]
        r = await self.ble.send(Cmd.LDAC_CFG_SET, bytes([0x0F, val]))
        if r and r.get("crc_ok") and r.get("length_ok", True):
            print(f"  LDAC sent ({mode_str}). Device may reboot.")
            return True
        return False

    async def set_prompt_tone(self, enable: bool) -> bool:
        if not self.profile.supports_prompt_tone:
            print("  Prompt tone control is not supported on this model")
            return False
        r = await self.ble.send(Cmd.PROMPT_TONE_SET, bytes([0x02, 0x01, int(enable)]))
        if r and r.get("crc_ok") and r.get("length_ok", True):
            self.state.prompt_tone = enable
            print(f"  Prompt tones: {'On' if enable else 'Off'}")
            return True
        return False

    # ---- System ----

    async def query_input_source(self) -> Optional[int]:
        p = await self.ble.query(Cmd.INPUT_SOURCE_QUERY, 1)
        if p:
            self.state.input_source = p[0]
            return p[0]
        return None

    async def shutdown_device(self) -> bool:
        r = await self.ble.send(Cmd.SHUTDOWN)
        return bool(r and r.get("crc_ok") and r.get("length_ok", True))


# ============================================================
# CLI
# ============================================================


class CLI:
    """Interactive command-line interface for MR speaker control."""

    BANNER = """
+==============================================================+
|   EDIFIER MR Speaker - BLE Control Tool v2.3                 |
|                                                              |
|   Supports: MR5BT / MR3BT                                    |
|   EQ: model-dependent bands, -3.0..+3.0 dB, step 0.5 dB      |
|   Protocol: verified from real BLE capture                   |
+==============================================================+
"""

    HELP_TEXT = """
+-- Commands ---------------------------------------------------+
|                                                                 |
|  VOLUME                                                         |
|    vol                    Show current volume                   |
|    vol <0-30>             Set volume (absolute)                 |
|    vol <0-100>%           Set volume (percent)                  |
|    vol +/-<n>             Adjust volume                         |
|                                                                 |
|  PARAMETRIC EQ  (model-dependent bands, -3.0..+3.0 dB)         |
|    show                   Show current EQ curve                 |
|    band <n> <freq> <gain> Set band n                            |
|      Example: band 0 500 +1.5                                   |
|    eq <n> <gain>          Change gain only (keep freq)          |
|      Example: eq 3 -2.0                                         |
|    flat                   All bands to 0 dB                     |
|    reset                  Hardware EQ reset                     |
|    query                  Read EQ from device                   |
|                                                                 |
|  PRESETS                                                        |
|    list                   List all presets                      |
|    preset <name>          Apply preset                          |
|    save <name>            Save current as preset                |
|    delete <name>          Delete user preset                    |
|    export <name> <file>   Export to JSON                        |
|    import <file>          Import from JSON                      |
|                                                                 |
|  ADVANCED AUDIO                                                 |
|    lowcut <freq> <slope>  Ex: 'lowcut 100 24'                   |
|      Slopes: 6, 12, 18, 24 dB/oct                               |
|    space <dB>             Ex: 'space -4' or 'space 0'           |
|    desktop <on|off>       Toggle desktop mode                   |
|    speaker <left|right>   Set active speaker (MR5BT)            |
|    ldac <off|48k|96k>     Set LDAC mode (MR5BT)                 |
|    prompt <on|off>        Toggle beeps                          |
|                                                                 |
|  SYSTEM                                                         |
|    info                   Device information                    |
|    source                 Input source                          |
|    shutdown               Power off                             |
|    raw <hex>              Send raw command                      |
|    exit                   Quit                                  |
|                                                                 |
|  Shortcuts: v=vol b=band s=show p=preset l=list q=query x=exit  |
+-----------------------------------------------------------------+
"""

    def __init__(self):
        self.ctrl = MRController()
        self.running = True
        self._commands = self._build_command_table()

    def _build_command_table(self) -> Dict[str, Callable]:
        handlers = {
            self._help: ("help", "h", "?"),
            self._info: ("info", "status"),
            self._exit: ("exit", "quit", "x"),
            self._vol: ("vol", "volume", "v"),
            self._eq: ("eq",),
            self._show: ("show", "s"),
            self._band: ("band", "b"),
            self._flat: ("flat",),
            self._reset: ("reset",),
            self._query: ("query", "q"),
            self._preset: ("preset", "p"),
            self._save: ("save",),
            self._list: ("list", "l"),
            self._export: ("export",),
            self._import: ("import",),
            self._delete: ("delete",),
            self._lowcut: ("lowcut", "lc"),
            self._space: ("space", "as"),
            self._desktop: ("desktop", "d"),
            self._speaker: ("speaker",),
            self._ldac: ("ldac",),
            self._prompt: ("prompt",),
            self._source: ("source",),
            self._shutdown: ("shutdown",),
            self._raw: ("raw",),
        }
        table = {}
        for handler, aliases in handlers.items():
            for alias in aliases:
                table[alias] = handler
        return table

    def _band_range_label(self) -> str:
        return f"0-{self.ctrl.profile.eq_num_bands - 1}"

    async def run(self, address: Optional[str] = None):
        print(self.BANNER)
        if not await self.ctrl.connect(address):
            print("\n  Failed to connect.")
            return

        print("  Type 'help' for commands\n")

        while self.running and self.ctrl.ble.connected:
            try:
                line = await prompt_input("MR> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue

            try:
                await self._dispatch(line)
            except Exception as e:
                print(f"  Error: {e}")

        await self.ctrl.disconnect()

    async def _dispatch(self, line: str):
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"  Parse error: {e}")
            return

        if not parts:
            return

        cmd_name = parts[0].lower()
        args = parts[1:]

        handler = self._commands.get(cmd_name)
        if handler:
            await handler(args)
        else:
            print(f"  Unknown: '{cmd_name}'. Type 'help'.")

    # ---- Basic Commands ----

    async def _help(self, _args: List[str]):
        print(self.HELP_TEXT)

    async def _info(self, _args: List[str]):
        await init_device_state(self.ctrl.ble)
        self.ctrl.profile = profile_from_name_and_support(
            self.ctrl.state.name, self.ctrl.state.eq_band_count
        )
        self.ctrl.state.model = self.ctrl.profile.model
        self.ctrl.state.eq_band_count = (
            self.ctrl.profile.eq_num_bands
            if self.ctrl.state.eq_band_count <= 0
            else self.ctrl.state.eq_band_count
        )
        Display.device_info(self.ctrl.state)

    async def _exit(self, _args: List[str]):
        self.running = False

    # ---- Volume ----

    async def _vol(self, args: List[str]):
        if not args:
            cur, mx = await self.ctrl.get_volume()
            Display.volume_bar(cur, mx)
            return

        val = args[0]
        try:
            if val.endswith("%"):
                ok = await self.ctrl.set_volume_percent(float(val[:-1]))
            elif val and val[0] in "+-" and len(val) > 1:
                cur, mx = await self.ctrl.get_volume()
                ok = await self.ctrl.set_volume(max(0, min(mx, cur + int(val))))
            else:
                v = int(val)
                if v > self.ctrl.state.max_volume and v <= 100:
                    ok = await self.ctrl.set_volume_percent(v)
                else:
                    ok = await self.ctrl.set_volume(v)
        except ValueError:
            print("  Invalid volume value")
            return

        if ok:
            cur, mx = await self.ctrl.get_volume()
            Display.volume_bar(cur, mx)
        else:
            print("  Failed")

    # ---- EQ ----

    async def _band(self, args: List[str]):
        if len(args) < 3:
            print(f"  Usage: band <{self._band_range_label()}> <freq> <gain>")
            print("  Example: band 0 500 +1.5")
            return

        try:
            idx, freq, gain = int(args[0]), int(args[1]), float(args[2])
        except ValueError:
            print("  Invalid number")
            return

        if not (0 <= idx < self.ctrl.profile.eq_num_bands):
            print(f"  Index: {self._band_range_label()}")
            return
        if not (20 <= freq <= 20000):
            print("  Freq: 20-20000")
            return
        if not (EQ_GAIN_MIN <= gain <= EQ_GAIN_MAX):
            print(f"  Gain: {EQ_GAIN_MIN}..{EQ_GAIN_MAX}")
            return

        if await self.ctrl.set_band(idx, freq, snap_gain(gain)):
            Display.band_detail(self.ctrl.bands[idx])
        else:
            print("  Failed")

    async def _eq(self, args: List[str]):
        if len(args) < 2:
            print(f"  Usage: eq <{self._band_range_label()}> <gain>")
            return

        try:
            idx, gain = int(args[0]), float(args[1])
        except ValueError:
            print("  Invalid number")
            return

        if not (0 <= idx < self.ctrl.profile.eq_num_bands):
            print(f"  Index: {self._band_range_label()}")
            return

        self.ctrl._ensure_full_band_list()
        band = self.ctrl.bands[idx]
        if await self.ctrl.set_band(idx, band.frequency, snap_gain(gain)):
            Display.band_detail(self.ctrl.bands[idx])
        else:
            print("  Failed")

    async def _show(self, _args: List[str]):
        await self.ctrl.get_volume()
        Display.eq_curve(self.ctrl.bands)
        for b in self.ctrl.bands:
            Display.band_detail(b)
        print()
        Display.volume_bar(self.ctrl.state.current_volume, self.ctrl.state.max_volume)

    async def _flat(self, _args: List[str]):
        preset = self.ctrl.presets.get_for_profile("flat", self.ctrl.profile)
        if preset and await self.ctrl.apply_preset(preset):
            Display.eq_curve(self.ctrl.bands)

    async def _reset(self, _args: List[str]):
        print("  EQ reset done" if await self.ctrl.reset_eq() else "  Reset failed")

    async def _query(self, _args: List[str]):
        print("  Reading EQ from device...")
        bands = await self.ctrl.query_eq()
        target = bands or self.ctrl.bands
        if not bands:
            print("  Using stored state:")
        Display.eq_curve(target)
        for b in target:
            Display.band_detail(b)

    # ---- Presets ----

    async def _preset(self, args: List[str]):
        if not args:
            print("  Usage: preset <name>. Use 'list'.")
            return

        name = " ".join(args)
        preset = self.ctrl.presets.get_for_profile(name, self.ctrl.profile)

        if not preset:
            matches = self.ctrl.presets.fuzzy_match_for_profile(name, self.ctrl.profile)
            if len(matches) == 1:
                preset = self.ctrl.presets.get_for_profile(matches[0], self.ctrl.profile)
            elif matches:
                print(f"  Did you mean: {', '.join(matches)}?")
                return
            else:
                print(f"  '{name}' not found. Use 'list'.")
                return

        if len(preset.bands) != self.ctrl.profile.eq_num_bands:
            print(
                f"  Preset '{preset.name}' has {len(preset.bands)} bands, "
                f"but device requires {self.ctrl.profile.eq_num_bands}."
            )
            return

        if await self.ctrl.apply_preset(preset):
            Display.eq_curve(self.ctrl.bands)

    async def _save(self, args: List[str]):
        if not args:
            print("  Usage: save <name>")
            return
        name = " ".join(args)
        self.ctrl.presets.save(EQPreset(name, list(self.ctrl.bands), "User"))

    async def _list(self, _args: List[str]):
        all_presets = self.ctrl.presets.list_all_for_profile(self.ctrl.profile)

        print("\n  +-- Presets ----------------------------------------+")
        print("  |  Built-in:                                        |")
        for key, preset, is_builtin in all_presets:
            if is_builtin:
                desc = preset.description[:28] if preset.description else ""
                print(f"  |    {key:<16} {desc:<32}|")

        user_presets = [(k, p) for k, p, b in all_presets if not b]
        if user_presets:
            print("  |  User:                                             |")
            for key, preset in user_presets:
                desc = preset.description[:28] if preset.description else ""
                print(f"  |    {key:<16} {desc:<32}|")

        dir_str = str(PRESETS_DIR)
        if len(dir_str) > 46:
            dir_str = "..." + dir_str[-43:]
        print(f"  |  Dir: {dir_str:<47}|")
        print("  +--------------------------------------------------+\n")

    async def _export(self, args: List[str]):
        if len(args) < 2:
            print("  Usage: export <name> <file>")
            return

        name = " ".join(args[:-1])
        path = args[-1]
        if not path.endswith(".json"):
            path += ".json"

        if self.ctrl.presets.export_file(name, path, self.ctrl.profile):
            print(f"  Exported → {path}")
        else:
            print(f"  '{name}' not found")

    async def _import(self, args: List[str]):
        if not args:
            print("  Usage: import <file.json>")
            return
        if not os.path.exists(args[0]):
            print(f"  Not found: {args[0]}")
            return
        preset = self.ctrl.presets.import_file(args[0])
        if preset:
            print(f"  Imported '{preset.name}'")

    async def _delete(self, args: List[str]):
        if not args:
            print("  Usage: delete <name>")
            return
        name = " ".join(args)
        builtin_map = self.ctrl.presets.get_builtin_map(self.ctrl.profile)
        if name.lower() in builtin_map:
            print("  Can't delete built-in preset")
        elif self.ctrl.presets.delete(name):
            print(f"  Deleted '{name}'")
        else:
            print(f"  '{name}' not found")

    # ---- Advanced Audio ----

    async def _lowcut(self, args: List[str]):
        if len(args) < 2:
            print("  Usage: lowcut <freq> <slope_db>  (e.g., lowcut 100 24)")
            return
        try:
            await self.ctrl.set_low_cut(int(args[0]), int(args[1]))
        except ValueError:
            print("  Error: Use integers (e.g., 100 24)")

    async def _space(self, args: List[str]):
        if not args:
            print("  Usage: space <dB> (e.g., 0 or -4)")
            return
        try:
            await self.ctrl.set_acoustic_space(int(args[0]))
        except ValueError:
            print("  Error: Use integer (0, -2, -4, -6)")

    async def _desktop(self, args: List[str]):
        if not args or args[0].lower() not in ("on", "off"):
            print("  Usage: desktop <on|off>")
            return
        await self.ctrl.set_desktop_mode(args[0].lower() == "on")

    async def _speaker(self, args: List[str]):
        if not self.ctrl.profile.supports_active_speaker:
            print("  Active speaker selection is not supported on this model")
            return
        if not args or args[0].lower() not in ("left", "right"):
            print("  Usage: speaker <left|right>")
            return
        await self.ctrl.set_active_speaker(args[0])

    async def _ldac(self, args: List[str]):
        if not self.ctrl.profile.supports_ldac:
            print("  LDAC is not supported on this model")
            return
        if not args or args[0].lower() not in LDAC_MODES:
            print("  Usage: ldac <off|48k|96k>")
            return
        await self.ctrl.set_ldac(args[0])

    async def _prompt(self, args: List[str]):
        if not args or args[0].lower() not in ("on", "off"):
            print("  Usage: prompt <on|off>")
            return
        await self.ctrl.set_prompt_tone(args[0].lower() == "on")

    # ---- System ----

    async def _source(self, _args: List[str]):
        source = await self.ctrl.query_input_source()
        if source is not None:
            print(f"  Source: {INPUT_SOURCE_NAMES.get(source, f'0x{source:02X}')}")
        else:
            print("  Query failed")

    async def _shutdown(self, _args: List[str]):
        try:
            confirm = await prompt_input("  Power off? (y/N): ")
        except (EOFError, KeyboardInterrupt):
            return
        if confirm.lower() == "y":
            ok = await self.ctrl.shutdown_device()
            print("  Shutdown sent" if ok else "  Shutdown failed")
            self.running = False if ok else self.running

    async def _raw(self, args: List[str]):
        if not args:
            print("  Usage: raw <hex>")
            return

        hex_str = "".join(args).replace(" ", "")
        if hex_str.lower().startswith("0x"):
            hex_str = hex_str[2:]

        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            print("  Bad hex")
            return

        print(f"  TX [{len(data)}]: {format_hex(data)}")
        r = await self.ctrl.ble.send_raw(data, expected_cmd=None)

        if r:
            crc_label = "OK" if r["crc_ok"] else "FAIL"
            len_label = "OK" if r.get("length_ok", True) else "FAIL"
            print(
                f"  RX: CMD=0x{r['cmd']:02X} [{format_hex(r['payload'])}] CRC={crc_label} LEN={len_label}"
            )
            print(f"  Raw: {format_hex(r['raw'])}")
        else:
            print("  Timeout")


# ============================================================
# Protocol Verification
# ============================================================


def verify() -> bool:
    """Verify packet building and gain encoding against captured traffic."""
    print("  Verification against captured MR traffic:")
    print("  " + "-" * 50)
    all_passed = True

    pkt = Packet.build(0x44, bytes([0x00, 0x00, 0x01, 0xF4, 0x0C]))
    expected = bytes.fromhex("AAEC440005000001F40CE0")
    if pkt == expected:
        print("  [OK] TX band 0 freq=500 gain=+3dB")
    else:
        print(f"  [FAIL] TX band 0: got {pkt.hex()}, expected {expected.hex()}")
        all_passed = False

    test_cases = [(-3.0, 0), (-2.5, 1), (0.0, 6), (2.5, 11), (3.0, 12)]
    for db, expected_byte in test_cases:
        result = gain_to_byte(db)
        if result == expected_byte:
            print(f"  [OK] {db:+4.1f}dB → 0x{expected_byte:02X}")
        else:
            print(f"  [FAIL] {db:+4.1f}dB → 0x{result:02X}, expected 0x{expected_byte:02X}")
            all_passed = False

    parsed = Packet.parse(bytes.fromhex("BBEC6600021E0F34"))
    if parsed and parsed["cmd"] == 0x66 and parsed["length"] == 2 and parsed["length_ok"]:
        print("  [OK] RX parse length validation")
    else:
        print("  [FAIL] RX parse length validation")
        all_passed = False

    print()
    print("  ALL TESTS PASSED!" if all_passed else "  SOME TESTS FAILED")
    print()
    return all_passed


# ============================================================
# Scan-Only Mode
# ============================================================


async def run_scan():
    """Scan and print discovered Edifier devices."""
    print("  Scanning (10s)...")
    all_devices = await scan_ble(10.0)
    edifier_devices = filter_edifier(all_devices)

    if edifier_devices:
        print("\n  Edifier devices:")
        for d in edifier_devices:
            print(f"    {d.name} [{d.address}] RSSI={d.rssi_label}")
    else:
        print("\n  No Edifier devices found")
        named = [d for d in all_devices if d.name != "Unknown"]
        if named:
            print(f"\n  All BLE ({len(named)}):")
            for d in sorted(named, key=lambda x: x.rssi, reverse=True):
                print(f"    {d.name:<30} [{d.address}] {d.rssi_label}")


# ============================================================
# Entry Point
# ============================================================


async def main():
    parser = argparse.ArgumentParser(description="Edifier MR BLE Tool")
    parser.add_argument("--address", "-a", type=str, default=None, help="Connect by BLE address")
    parser.add_argument("--scan", action="store_true", help="Scan only, don't connect")
    parser.add_argument(
        "--verify", action="store_true", help="Verify protocol against captured data"
    )
    args = parser.parse_args()

    if args.verify:
        verify()
    elif args.scan:
        await run_scan()
    else:
        cli = CLI()
        await cli.run(address=args.address)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Bye!")
    except Exception as e:
        print(f"\n  Fatal: {e}")
        traceback.print_exc()
