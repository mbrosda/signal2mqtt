# signal2mqtt

A lightweight Python bridge that connects [bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) to an MQTT broker. It listens for incoming Signal messages via a persistent WebSocket connection and publishes relevant messages to MQTT topics in real time.

---

## How it works

```
Signal Network
     │
     ▼
signal-cli-rest-api          (json-rpc mode, WebSocket)
     │
     │  ws://host/v1/receive/<number>
     ▼
signal2mqtt.py               (filters dataMessage entries)
     │
     │  MQTT publish  QoS 1
     ▼
MQTT Broker
```

The script connects to the Signal REST API via WebSocket and receives a continuous stream of JSON objects. Only objects that contain a `dataMessage` field (i.e. actual text messages) are forwarded to the MQTT broker. All other event types — delivery receipts, typing indicators, read receipts, etc. — are silently discarded.

On any WebSocket disconnect the script waits for `RECONNECT_DELAY` seconds and then reconnects automatically. Fatal MQTT errors (wrong credentials, not authorized) cause an immediate exit with a clear error message.

---

## Requirements

### Signal REST API — required mode: `json-rpc`

This script **requires** the Signal REST API to run in **`json-rpc` mode**. In this mode the `/v1/receive/<number>` endpoint is a WebSocket, not an HTTP polling endpoint.

> **Important:** In `normal` and `native` mode the receive endpoint only supports HTTP polling — WebSocket connections will be refused. The script will not work with those modes.

