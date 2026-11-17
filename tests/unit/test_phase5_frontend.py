"""Customer/staff controls and safe retries for appointment-management intents."""

import ast
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def browser_harness() -> str:
    """Reuse the existing DOM/fetch substitute without copying its implementation."""
    module = ast.parse((ROOT / "tests/unit/test_frontend.py").read_text(encoding="utf-8"))
    original = next(
        ast.literal_eval(statement.value)
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "JAVASCRIPT_CHECK" for target in statement.targets)
    )
    return original.split("async function main() {", 1)[0]


CHANGE_SCENARIOS = r"""
function change(kind = 'reschedule') {
  return {userId:'usr_one', kind, method:kind === 'cancel' ? 'DELETE' : 'PUT', path:'/appointments/apt_one',
    ifMatch:'"3"', key:'change-one', body:kind === 'cancel' ? null : {slotId:'slot_two'}, summary:'Change my appointment'};
}
const appointment = {id:'apt_one', barberId:'bar_one', slotId:'slot_one', service:'both', durationMinutes:60,
  startTime:'2030-01-15T15:00:00Z', endTime:'2030-01-15T16:00:00Z', status:'confirmed', version:3};
function versioned(status, body, etag = '"3"') {
  const result = response(status, body); result.headers = {get: name => name === 'ETag' ? etag : null}; return result;
}
function chooseChange(app) {
  app.context.appointment = appointment;
  app.run(`management = {...appointment, ifMatch:'"3"'}; changeSelected = {slotId:'slot_two', summary:'New time with Alex'};
    ui['change-confirm'].checked = true; updateControls();`);
}
async function main() {
  const app = createContext();
  await new Promise(setImmediate);
  identify(app);
  if (scenario === 'change_restore') {
    app.context.intent = change();
    app.run('pendingChanges.set(user.id, intent); savePendingChanges(); updateControls();');
    const restored = createContext(); await new Promise(setImmediate); identify(restored);
    const pending = JSON.parse(restored.run('JSON.stringify(pendingChangeForUser())'));
    assert.deepEqual(pending, change());
    assert.equal(restored.nodes.get('change-retry-button').hidden, false);
    assert.equal(restored.nodes.get('book-button').disabled, true);
    assert.doesNotMatch(storage.get('bestapp.pendingChanges'), /private-token/);
    restored.run('signOut()'); identify(restored, 'usr_two');
    assert.equal(restored.nodes.get('change-retry-button').hidden, true);
  } else if (scenario === 'change_blocks_booking') {
    choose(app); app.context.intent = change();
    app.run('pendingChanges.set(user.id, intent); updateControls();');
    const count = app.calls.length;
    await app.run('book()'); await app.run('loadAvailability()');
    assert.equal(app.calls.length, count);
    assert.equal(app.nodes.get('browse-fields').disabled, true);
  } else if (scenario === 'reject_unsafe_restore') {
    const unsafe = change(); unsafe.path = 'https://untrusted.invalid/appointments/apt_one';
    storage.set('bestapp.pendingChanges', JSON.stringify([unsafe]));
    const restored = createContext(); await new Promise(setImmediate); identify(restored);
    assert.equal(restored.run('pendingChanges.size'), 0);
    assert.equal(restored.nodes.get('change-retry-button').hidden, true);
  } else if (scenario === 'time_only_choices') {
    app.queue.push(versioned(200, appointment)); await app.run("openAppointment('apt_one')");
    assert.equal(app.nodes.get('management-section').hidden, false);
    assert.match(app.nodes.get('management-summary').textContent, /Haircut and beard.*Alex/);
    assert.equal(app.nodes.get('cancel-appointment-button').disabled, true);
    app.nodes.get('change-confirm').checked = true; app.run('updateControls()');
    assert.equal(app.nodes.get('cancel-appointment-button').disabled, false);
    app.queue.push(response(200, {slots:[
      {slotId:'slot_one',startTime:appointment.startTime,endTime:appointment.endTime},
      {slotId:'slot_two',startTime:'2030-01-15T15:30:00Z',endTime:'2030-01-15T16:30:00Z'},
    ]}));
    await app.run('loadReplacementTimes()');
    assert.equal(app.nodes.get('change-slots').children.length, 1);
    const browse = app.calls.find(call => call.path.includes('/apt_one/availability?'));
    assert.equal(browse.path, '/appointments/apt_one/availability?date=2030-01-15');
    app.nodes.get('change-slots').children[0].children[0].listeners.change();
    app.queue.push(response(503, {error:{message:'Unavailable'}})); await app.run("changeAppointment('reschedule')");
    const mutation = app.calls.find(call => call.options.method === 'PUT');
    assert.deepEqual(JSON.parse(mutation.options.body), {slotId:'slot_two'});
    assert.equal(mutation.options.headers['If-Match'], '"3"');
  } else if (scenario === 'reschedule_retry') {
    chooseChange(app); app.queue.push(Promise.reject(new Error('lost reply')));
    await app.run("changeAppointment('reschedule')");
    const saved = JSON.parse(storage.get('bestapp.pendingChanges'))[0];
    app.queue.push(versioned(200, {...appointment,version:4}), response(200,{appointments:[]}));
    await app.run('submitChange()');
    const puts = app.calls.filter(call => call.options.method === 'PUT');
    assert.equal(puts.length, 2);
    for (const call of puts) {
      assert.equal(call.path, saved.path);
      assert.equal(call.options.headers['If-Match'], saved.ifMatch);
      assert.equal(call.options.headers['Idempotency-Key'], saved.key);
      assert.deepEqual(JSON.parse(call.options.body), saved.body);
    }
    assert.equal(app.run('pendingChanges.size'), 0);
    assert.equal(app.nodes.get('appointments').children.length, 0);
    assert.match(app.nodes.get('appointments-status').textContent, /no upcoming/);
  } else if (scenario === 'cancel_restore') {
    chooseChange(app); app.queue.push(response(503,{error:{message:'Unavailable'}}));
    await app.run("changeAppointment('cancel')");
    const saved = JSON.parse(storage.get('bestapp.pendingChanges'))[0];
    const restored = createContext(); await new Promise(setImmediate); identify(restored);
    restored.queue.push(versioned(200,{...appointment,status:'cancelled',version:4}), response(200,{appointments:[]}));
    await restored.run('submitChange()');
    const deletion = restored.calls.find(call => call.options.method === 'DELETE');
    assert.equal(deletion.options.body, undefined);
    assert.equal(deletion.options.headers['Content-Type'], undefined);
    assert.equal(deletion.options.headers['Idempotency-Key'], saved.key);
    assert.equal(deletion.options.headers['If-Match'], saved.ifMatch);
  } else if (scenario === 'expired_change') {
    chooseChange(app); app.queue.push(response(401,{error:{message:'Expired'}}));
    await app.run("changeAppointment('reschedule')");
    const saved = JSON.parse(storage.get('bestapp.pendingChanges'))[0];
    assert.equal(app.run('token'), null);
    assert.equal(app.nodes.get('change-retry-button').hidden, true);
    identify(app);
    assert.equal(app.run('pendingChangeForUser().key'), saved.key);
    app.queue.push(response(503,{error:{message:'Unavailable'}})); await app.run('submitChange()');
    const puts = app.calls.filter(call => call.options.method === 'PUT');
    assert.equal(puts[0].options.headers['Idempotency-Key'], puts[1].options.headers['Idempotency-Key']);
  } else if (scenario === 'management_account_race') {
    const old = deferred(); app.queue.push(old.promise);
    const first = app.run("openAppointment('apt_one')");
    app.run('signOut()'); identify(app, 'usr_two');
    old.resolve(versioned(200,appointment)); await first;
    assert.equal(app.run('management'), null);
    assert.equal(app.nodes.get('management-section').hidden, true);
  } else if (scenario === 'mutation_account_race') {
    chooseChange(app); const old = deferred(); app.queue.push(old.promise);
    const first = app.run("changeAppointment('reschedule')");
    app.run('signOut()'); identify(app, 'usr_two');
    old.resolve(versioned(200,{...appointment,version:4})); await first;
    assert.equal(app.run("pendingChanges.has('usr_one')"), true);
    assert.equal(app.run('pendingChangeForUser()'), undefined);
    assert.doesNotMatch(app.nodes.get('change-status').textContent, /completed/);
  } else if (scenario === 'replacement_race') {
    chooseChange(app); app.nodes.get('change-date').value = '2030-01-15';
    const old = deferred(); app.queue.push(old.promise); const first = app.run('loadReplacementTimes()');
    app.nodes.get('change-date').value = '2030-01-16'; app.nodes.get('change-date').listeners.change();
    app.queue.push(response(200,{slots:[]})); await app.run('loadReplacementTimes()');
    old.resolve(response(200,{slots:[{slotId:'slot_old',startTime:appointment.startTime,endTime:appointment.endTime}]})); await first;
    assert.equal(app.nodes.get('change-slots').children.length, 0);
  } else if (scenario === 'stale_change') {
    chooseChange(app);
    app.queue.push(response(412,{error:{code:'stale_version',message:'Reload current appointment'}}),response(200,{appointments:[]}));
    await app.run("changeAppointment('reschedule')");
    assert.equal(app.run('pendingChanges.size'), 0);
    assert.equal(app.run('management'), null);
    assert.match(app.nodes.get('change-status').textContent,/stale_version/);
  } else throw new Error('Unknown scenario');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("scenario", [
    "change_restore", "change_blocks_booking", "reject_unsafe_restore", "time_only_choices", "reschedule_retry",
    "cancel_restore", "expired_change", "management_account_race", "mutation_account_race", "replacement_race", "stale_change",
])
def test_management_state_machine(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node executable is unavailable for JavaScript state checks")
    result = subprocess.run(
        [node, "-e", browser_harness() + CHANGE_SCENARIOS, str(ROOT / "app/static/app.js"), scenario],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
