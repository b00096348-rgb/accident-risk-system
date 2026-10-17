"use strict";
const $ = id => document.getElementById(id);
const state = {mode: "live", running: true, generation: 0, map: null, marker: null, tick: 0};
const valid = value => typeof value === "number" && Number.isFinite(value);
const fmt = (value, unit = "", digits = 0) => valid(value) ? `${value.toFixed(digits)}${unit}` : "—";
function text(id, value) { if ($(id)) $(id).textContent = value; }
function render(data = {}) {
  const demo = state.mode === "demo";
  const active = data.status === "active";
  const message = demo ? "Demo · simulated readings" : active ? "Receiving live readings" : data.status === "stale" ? "Stale · waiting for new readings" : "Waiting for sensors";
  $("connection").className = `connection ${active ? "online" : "offline"}`;
  $("connection").querySelector("span").textContent = message;
  text("event-name", message);
  text("speed", fmt(data.speed)); text("gps-speed", fmt(data.speed, " km/h"));
  text("rpm", fmt(data.rpm, " rpm")); text("throttle", fmt(data.throttle, "%"));
  text("engine-load", fmt(data.engine_load, "%")); text("heart-rate", fmt(data.heart));
  text("spo2", fmt(data.spo2)); text("quality", fmt(valid(data.quality) ? data.quality * 100 : null));
  text("heart-note", valid(data.heart) ? (demo ? "Simulated reading" : "Received sensor reading") : "Waiting for sensor");
  text("wearable-status", valid(data.heart) || valid(data.spo2) ? (demo ? "Demo wearable" : "Wearable readings received") : "Waiting for wearable");
  text("heading", fmt(valid(data.heading) ? data.heading * 180 / Math.PI : null, "°"));
  text("steering", fmt(valid(data.steering_angle) ? data.steering_angle * 180 / Math.PI : null, "°", 1));
  for (const group of ["accel", "gyro"]) {
    const values = ["x", "y", "z"].map(axis => data[`${group}_${axis}`]);
    text(group, values.some(valid) ? values.map(v => fmt(v, "", 2)).join(" / ") + (group === "accel" ? " m/s²" : " rad/s") : "—");
  }
  text("lateral-force", "—"); text("swerve", "—");
  for (const tire of ["fl", "fr", "rl", "rr"]) text(`tire-${tire}`, fmt(data[`tire_pressure_${tire}`], " kPa"));
  const lat = data.location?.latitude, lon = data.location?.longitude;
  const hasGPS = valid(lat) && valid(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
  text("gps-lat", fmt(lat, "°", 5)); text("gps-lon", fmt(lon, "°", 5));
  text("coordinates", hasGPS ? `${fmt(lat, "", 5)}, ${fmt(lon, "", 5)}` : "—");
  text("place-name", hasGPS ? (demo ? "Simulated Dubai location" : "Received GPS location") : "Waiting for GPS");
  if (state.map) {
    if (hasGPS) {
      if (!state.marker) state.marker = L.marker([lat, lon]).addTo(state.map);
      state.marker.setLatLng([lat, lon]); state.map.setView([lat, lon], 14);
    } else if (state.marker) { state.marker.remove(); state.marker = null; }
  }
  const score = valid(data.risk_score) ? data.risk_score * 100 : null;
  text("risk-score", fmt(score)); text("risk-chip", score === null ? "UNAVAILABLE" : demo ? "DEMO" : "MODEL OUTPUT");
  text("risk-message", demo ? "Simulated index" : data.risk_status || "Waiting for required sensors");
  text("risk-detail", demo ? "Made-up values for presentation only." : "Research output · not a crash probability or safety assessment.");
  $("risk-gauge").style.setProperty("--score", score ?? 0);
  text("last-update", data.received_at ? `Last packet: ${new Date(data.received_at * 1000).toLocaleTimeString()}` : message);
  for (const id of ["travelled", "elapsed", "data-points"]) text(id, "—");
  text("packet-status", message);
}
function demoData() {
  state.tick++;
  return {status: "active", received_at: Date.now() / 1000, speed: 40 + Math.random() * 15,
    rpm: 1800 + Math.random() * 400, throttle: 24 + Math.random() * 8, engine_load: 38,
    heart: 74 + Math.random() * 8, spo2: 98, quality: .96, risk_score: (Math.sin(state.tick / 8) + 1) / 2,
    location: {latitude: 25.1972 + Math.sin(state.tick / 30) * .003, longitude: 55.2744 + Math.cos(state.tick / 30) * .003},
    accel_x: .2, accel_y: .1, accel_z: 9.81, gyro_x: 0, gyro_y: 0, gyro_z: .02,
    heading: .5, steering_angle: .03, tire_pressure_fl: 235, tire_pressure_fr: 236,
    tire_pressure_rl: 238, tire_pressure_rr: 237};
}
async function update() {
  if (!state.running) return;
  if (state.mode === "demo") { render(demoData()); return; }
  const generation = state.generation;
  try {
    const response = await fetch("/api/v1/predict/live", {cache: "no-store", signal: AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error("Unavailable");
    const data = await response.json();
    if (generation === state.generation && state.running) render(data);
  } catch {
    if (generation === state.generation && state.running) render({status: "unavailable", risk_status: "Server unavailable"});
  }
}
function switchMode(mode) {
  state.mode = mode; state.generation++; state.running = true; state.tick = 0;
  for (const name of ["live", "demo"]) $(`${name}-tab`).setAttribute("aria-pressed", String(name === mode));
  text("mode-notice", mode === "demo" ? "Demo mode: all readings are simulated and never sent to the live API." : "Live mode: only fresh received sensor values are shown. Missing or stale readings appear as —.");
  $("toggle-button").querySelector("span").textContent = "Pause updates";
  $("toggle-button").setAttribute("aria-pressed", "false");
  render(); update();
}
$("live-tab").onclick = () => switchMode("live");
$("demo-tab").onclick = () => switchMode("demo");
$("toggle-button").onclick = () => {
  state.running = !state.running; state.generation++;
  $("toggle-button").setAttribute("aria-pressed", String(!state.running));
  $("toggle-button").querySelector("span").textContent = state.running ? "Pause updates" : "Resume updates";
  if (state.running) update(); else { render(); $("connection").querySelector("span").textContent = "Updates paused"; }
};
if (window.L) {
  state.map = L.map("actual-map").setView([0, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {attribution: "© OpenStreetMap contributors"}).addTo(state.map);
} else text("actual-map", "Map unavailable · GPS readings still appear below");
switchMode("live");
setInterval(update, 1000);
