"""Media disposal closes pipelines even when other references retain the stream."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from clip_editor.ui import EditorWindow


class MediaShutdownTest(unittest.TestCase):
    def test_dispose_detaches_callbacks_and_explicitly_closes_source(self):
        calls = Mock()
        media = Mock()
        preview = Mock()
        calls.attach_mock(media, 'media')
        calls.attach_mock(preview, 'preview')
        win = SimpleNamespace(_vmedia=media, _vmedia_path='video.mp4',
                              _prep_handler=42, preview=preview)
        EditorWindow._dispose_media(win)
        self.assertIsNone(win._vmedia)
        self.assertIsNone(win._vmedia_path)
        self.assertEqual(win._prep_handler, 0)
        self.assertEqual([c[0] for c in calls.mock_calls],
                         ['media.disconnect', 'preview.set_media', 'media.pause', 'media.clear'])
        EditorWindow._dispose_media(win)
        media.clear.assert_called_once()

    def test_window_close_disposes_media_and_runs_cleanup_once(self):
        win = SimpleNamespace(
            _closed=False, _shutdown_handler=1, _ckpt_src=0,
            get_application=Mock(return_value=Mock()),
            _abandon_preview_render=Mock(), _stop=Mock(),
            _reset_compiled_preview_flags=Mock(), _flush_autosave=Mock(),
            _dispose_media=Mock(),
        )
        self.assertFalse(EditorWindow._on_close(win))
        self.assertFalse(EditorWindow._on_close(win))
        win._dispose_media.assert_called_once()
        win._flush_autosave.assert_called_once()
        win.get_application.return_value.disconnect.assert_called_once_with(1)
