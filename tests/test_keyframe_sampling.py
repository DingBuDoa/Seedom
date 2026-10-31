"""Starting frame offsets for Blender camera exports."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from frame_spacing import export_frame_numbers


class KeyframeSamplingTests(unittest.TestCase):
    def test_zero_offset_starts_camera_keys_at_frame_one(self):
        self.assertEqual(export_frame_numbers(7), list(range(1, 8)))

    def test_offset_moves_all_camera_keys_and_related_frames_together(self):
        self.assertEqual(export_frame_numbers(5, 30), [31, 32, 33, 34, 35])
        self.assertEqual(export_frame_numbers(5, 1), [2, 3, 4, 5, 6])
        self.assertEqual(len(export_frame_numbers(5, 100)), 5)

    def test_offset_must_be_bounded_integer(self):
        for offset in (-1, 10001, 1.5, True):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                export_frame_numbers(2, offset)
        for count in (-1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                export_frame_numbers(count)


if __name__ == '__main__':
    unittest.main()
