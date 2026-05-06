# Modem Monitor Changelog

All notable changes to Modem Monitor are documented here.

This project evolved from a standalone Python script → Home Assistant add-on → hardened MQTT telemetry service.

---

## [3.4.4] - 2026-05-06 — DROP WITH-CONTENV FROM RUN.SH

### Fixed
- **Replaced `#!/usr/bin/with-contenv bashio` shebang with `#!/bin/bash`** — `with-contenv` is a binary that reads s6's container environment directory; if it can't find that directory (startup race condition), it exits non-zero before ever exec'ing bash. Combined with `set -e`, this caused the script to die silently before the first `echo` line, explaining why no output ever appeared in the log viewer
- Removed `set -e` to prevent any bash-level failure from killing the script before Python starts
- Added `-u` flag to `python3` invocation (explicit unbuffered mode, belt-and-suspenders alongside `PYTHONUNBUFFERED=1`)
- None of the removed functionality was needed: Python reads config from `/data/options.json` directly and doesn't use any HA supervisor environment variables injected by `with-contenv`

---

## [3.4.3] - 2026-05-06 — REMOVE BROKEN STDOUT REDIRECT

### Fixed
- **Removed `exec 1>/proc/1/fd/1 2>/proc/1/fd/2` from run.sh** — this redirect was added in v3.4.1 to bypass the s6-log pipeline but instead caused bash to exit immediately if the fd path was inaccessible, preventing Python from ever starting. The hassio-addons base image already routes service stdout to the HA log system correctly; the redirect was never needed.
- Added UTC timestamp to the run.sh startup banner line

---

## [3.4.2] - 2026-05-06 — BOOTSTRAP BEST-EFFORT

### Fixed
- **Bootstrap no longer blocks startup on slow/unresponsive modem GUI**
  - GUI page timeout reduced from 20s → 8s
  - JS asset timeout reduced from 10s → 5s
  - Bootstrap failures now log `WARN` and allow login to proceed immediately (previously raised exception → backoff retry loop → silent hang)
- `startup: services` changed to `startup: application` in config.yaml — prevents add-on from blocking HA core startup sequence

---

## [3.4.1] - 2026-05-06 — STDOUT REDIRECT FIX

### Fixed
- **Add-on log output now visible in HA UI**
  - Added `exec 1>/proc/1/fd/1 2>/proc/1/fd/2` to `run.sh` before `exec python3`
  - s6-overlay v3 pipes supervised service stdout through its own log infrastructure (not Docker stdout); this redirect bypasses it so HA supervisor captures all output
- **Config load errors now print before crashing** — wrapped module-level `open(CONFIG_PATH)` in try/except with `print()` so fatal config errors are visible in the log
- Added `.gitattributes` to repo root forcing LF line endings for all `.sh`, `.py`, `.yaml`, and `Dockerfile` — prevents Windows `core.autocrlf=true` from injecting `\r` into shell scripts, which caused `bad interpreter` errors in the Linux container

---

## [3.4.0] - 2026-05-06 -- NON-BLOCKING THREADED ARCHITECTURE

### Architecture (breaking change from v3.3.x)
- **Session manager** runs in a dedicated background thread (`_session_thread`)
  - Handles: PROBE -> BOOTSTRAP -> LOGIN -> ACTIVE (keepalive) completely off the main thread
  - Uses `_session_ready` threading.Event to signal the poll loop
  - Re-bootstraps automatically on any session loss without blocking anything
- **Poll loop** runs on the main thread and starts immediately on boot
  - Logs `[POLL] Waiting for session... (boot +Xs)` every 10s until session is ready
  - Wakes immediately when session becomes available (no wasted interval sleep)
  - Detects session loss from poll failures and signals session thread to re-bootstrap

### Startup visibility
- Boot banner prints within <1 second, before any network I/O
- Every stage logs as it starts: BOOT, HTTP, MQTT, SESSION, PROBE, BOOTSTRAP, LOGIN, POLL
- No silent waiting -- heartbeat logs every 10s if modem is slow to respond

### New: Modem probe step
- Lightweight `GET` to modem root before bootstrap (5s timeout)
- Detects: reachable / unreachable (timeout) / gateway error (504 HTML)
- Avoids wasting a full 20s bootstrap timeout when modem is simply down

### Login
- Now logs attempt number: `[LOGIN] Attempt 1/inf as 'admin'`
- `HtmlResponseError` during login triggers re-bootstrap (not re-login)
- Other exceptions retry login with backoff before re-bootstrapping

### Keepalive
- `_interruptible_sleep()` helper replaces the midpoint keepalive in sleep
- Session thread wakes from keepalive sleep immediately if poll loop clears session

### MQTT
- Renamed to `_mqtt_connect_loop` -- clearer it is a daemon thread
- Silently skips publish if not connected (was logging WARN every cycle)

