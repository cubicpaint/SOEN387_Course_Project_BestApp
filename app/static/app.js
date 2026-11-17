"use strict";

const element = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "login-form", "login-button", "email", "password", "signed-in", "identity", "logout-button", "account-status",
  "availability-form", "browse-fields", "barber", "service", "date", "availability-button", "availability-status",
  "slot-options", "slots", "selection", "book-button", "retry-button", "booking-status",
  "appointments-button", "appointments-status", "appointments",
  "management-section", "management-summary", "change-availability-form", "change-date", "change-availability-button",
  "change-availability-status", "change-slot-options", "change-slots", "change-confirm", "reschedule-button",
  "cancel-appointment-button", "close-management-button", "change-result-section", "change-status", "change-retry-button",
  "staff-section", "staff-availability-form", "staff-barber", "staff-date", "staff-load-button", "staff-editor",
  "staff-availability-status", "staff-configured", "staff-ranges", "staff-add-range-button", "staff-close-date",
  "staff-save-button", "staff-appointments-button", "staff-appointments-status", "staff-appointments",
].map((id) => [id, element(id)]));
const services = { haircut: "Haircut", beard: "Beard", both: "Haircut and beard" };
const barberNames = new Map();
const pendingBookings = new Map();
const pendingChanges = new Map();
const storageKey = "bestapp.pendingBookings";
const changeStorageKey = "bestapp.pendingChanges";
const timeFormat = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const clockFormat = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
let token = null;
let user = null;
let selected = null;
let authEpoch = 0;
let loginAttempt = 0;
let availabilityAttempt = 0;
let appointmentsAttempt = 0;
let bookingAttempt = 0;
let bookingBusy = false;
let availabilityBusy = false;
let storageAvailable = true;
let management = null;
let changeSelected = null;
let mutationBusy = false;
let mutationAttempt = 0;
let managementAttempt = 0;
let changeAvailabilityAttempt = 0;
let staffAvailabilityAttempt = 0;
let staffAppointmentsAttempt = 0;
let staffRevision = null;
let staffRangeRows = [];
let staffRangeSequence = 0;

function validChangeIntent(pending) {
  if (!pending || typeof pending.userId !== "string" || typeof pending.key !== "string" ||
      typeof pending.path !== "string" || !/^usr_[A-Za-z0-9_-]{1,60}$/.test(pending.userId) ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(pending.key) ||
      typeof pending.summary !== "string" || pending.summary.length > 500 ||
      typeof pending.ifMatch !== "string") return false;
  const appointmentPath = /^\/appointments\/apt_[A-Za-z0-9_-]{1,60}$/;
  if (pending.kind === "cancel") return pending.method === "DELETE" && appointmentPath.test(pending.path) &&
    /^"[1-9][0-9]*"$/.test(pending.ifMatch) && pending.body === null;
  if (pending.kind === "reschedule") return pending.method === "PUT" && appointmentPath.test(pending.path) &&
    /^"[1-9][0-9]*"$/.test(pending.ifMatch) && pending.body && Object.keys(pending.body).length === 1 &&
    /^slot_[A-Za-z0-9_-]{1,59}$/.test(pending.body.slotId);
  return pending.kind === "availability" && pending.method === "PUT" &&
    /^\/barbers\/bar_[A-Za-z0-9_-]{1,60}\/availability\?date=[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(pending.path) &&
    /^"[a-f0-9]{64}"$/.test(pending.ifMatch) && pending.body && Object.keys(pending.body).length === 1 &&
    Array.isArray(pending.body.ranges) && pending.body.ranges.length <= 50 && pending.body.ranges.every(range =>
      range && Object.keys(range).length === 2 && typeof range.startTime === "string" &&
      typeof range.endTime === "string" && range.startTime.length <= 40 && range.endTime.length <= 40);
}

function restorePendingChanges() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(changeStorageKey) || "[]");
    if (Array.isArray(saved)) for (const pending of saved) {
      if (validChangeIntent(pending)) pendingChanges.set(pending.userId, pending);
    }
  } catch { storageAvailable = false; }
}

function savePendingChanges() {
  try { sessionStorage.setItem(changeStorageKey, JSON.stringify([...pendingChanges.values()])); }
  catch { storageAvailable = false; }
}

function pendingChangeForUser() { return user ? pendingChanges.get(user.id) : undefined; }

function staff() { return Boolean(token && user && user.role === "staff"); }

function hasPendingIntent() { return Boolean(pendingForUser() || pendingChangeForUser()); }

