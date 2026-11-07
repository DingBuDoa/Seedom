"""Device selection through native commands, GPU probing and persisted options."""
from pathlib import Path
from dataclasses import asdict
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from hardware import get_hardware, hardware_summary, settings_signature
from tracker_core import Options, Pipeline


class GpuSelectionTests(unittest.TestCase):
    def test_probe_and_history_are_separate_for_each_gpu(self):
        profiles = [dict(gpu_index=index, fingerprint=f'gpu-{index}', cuda_usable=True,
                         devices=[dict(index=0, name='GPU A', total_bytes=8 * 2**30),
                                  dict(index=1, name='GPU B', total_bytes=24 * 2**30)])
                    for index in (0, 1)]
        def probe(args, **kwargs):
            index = int(args[-1])
            return subprocess.CompletedProcess(args, 0, json.dumps(profiles[index]), '')
        with patch('hardware.subprocess.run', side_effect=probe) as called:
            first = get_hardware('test-gpu-runtime.exe', force=True, gpu_index=0)
            second = get_hardware('test-gpu-runtime.exe', force=True, gpu_index=1)
            self.assertEqual(get_hardware('test-gpu-runtime.exe', gpu_index=0), first)
            self.assertEqual(get_hardware('test-gpu-runtime.exe', gpu_index=1), second)
            self.assertEqual(called.call_count, 2)
        self.assertIn('GPU 1: GPU B · 显存 24.0 GiB', hardware_summary(second))
        self.assertNotEqual(settings_signature(Options()), settings_signature(Options(gpu_index=1)))
        self.assertNotIn('gpu_index', settings_signature(Options()))
        self.assertEqual(Options(**asdict(Options(gpu_index=1))).validate().gpu_index, 1)
        for invalid in (-1, True, 1.5, '1'):
            with self.assertRaises(ValueError):
                Options(gpu_index=invalid).validate()

    def test_both_solvers_and_decode_fallback_use_selected_gpu(self):
        for backend, decode_fallback in (('fastmap', True), ('glomap', False)):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as directory:
                root = Path(directory)
                video = root / 'sample.mp4'; video.write_bytes(b'test')
                options = Options(backend=backend, gpu_index=1, output_root=str(root / 'output'))
                pipeline = Pipeline(options)
                calls = {}
                def command(args, label, cwd=None, env=None):
                    args = [str(arg) for arg in args]
                    calls[label] = (args, env)
                    if label == '01-frames' and decode_fallback:
                        raise RuntimeError('CUDA decoder unavailable')
                    if label in ('01-frames', '01-frames-cpu'):
                        images = Path(args[-1]).parent
                        for number in range(3):
                            (images / f'frame_{number:06}.jpg').write_bytes(b'test')
                    if label == '02-features':
                        Path(args[args.index('--database_path') + 1]).touch()
                    if label in ('04-fastmap', '04-glomap'):
                        output = Path(args[args.index('--output_dir' if backend == 'fastmap' else '--output_path') + 1])
                        model = output / ('sparse/0' if backend == 'fastmap' else '0')
                        model.mkdir(parents=True)
                        for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
                            (model / name).write_bytes(b'test')
                        if backend == 'glomap':
                            (pipeline.log_dir / '04-glomap.log').write_text(
                                '[GPU] Global positioning: CUDA_SPARSE\n[GPU] Bundle adjustment: CUDA_SPARSE', encoding='utf-8')
                    if label == '05-model-analysis':
                        (pipeline.log_dir / '05-model-analysis.log').write_text(
                            'Registered images: 3\nPoints: 10\nMean reprojection error: 0.2', encoding='utf-8')
                profile = dict(gpu_index=1, cuda_usable=True, devices=[dict(index=1, name='GPU B', total_bytes=24 * 2**30)])
                with patch.object(pipeline, 'preflight'), patch.object(pipeline, 'command', side_effect=command), \
                     patch('tracker_core.find_binary', side_effect=lambda folder, name: root / name), \
                     patch('tracker_core.get_hardware', return_value=profile) as probe, \
                     patch('tracker_core.describe_workload', return_value=None), \
                     patch('tracker_core.database_counts', return_value=dict(images=3, pairs=3)):
                    result = pipeline.run(video)
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(probe.call_args.kwargs['gpu_index'], 1)
                for label, flag in (('02-features', '--SiftExtraction.gpu_index'), ('03-matching', '--SiftMatching.gpu_index')):
                    args = calls[label][0]
                    self.assertEqual(args[args.index(flag) + 1], '1')
                args = calls['01-frames'][0]
                self.assertEqual(args[args.index('-hwaccel_device') + 1], '1')
                if decode_fallback:
                    self.assertNotIn('-hwaccel_device', calls['01-frames-cpu'][0])
                    self.assertNotIn('-hwaccel', calls['01-frames-cpu'][0])
                if backend == 'fastmap':
                    args = calls['04-fastmap'][0]
                    self.assertEqual(args[args.index('--device') + 1], 'cuda:1')
                else:
                    args = calls['04-glomap'][0]
                    for stage in ('GlobalPositioning', 'BundleAdjustment'):
                        self.assertEqual(args[args.index(f'--{stage}.gpu_index') + 1], '1')
                    self.assertNotIn('CUDA_VISIBLE_DEVICES', calls['04-glomap'][1])
                    self.assertEqual(calls['04-gpu-preflight'][1]['CUDA_VISIBLE_DEVICES'], '1')

    def test_missing_selected_gpu_stops_before_processing(self):
        pipeline = Pipeline(Options(gpu_index=1))
        with patch.object(pipeline, 'preflight'), patch.object(pipeline, 'prepare_video') as prepare, \
             patch('tracker_core.get_hardware', return_value=dict(cuda_usable=True, devices=[dict(index=0)])):
            with self.assertRaisesRegex(RuntimeError, 'GPU 1 不可用'):
                pipeline.run('unused.mp4')
            prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