The `json-rpc` mode is available on all platforms including Raspberry Pi (ARM). See the [compatibility table](#platform-compatibility) below.

A minimal `docker-compose.yml` for the Signal REST API:

```yaml
version: "3"
services:
  signal-cli-rest-api:
    image: bbernhard/signal-cli-rest-api:latest
    environment:
      - MODE=json-rpc
    ports:
      - "8080:8080"
    volumes:
      - "./signal-cli-config:/home/.local/share/signal-cli"
    restart: unless-stopped
```

For full documentation and registration instructions refer to the official repository:
[https://github.com/bbernhard/signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)

### Python dependencies

```
paho-mqtt >= 2.0
websocket-client
```

Install with:

```bash
pip install paho-mqtt websocket-client
```

---

## Configuration

All configuration is passed via environment variables. There are no config files.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SIGNAL_API_URL` | yes | — | Base URL of the Signal REST API, e.g. `http://localhost:8080`. Must include `http://` or `https://`. |
| `SIGNAL_API_PHONE_NUMBER` | yes | — | The phone number registered with the Signal API, in international format e.g. `+49123456789`. |
| `MQTT_BROKER` | no | `localhost` | Hostname or IP address of the MQTT broker. |
| `MQTT_PORT` | no | `1883` | TCP port of the MQTT broker. |
| `MQTT_USERNAME` | no | — | MQTT username. Leave unset for anonymous access. |
| `MQTT_PASSWORD` | no | — | MQTT password. Only used if `MQTT_USERNAME` is set. |
| `MQTT_TOPIC_PREFIX` | no | `signal` | Prefix for all published MQTT topics. |
| `RECONNECT_DELAY` | no | `5` | Seconds to wait before attempting a WebSocket reconnect after a disconnect. |

### SIGNAL_API_URL — common mistakes

The URL **must** include the schema:

```bash
# correct
SIGNAL_API_URL=http://signal-cli-rest-api:8080

# wrong — will cause "hostname is invalid" at startup
SIGNAL_API_URL=signal-cli-rest-api:8080
```

The script validates the schema at startup and exits with a clear error message if it is missing.

---

## MQTT topics and payload

Each incoming Signal message is published to a topic derived from the sender's phone number:

```
<MQTT_TOPIC_PREFIX>/message/<sender_number>
```

The leading `+` is stripped from the sender number to keep the topic valid. Example:

```
signal/message/491603708662
```

The payload is a JSON object:

```json
{
  "sender":       "John Doe",
  "senderNumber": "+49987654321",
  "timestamp":    1774980300317,
  "message":      "Hello!",
  "account":      "+49123456789"
}
```

| Field | Description |
|---|---|
| `sender` | Display name of the sender, or phone number if no name is available. |
| `senderNumber` | Sender's phone number in international format. |
| `timestamp` | Unix timestamp in milliseconds (as provided by Signal). |
| `message` | The text content of the message. |
| `account` | The receiving Signal account (your registered number). |

Messages are published with **QoS 1** (at least once delivery).

---

## Running directly

```bash
export SIGNAL_API_URL="http://localhost:8080"
export SIGNAL_API_PHONE_NUMBER="+49123456789"
export MQTT_BROKER="localhost"
export MQTT_USERNAME="myuser"
export MQTT_PASSWORD="mypassword"

python signal2mqtt.py
```

---

## Running in Docker

A minimal `requirements.txt`:

```
paho-mqtt>=2.0
websocket-client
```

A minimal `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY signal2mqtt.py .

CMD ["python", "signal2mqtt.py"]
```

Build and run:

```bash
docker build -t signal2mqtt .

docker run -d \
  --name signal2mqtt \
  --restart unless-stopped \
  -e SIGNAL_API_URL="http://signal-cli-rest-api:8080" \
  -e SIGNAL_API_PHONE_NUMBER="+49123456789" \
  -e MQTT_BROKER="mosquitto" \
  -e MQTT_USERNAME="myuser" \
  -e MQTT_PASSWORD="mypassword" \
  signal2mqtt
```

### Docker Compose example (full stack)

```yaml
version: "3"
services:

  signal-cli-rest-api:
    image: bbernhard/signal-cli-rest-api:latest
    environment:
      - MODE=json-rpc
    ports:
      - "8080:8080"
    volumes:
      - "./signal-cli-config:/home/.local/share/signal-cli"
    restart: unless-stopped

  mosquitto:
    image: eclipse-mosquitto:latest
    ports:
      - "1883:1883"
    volumes:
      - "./mosquitto/config:/mosquitto/config"
      - "./mosquitto/data:/mosquitto/data"
    restart: unless-stopped

  signal2mqtt:
    build: .
    environment:
      - SIGNAL_API_URL=http://signal-cli-rest-api:8080
      - SIGNAL_API_PHONE_NUMBER=+49123456789
      - MQTT_BROKER=mosquitto
      - MQTT_USERNAME=myuser
      - MQTT_PASSWORD=mypassword
      - MQTT_TOPIC_PREFIX=signal
      - RECONNECT_DELAY=5
    depends_on:
      - signal-cli-rest-api
      - mosquitto
    restart: unless-stopped
```

> **Note:** When all services are in the same Docker Compose stack, use the service name as hostname (e.g. `signal-cli-rest-api`, `mosquitto`) — not `localhost`. `localhost` inside a container refers to the container itself, not the host machine.

---

## Verifying the WebSocket connection manually

Before running the script you can verify that the Signal API is reachable and delivering messages.

### Using wscat

```bash
# install wscat (one-time)
npm install -g wscat

# connect and listen
wscat -c "ws://localhost:8080/v1/receive/+49123456789" --show-ping-pong
```

### Using curl

`curl` can perform the WebSocket upgrade handshake. It will not decode the binary WebSocket frames,
but a successful HTTP 101 response confirms that the endpoint is reachable and the API is running
in `json-rpc` mode:

```bash
curl -i   --no-buffer   --header "Connection: Upgrade"   --header "Upgrade: websocket"   --header "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="   --header "Sec-WebSocket-Version: 13"   http://localhost:8080/v1/receive/+49123456789
```

A successful response starts with:

```
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
```

If you receive `404 Not Found` instead, the Signal REST API is not running in `json-rpc` mode.
If you receive `400 Bad Request` or a connection refusal, the URL or port is incorrect.

Any incoming Signal message will appear as a JSON object in the terminal (readable with `wscat`).

---

## Platform compatibility

| Mode | x86-64 | arm64 (Pi 4) | armv7 (Pi 3 and older) |
|---|---|---|---|
| `normal` | ✓ | ✓ | ✓ |
| `json-rpc` | ✓ | ✓ | ✓ |
| `native` | ✓ | ✓ | ✗ (falls back to normal) |

`json-rpc` mode is the recommended choice for this bridge on all platforms.

---

## Message filtering

The Signal REST API delivers all envelope types over the WebSocket, not just text messages. The following types are received but **discarded** by this bridge:

- Delivery receipts
- Read receipts
- Typing indicators
- Story updates
- Sync messages without a text body

Only envelopes that contain a `dataMessage` object are forwarded to MQTT.

---

## Troubleshooting

**`hostname is invalid` at startup**
The `SIGNAL_API_URL` is missing the `http://` schema. Set it to `http://hostname:port`.

**`MQTT connection failed, code 5: not authorized`**
The MQTT broker rejected the credentials. Check `MQTT_USERNAME` and `MQTT_PASSWORD`. The script exits immediately on this error — retrying with wrong credentials is pointless.

**WebSocket connects but no messages arrive**
Verify that the Signal REST API is actually running in `json-rpc` mode. Check the container logs:
```bash
docker logs signal-cli-rest-api | grep MODE
```
In `normal` or `native` mode the WebSocket endpoint does not exist and the connection will be closed immediately.

**Messages arrive but nothing is published to MQTT**
Enable debug logging to see what envelope types are being received and discarded:
```python
logging.basicConfig(level=logging.DEBUG, ...)
```

---

## License

MIT License

Copyright (c) 2026 Marcus Brosda

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

**The software is provided "as is", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability,
fitness for a particular purpose and noninfringement. In no event shall the
authors or copyright holders be liable for any claim, damages or other
liability, whether in an action of contract, tort or otherwise, arising from,
out of or in connection with the software or the use or other dealings in the
software.**