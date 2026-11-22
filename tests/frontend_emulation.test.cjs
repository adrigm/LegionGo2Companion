const { assert, mount, run, settle, label, text, button } = require('./legion_harness.cjs');

run(async () => {
  let steamList;
  const h = mount('emulation.tsx', { component: 'EmulationPage', deferAll: true, props: { visible: true },
    window: { SteamClient: { Input: { RegisterForControllerListChanges(callback) {
      steamList = callback;
      return { unregister() { steamList = undefined; } };
    } } } },
  });
  const state = { selected: 'system', active: 'steam', effective: 'system', reason: '', steam_menu: false,
    options: ['steam', 'xbox-elite', 'ds5', 'ds5-edge'], profiles: {}, running_app: '200', running_name: 'Example Game' };
  h.render();
  h.respond('emulation_get_status', state);
  await settle();
  const picker = () => label(h.render(), 'Default controller');
  assert.deepEqual([...picker().props.rgOptions].map(option => option.data), ['system', 'steam', 'xbox-elite', 'ds5', 'ds5-edge']);
  picker().props.onChange({ data: 'ds5' });
  assert.deepEqual(h.calls.at(-1).args, ['ds5', true]);
  h.respond('emulation_set_default', { ...state, selected: 'ds5', effective: 'ds5', waiting_for_qam: true });
  await settle();
  assert.match(text(h.render()), /Controller change pending/);
  label(h.render(), 'Controller for this game').props.onChange({ data: 'ds5-edge' });
  assert.deepEqual(h.calls.at(-1).args, ['200', 'Example Game', 'ds5-edge', false]);
  const profile = { mode: 'ds5-edge', enabled: false, name: 'Example Game' };
  const saved = { ...state, profiles: { 200: profile } };
  h.respond('emulation_set_profile', saved);
  await settle();
  label(h.render(), 'Apply profile automatically').props.onChange(true);
  assert.deepEqual(h.calls.at(-1).args, ['200', 'Example Game', 'ds5-edge', true]);
  h.respond('emulation_set_profile', { ...saved, active: 'ds5-edge', effective: 'ds5-edge' });
  await settle();
  assert.equal(picker().props.selectedOption, 'system', 'running profile does not change the default');
  steamList([]);
  assert.match(text(h.render()), /Steam does not list a matching PlayStation controller/);
  steamList([{ unVendorID: 0x054c, unProductID: 0x0df2 }]);
  assert.match(text(h.render()), /Steam reports a matching PlayStation controller/);
  assert.match(text(h.render()), /Y1 → left Fn/);
  button(h.render(), 'Test PlayStation sticks').props.onClick();
  h.respond('emulation_test_sticks', { mode: 'ds5-edge', physical: { reports: 12, span: [150, 150, 150, 150] },
    virtual: { reports: 12, span: [0, 0, 0, 0] } });
  await settle();
  assert.match(text(h.render()), /did not reach the virtual DualSense/);
  h.unmount();
  assert.equal(steamList, undefined);
});
