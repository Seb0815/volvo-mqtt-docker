import json
import os
import stat
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError
from urllib.request import Request

from volvo_mqtt.api import ApiError, BridgeError, Http, Tokens, Volvo, retry_seconds
from volvo_mqtt.bridge import Bridge, Publisher, measurements, run
from volvo_mqtt.config import Config

VIN = "YV1AB12C3D1234567"
ENV = {
    "VOLVO_CLIENT_ID": "test-client",
    "VOLVO_CLIENT_SECRET": "test-secret",
    "VOLVO_API_KEY": "test-key",
    "MQTT_HOST": "127.0.0.1",
}


def config(**changes):
    with patch.dict(os.environ, ENV, clear=True):
        return replace(Config.from_env("run"), **changes)


def energy():
    return {
        "batteryChargeLevel": {
            "status": "OK",
            "value": 61,
            "unit": "percentage",
            "updatedAt": "2026-10-24T20:00:00Z",
        },
        "electricRange": {"status": "OK", "value": 240, "unit": "km"},
        "chargingStatus": {"status": "OK", "value": "IDLE"},
        "chargingPower": {"status": "ERROR", "code": "NOT_SUPPORTED"},
    }


def odometer():
    return {"odometer": {"value": 12345, "unit": "km", "timestamp": "2026-10-24T20:00:00Z"}}


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        cfg = config()
        self.assertEqual(cfg.poll_interval, 300)
        self.assertEqual(cfg.mqtt_port, 1883)
        self.assertEqual(cfg.topic_prefix, "volvo")
        for name, value in (
            ("VOLVO_CLIENT_ID", ""),
            ("MQTT_HOST", ""),
            ("POLL_INTERVAL_SECONDS", "59"),
            ("POLL_INTERVAL_SECONDS", "abc"),
            ("MQTT_PORT", "65536"),
            ("MQTT_TOPIC_PREFIX", "volvo/+"),
            ("MQTT_TOPIC_PREFIX", "/"),
            ("MQTT_TLS", "yes"),
            ("DASHBOARD_ENABLED", "yes"),
            ("DASHBOARD_PORT", "0"),
            ("MQTT_CA_CERT", "/missing.pem"),
            ("MQTT_PASSWORD", "secret"),
            ("VOLVO_VIN", "not-a-vin"),
            ("VOLVO_REDIRECT_URI", "http://example.com/callback"),
            ("VOLVO_REDIRECT_URI", "https://example.com/other"),
        ):
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, ENV | {name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        Config.from_env("run")

    def test_auth_needs_no_broker_or_api_key(self):
        with patch.dict(os.environ, {
            "VOLVO_CLIENT_ID": "id",
            "VOLVO_CLIENT_SECRET": "secret",
        }, clear=True):
            self.assertEqual(Config.from_env("auth").mqtt_host, "")

    def test_tls_defaults_and_vin_normalization(self):
        with patch.dict(os.environ, ENV | {
            "MQTT_TLS": "true", "VOLVO_VIN": VIN.lower(),
        }, clear=True):
            cfg = Config.from_env("run")
        self.assertEqual(cfg.mqtt_port, 8883)
        self.assertEqual(cfg.vin, VIN)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tokens.json"
        self.http = Mock(spec=Http)
        self.tokens = Tokens(config(token_file=self.path), self.http)
        self.http.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 1800,
        }

    def test_grant_is_persisted_with_private_permissions(self):
        self.tokens.grant({"grant_type": "authorization_code", "code": "test-code"})
        self.assertEqual(self.tokens.access(), "new-access")
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "new-refresh")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        request = self.http.json.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_rotating_refresh_token_survives_restart(self):
        self.tokens.token = {
            "access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0,
        }
        self.assertEqual(self.tokens.access(), "new-access")
        self.assertIn(b"refresh_token=old-refresh", self.http.json.call_args.args[0].data)
        loaded = Tokens(config(token_file=self.path), self.http)
        loaded.load()
        self.assertEqual(loaded.token["refresh_token"], "new-refresh")

    def test_refresh_without_rotated_token_preserves_previous_token(self):
        self.tokens.token = {"refresh_token": "old-refresh", "expires_at": 0}
        self.http.json.return_value.pop("refresh_token")
        self.tokens.access()
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "old-refresh")

    def test_save_failure_does_not_replace_in_memory_token(self):
        self.tokens.token = {"refresh_token": "old-refresh"}
        with patch("volvo_mqtt.api.os.replace", side_effect=PermissionError):
            with self.assertRaisesRegex(BridgeError, "dauerhaft"):
                self.tokens.grant({})
        self.assertEqual(self.tokens.token["refresh_token"], "old-refresh")
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_invalid_token_responses(self):
        for changes in (
            {"access_token": ""},
            {"refresh_token": None},
            {"expires_in": None},
            {"expires_in": float("nan")},
            {"expires_in": -1},
        ):
            with self.subTest(changes=changes):
                self.http.json.return_value = {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 1800,
                } | changes
                with self.assertRaises(BridgeError):
                    self.tokens.grant({})
                self.assertFalse(self.path.exists())

    def test_missing_and_corrupt_file_errors_are_safe(self):
        with self.assertRaisesRegex(BridgeError, "auth"):
            self.tokens.load()
        for content in ("secret-not-json", "[]", '{"access_token":"secret"}'):
            self.path.write_text(content)
            with self.assertRaises(BridgeError) as error:
                self.tokens.load()
            self.assertNotIn("secret", str(error.exception))

    def test_valid_access_does_not_refresh(self):
        self.tokens.token = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": time.time() + 600,
        }
        self.assertEqual(self.tokens.access(), "access")
        self.http.json.assert_not_called()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tokens = Mock(spec=Tokens)
        self.tokens.access.side_effect = ["first-token", "second-token"]
        self.http = Mock(spec=Http)
        self.api = Volvo(config(), self.tokens, self.http)

    def test_refresh_and_retry_once_after_401(self):
        self.http.json.side_effect = [ApiError(401), {"data": odometer()}]
        self.assertEqual(self.api.odometer(VIN), odometer())
        self.assertEqual(self.tokens.access.call_count, 2)
        self.tokens.access.assert_called_with(force=True)
        requests = [call.args[0] for call in self.http.json.call_args_list]
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer second-token")
        self.assertEqual(requests[1].get_header("Vcc-api-key"), "test-key")

    def test_does_not_loop_on_repeated_401_or_retry_403(self):
        for status, count in ((401, 2), (403, 1), (429, 1)):
            with self.subTest(status=status):
                self.setUp()
                self.http.json.side_effect = ApiError(status)
                with self.assertRaises(ApiError):
                    self.api.get("/test")
                self.assertEqual(self.http.json.call_count, count)

    def test_energy_v2_is_not_wrapped(self):
        self.http.json.return_value = energy()
        self.assertEqual(self.api.energy(VIN), energy())
        self.assertTrue(self.http.json.call_args.args[0].full_url.endswith(f"/{VIN}/state"))

    def test_malformed_endpoint_responses_are_not_empty_successes(self):
        for response in ({}, {"data": {}}):
            self.http.json.return_value = response
            self.tokens.access.side_effect = None
            with self.assertRaises(BridgeError):
                self.api.energy(VIN)
            with self.assertRaises(BridgeError):
                self.api.odometer(VIN)

    def test_discovery_requires_one_vehicle(self):
        self.http.json.return_value = {"data": [{"vin": VIN}]}
        self.assertEqual(self.api.vin(), VIN)
        self.tokens.access.side_effect = None
        for vehicles in ([], [{"vin": VIN}, {"vin": VIN}], [{"vin": "bad"}], [None]):
            self.http.json.return_value = {"data": vehicles}
            with self.assertRaises(BridgeError):
                self.api.vin()

    def test_explicit_vin_avoids_discovery(self):
        api = Volvo(config(vin=VIN), self.tokens, self.http)
        self.assertEqual(api.vin(), VIN)
        self.http.json.assert_not_called()

    def test_retry_after_supports_seconds_dates_and_invalid_headers(self):
        self.assertEqual(retry_seconds("240"), 240)
        self.assertEqual(retry_seconds("-1"), 0)
        self.assertEqual(retry_seconds("invalid"), 0)
        self.assertEqual(retry_seconds(None), 0)
        self.assertEqual(retry_seconds("Thu, 01 Jan 1970 00:00:00 GMT"), 0)
        future = time.time() + 600
        from email.utils import formatdate
        self.assertGreater(retry_seconds(formatdate(future, usegmt=True)), 598)

    def test_http_network_and_json_errors_are_explicit(self):
        http = Http()
        http.opener = MagicMock()
        http.opener.open.side_effect = URLError("sensitive detail")
        with self.assertRaises(BridgeError) as error:
            http.json(Request("https://example.invalid"))
        self.assertNotIn("sensitive", str(error.exception))
        http.opener.open.side_effect = None
        response = http.opener.open.return_value.__enter__.return_value
        for body in (b"not JSON", b"[]", b"x" * 2_000_001):
            response.read.return_value = body
            with self.assertRaises(BridgeError):
                http.json(Request("https://example.invalid"))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(spec=Volvo)
        self.api.energy.return_value = energy()
        self.api.odometer.return_value = odometer()
        self.publisher = Mock(spec=Publisher)
        self.bridge = Bridge(config(), self.api, self.publisher, VIN)

    def messages(self):
        return dict(call.args for call in self.publisher.send.call_args_list)

    def test_documented_energy_ok_status_yields_scalars(self):
        self.assertEqual(measurements("energy", energy()), {
            "energy/batteryChargeLevel": "61",
            "energy/electricRange": "240",
            "energy/chargingStatus": "IDLE",
            "energy/chargingPower": "",
        })

    def test_successful_poll_has_json_scalars_and_availability(self):
        self.assertEqual(self.bridge.poll(), 300)
        messages = self.messages()
        self.assertEqual(json.loads(messages["energy"]), energy())
        self.assertEqual(json.loads(messages["odometer"]), odometer())
        self.assertEqual(messages["energy/batteryChargeLevel"], "61")
        self.assertEqual(messages["odometer/value"], "12345")
        self.assertEqual(messages["availability"], "online")
        self.assertEqual(messages["error"], "{}")
        self.assertIn("last_success", messages)

    def test_zero_is_a_real_measurement(self):
        result = measurements("energy", {"chargingPower": {"status": "OK", "value": 0}})
        self.assertEqual(result["energy/chargingPower"], "0")

    def test_unavailable_values_are_cleared_not_published_as_zero(self):
        result = measurements("energy", {
            "batteryChargeLevel": {"status": "ERROR", "value": 0},
            "chargingPower": {"status": "OK", "value": None},
        })
        self.assertEqual(set(result.values()), {""})

    def test_partial_failure_does_not_hide_other_endpoint(self):
        self.api.energy.side_effect = ApiError(403)
        self.bridge.poll()
        messages = self.messages()
        self.assertEqual(messages["energy/availability"], "offline")
        self.assertEqual(messages["odometer/availability"], "online")
        self.assertEqual(messages["availability"], "offline")
        self.assertIn("energy", json.loads(messages["error"]))
        self.assertNotIn("last_success", messages)

    def test_all_measurements_invalid_mark_area_offline(self):
        self.api.energy.return_value = {"batteryChargeLevel": {"status": "ERROR"}}
        self.bridge.poll()
        self.assertEqual(self.messages()["energy/availability"], "offline")
        self.assertEqual(self.messages()["availability"], "offline")

    def test_rate_limit_skips_remaining_endpoint_and_honors_retry_after(self):
        self.api.energy.side_effect = ApiError(429, 1200)
        self.assertEqual(self.bridge.poll(), 1200)
        self.api.odometer.assert_not_called()
        self.assertEqual(self.messages()["odometer/availability"], "offline")

    def test_disappearing_measurement_removes_retained_scalar(self):
        self.bridge.poll()
        self.publisher.send.reset_mock()
        self.api.energy.return_value = {"electricRange": {"status": "OK", "value": 100}}
        self.bridge.poll()
        self.assertEqual(self.messages()["energy/batteryChargeLevel"], "")

    def test_invalid_topic_and_nonfinite_values_are_rejected(self):
        for data in (
            {"bad/topic": {"value": 1}},
            {"chargingPower": {"value": float("inf")}},
            {"chargingPower": {"value": []}},
            {},
        ):
            with self.subTest(data=data):
                with self.assertRaises(BridgeError):
                    measurements("energy", data)

    def test_cooldown_is_not_shortened_by_request_duration(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        self.api.vin.return_value = VIN
        self.publisher.connected = Mock()
        self.publisher.connected.wait.return_value = True
        with (
            patch("volvo_mqtt.bridge.Publisher", return_value=self.publisher),
            patch("volvo_mqtt.bridge.Bridge") as bridge_class,
        ):
            bridge_class.return_value.poll.return_value = 300
            run(config(), self.api, stop)
        self.assertGreaterEqual(stop.wait.call_args.args[0], 300)


if __name__ == "__main__":
    unittest.main()