function restorePendingBookings() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (!Array.isArray(saved)) return;
    for (const pending of saved) {
      if (pending && /^usr_[A-Za-z0-9_-]+$/.test(pending.userId) &&
          /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(pending.key) &&
          pending.body && /^slot_[A-Za-z0-9_-]+$/.test(pending.body.slotId) &&
          Object.hasOwn(services, pending.body.service) &&
          typeof pending.summary === "string" && pending.summary.length <= 500) {
        pendingBookings.set(pending.userId, {
          userId: pending.userId, key: pending.key,
          body: { slotId: pending.body.slotId, service: pending.body.service }, summary: pending.summary,
        });
      }
    }
  } catch {
    storageAvailable = false;
  }
}

function savePendingBookings() {
  try {
    sessionStorage.setItem(storageKey, JSON.stringify([...pendingBookings.values()]));
  } catch {
    storageAvailable = false;
  }
}

function pendingForUser() {
  return user ? pendingBookings.get(user.id) : undefined;
}

function customer() {
  return Boolean(token && user && user.role === "customer");
}

function displayTime(value) {
  return timeFormat.format(new Date(value));
}

function displayRange(startTime, endTime) {
  const end = torontoDate(startTime) === torontoDate(endTime)
    ? clockFormat.format(new Date(endTime)) : displayTime(endTime);
  return `${displayTime(startTime)} to ${end}`;
}

function summary(slot, service, barberName) {
  return `${services[service]} (${service === "both" ? 60 : 30} minutes) with ${barberName}: ${displayRange(slot.startTime, slot.endTime)}.`;
}

function updateControls() {
  const pending = pendingForUser();
  const change = pendingChangeForUser();
  const blocked = Boolean(pending || change);
  ui["browse-fields"].disabled = blocked;
  ui["availability-button"].disabled = availabilityBusy || barberNames.size === 0;
  ui["slot-options"].disabled = blocked;
  ui["book-button"].disabled = !customer() || !selected || blocked || bookingBusy;
  ui["book-button"].hidden = Boolean(pending);
  ui["retry-button"].hidden = !pending;
  ui["retry-button"].disabled = !customer() || bookingBusy;
  ui["appointments-button"].disabled = !customer();
  ui["change-result-section"].hidden = !user;
  ui["change-retry-button"].hidden = !change;
  ui["change-retry-button"].disabled = !user || mutationBusy;
  ui["change-availability-button"].disabled = !customer() || !management || blocked || mutationBusy;
  ui["change-slot-options"].disabled = blocked || mutationBusy;
  ui["reschedule-button"].disabled = !customer() || !management || !changeSelected || blocked || mutationBusy || !ui["change-confirm"].checked;
  ui["cancel-appointment-button"].disabled = !customer() || !management || blocked || mutationBusy || !ui["change-confirm"].checked;
  ui["staff-load-button"].disabled = !staff() || mutationBusy;
  ui["staff-barber"].disabled = blocked || mutationBusy;
  ui["staff-date"].disabled = blocked || mutationBusy;
  ui["staff-editor"].disabled = !staff() || !staffRevision || blocked || mutationBusy;
  ui["staff-appointments-button"].disabled = !staff();
  if (pending) ui.selection.textContent = `Pending booking: ${pending.summary}`;
  else ui.selection.textContent = selected ? selected.summary : "Select an available starting time above.";
}

function safeMessage(result) {
  const error = result.body && result.body.error;
  return error && typeof error.message === "string"
    ? `${error.message}${typeof error.code === "string" ? ` (${error.code})` : ""}`
    : `The request could not be completed (HTTP ${result.status}).`;
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, cache: "no-store" });
    let body;
    try { body = await response.json(); } catch { body = null; }
    const etag = response.headers && typeof response.headers.get === "function" ? response.headers.get("ETag") : null;
    return { status: response.status, ok: response.ok, body, etag };
  } finally {
    clearTimeout(timeout);
  }
}

function clearSelection() {
  selected = null;
  ui.slots.replaceChildren();
  ui["slot-options"].hidden = true;
  updateControls();
}

function resetAccountViews() {
  managementAttempt += 1;
  changeAvailabilityAttempt += 1;
  staffAvailabilityAttempt += 1;
  staffAppointmentsAttempt += 1;
  mutationAttempt += 1;
  management = null;
  changeSelected = null;
  mutationBusy = false;
  staffRevision = null;
  staffRangeRows = [];
  ui["management-section"].hidden = true;
  ui["management-summary"].textContent = "";
  ui["staff-section"].hidden = true;
  ui["change-confirm"].checked = false;
  ui["change-slots"].replaceChildren();
  ui["change-slot-options"].hidden = true;
  ui["change-status"].textContent = "";
  ui["staff-configured"].replaceChildren();
  ui["staff-ranges"].replaceChildren();
  ui["staff-appointments"].replaceChildren();
  ui["staff-availability-status"].textContent = "";
  ui["staff-appointments-status"].textContent = "";
}

