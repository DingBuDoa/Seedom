"""Adaptive registration retries remain bounded and never rewrite output frames."""
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import numpy as np
from vggt_geometry import fit_camera_similarity
from vggt_worker import larger_overlap, registration_points, run


class AdaptiveTests(unittest.TestCase):
    def test_retry_overlap_grows_within_retained_history_and_leaves_new_frame(self):
        previous = dict.fromkeys(range(12, 32))
        shared = 4
        overlaps = []
        while True:
            shared = larger_overlap(shared, 20, 32, previous)
            if shared is None:
                break
            overlaps.append(shared)
        self.assertEqual(overlaps, [8, 16, 19])
        self.assertEqual(larger_overlap(4, 8, 8, dict.fromkeys(range(8))), 7)

    def test_missing_history_is_never_requested(self):
        previous = dict.fromkeys(range(16, 20))
        self.assertIsNone(larger_overlap(4, 20, 20, previous))
        self.assertIsNone(larger_overlap(4, 5, 20, dict.fromkeys(range(20))))

    def test_refresh_keeps_new_camera_space_rays_in_exported_pose(self):
        predicted = np.column_stack((np.eye(3), [.2, -.1, .3]))
        exported = np.column_stack((np.diag([-1., -1., 1.]), [1., 2., 3.]))
        original = exported.copy()
        points = np.array([[.1, .2, 2.], [.3, -.2, 4.]])
        refreshed = registration_points(points, predicted, exported)
        np.testing.assert_allclose(refreshed @ exported[:, :3].T + exported[:, 3],
                                   points @ predicted[:, :3].T + predicted[:, 3])
        np.testing.assert_array_equal(exported, original)

    def run_worker(self, failure='once', oom=False):
        import torch
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        files = directory / 'images'; files.mkdir()
        for i in range(14):
            (files / f'frame_{i+1:06d}.jpg').write_bytes(b'fixture')
        args = SimpleNamespace(source=str(directory), checkpoint='fixture', images=str(files),
            output=str(directory / 'model'), device='cpu', window=8, resolution=384, color=1)
        class Model:
            def __init__(self, **kwargs): pass
            def load_state_dict(self, *args, **kwargs): pass
            def eval(self): return self
            def to(self, *args): return self
        inferred = []
        comparisons = []
        def predict(model, paths, resolution, device):
            indices = [int(p.stem.split('_')[-1]) - 1 for p in paths]
            inferred.append(indices)
            if oom and len(inferred) == 3:
                raise torch.OutOfMemoryError('fixture')
            cameras = np.array([np.column_stack((np.eye(3), [-i*.001, 0, 0])) for i in indices])
            intrinsics = np.repeat(np.array([[[64., 0, 32], [0, 64, 32], [0, 0, 1]]]), len(paths), axis=0)
            depth = np.full((len(paths), 64, 64), 2.)
            return cameras, intrinsics, depth, np.ones_like(depth), np.full((len(paths), 64, 64, 3), 100, np.uint8), \
                [(64, 64, (0, 0, 64, 64), (64, 64))] * len(paths)
        def fit(*values):
            comparisons.append(len(values[0]))
            if failure == 'always' or len(comparisons) == 1 or failure == 'pin' and len(comparisons) <= 2:
                raise ValueError('fixture: unreliable overlap')
            return fit_camera_similarity(*values)
        with patch.dict(sys.modules, {'vggt_omega.models': SimpleNamespace(VGGTOmega=Model)}), \
             patch('torch.load', return_value={}), patch('vggt_worker.infer', side_effect=predict), \
             patch('vggt_worker.fit_camera_similarity', side_effect=fit), patch('sys.stdout', io.StringIO()):
            if failure == 'always':
                with self.assertRaisesRegex(ValueError, '仍无法可靠连接'):
                    run(args)
                self.assertFalse((directory / 'model/vggt-report.json').exists())
                self.assertEqual(comparisons, [4, 7, 7, 7])
                return
            run(args)
        report = json.loads((directory / 'model/vggt-report.json').read_text())
        self.assertEqual(report['metrics']['registered_images'], 14)
        self.assertEqual(report['alignment_retries'], 2 if failure == 'pin' else 1)
        if failure == 'pin':
            self.assertTrue(any(a.get('anchor') == 1 for a in report['alignments']))
        with (directory / 'model/images.bin').open('rb') as stream:
            self.assertEqual(struct.unpack('<Q', stream.read(8))[0], 14)
        lines = [line for line in (directory / 'model/images.txt').read_text().splitlines() if not line.startswith('#')]
        names = [line.split()[-1] for line in lines[::2]]
        self.assertEqual(names, [f'frame_{i+1:06d}.jpg' for i in range(14)])
        self.assertTrue(all(len(batch) <= 8 for batch in inferred))
        self.assertEqual(args.window, 5 if oom else 8)

    def test_context_retry_exports_each_frame_exactly_once(self):
        self.run_worker()

    def test_oom_during_context_retry_shrinks_batch_without_skipping_frames(self):
        self.run_worker(oom=True)

    def test_reference_anchor_retry_never_duplicates_or_skips_frames(self):
        self.run_worker(failure='pin')

    def test_persistent_inconsistency_stops_instead_of_looping_or_claiming_success(self):
        self.run_worker(failure='always')


if __name__ == '__main__':
    unittest.main()
