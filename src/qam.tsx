// SPDX-License-Identifier: BSD-3-Clause
import { PanelSectionRow, ToggleField } from "@decky/ui";
import { type ReactNode, useEffect, useState } from "react";

type Tab = { id: number; title: ReactNode; content: ReactNode; icon: ReactNode; owner?: string };
type RenderedTab = { key?: number | string; decky?: boolean };
type TabsHook = {
  tabs: Tab[];
  add(tab: Tab): void;
  removeById(id: number): void;
  render?(tabs: RenderedTab[], visible: boolean): unknown;
};
const OWNER = "legiongo2companion";
const TAB_ID = 0x4c4732;
const DECKY_TAB_ID = 999;
const PREFERENCE = `${OWNER}.qamShortcut`;
type State = { enabled: boolean; registered: boolean; error?: string };

function tabsHook() {
  const hook = (window as Window & { __TABS_HOOK_INSTANCE?: TabsHook }).__TABS_HOOK_INSTANCE;
  return hook && Array.isArray(hook.tabs) && typeof hook.add === "function" && typeof hook.removeById === "function" ? hook : undefined;
}

// Some Decky builds reuse the rendered array and compare only the number of Decky tabs.
// Reconcile our entry before that comparison, then move it ahead of the native tabs.
function reconcileCachedTab(hook: TabsHook, tab: Tab) {
  const render = hook.render;
  if (typeof render !== "function" || Object.prototype.hasOwnProperty.call(hook, "render")) return;
  let disposed = false;
  const isShortcut = (entry: RenderedTab) => entry?.decky && String(entry.key) === String(tab.id);
  const wrapped: NonNullable<TabsHook["render"]> = function (this: TabsHook, entries, visible) {
    const enabled = this.tabs.some(entry => entry.id === tab.id && entry.owner === OWNER);
    if (!enabled) {
      for (let i = entries.length - 1; i >= 0; i--) if (isShortcut(entries[i])) entries.splice(i, 1);
    } else if (entries.some(entry => entry?.decky) && !entries.some(isShortcut)) {
      const next: RenderedTab[] = [];
      render.call(this, next, visible);
      const shortcut = next.find(isShortcut);
      if (shortcut) entries.push(shortcut);
    }
    if (!enabled && disposed && hook.render === wrapped) delete hook.render;
    const result = render.call(this, entries, visible);
    if (enabled) {
      const index = entries.findIndex(isShortcut);
      if (index > 0) entries.unshift(entries.splice(index, 1)[0]);
    }
    return result;
  };
  hook.render = wrapped;
  return () => { disposed = true; };
}

export function createQamShortcut(view: Omit<Tab, "id" | "owner">) {
  const tab = {
    ...view, id: TAB_ID, owner: OWNER,
    // Another plugin may already own Decky's renderer. Keep our visual order in that case.
    icon: <><style>{`#quickaccess_tab_${TAB_ID} { order: -1; }`}</style>{view.icon}</>,
  };
  let state: State = { enabled: false, registered: false };
  const listeners = new Set<(next: State) => void>();
  let registeredWith: TabsHook | undefined;
  let releaseRender: (() => void) | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  try { state.enabled = window.localStorage.getItem(PREFERENCE) === "true"; } catch { /* optional Steam UI storage */ }

  const publish = (next: State) => {
    if (state.enabled === next.enabled && state.registered === next.registered && state.error === next.error) return;
    state = next;
    listeners.forEach(listener => listener(next));
  };
  const remove = (hook: TabsHook | undefined) => {
    if (!hook) return;
    const matches = hook.tabs.filter(existing => existing.id === TAB_ID);
    if (matches.length && matches.every(existing => existing.owner === OWNER)) hook.removeById(TAB_ID);
  };
  const sync = () => {
    try {
      const hook = tabsHook();
      if (registeredWith && registeredWith !== hook) {
        remove(registeredWith);
        releaseRender?.();
        releaseRender = undefined;
      }
      registeredWith = hook;
      if (!state.enabled) {
        remove(hook);
        publish({ enabled: false, registered: false });
        return;
      }
      if (!hook?.tabs.some(existing => existing.id === DECKY_TAB_ID)) {
        publish({ enabled: true, registered: false, error: "Waiting for Decky's Quick Access tab support." });
        return;
      }
      const matches = hook.tabs.filter(existing => existing.id === TAB_ID);
      if (matches.some(existing => existing.owner !== OWNER)) {
        publish({ enabled: true, registered: false, error: "A different plugin already uses this shortcut ID." });
        return;
      }
      if (matches.length !== 1 || matches[0] !== tab) {
        remove(hook);
        releaseRender ??= reconcileCachedTab(hook, tab);
        hook.add(tab);
      }
      const registered = hook.tabs.includes(tab) && hook.tabs.some(existing => existing.id === DECKY_TAB_ID);
      publish({ enabled: true, registered, error: registered ? undefined : "Decky did not register the shortcut." });
    } catch {
      publish({ enabled: state.enabled, registered: false, error: "Decky could not update the shortcut. Reopen Quick Access and try again." });
    }
  };
  const run = () => {
    clearInterval(timer);
    timer = state.enabled ? setInterval(sync, 5000) : undefined;
    sync();
  };
  run();
  return {
    state: () => state,
    watch: (listener: (next: State) => void) => {
      listeners.add(listener);
      listener(state);
      return () => { listeners.delete(listener); };
    },
    set(enabled: boolean) {
      try { window.localStorage.setItem(PREFERENCE, String(enabled)); }
      catch {
        publish({ ...state, error: "Steam could not save the shortcut preference." });
        return;
      }
      publish({ ...state, enabled });
      run();
    },
    dispose() {
      clearInterval(timer);
      try { remove(registeredWith ?? tabsHook()); } catch { /* the Steam window may already be closing */ }
      releaseRender?.();
      listeners.clear();
    },
  };
}

export function QamShortcutSetting({ shortcut }: { shortcut: ReturnType<typeof createQamShortcut> }) {
  const [value, setValue] = useState(shortcut.state);
  useEffect(() => shortcut.watch(setValue), [shortcut]);
  return <PanelSectionRow><ToggleField label="Show Companion in Quick Access" checked={value.enabled}
    description={value.error || (value.registered ? "Shortcut added. Reopen Quick Access to see it." : "Add Companion at the top of Quick Access.")}
    onChange={checked => shortcut.set(checked)} /></PanelSectionRow>;
}
