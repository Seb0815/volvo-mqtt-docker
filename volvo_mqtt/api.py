import base64
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config, validate_vin

API_URL = "https://api.volvocars.com"
AUTH_URL = "https://volvoid.eu.volvocars.com/as/authorization.oauth2"
TOKEN_URL = "https://volvoid.eu.volvocars.com/as/token.oauth2"
SCOPES = "openid conve:vehicle_relation conve:odometer_status energy:state:read"


class BridgeError(Exception):
    pass


class ApiError(BridgeError):
    def __init__(self, status: int, retry_after: float = 0) -> None:
        self.status = status
        self.retry_after = retry_after
        hints = {
            400: "Anfrage oder OAuth-Grant ungueltig; ggf. erneut mit 'auth' anmelden",
            401: "Autorisierung abgelaufen; erneut mit 'auth' anmelden",
            403: "API-Key, App-Freigabe, Scopes und Fahrzeugberechtigung pruefen",
            404: "Endpunkt oder Fahrzeugdaten fuer diese VIN nicht verfuegbar",
            429: "Volvo-Abfragelimit erreicht; Abfrageintervall erhoehen",
        }
        super().__init__(f"Volvo HTTP {status}: {hints.get(status, 'API-Anfrage fehlgeschlagen')}")


def retry_seconds(value: str | None) -> float:
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            )
        except (ValueError, TypeError, OverflowError):
            return 0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self) -> None:
        self.opener = build_opener(NoRedirect())

    def json(self, request: Request) -> dict[str, Any]:
        try:
            with self.opener.open(request, timeout=20) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise BridgeError("Volvo-Antwort ist unerwartet gross")
        except HTTPError as exc:
            retry_after = retry_seconds(exc.headers.get("Retry-After"))
            exc.close()
            # Response bodies can contain tokens or personal vehicle information.
            raise ApiError(exc.code, retry_after) from None
        except (URLError, TimeoutError, OSError):
            raise BridgeError("Volvo-Verbindung fehlgeschlagen (Netzwerk/TLS/Timeout)") from None
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise BridgeError("Volvo hat kein gueltiges JSON geliefert") from None
        if not isinstance(result, dict):
            raise BridgeError("Volvo hat kein JSON-Objekt geliefert")
        return result


class Tokens:
    def __init__(self, config: Config, http: Http) -> None:
        self.config = config
        self.http = http
        self.token: dict[str, Any] = {}

    def load(self) -> None:
        try:
            token = json.loads(self.config.token_file.read_text())
        except FileNotFoundError:
            raise BridgeError("Token-Datei fehlt. Zuerst mit 'auth' bei Volvo anmelden.") from None
        except (OSError, ValueError, UnicodeError):
            raise BridgeError("Token-Datei kann nicht gelesen werden; erneut anmelden.") from None
        if not isinstance(token, dict) or not isinstance(token.get("refresh_token"), str):
            raise BridgeError("Token-Datei enthaelt keinen gueltigen Refresh-Token")
        self.token = token

    def grant(self, fields: dict[str, str]) -> None:
        credentials = base64.b64encode(
            f"{quote_plus(self.config.client_id)}:{quote_plus(self.config.client_secret)}".encode()
        ).decode()
        response = self.http.json(
            Request(
                TOKEN_URL,
                data=urlencode(fields).encode(),
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                method="POST",
            )
        )
        access = response.get("access_token")
        refresh = response.get("refresh_token", self.token.get("refresh_token"))
        expires = response.get("expires_in")
        try:
            lifetime = float(expires)
        except (ValueError, TypeError):
            raise BridgeError("Token-Antwort enthaelt keine gueltige Laufzeit") from None
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not math.isfinite(lifetime)
            or lifetime <= 0
        ):
            raise BridgeError("Token-Antwort enthaelt keine gueltigen Tokens")
        token = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": time.time() + lifetime,
        }
        self.save(token)
        self.token = token

    def save(self, token: dict[str, Any]) -> None:
        path: Path = self.config.token_file
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tokens-")
            with os.fdopen(fd, "w") as stream:
                json.dump(token, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise BridgeError("Tokens konnten nicht dauerhaft gespeichert werden") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def access(self, force: bool = False) -> str:
        expires = self.token.get("expires_at", 0)
        if not isinstance(expires, (int, float)) or not math.isfinite(expires):
            raise BridgeError("Token-Datei enthaelt keine gueltige Ablaufzeit")
        if force or expires <= time.time() + 60:
            refresh = self.token.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise BridgeError("Refresh-Token fehlt; erneut mit 'auth' anmelden")
            self.grant({"grant_type": "refresh_token", "refresh_token": refresh})
        access = self.token.get("access_token")
        if not isinstance(access, str) or not access:
            raise BridgeError("Access-Token fehlt; erneut mit 'auth' anmelden")
        return access


class Volvo:
    def __init__(self, config: Config, tokens: Tokens, http: Http) -> None:
        self.config = config
        self.tokens = tokens
        self.http = http

    def get(self, path: str) -> dict[str, Any]:
        for attempt in range(2):
            token = self.tokens.access(force=attempt == 1)
            try:
                return self.http.json(
                    Request(
                        API_URL + path,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "vcc-api-key": self.config.api_key,
                            "Accept": "application/json",
                        },
                    )
                )
            except ApiError as exc:
                if exc.status != 401 or attempt:
                    raise
        raise BridgeError("Volvo-Autorisierung fehlgeschlagen")

    def vin(self) -> str:
        if self.config.vin:
            return self.config.vin
        response = self.get("/connected-vehicle/v2/vehicles")
        vehicles = response.get("data")
        if not isinstance(vehicles, list) or len(vehicles) != 1:
            raise BridgeError(
                "Keine eindeutige VIN: Konto muss genau ein Fahrzeug enthalten; "
                "sonst VOLVO_VIN explizit setzen"
            )
        vehicle = vehicles[0]
        if not isinstance(vehicle, dict) or not isinstance(vehicle.get("vin"), str):
            raise BridgeError("Volvo-Fahrzeugliste enthaelt keine VIN")
        try:
            return validate_vin(vehicle["vin"])
        except ValueError:
            raise BridgeError("Volvo-Fahrzeugliste enthaelt eine ungueltige VIN") from None

    def energy(self, vin: str) -> dict[str, Any]:
        # Energy v2 is unwrapped; Connected Vehicle v2 uses a "data" envelope.
        data = self.get(f"/energy/v2/vehicles/{vin}/state")
        if not any(isinstance(value, dict) and "status" in value for value in data.values()):
            raise BridgeError("Energy API hat keine Messwerte geliefert")
        return data

    def odometer(self, vin: str) -> dict[str, Any]:
        data = self.get(f"/connected-vehicle/v2/vehicles/{vin}/odometer").get("data")
        if not isinstance(data, dict) or not isinstance(data.get("odometer"), dict):
            raise BridgeError("Connected Vehicle API hat keinen Kilometerstand geliefert")
        return data
