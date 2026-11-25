import unittest

from clip_editor.commands import EditorCommand, parse_command


class CommandGrammarTest(unittest.TestCase):
    def test_parses_render_preview(self) -> None:
        self.assertEqual(parse_command(":rp"), EditorCommand("render_preview"))

    def test_parses_each_supported_compact_aspect_ratio(self) -> None:
        expected = {
            "r916": "9:16",
            "r34": "3:4",
            "r45": "4:5",
            "r11": "1:1",
            "r43": "4:3",
            "r169": "16:9",
        }
        for command, aspect in expected.items():
            with self.subTest(command=command):
                self.assertEqual(parse_command(command), EditorCommand("aspect", aspect))

    def test_unknown_commands_have_no_dispatch(self) -> None:
        self.assertIsNone(parse_command("r235"))
        self.assertIsNone(parse_command("export"))

    def test_parses_grain_strength_and_whitespace(self) -> None:
        for text, strength in ((":grain 0", "0"), ("grain 25", "25"),
                               ("  :GRAIN\t100  ", "100")):
            with self.subTest(text=text):
                self.assertEqual(parse_command(text), EditorCommand("grain", strength))

    def test_rejects_invalid_grain_strengths(self) -> None:
        for text in ("grain", "grain -1", "grain 101", "grain 1.5", "grain nan",
                     "grain inf", "grain off", "grain 10 20", "grain25"):
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))

