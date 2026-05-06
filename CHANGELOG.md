# Modem Monitor Changelog

All notable changes to Modem Monitor are documented here.

This project evolved from a standalone Python script → Home Assistant add-on → hardened MQTT telemetry service.

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
