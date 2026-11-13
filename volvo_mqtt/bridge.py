import json
import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .api import ApiError, BridgeError, Volvo
from .config import Config
from .dashboard import Status

LOG = logging.getLogger(__name__)


class Publisher:
    def __init__(self, config: Config, vin: str, status: Status | None = None) -> None:
        self.config = config
        self.status = status if status is not None else Status(config)
        self.base = f"{config.topic_prefix}/{vin}"
        self.connected = threading.Event()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.mqtt_client_id,
            protocol=mqtt.MQTTv311,
        )
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_disconnect = self.on_disconnect
        self.client.will_set(f"{self.base}/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.max_queued_messages_set(100)
        if config.mqtt_username:
            self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set(ca_certs=config.mqtt_ca_cert or None)

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT-Verbindung abgelehnt: %s", reason_code)
            self.status.mqtt(False, "Broker hat die Verbindung abgelehnt")
            return
        # Do not announce fresh data before a complete successful poll.
        client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
        self.connected.set()
        self.status.mqtt(True)
        LOG.info("MQTT verbunden")

    def on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected.clear()
        self.status.mqtt(
            False, "Verbindung unterbrochen; neuer Versuch folgt" if reason_code.is_failure else ""
        )
        if reason_code.is_failure:
            LOG.warning("MQTT-Verbindung unterbrochen; automatischer Wiederaufbau")

    def on_connect_fail(self, client, userdata) -> None:
        self.status.mqtt(False, "Broker nicht erreichbar; Adresse, Port und TLS pruefen")
        LOG.error("MQTT nicht erreichbar; Broker-Adresse, Port und TLS pruefen. Neuer Versuch folgt.")

    def start(self) -> None:
        self.client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=60)
        self.client.loop_start()

    def send(self, topic: str, payload: str) -> None:
        if not self.connected.is_set():
            raise BridgeError("MQTT ist nicht verbunden")
        result = self.client.publish(f"{self.base}/{topic}", payload, qos=1, retain=True)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise BridgeError("MQTT-Publish konnte nicht gestartet werden")
        try:
            result.wait_for_publish(timeout=10)
        except (RuntimeError, ValueError):
            raise BridgeError("MQTT-Publish fehlgeschlagen") from None
        if not result.is_published():
            raise BridgeError("MQTT-Publish wurde nicht rechtzeitig bestaetigt")
        self.status.published()

    def close(self) -> None:
        try:
            if self.connected.is_set():
                self.send("availability", "offline")
        finally:
            self.client.disconnect()
            self.client.loop_stop()


def scalar(value: object) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise BridgeError("Volvo-Messwert ist keine endliche Zahl")
    if not isinstance(value, (str, int, float, bool)):
        raise BridgeError("Volvo-Messwert hat einen unerwarteten Datentyp")
    return json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value


def measurements(area: str, data: dict[str, Any]) -> dict[str, str]:
    output = {}
    for name, measurement in data.items():
        if not isinstance(measurement, dict):
            continue
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", name):
            raise BridgeError("Volvo-Messwertname ist kein gueltiges Topic-Segment")
        topic = f"{area}/{name}" if area == "energy" else "odometer/value"
        value = measurement.get("value")
        status = measurement.get("status")
        output[topic] = (
            scalar(value) if value is not None and status in (None, "OK") else ""
        )
    if not output:
        raise BridgeError(f"{area}: Keine Messwerte enthalten")
    return output


class Bridge:
    def __init__(
        self, config: Config, api: Volvo, publisher: Publisher, vin: str,
        status: Status | None = None,
    ) -> None:
        self.config = config
        self.api = api
        self.publisher = publisher
        self.vin = vin
        self.status = status if status is not None else Status(config)
        self.previous: dict[str, set[str]] = {"energy": set(), "odometer": set()}

    def poll(self) -> float:
        self.status.begin_poll()
        errors: dict[str, str] = {}
        delay = float(self.config.poll_interval)
        for area, fetch in (("energy", self.api.energy), ("odometer", self.api.odometer)):
            try:
                data = fetch(self.vin)
                values = measurements(area, data)
                raw = json.dumps(data, allow_nan=False, separators=(",", ":"))
                available = any(value != "" for value in values.values())
                self.status.received(area, data, available)
                self.publisher.send(area, raw)
                for topic in self.previous[area] - values.keys():
                    self.publisher.send(topic, "")
                for topic, value in values.items():
                    self.publisher.send(topic, value)
                self.previous[area] = set(values)
                self.publisher.send(f"{area}/availability", "online" if available else "offline")
                if not available:
                    raise BridgeError(f"{area}: Alle Messwerte sind ungueltig/nicht verfuegbar")
            except (BridgeError, ValueError) as exc:
                errors[area] = str(exc)
                self.status.area_error(area, str(exc))
                LOG.error("%s: %s", area, exc)
                self.publisher.send(f"{area}/availability", "offline")
                if isinstance(exc, ApiError) and exc.status == 429:
                    delay = max(delay, exc.retry_after, 60)
                    # A rate limit applies to the app, not just one endpoint.
                    for remaining in ("energy", "odometer"):
                        self.status.area_error(remaining, "Volvo-Abfragelimit erreicht")
                        self.publisher.send(f"{remaining}/availability", "offline")
                    break
        self.publisher.send("error", json.dumps(errors, separators=(",", ":")))
        self.publisher.send("availability", "offline" if errors else "online")
        if not errors:
            self.publisher.send(
                "last_success", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            LOG.info("Fahrzeugdaten an MQTT uebertragen")
        self.status.finish_poll(errors)
        return delay


def run(
    config: Config, api: Volvo, stop: threading.Event, status: Status | None = None
) -> None:
    status = status if status is not None else Status(config)
    vin = api.vin()
    status.vehicle(vin)
    publisher = Publisher(config, vin, status=status)
    bridge = Bridge(config, api, publisher, vin, status=status)
    publisher.start()
    try:
        while not stop.is_set():
            if not publisher.connected.wait(timeout=1):
                continue
            try:
                delay = bridge.poll()
            except BridgeError as exc:
                LOG.error("%s", exc)
                status.finish_poll({"bridge": str(exc)})
                delay = config.poll_interval
            status.schedule(delay)
            stop.wait(delay)
    finally:
        publisher.close()
