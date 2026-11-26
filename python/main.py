"""
risk-classifier: Dengue risk classification on the UNO Q.

Two interfaces over the same classify() function:
  1. Bridge RPC for the on-board MCU sketch (returns a float code).
  2. Flask HTTP endpoint for off-board clients (returns the full JSON verdict).
"""

import json
import re
import socket
import struct
import threading
import time
import requests
from flask import Flask, request, jsonify
from arduino.app_utils import *

# ─── Container-aware host discovery ────────────────────────────────
def _host_gateway():
    """Return the default gateway IP (the UNO Q host from inside the container)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except OSError:
        pass
    return "127.0.0.1"  # fallback when running outside a container

# ─── Configuration ─────────────────────────────────────────────────
LLM_HOST = _host_gateway()
LLM_URL = f"http://{LLM_HOST}:8081/v1/chat/completions"
LLM_HEALTH_URL = f"http://{LLM_HOST}:8081/health"
TIMEOUT_S = 120
FLASK_PORT = 7000

RISK_CODE = {"low": 0.0, "medium": 1.0, "high": 2.0}

# Qwen3.5 non-thinking mode parameters (from Unsloth docs)
LLM_PARAMS = {
    "temperature": 0.7,
    "top_p": 0.8,
    "presence_penalty": 1.5,
}

SYSTEM_PROMPT = (
    "You are an environmental risk classifier for dengue surveillance. "
    "Given temperature (Celsius), humidity (%), and whether standing water "
    "is reported, output ONLY a JSON object with two fields: "
    '"risk" (one of: low, medium, high) and '
    '"reason" (one short sentence, max 20 words). '
    "Do not include any prose outside the JSON."
)

FEW_SHOTS = [
    {"role": "user",
     "content": '{"temp_c": 18.0, "humidity_pct": 40, "standing_water": false}'},
    {"role": "assistant",
     "content": '{"risk":"low","reason":"Cool and dry conditions with no water; Aedes mosquito activity unlikely."}'},
    {"role": "user",
     "content": '{"temp_c": 29.5, "humidity_pct": 82, "standing_water": true}'},
    {"role": "assistant",
     "content": '{"risk":"high","reason":"Warm humid conditions plus standing water create ideal Aedes breeding habitat."}'},
]

# ─── Core inference function (used by both Bridge and Flask) ───────
def strip_think_blocks(text):
    """Remove residual <think>...</think> tags that Qwen3.5 emits even with
    --reasoning off. A known behavior in current llama.cpp builds."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

def build_messages(payload):
    user_msg = {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    return [{"role": "system", "content": SYSTEM_PROMPT}, *FEW_SHOTS, user_msg]

def call_llm(messages, max_tokens=80):
    body = {
        "model": "qwen3.5-0.8b",
        "messages": messages,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **LLM_PARAMS,
    }
    t0 = time.perf_counter()
    r = requests.post(LLM_URL, json=body, timeout=TIMEOUT_S)
    r.raise_for_status()
    latency_ms = (time.perf_counter() - t0) * 1000
    content = r.json()["choices"][0]["message"]["content"]
    content = strip_think_blocks(content)
    return content, latency_ms

def parse_verdict(content):
    """Defensive parse: tolerate stray code fences if the model adds them."""
    try:
        verdict = json.loads(content)
    except json.JSONDecodeError:
        cleaned = content.strip().lstrip("`").rstrip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].lstrip()
        verdict = json.loads(cleaned)
    if verdict.get("risk") not in {"low", "medium", "high"}:
        raise ValueError(f"unexpected risk value: {verdict!r}")
    return verdict

def classify(temp_c, humidity_pct, standing_water):
    """Inner classifier. Returns the full verdict dict."""
    payload = {
        "temp_c": float(temp_c),
        "humidity_pct": float(humidity_pct),
        "standing_water": bool(standing_water),
    }
    messages = build_messages(payload)
    content, latency_ms = call_llm(messages)
    try:
        verdict = parse_verdict(content)
    except (json.JSONDecodeError, ValueError):
        # one retry
        content, latency_ms2 = call_llm(messages, max_tokens=60)
        verdict = parse_verdict(content)
        latency_ms += latency_ms2
    verdict["latency_ms"] = round(latency_ms, 1)
    print(f"[classify] {payload} -> {verdict}")
    return verdict

def classify_risk(temp_c, humidity_pct, standing_water):
    """Bridge-facing wrapper. Returns a float code: 0=low, 1=medium, 2=high."""
    verdict = classify(temp_c, humidity_pct, standing_water)
    return RISK_CODE.get(verdict.get("risk", "unknown"), -1.0)

# ─── Flask app (off-board interface) ───────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/healthz", methods=["GET"])
def healthz():
    try:
        r = requests.get(LLM_HEALTH_URL, timeout=5)
        return jsonify({"flask": "ok", "llm": r.json()}), 200
    except Exception as e:
        return jsonify({"flask": "ok", "llm_error": str(e)}), 503

@flask_app.route("/classify", methods=["POST"])
def classify_endpoint():
    p = request.get_json(force=True)
    required = {"temp_c", "humidity_pct", "standing_water"}
    if not required.issubset(p):
        return jsonify({"error": f"missing: {required - set(p)}"}), 400
    verdict = classify(p["temp_c"], p["humidity_pct"], p["standing_water"])
    return jsonify(verdict), 200

def run_flask():
    # threaded=False: the model handles one request at a time anyway
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, threaded=False)

# ─── Main entry ────────────────────────────────────────────────────
Bridge.provide("classify", classify_risk)
threading.Thread(target=run_flask, daemon=True).start()

print(f"[init] llama-server target: {LLM_URL}")
print(f"[init] Bridge registered, Flask on :{FLASK_PORT}")

def loop():
    # Idle: all work is event-driven (Bridge calls and HTTP requests)
    time.sleep(1)

App.run(user_loop=loop)