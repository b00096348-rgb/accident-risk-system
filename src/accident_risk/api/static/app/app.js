/* RoadSense driver and operator interface.
   Vanilla ES2020, no build step. Talks to /app/api. */
"use strict";

const API = "/app/api";
const STORE = "roadsense.token";

const state = {
  token: null,
  user: null,
  phone: "",
  timer: null,
  map: null,
  markers: new Map(),
  collisionTimer: null,
  collisionAlert: null,
  armedStream: null,
};

/* ────────────────────────────────── utilities ─────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function num(value, digits = 0, fallback = "—") {
  return value === null || value === undefined || Number.isNaN(value)
    ? fallback
    : Number(value).toFixed(digits);
}

function clock(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(API + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    signOut(true);
    throw new Error("Your session has expired. Sign in again.");
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = payload && payload.detail;
    throw new Error(
      typeof detail === "string" ? detail
        : Array.isArray(detail) && detail.length ? detail[0].msg
          : `Request failed (${response.status})`
    );
  }
  return payload;
}

/* ───────────────────────────────── sign in ────────────────────────────── */

function showSignIn() {
  document.body.dataset.view = "signin";
  $("#signin").hidden = false;
  $("#shell").hidden = true;
  $("#phone-form").hidden = false;
  $("#code-form").hidden = true;
}

function signInError(message) {
  const el = $("#signin-error");
  el.hidden = !message;
  el.textContent = message || "";
}

$("#phone-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  signInError("");
  state.phone = $("#phone").value.trim();
  try {
    const result = await api("/auth/request-code", {
      method: "POST", body: { phone: state.phone },
    });
    $("#phone-form").hidden = true;
    $("#code-form").hidden = false;
    $("#code").focus();
    $("#code-hint").textContent = result.code
      ? `Development build: your code is ${result.code}`
      : `Sent to ${state.phone}. It expires in five minutes.`;
  } catch (error) {
    signInError(error.message);
  }
});

$("#change-phone").addEventListener("click", () => {
  $("#code-form").hidden = true;
  $("#phone-form").hidden = false;
  signInError("");
  $("#phone").focus();
});

$("#code-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  signInError("");
  try {
    const result = await api("/auth/verify", {
      method: "POST", body: { phone: state.phone, code: $("#code").value.trim() },
    });
    state.token = result.token;
    localStorage.setItem(STORE, result.token);
    await start();
  } catch (error) {
    signInError(error.message);
    $("#code").select();
  }
});

async function signOut(silent = false) {
  stopPolling();
  try {
    if (!silent && state.token) await api("/auth/logout", { method: "POST" });
  } catch { /* signing out locally is enough */ }
  state.token = null;
  state.user = null;
  localStorage.removeItem(STORE);
  $("#phone").value = "";
  $("#code").value = "";
  showSignIn();
}

$("#signout").addEventListener("click", () => signOut());

/* ───────────────────────────────── routing ────────────────────────────── */

const ROLE_NAMES = {
  driver: "Driver", account_manager: "Account manager", administrator: "Administrator",
};

function navFor(role) {
  const items = [];
  if (role === "driver") items.push(["drive", "Drive"]);
  if (role !== "driver") items.push(["monitor", "Live drivers"]);
  items.push(["sessions", role === "driver" ? "My trips" : "Trips"]);
  items.push(["alerts", "Alerts"]);
  if (role !== "driver") items.push(["drivers", "Drivers"], ["devices", "Devices"]);
  if (role === "administrator") items.push(["users", "Users"], ["settings", "Settings"]);
  return items;
}

function renderNav() {
  const current = (location.hash.replace("#/", "").split("/")[0]) || defaultRoute();
  $("#nav").innerHTML = navFor(state.user.role)
    .map(([key, label]) =>
      `<button data-route="${key}"${key === current ? ' aria-current="page"' : ""}>${label}</button>`)
    .join("");
  $("#nav").querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => { location.hash = `#/${button.dataset.route}`; });
  });
}

function defaultRoute() {
  return state.user.role === "driver" ? "drive" : "monitor";
}

const ROUTES = {
  drive: driveView,
  monitor: monitorView,
  sessions: sessionsView,
  alerts: alertsView,
  drivers: driversView,
  devices: devicesView,
  users: usersView,
  settings: settingsView,
};

