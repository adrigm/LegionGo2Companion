const { assert, mount, run, settle, label, text } = require('./legion_harness.cjs');

run(async () => {
  const h = mount('emulation.tsx', { component: 'EmulationPage', deferAll: true, props: { visible: true } });
  const state = { selected: 'system', active: 'steam', reason: '', selection_pending: false,
    options: ['steam', 'xbox-elite', 'ds5', 'ds5-edge'] };
  h.render();
  h.respond('emulation_get_status', state);
  await settle();
  const picker = () => label(h.render(), 'Emulated controller');
  assert.deepEqual([...picker().props.rgOptions].map(option => option.data), ['system', 'steam', 'xbox-elite', 'ds5', 'ds5-edge']);
  picker().props.onChange({ data: 'system' });
  assert.equal(h.calls.at(-1).name, 'emulation_get_status');
  picker().props.onChange({ data: 'ds5' });
  assert.deepEqual(h.calls.at(-1).args, ['ds5', true]);
  h.respond('emulation_set_default', { ...state, selected: 'ds5', selection_pending: true });
  await settle();
  assert.equal(picker().props.description, 'Close Quick Access to apply.');
  h.fire('interval');
  h.respond('emulation_get_status', { ...state, selected: 'ds5', active: 'ds5',
    profiles: { 200: { mode: 'ds5-edge', name: 'Example Game', enabled: true } }, steam_menu: true });
  await settle();
  assert.equal(picker().props.selectedOption, 'ds5');
  assert.equal(picker().props.description, undefined);
  assert.doesNotMatch(text(h.render()), /Game profiles|Steam Controller in Steam menus|Test PlayStation sticks|SteamDeck=0/);
  h.unmount();

  const router = { MainRunningApp: null };
  const lifecycle = mount('emulation.tsx', { ui: { Router: router } });
  const stop = lifecycle.exports.startEmulationWatcher();
  await settle();
  assert.deepEqual(lifecycle.calls.at(-1).args, [false, false]);
  router.MainRunningApp = { appid: 2322010 };
  lifecycle.fire('interval');
  await settle();
  assert.deepEqual(lifecycle.calls.at(-1).args, [false, true]);
  router.MainRunningApp = null;
  lifecycle.fire('interval');
  await settle();
  assert.deepEqual(lifecycle.calls.at(-1).args, [false, false]);
  stop();
});
