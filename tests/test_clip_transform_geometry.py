import unittest

from clip_editor.aspects import cover_source_placement


class ClipTransformGeometryTest(unittest.TestCase):
    def test_square_source_pans_inside_portrait_frame_without_margin(self) -> None:
        # A 1024-square clip in a 576x1024 portrait frame is a 1024x1024
        # source layer. Moving it right changes which source pixels are shown;
        # it does not move a 576x1024 crop and reveal empty pixels.
        centered = cover_source_placement(1024, 1024, 576, 1024)
        self.assertEqual((centered.w, centered.h, centered.x, centered.y), (1024, 1024, -224, 0))

        moved_right = cover_source_placement(
            1024, 1024, 576, 1024, transform_x=160
        )
        self.assertEqual((moved_right.w, moved_right.h, moved_right.x, moved_right.y), (1024, 1024, -64, 0))
        self.assertLessEqual(moved_right.x, 0)
        self.assertGreaterEqual(moved_right.x + moved_right.w, 576)

    def test_translation_is_clamped_to_source_edges(self) -> None:
        placement = cover_source_placement(
            1024, 1024, 576, 1024, transform_x=9999
        )
        self.assertEqual(placement.x, 0)
        placement = cover_source_placement(
            1024, 1024, 576, 1024, transform_x=-9999
        )
        self.assertEqual(placement.x, 576 - 1024)

    def test_scale_never_shrinks_below_cover(self) -> None:
        placement = cover_source_placement(1024, 1024, 576, 1024, scale=0.5)
        self.assertEqual((placement.w, placement.h), (1024, 1024))

