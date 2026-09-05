"""Keyboard dispatch regressions; no display or project/autosave required."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from clip_editor.ui import EditorApp, EditorWindow, Gdk, Gtk


class KeyboardOwnershipTest(unittest.TestCase):
    def make_window(self, timeline_focus=True):
        timeline = SimpleNamespace(
            move_clip_selection=Mock(), move_navigation_track=Mock(),
        )
        return SimpleNamespace(
            timeline=timeline,
            get_focus=lambda: timeline if timeline_focus else object(),
            _on_undo=Mock(), _on_redo=Mock(), _on_play=Mock(),
            _show_command_line=Mock(), _split_selected_clip=Mock(),
            _delete_selected_clip=Mock(return_value=True),
            _guard_edit=Mock(return_value=True), _space_held=False,
        )

    def key(self, win, key, modifiers=0):
        return EditorWindow._on_key_pressed(win, None, key, 0, modifiers)

    def test_other_controls_keep_timeline_and_history_keys(self):
        win = self.make_window(False)
        for key in (Gdk.KEY_h, Gdk.KEY_j, Gdk.KEY_k, Gdk.KEY_l,
                    Gdk.KEY_t, Gdk.KEY_colon, Gdk.KEY_space,
                    Gdk.KEY_Delete, Gdk.KEY_Escape):
            self.assertFalse(self.key(win, key))
        for key in (Gdk.KEY_z, Gdk.KEY_y):
            self.assertFalse(self.key(win, key, Gdk.ModifierType.CONTROL_MASK))
        win._on_undo.assert_not_called()
        win._on_redo.assert_not_called()
        win._guard_edit.assert_not_called()
        win.timeline.move_clip_selection.assert_not_called()

    def test_timeline_dispatch_and_rendered_preview_guard(self):
        win = self.make_window()
        self.assertTrue(self.key(win, Gdk.KEY_l))
        win.timeline.move_clip_selection.assert_called_once_with(1, extend=False)
        self.key(win, Gdk.KEY_z, Gdk.ModifierType.CONTROL_MASK)
        win._on_undo.assert_called_once()
        win._guard_edit.return_value = False
        self.assertTrue(self.key(win, Gdk.KEY_t))
        self.assertTrue(self.key(win, Gdk.KEY_Delete))
        win._split_selected_clip.assert_not_called()
        win._delete_selected_clip.assert_not_called()

    def test_application_accelerators_cannot_bypass_focus_guard(self):
        app = SimpleNamespace(set_accels_for_action=Mock())
        EditorApp._install_accels(app)
        accelerators = dict(call.args for call in app.set_accels_for_action.call_args_list)
        self.assertEqual(accelerators['win.save'], ['<Control>s'])
        self.assertEqual(accelerators['win.open-project'], ['<Control>o'])
        self.assertNotIn('win.undo', accelerators)
        self.assertNotIn('win.redo', accelerators)

    def test_command_escape_returns_focus_to_timeline(self):
        win = SimpleNamespace(_hide_command_line=Mock())
        self.assertTrue(EditorWindow._on_command_key_pressed(
            win, None, Gdk.KEY_Escape, 0, 0))
        win._hide_command_line.assert_called_once()

    def test_focus_can_return_without_a_stuck_space_key(self):
        win = self.make_window()
        self.key(win, Gdk.KEY_space)
        self.key(win, Gdk.KEY_space)
        self.assertEqual(win._on_play.call_count, 1)
        win.get_focus = lambda: None
        EditorWindow._on_key_released(win, None, Gdk.KEY_space, 0, 0)
        win.get_focus = lambda: win.timeline
        self.key(win, Gdk.KEY_space)
        self.assertEqual(win._on_play.call_count, 2)

    def test_controller_dispatch_with_real_gtk_focus(self):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            self.skipTest('GTK display unavailable')
        window = Gtk.Window()
        box = Gtk.Box()
        window.set_child(box)
        timeline = Gtk.DrawingArea(focusable=True)
        controls = [Gtk.Entry(), Gtk.SpinButton.new_with_range(0, 100, 1),
                    Gtk.Button(label='Inspector action'), Gtk.Entry()]
        for widget in [timeline, *controls]:
            box.append(widget)
        win = self.make_window()
        win.timeline = timeline
        win.get_focus = window.get_focus
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect('key-pressed', lambda c, k, code, state:
                           EditorWindow._on_key_pressed(win, c, k, code, state))
        window.add_controller(controller)
        try:
            for widget in controls:
                widget.grab_focus()
                for key in (Gdk.KEY_t, Gdk.KEY_Delete, Gdk.KEY_space, Gdk.KEY_h):
                    self.assertFalse(controller.emit('key-pressed', key, 0, 0))
                self.assertFalse(controller.emit('key-pressed', Gdk.KEY_z, 0,
                                                 Gdk.ModifierType.CONTROL_MASK))
            timeline.grab_focus()
            self.assertIs(window.get_focus(), timeline)
            self.assertTrue(controller.emit('key-pressed', Gdk.KEY_t, 0, 0))
            win._split_selected_clip.assert_called_once()
        finally:
            window.destroy()