---

## [3.3.1] - 2026-05-06

### Fixed / Optimised
- **Keepalive no longer fires before every poll** — it now fires only during the idle sleep period via `_sleep_with_keepalive()`. With `INTERVAL=60s` this eliminates one redundant modem request per cycle
- `paho-mqtt` import is now lazy (inside `_init_mqtt`) — not loaded at all when `mqtt_enabled: false`
- Removed noisy `[KEEPALIVE] OK` INFO log; keepalive only logs when it fires (mid-sleep ping) or fails

---

## [3.3.0] - 2026-05-06 — BROWSER-SIMULATED STATE MACHINE REWRITE

### Architecture
- Full state machine implemented: `BOOTSTRAP -> LOGIN -> ACTIVE -> BOOTSTRAP`
- **GUI bootstrap step added** (was missing and is the root cause of 504/HTML failures):
  - `GET /2.0/gui/` — loads modem GUI page, binds backend session, captures cookies
  - Parses `<script src>` tags and fetches first JS asset to complete session binding
  - Falls back to known paths (`/2.0/gui/js/vendor.js`, `/2.0/gui/js/app.js`) if HTML parsing yields nothing
- `requests.Session` now set with browser-like `User-Agent` header
- Cookies cleared on every re-bootstrap to ensure a clean session

### Session Management
- `HtmlResponseError` exception class — raised whenever modem returns HTML instead of JSON at any call point
- Any HTML response at bootstrap, login, keepalive, or poll immediately triggers `-> BOOTSTRAP`
- 600ms stabilisation delay after successful login before first query
- Exponential backoff (5s → 60s cap) on bootstrap and login failures

### Keepalive
- Lightweight `getValue` ping sent to `Device/DeviceInfo/Manufacturer` every 30s when `INTERVAL > 30`
- Prevents silent session expiry between polls
- HTML response on keepalive triggers immediate re-bootstrap

### Logging (new events)
- `[STATE_MACHINE]` — state transitions
- `[BOOTSTRAP]` — GUI page load and JS asset fetch progress
- `[GUI_NOT_INITIALIZED]` — HTML returned where JSON expected
- `[KEEPALIVE]` — keepalive ping result
- `[LOGIN_OK]` / `[AUTH_FAILURE]` / `[SESSION_EXPIRED]` — as before

---

## [3.2.2] - 2026-05-06

### Fixed
- Removed 10s startup delay — unnecessary since the main loop already retries login on failure

---

## [3.2.1] - 2026-05-06

### Fixed
- `login()` now detects HTML responses (e.g. modem returning 504 gateway timeout) before attempting JSON parse — logs `AUTH_FAILURE` with a snippet of the response instead of crashing
- Added 10s startup delay to let the modem's web interface settle before the first login attempt
- `run.sh` version string updated to V3.2.0

---

## [3.2.0] - 2026-05-06 — SENSOR-FIRST ARCHITECTURE REWRITE

### Architecture
- **PRIMARY**: Add-on now exposes a built-in HTTP state API at `:<http_port>/api/modem/state` — no MQTT required
- **SECONDARY**: MQTT is now optional (`mqtt_enabled: false` by default) and mirrors the state object only when enabled
- Internal canonical state object written to `/data/state.json` on every successful poll

### New Config Options
- `http_port` (default: `8099`) — port for the HTTP state API
- `mqtt_enabled` (default: `false`) — toggle MQTT mirror on/off

### Observability / Logging
- Structured event-code logging: `[BOOT]`, `[LOGIN_OK]`, `[AUTH_FAILURE]`, `[SESSION_EXPIRED]`, `[POLL_FAIL]`, `[SENSOR_UPDATE_SUCCESS]`, `[SENSOR_UPDATE_SKIPPED]`, `[STATE_INVALID]`, `[INVALID_DOCSIS_RESPONSE]`, `[MODEM_UNREACHABLE]`, `[MODEM_TIMEOUT]`, `[MQTT_MIRROR]`, `[MQTT_FAILURE]`, `[HTTP_SERVER]`
- Boot banner logs modem host, interval, HTTP port, and MQTT status
- All timestamps in UTC ISO 8601

### Sensor State Schema
```json
{
  "modem":   { "status": "online|offline|auth_error", "session": "valid|expired|retry" },
  "docsis":  { "downstreams": [...], "upstreams": [...] },
  "metrics": { "downstream_count": 0, "upstream_count": 0, "downstream_snr_avg": null,
               "downstream_power_avg": null, "upstream_power_avg": null },
  "health":  { "fail_count": 0, "last_success": "...", "last_error": null }
}
```

