import base64
import hashlib
import http.client
import json
import queue
import socketserver
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from test_app import VIN, config, energy, odometer
from volvo_mqtt.api import ApiError, BridgeError, Http, Tokens, Volvo
from volvo_mqtt.auth import authorize
from volvo_mqtt.bridge import Bridge, Publisher


class WireBroker(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.messages = queue.Queue()
        self.connects = queue.Queue()
        super().__init__(("127.0.0.1", 0), WireHandler)


class WireHandler(socketserver.StreamRequestHandler):
    """Minimal MQTT 3.1.1 peer for exercising the real Paho publish path."""

    def handle(self):
        while header := self.rfile.read(1):
            multiplier = 1
            length = 0
            while True:
                digit = self.rfile.read(1)
                if not digit:
                    return
                length += (digit[0] & 127) * multiplier
                if not digit[0] & 128:
                    break
                multiplier *= 128
            payload = self.rfile.read(length)
            kind = header[0] >> 4
            if kind == 1:
                self.server.connects.put(payload)
                self.wfile.write(b"\x20\x02\x00\x00")
            elif kind == 3:
                topic_length = int.from_bytes(payload[:2], "big")
                offset = 2 + topic_length
                qos = (header[0] >> 1) & 3
                packet_id = payload[offset:offset + 2] if qos else b""
                body = payload[offset + (2 if qos else 0):]
                self.server.messages.put((
                    payload[2:offset].decode(), body.decode(), header[0],
                ))
                if qos == 1:
                    self.wfile.write(b"\x40\x02" + packet_id)
            elif kind == 12:
                self.wfile.write(b"\xd0\x00")
            elif kind == 14:
                return
            self.wfile.flush()


class LocalServers(unittest.TestCase):
    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.addCleanup(close)
        return server

    def test_http_to_real_paho_mqtt_with_refresh_and_retry(self):
        calls = []
        energy_requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def respond(self, status, data, headers=None):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(("POST", self.path, dict(self.headers), parse_qs(body.decode())))
                self.respond(200, {
                    "access_token": "renewed-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 1800,
                })

            def do_GET(self):
                nonlocal energy_requests
                calls.append(("GET", self.path, dict(self.headers), None))
                if self.path.endswith("/state"):
                    energy_requests += 1
                    if energy_requests == 1:
                        self.respond(401, {"private": "must not appear in errors"})
                    else:
                        self.respond(200, energy())
                elif self.path.endswith("/odometer"):
                    self.respond(200, {"data": odometer()})
                elif self.path == "/limited":
                    self.respond(429, {"secret": "not-for-log"}, {"Retry-After": "600"})
                elif self.path == "/redirect":
                    self.respond(302, {}, {"Location": "/forbidden"})
                else:
                    self.respond(404, {})

        server = self.serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        broker = self.serve(WireBroker())
        url = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(
                vin=VIN,
                mqtt_port=broker.server_address[1],
                token_file=Path(directory) / "tokens.json",
            )
            http = Http()
            tokens = Tokens(cfg, http)
            tokens.token = {
                "access_token": "initial-access",
                "refresh_token": "initial-refresh",
                "expires_at": time.time() + 900,
            }
            publisher = Publisher(cfg, VIN)
            publisher.start()
            try:
                self.assertTrue(publisher.connected.wait(timeout=5), "Paho did not connect")
                with (
                    patch("volvo_mqtt.api.API_URL", url),
                    patch("volvo_mqtt.api.TOKEN_URL", url + "/token"),
                ):
                    bridge = Bridge(cfg, Volvo(cfg, tokens, http), publisher, VIN)
                    self.assertEqual(bridge.poll(), 300)
            finally:
                publisher.close()

            messages = []
            while not broker.messages.empty():
                messages.append(broker.messages.get_nowait())
            values = {topic: payload for topic, payload, _ in messages}
            base = f"volvo/{VIN}"
            self.assertEqual(values[f"{base}/energy/batteryChargeLevel"], "61")
            self.assertEqual(values[f"{base}/odometer/value"], "12345")
            self.assertEqual(json.loads(values[f"{base}/energy"]), energy())
            self.assertEqual(values[f"{base}/availability"], "offline")  # Graceful shutdown.
            self.assertIn((f"{base}/availability", "online", 0x33), messages)
            self.assertTrue(all(header & 1 and (header >> 1) & 3 == 1 for _, _, header in messages))
            connect = broker.connects.get(timeout=1)
            self.assertEqual(connect[7] & 0x2C, 0x2C)  # Retained QoS 1 Last Will.
            self.assertIn(f"{base}/availability".encode(), connect)
            self.assertIn(b"offline", connect)
            self.assertEqual(json.loads(cfg.token_file.read_text())["refresh_token"], "rotated-refresh")
            self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET", "GET"])
            self.assertEqual(calls[1][3]["refresh_token"], ["initial-refresh"])
            self.assertEqual(calls[2][2]["Authorization"], "Bearer renewed-access")
            headers = {name.lower(): value for name, value in calls[2][2].items()}
            self.assertEqual(headers["vcc-api-key"], "test-key")

            with self.assertRaises(ApiError) as limited:
                http.json(Request(url + "/limited"))
            self.assertEqual(limited.exception.retry_after, 600)
            self.assertNotIn("not-for-log", str(limited.exception))
            with self.assertRaises(ApiError) as redirect:
                http.json(Request(url + "/redirect"))
            self.assertEqual(redirect.exception.status, 302)
            self.assertNotIn("/forbidden", [call[1] for call in calls])

    def test_mqtt_publish_requires_connection(self):
        publisher = Publisher(config(), VIN)
        with self.assertRaisesRegex(BridgeError, "nicht verbunden"):
            publisher.send("energy", "{}")
        publisher.close()


class AuthTests(unittest.TestCase):
    def request(self, port, path, cookie=""):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", path, headers={"Cookie": cookie})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def flow(self, denied=False, grant_error=False):
        from http.server import HTTPServer

        ready = threading.Event()
        servers = []
        failures = []
        tokens = Mock(spec=Tokens)
        if grant_error:
            tokens.grant.side_effect = BridgeError("test token exchange failure")

        def factory(address, handler):
            server = HTTPServer(("127.0.0.1", 0), handler)
            servers.append(server)
            ready.set()
            return server

        def worker():
            try:
                authorize(config(), tokens)
            except BridgeError as exc:
                failures.append(exc)

        with patch("volvo_mqtt.auth.HTTPServer", side_effect=factory):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(timeout=3))
            port = servers[0].server_port
            start = self.request(port, "/auth")
            parameters = parse_qs(urlsplit(start[1]["Location"]).query)
            cookie = start[1]["Set-Cookie"].split(";")[0]
            state = parameters["state"][0]
            bad_state = self.request(port, "/callback?state=invalid&code=bad", cookie)
            no_cookie = self.request(
                port, "/callback?" + urlencode({"state": state, "code": "bad"})
            )
            unicode_state = self.request(port, "/callback?state=%C3%A4&code=bad", cookie)
            query = {"state": state}
            query.update({"error": "access_denied"} if denied else {"code": "test-code"})
            callback = self.request(port, "/callback?" + urlencode(query), cookie)
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(start[0], 302)
        self.assertEqual(start[1]["Cache-Control"], "no-store")
        self.assertEqual(bad_state[0], 400)
        self.assertEqual(no_cookie[0], 400)
        self.assertEqual(unicode_state[0], 400)
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertEqual(parameters["response_type"], ["code"])
        self.assertIn("energy:state:read", parameters["scope"][0])
        return parameters, tokens, callback, failures

    def test_authorization_pkce_browser_binding_and_success(self):
        parameters, tokens, callback, failures = self.flow()
        self.assertEqual(failures, [])
        self.assertEqual(callback[0], 200)
        fields = tokens.grant.call_args.args[0]
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code"], "test-code")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(fields["code_verifier"].encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(parameters["code_challenge"], [challenge])

    def test_oauth_denial_is_not_success(self):
        _, tokens, callback, failures = self.flow(denied=True)
        tokens.grant.assert_not_called()
        self.assertEqual(callback[0], 400)
        self.assertEqual(len(failures), 1)

    def test_token_exchange_failure_is_not_success(self):
        _, _, callback, failures = self.flow(grant_error=True)
        self.assertEqual(callback[0], 502)
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
