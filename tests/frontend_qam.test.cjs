const { assert, mount } = require('./legion_harness.cjs');

class TabsHook {
  tabs = [{ id: 999 }, { id: 519 }];
  add(tab) { this.tabs.push(tab); }
  removeById(id) { this.tabs = this.tabs.filter(tab => tab.id !== id); }
  render(entries) {
    if (entries.filter(tab => tab.decky).length !== this.tabs.length) {
      for (const tab of this.tabs) entries.push({ key: tab.id, decky: true });
    }
  }
}

const storage = new Map();
const hook = new TabsHook();
const window = { __TABS_HOOK_INSTANCE: hook,
  localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } };
const { exports: qam, fire } = mount('qam.tsx', { window });
const native = [{ key: 1 }, { key: 2 }, { key: 3 }];
const rendered = [...native];
hook.render(rendered);
const ids = (entries = rendered) => entries.map(tab => tab.key);
const originalTabs = [...rendered];
const register = () => qam.createQamShortcut({ title: 'Companion', content: 'content', icon: 'icon' });
let shortcut = register();
assert.equal(shortcut.state().enabled, false);
shortcut.set(true);
const id = hook.tabs.at(-1).id;
assert.equal(hook.tabs.at(-1).icon.props.children[0].props.children, `#quickaccess_tab_${id} { order: -1; }`);
assert.equal(shortcut.state().registered, true);
hook.render(rendered);
const entry = rendered.at(-1);
fire('interval');
hook.render(rendered);
assert.deepEqual(ids(), [1, 2, 3, 999, 519, id]);
assert.equal(rendered.at(-1), entry, 'reopening preserves the mounted panel');
assert.equal(rendered[3], originalTabs[3], 'Steam’s focus indices and the mounted Decky panel stay stable');
const fresh = [...native];
hook.render(fresh);
assert.deepEqual(ids(fresh), [1, 2, 3, 999, 519, id], 'fresh Quick Access preserves the native indices');
shortcut.set(false);
hook.render(rendered);
assert.deepEqual(ids(), [1, 2, 3, 999, 519]);
shortcut.set(true);
hook.render(rendered);
shortcut.dispose();
hook.render(rendered);
assert.deepEqual(ids(), [1, 2, 3, 999, 519]);
shortcut = register();
assert.equal(shortcut.state().enabled, true);
assert.equal(hook.tabs.filter(tab => tab.id === id).length, 1);
shortcut.dispose();
hook.render(rendered);
hook.add({ id });
shortcut = register();
assert.match(shortcut.state().error, /different plugin/);
shortcut.dispose();
assert.deepEqual(hook.tabs.map(tab => tab.id), [999, 519, id]);
