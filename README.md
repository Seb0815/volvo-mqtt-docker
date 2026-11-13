# Volvo EX30 zu MQTT

Schlanke, rein lesende Python-App fuer Docker. Sie fragt den Volvo-Cloud-Backend
ab und veroeffentlicht Akku-, Reichweiten-, Lade- und Kilometerstandsdaten an
einem vorhandenen MQTT-Broker. Keine Fahrzeugsteuerung, kein evcc, kein
Home-Assistant-Zwang und kein Speichern deines Volvo-Passworts.

## Voraussetzungen und Grenzen

- Volvo ID mit Zugriff auf dein Fahrzeug und eine freigegebene Anwendung im
  [Volvo Developer Portal](https://developer.volvocars.com/account/).
- VCC API Key, OAuth Client ID und Client Secret dieser Anwendung.
- Zugriff auf **Connected Vehicle API v2** und **Energy API v2**. Die Connected
  Vehicle API allein liefert die hier gewuenschten Energiedaten nicht.
- Docker und ein erreichbarer MQTT-Broker, z.B. Mosquitto oder der MQTT-Server
  deines Smarthomes. **Docker Compose ist optional.** Der Hauptablauf unten
  verwendet ausschliesslich `docker build` und `docker run`.

Die App verwendet dieselben Lese-Endpunkte wie die aktuelle evcc-Anbindung.
Das ist keine Garantie fuer jedes EX30-Modell, Modelljahr oder Land:
Fahrzeugzuordnung, Datenfelder und Freigaben werden von Volvo bestimmt.
Einzelne Felder koennen `ERROR` / `NOT_SUPPORTED` liefern. Solche Felder werden
nicht als vermeintliche Nullwerte veroeffentlicht. Bei HTTP 403/404 steht der
Fehler im Log und im MQTT-Fehlertopic.

Die Daten stammen aus der Cloud, nicht direkt aus dem Auto. Ein erfolgreicher
Abruf garantiert keine aktuelle Messung: `updatedAt` (Energy) bzw. `timestamp`
(Kilometerstand) in den JSON-Nachrichten beachten. Ein kuerzeres Intervall
erzwingt kein Aufwecken des Fahrzeugs.

## 1. Volvo-Anwendung einrichten

Im Developer Portal eine Anwendung erstellen, die benoetigten APIs auswaehlen
und den dort vorgesehenen Veroeffentlichungs-/Freigabeprozess durchlaufen.
Ein API Key allein ersetzt die OAuth-Freigabe nicht.

Diese Scopes freigeben:

```text
openid
conve:vehicle_relation
conve:odometer_status
energy:state:read
```

Die technischen OAuth-Namen enthalten **Unterstriche**, auch wenn das Portal
teilweise andere Anzeigenamen verwendet.

Als Redirect URI registrieren:

```text
http://localhost:8080/callback
```

Die URI muss exakt mit `VOLVO_REDIRECT_URI` uebereinstimmen. Falls deine
Volvo-App keine HTTP-Loopback-URI akzeptiert, eine registrierte HTTPS-URI mit
Pfad `/callback` verwenden und einen vertrauenswuerdigen Reverse Proxy auf
Port 8080 vorschalten. Der gleiche Proxy muss auch `/auth` weiterleiten.
Der temporaere Anmeldeserver spricht selbst nur HTTP; TLS endet am Proxy.
Zugriff darauf auf dich beschraenken, nicht offen ins Internet stellen.

## 2. Konfiguration

Alle folgenden Befehle im Projektverzeichnis ausfuehren, in dem auch
`Dockerfile` und `.env.example` liegen. Zuerst die Konfigurationsdatei anlegen:

```sh
cp .env.example .env
chmod 600 .env
```

Die Datei `.env` mit einem Texteditor oeffnen und mindestens diese Werte
eintragen. Die Platzhalter durch die Werte aus deiner Volvo-Anwendung und
die Adresse deines MQTT-Brokers ersetzen:

```dotenv
VOLVO_CLIENT_ID=deine-client-id
VOLVO_CLIENT_SECRET=dein-client-secret
VOLVO_API_KEY=dein-vcc-api-key
MQTT_HOST=192.168.1.10
```

Falls dein Broker eine Anmeldung verlangt, zusaetzlich setzen:

```dotenv
MQTT_USERNAME=dein-mqtt-benutzer
MQTT_PASSWORD=dein-mqtt-passwort
```

**Format fuer `docker run --env-file .env`:**

- Eine Variable pro Zeile im Format `NAME=WERT`, ohne Leerzeichen um `=`.
- Keine Anfuehrungszeichen um Werte setzen: Docker wuerde diese als Teil des
  Werts uebernehmen. Beispiel: `MQTT_PASSWORD=mein$passwort#mitzeichen`.
- `$` und `#` innerhalb eines Werts werden von Docker woertlich uebernommen.
  Kommentare nur auf eigene Zeilen schreiben, die mit `#` beginnen.
- Die Datei nicht mit `source .env` ausfuehren; Docker liest sie direkt.
  Ein `export` im Terminal ist bei `--env-file` nicht noetig.
- Die Datei enthaelt Zugangsdaten. Nicht committen oder weitergeben;
  Dateirechte `600` beibehalten.

Compose verwendet einen anderen Env-Dateiparser. **Nur bei Compose** Werte
mit `$`, `#` oder Leerzeichen bei Bedarf in einfache Anfuehrungszeichen
setzen, z.B. `MQTT_PASSWORD='mein$passwort#mitzeichen'`. Eine solche Datei
nicht unveraendert mit `docker run --env-file` verwenden.

`VOLVO_VIN` ist optional, wenn genau ein Fahrzeug im Konto verfuegbar ist.
Bei mehreren Fahrzeugen die gewuenschte 17-stellige VIN explizit eintragen.
`MQTT_CLIENT_ID` muss pro laufender Instanz eindeutig sein.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `MQTT_PORT` | `1883` | Broker-Port; ohne Angabe bei TLS: `8883` |
| `MQTT_TLS` | `false` | `true` aktiviert TLS mit Zertifikatspruefung |
| `MQTT_CA_CERT` | leer | Optionaler Pfad zu einer eigenen CA im Container |
| `MQTT_TOPIC_PREFIX` | `volvo` | Basis der Topics |
| `MQTT_CLIENT_ID` | `volvo-ex30` | MQTT-Client-ID |
| `POLL_INTERVAL_SECONDS` | `300` | Pause nach einem Abfragezyklus, mindestens `60` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` oder `ERROR` |
| `DASHBOARD_ENABLED` | `true` | Read-only Dashboard im Bridge-Betrieb aktivieren |

Fuer eine eigene CA das Zertifikat zusaetzlich read-only in den Container
mounten und `MQTT_CA_CERT=/certs/ca.crt` setzen. Bei `docker run` zusaetzlich:

```sh
--mount "type=bind,source=$(pwd)/certs,target=/certs,readonly"
```

Das lokale Verzeichnis `certs` muss existieren. Bei Compose stattdessen
`./certs:/certs:ro` zur Volume-Liste hinzufuegen.
TLS ist fuer nicht vertrauenswuerdige Netze empfohlen; Port 1883 ist unverschluesselt.
`localhost` als Brokeradresse bezeichnet im Container den Container selbst.
Bei Docker Desktop kann ein Broker auf dem Host unter `host.docker.internal`
erreichbar sein.

## 3. Betrieb ohne Compose: Docker run

### Image bauen und Token-Volume anlegen

```sh
docker build -t volvo-mqtt:local .
docker volume create volvo-data
```

Das benannte Volume speichert die Volvo-Tokens unabhaengig vom Container.
Der Container verwendet den im Image eingerichteten Benutzer `app`
(UID `10001`); kein `--user` oder manuelles `chmod 777` hinzufuegen.

### Einmalig bei Volvo anmelden

```sh
docker run --rm --init \
  --name volvo-mqtt-auth \
  --env-file .env \
  --env TOKEN_FILE=/data/tokens.json \
  --env AUTH_BIND_HOST=0.0.0.0 \
  --mount type=volume,source=volvo-data,target=/data \
  --publish 127.0.0.1:8080:8080 \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  volvo-mqtt:local auth
```

Den Befehl im Vordergrund laufen lassen und im Browser oeffnen:

```text
http://localhost:8080/auth
```

Bei Volvo anmelden, ggf. MFA bestaetigen und die Leseberechtigungen erteilen.
Nach erfolgreichem Login speichert die App die Tokens im Volume, beendet sich
und der temporaere Container wird automatisch entfernt. Die Anmeldung nutzt
PKCE und einen an den Browser gebundenen State; sie hat zehn Minuten Zeit.

Laeuft Docker auf einem NAS oder anderen Rechner, vorher auf deinem lokalen
Rechner einen SSH-Tunnel starten und den Browser lokal verwenden:

```sh
ssh -L 8080:127.0.0.1:8080 benutzer@docker-host
```

### Bridge und Dashboard starten

Erst nach erfolgreicher Anmeldung:

```sh
docker run -d --init \
  --name volvo-mqtt \
  --restart unless-stopped \
  --env-file .env \
  --env TOKEN_FILE=/data/tokens.json \
  --env DASHBOARD_BIND_HOST=0.0.0.0 \
  --mount type=volume,source=volvo-data,target=/data \
  --publish 127.0.0.1:8080:8080 \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 60 \
  --log-driver json-file \
  --log-opt max-size=5m \
  --log-opt max-file=3 \
  volvo-mqtt:local run
```

Status und Logs anzeigen:

```sh
docker ps --filter name=volvo-mqtt
docker logs -f --tail=100 volvo-mqtt
```

Dashboard: **http://localhost:8080/**. Bei einem entfernten Docker-Host den
gleichen SSH-Tunnel wie fuer die Anmeldung verwenden. Die interne Bindung
`DASHBOARD_BIND_HOST=0.0.0.0` macht den Server fuer das Docker-Portmapping
erreichbar; `--publish 127.0.0.1:8080:8080` beschraenkt den Zugriff weiterhin
auf den Docker-Host. Nicht ungeschuetzt auf `0.0.0.0` des Hosts veroeffentlichen.

Stoppen und spaeter mit unveraenderter Konfiguration wieder starten:

```sh
docker stop volvo-mqtt
docker start volvo-mqtt
```

### Einzelne Umgebungsvariablen ueberschreiben

Zusaetzliche `--env NAME=WERT`-Optionen in einem `docker run`-Befehl
ueberschreiben Werte aus `--env-file`. Beispielsweise vor dem Imagenamen
`--env POLL_INTERVAL_SECONDS=600` einfuegen, um alle zehn Minuten abzufragen.

Alternativ einen Wert aus dem Terminal uebernehmen:

```sh
export POLL_INTERVAL_SECONDS=600
```

Dann im `docker run`-Befehl vor dem Imagenamen
`--env POLL_INTERVAL_SECONDS` einfuegen. Ohne `--env` reicht `export` allein
nicht aus, da Docker Host-Variablen nicht automatisch weitergibt.
Zugangsdaten vorzugsweise ueber die geschuetzte `.env`-Datei setzen statt
als Klartext in Shell-Befehlen und deren History.

### Konfiguration aendern oder Image aktualisieren

Aenderungen an `.env` werden **nicht** durch `docker restart` oder
`docker start` eingelesen. Den Container neu erstellen:

1. `.env` bearbeiten; bei Code-Updates zuerst erneut
   `docker build -t volvo-mqtt:local .` ausfuehren.
2. Den bisherigen Bridge-Container stoppen und entfernen:

   ```sh
   docker stop volvo-mqtt
   docker rm volvo-mqtt
   ```

3. Den vollstaendigen `docker run -d ...`-Befehl aus dem Abschnitt
   **Bridge und Dashboard starten** erneut ausfuehren.

Das Volume `volvo-data` dabei beibehalten; die Tokens gehen beim Entfernen
des Containers nicht verloren. Das Volume nicht loeschen. Die Anmeldung
muss normalerweise nicht wiederholt werden.

### Erneute Volvo-Anmeldung

Volvo kann trotz automatischem Token-Refresh eine neue interaktive Anmeldung
verlangen (laut evcc-Dokumentation ggf. nach sieben Tagen):

1. `docker stop volvo-mqtt` ausfuehren.
2. Den vollstaendigen `docker run --rm ... auth`-Befehl von oben wiederholen
   und im Browser `http://localhost:8080/auth` oeffnen.
3. Nach erfolgreichem Login `docker start volvo-mqtt` ausfuehren.

Bridge und Anmeldung muessen **dasselbe Volume `volvo-data`** verwenden.
Nicht parallel betreiben: Beide nutzen Port 8080 und duerfen nicht
gleichzeitig dieselben Refresh-Tokens verwenden.

## Alternative: Betrieb mit Docker Compose

Die folgenden Befehle sind nur fuer Compose-Nutzer. Bei Betrieb mit
`docker run` ist dieser Abschnitt nicht erforderlich.
Compose benennt das Token-Volume standardmaessig mit Projektpraefix
(z.B. `volvo-mqtt-docker_volvo-data`); es ist **nicht automatisch dasselbe**
Volume wie `volvo-data` aus den `docker run`-Befehlen. Die Betriebsarten
nicht ohne ausdrueckliche Anpassung der Volume-Zuordnung mischen.

### Einmalig bei Volvo anmelden

```sh
docker compose build
docker compose run --rm --service-ports volvo-mqtt auth
```

Dann im Browser auf demselben Rechner oeffnen:

```text
http://localhost:8080/auth
```

Bei Volvo anmelden, ggf. MFA bestaetigen und die Leseberechtigungen erteilen.
Die App nutzt Authorization Code Flow mit PKCE und einem an den Browser
gebundenen State. Nach erfolgreichem Login beendet sich der Anmeldecontainer.
Es gibt zehn Minuten Zeit fuer die Anmeldung.

Laeuft Docker auf einem anderen Rechner/NAS, vorher lokal einen SSH-Tunnel
aufbauen und den Browser weiterhin lokal benutzen:

```sh
ssh -L 8080:127.0.0.1:8080 benutzer@docker-host
```

Compose bindet Port 8080 absichtlich nur an `127.0.0.1` des Docker-Hosts.
Nicht parallel mehrere Anmeldecontainer starten.

### Bridge starten

```sh
docker compose up -d
docker compose logs -f --tail=100
```

Tokens liegen mit Dateirechten `0600` im persistenten Docker-Volume
`volvo-data`. Access-Tokens werden rechtzeitig erneuert und rotierte
Refresh-Tokens atomar gespeichert. Das Volume bei Updates behalten;
`docker compose down -v` wuerde die Anmeldung loeschen.

Volvo kann trotz Refresh eine erneute interaktive Anmeldung verlangen
(laut evcc-Dokumentation ggf. nach sieben Tagen). Dann:

```sh
docker compose stop volvo-mqtt
docker compose run --rm --service-ports volvo-mqtt auth
docker compose up -d
```

Vor einer erneuten Anmeldung **immer stoppen**, damit keine zwei Prozesse
dieselben Refresh-Tokens gleichzeitig verwenden.

Die App laeuft ohne root mit schreibgeschuetztem Root-Dateisystem.
Im normalen Bridge-Betrieb laeuft das unten beschriebene Read-only Dashboard.
Die MQTT-Nachrichten verwenden QoS 1 und Retain. Der Broker sollte die Topics
per ACL auf diese App und berechtigte Smarthome-Clients beschraenken.
VIN und Fahrdaten sind personenbezogen; `.env` und Token-Volume nicht teilen.

## Dashboard

Nach dem Start der Bridge im Browser oeffnen:

```text
http://localhost:8080/
```

Das responsive Dashboard zeigt:

- Akkustand, elektrische Restreichweite, Ladestatus und Kilometerstand.
- MQTT-Verbindung, Verfuegbarkeit der beiden APIs und letzte MQTT-Uebertragung.
- Letzten Abrufversuch, letzten kompletten Erfolg und naechsten geplanten Abruf.
- API-/MQTT-Fehler sowie alle gelieferten Messwerte mit Einheit und Messzeit.

Die Webseite aktualisiert sich alle fuenf Sekunden. Dabei wird nur der
lokale Status unter `/api/status` gelesen, **keine zusaetzliche Volvo-Abfrage**
ausgeloest. Alte Werte bleiben bei Fehlern sichtbar und werden entsprechend
markiert. Messungen, die aelter als das Doppelte des API-Intervalls
(mindestens zehn Minuten) sind, bekommen einen Altershinweis.
Bei einem Neustart ist der Dashboard-Speicher leer, bis die Daten erneut
abgerufen wurden. Es gibt keine Fahrzeugbefehle oder Konfigurationsaenderungen.
Tokens, Passwoerter und API-Keys werden nicht an den Browser geliefert.
Alle Assets werden lokal ausgeliefert, ohne CDN oder externe Dienste.

Dashboard und temporaerer OAuth-Anmeldeserver nutzen nacheinander Port 8080.
Vor `auth` muss die Bridge deshalb wie oben beschrieben gestoppt werden.
Mit `DASHBOARD_ENABLED=false` laesst sich das Dashboard abschalten.
Ausserhalb von Docker bindet es standardmaessig an `127.0.0.1`;
`DASHBOARD_BIND_HOST` und `DASHBOARD_PORT` steuern die interne Bindung.
Bei Aenderung des internen Ports muss auch `--publish` bzw. das
Compose-Portmapping angepasst werden; fuer die Anmeldung bleibt `AUTH_PORT`
separat.

Das Dashboard hat **keinen eigenen Login** und enthaelt VIN und Fahrdaten.
Die gezeigten Docker-Befehle und Compose veroeffentlichen es deshalb nur
auf dem lokalen Docker-Host.
Vom NAS aus den oben beschriebenen SSH-Tunnel verwenden. Fuer weiteren
Zugriff ausschliesslich einen vertrauenswuerdigen Reverse Proxy mit
Authentifizierung und HTTPS vorschalten; nicht ungeschuetzt ins Internet
stellen.

## MQTT-Topics

Beispielbasis: `volvo/YV1AB12C3D1234567`

| Topic relativ zur Basis | Beispiel | Bedeutung |
| --- | --- | --- |
| `energy` | JSON, siehe unten | Energy-v2-Objekt inkl. Status, Einheit, Messzeit |
| `energy/batteryChargeLevel` | `61` | Akkustand |
| `energy/electricRange` | `240` | Restreichweite |
| `energy/chargingStatus` | `IDLE` | Ladestatus, Originalwert von Volvo |
| `energy/chargerConnectionStatus` | `DISCONNECTED` | Kabel-/Verbindungsstatus |
| `energy/targetBatteryChargeLevel` | `80` | Eingestelltes Ladeziel, sofern verfuegbar |
| `energy/chargingPower` | `7.2` | Ladeleistung, sofern verfuegbar; Einheit im JSON |
| `energy/estimatedChargingTimeToTargetBatteryChargeLevel` | `90` | Restladezeit, sofern verfuegbar |
| `odometer` | `{"odometer":{"value":12345,"unit":"km","timestamp":"..."}}` | Kilometerstand inkl. Metadaten |
| `odometer/value` | `12345` | Kilometerstand als Einzelwert |
| `availability` | `online` / `offline` | Letzter kompletter Abruf erfolgreich / Fehler oder Bridge offline |
| `energy/availability` | `online` / `offline` | Mindestens ein gueltiger Energiewert im letzten Abruf |
| `odometer/availability` | `online` / `offline` | Gueltiger Kilometerstand im letzten Abruf |
| `last_success` | `2026-10-24T20:00:00+00:00` | Zeit des letzten komplett erfolgreichen Abrufs |
| `error` | `{}` oder `{"energy":"Volvo HTTP 403: ..."}` | Fehler des letzten Zyklus |

Beispiel fuer eine `energy`-Nachricht (gekuerzt):

```json
{
  "batteryChargeLevel": {
    "status": "OK",
    "value": 61,
    "unit": "percentage",
    "updatedAt": "2026-10-24T20:00:00Z"
  },
  "chargingStatus": {
    "status": "OK",
    "value": "IDLE",
    "updatedAt": "2026-10-24T20:00:00Z"
  }
}
```

Alle vom Energy-Endpunkt gelieferten Messfelder werden mit Originalnamen
abgebildet. Die JSON-Nachricht ist massgeblich fuer Einheiten, Messzeitpunkte
und Fehler einzelner Felder. Nicht unterstuetzte/ungueltige Werte bleiben im
JSON als Fehler sichtbar; ihr retained Einzelwert wird durch eine leere
Nachricht geloescht. Eine echte `0` bleibt dagegen eine `0`.

Bei HTTP-/Netzwerkfehlern bleiben letzte Messwerte retained erhalten, werden
aber als nicht verfuegbar markiert. Smarthome-Automationen sollten **immer
die globale Verfuegbarkeit und die Messzeit** pruefen. Die Bereichsverfuegbarkeit
allein erkennt einen abgestuerzten Container nicht: dafuer sendet der Broker
das globale Last Will `offline`. QoS 1 kann doppelte Nachrichten erzeugen.

Zum Beobachten mit einem vorhandenen Mosquitto-Client:

```sh
mosquitto_sub -h BROKER -u BENUTZER -P PASSWORT -t 'volvo/#' -v
```

## API-Verhalten und Fehler

Pro Zyklus gibt es normalerweise genau zwei Fahrzeugabfragen:

```text
GET https://api.volvocars.com/energy/v2/vehicles/{vin}/state
GET https://api.volvocars.com/connected-vehicle/v2/vehicles/{vin}/odometer
```

Ohne konfigurierte VIN einmal beim Start zusaetzlich:

```text
GET https://api.volvocars.com/connected-vehicle/v2/vehicles
```

Das Standardintervall ergibt hoechstens etwa 576 Fahrzeugabfragen pro Tag.
Die fuer deine App geltenden Volvo-Limits im Portal pruefen und bei Bedarf
das Intervall erhoehen. Bei HTTP 429 wird `Retry-After` (Sekunden oder
HTTP-Datum) beruecksichtigt und der restliche Zyklus abgebrochen.
Bei HTTP 401 wird genau einmal ein Token-Refresh mit erneuter Anfrage versucht.
Andere HTTP-Fehler werden nicht sofort wiederholt.

MQTT-Verbindungen werden automatisch mit Backoff wiederhergestellt.
API-Fehler eines Bereichs verhindern nicht den Abruf des anderen Bereichs
(ausser bei Rate-Limits). Fehlende Konfiguration oder unbrauchbare Tokens
werden explizit gemeldet, nicht durch Dummy-Daten ersetzt.

## Lokale Entwicklung und Tests

Python 3.14 und nur eine externe Laufzeitabhaengigkeit: `paho-mqtt`.
Die App liest Umgebungsvariablen; die `.env`-Datei wird nicht von Python
selbst geladen, sondern ueber `docker run --env-file .env` oder Compose
an den Container uebergeben.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Die Tests benoetigen weder echte Volvo-Zugangsdaten noch einen vorhandenen
MQTT-Broker. Sie pruefen API-Formate, Token-Rotation, PKCE/State, Fehlerfaelle
und MQTT-Publish auf Protokollebene mit lokalen Testservern.

### Tests im Docker-Image ohne Compose

Nach `docker build -t volvo-mqtt:local .`:

```sh
docker run --rm --init \
  --read-only \
  --tmpfs /tmp \
  --mount "type=bind,source=$(pwd)/tests,target=/app/tests,readonly" \
  --entrypoint python \
  volvo-mqtt:local -m unittest discover -s tests -v
```

Diese Tests verwenden lokale Testserver im Container, keine echten
Volvo-Zugangsdaten. Die drei Tests mit externem Mosquitto-Broker werden
hierbei uebersprungen.

### Docker-End-to-End-Test mit echtem MQTT-Broker (benoetigt Compose)

Mit laufendem Docker-Daemon:

```sh
sh tests/docker-test.sh
```

Der Test baut das regulaere App-Image und startet einen echten Mosquitto-Broker
in einem isolierten Docker-Netzwerk ohne veroeffentlichte Ports. Die gesamte
Testsuite laeuft im App-Image als Benutzer `10001` mit schreibgeschuetztem
Root-Dateisystem. Zusaetzliche End-to-End-Tests starten die CLI als Prozess
und pruefen:

- Simulierte Volvo-HTTP-API -> Bridge -> Mosquitto -> unabhaengiger MQTT-Subscriber.
- JSON-Nachrichten, Einzelwerte, QoS 1 und Retain fuer spaeter verbundene Clients.
- Token-Refresh mit dauerhaft gespeicherter Rotation und Dateirechten `0600`.
- Sauberes Beenden mit SIGTERM und Last Will nach einem Prozessabsturz.
- HTTP 429 als MQTT-Fehler und Abbruch weiterer Fahrzeugabfragen.

Es werden ausschliesslich Testdaten verwendet; `.env`, echte Volvo-Tokens und
ein vorhandener Smarthome-Broker bleiben unberuehrt. Container, Test-Volumes
und Netzwerk werden auch bei fehlgeschlagenen Tests automatisch entfernt.
Ohne `MQTT_TEST_HOST` werden die drei Docker-spezifischen Tests beim lokalen
Testlauf uebersprungen.

## Quellen

- [Connected Vehicle v2 Uebersicht](https://developer.volvocars.com/apis/connected-vehicle/v2/overview/)
- [Connected Vehicle v2 Spezifikation](https://developer.volvocars.com/apis/connected-vehicle/v2/specification/)
- [Energy v2 Spezifikation](https://developer.volvocars.com/apis/energy/v2/specification/)
- [Offizielles Volvo-OAuth-Beispiel](https://github.com/volvo-cars/developer-portal-api-samples/tree/main/oauth2-code-flow-sample)
- [Aktuelle evcc-Volvo-Anbindung](https://github.com/evcc-io/evcc/tree/master/vehicle/volvo/connected)
- [Oeffentlicher Spiegel der Energy-OpenAPI-Spezifikation](https://github.com/YOU54F/volvo-cars-api-drift/blob/main/specs/energy-api-specification.json)

Die Portalseiten waren waehrend der Umsetzung aus dieser Umgebung nicht
direkt abrufbar (HTTP 403). Endpunkte und Scopes wurden mit evcc, dem
offiziellen OAuth-Beispiel und den erreichbaren OpenID-Metadaten abgeglichen;
die Energy-Datenform mit dem verlinkten Spezifikationsspiegel.