async function route() {
  if (!state.user) return;
  stopPolling();
  const parts = location.hash.replace("#/", "").split("/").filter(Boolean);
  const name = parts[0] || defaultRoute();
  renderNav();
  const view = name === "sessions" && parts[1] ? sessionDetailView : ROUTES[name];
  if (!view) { location.hash = `#/${defaultRoute()}`; return; }
  try {
    await view(parts[1]);
  } catch (error) {
    $("#main").innerHTML = `<div class="card"><div class="empty">
      <strong>That didn't load</strong>${esc(error.message)}</div></div>`;
  }
}

window.addEventListener("hashchange", route);

function stopPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  state.map = null;
  state.markers.clear();
}

function poll(fn, ms) {
  fn();
  state.timer = setInterval(fn, ms);
}

/* ─────────────────────────────── driver view ──────────────────────────── */

const RISK_COPY = {
  LOW: "Driving pattern and heart rate are both in their usual range.",
  MEDIUM: "Something is drifting. Ease off and keep more distance.",
  HIGH: "Vehicle data and your heart rate both suggest elevated risk. Slow down.",
  CRITICAL: "Pull over safely when you can.",
};

async function driveView() {
  $("#main").innerHTML = `
    <div class="drive-grid">
      <div class="risk area-risk" id="risk-panel">
        <div class="risk-state" id="risk-state">No session</div>
        <div class="risk-index" id="risk-index">Start a session to begin monitoring.</div>
        <div class="bands" id="risk-bands"><span></span><span></span><span></span><span></span></div>
        <p class="risk-note" id="risk-note">
          The risk index is a research output, not a probability of a crash.
        </p>
      </div>
      <div class="card area-session">
        <div class="card-head"><h2>Session</h2></div>
        <div class="links" id="links"></div>
        <div style="margin-top:16px" id="session-actions"></div>
      </div>
      <div class="card area-readings">
        <div class="card-head"><h2>Live readings</h2></div>
        <div class="readouts" id="readouts"></div>
      </div>
      <div class="card area-kit">
        <div class="card-head"><h2>Your kit</h2></div>
        <div id="kit"></div>
      </div>
    </div>`;
  poll(refreshDriver, 2000);
}

async function refreshDriver() {
  let data;
  try { data = await api("/driver/state"); } catch { return; }
  const level = data.risk_level;
  const panel = $("#risk-panel");
  if (!panel) return;
  panel.style.setProperty("--state", level ? `var(--${level.toLowerCase()})` : "var(--muted)");

  const running = Boolean(data.session);
  // Re-arm native capture if the app was restarted while a session was open:
  // the server still has the session, but the service may have been killed.
  if (native && running && state.armedStream !== data.session.stream_id) {
    state.armedStream = data.session.stream_id;
    native.sessionStarted(data.session.stream_id);
  } else if (native && !running && state.armedStream) {
    state.armedStream = null;
    native.sessionEnded();
  }
  $("#risk-state").textContent = level || (running ? "Warming up" : "No session");
  $("#risk-index").textContent = data.risk_index !== null && data.risk_index !== undefined
    ? `Risk index ${num(data.risk_index, 2)} of 1.00`
    : running
      ? data.models_loaded ? "Waiting for enough sensor data." : "No risk model is loaded."
      : "Start a session to begin monitoring.";
  $("#risk-note").textContent = (level && RISK_COPY[level])
    || "The risk index is a research output, not a probability of a crash.";

  const order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
  const reached = order.indexOf(level);
  $("#risk-bands").querySelectorAll("span").forEach((span, i) => {
    span.classList.toggle("on", reached >= 0 && i <= reached);
  });

  $("#links").innerHTML = [
    ["Vehicle adapter", data.vehicle_connected, "OBD-II over Bluetooth"],
    ["Wearable", data.wearable_connected, "Heart rate broadcast"],
    ["Upload", data.uploading, "Sending to the server"],
  ].map(([title, on, sub]) => `
    <div class="link-chip ${on ? "on" : "off"}">
      <span class="dot"></span>
      <span><strong>${title}</strong><small>${on ? sub : "Not connected"}</small></span>
    </div>`).join("");

  $("#readouts").innerHTML = [
    ["Speed", num(data.speed_kmh, 0), "km/h"],
    ["Engine", num(data.rpm, 0), "rpm"],
    ["Heart rate", num(data.heart_rate, 0), "bpm"],
    ["Engine load", num(data.engine_load, 0), "%"],
    ["Throttle", num(data.throttle, 0), "%"],
  ].map(([label, value, unit]) => `
    <div class="readout"><small>${label}</small>
      <strong>${value} <em>${unit}</em></strong></div>`).join("");

  $("#session-actions").innerHTML = running
    ? `<button class="btn big" id="stop-session">End session</button>
       <p class="hint">Started ${esc(clock(data.session.started_at))}</p>`
    : `<button class="btn primary big" id="start-session">Start session</button>
       <p class="hint">Connect the adapter and wearable first.</p>`;

  const startButton = $("#start-session");
  if (startButton) startButton.addEventListener("click", startSession);
  const stopButton = $("#stop-session");
  if (stopButton) stopButton.addEventListener("click", stopSession);

  const kit = state.user.kit || [];
  $("#kit").innerHTML = kit.length
    ? `<div class="table-wrap"><table><tbody>${kit.map((d) => `
        <tr><td>${d.kind === "adapter" ? "Adapter" : "Wearable"}</td>
        <td>${esc(d.identifier)}</td>
        <td><span class="pill ${esc(d.status)}">${esc(d.status.replace(/_/g, " "))}</span></td></tr>`
      ).join("")}</tbody></table></div>`
    : `<p class="muted">No kit assigned yet. Your account manager assigns one.</p>`;

  if (data.pending_collision) showCollision(data.pending_collision);
  else hideCollision();
}

