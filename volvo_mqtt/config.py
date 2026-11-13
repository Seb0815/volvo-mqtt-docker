import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} muss eine ganze Zahl sein") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} muss zwischen {minimum} und {maximum} liegen")
    return value


def boolean(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default).lower()).lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} muss true oder false sein")
    return value == "true"


def validate_vin(value: str) -> str:
    vin = value.upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise ValueError("VOLVO_VIN muss eine gueltige 17-stellige VIN sein")
    return vin


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    api_key: str
    vin: str
    redirect_uri: str
    token_file: Path
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_ca_cert: str
    mqtt_client_id: str
    topic_prefix: str
    poll_interval: int
    auth_bind_host: str
    auth_port: int
    dashboard_enabled: bool
    dashboard_bind_host: str
    dashboard_port: int

    @classmethod
    def from_env(cls, command: str) -> "Config":
        required = ["VOLVO_CLIENT_ID", "VOLVO_CLIENT_SECRET"]
        if command == "run":
            required.extend(["VOLVO_API_KEY", "MQTT_HOST"])
        for name in required:
            if not os.environ.get(name, "").strip():
                raise ValueError(f"{name} fehlt")

        redirect_uri = os.environ.get(
            "VOLVO_REDIRECT_URI", "http://localhost:8080/callback"
        )
        redirect = urlsplit(redirect_uri)
        if (
            redirect.path != "/callback"
            or redirect.query
            or redirect.fragment
            or redirect.username
            or not redirect.hostname
            or redirect.scheme not in ("http", "https")
            or (
                redirect.scheme == "http"
                and redirect.hostname not in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise ValueError(
                "VOLVO_REDIRECT_URI muss HTTPS oder HTTP auf localhost verwenden "
                "und den Pfad /callback haben"
            )
        prefix = os.environ.get("MQTT_TOPIC_PREFIX", "volvo").strip("/")
        if not prefix or any(c in prefix for c in ("#", "+", "\0")):
            raise ValueError("MQTT_TOPIC_PREFIX ist kein gueltiger MQTT-Topic-Prefix")
        client_id = os.environ.get("MQTT_CLIENT_ID", "volvo-ex30")
        if not client_id or "\0" in client_id:
            raise ValueError("MQTT_CLIENT_ID darf nicht leer sein oder NUL enthalten")
        tls = boolean("MQTT_TLS")
        ca_cert = os.environ.get("MQTT_CA_CERT", "")
        if ca_cert and not tls:
            raise ValueError("MQTT_CA_CERT benoetigt MQTT_TLS=true")
        username = os.environ.get("MQTT_USERNAME", "")
        password = os.environ.get("MQTT_PASSWORD", "")
        if password and not username:
            raise ValueError("MQTT_PASSWORD benoetigt MQTT_USERNAME")
        vin = os.environ.get("VOLVO_VIN", "").strip()
        return cls(
            client_id=os.environ["VOLVO_CLIENT_ID"],
            client_secret=os.environ["VOLVO_CLIENT_SECRET"],
            api_key=os.environ.get("VOLVO_API_KEY", ""),
            vin=validate_vin(vin) if vin else "",
            redirect_uri=redirect_uri,
            token_file=Path(os.environ.get("TOKEN_FILE", "/data/tokens.json")),
            mqtt_host=os.environ.get("MQTT_HOST", ""),
            mqtt_port=integer("MQTT_PORT", 8883 if tls else 1883, 1, 65535),
            mqtt_username=username,
            mqtt_password=password,
            mqtt_tls=tls,
            mqtt_ca_cert=ca_cert,
            mqtt_client_id=client_id,
            topic_prefix=prefix,
            poll_interval=integer("POLL_INTERVAL_SECONDS", 300, 60, 86400),
            auth_bind_host=os.environ.get("AUTH_BIND_HOST", "127.0.0.1"),
            auth_port=integer("AUTH_PORT", 8080, 1, 65535),
            dashboard_enabled=boolean("DASHBOARD_ENABLED", default=True),
            dashboard_bind_host=os.environ.get("DASHBOARD_BIND_HOST", "127.0.0.1"),
            dashboard_port=integer("DASHBOARD_PORT", 8080, 1, 65535),
        )
