"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  running: true,
  tick: 0,
  distance: 0,
  speeds: [],
  map: null,
  marker: null,
  pathLine: null,
  pathCoords: [],
  lastLat: null,
  lastLon: null
};

function fixed(value, digits = 2) {
  if (value === null || value === undefined || isNaN(value)) return "0.00";
  return Number(value).toFixed(digits);
}

function riskPresentation(score) {
  const pct = score > 1 ? Math.round(score) : Math.round(score * 100);
  if (pct >= 60) return [pct, "HIGH", "High Risk Warning", "Elevated vehicle dynamics or physiological strain detected.", "#d45454"];
  if (pct >= 35) return [pct, "MODERATE", "Moderate Risk", "Vehicle parameters or physiological vitals show slight elevation.", "#e98140"];
  return [pct, "LOW", "Conditions Normal", "Vehicle speeds and driver vitals are within safe baseline ranges.", "#20a574"];
}

function initMap() {
  const mapElem = $("actual-map");
  if (!mapElem) return;
  
  if (!window.L) {
    mapElem.innerHTML = '<div class="map-unavailable"><strong>Map Unavailable</strong><span>Connect to internet to load OpenStreetMap.</span></div>';
    return;
  }

  state.map = L.map("actual-map", { zoomControl: true }).setView([25.3223, 55.5136], 14);
  
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(state.map);

  state.pathLine = L.polyline([], { color: "#0f766e", weight: 5, opacity: 0.8 }).addTo(state.map);

  const icon = L.divIcon({
    className: "vehicle-marker-wrap",
    html: '<div class="vehicle-map-marker" aria-label="Vehicle"><svg viewBox="0 0 24 24"><path d="M5 16v-5l2-4h10l2 4v5"/><path d="M3 14h18v5H3zM6 19v2M18 19v2M7 16h.01M17 16h.01"/></svg></div>',
    iconSize: [46, 46],
    iconAnchor: [23, 23]
  });

  state.marker = L.marker([25.3223, 55.5136], { icon }).addTo(state.map).bindPopup("<strong>Live Hardware Vehicle</strong>");
}

function updateMap(lat, lon) {
  if (!state.map || !state.marker || !lat || !lon) return;
  const point = [lat, lon];
  
  state.marker.setLatLng(point);
  
  if (state.lastLat !== lat || state.lastLon !== lon) {
    state.pathCoords.push(point);
    if (state.pathCoords.length > 200) state.pathCoords.shift();
    state.pathLine.setLatLngs(state.pathCoords);
    state.map.panTo(point, { animate: true, duration: 0.5 });
    state.lastLat = lat;
    state.lastLon = lon;
  }
}

