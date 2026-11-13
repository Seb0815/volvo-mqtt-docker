"use strict";

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const units = { percentage: "%", kilometers: "km", minutes: "min", ampere: "A", kilowatt: "kW" };
const charging = {
  CHARGING: "Wird geladen", IDLE: "Bereit", DONE: "Abgeschlossen",
  COMPLETED: "Abgeschlossen", FAULT: "Ladefehler", ERROR: "Ladefehler",
  SCHEDULED: "Geplant", PAUSED: "Pausiert", NOT_CHARGING: "Laedt nicht",
};
const connection = {
  CONNECTED: "Ladekabel verbunden", DISCONNECTED: "Kein Ladekabel verbunden",
  FAULT: "Fehler am Ladeanschluss",
};

function text(id, value) { $(id).textContent = value; }
function date(value) {
  if (!value) return "--";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "--" : parsed.toLocaleString("de-DE");
}
function valid(field) {
  return field && (field.status === undefined || field.status === "OK")
    && field.value !== null && field.value !== undefined;
}
function value(field) {
  if (!valid(field)) return "--";
  const formatted = typeof field.value === "number" ? number.format(field.value) : String(field.value);
  return formatted + (field.unit ? " " + (units[field.unit] || field.unit) : "");
}
function measureTime(id, field, available, now, threshold) {
  const measured = field && (field.updatedAt || field.timestamp);
  const age = measured ? (now - new Date(measured).getTime()) / 1000 : NaN;
  const old = Number.isFinite(age) && age > threshold;
  text(id, !valid(field) ? "Nicht verfuegbar" : measured
    ? "Messung: " + date(measured) + (old ? " · Aelterer Messwert" : "") : "Messzeit nicht geliefert");
  $(id).className = "measurement-note" + (old || !available ? " old" : "");
}
function badge(label, style) {
  text("connection", label);
  $("connection").className = "badge " + style;
}
function area(id, state) {
  text(id, state.available ? "Daten verfuegbar" : state.error ? "Abruffehler" : "Warte auf Daten");
  $(id).className = state.available ? "good-text" : state.error ? "bad-text" : "";
}
function render(state) {
  const energy = state.areas.energy;
  const odometer = state.areas.odometer;
  const data = energy.data;
  const now = new Date(state.server_time).getTime();
  const threshold = Math.max(600, state.poll_interval * 2);
  text("vin", state.vin || "Warte auf Fahrzeug");
  text("topic", state.vin ? state.topic_prefix + "/" + state.vin : "--");
  text("battery", value(data.batteryChargeLevel));
  $("battery-progress").value = valid(data.batteryChargeLevel)
    && typeof data.batteryChargeLevel.value === "number"
    ? Math.max(0, Math.min(100, data.batteryChargeLevel.value)) : 0;
  text("range", value(data.electricRange));
  text("charging", valid(data.chargingStatus)
    ? (charging[data.chargingStatus.value] || data.chargingStatus.value) : "--");
  text("cable", valid(data.chargerConnectionStatus)
    ? (connection[data.chargerConnectionStatus.value] || data.chargerConnectionStatus.value)
    : "Kabelstatus nicht verfuegbar");
  text("odometer", value(odometer.data.odometer));
  for (const [id, field, available] of [
    ["battery-time", data.batteryChargeLevel, energy.available],
    ["range-time", data.electricRange, energy.available],
    ["charging-time", data.chargingStatus, energy.available],
    ["odometer-time", odometer.data.odometer, odometer.available],
  ]) measureTime(id, field, available, now, threshold);

  text("mqtt", state.mqtt.connected ? "Verbunden" : "Nicht verbunden");
  $("mqtt").className = state.mqtt.connected ? "good-text" : "bad-text";
  area("energy-status", energy);
  area("odometer-status", odometer);
  text("last-publish", date(state.mqtt.last_publish));
  text("last-attempt", date(state.poll.last_attempt));
  text("last-success", date(state.poll.last_success));
  text("next-attempt", state.poll.in_progress ? "Wird gerade abgefragt"
    : !state.mqtt.connected ? "Nach MQTT-Verbindung" : date(state.poll.next_attempt));
  text("interval", number.format(state.poll_interval) + " Sekunden");
  text("dashboard-time", new Date().toLocaleTimeString("de-DE"));

  const errors = Object.entries(state.poll.errors).map(([name, error]) => name + ": " + error);
  if (state.mqtt.error) errors.push("MQTT: " + state.mqtt.error);
  $("errors").replaceChildren(...errors.map((error) => {
    const item = document.createElement("li");
    item.textContent = error;
    return item;
  }));
  $("errors-panel").hidden = errors.length === 0;
  if (!state.mqtt.connected) {
    badge("MQTT offline", "bad");
    text("notice", "Keine MQTT-Verbindung. Letzte Werte bleiben sichtbar, werden aber derzeit nicht uebertragen.");
    $("notice").className = "notice bad";
  } else if (errors.length) {
    badge("Abruf eingeschraenkt", "warn");
    text("notice", "Mindestens ein Abruf ist fehlgeschlagen. Angezeigte Werte koennen aus einem frueheren Abruf stammen.");
    $("notice").className = "notice warn";
  } else if (state.stale) {
    badge(state.poll.last_success ? "Daten veraltet" : "Warte auf Daten", "warn");
    text("notice", state.poll.last_success
      ? "Seit laengerem kein vollstaendig erfolgreicher Abruf. Bitte Messzeiten und Verbindung pruefen."
      : "MQTT ist verbunden. Warte auf den ersten vollstaendigen Fahrzeugabruf.");
    $("notice").className = "notice warn";
  } else {
    badge("Bridge online", "good");
    text("notice", "Die letzte Uebertragung war erfolgreich. Die Messzeiten zeigen, wann das Fahrzeug die Daten geliefert hat.");
    $("notice").className = "notice";
  }

  const rows = [];
  for (const [name, group] of [["energy", energy.data], ["odometer", odometer.data]]) {
    for (const [key, field] of Object.entries(group)) {
      if (!field || typeof field !== "object") continue;
      const row = document.createElement("tr");
      for (const cell of [name + "/" + key, value(field), field.code || field.status || "OK",
        date(field.updatedAt || field.timestamp)]) {
        const td = document.createElement("td");
        td.textContent = cell;
        row.append(td);
      }
      rows.push(row);
    }
  }
  if (rows.length) $("measurements").replaceChildren(...rows);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: AbortSignal.timeout(4000) });
    if (!response.ok) throw new Error("HTTP " + response.status);
    render(await response.json());
  } catch (error) {
    badge("Dashboard nicht erreichbar", "bad");
    text("notice", "Keine Verbindung zur Bridge. Angezeigte Werte sind moeglicherweise veraltet. Erneuter Versuch in 5 Sekunden.");
    $("notice").className = "notice bad";
  } finally {
    setTimeout(refresh, 5000);
  }
}
refresh();
