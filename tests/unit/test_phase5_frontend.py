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
  } else throw new Error('Unknown scenario');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize("scenario", ["change_restore", "change_blocks_booking", "reject_unsafe_restore"])
def test_management_state_machine(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node executable is unavailable for JavaScript state checks")
    result = subprocess.run(
        [node, "-e", browser_harness() + CHANGE_SCENARIOS, str(ROOT / "app/static/app.js"), scenario],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
