import os
import json
import re
import time
import threading
import requests
import urllib3
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# CONFIG LOAD
# =========================

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/options.json")

try:
    with open(CONFIG_PATH, "r") as f:
        CONFIG = json.load(f)
except Exception as _cfg_err:
    print(f"[FATAL] Could not load config from {CONFIG_PATH}: {_cfg_err}", flush=True)
    raise

INTERVAL        = CONFIG.get("interval", 60)
MODEM_HOST      = CONFIG.get("modem_host")
MODEM_USER      = CONFIG.get("modem_username")
MODEM_PASS      = CONFIG.get("modem_password")
MQTT_ENABLED    = CONFIG.get("mqtt_enabled", False)
MQTT_HOST       = CONFIG.get("mqtt_host", "core-mosquitto")
MQTT_TOPIC      = CONFIG.get("mqtt_topic", "modem/telemetry")
MQTT_USER       = CONFIG.get("mqtt_username", "")
MQTT_PASS       = CONFIG.get("mqtt_password", "")
HTTP_PORT       = CONFIG.get("http_port", 8099)
STATE_PATH      = "/data/state.json"

MODEM_BASE      = f"https://{MODEM_HOST}"
MODEM_GUI_URL   = f"{MODEM_BASE}/2.0/gui/"
MODEM_API_URL   = f"{MODEM_BASE}/cgi/json-req"

STABILIZE_DELAY = 0.6   # seconds after login before first query
KEEPALIVE_EVERY = 30    # seconds between keepalive pings (only when INTERVAL > this)
PROBE_TIMEOUT   = 5     # seconds for modem reachability probe
HEARTBEAT_EVERY = 10    # seconds between "still waiting" log lines

_boot_time = time.time()

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
# SESSION COORDINATION
# =========================

# Set by session thread when authenticated, cleared by either thread on failure.
_session_ready = threading.Event()


def _interruptible_sleep(seconds, wake_if_cleared=None, chunk=1.0):
    """
    Sleep for `seconds` total, checking `wake_if_cleared` (an Event) each
    `chunk` seconds.  Returns True if we slept the full duration, False if
    the event was cleared early (session lost).
    """
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(chunk, remaining))
        if wake_if_cleared is not None and not wake_if_cleared.is_set():
            return False   # interrupted

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
        pass


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
    import paho.mqtt.client as mqtt   # lazy import
    _mqttc = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        _mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    _mqttc.reconnect_delay_set(min_delay=5, max_delay=60)


def _mqtt_connect_loop():
    """Runs in its own daemon thread.  Retries indefinitely without blocking."""
    retries = 0
    while True:
        try:
            _mqttc.connect(MQTT_HOST, 1883, 60)
            _mqttc.loop_start()
            log("MQTT", f"Connected to {MQTT_HOST}", "SUCCESS")
            return
        except Exception as e:
            retries += 1
            wait = min(60, 5 * retries)
            log("MQTT", f"Connect failed: {e} -- retry in {wait}s", "WARN")
            time.sleep(wait)


def mqtt_mirror(payload):
    if not MQTT_ENABLED or _mqttc is None:
        return
    if not _mqttc.is_connected():
        return   # silent skip; MQTT is optional
    try:
        _mqttc.publish(MQTT_TOPIC, json.dumps(payload))
        log("MQTT", f"Published to {MQTT_TOPIC}")
    except Exception as e:
        log("MQTT", f"Publish error: {e}", "WARN")

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
    """Modem returned HTML instead of JSON — session is invalid."""
    pass


def _is_html(text):
    return "<html" in text.lower()


def _rpc_call(payload, timeout=15):
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
        raise HtmlResponseError(f"Got HTML instead of JSON -- {snippet}")
    return r.json()

# =========================
# SESSION STATE
# =========================

_session_id    = None
_auth_token    = None
_fail_count    = 0
_last_api_call = 0.0

# =========================
# MODEM PROBE
# =========================

def probe_modem():
    """
    Lightweight reachability check before bootstrap.
    Returns True (reachable), False (timeout/unreachable), or 'html' (gateway error).
    """
    try:
        r = _http.get(
            MODEM_BASE,
            verify=False,
            timeout=PROBE_TIMEOUT,
            allow_redirects=True,
        )
        if _is_html(r.text) and r.status_code in (504, 502, 503):
            return "html_error"
        return True
    except requests.exceptions.Timeout:
        return False
    except Exception:
        return False