function configureStaffBarbers() {
  const previous = ui["staff-barber"].value;
  ui["staff-barber"].replaceChildren();
  const assigned = staff() && Array.isArray(user.managedBarberIds) ? user.managedBarberIds : [];
  for (const barberId of assigned) {
    const option = document.createElement("option");
    option.value = barberId;
    option.textContent = barberNames.get(barberId) || barberId;
    ui["staff-barber"].append(option);
  }
  if (assigned.includes(previous)) ui["staff-barber"].value = previous;
  else ui["staff-barber"].value = assigned[0] || "";
  ui["staff-section"].hidden = !staff();
  if (staff() && assigned.length === 0) ui["staff-availability-status"].textContent = "No barbers are assigned to this staff account.";
}

function signOut(message = "Logged out. Log in as a customer to book or view appointments.") {
  authEpoch += 1;
  loginAttempt += 1;
  availabilityAttempt += 1;
  appointmentsAttempt += 1;
  bookingAttempt += 1;
  token = null;
  user = null;
  bookingBusy = false;
  availabilityBusy = false;
  ui["login-form"].hidden = false;
  ui["login-button"].disabled = false;
  ui["signed-in"].hidden = true;
  ui.identity.textContent = "";
  ui.password.value = "";
  ui["account-status"].textContent = message;
  ui["booking-status"].textContent = "";
  ui["appointments-status"].textContent = "Log in as a customer to view your appointments.";
  ui.appointments.replaceChildren();
  resetAccountViews();
  clearSelection();
}

