import os
import json
import time
import threading
import requests
import urllib3
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# CONFIG LOAD
# =========================

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/options.json")

with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

INTERVAL     = CONFIG.get("interval", 60)
MODEM_HOST   = CONFIG.get("modem_host")
MODEM_USER   = CONFIG.get("modem_username")
MODEM_PASS   = CONFIG.get("modem_password")
MQTT_ENABLED = CONFIG.get("mqtt_enabled", False)
MQTT_HOST    = CONFIG.get("mqtt_host", "core-mosquitto")
MQTT_TOPIC   = CONFIG.get("mqtt_topic", "modem/telemetry")
MQTT_USER    = CONFIG.get("mqtt_username", "")
MQTT_PASS    = CONFIG.get("mqtt_password", "")
HTTP_PORT    = CONFIG.get("http_port", 8099)
STATE_PATH   = "/data/state.json"

MODEM_URL = f"https://{MODEM_HOST}/cgi/json-req"

# =========================
# LOGGING
# =========================

def log(event, msg, level="INFO"):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] [{level:7}] [{event}] {msg}", flush=True)

# =========================
# INTERNAL STATE
# =========================

_state = {
    "modem":   {"status": "offline",  "session": "expired"},
    "docsis":  {"downstreams": [],    "upstreams": []},
    "metrics": {
        "downstream_count":   0,
        "upstream_count":     0,
        "downstream_snr_avg": None,
        "downstream_power_avg": None,
        "upstream_power_avg": None,
    },
    "health":  {"fail_count": 0, "last_success": None, "last_error": None},
}

_state_lock = threading.Lock()


def _deep_merge(base, updates):
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def update_state(patch):
    with _state_lock:
        _deep_merge(_state, patch)
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(_state, f, indent=2)
        except Exception as e:
            log("STATE_WRITE", f"Failed to write state file: {e}", "WARN")


def get_state():
    with _state_lock:
        return json.loads(json.dumps(_state))

# =========================
# HTTP STATE SERVER
# =========================

class _StateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/api/modem/state":
            body = json.dumps(get_state(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silence default access logs


def _start_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), _StateHandler)
    log("HTTP_SERVER", f"State API listening on :{HTTP_PORT}/api/modem/state")
    server.serve_forever()

# =========================
# MQTT (OPTIONAL MIRROR)
# =========================

_mqttc = None


def _init_mqtt():
    global _mqttc
    _mqttc = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        _mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    _mqttc.reconnect_delay_set(min_delay=5, max_delay=60)


def _mqtt_connect():
    retries = 0
    while True:
        try:
            _mqttc.connect(MQTT_HOST, 1883, 60)
            _mqttc.loop_start()
            log("MQTT_CONNECT", f"Connected to {MQTT_HOST}", "SUCCESS")
            return
        except Exception as e:
            retries += 1
            wait = min(60, 5 * retries)
            log("MQTT_FAILURE", f"Connect failed: {e}, retry in {wait}s", "ERROR")
            time.sleep(wait)


def mqtt_mirror(payload):
    if not MQTT_ENABLED or _mqttc is None:
        return
    if not _mqttc.is_connected():
        log("MQTT_FAILURE", "Not connected, skipping mirror", "WARN")
        return
    try:
        _mqttc.publish(MQTT_TOPIC, json.dumps(payload))
        log("MQTT_MIRROR", f"Published to {MQTT_TOPIC}")
    except Exception as e:
        log("MQTT_FAILURE", f"Publish error: {e}", "WARN")

# =========================
# SESSION STATE
# =========================

_session    = requests.Session()
_session_id = None
_auth_token = None
_fail_count = 0

# =========================
# LOGIN
# =========================

def login():
    global _session_id, _auth_token, _fail_count

    log("LOGIN", f"Attempting login to {MODEM_HOST}")

    payload = {
        "request": {
            "id": 0,
            "session-id": "0",
            "actions": [{
                "id": 0,
                "method": "logIn",
                "parameters": {
                    "user": MODEM_USER,
                    "password": MODEM_PASS,
                    "persistent": "true",
                    "session-options": {"jwt-auth": "true"},
                },
            }],
            "cnonce": int(time.time() * 1000),
        }
    }

    try:
        r = _session.post(MODEM_URL, data={"req": json.dumps(payload)}, verify=False, timeout=20)
        r.raise_for_status()
        data = r.json()
        cb = data["reply"]["actions"][0]["callbacks"][0]["parameters"]
        _session_id = cb.get("id")
        _auth_token = data["reply"]["auth"]["token"]
        _fail_count = 0
        update_state({"modem": {"session": "valid"}})
        log("LOGIN_OK", f"Session established id={_session_id}", "SUCCESS")
    except Exception as e:
        _session_id = None
        _auth_token = None
        update_state({
            "modem":  {"status": "auth_error", "session": "expired"},
            "health": {"last_error": f"AUTH_FAILURE: {e}"},
        })
        log("AUTH_FAILURE", str(e), "ERROR")
        raise

# =========================
# QUERY
# =========================

