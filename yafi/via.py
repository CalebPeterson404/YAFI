# via.py
#
# Copyright 2026 Caleb Peterson
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Minimal VIA raw-HID client for Framework Laptop 16 QMK input modules.

Talks to the VIA interface (usage page 0xFF60) over Linux hidraw. No GTK
in here, so it can be tested or scripted on its own.
"""

import os
import re
import glob
import select

FRAMEWORK_VID = 0x32AC
VIA_USAGE_PAGE_PREFIX = b"\x06\x60\xff"
REPORT_SIZE = 32

# VIA command ids (protocol 9+)
ID_GET_PROTOCOL_VERSION = 0x01
ID_GET_KEYBOARD_VALUE = 0x02
ID_DYNAMIC_KEYMAP_GET_KEYCODE = 0x04
ID_DYNAMIC_KEYMAP_SET_KEYCODE = 0x05
ID_CUSTOM_SET_VALUE = 0x07
ID_CUSTOM_GET_VALUE = 0x08
ID_CUSTOM_SAVE = 0x09
ID_DYNAMIC_KEYMAP_MACRO_GET_COUNT = 0x0C
ID_DYNAMIC_KEYMAP_GET_LAYER_COUNT = 0x11
ID_DYNAMIC_KEYMAP_GET_BUFFER = 0x12
ID_UNHANDLED = 0xFF

CHANNEL_RGB_MATRIX = 3
RGB_MATRIX_BRIGHTNESS = 1
RGB_MATRIX_EFFECT = 2
RGB_MATRIX_EFFECT_SPEED = 3
RGB_MATRIX_COLOR = 4

# Supported modules. "layout" lists matrix (row, col) in visual order,
# left-to-right, top-to-bottom, from the module's QMK info.json.
DEVICES = {
    0x0013: {
        "name": "Laptop 16 RGB Macropad",
        "rows": 4,
        "cols": 8,
        "width": 4,
        "layout": [
            (2, 1), (2, 2), (3, 4), (2, 4),
            (0, 0), (0, 4), (1, 1), (1, 6),
            (0, 1), (0, 5), (1, 2), (2, 5),
            (0, 2), (0, 6), (1, 3), (1, 7),
            (0, 3), (0, 7), (1, 4), (2, 6),
            (1, 0), (2, 7), (1, 5), (2, 0),
        ],
        "layer_names": {
            0: "NumLock on",
            1: "NumLock off (Fn)",
            2: "Factory test",
        },
    },
}

# rgb_matrix modes in QMK enum order (Framework firmware enables all of them).
# VIA reports 0 when RGB is off.
RGB_MATRIX_EFFECTS = [
    "Off", "Solid Color", "Alphas Mods", "Gradient Up/Down",
    "Gradient Left/Right", "Breathing", "Band Saturation", "Band Value",
    "Band Pinwheel Saturation", "Band Pinwheel Value",
    "Band Spiral Saturation", "Band Spiral Value", "Cycle All",
    "Cycle Left/Right", "Cycle Up/Down", "Rainbow Moving Chevron",
    "Cycle Out/In", "Cycle Out/In Dual", "Cycle Pinwheel", "Cycle Spiral",
    "Dual Beacon", "Rainbow Beacon", "Rainbow Pinwheels", "Raindrops",
    "Jellybean Raindrops", "Hue Breathing", "Hue Pendulum", "Hue Wave",
    "Pixel Rain", "Pixel Flow", "Pixel Fractal", "Typing Heatmap",
    "Digital Rain", "Solid Reactive Simple", "Solid Reactive",
    "Solid Reactive Wide", "Solid Reactive Multiwide",
    "Solid Reactive Cross", "Solid Reactive Multicross",
    "Solid Reactive Nexus", "Solid Reactive Multinexus", "Splash",
    "Multisplash", "Solid Splash", "Solid Multisplash",
]


class ViaError(Exception):
    pass


def find_devices():
    """Return [(hidraw_path, pid)] for supported Framework VIA interfaces."""
    found = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                uevent = f.read()
            with open(os.path.join(node, "device", "report_descriptor"), "rb") as f:
                descriptor = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=[0-9A-Fa-f]+:([0-9A-Fa-f]+):([0-9A-Fa-f]+)", uevent)
        if not m:
            continue
        vid, pid = int(m.group(1), 16), int(m.group(2), 16)
        if vid == FRAMEWORK_VID and pid in DEVICES and descriptor.startswith(VIA_USAGE_PAGE_PREFIX):
            found.append(("/dev/" + os.path.basename(node), pid))
    return found


class ViaDevice:
    def __init__(self, path, pid):
        self.path = path
        self.pid = pid
        self.info = DEVICES[pid]
        self.rows = self.info["rows"]
        self.cols = self.info["cols"]
        self.fd = os.open(path, os.O_RDWR)
        self.protocol = self.get_protocol_version()
        self.layers = self.command(ID_DYNAMIC_KEYMAP_GET_LAYER_COUNT)[1]

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def command(self, *data, timeout=1.0):
        report = bytes(data) + bytes(REPORT_SIZE - len(data))
        # Leading 0 = report id (VIA uses none)
        os.write(self.fd, b"\x00" + report)
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            raise ViaError("Timed out waiting for the device")
        reply = os.read(self.fd, REPORT_SIZE)
        if reply[0] == ID_UNHANDLED:
            raise ViaError(f"Command 0x{data[0]:02x} not supported by firmware")
        return reply

    def get_protocol_version(self):
        r = self.command(ID_GET_PROTOCOL_VERSION)
        return (r[1] << 8) | r[2]

    # Keymap

    def get_keycode(self, layer, row, col):
        r = self.command(ID_DYNAMIC_KEYMAP_GET_KEYCODE, layer, row, col)
        return (r[4] << 8) | r[5]

    def set_keycode(self, layer, row, col, keycode):
        self.command(ID_DYNAMIC_KEYMAP_SET_KEYCODE, layer, row, col,
                     (keycode >> 8) & 0xFF, keycode & 0xFF)

    def read_keymap(self):
        """Whole dynamic keymap as keymap[layer][row][col]."""
        total = self.layers * self.rows * self.cols * 2
        buf = b""
        offset = 0
        while offset < total:
            size = min(28, total - offset)
            r = self.command(ID_DYNAMIC_KEYMAP_GET_BUFFER, offset >> 8, offset & 0xFF, size)
            buf += r[4:4 + size]
            offset += size
        codes = [(buf[i] << 8) | buf[i + 1] for i in range(0, len(buf), 2)]
        return [[[codes[(l * self.rows + r) * self.cols + c] for c in range(self.cols)]
                 for r in range(self.rows)] for l in range(self.layers)]

    def write_keymap(self, keymap):
        """Write keymap[layer][row][col]; only changed keys are written.
        Returns the number of keys changed."""
        current = self.read_keymap()
        changed = 0
        for l in range(min(self.layers, len(keymap))):
            for r in range(self.rows):
                for c in range(self.cols):
                    new = keymap[l][r][c]
                    if new != current[l][r][c]:
                        self.set_keycode(l, r, c, new)
                        changed += 1
        return changed

    def macro_count(self):
        return self.command(ID_DYNAMIC_KEYMAP_MACRO_GET_COUNT)[1]

    # RGB matrix lighting

    def get_rgb(self):
        def get(value_id):
            return self.command(ID_CUSTOM_GET_VALUE, CHANNEL_RGB_MATRIX, value_id)[3:5]
        return {
            "brightness": get(RGB_MATRIX_BRIGHTNESS)[0],
            "effect": get(RGB_MATRIX_EFFECT)[0],
            "speed": get(RGB_MATRIX_EFFECT_SPEED)[0],
            "hue": get(RGB_MATRIX_COLOR)[0],
            "saturation": get(RGB_MATRIX_COLOR)[1],
        }

    def set_rgb(self, brightness=None, effect=None, speed=None, hue=None, saturation=None):
        """Apply live (not saved). Call save_rgb() to persist."""
        ch = CHANNEL_RGB_MATRIX
        if effect is not None:
            self.command(ID_CUSTOM_SET_VALUE, ch, RGB_MATRIX_EFFECT, effect)
        if brightness is not None:
            self.command(ID_CUSTOM_SET_VALUE, ch, RGB_MATRIX_BRIGHTNESS, brightness)
        if speed is not None:
            self.command(ID_CUSTOM_SET_VALUE, ch, RGB_MATRIX_EFFECT_SPEED, speed)
        if hue is not None or saturation is not None:
            cur = self.get_rgb()
            self.command(ID_CUSTOM_SET_VALUE, ch, RGB_MATRIX_COLOR,
                         cur["hue"] if hue is None else hue,
                         cur["saturation"] if saturation is None else saturation)

    def save_rgb(self):
        self.command(ID_CUSTOM_SAVE, CHANNEL_RGB_MATRIX)

    # Backup

    def backup(self):
        return {
            "format": "yafi-via-keymap",
            "version": 1,
            "vid": FRAMEWORK_VID,
            "pid": self.pid,
            "device": self.info["name"],
            "protocol": self.protocol,
            "layers": self.layers,
            "rows": self.rows,
            "cols": self.cols,
            "keymap": self.read_keymap(),
            "rgb_matrix": self.get_rgb(),
        }

    def restore(self, data):
        if data.get("format") != "yafi-via-keymap":
            raise ViaError("Not a YAFI keymap backup")
        if data.get("pid") != self.pid or data.get("rows") != self.rows or data.get("cols") != self.cols:
            raise ViaError(f"Backup is for a different device ({data.get('device')})")
        changed = self.write_keymap(data["keymap"])
        if "rgb_matrix" in data:
            self.set_rgb(**data["rgb_matrix"])
            self.save_rgb()
        return changed


# ---------------------------------------------------------------------------
# Keycodes (QMK 0.19+ numbering, VIA protocol 12)

_BASIC = {
    0x00: "KC_NO", 0x01: "KC_TRNS",
    0x28: "KC_ENT", 0x29: "KC_ESC", 0x2A: "KC_BSPC", 0x2B: "KC_TAB",
    0x2C: "KC_SPC", 0x2D: "KC_MINS", 0x2E: "KC_EQL", 0x2F: "KC_LBRC",
    0x30: "KC_RBRC", 0x31: "KC_BSLS", 0x32: "KC_NUHS", 0x33: "KC_SCLN",
    0x34: "KC_QUOT", 0x35: "KC_GRV", 0x36: "KC_COMM", 0x37: "KC_DOT",
    0x38: "KC_SLSH", 0x39: "KC_CAPS",
    0x46: "KC_PSCR", 0x47: "KC_SCRL", 0x48: "KC_PAUS", 0x49: "KC_INS",
    0x4A: "KC_HOME", 0x4B: "KC_PGUP", 0x4C: "KC_DEL", 0x4D: "KC_END",
    0x4E: "KC_PGDN", 0x4F: "KC_RGHT", 0x50: "KC_LEFT", 0x51: "KC_DOWN",
    0x52: "KC_UP", 0x53: "KC_NUM", 0x54: "KC_PSLS", 0x55: "KC_PAST",
    0x56: "KC_PMNS", 0x57: "KC_PPLS", 0x58: "KC_PENT",
    0x62: "KC_P0", 0x63: "KC_PDOT", 0x64: "KC_NUBS", 0x65: "KC_APP",
    0x67: "KC_PEQL", 0x74: "KC_EXEC", 0x75: "KC_HELP", 0x77: "KC_SLCT",
    0x78: "KC_STOP", 0x79: "KC_AGIN", 0x7A: "KC_UNDO", 0x7B: "KC_CUT",
    0x7C: "KC_COPY", 0x7D: "KC_PSTE", 0x7E: "KC_FIND",
    0xA5: "KC_PWR", 0xA6: "KC_SLEP", 0xA7: "KC_WAKE",
    0xA8: "KC_MUTE", 0xA9: "KC_VOLU", 0xAA: "KC_VOLD", 0xAB: "KC_MNXT",
    0xAC: "KC_MPRV", 0xAD: "KC_MSTP", 0xAE: "KC_MPLY", 0xAF: "KC_MSEL",
    0xB0: "KC_EJCT", 0xB1: "KC_MAIL", 0xB2: "KC_CALC", 0xB3: "KC_MYCM",
    0xB4: "KC_WSCH", 0xB5: "KC_WHOM", 0xB6: "KC_WBAK", 0xB7: "KC_WFWD",
    0xB8: "KC_WSTP", 0xB9: "KC_WREF", 0xBA: "KC_WFAV", 0xBB: "KC_MFFD",
    0xBC: "KC_MRWD", 0xBD: "KC_BRIU", 0xBE: "KC_BRID", 0xBF: "KC_CPNL",
    0xC0: "KC_ASST", 0xC1: "KC_MCTL", 0xC2: "KC_LPAD",
    0xE0: "KC_LCTL", 0xE1: "KC_LSFT", 0xE2: "KC_LALT", 0xE3: "KC_LGUI",
    0xE4: "KC_RCTL", 0xE5: "KC_RSFT", 0xE6: "KC_RALT", 0xE7: "KC_RGUI",
}
for _i in range(26):
    _BASIC[0x04 + _i] = "KC_" + chr(ord("A") + _i)
for _i in range(9):
    _BASIC[0x1E + _i] = f"KC_{_i + 1}"
    _BASIC[0x59 + _i] = f"KC_P{_i + 1}"
_BASIC[0x27] = "KC_0"
for _i in range(12):
    _BASIC[0x3A + _i] = f"KC_F{_i + 1}"
    _BASIC[0x68 + _i] = f"KC_F{_i + 13}"

_SPECIAL = {
    0x7800: "BL_ON", 0x7801: "BL_OFF", 0x7802: "BL_TOGG", 0x7803: "BL_DOWN",
    0x7804: "BL_UP", 0x7805: "BL_STEP", 0x7806: "BL_BRTG",
    0x7820: "RGB_TOG", 0x7821: "RGB_MOD", 0x7822: "RGB_RMOD",
    0x7823: "RGB_HUI", 0x7824: "RGB_HUD", 0x7825: "RGB_SAI",
    0x7826: "RGB_SAD", 0x7827: "RGB_VAI", 0x7828: "RGB_VAD",
    0x7829: "RGB_SPI", 0x782A: "RGB_SPD",
    0x7C00: "QK_BOOT", 0x7C01: "QK_REBOOT", 0x7C03: "QK_CLEAR_EEPROM",
}

KEYCODE_NAMES = {**_BASIC, **_SPECIAL}
NAME_TO_KEYCODE = {v: k for k, v in KEYCODE_NAMES.items()}
NAME_TO_KEYCODE.update({"KC_TRANSPARENT": 0x01, "_______": 0x01, "XXXXXXX": 0x00,
                        "EE_CLR": 0x7C03, "KC_ENTER": 0x28, "KC_ESCAPE": 0x29})

# Short, human labels for the key grid
_LABELS = {
    "KC_NO": "", "KC_TRNS": "▽", "KC_ENT": "Enter", "KC_ESC": "Esc",
    "KC_BSPC": "Bksp", "KC_TAB": "Tab", "KC_SPC": "Space", "KC_MINS": "-",
    "KC_EQL": "=", "KC_LBRC": "[", "KC_RBRC": "]", "KC_BSLS": "\\",
    "KC_SCLN": ";", "KC_QUOT": "'", "KC_GRV": "`", "KC_COMM": ",",
    "KC_DOT": ".", "KC_SLSH": "/", "KC_CAPS": "Caps", "KC_PSCR": "PrtSc",
    "KC_SCRL": "ScrLk", "KC_PAUS": "Pause", "KC_INS": "Ins", "KC_HOME": "Home",
    "KC_PGUP": "PgUp", "KC_DEL": "Del", "KC_END": "End", "KC_PGDN": "PgDn",
    "KC_RGHT": "→", "KC_LEFT": "←", "KC_DOWN": "↓", "KC_UP": "↑",
    "KC_NUM": "Num", "KC_PSLS": "/", "KC_PAST": "*", "KC_PMNS": "-",
    "KC_PPLS": "+", "KC_PENT": "Enter", "KC_PDOT": ".", "KC_PEQL": "=",
    "KC_APP": "Menu", "KC_MUTE": "Mute", "KC_VOLU": "Vol+", "KC_VOLD": "Vol-",
    "KC_MNXT": "Next", "KC_MPRV": "Prev", "KC_MSTP": "Stop", "KC_MPLY": "Play",
    "KC_CALC": "Calc", "KC_MAIL": "Mail", "KC_MYCM": "PC", "KC_BRIU": "Bri+",
    "KC_BRID": "Bri-", "KC_UNDO": "Undo", "KC_CUT": "Cut", "KC_COPY": "Copy",
    "KC_PSTE": "Paste", "KC_FIND": "Find", "KC_SLEP": "Sleep", "KC_PWR": "Power",
    "RGB_TOG": "RGB\nToggle", "RGB_MOD": "RGB\nNext", "RGB_RMOD": "RGB\nPrev",
    "RGB_HUI": "Hue+", "RGB_HUD": "Hue-", "RGB_SAI": "Sat+", "RGB_SAD": "Sat-",
    "RGB_VAI": "Bright+", "RGB_VAD": "Bright-", "RGB_SPI": "Speed+",
    "RGB_SPD": "Speed-", "BL_STEP": "BL\nStep", "QK_BOOT": "Boot",
    "QK_CLEAR_EEPROM": "Clear\nEEPROM",
}

_MOD_NAMES = [(0x01, "CTL"), (0x02, "SFT"), (0x04, "ALT"), (0x08, "GUI")]
_LAYER_FUNCS = [  # (base, name)
    (0x5200, "TO"), (0x5220, "MO"), (0x5240, "DF"), (0x5260, "TG"),
    (0x5280, "OSL"), (0x52C0, "TT"),
]


def _mods_prefix(mods):
    side = "R" if mods & 0x10 else "L"
    return [side + n for bit, n in _MOD_NAMES if mods & bit]


def keycode_to_name(kc):
    """QMK-style expression for a keycode, e.g. 'LCTL(KC_F13)' or 'MO(2)'."""
    if kc in KEYCODE_NAMES:
        return KEYCODE_NAMES[kc]
    if 0x0100 <= kc <= 0x1FFF:
        inner = keycode_to_name(kc & 0xFF)
        for m in reversed(_mods_prefix((kc >> 8) & 0x1F)):
            inner = f"{m}({inner})"
        return inner
    if 0x4000 <= kc <= 0x4FFF:
        return f"LT({(kc >> 8) & 0xF}, {keycode_to_name(kc & 0xFF)})"
    for base, name in _LAYER_FUNCS:
        if base <= kc < base + 0x20:
            return f"{name}({kc - base})"
    if 0x7700 <= kc <= 0x777F:
        return f"QK_MACRO_{kc - 0x7700}"
    return f"0x{kc:04X}"


def keycode_label(kc):
    """Short label for the on-screen key."""
    name = keycode_to_name(kc)
    if name in _LABELS:
        return _LABELS[name]
    m = re.fullmatch(r"KC_P(\d)", name)
    if m:
        return m.group(1)
    if name.startswith("KC_"):
        return name[3:]
    if name.startswith("QK_MACRO_"):
        return "M" + name[9:]
    # Modifier combos: LCTL(LSFT(KC_F13)) -> Ctrl+Shift+F13
    mods = []
    rest = name
    while (m := re.fullmatch(r"([LR])(CTL|SFT|ALT|GUI)\((.*)\)", rest)):
        mods.append({"CTL": "Ctrl", "SFT": "Shift", "ALT": "Alt", "GUI": "Super"}[m.group(2)])
        rest = m.group(3)
    if mods:
        return "+".join(mods) + "+" + keycode_label(NAME_TO_KEYCODE.get(rest, 0))
    return name


def parse_keycode(text):
    """Parse 'KC_F13', 'LCTL(LSFT(KC_F13))', 'MO(2)', 'LT(1, KC_A)', 'M3', '0x5221'."""
    t = text.strip().upper().replace(" ", "")
    if not t:
        raise ValueError("Empty keycode")
    if re.fullmatch(r"0X[0-9A-F]{1,4}", t):
        return int(t, 16)
    if t in NAME_TO_KEYCODE:
        return NAME_TO_KEYCODE[t]
    if not t.startswith(("KC_", "QK_", "RGB_", "BL_")) and "KC_" + t in NAME_TO_KEYCODE:
        return NAME_TO_KEYCODE["KC_" + t]
    m = re.fullmatch(r"(?:QK_MACRO_|MACRO|M)(\d+)", t)
    if m:
        return 0x7700 + int(m.group(1))
    m = re.fullmatch(r"([LR])(CTL|SFT|ALT|GUI)\((.*)\)", t)
    if m:
        bit = {"CTL": 0x01, "SFT": 0x02, "ALT": 0x04, "GUI": 0x08}[m.group(2)]
        inner = parse_keycode(m.group(3))
        if inner > 0x1FFF:
            raise ValueError("Modifiers can only wrap basic keys")
        mods = ((inner >> 8) & 0x1F) | bit | (0x10 if m.group(1) == "R" else 0)
        return (mods << 8) | (inner & 0xFF)
    m = re.fullmatch(r"LT\((\d+),(.*)\)", t)
    if m:
        return 0x4000 | (int(m.group(1)) & 0xF) << 8 | (parse_keycode(m.group(2)) & 0xFF)
    m = re.fullmatch(r"(TO|MO|DF|TG|OSL|TT)\((\d+)\)", t)
    if m:
        return dict((n, b) for b, n in _LAYER_FUNCS)[m.group(1)] + int(m.group(2))
    raise ValueError(f"Unknown keycode: {text}")


# Picker groups: (group title, [keycode names])
KEYCODE_GROUPS = [
    ("Macro keys (F13–F24)", [f"KC_F{i}" for i in range(13, 25)]),
    ("Letters", [f"KC_{chr(c)}" for c in range(ord("A"), ord("Z") + 1)]),
    ("Numbers", [f"KC_{i}" for i in list(range(1, 10)) + [0]]),
    ("Numpad", ["KC_NUM", "KC_PSLS", "KC_PAST", "KC_PMNS", "KC_PPLS", "KC_PENT",
                "KC_PDOT", "KC_PEQL"] + [f"KC_P{i}" for i in range(10)]),
    ("Function keys", [f"KC_F{i}" for i in range(1, 13)]),
    ("Editing & navigation", ["KC_ESC", "KC_TAB", "KC_ENT", "KC_BSPC", "KC_DEL",
                              "KC_INS", "KC_SPC", "KC_HOME", "KC_END", "KC_PGUP",
                              "KC_PGDN", "KC_UP", "KC_DOWN", "KC_LEFT", "KC_RGHT",
                              "KC_PSCR", "KC_SCRL", "KC_PAUS", "KC_CAPS", "KC_APP"]),
    ("Symbols", ["KC_MINS", "KC_EQL", "KC_LBRC", "KC_RBRC", "KC_BSLS", "KC_SCLN",
                 "KC_QUOT", "KC_GRV", "KC_COMM", "KC_DOT", "KC_SLSH"]),
    ("Modifiers", ["KC_LCTL", "KC_LSFT", "KC_LALT", "KC_LGUI",
                   "KC_RCTL", "KC_RSFT", "KC_RALT", "KC_RGUI"]),
    ("Media & system", ["KC_MUTE", "KC_VOLU", "KC_VOLD", "KC_MPLY", "KC_MNXT",
                        "KC_MPRV", "KC_MSTP", "KC_BRIU", "KC_BRID", "KC_CALC",
                        "KC_MAIL", "KC_MYCM", "KC_WHOM", "KC_WSCH", "KC_SLEP"]),
    ("Layers", [f"MO({i})" for i in range(4)] + [f"TG({i})" for i in range(4)]
     + [f"TO({i})" for i in range(4)]),
    ("Lighting", ["RGB_TOG", "RGB_MOD", "RGB_RMOD", "RGB_VAI", "RGB_VAD",
                  "RGB_HUI", "RGB_HUD", "RGB_SAI", "RGB_SAD", "RGB_SPI",
                  "RGB_SPD", "BL_STEP"]),
    ("Special", ["KC_TRNS", "KC_NO", "QK_CLEAR_EEPROM", "QK_BOOT"]),
]
