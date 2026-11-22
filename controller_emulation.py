# SPDX-License-Identifier: BSD-3-Clause
"""Change the gamepad target of an existing InputPlumber composite device."""

from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from dk_process import run
from inputplumber_process import INTERFACE, InputPlumberError, json_value, run_busctl

MODES = {"steam": "deck-uhid", "xbox-elite": "xbox-elite", "ds5": "ds5", "ds5-edge": "ds5-edge"}
GAMEPADS = {"deck": "steam", "ds5-usb": "ds5", "ds5-bt": "ds5",
            "ds5-edge-usb": "ds5-edge", "ds5-edge-bt": "ds5-edge",
            **{target: mode for mode, target in MODES.items()}}
AUXILIARY = {"keyboard", "mouse", "touchpad", "touchscreen", "dbus", "debug", "null"}
MANAGER = "/org/shadowblip/InputPlumber/Manager"
MANAGER_INTERFACE = "org.shadowblip.InputManager"
TARGET_INTERFACE = "org.shadowblip.Input.Target"
TARGET_PATH = re.compile(r"/org/shadowblip/InputPlumber/devices/target/[A-Za-z0-9_]+")
OWNER = re.compile(r":[0-9]+\.[0-9]+")
SONY_PRODUCTS = {"ds5": "0CE6", "ds5-edge": "0DF2"}
UHID_ROOT = Path("/sys/devices/virtual/misc/uhid")
TARGET_SETTLE_SECONDS = 7.0


def _strings(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InputPlumberError("InputPlumber returned invalid controller targets.")
    return value


def targets(device: dict) -> list[str]:
    owner = device["service_owner"]
    if not OWNER.fullmatch(owner):
        raise InputPlumberError("InputPlumber service identity could not be verified.")
    paths = _strings(json_value(["get-property", owner, device["path"], INTERFACE, "TargetDevices"]))
    kinds = []
    for path in paths:
        if not TARGET_PATH.fullmatch(path):
            raise InputPlumberError("InputPlumber returned an unknown target path.")
        kind = json_value(["get-property", owner, path, TARGET_INTERFACE, "DeviceType"])
        if not isinstance(kind, str):
            raise InputPlumberError("InputPlumber returned an unknown target type.")
        kinds.append(kind)
    return kinds


def gamepad(kinds: list[str], *, allow_missing: bool = False) -> str | None:
    pads = [kind for kind in kinds if kind in GAMEPADS]
    if set(kinds) - GAMEPADS.keys() - AUXILIARY or len(pads) > 1 or (not pads and not allow_missing):
        raise InputPlumberError("InputPlumber must have one supported gamepad target.")
    return pads[0] if pads else None


def modes(device: dict) -> dict[str, str]:
    supported = _strings(json_value(["get-property", device["service_owner"], MANAGER,
                                     MANAGER_INTERFACE, "SupportedTargetDeviceIds"]))
    choices = {mode: target for mode, target in MODES.items() if target in supported}
    if "steam" not in choices and "deck" in supported:
        choices["steam"] = "deck"
    for mode in SONY_PRODUCTS:
        # Use wired reports for games that require a USB DualSense. Older InputPlumber
        # versions expose the same USB target without the -usb suffix.
        if f"{mode}-usb" in supported:
            choices[mode] = f"{mode}-usb"
    return choices


def sony_hidraw(target: str | None) -> Path | None:
    product = SONY_PRODUCTS.get(GAMEPADS.get(target))
    if product is None or not UHID_ROOT.is_dir():
        return None
    identity = re.compile(rf"000[35]:054C:{product}\.[0-9A-F]+", re.IGNORECASE)
    nodes = []
    for device in UHID_ROOT.iterdir():
        if not identity.fullmatch(device.name) or device.is_symlink():
            continue
        nodes.extend(node for node in (device / "hidraw").glob("hidraw[0-9]*")
                     if re.fullmatch(r"hidraw[0-9]+", node.name) and node.is_dir() and not node.is_symlink())
    # If HHD or another provider has a second Sony target, the node is ambiguous.
    return nodes[0] if len(nodes) == 1 else None


def refresh_steam_detection(node: Path) -> None:
    # Only replay the HID interface Steam uses for PlayStation discovery. The
    # evdev gamepad remains attached and other devices receive no hotplug event.
    try:
        result = run(["/usr/bin/udevadm", "trigger", "--action=change", "--settle", str(node)],
                     3.0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputPlumberError("Could not refresh Steam controller discovery.") from exc
    if result.returncode:
        raise InputPlumberError("Could not refresh Steam controller discovery.")


def _set_targets(device: dict, kinds: list[str]) -> None:
    run_busctl(["call", device["service_owner"], device["path"], INTERFACE,
                "SetTargetDevices", "as", str(len(kinds)), *kinds], timeout=2.0)


def _wait_for(device: dict, expected: list[str]) -> bool:
    # SetTargetDevices queues the change. The gamepad and its D-Bus interface appear later.
    until = time.monotonic() + TARGET_SETTLE_SECONDS
    while True:
        try:
            if sorted(targets(device)) == sorted(expected):
                return True
        except InputPlumberError:
            pass
        if time.monotonic() >= until:
            return False
        time.sleep(0.25)


def switch(device: dict, current: list[str], target: str) -> None:
    previous = gamepad(current)
    if previous == target:
        return
    expected = [target if kind == previous else kind for kind in current]
    _change_targets(device, current, expected, current)


def recover(device: dict, current: list[str], confirmed: list[str], target: str, baseline: str) -> None:
    """Reattach a missing target using this composite's last confirmed auxiliary devices."""
    if gamepad(current, allow_missing=True) is not None:
        raise InputPlumberError("The controller already has a gamepad target.")
    previous = gamepad(confirmed)
    auxiliary = list((Counter(current) | Counter(kind for kind in confirmed if kind != previous)).elements())
    _change_targets(device, current, [target, *auxiliary], [baseline, *auxiliary])


def _change_targets(device: dict, current: list[str], expected: list[str], fallback: list[str]) -> None:
    if sorted(targets(device)) != sorted(current):
        raise InputPlumberError("Controller targets changed. Try again.")
    try:
        _set_targets(device, expected)
    except InputPlumberError:
        # A D-Bus timeout can occur after the command is queued. Check the actual result.
        pass
    if _wait_for(device, expected):
        return
    # Restore only this composite, and only while it contains our targets or a subset
    # left by the switch. A different target means another client intervened.
    restored = False
    try:
        actual = targets(device)
        restored = sorted(actual) == sorted(fallback)
        if not restored and set(actual) <= set(current + expected + fallback):
            _set_targets(device, fallback)
            restored = _wait_for(device, fallback)
    except InputPlumberError:
        pass
    if restored:
        raise InputPlumberError("InputPlumber could not switch modes. The previous mode is active.")
    raise InputPlumberError("InputPlumber could not confirm the switch or restore the previous mode.")
