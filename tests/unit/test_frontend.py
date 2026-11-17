"""Exercise packaged HTTP assets and the browser's retry/account state machine."""

from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tomllib

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.interface.frontend import router


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as http:
        yield http


class PageControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = set()
        self.labels = set()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag in {"input", "select"}:
            self.controls.add(attributes.get("id"))
        if tag == "label":
            self.labels.add(attributes.get("for"))


def test_page_serves_labeled_controls_and_cutoff_before_confirmation(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    parser = PageControls()
    parser.feed(response.text)
    assert parser.controls <= parser.labels
    assert {"login-form", "availability-form", "book-button", "retry-button", "appointments"} <= parser.ids
    assert response.text.index('id="change-restriction"') < response.text.index('id="book-button"')
    assert "less than 24 hours ahead" in response.text
    assert 'lang="en"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'src="/static/app.js" defer' in response.text


@pytest.mark.parametrize("path,media", [
    ("/static/app.js", "text/javascript"),
    ("/static/style.css", "text/css"),
])
def test_assets_are_served_without_authentication(client, path, media):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert response.content


def test_frontend_routes_do_not_add_published_api_operations_or_expose_other_files(client):
    assert client.get("/openapi.json").json()["paths"] == {}
    assert client.get("/static/private.txt").status_code == 404


def test_frontend_assets_are_included_in_installed_package():
    settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = settings["tool"]["setuptools"]["package-data"]["app"]
    included = {path.name for pattern in patterns for path in (ROOT / "app").glob(pattern)}
    assert {"index.html", "app.js", "style.css"} <= included


# Node's VM supplies a small DOM/fetch substitute, not a browser. These checks
# exercise uncertain replies and account-switch races without adding a runtime
# dependency or exposing test hooks in the application JavaScript.
JAVASCRIPT_CHECK = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const storage = new Map();
function response(status, body) { return {status, ok: status >= 200 && status < 300, json: async () => body}; }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
class Element {
  constructor(tag = '') { this.tag = tag; this.value = ''; this.children = []; this.listeners = {}; this.textContent = ''; }
  append(...children) {
    this.children.push(...children);
    if (this.tag === 'select' && !this.value && children[0]) this.value = children[0].value;
  }
  replaceChildren(...children) { this.children = [...children]; this.textContent = ''; }
  addEventListener(event, listener) { this.listeners[event] = listener; }
}
function createContext() {
  const nodes = new Map();
  const queue = [];
  const calls = [];
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, new Element(['barber', 'service'].includes(id) ? 'select' : ''));
        return nodes.get(id);
      },
      createElement(tag) { return new Element(tag); },
    },
    sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)},
    crypto: require('node:crypto'), URLSearchParams, AbortController, setTimeout, clearTimeout,
    fetch(path, options) {
      calls.push({path, options});
      if (path === '/barbers') return Promise.resolve(response(200, {barbers: [{id:'bar_one', displayName:'Alex'}]}));
      assert.ok(queue.length, `Unexpected request ${path}`);
      return Promise.resolve(queue.shift());
    },
  });
  vm.runInContext(source, context);
  vm.runInContext("ui.service.value = 'haircut'", context);
  return {context, nodes, queue, calls, run: text => vm.runInContext(text, context)};
}
function identify(app, id = 'usr_one', role = 'customer') {
  app.run(`token = 'private-token'; user = {id: '${id}', role: '${role}'}; authEpoch += 1; updateControls();`);
}
function choose(app) {
  app.run("selected = {slotId:'slot_one', service:'both', summary:'60 minute appointment with Alex'}; updateControls();");
}
function list(id) {
  return response(200, {appointments:[{id, barberId:'bar_one', service:'both', durationMinutes:60, startTime:'2026-11-18T15:00:00Z', endTime:'2026-11-18T16:00:00Z'}]});
}
async function main() {
  const app = createContext();
  await new Promise(setImmediate);
  identify(app);
  if (scenario === 'retry') {
    choose(app);
    app.queue.push(response(503, {error:{message:'Unavailable', code:'service_unavailable'}}));
    await app.run('book()');
    assert.equal(app.nodes.get('browse-fields').disabled, true);
    assert.equal(app.nodes.get('retry-button').hidden, false);
    assert.equal(app.run('pendingBookings.size'), 1);
    app.queue.push(response(201, {id:'apt_one'}), list('apt_one'), response(200, {slots:[]}));
    await app.run('book()');
    const posts = app.calls.filter(call => call.options.method === 'POST');
    assert.equal(posts.length, 2);
    assert.equal(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
    assert.equal(posts[0].options.body, posts[1].options.body);
    assert.equal(app.run('pendingBookings.size'), 0);
    assert.match(app.nodes.get('booking-status').textContent, /confirmed: apt_one/);
    assert.equal(storage.get('bestapp.pendingBookings').includes('private-token'), false);
  } else if (scenario === 'account_list_race') {
    const old = deferred(); app.queue.push(old.promise);
    const first = app.run('loadAppointments()');
    app.run('signOut()'); identify(app, 'usr_two');
    app.queue.push(list('apt_two')); await app.run('loadAppointments()');
    old.resolve(list('apt_one')); await first;
    const visible = app.nodes.get('appointments').children.map(item => item.children[2].textContent).join(' ');
    assert.match(visible, /apt_two/); assert.doesNotMatch(visible, /apt_one/);
  } else if (scenario === 'account_booking_race') {
    choose(app); const old = deferred(); app.queue.push(old.promise);
    const first = app.run('book()');
    app.run('signOut()'); identify(app, 'usr_two'); choose(app);
    app.queue.push(response(503, {error:{message:'Unavailable'}})); await app.run('book()');
    old.resolve(response(201, {id:'apt_old'})); await first;
    assert.equal(app.run('pendingBookings.size'), 2);
    assert.match(app.nodes.get('booking-status').textContent, /uncertain/);
    assert.doesNotMatch(app.nodes.get('booking-status').textContent, /apt_old/);
  } else if (scenario === 'restore') {
    choose(app); app.queue.push(Promise.reject(new Error('lost reply'))); await app.run('book()');
    const original = app.run('pendingForUser().key');
    const reloaded = createContext(); await new Promise(setImmediate); identify(reloaded);
    assert.equal(reloaded.run('pendingForUser().key'), original);
    assert.equal(reloaded.nodes.get('retry-button').hidden, false);
    assert.equal(reloaded.run('token'), 'private-token');
    assert.doesNotMatch(storage.get('bestapp.pendingBookings'), /private-token/);
    reloaded.run('signOut()'); identify(reloaded, 'usr_two');
    assert.equal(reloaded.nodes.get('retry-button').hidden, true);
    assert.doesNotMatch(reloaded.nodes.get('selection').textContent, /Alex/);
  } else if (scenario === 'staff') {
    app.run('signOut()'); identify(app, 'usr_staff', 'staff'); choose(app);
    const count = app.calls.length;
    await app.run('book()'); await app.run('loadAppointments()');
    assert.equal(app.calls.length, count);
    assert.equal(app.nodes.get('book-button').disabled, true);
    assert.equal(app.nodes.get('appointments-button').disabled, true);
  } else if (scenario === 'availability_race') {
    const old = deferred(); app.queue.push(old.promise);
    const first = app.run('loadAvailability()');
    app.nodes.get('service').value = 'both';
    app.nodes.get('service').listeners.change();
    app.queue.push(response(200, {slots:[]})); await app.run('loadAvailability()');
    old.resolve(response(200, {slots:[{slotId:'slot_old', startTime:'2026-11-18T15:00:00Z', endTime:'2026-11-18T15:30:00Z'}]}));
    await first;
    assert.equal(app.nodes.get('slots').children.length, 0);
    assert.equal(app.nodes.get('slot-options').hidden, true);
  } else if (scenario === 'login') {
    app.run('signOut()');
    app.nodes.get('email').value = 'Customer@example.com';
    app.nodes.get('password').value = '  private password  ';
    app.queue.push(response(200, {accessToken:'sensitive-token',user:{id:'usr_one',role:'customer'}}), list('apt_one'));
    await app.run('login({preventDefault(){}})');
    const login = app.calls.find(call => call.path === '/auth/login');
    assert.deepEqual(JSON.parse(login.options.body), {email:'Customer@example.com',password:'  private password  '});
    assert.equal(app.nodes.get('password').value, '');
    assert.equal(app.run('token'), 'sensitive-token');
    assert.equal([...storage.values()].some(value => /sensitive-token|private password/.test(value)), false);
  } else if (scenario === 'login_availability_race') {
    const old = deferred(); app.queue.push(old.promise);
    const first = app.run('loadAvailability()');
    app.queue.push(response(200, {accessToken:'new-token',user:{id:'usr_one',role:'customer'}}), list('apt_one'));
    await app.run('login({preventDefault(){}})');
    old.resolve(response(200, {slots:[]})); await first;
    assert.equal(app.nodes.get('availability-button').disabled, false);
    assert.doesNotMatch(app.nodes.get('availability-status').textContent, /Checking availability/);
  } else if (scenario === 'auth_retry') {
    choose(app); app.queue.push(response(401, {error:{code:'unauthenticated',message:'Session expired'}}));
    await app.run('book()');
    const saved = JSON.parse(storage.get('bestapp.pendingBookings'))[0];
    assert.equal(app.run('token'), null);
    assert.equal(app.nodes.get('retry-button').hidden, true);
    identify(app);
    assert.equal(app.run('pendingForUser().key'), saved.key);
    assert.equal(app.nodes.get('retry-button').hidden, false);
  } else if (scenario === 'new_intent_after_rejection') {
    choose(app);
    app.queue.push(response(409, {error:{code:'slot_unavailable',message:'Unavailable'}}), response(200,{appointments:[]}), response(200,{slots:[]}));
    await app.run('book()');
    assert.equal(app.run('pendingBookings.size'), 0);
    choose(app); app.queue.push(response(503,{error:{message:'Unavailable'}})); await app.run('book()');
    const posts = app.calls.filter(call => call.options.method === 'POST');
    assert.notEqual(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
  } else if (scenario === 'timezone') {
    assert.equal(app.run("displayTime('2026-11-01T05:00:00Z')"), 'Nov 1, 2026, 01:00');
    assert.equal(app.run("displayTime('2026-11-01T06:00:00Z')"), 'Nov 1, 2026, 01:00');
  } else throw new Error('Unknown scenario');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("scenario", [
    "retry", "account_list_race", "account_booking_race", "restore", "staff", "availability_race", "login", "timezone",
    "login_availability_race", "auth_retry", "new_intent_after_rejection",
])
def test_browser_state_machine(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node executable is unavailable for JavaScript state checks")
    result = subprocess.run(
        [node, "-e", JAVASCRIPT_CHECK, str(ROOT / "app" / "static" / "app.js"), scenario],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