/* Inside the Android app a native service owns the Bluetooth links, because a
   browser cannot open a serial connection to an ELM327. The object is absent in
   a normal browser, so these calls are no-ops there. */
const native = typeof RoadSenseNative !== "undefined" ? RoadSenseNative : null;

async function startSession() {
  try {
    const session = await api("/driver/session/start", { method: "POST", body: {} });
    if (native) {
      // Record it as armed before the next poll runs, or the re-arm check above
      // fires a second start and restarts capture mid-handshake.
      state.armedStream = session.stream_id;
      native.sessionStarted(session.stream_id);
    }
    toast(native ? "Recording — keep the app installed and signed in" : "Session started");
    refreshDriver();
  } catch (error) { toast(error.message, true); }
}

async function stopSession() {
  try {
    const result = await api("/driver/session/stop", { method: "POST" });
    if (native) {
      state.armedStream = null;
      native.sessionEnded();
    }
    const km = result.summary && result.summary.distance_km;
    toast(km ? `Session saved — ${km} km` : "Session saved");
    refreshDriver();
  } catch (error) { toast(error.message, true); }
}

/* ──────────────────────────── collision takeover ──────────────────────── */

function showCollision(alert) {
  if (state.collisionAlert === alert.id) return;
  state.collisionAlert = alert.id;
  $("#collision").hidden = false;
  let left = 20;
  $("#collision-count").textContent = left;
  clearInterval(state.collisionTimer);
  state.collisionTimer = setInterval(() => {
    left -= 1;
    $("#collision-count").textContent = Math.max(left, 0);
    if (left <= 0) resolveCollision("confirm");
  }, 1000);
}

function hideCollision() {
  if ($("#collision").hidden) return;
  $("#collision").hidden = true;
  clearInterval(state.collisionTimer);
  state.collisionAlert = null;
}

async function resolveCollision(action) {
  const id = state.collisionAlert;
  clearInterval(state.collisionTimer);
  $("#collision").hidden = true;
  state.collisionAlert = null;
  if (!id) return;
  try {
    await api(`/alerts/${id}/${action}`, { method: "POST" });
    toast(action === "cancel" ? "Alert cancelled" : "Emergency alert sent");
  } catch (error) { toast(error.message, true); }
}

$("#collision-cancel").addEventListener("click", () => resolveCollision("cancel"));
$("#collision-confirm").addEventListener("click", () => resolveCollision("confirm"));

/* ─────────────────────────────── monitor view ─────────────────────────── */

