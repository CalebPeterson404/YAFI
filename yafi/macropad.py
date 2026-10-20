# macropad.py
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

import os
import json
import time
import traceback

from gi.repository import Gtk, Adw, GLib, Gio

from . import via


class KeycodePicker(Adw.Dialog):
    """Searchable list of keycodes plus a free-form QMK expression entry."""

    def __init__(self, current, on_pick):
        super().__init__(title="Choose a key", content_width=420, content_height=560)
        self.on_pick = on_pick

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)

        self.search = Gtk.SearchEntry(placeholder_text="Search keys (e.g. F13, volume, layer)")
        box.append(self.search)

        custom = Gtk.Box(spacing=6)
        self.custom_entry = Gtk.Entry(
            hexpand=True, text=via.keycode_to_name(current),
            placeholder_text="QMK keycode, e.g. LCTL(LSFT(KC_F13)) or MO(3)",
        )
        self.custom_entry.connect("activate", self._on_custom)
        apply_btn = Gtk.Button(label="Set")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self._on_custom)
        custom.append(self.custom_entry)
        custom.append(apply_btn)
        box.append(custom)

        self.error_label = Gtk.Label(xalign=0, visible=False, wrap=True)
        self.error_label.add_css_class("error")
        box.append(self.error_label)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.rows = []
        for group, names in via.KEYCODE_GROUPS:
            for name in names:
                kc = via.parse_keycode(name)
                label = via.keycode_label(kc).replace("\n", " ") or "(none)"
                row = Adw.ActionRow(title=GLib.markup_escape_text(label),
                                    subtitle=GLib.markup_escape_text(f"{group} · {name}"),
                                    activatable=True)
                row.kc = kc
                row.search_text = f"{label} {name} {group}".lower()
                row.connect("activated", lambda r: self._pick(r.kc))
                self.listbox.append(row)
                self.rows.append(row)
        self.listbox.set_filter_func(self._filter)
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.listbox)
        box.append(scroller)

        toolbar.set_content(box)
        self.set_child(toolbar)

    def _filter(self, row):
        query = self.search.get_text().strip().lower()
        return all(word in row.search_text for word in query.split())

    def _on_custom(self, *_):
        try:
            kc = via.parse_keycode(self.custom_entry.get_text())
        except ValueError as e:
            self.error_label.set_label(str(e))
            self.error_label.set_visible(True)
            return
        self._pick(kc)

    def _pick(self, kc):
        self.close()
        self.on_pick(kc)