async function login(event) {
  event.preventDefault();
  const attempt = ++loginAttempt;
  ui["login-button"].disabled = true;
  ui["account-status"].textContent = "Logging in…";
  const body = JSON.stringify({ email: ui.email.value, password: ui.password.value });
  ui.password.value = "";
  try {
    const result = await request("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body });
    if (attempt !== loginAttempt) return;
    if (!result.ok) {
      ui["account-status"].textContent = safeMessage(result);
      return;
    }
    if (!result.body || typeof result.body.accessToken !== "string" || !result.body.user) throw new Error("Invalid login response");
    authEpoch += 1;
    resetAccountViews();
    availabilityAttempt += 1;
    appointmentsAttempt += 1;
    bookingAttempt += 1;
    if (availabilityBusy) ui["availability-status"].textContent = "Find availability again to view current starting times.";
    availabilityBusy = false;
    token = result.body.accessToken;
    user = result.body.user;
    bookingBusy = false;
    ui["login-form"].hidden = true;
    ui["signed-in"].hidden = false;
    ui.identity.textContent = `Signed in: ${ui.email.value} (${user.role}).`;
    ui["account-status"].textContent = customer()
      ? "You can book appointments and view your upcoming visits."
      : "This is a staff account. You can view schedules and appointments for your assigned barbers below.";
    ui.appointments.replaceChildren();
    if (pendingForUser()) {
      ui["booking-status"].textContent = "A previous booking needs a definite result. Retry the same booking before choosing a new one.";
    } else ui["booking-status"].textContent = "";
    if (pendingChangeForUser()) ui["change-status"].textContent = `Pending change: ${pendingChangeForUser().summary} Retry that same change before starting another.`;
    configureStaffBarbers();
    updateControls();
    if (customer()) await loadAppointments();
    else ui["appointments-status"].textContent = "Personal appointment listing is available to customers only.";
  } catch {
    if (attempt === loginAttempt) ui["account-status"].textContent = "Login could not be completed. Check the connection and try again.";
  } finally {
    if (attempt === loginAttempt) ui["login-button"].disabled = false;
  }
}

async function loadBarbers() {
  try {
    const result = await request("/barbers");
    if (!result.ok || !result.body || !Array.isArray(result.body.barbers)) {
      ui["availability-status"].textContent = result.ok ? "Barber data could not be read." : safeMessage(result);
      return;
    }
    ui.barber.replaceChildren();
    for (const barber of result.body.barbers) {
      barberNames.set(barber.id, barber.displayName);
      const option = document.createElement("option");
      option.value = barber.id;
      option.textContent = barber.displayName;
      ui.barber.append(option);
    }
    ui["availability-status"].textContent = barberNames.size ? "Choose your preferences, then find availability." : "No barbers are available.";
  } catch {
    ui["availability-status"].textContent = "Barbers could not be loaded. Refresh the page to try again.";
  } finally {
    configureStaffBarbers();
    updateControls();
  }
}

async function loadAvailability(event) {
  if (event) event.preventDefault();
  if (hasPendingIntent() || !ui.barber.value || !ui.date.value) return;
  const attempt = ++availabilityAttempt;
  const epoch = authEpoch;
  const service = ui.service.value;
  const barberName = barberNames.get(ui.barber.value);
  const query = new URLSearchParams({ barberId: ui.barber.value, date: ui.date.value, service });
  availabilityBusy = true;
  clearSelection();
  ui["availability-status"].textContent = "Checking availability…";
  try {
    const result = await request(`/availability?${query}`);
    if (attempt !== availabilityAttempt || epoch !== authEpoch) return;
    if (!result.ok || !result.body || !Array.isArray(result.body.slots)) {
      ui["availability-status"].textContent = result.ok ? "Availability data could not be read." : safeMessage(result);
      return;
    }
    for (const slot of result.body.slots) {
      const label = document.createElement("label");
      label.className = "slot";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "starting-slot";
      radio.value = slot.slotId;
      const text = document.createElement("span");
      text.textContent = displayRange(slot.startTime, slot.endTime);
      radio.addEventListener("change", () => {
        if (pendingForUser()) return;
        selected = { slotId: slot.slotId, service, summary: summary(slot, service, barberName) };
        ui["booking-status"].textContent = "";
        updateControls();
      });
      label.append(radio, text);
      ui.slots.append(label);
    }
    ui["slot-options"].hidden = result.body.slots.length === 0;
    ui["availability-status"].textContent = result.body.slots.length
      ? `${result.body.slots.length} available starting time(s). Select one below.`
      : "No eligible starting times match these preferences. Try another date, barber, or service.";
  } catch {
    if (attempt === availabilityAttempt && epoch === authEpoch) ui["availability-status"].textContent = "Availability could not be loaded. Try again.";
  } finally {
    if (attempt === availabilityAttempt && epoch === authEpoch) {
      availabilityBusy = false;
      updateControls();
    }
  }
}

async function loadAppointments() {
  if (!customer()) return;
  const attempt = ++appointmentsAttempt;
  const epoch = authEpoch;
  const authorization = `Bearer ${token}`;
  ui.appointments.replaceChildren();
  ui["appointments-status"].textContent = "Loading your appointments…";
  try {
    const result = await request("/appointments", { headers: { Authorization: authorization } });
    if (attempt !== appointmentsAttempt || epoch !== authEpoch) return;
    if (result.status === 401) {
      signOut("Your session expired. Log in again; any pending booking keeps its original retry key.");
      return;
    }
    if (!result.ok || !result.body || !Array.isArray(result.body.appointments)) {
      ui["appointments-status"].textContent = result.ok ? "Appointment data could not be read." : safeMessage(result);
      return;
    }
    for (const appointment of result.body.appointments) {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = `${services[appointment.service]} · ${appointment.durationMinutes} minutes`;
      const detail = document.createElement("span");
      detail.textContent = `${barberNames.get(appointment.barberId) || appointment.barberId} · ${displayRange(appointment.startTime, appointment.endTime)}`;
      const reference = document.createElement("small");
      reference.textContent = `Confirmed · ${appointment.id}`;
      item.append(title, detail, reference);
      const manage = document.createElement("button");
      manage.type = "button";
      manage.className = "secondary";
      manage.textContent = "Reschedule or cancel";
      manage.disabled = hasPendingIntent();
      manage.addEventListener("click", () => openAppointment(appointment.id));
      item.append(manage);
      ui.appointments.append(item);
    }
    ui["appointments-status"].textContent = result.body.appointments.length ? "Your confirmed upcoming appointments:" : "You have no upcoming confirmed appointments.";
  } catch {
    if (attempt === appointmentsAttempt && epoch === authEpoch) ui["appointments-status"].textContent = "Appointments could not be loaded. Try refreshing them.";
  }
}

function torontoDate(value) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Toronto", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(value)).filter(part => part.type !== "literal").map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function closeManagement() {
  managementAttempt += 1;
  changeAvailabilityAttempt += 1;
  management = null;
  changeSelected = null;
  ui["management-section"].hidden = true;
  ui["change-confirm"].checked = false;
  ui["change-slots"].replaceChildren();
  ui["change-slot-options"].hidden = true;
  updateControls();
}

