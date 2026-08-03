"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const form = $("#monitorForm");
const state = {
  csrf: "", isAdmin: false, discordAllowed: false, cursor: 0,
  catalog: { licences: [], examinationTypes: [], locations: [], vehicleTypes: [], occasionChoices: [] },
  selectedLocations: [], currentStep: "bankid", users: [], live: null,
  status: null, authoritativeStatus: null, snapshot: null, savedConfig: null,
};

const SCANNING_STATES = new Set([
  "monitor_starting", "monitoring", "match_found", "reserving", "booking", "booked",
]);

const STATUS_FALLBACKS = {
  ready: ["Redo", "Identifiera dig med BankID för att börja."],
  bankid_starting: ["Startar BankID", "En säker BankID-inloggning förbereds."],
  bankid_waiting: ["Väntar på BankID", "Skanna QR-koden eller öppna BankID."],
  bankid_connected: ["BankID anslutet", "Din identitet har verifierats."],
  loading_options: ["Hämtar alternativ", "Dina bokningsalternativ hämtas."],
  ready_to_start: ["Redo att starta", "Välj inställningar och starta bevakningen."],
  monitor_starting: ["Startar bevakning", "Inställningarna kontrolleras på servern."],
  monitoring: ["Bevakning aktiv", "Lediga tider kontrolleras automatiskt."],
  match_found: ["Träff hittad", "En tid som matchar dina val har hittats."],
  reserving: ["Reserverar tid", "Den valda tiden reserveras hos Trafikverket."],
  booking: ["Bokar tid", "Reservationen slutförs med Pay later/faktura."],
  booked: ["Bokning klar", "Tiden är bokad och bekräftad av Trafikverket."],
  stopping: ["Stoppar", "Pågående arbete avslutas säkert."],
  stopped: ["Stoppad", "Bevakningen är stoppad."],
  reconnecting: ["Återansluter", "Anslutningen till serverstatus återställs."],
  error: ["Ett fel uppstod", "Kontrollera felet och försök igen."],
  action_required: ["Åtgärd krävs", "En reservation behöver hanteras innan du fortsätter."],
};

function clientStatus(code, description = "", overrides = {}) {
  const fallback = STATUS_FALLBACKS[code] || [code, description];
  const stoppable = new Set(["bankid_starting", "bankid_waiting", "monitor_starting", "monitoring", "match_found", "reserving", "booking"]);
  return {
    code, label: fallback[0], description: description || fallback[1], updatedAt: Date.now() / 1000,
    canStart: ["ready_to_start", "stopped", "booked"].includes(code),
    canStop: stoppable.has(code),
    canAuthenticate: ["ready", "stopped"].includes(code),
    ...overrides,
  };
}

function statusFromSnapshot(snapshot) {
  if (snapshot?.status?.code) return snapshot.status;
  const legacy = {
    idle: "ready", authentication: "bankid_waiting", authenticated: "bankid_connected",
    starting: "monitor_starting", running: "monitoring",
  }[snapshot?.state] || snapshot?.state || "ready";
  return clientStatus(legacy);
}

