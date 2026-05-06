import os
import json
import re
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

MODEM_BASE       = f"https://{MODEM_HOST}"
MODEM_GUI_URL    = f"{MODEM_BASE}/2.0/gui/"
MODEM_API_URL    = f"{MODEM_BASE}/cgi/json-req"

STABILIZE_DELAY  = 0.6   # seconds after login before first query
KEEPALIVE_EVERY  = 30    # seconds — keepalive fires if INTERVAL > this

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
    "modem":   {"status": "offline", "session": "expired"},
    "docsis":  {"downstreams": [],   "upstreams": []},
    "metrics": {
        "downstream_count":     0,
        "upstream_count":       0,
        "downstream_snr_avg":   None,
        "downstream_power_avg": None,
        "upstream_power_avg":   None,
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
# HTTP SESSION (browser-like)
# =========================

_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
})


class HtmlResponseError(Exception):
    """Raised when the modem returns HTML instead of JSON — session invalid."""
    pass


def _is_html(text):
    return "<html" in text.lower()


def _rpc_call(payload, timeout=15):
    """POST a JSON-RPC payload. Returns parsed JSON or raises on any failure."""
    r = _http.post(
        MODEM_API_URL,
        data={"req": json.dumps(payload)},
        verify=False,
        timeout=timeout,
    )
    if r.status_code != 200:
        snippet = r.text[:120].replace("\n", " ").strip()
        raise Exception(f"HTTP {r.status_code} -- {snippet}")
    if _is_html(r.text):
        snippet = r.text[:120].replace("\n", " ").strip()
        raise HtmlResponseError(f"Got HTML instead of JSON: {snippet}")
    return r.json()

# =========================
# SESSION STATE
# =========================

_session_id    = None
_auth_token    = None
_fail_count    = 0
_last_api_call = 0.0   # tracks last successful RPC for keepalive timing

# =========================
# BOOTSTRAP
# =========================

def bootstrap():
    """
    Simulate browser initialisation: load the modem GUI page then at least
    one JS asset.  This is REQUIRED to bind the backend session before any
    JSON-RPC calls will succeed.
    """
    log("BOOTSTRAP", f"Loading GUI -- {MODEM_GUI_URL}")

    try:
        r = _http.get(
            MODEM_GUI_URL,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            verify=False,
            timeout=20,
        )
    except requests.exceptions.Timeout:
        raise Exception("MODEM_TIMEOUT: GUI page timed out")
    except Exception as e:
        raise Exception(f"MODEM_UNREACHABLE: {e}")

    if r.status_code not in (200, 302, 304):
        snippet = r.text[:80].replace("\n", " ").strip()
        raise Exception(f"GUI_NOT_INITIALIZED: HTTP {r.status_code} -- {snippet}")

    log("BOOTSTRAP", f"GUI page received (HTTP {r.status_code}), fetching JS asset")

    # Parse <script src="..."> tags, then try known fallbacks
    js_candidates = re.findall(
        r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', r.text, re.IGNORECASE
    )
    fallbacks = [
        "/2.0/gui/js/vendor.js",
        "/2.0/gui/js/app.js",
        "/gui/js/vendor.js",
    ]
    all_paths = list(dict.fromkeys(js_candidates + fallbacks))

    fetched = False
    for js_path in all_paths[:6]:
        try:
            js_url = (
                js_path if js_path.startswith("http")
                else f"{MODEM_BASE}/{js_path.lstrip('/')}"
            )
            jr = _http.get(
                js_url,
                headers={"Accept": "application/javascript, */*"},
                verify=False,
                timeout=10,
            )
            if jr.status_code == 200:
                log("BOOTSTRAP", f"JS asset loaded: {js_path}", "SUCCESS")
                fetched = True
                break
        except Exception:
            continue

    if not fetched:
        log("BOOTSTRAP", "No JS asset fetched -- continuing anyway (may fail)", "WARN")

    log("BOOTSTRAP", "GUI bootstrap complete", "SUCCESS")

# =========================
# LOGIN
# =========================

def login():
    global _session_id, _auth_token, _fail_count, _last_api_call

    log("LOGIN", f"Logging in as {MODEM_USER!r}")

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
        data = _rpc_call(payload, timeout=20)
        cb = data["reply"]["actions"][0]["callbacks"][0]["parameters"]
        _session_id = cb.get("id")
        _auth_token = data["reply"]["auth"]["token"]
        _fail_count = 0
        _last_api_call = time.time()
        update_state({"modem": {"session": "valid"}})
        log("LOGIN_OK", f"Session established id={_session_id}", "SUCCESS")
        time.sleep(STABILIZE_DELAY)

    except HtmlResponseError as e:
        _session_id = None
        _auth_token = None
        update_state({
            "modem":  {"status": "auth_error", "session": "expired"},
            "health": {"last_error": str(e)},
        })
        log("GUI_NOT_INITIALIZED", f"Login returned HTML -- re-bootstrap needed: {e}", "ERROR")
        raise

    except Exception as e:
        _session_id = None
        _auth_token = None
        update_state({
            "modem":  {"status": "auth_error", "session": "expired"},
            "health": {"last_error": str(e)},
        })
        log("AUTH_FAILURE", str(e), "ERROR")
        raise