async function openAppointment(appointmentId) {
  if (!customer() || hasPendingIntent()) return;
  closeManagement();
  const attempt = ++managementAttempt;
  const epoch = authEpoch;
  ui["change-status"].textContent = "Loading the current appointment...";
  try {
    const result = await request(`/appointments/${appointmentId}`, { headers: { Authorization: `Bearer ${token}` } });
    if (attempt !== managementAttempt || epoch !== authEpoch) return;
    if (result.status === 401) { signOut("Your session expired. Log in again to manage your appointment."); return; }
    if (!result.ok) { ui["change-status"].textContent = safeMessage(result); return; }
    if (!result.body || result.body.id !== appointmentId || !Number.isInteger(result.body.version) ||
        result.body.version < 1 || !/^"[1-9][0-9]*"$/.test(result.etag || "")) throw new Error("Invalid appointment response");
    if (result.body.status !== "confirmed") {
      ui["change-status"].textContent = "This appointment is already cancelled. Refresh your appointments.";
      return;
    }
    management = { ...result.body, ifMatch: result.etag };
    ui["management-summary"].textContent = summary(management, management.service, barberNames.get(management.barberId) || management.barberId);
    ui["change-date"].value = torontoDate(management.startTime);
    ui["management-section"].hidden = false;
    ui["change-availability-status"].textContent = "Find replacement times, or review and cancel this appointment.";
    ui["change-status"].textContent = "";
  } catch {
    if (attempt === managementAttempt && epoch === authEpoch) ui["change-status"].textContent = "The appointment could not be loaded. Refresh and try again.";
  } finally {
    if (attempt === managementAttempt && epoch === authEpoch) updateControls();
  }
}

async function loadReplacementTimes(event) {
  if (event) event.preventDefault();
  if (!customer() || !management || hasPendingIntent() || !ui["change-date"].value) return;
  const appointment = management;
  const attempt = ++changeAvailabilityAttempt;
  const epoch = authEpoch;
  changeSelected = null;
  ui["change-slots"].replaceChildren();
  ui["change-slot-options"].hidden = true;
  ui["change-availability-status"].textContent = "Checking replacement times...";
  updateControls();
  try {
    const query = new URLSearchParams({ date: ui["change-date"].value });
    const result = await request(`/appointments/${appointment.id}/availability?${query}`, { headers: { Authorization: `Bearer ${token}` } });
    if (attempt !== changeAvailabilityAttempt || epoch !== authEpoch || management !== appointment) return;
    if (result.status === 401) { signOut("Your session expired. Log in again to manage your appointment."); return; }
    if (!result.ok || !result.body || !Array.isArray(result.body.slots)) {
      ui["change-availability-status"].textContent = result.ok ? "Replacement times could not be read." : safeMessage(result);
      return;
    }
    const choices = result.body.slots.filter(slot => slot.slotId !== appointment.slotId);
    for (const slot of choices) {
      const label = document.createElement("label"); label.className = "slot";
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = "replacement-slot"; radio.value = slot.slotId;
      const text = document.createElement("span"); text.textContent = displayRange(slot.startTime, slot.endTime);
      radio.addEventListener("change", () => {
        if (hasPendingIntent() || management !== appointment) return;
        changeSelected = { slotId: slot.slotId, summary: summary(slot, appointment.service, barberNames.get(appointment.barberId) || appointment.barberId) };
        updateControls();
      });
      label.append(radio, text); ui["change-slots"].append(label);
    }
    ui["change-slot-options"].hidden = choices.length === 0;
    ui["change-availability-status"].textContent = choices.length
      ? "Choose a replacement time. Your barber and service will stay the same."
      : "No other eligible starting times are available on this date.";
  } catch {
    if (attempt === changeAvailabilityAttempt && epoch === authEpoch) ui["change-availability-status"].textContent = "Replacement times could not be loaded. Try again.";
  }
}

function changeAppointment(kind) {
  if (!customer() || !management || !ui["change-confirm"].checked || hasPendingIntent() || mutationBusy) return;
  if (kind === "reschedule" && !changeSelected) return;
  return submitChange({
    userId: user.id, kind, method: kind === "cancel" ? "DELETE" : "PUT",
    path: `/appointments/${management.id}`, ifMatch: management.ifMatch, key: crypto.randomUUID(),
    body: kind === "cancel" ? null : { slotId: changeSelected.slotId },
    summary: kind === "cancel" ? `Cancel appointment ${management.id}.` : changeSelected.summary,
  });
}