function renderStatus(status, authoritative = true) {
  if (authoritative) state.authoritativeStatus = status;
  state.status = status;
  ["#statusBadge", "#statusTitle", "#metricEvent", "#footerStatus"].forEach((selector) => {
    $(selector).textContent = status.label;
  });
  $("#statusDescription").textContent = status.description;
  $("#metricEventTime").textContent = status.description;
  $("#footerDescription").textContent = status.description;
  $("#startButton").disabled = !status.canStart;
  $("#stopButton").disabled = !status.canStop;
  $("#bankidButton").disabled = !status.canAuthenticate;
  $("#statusDot").dataset.status = status.code;
  if ($("#eventDialog").open) {
    $("#dialogTitle").textContent = status.label;
    $("#dialogMessage").textContent = status.description;
  }
  if ($("#bankidDialog").open && status.code.startsWith("bankid_")) {
    $("#bankidStatus").textContent = status.description;
  }
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message; node.classList.add("visible");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.remove("visible"), 2800);
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (state.csrf && options.method && !["GET", "HEAD"].includes(options.method)) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(url, { credentials: "same-origin", ...options, headers });
  if (response.status === 401 && url !== "/api/auth/login") {
    showView("login");
    throw new Error("Sessionen har gått ut.");
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { const value = await response.json(); detail = value.detail || detail; } catch {}
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join(" ");
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function showView(view) {
  $("#homeView").hidden = view !== "home";
  $("#loginView").hidden = view !== "login";
  $("#resetView").hidden = view !== "reset";
  $("#appView").hidden = view !== "app";
  if (view !== "app") state.live?.stop();
}

function showStep(step) {
  state.currentStep = step;
  $$(".wizard-step").forEach((node) => { node.hidden = node.dataset.step !== step; });
  const order = ["bankid", "options", "locations", "schedule", "notifications"];
  const current = step === "loading" ? 1 : Math.max(0, order.indexOf(step));
  $$("[data-progress]").forEach((node, index) => {
    node.classList.toggle("active", index === current);
    node.classList.toggle("done", index < current);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setOptions(select, values, placeholder) {
  const current = Number(select.value);
  select.replaceChildren(new Option(placeholder, ""));
  values.forEach((item) => select.add(new Option(item.name, item.id)));
  select.disabled = !values.length;
  if (values.some((item) => item.id === current)) select.value = String(current);
  else if (values.length === 1) select.value = String(values[0].id);
  select.closest("label").hidden = !values.length;
}

function applyCatalog(data) {
  state.catalog = {
    licences: data.licences || [], examinationTypes: data.examinationTypes || [],
    locations: data.locations || [], vehicleTypes: data.vehicleTypes || [],
    occasionChoices: data.occasionChoices || [],
  };
  setOptions(form.elements.licence_id, state.catalog.licences, "Välj behörighet");
  setOptions(form.elements.examination_type_id, state.catalog.examinationTypes, "Välj provtyp");
  setOptions(form.elements.vehicle_type_id, state.catalog.vehicleTypes, "Välj växellåda / fordon");
  setOptions(form.elements.occasion_choice_id, state.catalog.occasionChoices, "Välj hyrbilsalternativ");
  applySavedConfig();
  renderLocations();
}

function applySavedConfig() {
  const config = state.savedConfig;
  if (!config) return;
  ["licence_id", "examination_type_id", "vehicle_type_id", "occasion_choice_id"].forEach((name) => {
    if (config[name] != null && [...form.elements[name].options].some((option) => Number(option.value) === Number(config[name]))) {
      form.elements[name].value = String(config[name]);
    }
  });
  ["date_from", "date_to", "earliest_time", "latest_time"].forEach((name) => {
    if (config[name]) form.elements[name].value = config[name];
  });
  if (Array.isArray(config.allowed_weekdays)) {
    $$('input[name="weekday"]').forEach((node) => { node.checked = config.allowed_weekdays.includes(Number(node.value)); });
  }
  if (config.location_id) {
    state.selectedLocations = [config.location_id, ...(config.nearby_location_ids || [])]
      .map(Number).filter((id, index, values) => id && values.indexOf(id) === index).slice(0, 4);
  }
  const mode = config.auto_book ? "book" : "notify";
  const radio = form.querySelector(`input[name="action_mode"][value="${mode}"]`);
  if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change")); }
}

function itemName(values, id) {
  return values.find((item) => Number(item.id) === Number(id))?.name || (id ? String(id) : "–");
}

function renderMonitoringView(snapshot) {
  const status = statusFromSnapshot(snapshot);
  const scanning = SCANNING_STATES.has(status.code);
  $("#wizardProgress").hidden = scanning;
  form.hidden = scanning;
  $("#scanningView").hidden = !scanning;
  if (!scanning) {
    if (snapshot.resumePending) showStep("bankid");
    return;
  }
  const config = state.savedConfig || {};
  const licence = itemName(state.catalog.licences, config.licence_id);
  $("#scanningTitle").textContent = status.code === "booked" ? "Bokningen är klar" : `Söker efter lediga ${licence === "–" ? "" : `${licence}-`}tider`;
  $("#scanningDescription").textContent = status.description;
  $("#scanningLicence").textContent = licence;
  const dates = [config.date_from, config.date_to].filter(Boolean).join(" – ") || "Alla datum";
  const times = [config.earliest_time, config.latest_time].filter(Boolean).join(" – ") || "Alla tider";
  $("#scanningSchedule").textContent = `${dates}, ${times}`;
  const locationIds = [config.location_id, ...(config.nearby_location_ids || [])].filter(Boolean);
  $("#scanningLocations").textContent = locationIds.map((id) => itemName(state.catalog.locations, id)).join(", ") || "–";
  $("#scanningMode").textContent = config.auto_book ? "Boka åt mig" : "Notifiering";
  $("#scanStopButton").hidden = status.code === "booked";
  $("#scanStopButton").disabled = !status.canStop;
}

function renderLocations() {
  const query = $("#locationSearch").value.trim().toLocaleLowerCase("sv");
  const values = state.catalog.locations.filter((item) => item.name.toLocaleLowerCase("sv").includes(query));
  const root = $("#locationChoices"); root.replaceChildren();
  values.forEach((item) => {
    const label = document.createElement("label"); label.className = `location-choice${state.selectedLocations.includes(item.id) ? " selected" : ""}`;
    const input = document.createElement("input"); input.type = "checkbox"; input.checked = state.selectedLocations.includes(item.id); input.value = item.id;
    input.addEventListener("change", () => toggleLocation(item.id));
    label.append(input, document.createTextNode(item.name)); root.append(label);
  });
  $("#locationCount").textContent = `${state.selectedLocations.length} av 4 valda`;
  form.elements.location_id.value = state.selectedLocations[0] || "";
  const nearby = form.elements.nearby_location_ids; nearby.replaceChildren();
  state.selectedLocations.slice(1).forEach((id) => { const item = state.catalog.locations.find((x) => x.id === id); if (item) nearby.add(new Option(item.name, id, true, true)); });
}

function toggleLocation(id) {
  if (state.selectedLocations.includes(id)) state.selectedLocations = state.selectedLocations.filter((value) => value !== id);
  else if (state.selectedLocations.length >= 4) return toast("Du kan välja högst fyra orter.");
  else state.selectedLocations.push(id);
  renderLocations();
}

async function loadLicenceOptions() {
  const licenceId = Number(form.elements.licence_id.value);
  if (!licenceId) return;
  showStep("loading"); renderStatus(clientStatus("loading_options"), false);
  try {
    const data = await api("/api/catalog/refresh", { method: "POST", body: JSON.stringify({ ssn: "", licence_id: licenceId }) });
    applyCatalog(data); showStep("options");
  } catch (error) { showStep("options"); toast(error.message); }
}

function updateStatus(snapshot) {
  state.snapshot = snapshot;
  renderStatus(statusFromSnapshot(snapshot));
  const bankId = snapshot.bankId || {};
  $("#bankidSummary").textContent = bankId.authenticated ? "BankID anslutet" : bankId.state === "pending" ? "Väntar på BankID" : "Inte anslutet";
  $("#bankidStatus").textContent = ({ starting: "Förbereder säker inloggning…", pending: "Skanna QR-koden eller öppna BankID.", complete: "BankID-inloggningen är klar.", error: bankId.error || "BankID kunde inte anslutas." })[bankId.state] || "Förbereder säker inloggning…";
  $("#bankidQr").hidden = bankId.state !== "pending";
  $("#bankidOpen").hidden = !bankId.canOpenOnDevice;
  $("#bankidRetry").hidden = bankId.state !== "error";
  if (bankId.state === "pending") $("#bankidQr").src = `/api/bankid/qr.svg?v=${bankId.qrVersion || Date.now()}`;
  if (bankId.state === "pending" && !$("#bankidDialog").open) $("#bankidDialog").showModal();
  if ($("#bankidDialog").open && ["idle", "cancelled"].includes(bankId.state)) $("#bankidDialog").close();
  if (bankId.authenticated && ["bankid", "loading"].includes(state.currentStep)) loadInitialCatalog();
  if (snapshot.events) renderEvents(snapshot.events);
  renderMonitoringView(snapshot);
}

async function loadInitialCatalog() {
  if (loadInitialCatalog.running) return; loadInitialCatalog.running = true; showStep("loading"); renderStatus(clientStatus("loading_options"), false);
  try {
    let data;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try { data = await api("/api/catalog"); break; } catch { await new Promise((resolve) => setTimeout(resolve, 1000)); }
    }
    if (!data) data = await api("/api/catalog/refresh", { method: "POST", body: JSON.stringify({ ssn: "", licence_id: 0 }) });
    applyCatalog(data);
    const savedLicenceId = Number(state.savedConfig?.licence_id || form.elements.licence_id.value);
    if (savedLicenceId && (!state.catalog.examinationTypes.length || !state.catalog.locations.length)) {
      data = await api("/api/catalog/refresh", { method: "POST", body: JSON.stringify({ ssn: "", licence_id: savedLicenceId }) });
      applyCatalog(data);
    }
    $("#bankidDialog").open && $("#bankidDialog").close(); showStep("options");
  } catch (error) { showStep("bankid"); toast(error.message); }
  finally { loadInitialCatalog.running = false; }
}

function renderEvents(events) {
  if (!state.isAdmin || !events.length) return;
  const root = $("#activityList"); if (root.querySelector(".empty-state")) root.replaceChildren();
  events.forEach((event) => {
    state.cursor = Math.max(state.cursor, Number(event.id) || 0);
    const item = document.createElement("article"); item.className = "activity-item";
    const title = document.createElement("strong"); title.textContent = event.message;
    const time = document.createElement("small"); time.textContent = new Date(event.timestamp * 1000).toLocaleString("sv-SE");
    item.append(title, time); root.prepend(item);
  });
}

function startLive() {
  state.live?.stop();
  state.live = new LiveTransport({
    EventSource: window.EventSource, AbortController: window.AbortController, TextDecoder: window.TextDecoder,
    fetch: window.fetch.bind(window),
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
    isOnline: () => navigator.onLine,
    getCursor: () => state.cursor, streamUrl: state.isAdmin ? "/api/live/stream" : "/api/status/stream",
    snapshotUrl: state.isAdmin ? "/api/live" : "/api/status",
    onSnapshot: updateStatus, onUnauthorized: () => showView("login"),
    onState: (value) => {
      const node = $("#connectionBadge");
      node.textContent = value === "live" ? "Live" : value === "offline" ? "Offline" : "Återansluter";
      node.classList.toggle("reconnecting", value !== "live");
      if (value === "live" && state.authoritativeStatus) renderStatus(state.authoritativeStatus, false);
      else if (value !== "live") renderStatus(clientStatus("reconnecting", value === "offline" ? "Nätverket är offline. Serverstatus återansluts automatiskt." : "Anslutningen till serverstatus återställs."), false);
    },
  });
  state.live.start();
}

async function bootstrap() {
  const data = await api("/api/bootstrap");
  state.csrf = data.csrfToken; state.isAdmin = data.isAdmin; state.discordAllowed = data.discordAllowed; state.savedConfig = data.config || null;
  $("#adminTopNav").hidden = !state.isAdmin; $("#activity").hidden = !state.isAdmin;
  $("#discordPanel").hidden = !state.discordAllowed; $("#discordDefault").checked = !!data.discordDefaultForNewUsers;
  $("#accountEmail").textContent = data.account?.email || ""; $("#modeBadge").textContent = data.mode.toUpperCase();
  $("#logoutButton").hidden = data.mode !== "server"; $("#exitButton").hidden = data.mode === "server";
  showView("app"); applySavedConfig(); updateStatus(data); if (data.catalogUpdatedAt && !data.bankId?.authenticated) { try { applyCatalog(await api("/api/catalog")); renderMonitoringView(data); } catch {} }
  startLive();
}

function monitorPayload() {
  const weekdays = $$('input[name="weekday"]:checked').map((node) => Number(node.value));
  return {
    name: "Min provtidsbevakning", ssn: "", licence_id: Number(form.elements.licence_id.value),
    examination_type_id: Number(form.elements.examination_type_id.value), location_id: state.selectedLocations[0],
    nearby_location_ids: state.selectedLocations.slice(1), vehicle_type_id: Number(form.elements.vehicle_type_id.value || 1),
    tachograph_type_id: 1, occasion_choice_id: Number(form.elements.occasion_choice_id.value || 1), language_id: 13,
    date_from: form.elements.date_from.value || null, date_to: form.elements.date_to.value || null,
    earliest_time: form.elements.earliest_time.value || null, latest_time: form.elements.latest_time.value || null,
    allowed_weekdays: weekdays, discord_webhook_url: state.discordAllowed ? form.elements.discord_webhook_url.value.trim() : "",
    auto_book: form.elements.action_mode.value === "book", timezone: "Europe/Stockholm",
  };
}

function validateStep(step) {
  const afterOptions = ["locations", "schedule", "notifications"].includes(step);
  if (afterOptions && !Number(form.elements.licence_id.value)) return "Välj en behörighet.";
  if (afterOptions && !state.catalog.examinationTypes.length) return "Hämta bokningsalternativen för vald behörighet igen.";
  if (afterOptions && !Number(form.elements.examination_type_id.value)) return "Välj en provtyp.";
  if (["schedule", "notifications"].includes(step) && !state.selectedLocations.length) return "Välj minst en provort.";
  if (step === "notifications" && !form.elements.date_from.value) return "Välj ett startdatum.";
  if (step === "notifications" && form.elements.date_from.value < form.elements.date_from.min) return "Från datum kan inte vara tidigare än idag.";
  return "";
}

async function loadUsers() {
  const query = new URLSearchParams({ q: $("#userSearch").value, status: $("#userStatusFilter").value, role: $("#userRoleFilter").value });
  const data = await api(`/api/admin/users?${query}`); state.users = data.users; $("#userResultCount").textContent = `${data.total} användare`;
  const root = $("#userList"); root.replaceChildren(); data.users.forEach((user) => {
    const row = document.createElement("article"); row.className = "user-row";
    const identity = document.createElement("div"); const email = document.createElement("strong"); email.textContent = user.email; const name = document.createElement("small"); name.textContent = user.displayName || "Inget visningsnamn"; identity.append(email, name); row.append(identity);
    [user.status, user.role, user.discordAllowed ? "Discord" : "Ingen Discord"].forEach((text) => { const span = document.createElement("span"); span.textContent = text; row.append(span); });
    const button = document.createElement("button"); button.textContent = "Redigera"; button.addEventListener("click", () => openUser(user)); row.append(button); root.append(row);
  });
}

function openUser(user = null) {
  const dialog = $("#userDialog"), target = $("#userForm"); target.reset();
  target.elements.id.value = user?.id || ""; target.elements.email.value = user?.email || ""; target.elements.display_name.value = user?.displayName || "";
  $("#existingUserFields").hidden = !user; $("#resetUserPassword").hidden = !user; $("#deleteUser").hidden = !user;
  if (user) { target.elements.status.value = user.status; target.elements.role.value = user.role; target.elements.paid.checked = user.paid; target.elements.discord_allowed.checked = user.discordAllowed; }
  $("#userDialogTitle").textContent = user ? "Redigera användare" : "Skapa användare"; dialog.showModal();
}

$$('[data-open-login]').forEach((node) => node.addEventListener("click", () => showView("login")));
$$('[data-open-home]').forEach((node) => node.addEventListener("click", () => showView("home")));
$("#showRegister").addEventListener("click", () => { $("#loginForm").hidden = true; $("#showRegister").hidden = true; $("#registerForm").hidden = false; });
$("#showLogin").addEventListener("click", () => { $("#loginForm").hidden = false; $("#showRegister").hidden = false; $("#registerForm").hidden = true; });
$("#loginForm").addEventListener("submit", async (event) => { event.preventDefault(); $("#loginError").textContent = ""; try { await api("/api/auth/login", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) }); await bootstrap(); } catch (error) { $("#loginError").textContent = error.message; } });
$("#registerForm").addEventListener("submit", async (event) => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget)); if (values.password !== values.password_confirm) return $("#registerMessage").textContent = "Lösenorden matchar inte."; try { await api("/api/auth/register", { method: "POST", body: JSON.stringify({ email: values.email, password: values.password }) }); $("#registerMessage").textContent = "Kontot väntar på betalning eller administratörsgodkännande."; } catch (error) { $("#registerMessage").textContent = error.message; } });
$("#resetPasswordForm").addEventListener("submit", async (event) => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget)); if (values.password !== values.password_confirm) return $("#resetMessage").textContent = "Lösenorden matchar inte."; try { await api("/api/auth/reset-password", { method: "POST", body: JSON.stringify({ token: new URLSearchParams(location.search).get("reset"), password: values.password }) }); $("#resetMessage").textContent = "Lösenordet är ändrat."; } catch (error) { $("#resetMessage").textContent = error.message; } });
$("#logoutButton").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST", body: "{}" }); state.csrf = ""; showView("home"); });
$("#exitButton").addEventListener("click", () => api("/api/app/exit", { method: "POST", body: "{}" }).catch((error) => toast(error.message)));

