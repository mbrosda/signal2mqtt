#!/usr/bin/env python3
# Copyright (c) 2026 Marcus Brosda
# Licensed under the MIT License — see README.md for details.
# This software is provided "as is", without warranty of any kind.
"""
signal_to_mqtt.py
Connects via WebSocket to the Signal REST API (json-rpc mode)
and forwards incoming messages containing a dataMessage entry
to an MQTT broker. All other messages are discarded.

Environment variables:
  SIGNAL_API_URL           Base URL of the API, e.g. http://localhost:2080
  SIGNAL_API_PHONE_NUMBER  Own phone number, e.g. +49123456789
  MQTT_BROKER              Hostname of the MQTT broker (default: localhost)
  MQTT_PORT                Port of the MQTT broker (default: 1883)
  MQTT_USERNAME            MQTT username (optional)
  MQTT_PASSWORD            MQTT password (optional)
  MQTT_TOPIC_PREFIX        Prefix for MQTT topics (default: signal)
  RECONNECT_DELAY          Seconds to wait before WebSocket reconnect (default: 5)
"""

import json
import logging
import os
import time

import paho.mqtt.client as mqtt
import websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    required = ("SIGNAL_API_URL", "SIGNAL_API_PHONE_NUMBER")
    config = {}

    for key in required:
        value = os.environ.get(key)
        if not value:
            raise EnvironmentError(f"Environment variable '{key}' is not set.")
        config[key] = value

    config["MQTT_BROKER"]       = os.environ.get("MQTT_BROKER", "localhost")
    config["MQTT_PORT"]         = int(os.environ.get("MQTT_PORT", "1883"))
    config["MQTT_USERNAME"]     = os.environ.get("MQTT_USERNAME")
    config["MQTT_PASSWORD"]     = os.environ.get("MQTT_PASSWORD")
    config["MQTT_TOPIC_PREFIX"] = os.environ.get("MQTT_TOPIC_PREFIX", "signal")
    config["RECONNECT_DELAY"]   = float(os.environ.get("RECONNECT_DELAY", "5"))

    # Convert HTTP URL to WebSocket URL: http(s):// -> ws(s)://
    ws_url = config["SIGNAL_API_URL"].strip().rstrip("/")
    if not ws_url.startswith(("http://", "https://")):
        raise EnvironmentError(
            f"SIGNAL_API_URL must start with http:// or https://, got: '{ws_url}'"
        )
    ws_url = ws_url.replace("https://", "wss://").replace("http://", "ws://")
    config["WS_URL"] = f"{ws_url}/v1/receive/{config['SIGNAL_API_PHONE_NUMBER']}"

    return config


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def is_data_message(envelope: dict) -> bool:
    """Returns True if the envelope contains a dataMessage."""
    return "dataMessage" in envelope


def build_mqtt_payload(entry: dict) -> dict:
    """Extracts the relevant fields for the MQTT message."""
    envelope = entry.get("envelope", {})
    data_msg = envelope.get("dataMessage", {})

    return {
        "sender":       envelope.get("sourceName") or envelope.get("sourceNumber"),
        "senderNumber": envelope.get("sourceNumber"),
        "timestamp":    envelope.get("timestamp"),
        "message":      data_msg.get("message"),
        "account":      entry.get("account"),
    }


def build_mqtt_topic(prefix: str, sender_number: str) -> str:
    """Builds an MQTT topic from the prefix and sender number."""
    safe_number = sender_number.lstrip("+").replace(" ", "")
    return f"{prefix}/message/{safe_number}"


def process_message(raw: str, mqtt_client: mqtt.Client, config: dict) -> None:
    """Parses a single WebSocket message and forwards it if applicable."""
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Invalid JSON received: %.80s", raw)
        return

    # The server occasionally sends empty ping objects - ignore them
    if not entry:
        return

    envelope = entry.get("envelope", {})

    if not is_data_message(envelope):
        log.debug("Message without dataMessage discarded.")
        return

    payload = build_mqtt_payload(entry)
    sender_number = envelope.get("sourceNumber", "unknown")
    topic = build_mqtt_topic(config["MQTT_TOPIC_PREFIX"], sender_number)

    result = mqtt_client.publish(
        topic,
        payload=json.dumps(payload, ensure_ascii=False),
        qos=1,
    )

    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        log.info(
            "Published -> %s | From: %s | Message: %.60s",
            topic,
            payload.get("sender"),
            payload.get("message") or "(empty)",
        )
    else:
        log.error("MQTT error while publishing to '%s': %d", topic, result.rc)


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def create_mqtt_client(config: dict) -> mqtt.Client:
    """Creates and connects an MQTT client."""
    client = mqtt.Client(
        client_id="signal-bridge",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )

    FATAL_RC = {
        1: "incompatible protocol version",
        2: "client ID rejected",
        3: "broker unavailable",
        4: "wrong username or password",
        5: "not authorized (ACL or invalid credentials)",
    }

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        if reason_code.is_failure:
            rc = reason_code.value
            reason = FATAL_RC.get(rc, f"unknown error ({reason_code})")
            log.error("MQTT connection failed, code %d: %s", rc, reason)
            if rc in FATAL_RC:
                log.error("Fatal error - script will exit.")
                client.disconnect()
                os._exit(1)
        else:
            log.info("MQTT connected to %s:%d", config["MQTT_BROKER"], config["MQTT_PORT"])

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        if reason_code.value != 0:
            rc = reason_code.value
            if rc in FATAL_RC:
                log.error("MQTT disconnected with error %d: %s", rc, FATAL_RC[rc])
            else:
                log.warning("MQTT unexpectedly disconnected (code %d). Attempting reconnect...", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    if config.get("MQTT_USERNAME"):
        client.username_pw_set(config["MQTT_USERNAME"], config.get("MQTT_PASSWORD"))
        log.info("MQTT authentication enabled for user '%s'.", config["MQTT_USERNAME"])

    client.connect(config["MQTT_BROKER"], config["MQTT_PORT"], keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def run_websocket(config: dict, mqtt_client: mqtt.Client) -> None:
    """
    Opens the WebSocket connection to the Signal API and processes
    incoming messages. Returns on disconnect so that main() can
    initiate a reconnect.
    """

    def on_open(ws):
        log.info("WebSocket connected: %s", config["WS_URL"])

    def on_message(ws, message):
        process_message(message, mqtt_client, config)

    def on_error(ws, error):
        log.error("WebSocket error: %s", error)

    def on_close(ws, close_status_code, close_msg):
        log.warning(
            "WebSocket disconnected (code: %s, message: %s).",
            close_status_code,
            close_msg,
        )

    ws = websocket.WebSocketApp(
        config["WS_URL"],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # run_forever blocks until the connection is closed.
    # ping_interval keeps the connection to the server alive.
    ws.run_forever(ping_interval=30, ping_timeout=10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Signal -> MQTT bridge starting (WebSocket mode).")

    try:
        config = load_config()
    except EnvironmentError as exc:
        log.error(str(exc))
        return

    log.info(
        "Configuration: WS=%s, broker=%s:%d",
        config["WS_URL"],
        config["MQTT_BROKER"],
        config["MQTT_PORT"],
    )

    mqtt_client = create_mqtt_client(config)

    try:
        while True:
            run_websocket(config, mqtt_client)
            # run_websocket only returns on disconnect
            log.info(
                "Reconnecting in %.0f seconds...",
                config["RECONNECT_DELAY"],
            )
            time.sleep(config["RECONNECT_DELAY"])

    except KeyboardInterrupt:
        log.info("Bridge stopped.")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()