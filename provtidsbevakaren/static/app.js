"use strict";

const state = {
  mode: "local",
  csrf: "",
  lastEvent: 0,
  pollTimer: null,
  polling: false,
  catalog: { licences: [], examinationTypes: [], locations: [] },
  qrVersion: 0,
  authenticated: false,
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
  if (selectedValue) ensureSavedOption(select, selectedValue, "Unavailable saved selection");
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
  filterLocations($("#locationSearch").value);
}
async function refreshCatalog(licenceId = 0) {
  const ssn = form.elements.ssn.value.trim();
  if (!/^\d{8}-?\d{4}$/.test(ssn)) throw new Error("Enter a valid identity number first");
  const data = await api("/api/catalog/refresh", {
    method: "POST",
    body: JSON.stringify({ ssn, licence_id: Number(licenceId) || 0 }),
  });
  applyCatalog(data);
  toast(`${data.locations.length} provorter indexerade`);
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
    $("#bankidFallback").hidden = bankId.state !== "error";
    $("#bankidRetry").hidden = bankId.state !== "error";
    if (bankId.qrVersion && bankId.qrVersion !== state.qrVersion) {
      state.qrVersion = bankId.qrVersion;
      $("#bankidQr").src = `/api/bankid/qr.svg?v=${bankId.qrVersion}`;
    }
    const remaining = Math.max(0, Math.ceil((bankId.expiresAt * 1000 - Date.now()) / 1000));
    $("#bankidCountdown").textContent = remaining ? `Löper ut om ${remaining} sekunder` : "";
  } else if (bankId.state === "complete") {
    if (dialog.open) dialog.close();
    const canLoadCatalog = /^\d{8}-?\d{4}$/.test(form.elements.ssn.value.trim());
    if (!state.catalog.locations.length && !state.catalogAttempted && canLoadCatalog) {
      state.catalogAttempted = true;
      refreshCatalog(Number(form.elements.licence_id.value) || 0).catch((error) => {
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
async function poll() {
  if (state.polling) return;
  state.polling = true;
  let continuePolling = true;
  try {
    const data = await api(`/api/events?after=${state.lastEvent}`);
    setRuntime(data.state);
    updateBankId(data.bankId);
    for (const event of data.events) {
      state.lastEvent = Math.max(state.lastEvent, event.id);
      addEvent(event);
    }
  } catch (error) {
    if (error.status === 401) {
      continuePolling = false;
      clearTimeout(state.pollTimer);
      showLogin();
    } else console.error(error);
  } finally {
    state.polling = false;
    if (continuePolling) state.pollTimer = setTimeout(poll, 1000);
  }
}
function showLogin() {
  clearTimeout(state.pollTimer);
  $("#appView").hidden = true;
  $("#loginView").hidden = false;
}
function showApp() {
  $("#loginView").hidden = true;
  $("#appView").hidden = false;
}
async function bootstrap() {
  enforceDateMinimum();
  const health = await api("/api/health");
  state.mode = health.mode;
  try {
    const data = await api("/api/bootstrap");
    state.csrf = data.csrfToken;
    $("#modeBadge").textContent = data.mode.toUpperCase();
    $("#metricMode").dataset.mode = data.mode;
    $("#logoutButton").hidden = data.mode !== "server";
    $("#exitButton").hidden = data.mode === "server";
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
    poll();
  } catch (error) {
    if (error.status === 401) {
      if (health.mode === "server") showLogin();
      else {
        $("#loginView").hidden = false;
        $("#loginError").textContent =
          "Start the interface through No-Comment-Booking.exe.";
      }
    } else throw error;
  }
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
      await refreshCatalog(Number(form.elements.licence_id.value) || 0);
      return;
    }
    await api("/api/bankid/start", { method: "POST", body: "{}" });
    $("#bankidStatus").textContent = "Förbereder säker inloggning…";
    if (!$("#bankidDialog").open) $("#bankidDialog").showModal();
  } catch (error) {
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
    await api("/api/bankid/retry", { method: "POST", body: "{}" });
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
$("#logoutButton").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.csrf = "";
  showLogin();
});
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
bootstrap().catch((error) => {
  console.error(error);
  showLogin();
  $("#loginError").textContent = "Tjänsten kunde inte startas korrekt.";
});