$("#bankidButton").addEventListener("click", async () => { renderStatus(clientStatus("bankid_starting"), false); try { const result = await api("/api/bankid/start", { method: "POST", body: "{}" }); updateStatus(result); } catch (error) { renderStatus(clientStatus("error", error.message, { canAuthenticate: true })); toast(error.message); } });
$("#bankidCancel").addEventListener("click", async () => { renderStatus(clientStatus("stopping"), false); const result = await api("/api/bankid/cancel", { method: "POST", body: "{}" }); updateStatus(result); $("#bankidDialog").close(); });
$("#bankidClose").addEventListener("click", () => $("#bankidDialog").close());
$("#bankidRetry").addEventListener("click", async () => { renderStatus(clientStatus("bankid_starting"), false); try { updateStatus(await api("/api/bankid/retry", { method: "POST", body: "{}" })); } catch (error) { renderStatus(clientStatus("error", error.message, { canAuthenticate: true })); toast(error.message); } });
$("#bankidFallback").addEventListener("click", () => api("/api/bankid/browser-fallback", { method: "POST", body: "{}" }).catch((error) => toast(error.message)));
form.elements.licence_id.addEventListener("change", loadLicenceOptions); $("#locationSearch").addEventListener("input", renderLocations);
function localDateValue() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}
function updateDateLimits() {
  const today = localDateValue(), from = form.elements.date_from, to = form.elements.date_to;
  from.min = today;
  if (!from.value || from.value < today) from.value = today;
  to.min = from.value;
  if (to.value && to.value < from.value) to.value = from.value;
}
updateDateLimits();
form.elements.date_from.addEventListener("change", updateDateLimits);
$$('input[name="action_mode"]').forEach((input) => input.addEventListener("change", () => {
  $$(".mode-card").forEach((card) => card.classList.toggle("selected", card.contains(input) && input.checked));
  const books = form.elements.action_mode.value === "book";
  $("#metricMode").textContent = books ? "Boka åt mig" : "Notifiering";
  $("#metricModeDescription").textContent = books ? "Automatisk bokning" : "Du bokar själv";
}));
$$('[data-next]').forEach((button) => button.addEventListener("click", () => { const error = validateStep(button.dataset.next); error ? toast(error) : showStep(button.dataset.next); }));
$$('[data-back]').forEach((button) => button.addEventListener("click", () => showStep(button.dataset.back)));
form.addEventListener("submit", async (event) => {
  event.preventDefault(); const error = validateStep("notifications"); if (error) return toast(error);
  const payload = monitorPayload(); state.savedConfig = payload;
  const starting = { ...state.snapshot, status: clientStatus("monitor_starting", "", { canStop: true }) };
  updateStatus(starting);
  try { updateStatus(await api("/api/monitor/start", { method: "POST", body: JSON.stringify(payload) })); toast("Bevakningen startar."); }
  catch (err) { const failed = { ...state.snapshot, status: clientStatus("error", err.message, { canStart: true }) }; updateStatus(failed); toast(err.message); }
});
async function stopMonitoring() {
  renderStatus(clientStatus("stopping"), false);
  try { updateStatus(await api("/api/monitor/stop", { method: "POST", body: "{}" })); toast("Bevakningen är stoppad."); }
  catch (error) { renderStatus(clientStatus("error", error.message)); toast(error.message); }
}
$("#stopButton").addEventListener("click", stopMonitoring);
$("#scanStopButton").addEventListener("click", stopMonitoring);
$("#discordButton").addEventListener("click", async () => { const url = form.elements.discord_webhook_url.value.trim(); try { await api("/api/discord/test", { method: "POST", body: JSON.stringify({ name: "Min provtidsbevakning", discord_webhook_url: url }) }); toast("Discord-test skickat."); } catch (error) { toast(error.message); } });
$("#clearActivity").addEventListener("click", () => $("#activityList").innerHTML = '<div class="empty-state"><strong>Inga händelser ännu</strong></div>');

