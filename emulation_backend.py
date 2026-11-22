# SPDX-License-Identifier: BSD-3-Clause
"""Controller emulation settings and module lifecycle."""

import asyncio
import functools
import time

import controller_backend
import controller_emulation
import module_runtime
from diagnostics import event
from inputplumber_process import SETTLE_S

_offload = functools.partial(module_runtime.offload, "emulation")
MODES = {"system", *controller_emulation.MODES}
CHECK_SECONDS = 2.0
QAM_LEASE_SECONDS = 12.0
QAM_SETTLE_SECONDS = 1.0
QAM_SELECTION_GRACE_SECONDS = 4.0


def _mode(value):
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("Unknown controller emulation mode.")
    return value


class Plugin:
    def __init__(self, controller):
        self._controller = controller
        self._task = None
        self._running = False
        self._qam_open = False
        self._qam_seen_at = float("-inf")
        self._qam_closed_at = float("-inf")
        self._defer_until = 0.0
        self._deferred_at = None
        self._steam_node = None

    @staticmethod
    def applicable():
        return controller_backend._console() == "go2"

    def _qam_blocks_switch(self, now):
        waiting_for_close = self._deferred_at is not None and (self._qam_open or self._qam_seen_at < self._deferred_at)
        return (waiting_for_close or now < self._defer_until or now - self._qam_closed_at < QAM_SETTLE_SECONDS
                or (self._qam_open and now - self._qam_seen_at < QAM_LEASE_SECONDS))

    def _status(self):
        with self._controller._operation:
            status = self._controller._emulation_status(None, None) or {
                "selected": "system", "active": None, "options": [], "reason": "Requires Legion Go 2."
            }
            return {**status, "selection_pending": self._deferred_at is not None}

    async def get_status(self):
        return await _offload(self._status)

    def _set_default(self, mode, after_qam):
        with self._controller._operation:
            now = time.monotonic()
            apply = not after_qam and not self._qam_blocks_switch(now)
            self._controller._set_emulation(mode, apply=apply)
            if after_qam:
                # Native dropdowns temporarily report QAM as hidden while committing a selection.
                self._defer_until = now + QAM_SELECTION_GRACE_SECONDS
                self._deferred_at = now

    async def set_default(self, mode, after_qam=False):
        if type(after_qam) is not bool:
            raise ValueError("Invalid controller switch timing.")
        await _offload(self._set_default, _mode(mode), after_qam)
        return await self.get_status()

    async def report_qam(self, opened):
        if type(opened) is not bool:
            raise ValueError("Invalid Quick Access state.")
        now = time.monotonic()
        if not opened and self._qam_open:
            self._qam_closed_at = now
        self._qam_open = opened
        self._qam_seen_at = now
        return {}

    def _refresh_steam(self, target):
        node = controller_emulation.sony_hidraw(target)
        if node is None or node == self._steam_node:
            return
        # Mark before re-announcing so a failure cannot cause a hotplug event every poll.
        self._steam_node = node
        try:
            controller_emulation.refresh_steam_detection(node)
        except controller_emulation.InputPlumberError as exc:
            event("emulation", "steam_detection_refresh_failed", level="warning", error=exc, repeat=60)

    def _tick(self, now, resumed):
        with self._controller._operation:
            if not self._running or self._qam_blocks_switch(now):
                return
            self._deferred_at = None
            target = self._controller._sync_emulation(now=now, resumed=resumed)
            self._refresh_steam(target)
            return True

    @module_runtime.controller_operation("emulation")
    async def _tick_async(self, now, resumed):
        return await _offload(self._tick, now, resumed)

    async def _watch(self):
        resume = module_runtime.ResumeWatch()
        resume.mark()
        settle_until = None
        while self._running:
            await asyncio.sleep(CHECK_SECONDS)
            now = time.monotonic()
            if resume():
                settle_until = now + SETTLE_S
            if settle_until is not None and now < settle_until:
                continue
            try:
                if await self._tick_async(now, settle_until is not None):
                    settle_until = None
            except module_runtime.ControllerPaused:
                continue
            except Exception as exc:
                event("emulation", "controller_poll_failed", level="warning", error=exc, repeat=60)

    async def _main(self):
        self._running = True
        self._task = module_runtime.background_task(self._watch())

    async def _unload(self):
        self._running = False
        self._qam_open = False
        self._qam_seen_at = self._qam_closed_at = float("-inf")
        self._defer_until = 0.0
        self._deferred_at = None
        self._steam_node = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await module_runtime.drain("emulation")
        await _offload(self._controller._sync_emulation, release=True)
