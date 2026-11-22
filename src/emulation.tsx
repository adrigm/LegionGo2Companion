// SPDX-License-Identifier: BSD-3-Clause
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { EDGE_BUTTONS, EMULATION_LABELS, emulationNote, emulationOptions, isPlayStation,
  type EmulationMode, type EmulationStatus } from "../desktop/native/controller.mjs";
import { errorText, rpc, runningApp, watchRunningApp, type RunningApp } from "./shared";
import { infoRow, statusPlaceholder } from "./ui";

const readStatus = rpc<[], EmulationStatus>("emulation_get_status");
const setDefault = rpc<[EmulationMode], EmulationStatus>("emulation_set_default");
const setProfile = rpc<[string, string, EmulationMode, boolean], EmulationStatus>("emulation_set_profile");
const deleteProfile = rpc<[string], EmulationStatus>("emulation_delete_profile");
const setSteamMenu = rpc<[boolean], EmulationStatus>("emulation_set_steam_menu");
const setSteamDiscovery = rpc<[boolean], EmulationStatus>("emulation_set_steam_discovery");
const refreshSteam = rpc<[], EmulationStatus>("emulation_refresh_steam_detection");
const reportApp = rpc<[string, string], object>("emulation_report_app");

export function startEmulationWatcher() {
  let pending = false;
  let last = "";
  let sentAt = -Infinity;
  const check = async (app: RunningApp | null) => {
    const id = app ? String(app.appid) : "";
    const name = app?.display_name || id;
    const key = `${id}\n${name}`;
    const now = Date.now();
    if (pending || (key === last && now - sentAt < 6000)) return;
    pending = true;
    try {
      await reportApp(id, name);
      last = key;
      sentAt = now;
    } catch {
      // The backend falls back to Steam's game processes when the report expires.
    } finally { pending = false; }
  };
  return watchRunningApp({ check, resume: () => { sentAt = -Infinity; void check(runningApp()); } });
}

type SteamController = { unVendorID?: number; unProductID?: number };
type ControllerListInput = {
  RegisterForControllerListChanges?: (callback: (list: SteamController[]) => void) => SteamUnregister;
  ShowControllerSettings?: () => void;
};
const steamInput = () => (window.SteamClient as (typeof SteamClient & { Input?: ControllerListInput }) | undefined)?.Input;

function useSteamControllerList(visible: boolean) {
  const [controllers, setControllers] = useState<SteamController[] | null>(null);
  useEffect(() => {
    if (!visible) return;
    try {
      const subscription = steamInput()?.RegisterForControllerListChanges?.(list => {
        if (Array.isArray(list)) setControllers(list);
      });
      return () => {
        if (typeof subscription === "function") subscription();
        else subscription?.unregister();
        setControllers(null);
      };
    } catch { return; }
  }, [visible]);
  return controllers;
}

