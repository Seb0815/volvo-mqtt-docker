import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import paho.mqtt.client as mqtt

from test_app import ENV, VIN, energy, odometer


class Observer:
    def __init__(self, host: str, topic: str) -> None:
        self.messages: list[tuple[str, str, bool, int]] = []
        self.changed = threading.Condition()
        self.ready = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message
        self.host = host
        self.topic = topic

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe(self.topic, qos=1)

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if all(not code.is_failure for code in reason_codes):
            self.ready.set()

    def on_message(self, client, userdata, message):
        with self.changed:
            self.messages.append((
                message.topic, message.payload.decode(), message.retain, message.qos,
            ))
            self.changed.notify_all()

    def start(self) -> None:
        self.client.connect(self.host, 1883)
        self.client.loop_start()
        if not self.ready.wait(timeout=10):
            raise AssertionError("Mosquitto subscription was not acknowledged")

    def mark(self) -> int:
        with self.changed:
            return len(self.messages)

    def wait(
        self, topic: str, payload: str, retained: bool | None = None, after: int = 0
    ) -> None:
        with self.changed:
            received = self.changed.wait_for(
                lambda: any(
                    name == topic
                    and value == payload
                    and (retained is None or retained == retain)
                    and qos == 1
                    for name, value, retain, qos in self.messages[after:]
                ),
                timeout=15,
            )
            if not received:
                raise AssertionError(
                    f"Missing MQTT message {topic}={payload!r}; received: {self.messages}"
                )

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