async function monitorView() {
  $("#main").innerHTML = `
    <div class="grid two">
      <div class="card">
        <div class="card-head"><h2>Live drivers</h2>
          <span class="muted" id="live-count">—</span></div>
        <div class="map-frame">
          <div id="map"></div>
          <div class="map-fallback" id="map-fallback">
            <strong>Map unavailable</strong>
            Street tiles need an internet connection. Positions still appear in the list.
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Active sessions</h2></div>
        <div id="live-list"></div>
      </div>
    </div>`;
  if (window.L) {
    state.map = L.map("map", { zoomControl: true }).setView([25.33, 55.42], 11);
    const tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    });
    // Only clear the fallback once a tile has actually painted, so a blocked or
    // offline network leaves an explanation rather than an empty grey box.
    tiles.on("load", () => { const f = $("#map-fallback"); if (f) f.remove(); });
    tiles.addTo(state.map);
  }
  poll(refreshLive, 3000);
}

async function refreshLive() {
  let data;
  try { data = await api("/live"); } catch { return; }
  const drivers = data.drivers || [];
  const countEl = $("#live-count");
  if (!countEl) return;
  countEl.textContent = drivers.length
    ? `${drivers.length} driving now` : "Nobody is driving";

  $("#live-list").innerHTML = drivers.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Driver</th><th>Risk</th><th>Speed</th><th>Heart</th><th>Started</th></tr></thead>
      <tbody>${drivers.map((d) => `
        <tr><td>${esc(d.driver_name)}</td>
        <td>${d.risk_level ? `<span class="pill ${d.risk_level.toLowerCase()}">${d.risk_level}</span>`
      : '<span class="pill">warming up</span>'}</td>
        <td class="num">${num(d.speed_kmh, 0)}</td>
        <td class="num">${num(d.heart_rate, 0)}</td>
        <td>${esc(clock(d.started_at))}</td></tr>`).join("")}
      </tbody></table></div>`
    : `<div class="empty"><strong>No active sessions</strong>
       Drivers appear here the moment they start one.</div>`;

  if (!state.map) return;
  const seen = new Set();
  drivers.forEach((d) => {
    if (d.latitude === null || d.longitude === null) return;
    seen.add(d.stream_id);
    const colour = d.risk_level ? `var(--${d.risk_level.toLowerCase()})` : "var(--teal)";
    const initials = String(d.driver_name || "?").trim().charAt(0).toUpperCase();
    const existing = state.markers.get(d.stream_id);
    if (existing) {
      existing.setLatLng([d.latitude, d.longitude]);
    } else {
      const icon = L.divIcon({
        className: "", iconSize: [34, 34], iconAnchor: [17, 17],
        html: `<div class="map-pin" style="background:${colour}">${esc(initials)}</div>`,
      });
      const marker = L.marker([d.latitude, d.longitude], { icon }).addTo(state.map);
      marker.bindPopup(`<strong>${esc(d.driver_name)}</strong>`);
      state.markers.set(d.stream_id, marker);
    }
  });
  state.markers.forEach((marker, key) => {
    if (!seen.has(key)) { state.map.removeLayer(marker); state.markers.delete(key); }
  });
}

/* ─────────────────────────────── sessions ─────────────────────────────── */

async function sessionsView() {
  const sessions = await api("/sessions?limit=100");
  const manager = state.user.role !== "driver";
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>${manager ? "All trips" : "Your trips"}</h2>
        <span class="muted">${sessions.length} recorded</span></div>
      ${sessions.length ? `<div class="table-wrap"><table>
        <thead><tr>${manager ? "<th>Driver</th>" : ""}<th>Started</th><th>Duration</th>
          <th>Distance</th><th>Peak risk</th><th>Status</th></tr></thead>
        <tbody>${sessions.map((s) => `
          <tr class="clickable" data-id="${s.id}">
            ${manager ? `<td>${esc(s.driver_name)}</td>` : ""}
            <td>${esc(clock(s.started_at))}</td>
            <td class="num">${duration(s.summary.duration_seconds)}</td>
            <td class="num">${s.summary.distance_km ?? "—"} km</td>
            <td class="num">${s.summary.max_risk_index ?? "—"}</td>
            <td>${s.ended_at
    ? (s.complete ? '<span class="pill">complete</span>'
      : '<span class="pill pending">gaps</span>')
    : '<span class="pill active">running</span>'}</td>
          </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><strong>No trips yet</strong>
         A trip appears here once a session ends.</div>`}
    </div>`;
  $("#main").querySelectorAll("tr.clickable").forEach((row) => {
    row.addEventListener("click", () => { location.hash = `#/sessions/${row.dataset.id}`; });
  });
}