async function submitChange(intent = null) {
  if (!user || !token || mutationBusy || pendingForUser()) return;
  let pending = pendingChangeForUser();
  if (!pending) {
    if (!validChangeIntent(intent) || intent.userId !== user.id) return;
    pending = intent;
    pendingChanges.set(user.id, pending);
    savePendingChanges();
  }
  const epoch = authEpoch;
  const attempt = ++mutationAttempt;
  mutationBusy = true;
  updateControls();
  ui["change-status"].textContent = `Waiting for the result: ${pending.summary}`;
  try {
    const headers = { Authorization: `Bearer ${token}`, "Idempotency-Key": pending.key, "If-Match": pending.ifMatch };
    if (pending.body !== null) headers["Content-Type"] = "application/json";
    const result = await request(pending.path, {
      method: pending.method, headers, ...(pending.body === null ? {} : { body: JSON.stringify(pending.body) }),
    });
    if (epoch !== authEpoch || attempt !== mutationAttempt) return;
    const expectedId = pending.path.split("/").at(-1);
    const validSuccess = result.status === 200 && result.body && (pending.kind === "availability"
      ? /^[a-f0-9]{64}$/.test(result.body.revision || "") &&
        pending.path === `/barbers/${result.body.barberId}/availability?date=${result.body.date}`
      : result.body.id === expectedId && Number.isInteger(result.body.version) && result.body.version >= 1 &&
        result.body.status === (pending.kind === "cancel" ? "cancelled" : "confirmed"));
    if (result.status >= 500 || !result.body || (result.ok && !validSuccess)) {
      ui["change-status"].textContent = "The change outcome is uncertain. Retry the same change with its preserved request and key.";
      return;
    }
    if (result.status === 401) { signOut("Your session expired. Log in again to retry the same pending change."); return; }
    if (result.status === 403) {
      ui["change-status"].textContent = `${safeMessage(result)} Use this request's authorized account before retrying.`;
      return;
    }
    pendingChanges.delete(pending.userId);
    savePendingChanges();
    closeManagement();
    ui["change-status"].textContent = result.ok
      ? "The change completed. Current appointments and availability are refreshed below."
      : `${safeMessage(result)} Refresh the current record before starting a new change.`;
    // A replay describes its historical outcome. Render fresh reads, rather
    // than treating its snapshot as the appointment's current state.
    if (customer()) await loadAppointments();
    else if (staff()) await Promise.allSettled([loadStaffAvailability(), loadStaffAppointments()]);
  } catch {
    if (epoch === authEpoch && attempt === mutationAttempt) {
      ui["change-status"].textContent = "The reply was not received. Retry the same change; its original request, version, and key are preserved.";
      if (!storageAvailable) ui["change-status"].textContent += " Keep this page open until the result is resolved.";
    }
  } finally {
    if (epoch === authEpoch && attempt === mutationAttempt) { mutationBusy = false; updateControls(); }
  }
}

function torontoTimestamp(value) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Toronto", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "longOffset",
  }).formatToParts(new Date(value)).filter(part => part.type !== "literal").map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${parts.timeZoneName.replace("GMT", "")}`;
}

function addStaffRange(startTime = "", endTime = "") {
  const number = ++staffRangeSequence;
  const row = document.createElement("div"); row.className = "range-row";
  const fields = document.createElement("div"); fields.className = "fields";
  const start = document.createElement("input"); start.id = `range-start-${number}`; start.type = "text"; start.maxLength = 40; start.value = startTime;
  const end = document.createElement("input"); end.id = `range-end-${number}`; end.type = "text"; end.maxLength = 40; end.value = endTime;
  const startLabel = document.createElement("label"); startLabel.htmlFor = start.id; startLabel.textContent = "Range start (with offset)"; startLabel.append(start);
  const endLabel = document.createElement("label"); endLabel.htmlFor = end.id; endLabel.textContent = "Range end (with offset)"; endLabel.append(end);
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = "Remove range";
  const entry = { node: row, start, end };
  remove.addEventListener("click", () => {
    staffRangeRows = staffRangeRows.filter(existing => existing !== entry);
    ui["staff-ranges"].replaceChildren(...staffRangeRows.map(existing => existing.node));
  });
  fields.append(startLabel, endLabel); row.append(fields, remove);
  staffRangeRows.push(entry); ui["staff-ranges"].append(row);
  start.disabled = end.disabled = Boolean(ui["staff-close-date"].checked);
}

function invalidateStaffSelection() {
  staffAvailabilityAttempt += 1;
  staffAppointmentsAttempt += 1;
  staffRevision = null;
  staffRangeRows = [];
  ui["staff-ranges"].replaceChildren();
  ui["staff-configured"].replaceChildren();
  ui["staff-appointments"].replaceChildren();
  ui["staff-availability-status"].textContent = "Selection changed. Load this date before editing availability.";
  ui["staff-appointments-status"].textContent = "";
  updateControls();
}

async function loadStaffAvailability(event) {
  if (event) event.preventDefault();
  const barberId = ui["staff-barber"].value;
  const date = ui["staff-date"].value;
  if (!staff() || !barberId || !date || !user.managedBarberIds?.includes(barberId)) return;
  const attempt = ++staffAvailabilityAttempt;
  const epoch = authEpoch;
  staffRevision = null;
  ui["staff-availability-status"].textContent = "Loading the configured schedule...";
  updateControls();
  try {
    const query = new URLSearchParams({ date });
    const result = await request(`/barbers/${barberId}/availability?${query}`, { headers: { Authorization: `Bearer ${token}` } });
    if (attempt !== staffAvailabilityAttempt || epoch !== authEpoch) return;
    if (result.status === 401) { signOut("Your session expired. Log in again to view staff schedules."); return; }
    if (!result.ok) { ui["staff-availability-status"].textContent = safeMessage(result); return; }
    if (!result.body || !Array.isArray(result.body.ranges) || !Array.isArray(result.body.blocks) ||
        !/^[a-f0-9]{64}$/.test(result.body.revision || "") || result.etag !== `"${result.body.revision}"`) throw new Error("Invalid schedule response");
    staffRevision = { value: result.etag, barberId, date };
    staffRangeRows = [];
    ui["staff-ranges"].replaceChildren();
    ui["staff-configured"].replaceChildren();
    ui["staff-close-date"].checked = result.body.ranges.length === 0;
    for (const range of result.body.ranges) {
      const item = document.createElement("li"); item.textContent = displayRange(range.startTime, range.endTime);
      ui["staff-configured"].append(item);
      addStaffRange(torontoTimestamp(range.startTime), torontoTimestamp(range.endTime));
    }
    if (result.body.ranges.length === 0) {
      const item = document.createElement("li"); item.textContent = "No available ranges are configured for this date.";
      ui["staff-configured"].append(item);
      addStaffRange();
    }
    const occupied = result.body.blocks.filter(block => block.occupied).length;
    ui["staff-availability-status"].textContent = `${occupied} occupied half-hour block(s). Review the ranges before replacing this date's schedule.`;
  } catch {
    if (attempt === staffAvailabilityAttempt && epoch === authEpoch) ui["staff-availability-status"].textContent = "The schedule could not be loaded. Try again.";
  } finally {
    if (attempt === staffAvailabilityAttempt && epoch === authEpoch) updateControls();
  }
}