@unittest.skipUnless(os.environ.get("MQTT_TEST_HOST"), "Run with sh tests/docker-test.sh")
class DockerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_file = Path(self.directory.name) / "tokens.json"
        self.token_file.write_text(json.dumps({
            "access_token": "expired-test-access",
            "refresh_token": "test-refresh",
            "expires_at": 0,
        }))
        self.energy_error = False
        self.api_requests = []
        self.token_requests = 0
        self.prefix = f"docker-test/{uuid.uuid4().hex}"
        self.base = f"{self.prefix}/{VIN}"
        self.host = os.environ["MQTT_TEST_HOST"]
        self.observer = Observer(self.host, self.base + "/#")
        self.addCleanup(self.observer.close)
        self.observer.start()
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def respond(self, code, data, headers=None):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                case.token_requests += 1
                self.respond(200, {
                    "access_token": "docker-test-access",
                    "refresh_token": "docker-test-rotated-refresh",
                    "expires_in": 1800,
                })

            def do_GET(self):
                case.api_requests.append(self.path)
                if self.path.endswith("/state"):
                    if case.energy_error:
                        self.respond(429, {"error": "limited"}, {"Retry-After": "600"})
                    else:
                        self.respond(200, energy())
                elif self.path.endswith("/odometer"):
                    self.respond(200, {"data": odometer()})
                else:
                    self.respond(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)
        self.process = None
        self.output = ""
        self.addCleanup(self.stop_app)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)

    def start_app(self):
        environment = os.environ | ENV | {
            "VOLVO_VIN": VIN,
            "TOKEN_FILE": str(self.token_file),
            "MQTT_HOST": self.host,
            "MQTT_PORT": "1883",
            "MQTT_CLIENT_ID": "app-" + uuid.uuid4().hex,
            "MQTT_TOPIC_PREFIX": self.prefix,
            "TEST_VOLVO_URL": f"http://127.0.0.1:{self.server.server_port}",
            "DASHBOARD_ENABLED": "true",
            "DASHBOARD_BIND_HOST": "127.0.0.1",
            "DASHBOARD_PORT": "8080",
        }
        # Redirect only this test subprocess; production API URLs stay fixed.
        bootstrap = (
            "import os, runpy; import volvo_mqtt.api as api; "
            "api.API_URL = os.environ['TEST_VOLVO_URL']; "
            "api.TOKEN_URL = api.API_URL + '/token'; "
            "runpy.run_module('volvo_mqtt', run_name='__main__')"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", bootstrap, "run"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop_app(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.output, _ = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.output, _ = self.process.communicate(timeout=5)
            self.fail("Bridge did not stop on SIGTERM:\n" + self.output)

    def dashboard_status(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with urlopen("http://127.0.0.1:8080/api/status", timeout=3) as response:
                status = json.load(response)
            if not status["poll"]["in_progress"] and status["poll"]["next_attempt"]:
                return status
            time.sleep(0.05)
        self.fail("Dashboard did not report the completed poll")

    def test_cli_publishes_json_scalars_and_retained_values(self):
        self.assertEqual(os.getuid(), 10001)
        self.assertTrue(os.statvfs("/").f_flag & os.ST_RDONLY)
        self.start_app()
        self.observer.wait(self.base + "/availability", "online")
        self.observer.wait(self.base + "/energy/batteryChargeLevel", "61")
        self.observer.wait(self.base + "/energy/electricRange", "240")
        self.observer.wait(self.base + "/energy/chargingStatus", "IDLE")
        self.observer.wait(self.base + "/odometer/value", "12345")
        self.observer.wait(
            self.base + "/energy", json.dumps(energy(), separators=(",", ":"))
        )
        self.observer.wait(
            self.base + "/odometer", json.dumps(odometer(), separators=(",", ":"))
        )
        self.observer.wait(self.base + "/error", "{}")
        status = self.dashboard_status()
        self.assertTrue(status["online"])
        self.assertTrue(status["mqtt"]["connected"])
        self.assertEqual(status["areas"]["energy"]["data"], energy())
        self.assertEqual(status["areas"]["odometer"]["data"], odometer())
        with urlopen("http://127.0.0.1:8080/", timeout=3) as response:
            self.assertIn(b"Alles im Blick.", response.read())
        self.assertNotIn("docker-test-access", json.dumps(status))
        self.assertNotIn("docker-test-rotated-refresh", json.dumps(status))

        late = Observer(self.host, self.base + "/#")
        self.addCleanup(late.close)
        late.start()
        late.wait(self.base + "/energy/batteryChargeLevel", "61", retained=True)
        late.wait(self.base + "/availability", "online", retained=True)

        before_stop = self.observer.mark()
        self.stop_app()
        self.assertEqual(self.process.returncode, 0, self.output)
        self.observer.wait(self.base + "/availability", "offline", after=before_stop)
        self.assertEqual(self.token_requests, 1)
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "docker-test-rotated-refresh")
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(self.api_requests, [
            f"/energy/v2/vehicles/{VIN}/state",
            f"/connected-vehicle/v2/vehicles/{VIN}/odometer",
        ])

    def test_broker_sends_last_will_after_process_crash(self):
        self.start_app()
        self.observer.wait(self.base + "/availability", "online")
        before_crash = self.observer.mark()
        self.process.kill()
        self.process.wait(timeout=5)
        self.observer.wait(self.base + "/availability", "offline", after=before_crash)
        late = Observer(self.host, self.base + "/availability")
        self.addCleanup(late.close)
        late.start()
        late.wait(self.base + "/availability", "offline", retained=True)

    def test_rate_limit_is_reported_without_further_vehicle_requests(self):
        self.energy_error = True
        self.start_app()
        self.observer.wait(self.base + "/error", json.dumps({
            "energy": "Volvo HTTP 429: Volvo-Abfragelimit erreicht; Abfrageintervall erhoehen",
        }, separators=(",", ":")))
        self.observer.wait(self.base + "/energy/availability", "offline")
        self.observer.wait(self.base + "/odometer/availability", "offline")
        status = self.dashboard_status()
        self.assertFalse(status["online"])
        self.assertIn("429", status["poll"]["errors"]["energy"])
        self.assertFalse(status["areas"]["odometer"]["available"])
        self.stop_app()
        self.assertEqual(self.process.returncode, 0, self.output)
        self.assertEqual(self.api_requests, [f"/energy/v2/vehicles/{VIN}/state"])


if __name__ == "__main__":
    unittest.main()
