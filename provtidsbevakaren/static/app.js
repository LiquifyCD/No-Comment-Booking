"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const form = $("#monitorForm");
const state = {
  csrf: "", isAdmin: false, discordAllowed: false, cursor: 0,
  catalog: { licences: [], examinationTypes: [], locations: [], vehicleTypes: [], occasionChoices: [] },
  selectedLocations: [], currentStep: "bankid", users: [], live: null,
};

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
  if (response.status === 401) { showView("login"); throw new Error("Sessionen har gått ut."); }
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
  renderLocations();
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
  showStep("loading");
  try {
    const data = await api("/api/catalog/refresh", { method: "POST", body: JSON.stringify({ ssn: "", licence_id: licenceId }) });
    applyCatalog(data); showStep("options");
  } catch (error) { showStep("options"); toast(error.message); }
}

function updateStatus(snapshot) {
  const labels = { idle: "Redo att starta", authentication: "Väntar på BankID", authenticated: "BankID klart", starting: "Startar", running: "Bevakning aktiv", stopping: "Stoppar", error: "Ett fel uppstod" };
  $("#statusTitle").textContent = labels[snapshot.state] || snapshot.state;
  $("#statusDescription").textContent = snapshot.state === "running" ? "Lediga tider kontrolleras var 15:e sekund." : "Följ stegen för att konfigurera bevakningen.";
  $("#footerStatus").textContent = labels[snapshot.state] || snapshot.state;
  $("#stopButton").disabled = !["running", "starting", "authentication"].includes(snapshot.state);
  const bankId = snapshot.bankId || {};
  $("#bankidSummary").textContent = bankId.authenticated ? "BankID anslutet" : bankId.state === "pending" ? "Väntar på BankID" : "Inte anslutet";
  $("#bankidStatus").textContent = ({ starting: "Förbereder säker inloggning…", pending: "Skanna QR-koden eller öppna BankID.", complete: "BankID-inloggningen är klar.", error: bankId.error || "BankID kunde inte anslutas." })[bankId.state] || "Förbereder säker inloggning…";
  $("#bankidQr").hidden = bankId.state !== "pending";
  $("#bankidOpen").hidden = !bankId.canOpenOnDevice;
  $("#bankidRetry").hidden = bankId.state !== "error";
  if (bankId.state === "pending") $("#bankidQr").src = `/api/bankid/qr.svg?v=${bankId.qrVersion || Date.now()}`;
  if (bankId.authenticated && ["bankid", "loading"].includes(state.currentStep)) loadInitialCatalog();
  if (snapshot.events) renderEvents(snapshot.events);
}

async function loadInitialCatalog() {
  if (loadInitialCatalog.running) return; loadInitialCatalog.running = true; showStep("loading");
  try {
    let data;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try { data = await api("/api/catalog"); break; } catch { await new Promise((resolve) => setTimeout(resolve, 1000)); }
    }
    if (!data) data = await api("/api/catalog/refresh", { method: "POST", body: JSON.stringify({ ssn: "", licence_id: 0 }) });
    applyCatalog(data); $("#bankidDialog").open && $("#bankidDialog").close(); showStep("options");
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
    onState: (value) => { const node = $("#connectionBadge"); node.textContent = value === "live" ? "Live" : value === "offline" ? "Offline" : "Återansluter"; node.classList.toggle("reconnecting", value !== "live"); },
  });
  state.live.start();
}

async function bootstrap() {
  const data = await api("/api/bootstrap");
  state.csrf = data.csrfToken; state.isAdmin = data.isAdmin; state.discordAllowed = data.discordAllowed;
  $("#adminTopNav").hidden = !state.isAdmin; $("#activity").hidden = !state.isAdmin;
  $("#discordPanel").hidden = !state.discordAllowed; $("#discordDefault").checked = !!data.discordDefaultForNewUsers;
  $("#accountEmail").textContent = data.account?.email || ""; $("#modeBadge").textContent = data.mode.toUpperCase();
  $("#logoutButton").hidden = data.mode !== "server"; $("#exitButton").hidden = data.mode === "server";
  showView("app"); updateStatus(data); if (data.catalogUpdatedAt) { try { applyCatalog(await api("/api/catalog")); } catch {} }
  startLive();
}

function monitorPayload() {
  const weekdays = $$('input[name="weekday"]:checked').map((node) => Number(node.value));
  return {
    name: "Min provtidsbevakning", ssn: "", licence_id: Number(form.elements.licence_id.value),
    examination_type_id: Number(form.elements.examination_type_id.value || 1), location_id: state.selectedLocations[0],
    nearby_location_ids: state.selectedLocations.slice(1), vehicle_type_id: Number(form.elements.vehicle_type_id.value || 1),
    tachograph_type_id: 1, occasion_choice_id: Number(form.elements.occasion_choice_id.value || 1), language_id: 13,
    date_from: form.elements.date_from.value || null, date_to: form.elements.date_to.value || null,
    earliest_time: form.elements.earliest_time.value || null, latest_time: form.elements.latest_time.value || null,
    allowed_weekdays: weekdays, discord_webhook_url: state.discordAllowed ? form.elements.discord_webhook_url.value.trim() : "",
    timezone: "Europe/Stockholm",
  };
}

function validateStep(step) {
  if (step === "locations" && !Number(form.elements.licence_id.value)) return "Välj en behörighet.";
  if (step === "schedule" && !state.selectedLocations.length) return "Välj minst en provort.";
  if (step === "notifications" && !form.elements.date_from.value) return "Välj ett startdatum.";
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

$("#bankidButton").addEventListener("click", async () => { try { await api("/api/bankid/start", { method: "POST", body: "{}" }); $("#bankidDialog").showModal(); } catch (error) { toast(error.message); } });
$("#bankidCancel").addEventListener("click", async () => { await api("/api/bankid/cancel", { method: "POST", body: "{}" }); $("#bankidDialog").close(); });
$("#bankidClose").addEventListener("click", () => $("#bankidDialog").close());
$("#bankidRetry").addEventListener("click", () => api("/api/bankid/retry", { method: "POST", body: "{}" }).catch((error) => toast(error.message)));
$("#bankidFallback").addEventListener("click", () => api("/api/bankid/browser-fallback", { method: "POST", body: "{}" }).catch((error) => toast(error.message)));
form.elements.licence_id.addEventListener("change", loadLicenceOptions); $("#locationSearch").addEventListener("input", renderLocations);
$$('[data-next]').forEach((button) => button.addEventListener("click", () => { const error = validateStep(button.dataset.next); error ? toast(error) : showStep(button.dataset.next); }));
$$('[data-back]').forEach((button) => button.addEventListener("click", () => showStep(button.dataset.back)));
form.addEventListener("submit", async (event) => { event.preventDefault(); const error = validateStep("notifications"); if (error) return toast(error); try { await api("/api/monitor/start", { method: "POST", body: JSON.stringify(monitorPayload()) }); toast("Bevakningen startar."); } catch (err) { toast(err.message); } });
$("#stopButton").addEventListener("click", async () => { try { await api("/api/monitor/stop", { method: "POST", body: "{}" }); toast("Bevakningen är stoppad."); } catch (error) { toast(error.message); } });
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