# =========================
# BOOTSTRAP
# =========================

def bootstrap():
    log("BOOTSTRAP", f"Loading GUI -- {MODEM_GUI_URL}")
    try:
        r = _http.get(
            MODEM_GUI_URL,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            verify=False,
            timeout=20,
        )
    except requests.exceptions.Timeout:
        raise Exception("MODEM_TIMEOUT: GUI page timed out after 20s")
    except Exception as e:
        raise Exception(f"MODEM_UNREACHABLE: {e}")

    if r.status_code not in (200, 302, 304):
        snippet = r.text[:80].replace("\n", " ").strip()
        raise Exception(f"GUI_NOT_INITIALIZED: HTTP {r.status_code} -- {snippet}")

    log("BOOTSTRAP", f"GUI page received (HTTP {r.status_code}), loading JS asset")

    js_candidates = re.findall(
        r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', r.text, re.IGNORECASE
    )
    fallbacks = ["/2.0/gui/js/vendor.js", "/2.0/gui/js/app.js", "/gui/js/vendor.js"]
    all_paths = list(dict.fromkeys(js_candidates + fallbacks))

    fetched = False
    for js_path in all_paths[:6]:
        try:
            js_url = (
                js_path if js_path.startswith("http")
                else f"{MODEM_BASE}/{js_path.lstrip('/')}"
            )
            jr = _http.get(js_url, headers={"Accept": "application/javascript, */*"},
                           verify=False, timeout=10)
            if jr.status_code == 200:
                log("BOOTSTRAP", f"JS asset loaded: {js_path}", "SUCCESS")
                fetched = True
                break
        except Exception:
            continue

    if not fetched:
        log("BOOTSTRAP", "No JS asset loaded -- proceeding (may fail)", "WARN")

    log("BOOTSTRAP", "Complete", "SUCCESS")

# =========================
# LOGIN
# =========================

def login(attempt=1):
    global _session_id, _auth_token, _fail_count, _last_api_call

    log("LOGIN", f"Attempt {attempt}/inf as {MODEM_USER!r}")

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
        log("LOGIN", f"Success -- session id={_session_id}", "SUCCESS")
        time.sleep(STABILIZE_DELAY)

    except HtmlResponseError as e:
        _session_id = None
        _auth_token = None
        update_state({"modem": {"status": "auth_error", "session": "expired"},
                      "health": {"last_error": str(e)}})
        log("LOGIN", f"Got HTML -- GUI not bound, need re-bootstrap", "ERROR")
        raise

    except Exception as e:
        _session_id = None
        _auth_token = None
        update_state({"modem": {"status": "auth_error", "session": "expired"},
                      "health": {"last_error": str(e)}})
        log("LOGIN", f"Failed: {e}", "ERROR")
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
        log("SESSION", f"HTML response -- session expired: {e}", "WARN")
        raise

    except requests.exceptions.Timeout:
        _fail_count += 1
        log("MODEM", "Request timed out", "WARN")
        return None

    except Exception as e:
        _fail_count += 1
        log("MODEM", str(e), "WARN")
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
        log("PARSE", "Unexpected actions structure", "WARN")
        return None, None
    return _extract_channels(ds_raw), _extract_channels(us_raw)


def _compute_metrics(ds, us):
    def avg(lst, *keys):
        for key in keys:
            vals = [_safe_float(c.get(key)) for c in lst if _safe_float(c.get(key)) is not None]
            if vals:
                return round(sum(vals) / len(vals), 2)
        return None

    return {
        "downstream_count":     len(ds),
        "upstream_count":       len(us),
        "downstream_snr_avg":   avg(ds, "SNRLevel", "SnrLevel"),
        "downstream_power_avg": avg(ds, "PowerLevel", "Powerlevels"),
        "upstream_power_avg":   avg(us, "PowerLevel", "Powerlevels"),
    }

# =========================
# POLL
# =========================

