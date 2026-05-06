import os
import json
import time
import threading
import requests
import urllib3
from datetime import datetime
import paho.mqtt.client as mqtt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# CONFIG LOAD
# =========================

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/options.json")

with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

INTERVAL = CONFIG.get("interval", 60)

MODEM_HOST = CONFIG.get("modem_host")
MODEM_USER = CONFIG.get("modem_username")
MODEM_PASS = CONFIG.get("modem_password")

MQTT_HOST = CONFIG.get("mqtt_host")
MQTT_TOPIC = CONFIG.get("mqtt_topic")
MQTT_USER = CONFIG.get("mqtt_username")
MQTT_PASS = CONFIG.get("mqtt_password")

# =========================
# ENDPOINTS
# =========================

MODEM_URL = f"https://{MODEM_HOST}/cgi/json-req"

# =========================
# STATE
# =========================

session = requests.Session()
session_id = None
auth_token = None

fail_count = 0
last_success = 0

lock = threading.Lock()

# =========================
# LOGGING
# =========================

def log(msg, level="INFO"):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] [{level}] {msg}", flush=True)

# =========================
# MQTT
# =========================

mqttc = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2
)

mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.reconnect_delay_set(min_delay=5, max_delay=60)


def mqtt_connect():
    retries = 0
    while True:
        try:
            mqttc.connect(MQTT_HOST, 1883, 60)
            mqttc.loop_start()
            log("MQTT connected", "SUCCESS")
            return
        except Exception as e:
            retries += 1
            wait = min(60, 5 * retries)
            log(f"MQTT connect failed ({e}), retry in {wait}s", "ERROR")
            time.sleep(wait)


def publish(payload):
    if not mqttc.is_connected():
        log("MQTT not connected, skipping publish", "WARN")
        return
    try:
        mqttc.publish(MQTT_TOPIC, json.dumps(payload))
    except Exception as e:
        log(f"MQTT publish error: {e}", "WARN")

# =========================
# LOGIN
# =========================

def login():
    global session_id, auth_token

    log("Logging into modem...")

    payload = {
        "request": {
            "id": 0,
            "session-id": "0",
            "actions": [
                {
                    "id": 0,
                    "method": "logIn",
                    "parameters": {
                        "user": MODEM_USER,
                        "password": MODEM_PASS,
                        "persistent": "true",
                        "session-options": {
                            "jwt-auth": "true"
                        }
                    }
                }
            ],
            "cnonce": int(time.time() * 1000)
        }
    }

    try:
        r = session.post(
            MODEM_URL,
            data={"req": json.dumps(payload)},
            verify=False,
            timeout=20
        )

        r.raise_for_status()
        data = r.json()

        cb = data["reply"]["actions"][0]["callbacks"][0]["parameters"]

        session_id = cb.get("id")
        auth_token = data["reply"]["auth"]["token"]

        log(f"Login OK session={session_id}", "SUCCESS")

    except Exception as e:
        session_id = None
        auth_token = None
        raise Exception(f"Login failed: {e}")

# =========================
# QUERY
# =========================

def query(xpaths):
    global fail_count

    if not session_id or not auth_token:
        return None

    payload = {
        "request": {
            "id": int(time.time()),
            "session-id": session_id,
            "auth-key": auth_token,
            "actions": [
                {"id": i, "method": "getValue", "xpath": xp}
                for i, xp in enumerate(xpaths)
            ]
        }
    }

    try:
        r = session.post(
            MODEM_URL,
            data={"req": json.dumps(payload)},
            verify=False,
            timeout=15
        )

        if r.status_code != 200:
            fail_count += 1
            return None

        if "<html>" in r.text.lower():
            fail_count += 1
            return None

        return r.json()

    except Exception as e:
        fail_count += 1
        log(f"Query error: {e}", "WARN")
        return None

# =========================
# POLL
# =========================

def poll():
    global fail_count, last_success

    r = query([
        "Device/Docsis/CableModem/Downstreams",
        "Device/Docsis/CableModem/Upstreams"
    ])

    if not r:
        return False

    try:
        actions = r["reply"]["actions"]
    except Exception:
        fail_count += 1
        return False

    payload = {
        "timestamp": datetime.now().isoformat(),
        "docsis": actions
    }

    publish(payload)

    fail_count = 0
    last_success = time.time()

    log("Telemetry OK", "SUCCESS")
    return True

# =========================
# REAUTH CHECK
# =========================

def needs_reauth():
    return (
        session_id is None or
        auth_token is None or
        fail_count >= 3 or
        (time.time() - last_success > 180)
    )

# =========================
# MAIN LOOP
# =========================

def run():
    global fail_count

    log("Modem Monitor V3.1 starting")

    mqtt_connect()
    login()

    while True:

        with lock:
            if needs_reauth():
                log("Re-auth required", "WARN")
                try:
                    login()
                    fail_count = 0
                except Exception as e:
                    log(f"Login failed: {e}", "ERROR")
                    time.sleep(10)
                    continue

        ok = poll()

        if not ok:
            log("Poll failed → backoff", "WARN")
            time.sleep(10)
        else:
            time.sleep(INTERVAL)

# =========================
# START
# =========================

run()