async function sessionDetailView(id) {
  const data = await api(`/sessions/${encodeURIComponent(id)}`);
  const s = data.session;
  const sum = s.summary || {};
  const stats = [
    ["Duration", duration(sum.duration_seconds)],
    ["Distance", `${sum.distance_km ?? "—"} km`],
    ["Top speed", `${sum.max_speed_kmh ?? "—"} km/h`],
    ["Idling", duration(sum.idling_seconds)],
    ["Peak risk", sum.max_risk_index ?? "—"],
    ["Mean heart rate", sum.mean_heart_rate ?? "—"],
  ];
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head">
        <h2>${esc(s.driver_name)} — ${esc(clock(s.started_at))}</h2>
        <button class="btn quiet small" id="back">Back to trips</button>
      </div>
      <div class="readouts">${stats.map(([label, value]) => `
        <div class="readout"><small>${label}</small><strong>${esc(value)}</strong></div>`).join("")}
      </div>
      ${data.gaps.length ? `<p class="muted" style="margin-top:16px">
        ${data.gaps.length} connection gap${data.gaps.length === 1 ? "" : "s"} recorded,
        longest ${duration(sum.longest_gap_seconds)}.
        ${s.complete ? "" : " This trip is flagged incomplete and is excluded from training."}
      </p>` : ""}
    </div>
    <div class="card">
      <div class="card-head"><h2>Track</h2>
        <span class="muted">${data.points.length} points</span></div>
      ${data.points.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Speed</th><th>Engine</th><th>Heart</th><th>Risk</th></tr></thead>
        <tbody>${data.points.slice(-200).reverse().map((p) => `
          <tr><td>${esc(clock(p.ts))}</td><td class="num">${num(p.speed_kmh, 0)}</td>
          <td class="num">${num(p.rpm, 0)}</td><td class="num">${num(p.heart_rate, 0)}</td>
          <td class="num">${num(p.risk_index, 2)}</td></tr>`).join("")}
        </tbody></table></div>`
      : `<div class="empty"><strong>No data points</strong>
         Nothing was ingested during this session.</div>`}
    </div>`;
  $("#back").addEventListener("click", () => { location.hash = "#/sessions"; });
}

/* ──────────────────────────────── alerts ──────────────────────────────── */

async function alertsView() {
  const alerts = await api("/alerts?limit=100");
  const manager = state.user.role !== "driver";
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Alerts</h2>
        <span class="muted">${alerts.length} recorded</span></div>
      ${alerts.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Raised</th>${manager ? "<th>Driver</th>" : ""}<th>Type</th>
          <th>Risk</th><th>Status</th><th></th></tr></thead>
        <tbody>${alerts.map((a) => `
          <tr><td>${esc(clock(a.created_at))}</td>
          ${manager ? `<td>${esc(a.driver_name)}</td>` : ""}
          <td>${a.kind === "collision" ? "Collision" : "High risk"}</td>
          <td class="num">${num(a.risk_index, 2)}</td>
          <td><span class="pill ${esc(a.status)}">${esc(a.status)}</span></td>
          <td>${a.status === "pending"
    ? `<button class="btn small" data-ack="${a.id}">Acknowledge</button>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><strong>No alerts</strong>
         Nothing has crossed the high-risk threshold.</div>`}
    </div>`;
  $("#main").querySelectorAll("[data-ack]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/alerts/${button.dataset.ack}/confirm`, { method: "POST" });
        toast("Alert acknowledged");
        alertsView();
      } catch (error) { toast(error.message, true); }
    });
  });
}

/* ──────────────────────────────── drivers ─────────────────────────────── */

