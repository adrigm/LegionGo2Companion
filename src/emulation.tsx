// SPDX-License-Identifier: BSD-3-Clause
import { DropdownItem, findModuleExport, PanelSection, PanelSectionRow, Router } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { emulationOptions, type EmulationMode, type EmulationStatus } from "../desktop/native/controller.mjs";
import { errorText, rpc } from "./shared";
import { infoRow, statusPlaceholder } from "./ui";

const readStatus = rpc<[], EmulationStatus>("emulation_get_status");
const setDefault = rpc<[EmulationMode, boolean], EmulationStatus>("emulation_set_default");
const reportQam = rpc<[boolean, boolean | null, boolean | null], object>("emulation_report_qam");

type ControllerStore = { GetControllers(): unknown; GetController(index: number): unknown };
const isControllerStore = (value: unknown): value is ControllerStore => {
  const store = value as Partial<ControllerStore> | null;
  return !!store && typeof store === "object" && typeof store.GetControllers === "function" &&
    typeof store.GetController === "function";
};

function gameRunning(active: Map<number, Set<number>>, ended: Set<number>, lastLifetimeAt: number): boolean | null {
  try {
    const router = Router as typeof Router & { IsAnyAppRunning?(): boolean };
    const running = typeof router?.IsAnyAppRunning === "function" ? router.IsAnyAppRunning() : null;
    if (active.size) {
      if (running !== false || Date.now() - lastLifetimeAt < 12000) return true;
      active.clear(); // A missed end notification must not suppress menu recovery indefinitely.
    }
    if (running === false && Date.now() - lastLifetimeAt >= 12000) ended.clear();
    if (ended.size) {
      const apps = router?.RunningApps;
      if (Array.isArray(apps)) return apps.some(app => !ended.has(Number(app.appid)));
      const id = Number(router?.MainRunningApp?.appid);
      if (id) return !ended.has(id);
      return false;
    }
    if (running !== null) return running;
    if (router && "MainRunningApp" in router) return !!router.MainRunningApp?.appid;
  } catch { /* SteamUI can be between instances. */ }
  return null;
}

const visibleQamViews = new Set<symbol>();
let publishQam: (() => void) | undefined;

function qamOpen(): boolean {
  try {
    type SteamWindow = { MenuStore?: { GetOpenSideMenu?(): number } };
    const router = Router as unknown as {
      GetFocusedWindowInstance?(): SteamWindow | null;
      WindowStore?: { GamepadUIMainWindowInstance?: SteamWindow };
    };
    const instance = router?.GetFocusedWindowInstance?.() ?? router?.WindowStore?.GamepadUIMainWindowInstance;
    const menu = instance?.MenuStore?.GetOpenSideMenu?.();
    if (typeof menu === "number") return menu === 2; // Steam's SideMenu.QuickAccess
  } catch { /* Use Decky's mounted views while the Steam window is unavailable. */ }
  return visibleQamViews.size > 0;
}

export function useEmulationQamVisibility(visible: boolean) {
  useEffect(() => {
    if (!visible) return;
    const view = Symbol();
    visibleQamViews.add(view);
    publishQam?.();
    return () => { visibleQamViews.delete(view); publishQam?.(); };
  }, [visible]);
}

export function startEmulationWatcher() {
  let pending = false;
  let dirty = false;
  let stopped = false;
  let store: ControllerStore | undefined;
  let nextLookup = 0;
  let nativeList: boolean | null = null;
  const sessions = new Map<number, Set<number>>();
  const ended = new Set<number>();
  let lastLifetimeAt = 0;
  const connected = () => {
    if (nativeList !== null) return nativeList;
    try {
      if (!store && Date.now() >= nextLookup) {
        nextLookup = Date.now() + 30000;
        store = findModuleExport(isControllerStore) as ControllerStore | undefined;
      }
      const list = store?.GetControllers();
      if (Array.isArray(list)) return list.length > 0;
    } catch { store = undefined; }
    return nativeList;
  };
  const publish = async () => {
    if (stopped) return;
    if (pending) { dirty = true; return; }
    dirty = false;
    pending = true;
    try {
      const inGame = gameRunning(sessions, ended, lastLifetimeAt);
      await reportQam(qamOpen(), inGame, inGame === false ? connected() : null);
    } catch {
      // The heartbeat retries after Decky reconnects.
    } finally {
      pending = false;
      if (dirty) void publish();
    }
  };
  const notify = () => void publish();
  const subscriptions: (SteamUnregister | undefined)[] = [];
  try {
    subscriptions.push(window.SteamClient?.Input?.RegisterForControllerListChanges?.(list => {
      nativeList = Array.isArray(list) ? list.length > 0 : null;
      notify();
    }));
  } catch { /* Older clients only expose the SteamUI store. */ }
  try {
    subscriptions.push(window.SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.(event => {
      if (!event.unAppID || typeof event.bRunning !== "boolean") return;
      const id = event.unAppID;
      lastLifetimeAt = Date.now();
      if (event.bRunning) {
        ended.delete(id);
        const instances = sessions.get(id) ?? new Set<number>();
        instances.add(event.nInstanceID);
        sessions.set(id, instances);
      } else {
        const instances = sessions.get(id);
        instances?.delete(event.nInstanceID);
        if (!instances?.size) { sessions.delete(id); ended.add(id); }
      }
      notify();
    }));
  } catch { /* The heartbeat still samples the running applications. */ }
  publishQam = notify;
  const heartbeat = setInterval(notify, 3000);
  notify();
  return () => {
    stopped = true;
    clearInterval(heartbeat);
    for (const sub of subscriptions) {
      if (typeof sub === "function") sub();
      else sub?.unregister();
    }
    if (publishQam === notify) publishQam = undefined;
  };
}

export function EmulationPage({ visible }: { visible: boolean }) {
  const [status, setStatus] = useState<EmulationStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(false);
  const pending = useRef(false);
  const revision = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const poll = async () => {
    if (pending.current) return;
    const current = ++revision.current;
    try {
      const next = await readStatus();
      if (mounted.current && revision.current === current) setStatus(next);
    } catch (failure) {
      if (mounted.current && revision.current === current) setError(errorText(failure));
    }
  };
  useEffect(() => {
    if (!visible) return;
    void poll();
    const timer = setInterval(() => void poll(), 2500);
    return () => { clearInterval(timer); revision.current += 1; };
  }, [visible]);

  const change = async (mode: EmulationMode) => {
    if (pending.current) return;
    pending.current = true;
    revision.current += 1;
    setBusy(true);
    setError("");
    try {
      const next = await setDefault(mode, true);
      if (mounted.current) setStatus(next);
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  if (!status) return statusPlaceholder({ title: "Controller emulation", label: "Status unavailable", error,
    onRetry: () => { setError(""); void poll(); } });

  const options = emulationOptions(status).map(({ id, label }) => ({
    data: id, label, disabled: id !== "system" && !status.options.includes(id),
  }));
  return <PanelSection title="Controller">
    {(error || status.reason) && infoRow("Controller emulation", error || status.reason)}
    <PanelSectionRow><DropdownItem label="Emulated controller" strDefaultLabel="System default"
      selectedOption={status.selected} rgOptions={options} disabled={busy || !status.active}
      description={status.selection_pending ? "Close Quick Access to apply." : undefined}
      onChange={option => {
        const choice = options.find(item => !item.disabled && item.data === option.data);
        if (choice && choice.data !== status.selected) void change(choice.data);
      }} /></PanelSectionRow>
  </PanelSection>;
}