def _query(xpaths):
    global _fail_count

    if not _session_id or not _auth_token:
        return None

    payload = {
        "request": {
            "id": int(time.time()),
            "session-id": _session_id,
            "auth-key": _auth_token,
            "actions": [
                {"id": i, "method": "getValue", "xpath": xp}
                for i, xp in enumerate(xpaths)
            ],
        }
    }

    try:
        r = _session.post(MODEM_URL, data={"req": json.dumps(payload)}, verify=False, timeout=15)

        if r.status_code != 200:
            _fail_count += 1
            log("MODEM_UNREACHABLE", f"HTTP {r.status_code}", "WARN")
            return None

        if "<html>" in r.text.lower():
            _fail_count += 1
            update_state({"modem": {"session": "expired"}})
            log("SESSION_EXPIRED", "Got HTML response — session lost", "WARN")
            return None

        return r.json()

    except requests.exceptions.Timeout:
        _fail_count += 1
        log("MODEM_TIMEOUT", "Request timed out", "WARN")
        return None
    except Exception as e:
        _fail_count += 1
        log("MODEM_UNREACHABLE", str(e), "WARN")
        return None

# =========================
# DOCSIS PARSING
# =========================

def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_channels(raw):
    value = raw.get("value", raw)
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = list(value.values())
    else:
        return []
    return [ch for ch in items if isinstance(ch, dict)]


def _parse_channels(actions):
    try:
        ds_raw = actions[0].get("callbacks", [{}])[0].get("parameters", {})
        us_raw = actions[1].get("callbacks", [{}])[0].get("parameters", {})
    except (IndexError, AttributeError, KeyError):
        log("INVALID_DOCSIS_RESPONSE", "Unexpected actions structure", "WARN")
        return None, None
    return _extract_channels(ds_raw), _extract_channels(us_raw)


def _compute_metrics(downstreams, upstreams):
    def avg(lst, *keys):
        for key in keys:
            vals = [_safe_float(c.get(key)) for c in lst if _safe_float(c.get(key)) is not None]
            if vals:
                return round(sum(vals) / len(vals), 2)
        return None

    return {
        "downstream_count":     len(downstreams),
        "upstream_count":       len(upstreams),
        "downstream_snr_avg":   avg(downstreams, "SNRLevel", "SnrLevel"),
        "downstream_power_avg": avg(downstreams, "PowerLevel", "Powerlevels"),
        "upstream_power_avg":   avg(upstreams,   "PowerLevel", "Powerlevels"),
    }

# =========================
# POLL + SENSOR UPDATE
# =========================

def poll():
    global _fail_count

    r = _query([
        "Device/Docsis/CableModem/Downstreams",
        "Device/Docsis/CableModem/Upstreams",
    ])

    if not r:
        _fail_count += 1
        update_state({"modem": {"status": "offline"}, "health": {"fail_count": _fail_count}})
        log("SENSOR_UPDATE_SKIPPED", f"No data received (fail_count={_fail_count})", "WARN")
        return False

    try:
        actions = r["reply"]["actions"]
    except (KeyError, TypeError):
        _fail_count += 1
        update_state({"health": {
            "fail_count": _fail_count,
            "last_error": "INVALID_DOCSIS_RESPONSE: missing reply.actions",
        }})
        log("INVALID_DOCSIS_RESPONSE", "Missing reply.actions", "WARN")
        return False

    downstreams, upstreams = _parse_channels(actions)

    if downstreams is None:
        _fail_count += 1
        log("STATE_INVALID", "Channel parse failed", "WARN")
        return False

    metrics = _compute_metrics(downstreams, upstreams)
    ts      = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _fail_count = 0

    update_state({
        "modem":   {"status": "online", "session": "valid"},
        "docsis":  {"downstreams": downstreams, "upstreams": upstreams},
        "metrics": metrics,
        "health":  {"fail_count": 0, "last_success": ts, "last_error": None},
    })

    log("SENSOR_UPDATE_SUCCESS",
        f"DS={metrics['downstream_count']} US={metrics['upstream_count']} "
        f"SNR_avg={metrics['downstream_snr_avg']} dB  "
        f"DS_pwr={metrics['downstream_power_avg']} dBmV  "
        f"US_pwr={metrics['upstream_power_avg']} dBmV",
        "SUCCESS")

    if MQTT_ENABLED:
        mqtt_mirror(get_state())

    return True

# =========================
# REAUTH CHECK
# =========================

def _needs_reauth():
    s = get_state()
    return (
        _session_id is None
        or _auth_token is None
        or _fail_count >= 3
        or s["modem"]["session"] != "valid"
    )

# =========================
# MAIN LOOP
# =========================

def run():
    log("BOOT", "========================================")
    log("BOOT", "  Modem Monitor V3.2.0 — sensor-first  ")
    log("BOOT", "========================================")
    log("BOOT", f"Modem host   : {MODEM_HOST}")
    log("BOOT", f"Poll interval: {INTERVAL}s")
    log("BOOT", f"HTTP state   : :{HTTP_PORT}/api/modem/state")
    log("BOOT", f"MQTT mirror  : {'enabled → ' + MQTT_HOST if MQTT_ENABLED else 'disabled'}")

    threading.Thread(target=_start_http_server, daemon=True).start()

    if MQTT_ENABLED:
        _init_mqtt()
        threading.Thread(target=_mqtt_connect, daemon=True).start()

    try:
        login()
    except Exception:
        log("BOOT", "Initial login failed — will retry in main loop", "WARN")

    while True:
        if _needs_reauth():
            log("SESSION_EXPIRED", "Re-auth required", "WARN")
            try:
                login()
            except Exception as e:
                log("AUTH_FAILURE", f"Login failed: {e}, retrying in 10s", "ERROR")
                time.sleep(10)
                continue

        ok = poll()

        if not ok:
            log("POLL_FAIL", f"Backing off 10s (fail_count={_fail_count})", "WARN")
            time.sleep(10)
        else:
            time.sleep(INTERVAL)

# =========================
# START
# =========================

run()