@Gtk.Template(resource_path='/au/stevetech/yafi/ui/macropad.ui')
class MacropadPage(Gtk.Box):
    __gtype_name__ = 'MacropadPage'

    status_row = Gtk.Template.Child()
    reload_button = Gtk.Template.Child()
    keymap_box = Gtk.Template.Child()
    layer_row = Gtk.Template.Child()
    key_grid = Gtk.Template.Child()
    lighting_box = Gtk.Template.Child()
    rgb_save_button = Gtk.Template.Child()
    rgb_effect_row = Gtk.Template.Child()
    rgb_brightness_scale = Gtk.Template.Child()
    rgb_speed_scale = Gtk.Template.Child()
    rgb_hue_scale = Gtk.Template.Child()
    rgb_sat_scale = Gtk.Template.Child()
    backup_box = Gtk.Template.Child()
    backup_row = Gtk.Template.Child()
    export_button = Gtk.Template.Child()
    import_button = Gtk.Template.Child()

    first_run = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.device = None
        self.keymap = None
        self.key_buttons = []
        self._updating = False

    # Setup / connection

    def setup(self, app):
        self.app = app
        if self.first_run:
            self.first_run = False
            self.reload_button.connect("clicked", lambda *_: self.connect_device())
            self.layer_row.connect("notify::selected", lambda *_: self.refresh_keys())
            self.rgb_effect_row.set_model(Gtk.StringList.new(via.RGB_MATRIX_EFFECTS))
            self.rgb_effect_row.connect(
                "notify::selected", lambda row, _: self._on_rgb_changed(effect=row.get_selected()))
            for scale, key in ((self.rgb_brightness_scale, "brightness"),
                               (self.rgb_speed_scale, "speed"),
                               (self.rgb_hue_scale, "hue"),
                               (self.rgb_sat_scale, "saturation")):
                scale.connect("value-changed",
                              lambda s, key=key: self._on_rgb_changed(**{key: int(s.get_value())}))
            self.rgb_save_button.connect("clicked", self._on_rgb_save)
            self.export_button.connect("clicked", self._on_export)
            self.import_button.connect("clicked", self._on_import)
        self.connect_device()

    def _set_status(self, text):
        self.status_row.set_subtitle(GLib.markup_escape_text(text))

    def _set_sensitive(self, enabled):
        for box in (self.keymap_box, self.lighting_box, self.backup_box):
            box.set_sensitive(enabled)

    def connect_device(self):
        if self.device:
            self.device.close()
            self.device = None
        try:
            found = via.find_devices()
            if not found:
                self._set_status("No Framework macropad found. Plug it in, then press reload.")
                self._set_sensitive(False)
                return
            self.device = via.ViaDevice(*found[0])
            self.keymap = self.device.read_keymap()
        except (OSError, via.ViaError) as e:
            traceback.print_exc()
            self.device = None
            self._set_status(f"Could not talk to the macropad: {e}")
            self._set_sensitive(False)
            return

        dev = self.device
        self.status_row.set_title(f"Framework {dev.info['name']}")
        self._set_status(f"Connected · VIA protocol {dev.protocol} · {dev.layers} layers · {dev.path}")
        self._set_sensitive(True)
        self._auto_backup()

        selected = self.layer_row.get_selected()
        names = [f"Layer {i}" + (f" — {dev.info['layer_names'][i]}" if i in dev.info["layer_names"] else "")
                 for i in range(dev.layers)]
        self._updating = True
        self.layer_row.set_model(Gtk.StringList.new(names))
        self.layer_row.set_selected(selected if selected < dev.layers else 0)
        self._updating = False
        self._build_grid()
        self.refresh_keys()
        self.refresh_rgb()

    def _auto_backup(self):
        """Keep a copy of the keymap as first seen, before YAFI changes anything."""
        try:
            folder = os.path.join(GLib.get_user_data_dir(), "yafi")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"macropad-{self.device.pid:04x}-original.json")
            if not os.path.exists(path):
                with open(path, "w") as f:
                    json.dump(self.device.backup(), f, indent=1)
            self.backup_row.set_subtitle(GLib.markup_escape_text(
                f"Save or restore the whole keymap and lighting as JSON. Original keymap kept at {path}"))
        except OSError:
            traceback.print_exc()

    def _device_error(self, e):
        traceback.print_exc()
        self._set_status(f"Lost the macropad ({e}). Press reload.")
        self._set_sensitive(False)

    # Keymap grid

    def _build_grid(self):
        while child := self.key_grid.get_first_child():
            self.key_grid.remove(child)
        self.key_buttons = []
        info = self.device.info
        for i, (row, col) in enumerate(info["layout"]):
            label = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True, max_width_chars=8)
            button = Gtk.Button(child=label, width_request=84, height_request=64)
            button.connect("clicked", lambda _b, r=row, c=col: self._on_key_clicked(r, c))
            self.key_grid.attach(button, i % info["width"], i // info["width"], 1, 1)
            self.key_buttons.append((button, label, row, col))

    def refresh_keys(self):
        if self._updating or not self.device or not self.keymap:
            return
        layer = self.layer_row.get_selected()
        for button, label, row, col in self.key_buttons:
            kc = self.keymap[layer][row][col]
            label.set_label(via.keycode_label(kc))
            button.set_tooltip_text(f"{via.keycode_to_name(kc)}  (matrix {row},{col})")
            if kc == 0x01:
                button.add_css_class("dim-label")
            else:
                button.remove_css_class("dim-label")

    def _on_key_clicked(self, row, col):
        layer = self.layer_row.get_selected()
        current = self.keymap[layer][row][col]

        def picked(kc):
            try:
                self.device.set_keycode(layer, row, col, kc)
                self.keymap[layer][row][col] = self.device.get_keycode(layer, row, col)
            except (OSError, via.ViaError) as e:
                self._device_error(e)
                return
            self.refresh_keys()
            self._set_status(f"Layer {layer}: {via.keycode_to_name(current)} → "
                             f"{via.keycode_to_name(self.keymap[layer][row][col])}")

        KeycodePicker(current, picked).present(self.get_root())

    # Lighting

    def refresh_rgb(self):
        try:
            rgb = self.device.get_rgb()
        except (OSError, via.ViaError) as e:
            self.lighting_box.set_visible(False)
            return
        self.lighting_box.set_visible(True)
        self._updating = True
        self.rgb_effect_row.set_selected(min(rgb["effect"], len(via.RGB_MATRIX_EFFECTS) - 1))
        self.rgb_brightness_scale.set_value(rgb["brightness"])
        self.rgb_speed_scale.set_value(rgb["speed"])
        self.rgb_hue_scale.set_value(rgb["hue"])
        self.rgb_sat_scale.set_value(rgb["saturation"])
        self._updating = False

    def _on_rgb_changed(self, **change):
        if self._updating or not self.device:
            return
        try:
            self.device.set_rgb(**change)
        except (OSError, via.ViaError) as e:
            self._device_error(e)

    def _on_rgb_save(self, *_):
        try:
            self.device.save_rgb()
            self._set_status("Lighting saved to the macropad.")
        except (OSError, via.ViaError) as e:
            self._device_error(e)

    # Backup / restore

    def _json_filter(self):
        f = Gtk.FileFilter(name="JSON files")
        f.add_pattern("*.json")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        return filters

    def _on_export(self, *_):
        dialog = Gtk.FileDialog(title="Export macropad keymap", filters=self._json_filter(),
                                initial_name=time.strftime("macropad-keymap-%Y-%m-%d.json"))

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return  # cancelled
            try:
                data = json.dumps(self.device.backup(), indent=1).encode()
                file.replace_contents(data, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                self._set_status(f"Exported keymap to {file.get_parse_name()}")
            except (OSError, via.ViaError, GLib.Error) as e:
                self._device_error(e)

        dialog.save(self.get_root(), None, done)

    def _on_import(self, *_):
        dialog = Gtk.FileDialog(title="Restore macropad keymap", filters=self._json_filter())

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                ok, contents, _ = file.load_contents(None)
                changed = self.device.restore(json.loads(contents))
            except (ValueError, KeyError, via.ViaError) as e:
                self._set_status(f"Restore failed: {e}")
                return
            except (OSError, GLib.Error) as e:
                self._device_error(e)
                return
            self.keymap = self.device.read_keymap()
            self.refresh_keys()
            self.refresh_rgb()
            self._set_status(f"Restored {file.get_basename()} ({changed} keys changed).")

        dialog.open(self.get_root(), None, done)
