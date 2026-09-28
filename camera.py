"""
Kamera-Funktionen: Schnappschuss holen und Bewegungsalarme von motionEye empfangen.

- SNAPSHOT_URL: Einzelbild-URL (z.B. motionEye „Snapshot URL“ oder ESP32-CAM /capture)
- motionEye ruft bei Bewegung eine Webhook-URL auf; dafür startet der Bot einen
  kleinen HTTP-Server (nur ein Endpunkt, mit geheimem Schlüssel).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger("mediathek-bot.camera")

SNAPSHOT_URL = os.environ.get("SNAPSHOT_URL", "").strip()
CAMERA_USER = os.environ.get("CAMERA_USER", "")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8089"))
WEBHOOK_KEY = os.environ.get("WEBHOOK_KEY", "").strip()
MOTION_COOLDOWN = int(os.environ.get("MOTION_COOLDOWN", "120"))  # Sekunden
STATE_FILE = Path(
    os.environ.get("CAMERA_STATE_FILE", "~/.config/mediathek-bot/camera.json")
).expanduser()


def enabled() -> bool:
    return bool(SNAPSHOT_URL)


# ---------------------------------------------------------------- Zustand (Alarm an/aus)
def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"alarm": True}


def alarm_on() -> bool:
    return bool(_load().get("alarm", True))


def set_alarm(on: bool) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _load()
    data["alarm"] = on
    STATE_FILE.write_text(json.dumps(data))


# ---------------------------------------------------------------- Schnappschuss
async def snapshot() -> bytes:
    auth = (CAMERA_USER, CAMERA_PASSWORD) if CAMERA_USER else None
    async with httpx.AsyncClient(timeout=15, auth=auth) as client:
        r = await client.get(SNAPSHOT_URL, follow_redirects=True)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image"):
            raise ValueError(f"Kein Bild erhalten (Content-Type: {r.headers.get('content-type')})")
        return r.content


# ---------------------------------------------------------------- Webhook-Server
async def start_webhook(on_motion) -> asyncio.base_events.Server | None:
    """
    Startet einen minimalen HTTP-Server. motionEye ruft
    http://<pi>:WEBHOOK_PORT/motion?key=WEBHOOK_KEY auf, dann wird on_motion() ausgeführt.
    """
    if not WEBHOOK_KEY:
        log.info("Kein WEBHOOK_KEY gesetzt – Bewegungsalarm deaktiviert.")
        return None

    last = {"t": 0.0}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        code, body = 404, "not found"
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace")
            # Header überspringen
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split()
            if len(parts) >= 2:
                url = urlparse(parts[1])
                key = parse_qs(url.query).get("key", [""])[0]
                if url.path.rstrip("/") == "/motion":
                    if key != WEBHOOK_KEY:
                        code, body = 403, "forbidden"
                    else:
                        code, body = 200, "ok"
                        now = time.monotonic()
                        if alarm_on() and now - last["t"] >= MOTION_COOLDOWN:
                            last["t"] = now
                            asyncio.create_task(on_motion())
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}[code]
                writer.write(
                    f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode()
                )
                await writer.drain()
                writer.close()
            except ConnectionError:
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", WEBHOOK_PORT)
    log.info("Bewegungs-Webhook lauscht auf Port %s", WEBHOOK_PORT)
    return server