def poll():
    global _fail_count

    try:
        r = _query([
            "Device/Docsis/CableModem/Downstreams",
            "Device/Docsis/CableModem/Upstreams",
        ])
    except HtmlResponseError:
        raise  # bubble to poll loop -> clears _session_ready

    if r is None:
        _fail_count += 1
        update_state({"modem": {"status": "offline"},
                      "health": {"fail_count": _fail_count}})
        log("POLL", f"No data (fail_count={_fail_count})", "WARN")
        return False

    try:
        actions = r["reply"]["actions"]
    except (KeyError, TypeError):
        _fail_count += 1
        update_state({"health": {"fail_count": _fail_count,
                                 "last_error": "missing reply.actions"}})
        log("POLL", "Invalid response: missing reply.actions", "WARN")
        return False

    ds, us = _parse_channels(actions)
    if ds is None:
        _fail_count += 1
        log("POLL", "Channel parse failed", "WARN")
        return False

    metrics = _compute_metrics(ds, us)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _fail_count = 0

    update_state({
        "modem":   {"status": "online", "session": "valid"},
        "docsis":  {"downstreams": ds, "upstreams": us},
        "metrics": metrics,
        "health":  {"fail_count": 0, "last_success": ts, "last_error": None},
    })

    log("POLL",
        f"OK -- DS={metrics['downstream_count']} US={metrics['upstream_count']} "
        f"SNR={metrics['downstream_snr_avg']} dB "
        f"DS_pwr={metrics['downstream_power_avg']} dBmV "
        f"US_pwr={metrics['upstream_power_avg']} dBmV",
        "SUCCESS")

    if MQTT_ENABLED:
        mqtt_mirror(get_state())

    return True

# =========================
# SESSION THREAD
#
# Manages: PROBE -> BOOTSTRAP -> LOGIN -> ACTIVE (keepalive)
# Runs entirely in background — never blocks the poll loop.
#
# Sets  _session_ready when authenticated.
# Clears _session_ready on any failure so poll loop skips cleanly.
# =========================

def _session_thread():
    backoff       = 5
    login_attempt = 0

    while True:

        # ── PROBE ────────────────────────────────────────────────────
        log("PROBE", f"Checking modem reachability at {MODEM_HOST}")
        result = probe_modem()
        if result is False:
            log("PROBE", f"Modem unreachable -- retry in {backoff}s", "WARN")
            _interruptible_sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if result == "html_error":
            log("PROBE", f"Modem returned gateway error -- retry in {backoff}s", "WARN")
            _interruptible_sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        log("PROBE", "Modem reachable", "SUCCESS")
        backoff = 5

        # ── BOOTSTRAP ────────────────────────────────────────────────
        log("BOOTSTRAP", "Starting GUI session bootstrap")
        _session_ready.clear()
        _http.cookies.clear()
        update_state({"modem": {"status": "offline", "session": "expired"}})

        try:
            bootstrap()
        except Exception as e:
            log("BOOTSTRAP", f"Failed: {e} -- retry in {backoff}s", "ERROR")
            update_state({"health": {"last_error": str(e)}})
            _interruptible_sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        backoff = 5

        # ── LOGIN (retry loop within one bootstrap cycle) ────────────
        login_ok = False
        login_attempt = 0
        while not login_ok:
            login_attempt += 1
            try:
                login(login_attempt)
                login_ok = True
                backoff = 5
            except HtmlResponseError:
                # GUI not bound — must re-bootstrap, not just retry login
                log("LOGIN", f"Session not bound -- re-bootstrap required", "WARN")
                break
            except Exception:
                delay = min(backoff, 30)
                log("LOGIN", f"Retrying in {delay}s", "WARN")
                _interruptible_sleep(delay)
                backoff = min(backoff * 2, 60)

        if not login_ok:
            # HtmlResponseError broke us out of the login loop
            _interruptible_sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        # ── ACTIVE — signal poll loop, then run keepalive ────────────
        log("SESSION", "Ready -- poll loop will proceed on next cycle", "SUCCESS")
        _session_ready.set()

        # Keepalive loop: sleep KEEPALIVE_EVERY seconds at a time.
        # Wake early if _session_ready is cleared by the poll loop.
        while True:
            if INTERVAL > KEEPALIVE_EVERY:
                still_alive = _interruptible_sleep(
                    KEEPALIVE_EVERY,
                    wake_if_cleared=_session_ready,
                )
                if not still_alive:
                    # Poll loop detected session loss
                    log("SESSION", "Session cleared by poll loop -- re-bootstrap", "WARN")
                    break

                log("KEEPALIVE", f"Mid-interval ping (idle {KEEPALIVE_EVERY}s)")
                try:
                    _query(["Device/DeviceInfo/Manufacturer"])
                    # success — continue keepalive loop
                except HtmlResponseError:
                    log("KEEPALIVE", "Session expired -- re-bootstrap", "WARN")
                    _session_ready.clear()
                    break
            else:
                # INTERVAL <= KEEPALIVE_EVERY: poll is frequent enough, no keepalive needed.
                # Just watch for session to be invalidated by poll loop.
                cleared = _interruptible_sleep(INTERVAL * 2, wake_if_cleared=_session_ready)
                if not cleared:
                    log("SESSION", "Session cleared by poll loop -- re-bootstrap", "WARN")
                    break