### Home Assistant REST Sensor (add to `configuration.yaml`)
```yaml
rest:
  - resource: http://localhost:8099/api/modem/state
    scan_interval: 60
    sensor:
      - name: "Modem Status"
        value_template: "{{ value_json.modem.status }}"
      - name: "Downstream Channels"
        value_template: "{{ value_json.metrics.downstream_count }}"
      - name: "Upstream Channels"
        value_template: "{{ value_json.metrics.upstream_count }}"
      - name: "Downstream SNR Avg"
        value_template: "{{ value_json.metrics.downstream_snr_avg }}"
        unit_of_measurement: "dB"
      - name: "Downstream Power Avg"
        value_template: "{{ value_json.metrics.downstream_power_avg }}"
        unit_of_measurement: "dBmV"
      - name: "Upstream Power Avg"
        value_template: "{{ value_json.metrics.upstream_power_avg }}"
        unit_of_measurement: "dBmV"
```

---

## [3.1.1] - 2026-05-06

### Changed
- Removed pre-filled default usernames (`admin`, `modemmonitor`) from config — all credential fields now default to empty

---

## [3.1.0] - 2026-05-06 — HARDENED SESSION + MQTT AUTH REWRITE (CURRENT STABLE ATTEMPT)

### 🔐 Security / Authentication
- Added modem session hardening (persistent session + auth-key handling)
- Improved login stability for Sagemcom F@ST3896 API
- Added retry-safe session management
- Prevented silent session expiry failures

### 📡 MQTT
- Added MQTT authentication support:
  - mqtt_username
  - mqtt_password
- Switched to loop_start() non-blocking MQTT client
- Added safe publish wrapper with exception handling

### 🧠 Reliability
- Added fail_count-based reauthentication trigger
- Added last_success timestamp tracking
- Improved polling resilience

### ⚙️ Add-on Structure
- Standardized Home Assistant options.json schema usage
- Clean separation of config → runtime logic

### ⚠️ Notes
- Requires correct HA base image (S6-compatible)
- Sensitive to incorrect Docker base selection

---

## [3.0.2] - 2026-05-06 — HOME ASSISTANT ADD-ON STRUCTURE MIGRATION

### 🧱 Build System
- Introduced proper HA add-on config.yaml schema
- Added BUILD_FROM-based Docker build system
- Attempted transition to Supervisor-managed lifecycle

### 🐳 Docker
- First structured Dockerfile implementation for HA add-on
- Added run.sh entrypoint system
- Began dependency installation via apk + pip

### 📁 Architecture
- Split runtime into:
  - modem.py (core logic)
  - run.sh (entrypoint)
- Introduced /app runtime model

### ⚠️ Issues Introduced
- Base image mismatch issues began here
- Initial s6 overlay/PID 1 instability appeared
- Early Supervisor build cache confusion

---

## [3.0.1] - 2026-05-06 — INITIAL HOME ASSISTANT ADD-ON CONVERSION

### ✨ Features
- Converted standalone script into Home Assistant add-on
- Introduced MQTT publishing layer
- Added DOCSIS polling:
  - Downstream channels
  - Upstream channels

### 🧪 System
- Implemented basic polling loop (interval-based)
- Introduced session login to modem API
- First structured JSON telemetry output

### ⚠️ Limitations
- No MQTT authentication support
- No retry or session recovery logic
- Weak Docker base assumptions
- No proper S6 lifecycle understanding yet

---

## [3.0.0] - 2026-05-05 — ORIGINAL WORKING PYTHON SCRIPT

### 🧠 Core Functionality
- Direct Python script communicating with Sagemcom F@ST3896 modem
- Manual login using requests session
- JSON-based modem API queries

### 📡 Data Collection
- DOCSIS downstream/upstream metrics extracted
- Basic telemetry structuring

### 📤 Output
- Initial MQTT publishing (unauthenticated)
- Simple JSON payloads

### 🔁 Execution
- Run manually or via shell script
- No containerization
- No Home Assistant integration

### ⚠️ Limitations
- No persistence layer
- No failure recovery
- No structured config system
- No add-on compatibility

---

## [2.x — PRE-ADDON EXPERIMENTATION PHASE]

### 🧪 Early Development
- Reverse engineered Sagemcom GUI API endpoints
- Validated session-based authentication flow
- Confirmed JSON request/response structure

### 📡 Discovery
- Identified:
  - `/cgi/json-req` endpoint
  - session-id + auth-token requirement
  - XPath-based telemetry queries

---

## [1.x — REVERSE ENGINEERING PHASE]

### 🔍 Research
- Captured modem web UI requests
- Identified:
  - GUI bootstrap endpoints
  - JavaScript-loaded session tokens
- Confirmed HTTPS self-signed certificate behavior

### 🧠 Understanding
- Learned modem uses:
  - session-based auth
  - JWT-style token optionality
  - RPC-like JSON action system

---