async function driversView() {
  const [drivers, devices] = await Promise.all([api("/drivers"), api("/devices")]);
  const options = (kind) => devices
    .filter((d) => d.kind === kind && !d.assigned_to && d.status !== "out_of_service")
    .map((d) => `<option value="${d.id}">${esc(d.identifier)}</option>`).join("");
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Enrol a driver</h2></div>
      <form id="enrol" class="form-row">
        <div class="field"><label for="d-name">Name</label>
          <input id="d-name" required placeholder="Full name"></div>
        <div class="field"><label for="d-phone">Mobile number</label>
          <input id="d-phone" type="tel" required placeholder="+971 50 123 4567"></div>
        <div class="field"><label for="d-adapter">Adapter</label>
          <select id="d-adapter"><option value="">None yet</option>${options("adapter")}</select></div>
        <div class="field"><label for="d-wearable">Wearable</label>
          <select id="d-wearable"><option value="">None yet</option>${options("wearable")}</select></div>
        <button class="btn primary" type="submit">Enrol driver</button>
      </form>
    </div>
    <div class="card">
      <div class="card-head"><h2>Enrolled drivers</h2>
        <span class="muted">${drivers.length} in the pilot</span></div>
      ${drivers.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Mobile</th><th>Kit</th><th>Account</th></tr></thead>
        <tbody>${drivers.map((d) => `
          <tr><td>${esc(d.name)}</td><td>${esc(d.phone)}</td>
          <td>${d.kit.length ? d.kit.map((k) => esc(k.identifier)).join(", ")
    : '<span class="muted">none assigned</span>'}</td>
          <td><span class="pill ${d.active ? "active" : "cancelled"}">${
  d.active ? "active" : "inactive"}</span></td></tr>`).join("")}
        </tbody></table></div>`
      : `<div class="empty"><strong>No drivers yet</strong>
         Enrol the first one above.</div>`}
    </div>`;
  $("#enrol").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/drivers", {
        method: "POST",
        body: {
          name: $("#d-name").value.trim(),
          phone: $("#d-phone").value.trim(),
          adapter_id: $("#d-adapter").value ? Number($("#d-adapter").value) : null,
          wearable_id: $("#d-wearable").value ? Number($("#d-wearable").value) : null,
        },
      });
      toast("Driver enrolled");
      driversView();
    } catch (error) { toast(error.message, true); }
  });
}

/* ──────────────────────────────── devices ─────────────────────────────── */

async function devicesView() {
  const admin = state.user.role === "administrator";
  const [devices, drivers] = await Promise.all([api("/devices"), api("/drivers")]);
  $("#main").innerHTML = `
    ${admin ? `<div class="card">
      <div class="card-head"><h2>Add a device</h2></div>
      <form id="add-device" class="form-row">
        <div class="field"><label for="k">Type</label>
          <select id="k"><option value="adapter">OBD-II adapter</option>
          <option value="wearable">Wearable</option></select></div>
        <div class="field"><label for="ident">Identifier</label>
          <input id="ident" required placeholder="ELM-003"></div>
        <button class="btn primary" type="submit">Add device</button>
      </form>
    </div>` : ""}
    <div class="card">
      <div class="card-head"><h2>Inventory</h2>
        <span class="muted">${devices.length} devices</span></div>
      ${devices.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Type</th><th>Identifier</th><th>Status</th><th>Assigned to</th>
          ${admin ? "<th></th>" : ""}</tr></thead>
        <tbody>${devices.map((d) => `
          <tr><td>${d.kind === "adapter" ? "Adapter" : "Wearable"}</td>
          <td>${esc(d.identifier)}</td>
          <td><span class="pill ${esc(d.status)}">${esc(d.status.replace(/_/g, " "))}</span></td>
          <td>${esc(d.assigned_name || "—")}</td>
          ${admin ? `<td><select data-assign="${d.id}">
            <option value="">Unassigned</option>
            ${drivers.map((u) => `<option value="${u.id}"${
  u.id === d.assigned_to ? " selected" : ""}>${esc(u.name)}</option>`).join("")}
          </select></td>` : ""}</tr>`).join("")}
        </tbody></table></div>`
      : `<div class="empty"><strong>No devices</strong>
         ${admin ? "Add your first adapter above." : "An administrator adds devices."}</div>`}
    </div>`;
  const form = $("#add-device");
  if (form) form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/devices", {
        method: "POST", body: { kind: $("#k").value, identifier: $("#ident").value.trim() },
      });
      toast("Device added");
      devicesView();
    } catch (error) { toast(error.message, true); }
  });
  $("#main").querySelectorAll("[data-assign]").forEach((select) => {
    select.addEventListener("change", async () => {
      const value = select.value;
      try {
        await api(`/devices/${select.dataset.assign}`, {
          method: "PATCH",
          body: value ? { assigned_to: Number(value) } : { clear_assignment: true },
        });
        toast(value ? "Device assigned" : "Device unassigned");
        devicesView();
      } catch (error) { toast(error.message, true); }
    });
  });
}

/* ───────────────────────────────── users ──────────────────────────────── */

async function usersView() {
  const users = await api("/users");
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Add a user</h2></div>
      <form id="add-user" class="form-row">
        <div class="field"><label for="u-name">Name</label><input id="u-name" required></div>
        <div class="field"><label for="u-phone">Mobile number</label>
          <input id="u-phone" type="tel" required placeholder="+971 50 123 4567"></div>
        <div class="field"><label for="u-role">Role</label><select id="u-role">
          <option value="driver">Driver</option>
          <option value="account_manager">Account manager</option>
          <option value="administrator">Administrator</option></select></div>
        <button class="btn primary" type="submit">Add user</button>
      </form>
    </div>
    <div class="card">
      <div class="card-head"><h2>Accounts</h2><span class="muted">${users.length} total</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Mobile</th><th>Role</th><th>Status</th><th></th></tr></thead>
        <tbody>${users.map((u) => `
          <tr><td>${esc(u.name)}</td><td>${esc(u.phone)}</td>
          <td>${esc(ROLE_NAMES[u.role] || u.role)}</td>
          <td><span class="pill ${u.active ? "active" : "cancelled"}">${
  u.active ? "active" : "inactive"}</span></td>
          <td>${u.id === state.user.id ? '<span class="muted">you</span>'
    : `<button class="btn small" data-toggle="${u.id}" data-active="${u.active}">${
      u.active ? "Deactivate" : "Reactivate"}</button>`}</td></tr>`).join("")}
      </tbody></table></div>
    </div>`;
  $("#add-user").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/users", {
        method: "POST",
        body: {
          name: $("#u-name").value.trim(), phone: $("#u-phone").value.trim(),
          role: $("#u-role").value,
        },
      });
      toast("User added");
      usersView();
    } catch (error) { toast(error.message, true); }
  });
  $("#main").querySelectorAll("[data-toggle]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/users/${button.dataset.toggle}`, {
          method: "PATCH", body: { active: button.dataset.active !== "1" },
        });
        toast("Account updated");
        usersView();
      } catch (error) { toast(error.message, true); }
    });
  });
}