# =========================
# POLL LOOP (MAIN THREAD)
#
# Starts immediately.  Skips with a heartbeat log until session is ready.
# Detects session loss and clears _session_ready so session thread re-bootstraps.
# =========================

def _poll_loop():
    last_poll        = 0.0
    wait_log_time    = 0.0

    log("POLL", "Poll loop started -- waiting for session")

    while True:
        now = time.time()

        if not _session_ready.is_set():
            # Log a heartbeat every HEARTBEAT_EVERY seconds while waiting
            if now - wait_log_time >= HEARTBEAT_EVERY:
                elapsed = now - _boot_time
                log("POLL", f"Waiting for session... (boot +{elapsed:.0f}s)")
                wait_log_time = now
            # Short sleep — stay responsive to session becoming ready
            _session_ready.wait(timeout=5)
            continue

        # Session is ready.  Throttle to INTERVAL.
        elapsed_since_poll = now - last_poll
        if elapsed_since_poll < INTERVAL:
            # Wait out the remainder, but wake if session is cleared
            _interruptible_sleep(INTERVAL - elapsed_since_poll,
                                 wake_if_cleared=_session_ready)
            continue

        # Time to poll
        last_poll    = time.time()
        wait_log_time = 0.0   # reset so we log immediately if session drops

        try:
            ok = poll()
        except HtmlResponseError:
            log("POLL", "Session lost during poll -- signalling re-bootstrap", "WARN")
            _session_ready.clear()
            update_state({"modem": {"status": "offline", "session": "expired"}})
            continue

        if not ok and _fail_count >= 3:
            log("POLL", f"3 consecutive failures -- signalling re-bootstrap", "WARN")
            _session_ready.clear()

# =========================
# ENTRY POINT
# =========================

def run():
    log("BOOT", "============================================")
    log("BOOT", "  Modem Monitor V3.4.0                     ")
    log("BOOT", "============================================")
    log("BOOT", f"Modem        : {MODEM_HOST}")
    log("BOOT", f"Poll interval: {INTERVAL}s")
    log("BOOT", f"Keepalive    : every {KEEPALIVE_EVERY}s (when INTERVAL > {KEEPALIVE_EVERY}s)")
    log("BOOT", f"HTTP state   : :{HTTP_PORT}/api/modem/state")
    log("BOOT", f"MQTT mirror  : {'enabled -> ' + MQTT_HOST if MQTT_ENABLED else 'disabled'}")
    log("BOOT", "Starting services...")

    # HTTP state server
    threading.Thread(target=_start_http_server, daemon=True).start()
    log("BOOT", "HTTP state server started")

    # MQTT (non-blocking, optional)
    if MQTT_ENABLED:
        log("BOOT", f"MQTT enabled -- connecting to {MQTT_HOST} in background")
        _init_mqtt()
        threading.Thread(target=_mqtt_connect_loop, daemon=True).start()
    else:
        log("BOOT", "MQTT disabled -- skipping")

    # Session manager (background thread)
    threading.Thread(target=_session_thread, daemon=True, name="session").start()
    log("BOOT", "Session manager started")

    # Poll loop (main thread — never returns)
    log("BOOT", "Startup complete -- entering poll loop")
    _poll_loop()

run()