$$('[data-admin-view]').forEach((button) => button.addEventListener("click", async () => { $$('[data-admin-view]').forEach((node) => node.classList.toggle("active", node === button)); const users = button.dataset.adminView === "users"; $("#monitorView").hidden = users; $("#users").hidden = !users; if (users) await loadUsers(); }));
$("#createUser").addEventListener("click", () => openUser()); $("#refreshUsers").addEventListener("click", loadUsers); $("#userSearchForm").addEventListener("submit", (event) => { event.preventDefault(); loadUsers(); });
$("#userForm").addEventListener("submit", async (event) => { event.preventDefault(); const target = event.currentTarget, id = target.elements.id.value; const body = id ? { email: target.elements.email.value, display_name: target.elements.display_name.value, status: target.elements.status.value, role: target.elements.role.value, paid: target.elements.paid.checked, discord_allowed: target.elements.discord_allowed.checked } : { email: target.elements.email.value, display_name: target.elements.display_name.value }; try { const result = await api(id ? `/api/admin/users/${encodeURIComponent(id)}` : "/api/admin/users", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }); $("#userDialog").close(); if (!id && result.resetToken) showReset(result.resetToken); await loadUsers(); } catch (error) { $("#userFormError").textContent = error.message; } });
$("#resetUserPassword").addEventListener("click", async () => { const id = $("#userForm").elements.id.value; try { const result = await api(`/api/admin/users/${encodeURIComponent(id)}/password-reset`, { method: "POST", body: "{}" }); showReset(result.resetToken); } catch (error) { toast(error.message); } });
$("#deleteUser").addEventListener("click", async () => { const id = $("#userForm").elements.id.value; if (!confirm("Radera användaren permanent?")) return; try { await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: "DELETE", body: "{}" }); $("#userDialog").close(); await loadUsers(); } catch (error) { toast(error.message); } });
function showReset(token) { $("#resetLink").value = `${location.origin}/?reset=${encodeURIComponent(token)}`; $("#resetLinkDialog").showModal(); }
$("#copyResetLink").addEventListener("click", async () => { await navigator.clipboard.writeText($("#resetLink").value); toast("Länken är kopierad."); });
$$('[data-close-user-dialog]').forEach((node) => node.addEventListener("click", () => $("#userDialog").close())); $$('[data-close-reset-dialog]').forEach((node) => node.addEventListener("click", () => $("#resetLinkDialog").close()));
$("#saveDiscordPolicy").addEventListener("click", async () => { try { await api("/api/admin/discord-policy", { method: "PUT", body: JSON.stringify({ enabled_for_new_users: $("#discordDefault").checked, apply_to_existing_users: $("#discordApplyExisting").checked }) }); $("#discordApplyExisting").checked = false; toast("Discord-policyn är sparad."); await loadUsers(); } catch (error) { toast(error.message); } });
$("#dialogClose").addEventListener("click", () => $("#eventDialog").close()); $("#dialogOk").addEventListener("click", () => $("#eventDialog").close());
window.addEventListener("online", () => state.live?.resume()); window.addEventListener("offline", () => state.live?.offline());

const resetToken = new URLSearchParams(location.search).get("reset");
if (resetToken) showView("reset"); else bootstrap().catch(() => showView("home"));
