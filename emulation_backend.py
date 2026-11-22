# SPDX-License-Identifier: BSD-3-Clause
"""Controller emulation settings, per-game routing and module lifecycle."""

import asyncio
import functools
import time

import active_app
import controller_backend
import controller_emulation
import module_runtime
from diagnostics import event
from inputplumber_process import SETTLE_S

_offload = functools.partial(module_runtime.offload, "emulation")
MODES = {"system", *controller_emulation.MODES}
PROFILE_LIMIT = 100
CHECK_SECONDS = 2.0
QAM_LEASE_SECONDS = 12.0
QAM_SETTLE_SECONDS = 1.0
QAM_SELECTION_GRACE_SECONDS = 4.0


def _mode(value):
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("Unknown controller emulation mode.")
    return value


def _app(value, *, allow_empty=False):
    app = active_app.normalise_app_id(value, allow_default=False, allow_empty=allow_empty)
    if app is None:
        raise ValueError("Invalid game ID.")
    return app


def _options():
    raw = controller_backend.settings.getSetting("emulation_options", {})
    if not isinstance(raw, dict):
        raw = {}
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    clean = {}
    for app, value in list(profiles.items())[:PROFILE_LIMIT]:
        if (active_app.normalise_app_id(app, allow_default=False) and isinstance(value, dict)
                and type(value.get("enabled")) is bool and isinstance(value.get("mode"), str)
                and value["mode"] in MODES):
            name = value.get("name")
            clean[app] = {"mode": value["mode"], "enabled": value["enabled"],
                          "name": name[:120] if isinstance(name, str) and name else app}
    return {"steam_menu": raw.get("steam_menu") is True, "profiles": clean}


class Plugin:
    def __init__(self, controller):
        self._controller = controller
        self._task = None
        self._app_id = ""
        self._app_name = ""
        self._reported_at = None
        self._running = False
        self._qam_open = False
        self._qam_seen_at = float("-inf")
        self._qam_closed_at = float("-inf")
        self._defer_until = 0.0
        self._deferred_at = None

    @staticmethod
    def applicable():
        return controller_backend._console() == "go2"

    def _app_context(self):
        app = active_app.resolve_running_app(self._app_id, self._reported_at, time.monotonic(),
                                             active_app.scan_proc_for_appid)
        return app, self._app_name if app and app == self._app_id else app

    def _override(self, options, app=None):
        app = self._app_context()[0] if app is None else app
        profile = options["profiles"].get(app)
        if profile and profile["enabled"]:
            return profile["mode"]
        return "steam" if not app and options["steam_menu"] else None

    def _qam_blocks_switch(self, now):
        waiting_for_close = self._deferred_at is not None and (self._qam_open or self._qam_seen_at < self._deferred_at)
        return (waiting_for_close or now < self._defer_until or now - self._qam_closed_at < QAM_SETTLE_SECONDS
                or (self._qam_open and now - self._qam_seen_at < QAM_LEASE_SECONDS))

    def _status(self):
        with self._controller._operation:
            options = _options()
            app, name = self._app_context()
            mode = self._override(options, app)
            status = self._controller._emulation_status(None, None) or {
                "selected": "system", "active": None, "options": [], "reason": "Requires Legion Go 2."
            }
            return {**status, **options, "running_app": app, "running_name": name,
                    "waiting_for_qam": self._qam_blocks_switch(time.monotonic()),
                    "effective": mode if mode is not None else status["selected"]}

    async def get_status(self):
        return await _offload(self._status)

    def _set_default(self, mode, after_qam):
        with self._controller._operation:
            now = time.monotonic()
            apply = not after_qam and not self._qam_blocks_switch(now) and self._override(_options()) is None
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

    def _set_profile(self, app, name, mode, enabled):
        with self._controller._operation:
            options = _options()
            if app not in options["profiles"] and len(options["profiles"]) >= PROFILE_LIMIT:
                raise ValueError("Remove a saved controller profile first.")
            options["profiles"][app] = {"mode": mode, "enabled": enabled, "name": name or app}
            controller_backend._save(options, "emulation_options")

    async def set_profile(self, app, name, mode, enabled):
        if not isinstance(name, str) or type(enabled) is not bool:
            raise ValueError("Invalid controller profile.")
        await _offload(self._set_profile, _app(app), name.strip()[:120], _mode(mode), enabled)
        return await self.get_status()

    def _delete_profile(self, app):
        with self._controller._operation:
            options = _options()
            options["profiles"].pop(app, None)
            controller_backend._save(options, "emulation_options")

    async def delete_profile(self, app):
        await _offload(self._delete_profile, _app(app))
        return await self.get_status()

    def _set_steam_menu(self, enabled):
        with self._controller._operation:
            options = _options()
            options["steam_menu"] = enabled
            controller_backend._save(options, "emulation_options")

    async def set_steam_menu(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Invalid Steam menu setting.")
        await _offload(self._set_steam_menu, enabled)
        return await self.get_status()

    async def report_app(self, app, name, qam_open=None):
        if not isinstance(name, str) or (qam_open is not None and type(qam_open) is not bool):
            raise ValueError("Invalid game or Quick Access state.")
        self._app_id = _app(app, allow_empty=True)
        self._app_name = name[:120]
        now = time.monotonic()
        self._reported_at = now
        if qam_open is not None:
            if not qam_open and self._qam_open:
                self._qam_closed_at = now
            self._qam_open = qam_open
            self._qam_seen_at = now
        return {}

    def _test_sticks(self):
        with self._controller._operation:
            device = self._controller._emulation_device()
            active = controller_emulation.gamepad(controller_emulation.targets(device))
            return controller_backend.test_playstation_sticks(controller_emulation.GAMEPADS[active])

    async def test_sticks(self):
        return await _offload(self._test_sticks)

    def _tick(self, now, resumed):
        with self._controller._operation:
            if not self._running or self._qam_blocks_switch(now):
                return
            self._deferred_at = None
            options = _options()
            override = self._override(options) if options["profiles"] or options["steam_menu"] else None
            self._controller._sync_emulation(now=now, resumed=resumed, override=override)
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
        self._app_id = self._app_name = ""
        self._reported_at = None
        self._qam_open = False
        self._qam_seen_at = self._qam_closed_at = float("-inf")
        self._defer_until = 0.0
        self._deferred_at = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await module_runtime.drain("emulation")
        await _offload(self._controller._sync_emulation, release=True)
