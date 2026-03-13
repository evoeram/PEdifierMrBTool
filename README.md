# PEdifierMrBTool

Production-ready BLE control server and web UI for Edifier MR-series speakers (MR3BT/MR5BT) using FastAPI + WebSocket.

## Features
- BLE scan/connect and live state sync.
- Volume control with server-side rate limiting.
- Parametric EQ management and preset storage.
- Advanced audio controls (model-dependent): low cut, acoustic space, desktop mode, prompt tones, LDAC, active speaker.
- Static web UI served by the API server.

## Architecture
- `mr_ble.py`: BLE protocol, packet parser, core controller logic.
- `server.py`: FastAPI app, WebSocket command handling, input validation, broadcasting.
- `app_config.py`: environment-based runtime configuration.
- `static/`: browser UI assets.
- `tests/`: baseline unit/API regression tests.

## Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run locally
```bash
make run
```

Custom host/port/BLE address:
```bash
python server.py --host 0.0.0.0 --port 8000 --address XX:XX:XX:XX:XX:XX
```

## Configuration
Environment variables (with defaults):

- `EDIFIER_HOST=0.0.0.0`
- `EDIFIER_PORT=8000`
- `EDIFIER_LOG_LEVEL=INFO`
- `EDIFIER_BLE_ADDRESS=` (optional auto-connect address)
- `EDIFIER_SCAN_TIMEOUT_SECONDS=8.0`
- `EDIFIER_WS_MAX_MESSAGE_SIZE=16384`
- `EDIFIER_VOLUME_MIN_INTERVAL_SECONDS=0.35`

## Developer workflow
```bash
make install
make format
make lint
make typecheck
make test
make check
```

## Docker
```bash
docker build -t pedifier-mr-tool .
docker run --rm -p 8000:8000 pedifier-mr-tool
```

## Supported devices / capabilities
- MR5BT: full feature set (9-band EQ, LDAC, active speaker, advanced audio).
- MR3BT: reduced feature set (6-band EQ, no LDAC / active-speaker switching).
- Unknown models: best-effort capability detection.

## Troubleshooting
- **No BLE devices found**: ensure Bluetooth adapter is enabled and user has BLE access permissions.
- **Web UI shows disconnected**: verify server logs and BLE address.
- **Slow/laggy volume changes**: adjust `EDIFIER_VOLUME_MIN_INTERVAL_SECONDS` carefully.
- **Command rejected**: check parameter ranges (band index, gain, frequency, etc.).

## Security notes
- WebSocket payloads are validated for type/shape.
- Oversized WebSocket messages are rejected.
- Invalid JSON and malformed command payloads return explicit errors.

## Release
See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