# =========================
# RAW QUERY
# =========================

def _query(xpaths):
    global _fail_count, _last_api_call

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
        result = _rpc_call(payload, timeout=15)
        _last_api_call = time.time()
        return result

    except HtmlResponseError as e:
        _fail_count += 1
        update_state({"modem": {"session": "expired"}})
        log("SESSION_EXPIRED", str(e), "WARN")
        raise  # bubble to state machine

    except requests.exceptions.Timeout:
        _fail_count += 1
        log("MODEM_TIMEOUT", "Request timed out", "WARN")
        return None

    except Exception as e:
        _fail_count += 1
        log("MODEM_UNREACHABLE", str(e), "WARN")
        return None

# =========================
# KEEPALIVE
# =========================

def keepalive():
    """Lightweight ping to prevent session expiry between polls."""
    log("KEEPALIVE", "Sending keepalive ping")
    try:
        _query(["Device/DeviceInfo/Manufacturer"])
        log("KEEPALIVE", "OK")
    except HtmlResponseError:
        raise  # bubble to state machine

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

    try:
        r = _query([
            "Device/Docsis/CableModem/Downstreams",
            "Device/Docsis/CableModem/Upstreams",
        ])
    except HtmlResponseError:
        raise  # bubble to state machine

    if r is None:
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
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
# MAIN STATE MACHINE
#
# BOOTSTRAP -> LOGIN -> ACTIVE ---> BOOTSTRAP (on session loss)
#     ^                                  |
#     +----------------------------------+
# =========================

def run():
    log("BOOT", "========================================")
    log("BOOT", "  Modem Monitor V3.3.0 -- sensor-first  ")
    log("BOOT", "========================================")
    log("BOOT", f"Modem host   : {MODEM_HOST}")
    log("BOOT", f"Poll interval: {INTERVAL}s")
    log("BOOT", f"Keepalive    : every {KEEPALIVE_EVERY}s (when INTERVAL > {KEEPALIVE_EVERY})")
    log("BOOT", f"HTTP state   : :{HTTP_PORT}/api/modem/state")
    log("BOOT", f"MQTT mirror  : {'enabled -> ' + MQTT_HOST if MQTT_ENABLED else 'disabled'}")

    threading.Thread(target=_start_http_server, daemon=True).start()

    if MQTT_ENABLED:
        _init_mqtt()
        threading.Thread(target=_mqtt_connect, daemon=True).start()

    sm_state = "BOOTSTRAP"
    backoff  = 5

    while True:

        # -- BOOTSTRAP -----------------------------------------------
        if sm_state == "BOOTSTRAP":
            log("STATE_MACHINE", "-> BOOTSTRAP")
            update_state({"modem": {"status": "offline", "session": "expired"}})
            _http.cookies.clear()
            try:
                bootstrap()
                sm_state = "LOGIN"
                backoff  = 5
            except Exception as e:
                log("GUI_NOT_INITIALIZED", f"Bootstrap failed: {e} -- retry in {backoff}s", "ERROR")
                update_state({"health": {"last_error": str(e)}})
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        # -- LOGIN ---------------------------------------------------
        elif sm_state == "LOGIN":
            log("STATE_MACHINE", "-> LOGIN")
            try:
                login()
                sm_state = "ACTIVE"
                backoff  = 5
            except HtmlResponseError:
                log("STATE_MACHINE", "HTML on login -- session not bound, re-bootstrap", "WARN")
                sm_state = "BOOTSTRAP"
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log("STATE_MACHINE", f"Login failed: {e} -- re-bootstrap in {backoff}s", "WARN")
                sm_state = "BOOTSTRAP"
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        # -- ACTIVE (poll + keepalive loop) --------------------------
        elif sm_state == "ACTIVE":

            # Keepalive: fire if INTERVAL is long and session is idle
            if INTERVAL > KEEPALIVE_EVERY:
                idle = time.time() - _last_api_call
                if idle >= KEEPALIVE_EVERY:
                    try:
                        keepalive()
                    except HtmlResponseError:
                        log("STATE_MACHINE", "Session lost during keepalive -> BOOTSTRAP", "WARN")
                        sm_state = "BOOTSTRAP"
                        continue

            # Poll
            try:
                ok = poll()
            except HtmlResponseError:
                log("STATE_MACHINE", "Session lost during poll -> BOOTSTRAP", "WARN")
                update_state({"modem": {"status": "offline", "session": "expired"}})
                sm_state = "BOOTSTRAP"
                continue

            if not ok:
                if _fail_count >= 3:
                    log("STATE_MACHINE", f"fail_count={_fail_count} -- re-bootstrap", "WARN")
                    sm_state = "BOOTSTRAP"
                else:
                    log("POLL_FAIL", "Transient failure, backing off 10s", "WARN")
                    time.sleep(10)
            else:
                time.sleep(INTERVAL)

# =========================
# START
# =========================

run()
