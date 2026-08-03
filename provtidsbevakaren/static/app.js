"use strict";

const state = {
  mode: "local",
  csrf: "",
  lastEvent: 0,
  runtime: "idle",
  catalog: {
    licences: [],
    examinationTypes: [],
    locations: [],
    vehicleTypes: [],
    occasionChoices: [],
  },
  qrVersion: 0,
  authenticated: false,
  browserFallbackAvailable: true,
  isAdmin: false,
  currentAccount: null,
  adminUsers: [],
  savedConfig: null,
  catalogAttempted: false,
  nearbySelection: new Set(),
};
const $ = (selector) => document.querySelector(selector);
const form = $("#monitorForm");

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  clearTimeout(node.timer);
  node.timer = setTimeout(() => node.classList.remove("visible"), 3200);
}
function setConnectionState(connectionState) {
  const badge = $("#connectionBadge");
  const labels = {
    live: "Live",
    reconnecting: "Återansluter",
    offline: "Offline",
  };
  badge.className = `connection-badge ${connectionState}`;
  badge.textContent = labels[connectionState] || labels.reconnecting;
}
async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (state.csrf && options.method && options.method !== "GET")
    headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}
function integer(name) {
  return Number(form.elements[name].value);
}
function nullable(name) {
  return form.elements[name].value || null;
}
function localDateValue(now = new Date()) {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function enforceDateMinimum() {
  const today = localDateValue();
  const from = form.elements.date_from;
  const to = form.elements.date_to;
  from.min = today;
  to.min = today;
  if (!from.value || from.value < today) {
    const changed = Boolean(from.value && from.value < today);
    from.value = today;
    if (changed) toast(`Från datum flyttades till idag (${today})`);
  }
  if (to.value && to.value < today) to.value = "";
}
function ensureSavedOption(select, value, label) {
  if (!value) return;
  const id = String(value);
  if (![...select.options].some((option) => option.value === id)) {
    select.add(new Option(`${label} (ID ${id})`, id));
  }
  select.disabled = false;
  select.value = id;
}
function setOptions(select, items, placeholder, selectedValue = "") {
  select.textContent = "";
  select.add(new Option(placeholder, ""));
  for (const item of items) {
    const label = item.description ? `${item.name} — ${item.description}` : item.name;
    select.add(new Option(label, String(item.id)));
  }
  select.disabled = items.length === 0;
  if (
    selectedValue
      && [...select.options].some((option) => option.value === String(selectedValue))
  ) {
    select.value = String(selectedValue);
  } else if (items.length === 1) {
    select.value = String(items[0].id);
  }
}
function filterLocations(query = "") {
  const wanted = query.trim().toLocaleLowerCase("sv-SE");
  const locations = state.catalog.locations.filter((item) =>
    item.name.toLocaleLowerCase("sv-SE").includes(wanted),
  );
  const main = form.elements.location_id;
  const nearby = form.elements.nearby_location_ids;
  const selectedMain = main.value || state.savedConfig?.location_id || "";
  const selectedNearby = new Set(
    state.nearbySelection,
  );
  setOptions(main, locations, "Välj en provort", selectedMain);
  nearby.textContent = "";
  for (const item of locations) {
    const option = new Option(item.name, String(item.id));
    option.selected = selectedNearby.has(option.value);
    nearby.add(option);
  }
  nearby.disabled = locations.length === 0;
}
function applyCatalog(data) {
  state.catalog = data;
  setOptions(
    form.elements.licence_id,
    data.licences || [],
    "Välj en behörighet",
    form.elements.licence_id.value || state.savedConfig?.licence_id,
  );
  setOptions(
    form.elements.examination_type_id,
    data.examinationTypes || [],
    "Välj en provtyp",
    state.savedConfig?.examination_type_id,
  );
  setOptions(
    form.elements.vehicle_type_id,
    data.vehicleTypes || [],
    "Välj ett tillgängligt alternativ",
    state.savedConfig?.vehicle_type_id,
  );
  setOptions(
    form.elements.occasion_choice_id,
    data.occasionChoices || [],
    "Välj ett tillgängligt alternativ",
    state.savedConfig?.occasion_choice_id,
  );
  const relatedMissing = Boolean(
    (data.examinationTypes || []).length
      && (!(data.vehicleTypes || []).length || !(data.occasionChoices || []).length),
  );
  $("#manualFallback").hidden = !relatedMissing;
  filterLocations($("#locationSearch").value);
}
async function refreshCatalog(licenceId = 0) {
  const ssn = form.elements.ssn.value.trim();
  const data = await api("/api/catalog/refresh", {
    method: "POST",
    body: JSON.stringify({ ssn, licence_id: Number(licenceId) || 0 }),
  });
  applyCatalog(data);
  if (data.locations.length) toast(`${data.locations.length} provorter hämtade`);
  return data;
}
function showIdentityFallback(error) {
  const message = String(error?.message || error || "");
  if (message.toLocaleLowerCase("sv-SE").includes("personnum")) {
    $("#identityFallback").hidden = false;
  }
}
async function initializeCatalog() {
  const licenceCatalog = await refreshCatalog(0);
  const licenceIds = new Set((licenceCatalog.licences || []).map((item) => String(item.id)));
  let selected = String(
    form.elements.licence_id.value || state.savedConfig?.licence_id || "",
  );
  if (!licenceIds.has(selected)) selected = String(licenceCatalog.licences?.[0]?.id || "");
  if (selected) {
    form.elements.licence_id.value = selected;
    await refreshCatalog(Number(selected));
  }
}
function collectConfig() {
  const mode = form.elements.booking_mode.value;
  return {
    name: form.elements.name.value.trim(),
    ssn: form.elements.ssn.value.trim(),
    licence_id: integer("licence_id"),
    examination_type_id: integer("examination_type_id"),
    location_id: integer("location_id"),
    nearby_location_ids: [...state.nearbySelection].map(Number),
    vehicle_type_id: integer("vehicle_type_id"),
    tachograph_type_id: integer("tachograph_type_id"),
    occasion_choice_id: integer("occasion_choice_id"),
    language_id: integer("language_id"),
    date_from: nullable("date_from"),
    date_to: nullable("date_to"),
    earliest_time: nullable("earliest_time"),
    latest_time: nullable("latest_time"),
    allowed_weekdays: [
      ...document.querySelectorAll('input[name="weekday"]:checked'),
    ].map((node) => Number(node.value)),
    poll_interval_seconds: integer("poll_interval_seconds"),
    discord_webhook_url: form.elements.discord_webhook_url.value.trim(),
    auto_reserve: mode === "reserve",
    auto_book: mode === "book",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Stockholm",
  };
}
function fillConfig(config) {
  if (!config) return;
  state.savedConfig = config;
  state.nearbySelection = new Set((config.nearby_location_ids || []).map(String));
  for (const [key, value] of Object.entries(config)) {
    if (
      [
        "auto_reserve",
        "auto_book",
        "allowed_weekdays",
        "nearby_location_ids",
        "licence_id",
        "examination_type_id",
        "location_id",
        "timezone",
      ].includes(key)
    )
      continue;
    const input = form.elements[key];
    if (input && value !== null) input.value = value;
  }
  ensureSavedOption(form.elements.licence_id, config.licence_id, "Saved licence");
  ensureSavedOption(
    form.elements.examination_type_id,
    config.examination_type_id,
    "Saved examination type",
  );
  ensureSavedOption(form.elements.location_id, config.location_id, "Saved location");
  form.elements.booking_mode.value = config.auto_book
    ? "book"
    : config.auto_reserve
      ? "reserve"
      : "notify";
  const days = new Set(config.allowed_weekdays || []);
  document
    .querySelectorAll('input[name="weekday"]')
    .forEach((node) => (node.checked = days.has(Number(node.value))));
  updateMetrics();
}
function updateMetrics() {
  const mode = form.elements.booking_mode.value;
  $("#metricMode").textContent = {
    notify: "Endast notifiering",
    reserve: "Automatisk reservation",
    book: "Automatisk bokning",
  }[mode];
  $("#metricInterval").textContent =
    `${form.elements.poll_interval_seconds.value || 60} sek`;
}
function setRuntime(runtime) {
  state.runtime = runtime;
  const active = [
    "starting",
    "running",
    "authentication",
    "action_required",
    "stopping",
  ].includes(runtime);
  $("#startButton").disabled = active;
  $("#stopButton").disabled = !active;
  const labels = {
    idle: [
      "Redo att starta",
      "Fyll i dina uppgifter och starta bevakningen när du är klar.",
    ],
    starting: [
      "Bevakningen startar",
      "Ansluter och kontrollerar dina inställningar.",
    ],
    running: [
      "Bevakning pågår",
      "Nya tider kontrolleras automatiskt i bakgrunden.",
    ],
    authentication: [
      "Inloggning krävs",
      "Slutför BankID-inloggningen i den säkra webbläsaren.",
    ],
    action_required: [
      "Åtgärd krävs",
      "En reservation väntar på att slutföras.",
    ],
    stopping: ["Bevakningen stoppas", "Ett pågående anrop avslutas säkert."],
    error: [
      "Bevakningen stoppades",
      "Kontrollera den senaste felhändelsen nedan.",
    ],
  };
  const [title, description] = labels[runtime] || labels.idle;
  $("#statusTitle").textContent = title;
  $("#statusDescription").textContent = description;
  $("#footerStatus").textContent = title;
  $("#statusDot").className =
    runtime === "error" ? "error" : active ? "active" : "";
}
function updateBankId(bankId) {
  if (!bankId) return;
  state.authenticated = Boolean(bankId.authenticated);
  $("#bankidSummary").textContent = state.authenticated
    ? "Anslutet"
    : bankId.state === "pending"
      ? "Väntar på BankID"
      : "Inte anslutet";
  $("#bankidButton").textContent = state.authenticated
    ? "Uppdatera bokningsalternativ"
    : "Anslut Mobilt BankID";
  const dialog = $("#bankidDialog");
  if (["starting", "pending", "error"].includes(bankId.state)) {
    if (!dialog.open) dialog.showModal();
    const messages = {
      starting: "Förbereder säker inloggning…",
      pending: "Skanna den roterande QR-koden med Mobilt BankID.",
      error: bankId.error || "Den integrerade inloggningen kunde inte fortsätta.",
    };
    $("#bankidStatus").textContent = messages[bankId.state];
    $("#bankidQr").hidden = bankId.state !== "pending";
    $("#bankidOpen").hidden = !bankId.canOpenOnDevice;
    $("#bankidFallback").hidden =
      bankId.state !== "error" || !state.browserFallbackAvailable;
    $("#bankidRetry").hidden = bankId.state !== "error";
    if (bankId.qrVersion && bankId.qrVersion !== state.qrVersion) {
      state.qrVersion = bankId.qrVersion;
      $("#bankidQr").src = `/api/bankid/qr.svg?v=${bankId.qrVersion}`;
    }
    const remaining = Math.max(0, Math.ceil((bankId.expiresAt * 1000 - Date.now()) / 1000));
    $("#bankidCountdown").textContent = remaining ? `Löper ut om ${remaining} sekunder` : "";
  } else if (bankId.state === "complete") {
    if (dialog.open) dialog.close();
    if (!state.catalog.locations.length && !state.catalogAttempted) {
      state.catalogAttempted = true;
      initializeCatalog().catch((error) => {
        showIdentityFallback(error);
        $("#manualFallback").hidden = false;
        toast(error.message);
        $("#bankidSummary").textContent = "Anslutet — alternativen kunde inte hämtas";
      });
    }
  } else if (bankId.state === "cancelled" && dialog.open) dialog.close();
}
function eventSymbol(type) {
  return (
    {
      error: "!",
      warning: "!",
      reserved: "R",
      booked: "✓",
      booking_error: "!",
      authentication: "↗",
      browser: "↗",
      stopped: "■",
      status: "•",
    }[type] || "•"
  );
}
function addEvent(event) {
  const list = $("#activityList");
  if (list.querySelector(".empty-state")) list.textContent = "";
  const row = document.createElement("article");
  row.className = `event-row ${event.type}`;
  const symbol = document.createElement("span");
  symbol.className = "event-symbol";
  symbol.textContent = eventSymbol(event.type);
  const copy = document.createElement("div");
  copy.className = "event-copy";
  const title = document.createElement("strong");
  title.textContent =
    {
      error: "Fel",
      warning: "Varning",
      reserved: "Tid reserverad",
      booked: "Bokning klar",
      booking_error: "Bokningen behöver slutföras",
      authentication: "BankID-inloggning",
      browser: "Bokningssida öppnad",
      stopped: "Stoppad",
      status: "Status",
    }[event.type] || "Händelse";
  const message = document.createElement("p");
  message.textContent = event.message;
  copy.append(title, message);
  const time = document.createElement("time");
  time.dateTime = new Date(event.timestamp * 1000).toISOString();
  time.textContent = new Date(event.timestamp * 1000).toLocaleTimeString(
    "sv-SE",
    { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  );
  row.append(symbol, copy, time);
  list.prepend(row);
  $("#metricEvent").textContent = title.textContent;
  $("#metricEventTime").textContent = time.textContent;
  const important = [
    "reserved",
    "booked",
    "booking_error",
    "error",
    "authentication",
  ].includes(event.type);
  if (important)
    showDialog(title.textContent, event.message, event.data?.url || "", event.type);
}
function showDialog(title, message, url = "", eventType = "") {
  $("#dialogTitle").textContent = title;
  $("#dialogMessage").textContent = message;
  $("#dialogIcon").textContent =
    title.includes("Fel") || title.includes("slutföras") ? "!" : "✓";
  const link = $("#browserLink");
  link.hidden = !url;
  link.href = url || "#";
  $("#reservationBook").hidden = !["reserved", "booking_error"].includes(eventType);
  $("#eventDialog").showModal();
}
function applySnapshot(data) {
  setRuntime(data.state);
  updateBankId(data.bankId);
  for (const event of data.events) {
    state.lastEvent = Math.max(state.lastEvent, event.id);
    addEvent(event);
  }
}
const liveTransport = new window.LiveTransport({
  EventSource: window.EventSource || null,
  AbortController: window.AbortController,
  TextDecoder: window.TextDecoder,
  fetch: window.fetch.bind(window),
  setTimeout: window.setTimeout.bind(window),
  clearTimeout: window.clearTimeout.bind(window),
  isOnline: () => navigator.onLine,
  getCursor: () => state.lastEvent,
  onState: setConnectionState,
  onSnapshot: applySnapshot,
  onUnauthorized: showHome,
});
function stopEventStream() {
  liveTransport.stop();
}
function connectEventStream() {
  liveTransport.start();
}
function showOnly(view) {
  for (const selector of ["#homeView", "#loginView", "#resetView", "#appView"])
    $(selector).hidden = selector !== view;
}
function showHome() {
  stopEventStream();
  state.csrf = "";
  showOnly("#homeView");
}
function showLogin() {
  stopEventStream();
  showOnly("#loginView");
  $("#loginForm").hidden = false;
  $("#showRegister").hidden = false;
  $("#registerForm").hidden = true;
  $("#loginError").textContent = "";
}
function showApp() {
  showOnly("#appView");
}
async function bootstrap() {
  enforceDateMinimum();
  const health = await api("/api/health");
  state.mode = health.mode;
  try {
    const data = await api("/api/bootstrap");
    state.csrf = data.csrfToken;
    state.isAdmin = data.isAdmin;
    state.currentAccount = data.account || null;
    state.browserFallbackAvailable = data.browserFallbackAvailable;
    $("#modeBadge").textContent = data.mode.toUpperCase();
    $("#metricMode").dataset.mode = data.mode;
    $("#logoutButton").hidden = data.mode !== "server";
    $("#exitButton").hidden = data.mode === "server";
    $("#adminNav").hidden = !data.isAdmin;
    $("#users").hidden = !data.isAdmin;
    $("#accountEmail").textContent = data.account?.email || "";
    $("#accountEmail").hidden = !data.account?.email;
    $("#privacyText").textContent =
      data.mode === "local"
        ? "Cookies finns bara i minnet tills programmet stängs."
        : "Sessioner isoleras och känslig konfiguration krypteras.";
    fillConfig(data.config);
    enforceDateMinimum();
    setRuntime(data.state);
    updateBankId(data.bankId);
    api("/api/catalog")
      .then(applyCatalog)
      .catch((error) => {
        if (error.status !== 404) console.error(error);
      });
    for (const event of data.events) {
      state.lastEvent = Math.max(state.lastEvent, event.id);
      addEvent(event);
    }
    showApp();
    connectEventStream();
    if (data.isAdmin) await loadUsers();
  } catch (error) {
    if (error.status === 401) {
      if (health.mode === "server") showHome();
      else {
        $("#loginView").hidden = false;
        $("#loginError").textContent =
          "Start the interface through No-Comment-Booking.exe.";
      }
    } else throw error;
  }
}

function userStatus(account) {
  if (account.role === "admin") return "Admin";
  if (account.status === "pending") return "Väntar";
  if (account.status === "disabled") return "Avstängd";
  if (account.paid) return "Betald";
  return "Godkänd";
}

function renderUsers(accounts, total = accounts.length) {
  state.adminUsers = accounts;
  const list = $("#userList");
  list.replaceChildren();
  $("#userResultCount").textContent = `${total} användare`;
  for (const account of accounts) {
    const card = document.createElement("article");
    card.className = "user-card";

    const identity = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = account.displayName || account.email;
    const detail = document.createElement("small");
    detail.textContent = account.email;
    identity.append(name, detail);

    const status = document.createElement("span");
    status.className = `user-status ${account.status}`;
    status.textContent = userStatus(account);

    const actions = document.createElement("div");
    actions.className = "user-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button secondary";
    edit.textContent = "Redigera";
    edit.addEventListener("click", () => openUserDialog(account));
    actions.append(edit);
    card.append(identity, status, actions);
    list.append(card);
  }
  if (!accounts.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Inga användare matchar sökningen.";
    list.append(empty);
  }
}

function openUserDialog(account = null) {
  const userForm = $("#userForm");
  userForm.reset();
  userForm.elements.id.value = account?.id || "";
  userForm.elements.email.value = account?.email || "";
  userForm.elements.display_name.value = account?.displayName || "";
  userForm.elements.status.value = account?.status || "pending";
  userForm.elements.role.value = account?.role || "user";
  userForm.elements.paid.checked = Boolean(account?.paid);
  $("#userDialogTitle").textContent = account ? "Redigera användare" : "Skapa användare";
  $("#existingUserFields").hidden = !account;
  $("#resetUserPassword").hidden = !account;
  $("#deleteUser").hidden = !account;
  $("#userFormError").textContent = "";
  $("#userDialog").showModal();
}

async function loadUsers() {
  if (!state.isAdmin) return;
  const params = new URLSearchParams();
  const query = $("#userSearch").value.trim();
  if (query) params.set("q", query);
  if ($("#userStatusFilter").value) params.set("status", $("#userStatusFilter").value);
  if ($("#userRoleFilter").value) params.set("role", $("#userRoleFilter").value);
  const data = await api(`/api/admin/users?${params}`);
  renderUsers(data.users, data.total);
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  form.classList.add("was-validated");
  if (!form.reportValidity()) return;
  try {
    const config = collectConfig();
    if (config.nearby_location_ids.some(Number.isNaN))
      throw new Error("Närliggande plats-ID:n måste vara heltal");
    await api("/api/monitor/start", {
      method: "POST",
      body: JSON.stringify(config),
    });
    setRuntime("starting");
    toast("Bevakningen startar");
    location.hash = "#overview";
  } catch (error) {
    toast(error.message);
    showDialog("Kunde inte starta", error.message);
  }
});
$("#stopButton").addEventListener("click", async () => {
  try {
    setRuntime("stopping");
    await api("/api/monitor/stop", { method: "POST", body: "{}" });
    toast("Bevakningen stoppades");
  } catch (error) {
    toast(error.message);
  }
});
$("#discordButton").addEventListener("click", async () => {
  const url = form.elements.discord_webhook_url.value.trim();
  if (!url) return toast("Ange en webhook först");
  try {
    await api("/api/discord/test", {
      method: "POST",
      body: JSON.stringify({
        name: form.elements.name.value.trim() || "Bevakare",
        discord_webhook_url: url,
      }),
    });
    toast("Testnotisen skickades");
  } catch (error) {
    toast(error.message);
  }
});
$("#bankidButton").addEventListener("click", async () => {
  try {
    if (state.authenticated) {
      await initializeCatalog();
      return;
    }
    state.catalogAttempted = false;
    await api("/api/bankid/start", { method: "POST", body: "{}" });
    connectEventStream();
    $("#bankidStatus").textContent = "Förbereder säker inloggning…";
    if (!$("#bankidDialog").open) $("#bankidDialog").showModal();
  } catch (error) {
    if (state.authenticated) {
      showIdentityFallback(error);
      $("#manualFallback").hidden = false;
    }
    toast(error.message);
  }
});
$("#bankidCancel").addEventListener("click", async () => {
  await api("/api/bankid/cancel", { method: "POST", body: "{}" });
  $("#bankidDialog").close();
});
$("#bankidClose").addEventListener("click", () => $("#bankidDialog").close());
$("#bankidFallback").addEventListener("click", async () => {
  try {
    await api("/api/bankid/browser-fallback", { method: "POST", body: "{}" });
    $("#bankidStatus").textContent = "Öppnar den säkra webbläsarfallbacken…";
  } catch (error) {
    toast(error.message);
  }
});
$("#bankidRetry").addEventListener("click", async () => {
  try {
    state.catalogAttempted = false;
    await api("/api/bankid/retry", { method: "POST", body: "{}" });
    connectEventStream();
    $("#bankidStatus").textContent = "Förbereder ett nytt inloggningsförsök…";
    $("#bankidRetry").hidden = true;
    $("#bankidFallback").hidden = true;
  } catch (error) {
    toast(error.message);
  }
});
form.elements.licence_id.addEventListener("change", async (event) => {
  if (!state.authenticated || !event.target.value) return;
  try {
    await refreshCatalog(Number(event.target.value));
  } catch (error) {
    toast(error.message);
  }
});
form.elements.ssn.addEventListener("change", async (event) => {
  if (!state.authenticated || !/^\d{8}-?\d{4}$/.test(event.target.value.trim())) return;
  try {
    await initializeCatalog();
    $("#identityFallback").hidden = true;
  } catch (error) {
    toast(error.message);
  }
});
form.elements.nearby_location_ids.addEventListener("change", (event) => {
  for (const option of event.target.options) state.nearbySelection.delete(option.value);
  for (const option of event.target.selectedOptions)
    state.nearbySelection.add(option.value);
});
$("#locationSearch").addEventListener("input", (event) =>
  filterLocations(event.target.value),
);
$("#manualIdsButton").addEventListener("click", () => {
  const mappings = [
    ["manual_licence_id", "licence_id", "Manuell behörighet"],
    ["manual_examination_type_id", "examination_type_id", "Manuell provtyp"],
    ["manual_location_id", "location_id", "Manuell provort"],
    ["manual_vehicle_type_id", "vehicle_type_id", "Manuell fordonstyp"],
    ["manual_occasion_choice_id", "occasion_choice_id", "Manuellt hyrbilsalternativ"],
  ];
  for (const [inputName, selectName, label] of mappings) {
    const value = form.elements[inputName].value;
    if (value) ensureSavedOption(form.elements[selectName], value, label);
  }
  toast("Manuella ID:n används");
});
$("#reservationBook").addEventListener("click", async () => {
  const button = $("#reservationBook");
  button.disabled = true;
  try {
    const result = await api("/api/reservation/book", { method: "POST", body: "{}" });
    $("#eventDialog").close();
    showDialog(
      "Bokningen är klar",
      `${result.date} ${result.time} — booking ID ${result.booking_id}`,
    );
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(data)),
    });
    $("#loginError").textContent = "";
    await bootstrap();
  } catch (error) {
    $("#loginError").textContent = error.message;
  }
});
$("#showRegister").addEventListener("click", () => {
  $("#loginForm").hidden = true;
  $("#showRegister").hidden = true;
  $("#registerForm").hidden = false;
  $("#loginError").textContent = "";
  $("#registerMessage").textContent = "";
});
$("#showLogin").addEventListener("click", showLogin);
$("#registerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  if (data.get("password") !== data.get("password_confirm")) {
    $("#registerMessage").textContent = "Lösenorden matchar inte.";
    return;
  }
  try {
    await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email: data.get("email"),
        password: data.get("password"),
      }),
    });
    event.currentTarget.reset();
    showLogin();
    $("#registerMessage").textContent =
      "Kontot är skapat och väntar på betalning eller administratörsgodkännande.";
  } catch (error) {
    $("#registerMessage").textContent = error.message;
  }
});
$("#logoutButton").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.csrf = "";
  state.isAdmin = false;
  state.currentAccount = null;
  showHome();
});
document.querySelectorAll("[data-open-login]").forEach((button) =>
  button.addEventListener("click", showLogin),
);
document.querySelectorAll("[data-open-home]").forEach((button) =>
  button.addEventListener("click", showHome),
);
$("#resetPasswordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  if (values.get("password") !== values.get("password_confirm")) {
    $("#resetMessage").textContent = "Lösenorden matchar inte.";
    return;
  }
  try {
    await api("/api/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({
        token: new URLSearchParams(location.search).get("reset"),
        password: values.get("password"),
      }),
    });
    history.replaceState({}, "", location.pathname);
    event.currentTarget.reset();
    showLogin();
    $("#loginError").textContent = "Lösenordet är uppdaterat. Logga in igen.";
  } catch (error) {
    $("#resetMessage").textContent = error.message;
  }
});
$("#createUser").addEventListener("click", () => openUserDialog());
$("#userSearchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  loadUsers().catch((error) => toast(error.message));
});
document.querySelectorAll("[data-close-user-dialog]").forEach((button) =>
  button.addEventListener("click", () => $("#userDialog").close()),
);
document.querySelectorAll("[data-close-reset-dialog]").forEach((button) =>
  button.addEventListener("click", () => $("#resetLinkDialog").close()),
);
$("#userForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  const id = String(values.get("id") || "");
  const payload = {
    email: values.get("email"),
    display_name: values.get("display_name") || null,
  };
  if (id) {
    payload.status = values.get("status");
    payload.role = values.get("role");
    payload.paid = values.get("paid") === "on";
  }
  try {
    const result = await api(id ? `/api/admin/users/${encodeURIComponent(id)}` : "/api/admin/users", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    $("#userDialog").close();
    await loadUsers();
    toast(id ? "Användaren uppdaterades" : "Användaren skapades");
    if (!id && result.resetToken) showResetLink(result.resetToken);
  } catch (error) {
    $("#userFormError").textContent = error.message;
  }
});
function showResetLink(token) {
  const url = new URL(location.pathname, location.origin);
  url.searchParams.set("reset", token);
  $("#resetLink").value = url.toString();
  $("#resetLinkDialog").showModal();
}
$("#resetUserPassword").addEventListener("click", async () => {
  const id = $("#userForm").elements.id.value;
  try {
    const result = await api(`/api/admin/users/${encodeURIComponent(id)}/password-reset`, {
      method: "POST",
      body: "{}",
    });
    $("#userDialog").close();
    showResetLink(result.resetToken);
  } catch (error) {
    $("#userFormError").textContent = error.message;
  }
});
$("#deleteUser").addEventListener("click", async () => {
  const userForm = $("#userForm");
  const id = userForm.elements.id.value;
  if (!confirm(`Radera ${userForm.elements.email.value}? Detta kan inte ångras.`)) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: "DELETE", body: "{}" });
    $("#userDialog").close();
    await loadUsers();
    toast("Användaren raderades");
  } catch (error) {
    $("#userFormError").textContent = error.message;
  }
});
$("#copyResetLink").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#resetLink").value);
  toast("Länken kopierades");
});
$("#refreshUsers").addEventListener("click", () =>
  loadUsers().catch((error) => toast(error.message)),
);
$("#exitButton").addEventListener("click", async () => {
  if (
    confirm("Close No-Comment-Booking and delete the temporary session?")
  ) {
    await api("/api/app/exit", { method: "POST", body: "{}" });
    document.body.innerHTML =
      '<main class="login-shell"><section class="login-card"><h1>Programmet är stängt</h1><p class="muted">Du kan stänga den här fliken.</p></section></main>';
  }
});
$("#clearActivity").addEventListener("click", () => {
  $("#activityList").innerHTML =
    '<div class="empty-state"><span>↻</span><strong>Visningen är rensad</strong><p>Nya händelser visas automatiskt.</p></div>';
});
$("#dialogClose").addEventListener("click", () => $("#eventDialog").close());
$("#dialogOk").addEventListener("click", () => $("#eventDialog").close());
$("#menuButton").addEventListener("click", () =>
  $(".sidebar").classList.toggle("open"),
);
document.querySelectorAll(".nav-link").forEach((link) =>
  link.addEventListener("click", () => {
    $(".sidebar").classList.remove("open");
    document
      .querySelectorAll(".nav-link")
      .forEach((item) => item.classList.toggle("active", item === link));
  }),
);
form.addEventListener("input", updateMetrics);
enforceDateMinimum();
setInterval(enforceDateMinimum, 30_000);
window.addEventListener("online", () => liveTransport.resume());
window.addEventListener("offline", () => {
  liveTransport.offline();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") liveTransport.resume();
});
if (new URLSearchParams(location.search).has("reset")) {
  showOnly("#resetView");
} else {
  bootstrap().catch((error) => {
    console.error(error);
    showHome();
    toast("Tjänsten kunde inte startas korrekt.");
  });
}
