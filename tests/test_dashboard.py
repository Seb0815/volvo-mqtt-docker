import http.client
import json
import unittest
from unittest.mock import Mock, patch

from test_app import VIN, config, energy, odometer
from volvo_mqtt.api import ApiError, Volvo
from volvo_mqtt.bridge import Bridge, Publisher
from volvo_mqtt.dashboard import Dashboard, Status


class StatusTests(unittest.TestCase):
    def test_initial_status_is_offline_and_contains_no_credentials(self):
        status = Status(config())
        snapshot = status.snapshot()
        self.assertFalse(snapshot["online"])
        self.assertTrue(snapshot["stale"])
        self.assertFalse(snapshot["mqtt"]["connected"])
        self.assertIsNone(snapshot["poll"]["last_success"])
        self.assertEqual(snapshot["areas"]["energy"]["data"], {})
        for secret in ("test-secret", "test-key", "test-client", "access_token", "refresh_token"):
            self.assertNotIn(secret, json.dumps(snapshot))

    def test_success_staleness_and_disconnect(self):
        status = Status(config())
        status.vehicle(VIN)
        status.mqtt(True)
        status.begin_poll()
        status.received("energy", energy(), True)
        status.finish_poll({})
        status.schedule(300)
        snapshot = status.snapshot()
        self.assertTrue(snapshot["online"])
        self.assertFalse(snapshot["poll"]["in_progress"])
        self.assertIsNotNone(snapshot["poll"]["next_attempt"])
        with patch("volvo_mqtt.dashboard.time.time", return_value=status.last_success_time + 601):
            self.assertTrue(status.snapshot()["stale"])
            self.assertFalse(status.snapshot()["online"])
        status.mqtt(False, "Broker nicht erreichbar")
        self.assertFalse(status.snapshot()["online"])
        self.assertEqual(status.snapshot()["mqtt"]["error"], "Broker nicht erreichbar")

    def test_errors_preserve_last_values_and_success_time(self):
        status = Status(config())
        data = energy()
        status.received("energy", data, True)
        status.finish_poll({})
        success = status.snapshot()["poll"]["last_success"]
        data["batteryChargeLevel"]["value"] = 0
        status.area_error("energy", "HTTP 403")
        status.finish_poll({"energy": "HTTP 403"})
        snapshot = status.snapshot()
        self.assertEqual(snapshot["areas"]["energy"]["data"]["batteryChargeLevel"]["value"], 61)
        self.assertEqual(snapshot["poll"]["last_success"], success)
        self.assertFalse(snapshot["areas"]["energy"]["available"])
        self.assertEqual(snapshot["poll"]["errors"], {"energy": "HTTP 403"})
        snapshot["areas"]["energy"]["data"].clear()
        self.assertTrue(status.snapshot()["areas"]["energy"]["data"])

    def test_bridge_updates_dashboard_even_on_partial_failure(self):
        cfg = config()
        status = Status(cfg)
        api = Mock(spec=Volvo)
        publisher = Mock(spec=Publisher)
        api.energy.return_value = energy()
        api.odometer.return_value = odometer()
        bridge = Bridge(cfg, api, publisher, VIN, status=status)
        bridge.poll()
        self.assertEqual(status.snapshot()["areas"]["odometer"]["data"], odometer())
        api.energy.side_effect = ApiError(403)
        bridge.poll()
        snapshot = status.snapshot()
        self.assertIn("energy", snapshot["poll"]["errors"])
        self.assertFalse(snapshot["areas"]["energy"]["available"])
        self.assertTrue(snapshot["areas"]["odometer"]["available"])
        self.assertEqual(snapshot["areas"]["energy"]["data"], energy())

    def test_rate_limit_marks_both_areas_unavailable(self):
        cfg = config()
        status = Status(cfg)
        api = Mock(spec=Volvo)
        api.energy.side_effect = ApiError(429, 600)
        bridge = Bridge(cfg, api, Mock(spec=Publisher), VIN, status=status)
        self.assertEqual(bridge.poll(), 600)
        snapshot = status.snapshot()
        self.assertFalse(snapshot["areas"]["odometer"]["available"])
        self.assertIn("Abfragelimit", snapshot["areas"]["odometer"]["error"])
        self.assertFalse(snapshot["poll"]["in_progress"])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.status = Status(config())
        self.dashboard = Dashboard(config(dashboard_port=0), self.status)
        self.dashboard.__enter__()
        self.addCleanup(self.dashboard.__exit__, None, None, None)
        self.port = self.dashboard.server.server_port

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_page_and_assets_are_self_contained(self):
        for path, content_type in (
            ("/", "text/html"),
            ("/dashboard.css", "text/css"),
            ("/dashboard.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 200)
                self.assertIn(content_type, headers["Content-Type"])
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"lang=\"de\"", self.request("/")[2])

    def test_status_endpoint_returns_latest_snapshot(self):
        self.assertFalse(json.loads(self.request("/api/status")[2])["online"])
        self.status.vehicle(VIN)
        self.status.mqtt(True)
        self.status.received("energy", energy(), True)
        self.status.finish_poll({})
        code, headers, body = self.request("/api/status?refresh=1")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["Content-Type"])
        snapshot = json.loads(body)
        self.assertTrue(snapshot["online"])
        self.assertEqual(snapshot["vin"], VIN)
        self.assertEqual(snapshot["areas"]["energy"]["data"], energy())
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_unknown_and_sensitive_paths_are_not_served(self):
        for path in ("/.env", "/data/tokens.json", "/../config.py", "/auth", "/callback"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/status", method="POST")[0], 501)

    def test_disabled_dashboard_does_not_bind(self):
        with Dashboard(config(dashboard_enabled=False), self.status) as dashboard:
            self.assertIsNone(dashboard.server)

    def test_dashboard_request_does_not_call_volvo(self):
        with patch("volvo_mqtt.api.Http.json") as call:
            for _ in range(3):
                self.assertEqual(self.request("/api/status")[0], 200)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
