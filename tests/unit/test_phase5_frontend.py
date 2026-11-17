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
const revision = 'a'.repeat(64);
function staffSchedule(currentRevision = revision, barberId = 'bar_one', ranges = null) {
  return {barberId,date:'2030-01-15',timeZone:'America/Toronto',revision:currentRevision,
    ranges:ranges === null ? [{startTime:'2030-01-15T15:00:00Z',endTime:'2030-01-15T16:00:00Z'}] : ranges,
    blocks:[{slotId:'slot_one',startTime:'2030-01-15T15:00:00Z',endTime:'2030-01-15T15:30:00Z',enabled:true,occupied:true}]};
}
function staffIdentity(app, barbers = ['bar_one']) {
  identify(app, 'usr_staff', 'staff'); app.context.assigned = barbers;
  app.run("user.managedBarberIds = assigned; configureStaffBarbers(); ui['staff-date'].value = '2030-01-15';");
}
async function loadSchedule(app) {
  staffIdentity(app);
  app.queue.push(versioned(200,staffSchedule(),`"${revision}"`));
  await app.run('loadStaffAvailability()');
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
  } else if (scenario === 'staff_editor') {
    await loadSchedule(app);
    assert.equal(app.nodes.get('staff-barber').children.length,1);
    assert.equal(app.nodes.get('staff-editor').disabled,false);
    assert.equal(app.run('staffRangeRows[0].start.value'),'2030-01-15T10:00:00-05:00');
    assert.equal(app.run('staffRangeRows[0].end.value'),'2030-01-15T11:00:00-05:00');
    app.queue.push(response(503,{error:{message:'Unavailable'}})); await app.run('saveStaffAvailability()');
    const mutation = app.calls.find(call => call.options.method === 'PUT');
    assert.equal(mutation.options.headers['If-Match'],`"${revision}"`);
    assert.deepEqual(JSON.parse(mutation.options.body),{ranges:[{startTime:'2030-01-15T10:00:00-05:00',endTime:'2030-01-15T11:00:00-05:00'}]});
    assert.equal(app.nodes.get('staff-date').disabled,true);
  } else if (scenario === 'staff_close_retry') {
    await loadSchedule(app); app.nodes.get('staff-close-date').checked = true;
    app.queue.push(Promise.reject(new Error('lost reply'))); await app.run('saveStaffAvailability()');
    const saved = JSON.parse(storage.get('bestapp.pendingChanges'))[0];
    assert.deepEqual(saved.body,{ranges:[]});
    const restored = createContext(); await new Promise(setImmediate); staffIdentity(restored);
    const current = staffSchedule('b'.repeat(64),'bar_one',[]);
    restored.queue.push(versioned(200,current,`"${current.revision}"`),versioned(200,current,`"${current.revision}"`),response(200,{appointments:[]}));
    await restored.run('submitChange()');
    const mutation = restored.calls.find(call => call.options.method === 'PUT');
    assert.equal(mutation.options.headers['Idempotency-Key'],saved.key);
    assert.equal(mutation.options.headers['If-Match'],saved.ifMatch);
    assert.deepEqual(JSON.parse(mutation.options.body),{ranges:[]});
    assert.equal(restored.run('pendingChanges.size'),0);
    assert.equal(restored.run('staffRevision.value'),`"${current.revision}"`);
  } else if (scenario === 'staff_occupied_rejection') {
    await loadSchedule(app); app.nodes.get('staff-close-date').checked = true;
    app.queue.push(response(409,{error:{code:'availability_occupied',message:'Existing appointment occupies this range'}}),
      versioned(200,staffSchedule(),`"${revision}"`),response(200,{appointments:[]}));
    await app.run('saveStaffAvailability()');
    assert.equal(app.run('pendingChanges.size'),0);
    assert.equal(app.run('staffRevision.value'),`"${revision}"`);
    assert.match(app.nodes.get('change-status').textContent,/availability_occupied/);
    assert.equal(app.nodes.get('staff-close-date').checked,false);
  } else if (scenario === 'staff_selection_race') {
    staffIdentity(app,['bar_one','bar_two']);
    const old = deferred(); app.queue.push(old.promise); const first = app.run('loadStaffAvailability()');
    app.nodes.get('staff-barber').value = 'bar_two'; app.nodes.get('staff-barber').listeners.change();
    app.queue.push(versioned(200,staffSchedule('b'.repeat(64),'bar_two'),`"${'b'.repeat(64)}"`));
    await app.run('loadStaffAvailability()');
    old.resolve(versioned(200,staffSchedule(),`"${revision}"`)); await first;
    assert.equal(app.run('staffRevision.barberId'),'bar_two');
    assert.equal(app.run('staffRevision.value'),`"${'b'.repeat(64)}"`);
  } else if (scenario === 'staff_list_account_race') {
    staffIdentity(app); const old = deferred(); app.queue.push(old.promise); const first = app.run('loadStaffAppointments()');
    app.run('signOut()'); identify(app,'usr_two');
    old.resolve(response(200,{appointments:[{...appointment,customerEmail:'private@example.com'}]})); await first;
    assert.equal(app.nodes.get('staff-appointments').children.length,0);
  } else if (scenario === 'staff_unassigned') {
    staffIdentity(app,[]); app.nodes.get('staff-barber').value = 'bar_one';
    const count = app.calls.length;
    await app.run('loadStaffAvailability()'); await app.run('loadStaffAppointments()');
    assert.equal(app.calls.length,count);
    assert.match(app.nodes.get('staff-availability-status').textContent,/No barbers/);
  } else if (scenario === 'staff_missing_offset') {
    await loadSchedule(app); app.run("staffRangeRows[0].start.value = '2030-01-15T10:00:00'");
    const count = app.calls.length; await app.run('saveStaffAvailability()');
    assert.equal(app.calls.length,count);
    assert.equal(app.run('pendingChanges.size'),0);
    assert.match(app.nodes.get('staff-availability-status').textContent,/explicit offset/);
  } else if (scenario === 'staff_dst_offsets') {
    assert.equal(app.run("torontoTimestamp('2026-11-01T05:00:00Z')"),'2026-11-01T01:00:00-04:00');
    assert.equal(app.run("torontoTimestamp('2026-11-01T06:00:00Z')"),'2026-11-01T01:00:00-05:00');
    assert.equal(app.run("torontoTimestamp('2026-03-08T07:00:00Z')"),'2026-03-08T03:00:00-04:00');
  } else if (scenario === 'missing_restored_key') {
    const incomplete = change(); delete incomplete.key;
    storage.set('bestapp.pendingChanges',JSON.stringify([incomplete]));
    const restored = createContext(); await new Promise(setImmediate); identify(restored);
    assert.equal(restored.run('pendingChanges.size'),0);
    assert.equal(restored.nodes.get('change-retry-button').hidden,true);
  } else if (scenario === 'unrelated_success') {
    chooseChange(app); app.queue.push(versioned(200,{...appointment,id:'apt_other',version:4}));
    await app.run("changeAppointment('reschedule')");
    assert.equal(app.run('pendingChanges.size'),1);
    assert.match(app.nodes.get('change-status').textContent,/uncertain/);
    assert.equal(app.calls.filter(call => call.path === '/appointments').length,0);
  } else if (scenario === 'wrong_cancel_state') {
    chooseChange(app); app.queue.push(versioned(200,{...appointment,version:4}));
    await app.run("changeAppointment('cancel')");
    assert.equal(app.run('pendingChanges.size'),1);
    assert.match(app.nodes.get('change-status').textContent,/uncertain/);
  } else if (scenario === 'wrong_staff_target') {
    await loadSchedule(app);
    app.queue.push(versioned(200,staffSchedule('b'.repeat(64),'bar_two'),`"${'b'.repeat(64)}"`));
    await app.run('saveStaffAvailability()');
    assert.equal(app.run('pendingChanges.size'),1);
    assert.match(app.nodes.get('change-status').textContent,/uncertain/);
  } else if (scenario === 'revoked_permission') {
    chooseChange(app); app.queue.push(response(403,{error:{code:'forbidden',message:'Permission changed'}}));
    await app.run("changeAppointment('reschedule')");
    const pending = JSON.parse(app.run('JSON.stringify(pendingChangeForUser())'));
    assert.equal(pending.ifMatch,'"3"');
    assert.equal(app.nodes.get('change-retry-button').hidden,false);
    assert.match(app.nodes.get('change-status').textContent,/authorized account/);
  } else throw new Error('Unknown scenario');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("scenario", [
    "change_restore", "change_blocks_booking", "reject_unsafe_restore", "time_only_choices", "reschedule_retry",
    "cancel_restore", "expired_change", "management_account_race", "mutation_account_race", "replacement_race", "stale_change",
    "staff_editor", "staff_close_retry", "staff_occupied_rejection", "staff_selection_race", "staff_list_account_race",
    "staff_unassigned", "staff_missing_offset", "staff_dst_offsets",
    "missing_restored_key", "unrelated_success", "wrong_cancel_state", "wrong_staff_target", "revoked_permission",
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
