"use strict";

const state = {
  mode: "local",
  csrf: "",
  lastEvent: 0,
  pollTimer: null,
  polling: false,
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
function collectConfig() {
  const mode = form.elements.booking_mode.value;
  return {
    name: form.elements.name.value.trim(),
    ssn: form.elements.ssn.value.trim(),
    licence_id: integer("licence_id"),
    examination_type_id: integer("examination_type_id"),
    location_id: integer("location_id"),
    nearby_location_ids: form.elements.nearby_location_ids.value
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean)
      .map(Number),
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
  };
}
function fillConfig(config) {
  if (!config) return;
  for (const [key, value] of Object.entries(config)) {
    if (
      [
        "auto_reserve",
        "auto_book",
        "allowed_weekdays",
        "nearby_location_ids",
      ].includes(key)
    )
      continue;
    const input = form.elements[key];
    if (input && value !== null) input.value = value;
  }
  form.elements.nearby_location_ids.value = (
    config.nearby_location_ids || []
  ).join(", ");
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
    showDialog(title.textContent, event.message, event.data?.url || "");
}
function showDialog(title, message, url = "") {
  $("#dialogTitle").textContent = title;
  $("#dialogMessage").textContent = message;
  $("#dialogIcon").textContent =
    title.includes("Fel") || title.includes("slutföras") ? "!" : "✓";
  const link = $("#browserLink");
  link.hidden = !url;
  link.href = url || "#";
  $("#eventDialog").showModal();
}
async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    const data = await api(`/api/events?after=${state.lastEvent}`);
    setRuntime(data.state);
    for (const event of data.events) {
      state.lastEvent = Math.max(state.lastEvent, event.id);
      addEvent(event);
    }
  } catch (error) {
    if (error.status === 401) {
      clearTimeout(state.pollTimer);
      showLogin();
    } else console.error(error);
  } finally {
    state.polling = false;
    state.pollTimer = setTimeout(poll, 1000);
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
    setRuntime(data.state);
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
bootstrap().catch((error) => {
  console.error(error);
  showLogin();
  $("#loginError").textContent = "Tjänsten kunde inte startas korrekt.";
});