function saveStaffAvailability() {
  if (!staff() || !staffRevision || hasPendingIntent() || mutationBusy ||
      staffRevision.barberId !== ui["staff-barber"].value || staffRevision.date !== ui["staff-date"].value) return;
  const ranges = ui["staff-close-date"].checked ? [] : staffRangeRows.map(entry => ({ startTime: entry.start.value, endTime: entry.end.value }));
  const offsetTime = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?[+-]\d{2}:\d{2}$/;
  if ((!ui["staff-close-date"].checked && ranges.length === 0) || ranges.some(range =>
      !offsetTime.test(range.startTime) || !offsetTime.test(range.endTime) ||
      Number.isNaN(Date.parse(range.startTime)) || Number.isNaN(Date.parse(range.endTime)))) {
    ui["staff-availability-status"].textContent = "Complete each range using a timestamp with an explicit offset, or select Close this date.";
    return;
  }
  const query = new URLSearchParams({ date: staffRevision.date });
  return submitChange({
    userId: user.id, kind: "availability", method: "PUT", path: `/barbers/${staffRevision.barberId}/availability?${query}`,
    ifMatch: staffRevision.value, key: crypto.randomUUID(), body: { ranges },
    summary: `Replace ${barberNames.get(staffRevision.barberId) || staffRevision.barberId}'s availability on ${staffRevision.date}.`,
  });
}

async function loadStaffAppointments() {
  const barberId = ui["staff-barber"].value;
  if (!staff() || !barberId || !user.managedBarberIds?.includes(barberId)) return;
  const attempt = ++staffAppointmentsAttempt;
  const epoch = authEpoch;
  ui["staff-appointments"].replaceChildren();
  ui["staff-appointments-status"].textContent = "Loading upcoming appointments...";
  try {
    const result = await request(`/barbers/${barberId}/appointments`, { headers: { Authorization: `Bearer ${token}` } });
    if (attempt !== staffAppointmentsAttempt || epoch !== authEpoch) return;
    if (result.status === 401) { signOut("Your session expired. Log in again to view staff appointments."); return; }
    if (!result.ok || !result.body || !Array.isArray(result.body.appointments)) {
      ui["staff-appointments-status"].textContent = result.ok ? "Appointment data could not be read." : safeMessage(result);
      return;
    }
    for (const appointment of result.body.appointments) {
      const item = document.createElement("li");
      const title = document.createElement("strong"); title.textContent = `${services[appointment.service]} · ${appointment.durationMinutes} minutes`;
      const detail = document.createElement("span"); detail.textContent = `${displayTime(appointment.startTime)} to ${displayTime(appointment.endTime)}`;
      const contact = document.createElement("span"); contact.textContent = appointment.customerEmail;
      const reference = document.createElement("small"); reference.textContent = appointment.id;
      item.append(title, detail, contact, reference); ui["staff-appointments"].append(item);
    }
    ui["staff-appointments-status"].textContent = result.body.appointments.length ? "Upcoming confirmed appointments:" : "No upcoming confirmed appointments for this barber.";
  } catch {
    if (attempt === staffAppointmentsAttempt && epoch === authEpoch) ui["staff-appointments-status"].textContent = "Appointments could not be loaded. Try again.";
  }
}