function sparkline(values) {
  const pathElem = $("speed-path");
  const areaElem = $("speed-area");
  if (!pathElem || values.length < 2) return;

  const max = Math.max(...values, 60);
  const min = 0;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * 360},${72 - ((v - min) / (max - min || 1)) * 60}`);

  pathElem.setAttribute("d", `M${points.join(" L")}`);
  if (areaElem) {
    areaElem.setAttribute("d", `M0,80 L${points.join(" L")} L360,80 Z`);
  }
}

function render(data) {
  if (!data) return;

  const telematics = data.live_telematics || {};
  const vitals = data.live_vitals || {};
  const location = data.location || data.position || {};
  
  const speed = Math.round(telematics.speed_kmh ?? data.speed ?? 0);
  const rpm = Math.round(telematics.rpm ?? data.rpm ?? data.engine_speed ?? 0);
  const throttle = Math.round(telematics.throttle ?? data.throttle ?? 0);
  const load = Math.round(data.engine_load ?? data.load ?? 0);
  const accel = telematics.acceleration ?? data.acceleration ?? data.ax ?? 0;
  
  const hr = Math.round(vitals.hr ?? data.heart ?? 0);
  const rmssd = Math.round(vitals.rmssd ?? 0);
  const sdnn = Math.round(vitals.sdnn ?? 0);
  
  const lat = location.latitude ?? location.lat ?? data.latitude ?? 25.3223;
  const lon = location.longitude ?? location.lon ?? data.longitude ?? 55.3418;

  // 1. Map & Location Cards
  updateMap(lat, lon);
  if ($("place-name")) $("place-name").textContent = location.name || "Live Vehicle GPS";
  if ($("coordinates")) $("coordinates").textContent = `${fixed(lat, 5)}, ${fixed(lon, 5)}`;
  if ($("gps-lat")) $("gps-lat").textContent = `${fixed(lat, 5)}°`;
  if ($("gps-lon")) $("gps-lon").textContent = `${fixed(lon, 5)}°`;

  // 2. Primary Telematics
  if ($("speed")) $("speed").textContent = speed;
  if ($("gps-speed")) $("gps-speed").textContent = `${speed} km/h`;
  if ($("rpm")) $("rpm").textContent = `${rpm.toLocaleString()} rpm`;
  if ($("throttle")) $("throttle").textContent = `${throttle}%`;
  if ($("engine-load")) $("engine-load").textContent = `${load}%`;
  if ($("accel")) $("accel").textContent = `${fixed(accel, 1)} / 0.0 / 9.8 m/s²`;
  
  if ($("heading")) $("heading").textContent = "0° N";
  if ($("gyro")) $("gyro").textContent = "0.00 / 0.00 / 0.00 rad/s";
  if ($("lateral-force")) $("lateral-force").textContent = "0.00 g";
  if ($("steering")) $("steering").textContent = "0.0°";
  if ($("swerve")) {
    const swerving = Math.abs(accel) > 2.5;
    $("swerve").textContent = swerving ? "Detected" : "Not detected";
    $("swerve").className = `reading-pill ${swerving ? "alert" : "normal"}`;
  }

  // 3. Vitals
  if ($("heart-rate")) $("heart-rate").textContent = hr || "--";
  if ($("spo2")) $("spo2").textContent = rmssd ? `${rmssd}` : "98";
  if ($("quality")) $("quality").textContent = sdnn ? `${sdnn}` : "95";
  if ($("heart-note")) {
    $("heart-note").textContent = hr > 110 ? "Elevated Heart Rate" : hr > 85 ? "Moderate Heart Rate" : "Normal Resting Range";
  }

  // 4. Risk Prediction Display
  const rawRisk = data.risk_score !== undefined ? data.risk_score : (data.risk ? data.risk / 100 : 0.15);
  const [scorePct, level, message, detail, color] = riskPresentation(rawRisk);
  
  if ($("risk-score")) $("risk-score").textContent = scorePct;
  if ($("risk-chip")) {
    $("risk-chip").textContent = level;
    $("risk-chip").style.color = color;
  }
  if ($("event-name")) $("event-name").textContent = data.event || "Live ECU Telematics Active";
  if ($("risk-message")) $("risk-message").textContent = message;
  if ($("risk-detail")) $("risk-detail").textContent = detail;
  if ($("risk-gauge")) {
    $("risk-gauge").style.setProperty("--score", scorePct);
    $("risk-gauge").style.setProperty("--green", color);
  }
  if ($("risk-card")) $("risk-card").style.setProperty("--risk-color", color);

  // 5. Trip Statistics
  state.tick++;
  state.distance += (speed / 3600) * 0.5;
  if ($("travelled")) $("travelled").textContent = `${state.distance.toFixed(2)} km`;
  if ($("data-points")) $("data-points").textContent = state.tick;
  if ($("packet-status")) $("packet-status").textContent = `Packet ${state.tick}`;
  if ($("elapsed")) $("elapsed").textContent = `${String(Math.floor(state.tick / 120)).padStart(2, "0")}:${String(Math.floor((state.tick % 120) / 2)).padStart(2, "0")}`;
  if ($("last-update")) $("last-update").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;

  state.speeds.push(speed);
  if (state.speeds.length > 25) state.speeds.shift();
  sparkline(state.speeds);
}

async function pollLiveAPI() {
  if (!state.running) return;

  try {
    const response = await fetch("/api/v1/predict/live");
    if (!response.ok) throw new Error(`HTTP error ${response.status}`);
    
    const data = await response.json();

    if ($("connection")) {
      $("connection").className = "connection online";
      $("connection").querySelector("span").textContent = "Live Hardware Stream Connected";
    }

    render(data);
  } catch (error) {
    if ($("connection")) {
      $("connection").className = "connection offline";
      $("connection").querySelector("span").textContent = "Waiting for Hardware Stream...";
    }
  }
}

if ($("toggle-button")) {
  $("toggle-button").addEventListener("click", () => {
    state.running = !state.running;
    const button = $("toggle-button");
    button.classList.toggle("paused", !state.running);
    button.querySelector("span").textContent = state.running ? "Pause drive" : "Continue drive";
  });
}

initMap();
setInterval(pollLiveAPI, 500);