/* ──────────────────────────────── settings ────────────────────────────── */

const SETTING_COPY = {
  risk_medium_threshold: ["Medium risk starts at", "Below this the driver sees Low."],
  risk_high_threshold: ["High risk starts at", "Crossing this alerts the driver and logs an alert."],
  risk_critical_threshold: ["Critical risk starts at", "The most severe band."],
  session_gap_tolerance_seconds: ["Gap tolerance (seconds)",
    "A trip with a longer gap is flagged incomplete and excluded from training."],
};

async function settingsView() {
  const settings = await api("/settings");
  $("#main").innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Risk thresholds</h2></div>
      <p class="muted" style="margin:0 0 18px;max-width:62ch">
        These bands decide what every driver sees and when an alert is raised. They change
        how scores are presented; they do not retrain the model.
      </p>
      <form id="settings-form" class="form-row">
        ${Object.entries(settings).map(([key, value]) => {
    const [label, help] = SETTING_COPY[key] || [key, ""];
    const seconds = key.endsWith("_seconds");
    return `<div class="field">
            <label for="s-${key}">${esc(label)}</label>
            <input id="s-${key}" name="${key}" type="number"
              step="${seconds ? 1 : 0.01}" min="${seconds ? 1 : 0.01}"
              ${seconds ? "" : 'max="0.99"'} value="${value}">
            <span class="hint">${esc(help)}</span></div>`;
  }).join("")}
        <button class="btn primary" type="submit">Save thresholds</button>
      </form>
    </div>`;
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = {};
    new FormData(event.target).forEach((value, key) => { values[key] = Number(value); });
    try {
      await api("/settings", { method: "PUT", body: { values } });
      toast("Thresholds saved");
    } catch (error) { toast(error.message, true); }
  });
}

/* ───────────────────────────────── startup ────────────────────────────── */

async function start() {
  state.user = await api("/me");
  $("#signin").hidden = true;
  $("#shell").hidden = false;
  document.body.dataset.view = "app";
  $("#who-name").textContent = state.user.name;
  $("#role-label").textContent = ROLE_NAMES[state.user.role] || state.user.role;
  if (!location.hash || location.hash === "#/") location.hash = `#/${defaultRoute()}`;
  else await route();
}

(async function boot() {
  state.token = localStorage.getItem(STORE);
  if (!state.token) { showSignIn(); return; }
  try { await start(); } catch { showSignIn(); }
})();