async function book() {
  if (!customer() || bookingBusy || pendingChangeForUser()) return;
  let pending = pendingForUser();
  if (!pending) {
    if (!selected) return;
    pending = {
      userId: user.id, key: crypto.randomUUID(),
      body: { slotId: selected.slotId, service: selected.service }, summary: selected.summary,
    };
    pendingBookings.set(user.id, pending);
    savePendingBookings();
  }
  const epoch = authEpoch;
  const attempt = ++bookingAttempt;
  const authorization = `Bearer ${token}`;
  bookingBusy = true;
  updateControls();
  ui["booking-status"].textContent = "Waiting for the booking result…";
  try {
    const result = await request("/appointments", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: authorization, "Idempotency-Key": pending.key },
      body: JSON.stringify(pending.body),
    });
    if (epoch !== authEpoch || attempt !== bookingAttempt) return;
    if (result.status >= 500 || !result.body || (result.ok && (result.status !== 201 || typeof result.body.id !== "string"))) {
      ui["booking-status"].textContent = "The booking outcome is uncertain. Retry the same booking to recover its result; your selection and retry key are preserved.";
      if (!storageAvailable) ui["booking-status"].textContent += " Keep this page open until it is resolved.";
      return;
    }
    if (result.status === 401) {
      signOut("Your session expired. Log in again to retry the same pending booking.");
      return;
    }
    if (result.status === 403) {
      ui["booking-status"].textContent = `${safeMessage(result)} Log in again with this booking's authorized customer account.`;
      return;
    }
    pendingBookings.delete(pending.userId);
    savePendingBookings();
    selected = null;
    clearSelection();
    ui["booking-status"].textContent = result.status === 201
      ? `Appointment confirmed: ${result.body.id} (original booking result). The current list below includes any later changes.`
      : `${safeMessage(result)} Choose a new available time for a new booking attempt.`;
    await Promise.allSettled([loadAppointments(), loadAvailability()]);
  } catch {
    if (epoch === authEpoch && attempt === bookingAttempt) {
      ui["booking-status"].textContent = "The reply was not received. The booking may have succeeded. Retry the same booking; its original selection and retry key are preserved.";
      if (!storageAvailable) ui["booking-status"].textContent += " Keep this page open until it is resolved.";
    }
  } finally {
    if (epoch === authEpoch && attempt === bookingAttempt) {
      bookingBusy = false;
      updateControls();
    }
  }
}

ui["login-form"].addEventListener("submit", login);
ui["logout-button"].addEventListener("click", () => signOut());
ui["availability-form"].addEventListener("submit", loadAvailability);
ui["appointments-button"].addEventListener("click", loadAppointments);
ui["book-button"].addEventListener("click", book);
ui["retry-button"].addEventListener("click", book);
ui["change-availability-form"].addEventListener("submit", loadReplacementTimes);
ui["change-confirm"].addEventListener("change", updateControls);
ui["reschedule-button"].addEventListener("click", () => changeAppointment("reschedule"));
ui["cancel-appointment-button"].addEventListener("click", () => changeAppointment("cancel"));
ui["close-management-button"].addEventListener("click", closeManagement);
ui["change-retry-button"].addEventListener("click", () => submitChange());
ui["staff-availability-form"].addEventListener("submit", loadStaffAvailability);
ui["staff-appointments-button"].addEventListener("click", loadStaffAppointments);
ui["staff-add-range-button"].addEventListener("click", () => addStaffRange());
ui["staff-save-button"].addEventListener("click", saveStaffAvailability);
ui["staff-close-date"].addEventListener("change", () => {
  for (const entry of staffRangeRows) entry.start.disabled = entry.end.disabled = ui["staff-close-date"].checked;
});
for (const control of [ui["staff-barber"], ui["staff-date"]]) control.addEventListener("change", invalidateStaffSelection);
ui["change-date"].addEventListener("change", () => {
  changeAvailabilityAttempt += 1;
  changeSelected = null;
  ui["change-slots"].replaceChildren();
  ui["change-slot-options"].hidden = true;
  ui["change-availability-status"].textContent = "Date changed. Find replacement times again.";
  updateControls();
});
for (const control of [ui.barber, ui.service, ui.date]) {
  control.addEventListener("change", () => {
    availabilityAttempt += 1;
    availabilityBusy = false;
    clearSelection();
    ui["availability-status"].textContent = "Preferences changed. Find availability again.";
  });
}
const today = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", year: "numeric", month: "2-digit", day: "2-digit",
}).formatToParts(new Date()).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
ui.date.value = new Date(Date.UTC(Number(today.year), Number(today.month) - 1, Number(today.day) + 1)).toISOString().slice(0, 10);
ui["staff-date"].value = ui.date.value;
restorePendingBookings();
restorePendingChanges();
updateControls();
loadBarbers();