export function EmulationPage({ visible }: { visible: boolean }) {
  const [status, setStatus] = useState<EmulationStatus | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedApp, setSelectedApp] = useState("");
  const mounted = useRef(false);
  const pending = useRef(false);
  const revision = useRef(0);
  const controllers = useSteamControllerList(visible);
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

  const change = async (action: () => Promise<EmulationStatus>, confirmed = "") => {
    if (pending.current) return;
    pending.current = true;
    revision.current += 1;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const next = await action();
      if (mounted.current) { setStatus(next); setMessage(confirmed); }
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  if (!status) return statusPlaceholder({ title: "Controller emulation", label: "Status unavailable", error,
    onRetry: () => { setError(""); void poll(); } });

  const games = Object.fromEntries(Object.entries(status.profiles).map(([id, profile]) => [id, profile.name]));
  if (status.running_app && (!games[status.running_app] || status.running_name !== status.running_app)) {
    games[status.running_app] = status.running_name || status.running_app;
  }
  const app = (selectedApp && Object.prototype.hasOwnProperty.call(games, selectedApp) ? selectedApp : "") || status.running_app || Object.keys(games)[0];
  const profile = app ? status.profiles[app] : undefined;
  const currentMode = profile?.mode ?? status.selected;
  const save = (mode: EmulationMode, enabled: boolean) => {
    if (app) void change(() => setProfile(app, games[app], mode, enabled));
  };
  const modeChoices = (saved: EmulationMode) => emulationOptions(status, saved).map(({ id, label }) => ({
    data: id, label, disabled: id !== "system" && !status.options.includes(id),
  }));
  const validChoice = (choice: unknown): choice is EmulationMode => choice === "system" || status.options.some(id => id === choice);
  const playStation = isPlayStation(status.active);
  const sonyPid = status.active === "ds5-edge" ? 0x0df2 : 0x0ce6;
  const detected = controllers?.some(item => item.unVendorID === 0x054c && item.unProductID === sonyPid);

  return <>
    <PanelSection title="Controller">
      {infoRow("Current controller", emulationNote(status))}
      {error && infoRow("Controller emulation", error)}
      {message && infoRow("Steam detection", message)}
      <PanelSectionRow><DropdownItem label="Default controller" description="Used when the running game has no enabled profile. A mode change reconnects the controller."
        strDefaultLabel="System default" selectedOption={status.selected} rgOptions={modeChoices(status.selected)}
        disabled={busy || !status.active} onChange={option => { const mode = option.data; if (validChoice(mode)) void change(() => setDefault(mode)); }} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="Steam Controller in Steam menus" checked={status.steam_menu}
        description="When no game is running, expose the Steam Controller to the Steam interface."
        disabled={busy || (!status.steam_menu && !status.options.includes("steam"))}
        onChange={enabled => void change(() => setSteamMenu(enabled))} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="Refresh PlayStation detection in Steam" checked={status.steam_discovery}
        description="Notify Steam when Companion switches to a DualSense or DualSense Edge, including after resume."
        disabled={busy} onChange={enabled => void change(() => setSteamDiscovery(enabled))} /></PanelSectionRow>
    </PanelSection>
    <PanelSection title="Game profiles">
      {!app && infoRow("Add a game profile", "Launch a Steam game and open this page while it is running.")}
      {app && <>
        <PanelSectionRow><DropdownItem label="Game" strDefaultLabel={games[app]} selectedOption={app}
          rgOptions={Object.entries(games).map(([id, label]) => ({ data: id, label: label + (id === status.running_app ? " (running)" : "") }))}
          disabled={busy} onChange={option => setSelectedApp(String(option.data))} /></PanelSectionRow>
        <PanelSectionRow><ToggleField label="Apply profile automatically" checked={profile?.enabled ?? false}
          disabled={busy} onChange={enabled => save(currentMode, enabled)} /></PanelSectionRow>
        <PanelSectionRow><DropdownItem label="Controller for this game" strDefaultLabel={EMULATION_LABELS[currentMode]}
          selectedOption={currentMode} rgOptions={modeChoices(currentMode)} disabled={busy || !status.active}
          onChange={option => { if (validChoice(option.data)) save(option.data, profile?.enabled ?? false); }} /></PanelSectionRow>
        {profile && <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => void change(() => deleteProfile(app))}>Remove profile</ButtonItem></PanelSectionRow>}
      </>}
    </PanelSection>
    {playStation && <PanelSection title="PlayStation in Steam">
      {detected !== undefined && infoRow("Steam controller list", detected ? "Steam reports a matching PlayStation controller." : "Steam does not list a matching PlayStation controller.")}
      {infoRow("Steam Input", "Enable PlayStation controller support in Steam Settings → Controller to use Steam mapping. For native PlayStation input, use the game's Steam Input override.")}
      {steamInput()?.ShowControllerSettings && <PanelSectionRow><ButtonItem layout="below" onClick={() => steamInput()?.ShowControllerSettings?.()}>Open Steam controller settings</ButtonItem></PanelSectionRow>}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => void change(refreshSteam, "Steam was notified of the virtual controller again.")}>Refresh Steam detection</ButtonItem></PanelSectionRow>
    </PanelSection>}
    {(status.active === "ds5-edge" || status.effective === "ds5-edge") && <PanelSection title="DualSense Edge">
      {infoRow("Auxiliary buttons", EDGE_BUTTONS)}
      {infoRow("Gyroscope", "DualSense Edge exposes motion to games and Steam Input. Adjust the motion source in Gyro & touchpad.")}
    </PanelSection>}
  </>;
}
