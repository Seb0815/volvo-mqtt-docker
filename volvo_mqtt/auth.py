import base64
import hashlib
import hmac
import logging
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .api import AUTH_URL, SCOPES, BridgeError, Tokens
from .config import Config

LOG = logging.getLogger(__name__)


def authorize(config: Config, tokens: Tokens) -> None:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    complete = False
    failure: BridgeError | None = None
    secure = urlsplit(config.redirect_uri).scheme == "https"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # The default access log would expose the authorization code.
            return

        def reply(self, status: int, text: str, cookie: str = "") -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            nonlocal complete, failure
            url = urlsplit(self.path)
            if url.path == "/auth":
                params = {
                    "response_type": "code",
                    "client_id": config.client_id,
                    "redirect_uri": config.redirect_uri,
                    "scope": SCOPES,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
                self.send_response(302)
                self.send_header("Location", AUTH_URL + "?" + urlencode(params))
                self.send_header("Cache-Control", "no-store")
                cookie = f"volvo_state={state}; HttpOnly; SameSite=Lax; Path=/; Max-Age=600"
                self.send_header("Set-Cookie", cookie + ("; Secure" if secure else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if url.path != "/callback":
                self.reply(404, "Nicht gefunden. Anmeldung unter /auth starten.")
                return
            query = parse_qs(url.query)
            cookie = self.headers.get("Cookie", "")
            cookies = dict(
                part.strip().split("=", 1) for part in cookie.split(";") if "=" in part
            )
            supplied = query.get("state", [""])[0]
            if not (
                hmac.compare_digest(supplied.encode(), state.encode())
                and hmac.compare_digest(cookies.get("volvo_state", "").encode(), state.encode())
            ):
                self.reply(400, "Ungueltiger OAuth-State. Anmeldung unter /auth neu starten.")
                return
            if "error" in query:
                failure = BridgeError("Volvo-Anmeldung wurde abgelehnt oder abgebrochen")
                complete = True
                self.reply(400, str(failure))
                return
            code = query.get("code", [""])[0]
            if not code:
                self.reply(400, "Authorization-Code fehlt.")
                return
            try:
                tokens.grant(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": config.redirect_uri,
                        "code_verifier": verifier,
                    }
                )
            except BridgeError as exc:
                failure = exc
                self.reply(502, "Token-Austausch fehlgeschlagen. Details im App-Log.")
            else:
                self.reply(
                    200,
                    "Volvo-Anmeldung erfolgreich. Dieses Fenster kann geschlossen werden.",
                    "volvo_state=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
                )
            complete = True

    start_url = config.redirect_uri.removesuffix("/callback") + "/auth"
    with HTTPServer((config.auth_bind_host, config.auth_port), Handler) as server:
        server.timeout = 1
        LOG.info("Im Browser oeffnen: %s", start_url)
        deadline = time.monotonic() + 600
        while not complete and time.monotonic() < deadline:
            server.handle_request()
    if failure:
        raise failure
    if not complete:
        raise BridgeError("Volvo-Anmeldung nach 10 Minuten abgelaufen")
    LOG.info("Tokens sicher gespeichert. Jetzt die Bridge mit 'run' starten.")
