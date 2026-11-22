// SPDX-License-Identifier: BSD-3-Clause
import { DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { emulationOptions, type EmulationMode, type EmulationStatus } from "../desktop/native/controller.mjs";
import { errorText, rpc, runningApp } from "./shared";
import { infoRow, statusPlaceholder } from "./ui";

const readStatus = rpc<[], EmulationStatus>("emulation_get_status");
const setDefault = rpc<[EmulationMode, boolean], EmulationStatus>("emulation_set_default");
const reportQam = rpc<[boolean, boolean], object>("emulation_report_qam");

const visibleQamViews = new Set<symbol>();
let publishQam: (() => void) | undefined;

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
  let stopped = false;
  const publish = async () => {
    if (pending || stopped) return;
    const open = visibleQamViews.size > 0;
    const inGame = !!Number(runningApp()?.appid);
    pending = true;
    try {
      await reportQam(open, inGame);
    } catch {
      // The heartbeat retries after Decky reconnects.
    } finally {
      pending = false;
      if (open !== (visibleQamViews.size > 0)) void publish();
    }
  };
  const notify = () => void publish();
  publishQam = notify;
  const heartbeat = setInterval(notify, 3000);
  notify();
  return () => {
    stopped = true;
    clearInterval(heartbeat);
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
