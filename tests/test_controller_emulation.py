# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_controller_backend import MemorySettings

import controller_backend as backend
import controller_emulation as emulation
import emulation_backend


DEVICE = {
    "service_owner": ":1.42",
    "path": "/org/shadowblip/InputPlumber/CompositeDevice0",
    "generation": "legion|:1.42",
}
BASE = ["deck-uhid", "mouse", "keyboard", "touchpad"]


class TargetTests(unittest.TestCase):
    def test_sony_modes_use_the_original_target_or_its_wired_alias(self):
        with patch.object(emulation, "json_value",
                          return_value=["deck", "ds5", "ds5-usb", "ds5-edge-usb", "ds5-bt"]):
            self.assertEqual(emulation.modes(DEVICE),
                             {"steam": "deck", "ds5": "ds5", "ds5-edge": "ds5-edge-usb"})

    @unittest.skipUnless(os.name == "posix", "Linux sysfs names require colons")
    def test_steam_discovery_includes_hidraw_and_input_events_of_one_virtual_sony_pad(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wired = root / "0003:054C:0CE6.0001/hidraw/hidraw3"
            wired.mkdir(parents=True)
            gamepad = wired.parents[1] / "input/input21/event17"
            touchpad = wired.parents[1] / "input/input22/event18"
            edge = root / "0003:054C:0DF2.0004/input/input24/event20"
            edge.mkdir(parents=True)
            (root / "0003:17EF:61EB.0002/hidraw/hidraw4").mkdir(parents=True)
            with patch.object(emulation, "UHID_ROOT", root):
                self.assertEqual(emulation.sony_discovery_nodes("ds5"), (wired,))
                gamepad.mkdir(parents=True)
                touchpad.mkdir(parents=True)
                self.assertEqual(emulation.sony_discovery_nodes("ds5-usb"), (wired, gamepad, touchpad))
                self.assertEqual(emulation.sony_discovery_nodes("ds5-edge"), (edge,))
                self.assertEqual(emulation.sony_discovery_nodes("xbox-elite"), ())
                (root / "0005:054C:0CE6.0003/hidraw/hidraw5").mkdir(parents=True)
                self.assertEqual(emulation.sony_discovery_nodes("ds5"), ())

    def test_discovery_never_triggers_udev_without_explicit_nodes(self):
        with patch.object(emulation, "run") as run:
            emulation.refresh_steam_detection(())
            run.assert_not_called()
            run.return_value.returncode = 0
            nodes = (Path("hidraw/hidraw3"), Path("input/input21/event17"))
            emulation.refresh_steam_detection(nodes)
            self.assertEqual(run.call_args.args[0],
                             ["/usr/bin/udevadm", "trigger", "--action=change", "--settle", *map(str, nodes)])
            emulation.refresh_steam_detection(nodes, reconnect=True)
            self.assertEqual(run.call_args.args[0],
                             ["/usr/bin/udevadm", "trigger", "--action=add", "--settle", *map(str, nodes)])

    def test_queries_only_the_discovered_service_and_reads_scalar_target_types(self):
        path = "/org/shadowblip/InputPlumber/devices/target/gamepad0"
        with patch.object(emulation, "json_value", side_effect=[[path], "deck-uhid"]) as dbus:
            self.assertEqual(emulation.targets(DEVICE), ["deck-uhid"])
        self.assertEqual(dbus.call_args_list[1].args[0],
                         ["get-property", ":1.42", path, emulation.TARGET_INTERFACE, "DeviceType"])

    def test_switch_replaces_only_the_gamepad_and_checks_readback(self):
        expected = ["xbox-elite", "mouse", "keyboard", "touchpad"]
        with (patch.object(emulation, "targets", side_effect=[BASE, expected]),
              patch.object(emulation, "run_busctl") as dbus):
            emulation.switch(DEVICE, BASE, "xbox-elite")
        dbus.assert_called_once_with(["call", ":1.42", DEVICE["path"], emulation.INTERFACE,
                                     "SetTargetDevices", "as", "4", *expected], timeout=2.0)

    def test_failed_attachment_restores_the_previous_gamepad_and_auxiliary_targets(self):
        with (patch.object(emulation, "targets", side_effect=[BASE, BASE[1:]]),
              patch.object(emulation, "_wait_for", side_effect=[False, True]),
              patch.object(emulation, "_set_targets") as write):
            with self.assertRaisesRegex(emulation.InputPlumberError, "previous mode is active"):
                emulation.switch(DEVICE, BASE, "ds5")
        self.assertEqual(write.call_args_list[0].args, (DEVICE, ["ds5", *BASE[1:]]))
        self.assertEqual(write.call_args_list[1].args, (DEVICE, BASE))

    def test_ambiguous_or_external_targets_are_left_alone(self):
        with patch.object(emulation, "_set_targets") as write:
            with self.assertRaises(emulation.InputPlumberError):
                emulation.switch(DEVICE, [*BASE, "xbox-elite"], "ds5")
            with patch.object(emulation, "targets", return_value=["ds5", *BASE[1:]]):
                with self.assertRaisesRegex(emulation.InputPlumberError, "targets changed"):
                    emulation.switch(DEVICE, BASE, "xbox-elite")
        write.assert_not_called()

    def test_missing_gamepad_is_recreated_with_confirmed_auxiliaries(self):
        current = ["keyboard"]
        expected = ["xbox-elite", *BASE[1:]]
        with (patch.object(emulation, "targets", side_effect=[current, expected]),
              patch.object(emulation, "_set_targets") as write):
            emulation.recover(DEVICE, current, BASE, "xbox-elite", "deck-uhid")
        self.assertEqual(write.call_args.args[0], DEVICE)
        self.assertCountEqual(write.call_args.args[1], expected)

    def test_failed_missing_gamepad_recovery_leaves_a_steam_target(self):
        current = list(BASE[1:])
        with (patch.object(emulation, "targets", return_value=current),
              patch.object(emulation, "_wait_for", side_effect=[False, True]),
              patch.object(emulation, "_set_targets") as write):
            with self.assertRaisesRegex(emulation.InputPlumberError, "previous mode is active"):
                emulation.recover(DEVICE, current, BASE, "xbox-elite", "deck-uhid")
        self.assertCountEqual(write.call_args.args[1], BASE)


class EmulationPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.plugin = backend.Plugin()
        self.state = MemorySettings()
        self.live = list(BASE)
        self.start_patch(backend, "settings", self.state)
        self.start_patch(backend, "_console", return_value="go2")
        self.start_patch(backend, "_hid_inventory", return_value=({"path": "/dev/hidraw0"}, None))
        self.start_patch(self.plugin, "_emulation_device", return_value=DEVICE)
        self.start_patch(emulation, "modes", return_value=emulation.MODES)
        self.start_patch(emulation, "targets", side_effect=lambda device: self.live.copy())
        self.start_patch(emulation, "switch", side_effect=self.switch)

    def start_patch(self, target, key, *args, **kwargs):
        mock = patch.object(target, key, *args, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def switch(self, device, current, target):
        self.live = [target if kind == emulation.gamepad(current) else kind for kind in current]

    def test_selections_keep_the_original_target_and_system_restores_it(self):
        self.plugin._set_emulation("xbox-elite")
        self.plugin._set_emulation("ds5")
        self.assertEqual(self.state.committed["emulation"], {"baseline": "deck-uhid", "target": "ds5"})
        self.assertEqual(self.plugin._emulation_status(None, DEVICE)["active"], "ds5")
        self.plugin._set_emulation("system")
        self.assertEqual(self.live, BASE)
        self.assertIsNone(self.state.committed["emulation"])

    def test_choice_is_restored_on_a_reset_without_overwriting_an_external_mode(self):
        self.plugin._set_emulation("ds5")
        self.plugin._sync_emulation(release=True)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=5)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=10)
        self.assertEqual(self.live[0], "ds5")
        self.live = ["xbox-elite", *BASE[1:]]
        self.plugin._sync_emulation(now=75)
        self.plugin._sync_emulation(now=80)
        self.assertEqual(self.live[0], "xbox-elite")

    def test_resume_reapplies_saved_mode_on_the_same_composite(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE.copy()
        self.plugin._emulation_retry_at = 100
        self.plugin._sync_emulation(now=15, resumed=True)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=20)
        self.assertEqual(self.live[0], "xbox-elite")
        self.assertEqual(self.state.committed["emulation"]["target"], "xbox-elite")

    def test_gamepad_temporarily_missing_after_exiting_a_game(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE[1:]
        with patch.object(emulation, "recover") as recover:
            self.plugin._sync_emulation(now=5)
            recover.assert_not_called()
            self.plugin._sync_emulation(now=10)
            recover.assert_called_once_with(DEVICE, BASE[1:], ["xbox-elite", *BASE[1:]],
                                            "xbox-elite", "deck-uhid")

    def test_reset_cannot_cause_a_switch_every_poll(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE.copy()
        self.plugin._sync_emulation(now=5)
        self.plugin._sync_emulation(now=10)
        self.assertEqual(self.live[0], "xbox-elite")
        self.live = BASE.copy()
        self.plugin._sync_emulation(now=15)
        self.plugin._sync_emulation(now=20)
        self.assertEqual(self.live, BASE)
        self.assertEqual(self.state.committed["emulation"]["target"], "xbox-elite")

    def test_game_exit_recovers_a_new_composite_with_only_its_own_auxiliaries(self):
        self.plugin._set_emulation("ds5")
        replacement = {**DEVICE, "generation": "legion|:1.43", "service_owner": ":1.43"}
        self.live = ["keyboard"]
        self.plugin._emulation_retry_at = 200
        with (patch.object(self.plugin, "_emulation_device", return_value=replacement),
              patch.object(emulation, "_set_targets", side_effect=lambda _, kinds: self.live.__setitem__(slice(None), kinds))):
            self.plugin._sync_emulation(now=20, game_exited=True)
            self.assertEqual(self.live, ["keyboard"])
            self.plugin._sync_emulation(now=22)
            self.assertEqual(self.live, ["ds5", "keyboard"])
            self.assertEqual(self.state.committed["emulation"]["target"], "ds5")

    def test_menu_reannounces_existing_sony_pad_once_without_switching_it(self):
        self.plugin._set_emulation("ds5-edge")
        watcher = emulation_backend.Plugin(self.plugin)
        watcher._running = True
        nodes = (Path("hidraw/hidraw4"), Path("input/input3/event8"))
        def report(now, qam, running):
            with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: now)):
                asyncio.run(watcher.report_qam(qam, running))
        with (patch.object(emulation, "sony_discovery_nodes", return_value=nodes),
              patch.object(emulation, "refresh_steam_detection") as refresh,
              patch.object(emulation, "switch") as switch):
            report(1, False, False)
            watcher._tick(2, False)
            report(4, False, True)
            watcher._tick(5, False)
            report(8, True, False)
            watcher._tick(12, False)
            self.assertEqual(refresh.call_count, 1)
            report(14, False, False)
            watcher._tick(14.5, False)
            watcher._tick(16, False)
            report(17, False, False)
            watcher._tick(18, False)
            self.assertEqual(refresh.call_count, 2)
            refresh.assert_called_with(nodes, reconnect=True)
            switch.assert_not_called()

    def test_watch_waits_for_resume_to_settle_and_checks_again(self):
        self.plugin._set_emulation("xbox-elite")
        watcher = emulation_backend.Plugin(self.plugin)
        watcher._running = True
        clock = [0.0]
        live_after_tick = {}

        async def sleep(seconds):
            live_after_tick[clock[0]] = self.live[0]
            clock[0] += seconds
            if clock[0] == 2:
                self.live = BASE.copy()
            if clock[0] > 18:
                raise asyncio.CancelledError

        with (patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: clock[0])),
              patch.object(emulation_backend.asyncio, "sleep", side_effect=sleep),
              patch.object(backend.module_runtime, "suspend_offset", side_effect=lambda: 10 if clock[0] else 0),
              patch.object(watcher, "_tick_async", new=AsyncMock(side_effect=watcher._tick))):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(watcher._watch())
        self.assertEqual(live_after_tick[12], "deck-uhid")
        self.assertEqual(live_after_tick[14], "deck-uhid")
        self.assertEqual(live_after_tick[16], "xbox-elite")

    def test_legacy_profiles_and_menu_override_cannot_replace_the_selected_sony_pad(self):
        self.plugin._set_emulation("ds5")
        self.state.data["emulation_options"] = {"steam_menu": True, "profiles": {
            "200": {"name": "Example", "enabled": True, "mode": "xbox-elite"}
        }}
        watcher = emulation_backend.Plugin(self.plugin)
        watcher._running = True
        watcher._tick(10, False)
        watcher._tick(12, False)
        self.assertEqual(self.live[0], "ds5")
        self.assertNotIn("profiles", watcher._status())
        self.assertNotIn("steam_menu", watcher._status())

    def test_steam_discovery_runs_only_with_qam_closed_and_once_per_attachment(self):
        self.plugin._set_emulation("ds5")
        watcher = emulation_backend.Plugin(self.plugin)
        watcher._running = True
        with (patch.object(emulation, "sony_discovery_nodes", return_value=(Path("hidraw/hidraw0"),)) as node,
              patch.object(emulation, "refresh_steam_detection") as refresh):
            with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: 10)):
                asyncio.run(watcher.report_qam(True))
            watcher._tick(11, False)
            refresh.assert_not_called()
            with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: 12)):
                asyncio.run(watcher.report_qam(False))
            watcher._tick(12.5, False)
            refresh.assert_not_called()
            watcher._tick(14, False)
            watcher._tick(16, False)
            refresh.assert_called_once_with(node.return_value, reconnect=False)
            node.return_value += (Path("input/input3/event8"),)
            watcher._tick(18, False)
            watcher._tick(20, False)
            self.assertEqual(refresh.call_count, 2)
            self.assertEqual(refresh.call_args.args, (node.return_value,))
            watcher._tick(24, True)
            watcher._tick(26, False)
            self.assertEqual(refresh.call_count, 3)

    def test_transient_udev_failure_retries_once_after_a_delay(self):
        watcher = emulation_backend.Plugin(self.plugin)
        with (patch.object(emulation, "sony_discovery_nodes", return_value=(Path("input/input3/event8"),)),
              patch.object(emulation, "refresh_steam_detection", side_effect=emulation.InputPlumberError("timeout")) as refresh):
            for now in (10, 12, 18, 20, 22, 60):
                watcher._refresh_steam("ds5", now, False)
            self.assertEqual(refresh.call_count, 2)

    def test_default_change_waits_for_a_fresh_qam_close_even_if_the_ui_stops_reporting(self):
        watcher = emulation_backend.Plugin(self.plugin)
        watcher._running = True
        with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: 10)):
            watcher._set_default("ds5", after_qam=True)
        self.assertEqual(self.state.committed["emulation"]["target"], "ds5")
        watcher._tick(40, False)
        self.assertEqual(self.live, BASE)
        with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: 41)):
            asyncio.run(watcher.report_qam(True))
        watcher._tick(42, False)
        self.assertEqual(self.live, BASE)
        with patch.object(emulation_backend, "time", types.SimpleNamespace(monotonic=lambda: 43)):
            asyncio.run(watcher.report_qam(False))
        watcher._tick(43.5, False)
        self.assertEqual(self.live, BASE)
        watcher._tick(45, False)
        watcher._tick(47, False)
        self.assertEqual(self.live, ["ds5", *BASE[1:]])

    def test_switch_during_diagnostics_does_not_change_the_gamepad(self):
        self.plugin._capture = {"active": True}
        with self.assertRaisesRegex(backend.ControllerError, "Stop the controller test"):
            self.plugin._set_emulation("ds5")
        self.assertEqual(self.live, BASE)
        self.assertIsNone(backend._emulation_choice())
