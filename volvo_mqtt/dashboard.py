import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Config

LOG = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Status:
    def __init__(self, config: Config) -> None:
        self.lock = threading.Lock()
        self.last_success_time = 0.0
        self.poll_interval = config.poll_interval
        self.data: dict[str, Any] = {
            "started_at": timestamp(),
            "vin": config.vin,
            "topic_prefix": config.topic_prefix,
            "poll_interval": config.poll_interval,
            "mqtt": {"connected": False, "error": "", "last_publish": None},
            "poll": {
                "in_progress": False,
                "last_attempt": None,
                "last_success": None,
                "next_attempt": None,
                "errors": {},
            },
            "areas": {
                area: {"data": {}, "available": False, "received_at": None, "error": ""}
                for area in ("energy", "odometer")
            },
        }

    def vehicle(self, vin: str) -> None:
        with self.lock:
            self.data["vin"] = vin

    def mqtt(self, connected: bool, error: str = "") -> None:
        with self.lock:
            self.data["mqtt"].update(connected=connected, error=error)

    def published(self) -> None:
        with self.lock:
            self.data["mqtt"]["last_publish"] = timestamp()

    def begin_poll(self) -> None:
        with self.lock:
            self.data["poll"].update(
                in_progress=True, last_attempt=timestamp(), next_attempt=None
            )

    def received(self, area: str, data: dict[str, Any], available: bool) -> None:
        with self.lock:
            self.data["areas"][area].update(
                data=copy.deepcopy(data), available=available, received_at=timestamp(), error=""
            )

    def area_error(self, area: str, error: str) -> None:
        with self.lock:
            self.data["areas"][area].update(available=False, error=error)

    def finish_poll(self, errors: dict[str, str]) -> None:
        with self.lock:
            self.data["poll"].update(in_progress=False, errors=dict(errors))
            if not errors:
                self.last_success_time = time.time()
                self.data["poll"]["last_success"] = timestamp()

    def schedule(self, delay: float) -> None:
        with self.lock:
            self.data["poll"]["next_attempt"] = datetime.fromtimestamp(
                time.time() + delay, timezone.utc
            ).isoformat(timespec="seconds")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = copy.deepcopy(self.data)
            stale = (
                not self.last_success_time
                or time.time() - self.last_success_time > max(600, self.poll_interval * 2)
            )
        result["server_time"] = timestamp()
        result["stale"] = stale
        result["online"] = (
            result["mqtt"]["connected"]
            and not result["poll"]["errors"]
            and not stale
        )
        return result


class Dashboard:
    def __init__(self, config: Config, status: Status) -> None:
        self.config = config
        self.status = status
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "Dashboard":
        if not self.config.dashboard_enabled:
            return self
        status = self.status
        static = Path(__file__).with_name("static")
        assets = {
            "/": ("text/html; charset=utf-8", (static / "index.html").read_bytes()),
            "/dashboard.css": ("text/css; charset=utf-8", (static / "dashboard.css").read_bytes()),
            "/dashboard.js": ("text/javascript; charset=utf-8", (static / "dashboard.js").read_bytes()),
        }

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/api/status":
                    code, content_type, body = (
                        200,
                        "application/json; charset=utf-8",
                        json.dumps(status.snapshot(), allow_nan=False).encode(),
                    )
                elif path in assets:
                    content_type, body = assets[path]
                    code = 200
                else:
                    code, content_type, body = 404, "text/plain; charset=utf-8", b"Nicht gefunden"
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                    "base-uri 'none'; object-src 'none'; form-action 'none'",
                )
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    LOG.debug("Dashboard-Client hat die Verbindung geschlossen")

        self.server = ThreadingHTTPServer(
            (self.config.dashboard_bind_host, self.config.dashboard_port), Handler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        LOG.info("Dashboard gestartet auf Port %s", self.server.server_port)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join()
