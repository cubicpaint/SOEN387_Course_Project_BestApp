"use strict";

const element = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "login-form", "login-button", "email", "password", "signed-in", "identity", "logout-button", "account-status",
  "availability-form", "browse-fields", "barber", "service", "date", "availability-button", "availability-status",
  "slot-options", "slots", "selection", "book-button", "retry-button", "booking-status",
  "appointments-button", "appointments-status", "appointments",
].map((id) => [id, element(id)]));
const services = { haircut: "Haircut", beard: "Beard", both: "Haircut and beard" };
const barberNames = new Map();
const pendingBookings = new Map();
const storageKey = "bestapp.pendingBookings";
const timeFormat = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "longOffset",
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

function summary(slot, service, barberName) {
  return `${services[service]} (${service === "both" ? 60 : 30} minutes) with ${barberName}: ${displayTime(slot.startTime)} to ${displayTime(slot.endTime)}.`;
}

function updateControls() {
  const pending = pendingForUser();
  ui["browse-fields"].disabled = Boolean(pending);
  ui["availability-button"].disabled = availabilityBusy || barberNames.size === 0;
  ui["slot-options"].disabled = Boolean(pending);
  ui["book-button"].disabled = !customer() || !selected || Boolean(pending) || bookingBusy;
  ui["book-button"].hidden = Boolean(pending);
  ui["retry-button"].hidden = !pending;
  ui["retry-button"].disabled = !customer() || bookingBusy;
  ui["appointments-button"].disabled = !customer();
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
    return { status: response.status, ok: response.ok, body };
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
      : "This is a staff account. Booking and viewing personal appointments require a customer account.";
    ui.appointments.replaceChildren();
    if (pendingForUser()) {
      ui["booking-status"].textContent = "A previous booking needs a definite result. Retry the same booking before choosing a new one.";
    } else ui["booking-status"].textContent = "";
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
    updateControls();
  }
}

async function loadAvailability(event) {
  if (event) event.preventDefault();
  if (pendingForUser() || !ui.barber.value || !ui.date.value) return;
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
      text.textContent = `${displayTime(slot.startTime)} to ${displayTime(slot.endTime)}`;
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
      detail.textContent = `${barberNames.get(appointment.barberId) || appointment.barberId} · ${displayTime(appointment.startTime)} to ${displayTime(appointment.endTime)}`;
      const reference = document.createElement("small");
      reference.textContent = `Confirmed · ${appointment.id}`;
      item.append(title, detail, reference);
      ui.appointments.append(item);
    }
    ui["appointments-status"].textContent = result.body.appointments.length ? "Your confirmed upcoming appointments:" : "You have no upcoming confirmed appointments.";
  } catch {
    if (attempt === appointmentsAttempt && epoch === authEpoch) ui["appointments-status"].textContent = "Appointments could not be loaded. Try refreshing them.";
  }
}

async function book() {
  if (!customer() || bookingBusy) return;
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
      ? `Appointment confirmed: ${result.body.id}.`
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
restorePendingBookings();
updateControls();
loadBarbers();
