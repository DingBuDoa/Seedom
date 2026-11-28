"""VGGT coordinate, solver routing, progress, persistence and dialog regressions."""
from dataclasses import asdict
import json
import gc
import os
from pathlib import Path
import struct
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PIL import Image
from tracker_core import Options, Pipeline
from tracker_core import video_filters
from task_progress import TaskProgress, stage_plan
from project_library import model_ready, validate_result
from task_storage import restore_jobs
from vggt_geometry import (image_geometry, original_intrinsics, load_images,
                           fit_similarity, fit_camera_similarity, transform_camera, rotation_quaternion, ModelWriter)
from vggt_runtime import worker_args, locate_assets, CHECKPOINT_BYTES
from workload import source_signature
from vggt_worker import check_overlap
from hardware import settings_signature
from estimate import prediction_range
import paths as app_paths
from app_info import DEFAULT_SOLVER, SOLVER_CHOICES, SOLVER_SELECTION_REVISION, preferred_solver


class SolverSelectionTests(unittest.TestCase):
    def test_cli_defaults_to_vggt_and_exposes_only_two_solvers(self):
        import AutoTracker_GUI as cli
        for arguments, expected in (([], 'vggt-omega'), (['--backend', 'glomap'], 'glomap')):
            with patch.object(sys, 'argv', ['seedom', '--video', 'clip.mov', *arguments]), \
                 patch.object(cli, 'Pipeline') as pipeline, patch.object(cli, 'save_sample'), \
                 patch('sys.stdout'):
                pipeline.return_value.run.return_value = {}
                cli.main()
                self.assertEqual(pipeline.call_args.args[0].backend, expected)
        with patch.object(sys, 'argv', ['seedom', '--video', 'clip.mov', '--backend', 'colmap']), \
             patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 2)

    def test_default_and_public_choices_are_vggt_and_legacy_glomap(self):
        self.assertEqual(Options().backend, 'vggt-omega')
        self.assertEqual(DEFAULT_SOLVER, 'vggt-omega')
        self.assertEqual(list(SOLVER_CHOICES), ['vggt-omega', 'glomap'])
        from AutoTracker_GUI import BACKENDS
        self.assertEqual(list(BACKENDS.values()), ['vggt-omega', 'glomap'])

    def test_preference_migrates_once_then_preserves_selected_glomap(self):
        self.assertEqual(preferred_solver({}), 'vggt-omega')
        self.assertEqual(preferred_solver(dict(backend='glomap')), 'vggt-omega')
        self.assertEqual(preferred_solver(dict(backend='colmap')), 'vggt-omega')
        saved = dict(solver_selection_revision=SOLVER_SELECTION_REVISION, backend='glomap')
        self.assertEqual(preferred_solver(saved), 'glomap')
        self.assertEqual(preferred_solver({**saved, 'backend': 'colmap'}), 'vggt-omega')
        self.assertEqual(preferred_solver({**saved, 'backend': []}), 'vggt-omega')

    def test_historical_tasks_keep_their_solver_including_missing_old_default(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
            entries = []
            for backend in ('colmap', 'glomap', 'vggt-omega', None):
                options = asdict(Options(backend=backend or 'glomap'))
                if backend is None:
                    del options['backend']
                name = backend or 'old-default'
                entries.append(dict(id=name, name=name, path='clip.mov', state='等待', options=options))
            queue = Path(tmp) / 'task-queue.json'
            queue.write_text(json.dumps(entries), encoding='utf-8')
            jobs, warnings = restore_jobs(queue)
            self.assertFalse(warnings)
            self.assertEqual([job['options']['backend'] for job in jobs.values()],
                             ['colmap', 'glomap', 'vggt-omega', 'glomap'])


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts')
        self.addCleanup(self.temporary.cleanup)
        workspace = Path(self.temporary.name) / 'GLOMAP GUI'
        self.checkout = workspace / 'src/Seedom'
        self.checkout.mkdir(parents=True)
        self.runtime = workspace / 'archive/legacy-1.x/runtime/python/python.exe'
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b'python fixture')
        root_patch = patch.object(app_paths, 'ROOT', self.checkout)
        python_patch = patch.object(app_paths, 'PYTHON', self.runtime)
        root_patch.start(); self.addCleanup(root_patch.stop)
        python_patch.start(); self.addCleanup(python_patch.stop)

    def test_workspace_runtime_is_not_relocated_and_roundtrips_portably(self):
        self.assertEqual(app_paths.saved_path(self.runtime, self.checkout), self.runtime)
        saved = app_paths.portable_path(self.runtime)
        self.assertTrue(Path(saved).is_absolute())
        self.assertEqual(app_paths.python_runtime_path(saved, self.checkout), self.runtime)
        # The ancestor workspace is not a retired installation, even if a
        # configured external file is temporarily missing.
        missing = self.runtime.with_name('temporarily-missing.exe')
        self.assertEqual(app_paths.saved_path(missing), missing)

    def test_stale_queue_default_is_repaired_for_restore_preflight_and_launch(self):
        stale = 'archive/legacy-1.x/runtime/python/python.exe'
        options = Options(backend='vggt-omega', python=stale)
        queue_file = self.checkout / 'task-queue.json'
        queue_file.write_text(json.dumps([dict(id='retry', path='clip.mov', name='test',
            state='失败', options=asdict(options))]), encoding='utf-8')
        jobs, warnings = restore_jobs(queue_file, str(self.checkout))
        self.assertFalse(warnings)
        self.assertEqual(jobs['retry']['options']['python'], str(self.runtime))
        with patch('vggt_runtime.locate_assets', return_value=(self.checkout, self.checkout)):
            pipeline = Pipeline(options)
            pipeline.preflight()
            self.assertEqual(options.python, str(self.runtime))
            args = worker_args(options, 'images', 'output')
            self.assertEqual(args[0], str(self.runtime))
            # Command building is also safe for an unvalidated saved record.
            self.assertEqual(worker_args(Options(python=stale), 'images', 'output')[0], str(self.runtime))
        self.assertEqual(app_paths.python_runtime_path('runtime/python/python.exe'), self.runtime)

    def test_missing_custom_python_remains_an_error_with_its_path(self):
        missing = self.checkout / 'custom/python.exe'
        self.assertEqual(app_paths.python_runtime_path(missing), missing)
        options = Options(backend='vggt-omega', python=str(missing))
        with patch('vggt_runtime.locate_assets', return_value=(self.checkout, self.checkout)):
            with self.assertRaises(FileNotFoundError) as raised:
                Pipeline(options).preflight()
        self.assertIn(str(missing), str(raised.exception))

    def test_application_owned_paths_still_migrate_on_installation_move(self):
        previous = Path(self.temporary.name) / 'old-install'
        self.assertEqual(app_paths.saved_path(previous / 'output/project', previous),
                         self.checkout / 'output/project')
        self.assertEqual(app_paths.saved_path('output/project'), self.checkout / 'output/project')
        self.assertEqual(app_paths.portable_path(self.checkout / 'output/project'), str(Path('output/project')))


class GeometryTests(unittest.TestCase):
    def test_crop_intrinsics_and_colmap_quaternion_convention(self):
        crop, target = image_geometry(4000, 1000, 512)
        self.assertEqual(crop, (1000, 0, 2000, 1000))
        self.assertEqual(target, (512, 256))
        k = np.array([[256., 0, 256], [0, 256, 128], [0, 0, 1]])
        w, h, parameters = original_intrinsics(k, (4000, 1000, crop, target))
        np.testing.assert_allclose(parameters, [1000, 1000, 2000, 500])
        angle = np.radians(179)
        r = np.array([[np.cos(angle), -np.sin(angle), 0],
                      [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        np.testing.assert_allclose(rotation_quaternion(r), [np.cos(angle/2), 0, 0, np.sin(angle/2)], atol=1e-10)

    def test_similarity_handles_outliers_and_keeps_camera_projection(self):
        rng = np.random.default_rng(10)
        source = rng.normal(size=(500, 3))
        q = np.array([[0., -1, 0], [1., 0, 0], [0, 0, 1]])
        b = np.array([3., -2, 5])
        target = 2.5 * (source @ q.T) + b
        target[:40] += 50
        scale, rotation, translation, error = fit_similarity(source, target)
        self.assertLess(error, 1e-5)
        np.testing.assert_allclose(rotation, q, atol=1e-6)
        self.assertAlmostEqual(scale, 2.5, places=6)
        np.testing.assert_allclose(translation, b, atol=1e-6)
        e = np.concatenate((np.eye(3), np.array([[.1], [.2], [2.]])), axis=1)
        transformed = transform_camera(e, scale, rotation, translation)
        local = (e[:3, :3] @ source.T + e[:3, 3:]).T
        world = scale * (source @ rotation.T) + translation
        remote = (transformed[:3, :3] @ world.T + transformed[:3, 3:]).T
        np.testing.assert_allclose(remote, scale * local, atol=1e-6)

    def test_degenerate_or_unrelated_overlap_rejected(self):
        with self.assertRaises(ValueError):
            fit_similarity(np.zeros((50, 3)), np.zeros((50, 3)))
        rng = np.random.default_rng(5)
        with self.assertRaises(ValueError):
            fit_similarity(rng.normal(size=(100, 3)), rng.normal(size=(100, 3)))
        e = np.concatenate((np.eye(3), np.zeros((3, 1))), axis=1)
        previous = {0: dict(camera=e, points=rng.normal(size=(50, 3)), valid=np.ones(50, bool))}
        moved = e.copy(); moved[0, 3] = 50
        with self.assertRaisesRegex(ValueError, '位置'):
            check_overlap([moved], previous, 0, 1)

    def test_camera_alignment_keeps_pose_under_context_dependent_depth_bias(self):
        # A small coherent depth tilt can fool cloud-only alignment: it gives
        # excellent cloud residuals but moves/rotates all shared cameras.
        rng = np.random.default_rng(7)
        xy = rng.uniform(-.35, .35, (800, 2))
        cloud = np.column_stack((xy, 1 + rng.normal(0, .005, len(xy))))
        angle = np.radians(6)
        tilt = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0],
                         [-np.sin(angle), 0, np.cos(angle)]])
        biased = (cloud - [0, 0, 1]) @ tilt.T + [0, 0, 1]
        cameras = [np.column_stack((np.eye(3), [-j*.0005, 0, 0])) for j in range(4)]
        points, targets = [biased] * 4, [cloud] * 4
        s, q, t, _ = fit_similarity(np.concatenate(points), np.concatenate(targets))
        previous = {j: dict(camera=c, points=cloud, valid=np.ones(len(cloud), bool))
                    for j, c in enumerate(cameras)}
        with self.assertRaisesRegex(ValueError, '位置'):
            check_overlap([transform_camera(c, s, q, t) for c in cameras], previous, 0, 4)
        s, q, t, error = fit_camera_similarity(points, targets, cameras, cameras)
        checks = check_overlap([transform_camera(c, s, q, t) for c in cameras], previous, 0, 4)
        self.assertLess(error, .12)
        self.assertLess(checks['max_position_error'], .001)
        self.assertLess(checks['max_rotation_degrees'], .01)

    def test_camera_alignment_recovers_gauge_and_rejects_unrelated_depths(self):
        rng = np.random.default_rng(3)
        q = np.array([[0., -1, 0], [1., 0, 0], [0, 0, 1]])
        offset = np.array([2., 3., -1.])
        cameras = [np.column_stack((np.eye(3), [-j*.02, 0, 0])) for j in range(4)]
        # Include outlier depths and unequal point counts across overlap frames.
        source = [rng.uniform([-1, -1, 2], [1, 1, 4], (200+j*20, 3)) for j in range(4)]
        target = [2.5 * (a @ q.T) + offset for a in source]
        for points in target:
            points[:10] += 10
        transformed = [transform_camera(c, 2.5, q, offset) for c in cameras]
        scale, rotation, translation, error = fit_camera_similarity(source, target, cameras, transformed)
        self.assertAlmostEqual(scale, 2.5, places=6)
        np.testing.assert_allclose(rotation, q, atol=1e-8)
        np.testing.assert_allclose(translation, offset, atol=1e-8)
        self.assertLess(error, 1e-6)
        with self.assertRaises(ValueError):
            fit_camera_similarity(source, [rng.normal(size=a.shape) for a in source], cameras, transformed)
        # Camera contradictions remain errors, even with identical clouds.
        flipped = np.column_stack((np.diag([-1., -1, 1]), [0, 0, 0]))
        previous = {0: dict(camera=cameras[0], points=source[0], valid=np.ones(len(source[0]), bool))}
        with self.assertRaisesRegex(ValueError, '方向'):
            check_overlap([flipped], previous, 0, 1)

    def test_binary_and_text_model_observations_match(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
            directory = Path(tmp)
            k = np.array([[100., 0, 50], [0, 100, 50], [0, 0, 1]])
            e = np.concatenate((np.eye(3), np.zeros((3, 1))), axis=1)
            geometry = (100, 100, (0, 0, 100, 100), (100, 100))
            with ModelWriter(directory) as writer:
                writer.add_frame('测试 001.png', e, geometry, k, np.array([[0, 0, 2.]]),
                                 np.array([[12, 34, 56]], dtype=np.uint8), (np.array([50]), np.array([50])))
            with (directory / 'images.bin').open('rb') as stream:
                self.assertEqual(struct.unpack('<Q', stream.read(8))[0], 1)
                camera = struct.unpack('<i7di', stream.read(64))
                self.assertEqual(camera[0], 1)
                np.testing.assert_allclose(camera[1:5], [1, 0, 0, 0])
                name = bytearray()
                while (byte := stream.read(1)) != b'\0':
                    self.assertTrue(byte)
                    name.extend(byte)
                self.assertEqual(name.decode('utf-8'), '测试 001.png')
                self.assertEqual(struct.unpack('<Q', stream.read(8))[0], 1)
                self.assertEqual(struct.unpack('<ddq', stream.read(24)), (50., 50., 1))
                self.assertEqual(stream.read(), b'')
            with (directory / 'points3D.bin').open('rb') as stream:
                self.assertEqual(struct.unpack('<Q', stream.read(8))[0], 1)
                point = struct.unpack('<Q3d3BdQii', stream.read())
                self.assertEqual(point[:7], (1, 0., 0., 2., 12, 34, 56))
                self.assertEqual(point[-3:], (1, 1, 0))
            self.assertIn('PINHOLE 100 100', (directory / 'cameras.txt').read_text())
            self.assertIn(b'element vertex 1\n', (directory / 'points3D.ply').read_bytes())
            self.assertFalse((directory / 'points.rgb').exists())


class IntegrationTests(unittest.TestCase):
    def test_vggt_preserves_source_resolution_and_sampling_rate(self):
        # A restored/CLI task may still carry the old GLOMAP half-size setting.
        vggt = Options(backend='vggt-omega', image_scale=.5, fps=12).validate()
        self.assertEqual(vggt.image_scale, 1.0)
        self.assertEqual(video_filters(vggt), 'fps=12')
        self.assertEqual(video_filters(vggt, original_size=True), 'fps=12')
        self.assertEqual(video_filters(Options(backend='vggt-omega').validate()), '')
        glomap = Options(backend='glomap', image_scale=.5, fps=12).validate()
        self.assertEqual(glomap.image_scale, .5)
        self.assertIn('scale=', video_filters(glomap))
        self.assertEqual(video_filters(glomap, original_size=True), 'fps=12')

    def test_solver_options_keep_legacy_and_clear_irrelevant_zoom(self):
        self.assertEqual(Options(backend='glomap').validate().backend, 'glomap')
        self.assertEqual(Options(backend='colmap').validate().backend, 'colmap')
        vggt = Options(backend='vggt-omega', smart_lens=True, auto_focal=True, initial_focal_length=500).validate()
        self.assertEqual(vggt.backend, 'vggt-omega')
        self.assertFalse(vggt.smart_lens)
        self.assertFalse(vggt.auto_focal)
        self.assertEqual(vggt.initial_focal_length, 0)
        with patch('vggt_runtime.locate_assets', return_value=(Path('source'), Path('model.pt'))):
            for gpu, device in ((True, 'cuda:2'), (False, 'cpu')):
                args = worker_args(Options(backend='vggt-omega', gpu_index=2, use_gpu=gpu), 'images', 'out')
                self.assertEqual(args[args.index('--device') + 1], device)

    def test_vggt_progress_has_no_sift_or_ba_and_has_live_frame_counts(self):
        options = Options(backend='vggt-omega', input_type='image_sequence')
        plan = dict(stage_plan(options, blender=True))
        self.assertNotIn('features', plan)
        self.assertNotIn('ba', plan)
        self.assertIn('blender', plan)
        events = []
        progress = TaskProgress(options, lambda kind, **data: events.append(data))
        progress.command('04-vggt-omega')
        progress.observe('04-vggt-omega', '[SEEDOM_VGGT] ' + json.dumps(dict(
            phase='omega_infer', text='已处理 12 / 16 帧', current=12, total=16)))
        self.assertEqual(progress.key, 'omega_infer')
        self.assertEqual(events[-1]['fraction'], .75)
        progress.observe('04-vggt-omega', '[SEEDOM_VGGT] {broken')
        self.assertEqual(progress.key, 'omega_infer')

    def test_history_estimates_survive_json_roundtrip_without_matching_pairs(self):
        options = Options(backend='vggt-omega')
        settings = settings_signature(options)
        self.assertEqual(json.loads(json.dumps(settings)), settings)
        workload = dict(frames=16, width=640, height=360, codec='image-sequence',
                        stages={'omega': True}, existing_scene=False, pairs=0)
        sample = dict(schema=2, backend='vggt-omega-cuda', hardware_key='test-gpu', settings=settings,
                      workload=workload, frames=16, seconds=12, gpu_solver_active=True)
        saved = json.loads(json.dumps(sample))
        predicted = prediction_range(workload, options, {'fingerprint': 'test-gpu'}, samples=[saved])
        self.assertEqual(predicted['samples'], 1)
        self.assertLess(predicted['lower'], 12)
        self.assertGreater(predicted['upper'], 12)
        saved['backend'] = 'glomap-cuda'
        self.assertIsNone(prediction_range(workload, options, {'fingerprint': 'test-gpu'}, samples=[saved]))

    def test_sequence_dispatch_skips_native_solver_and_persists_result(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
            root = Path(tmp)
            images = root / 'images'; images.mkdir()
            for n in range(3):
                Image.new('RGB', (100, 100)).save(images / f'frame_{n}.png')
            options = Options(backend='vggt-omega', input_type='image_sequence', use_gpu=False,
                              output_root=str(root / 'output'), python=sys.executable)
            commands = []
            class FakeWorker(Pipeline):
                def command(self, args, label, **kwargs):
                    commands.append(label)
                    self.assert_no_native = label == '04-vggt-omega'
                    if not self.assert_no_native:
                        raise AssertionError(label)
                    directory = Path(args[-1])
                    directory.mkdir(parents=True)
                    for name in ('cameras.bin', 'images.bin', 'points3D.bin', 'cameras.txt', 'images.txt', 'points3D.txt'):
                        (directory / name).write_text('fixture\n')
                    (directory / 'vggt-report.json').write_text(json.dumps(dict(metrics=dict(registered_images=3, points=30))))
            def fake_args(options, images, output):
                return ['worker', str(output)]
            with patch('vggt_runtime.locate_assets', return_value=(root, root)), \
                 patch('vggt_runtime.worker_args', side_effect=fake_args), \
                 patch('tracker_core.find_binary', side_effect=AssertionError('native binary used')), \
                 patch('tracker_core.get_hardware', return_value={}), \
                 patch.object(Pipeline, '_solve_native', side_effect=AssertionError('native pipeline used')):
                result = FakeWorker(options).run(images)
            self.assertEqual(commands, ['04-vggt-omega'])
            self.assertEqual(result['backend'], 'vggt-omega-cpu')
            self.assertEqual(result['focal_mode'], 'vggt_predicted_per_frame')
            self.assertTrue(model_ready(validate_result(result)))
            self.assertFalse((Path(result['project_dir']) / '_cache/database.db').exists())
            self.assertNotEqual(source_signature(images, options), source_signature(images, Options(backend='glomap')))
            queue = root / 'queue.json'
            queue.write_text(json.dumps([dict(id='a', path=str(images), name='VGGT测试', state='完成',
                                             options=asdict(options), result=result)]), encoding='utf-8')
            jobs, warnings = restore_jobs(queue)
            self.assertEqual(warnings, [])
            self.assertEqual(jobs['a']['options']['backend'], 'vggt-omega')

    def test_explicit_missing_assets_do_not_silently_use_other_weights(self):
        with patch.dict(os.environ, {'SEEDOM_VGGT_SOURCE': 'not-present-source'}):
            with self.assertRaisesRegex(FileNotFoundError, 'source'):
                locate_assets()

    def test_native_solver_routes_still_extract_match_solve_and_export(self):
        for backend in ('glomap', 'colmap'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
                root = Path(tmp)
                images = root / 'images'; images.mkdir()
                for n in range(3):
                    Image.new('RGB', (100, 100)).save(images / f'frame_{n}.png')
                options = Options(backend=backend, use_gpu=False, input_type='image_sequence',
                                  output_root=str(root / 'out'), python=sys.executable)
                commands = {}
                class NativePipeline(Pipeline):
                    def command(self, args, label, **kwargs):
                        args = list(map(str, args)); commands[label] = args
                        if label == '02-features':
                            with sqlite3.connect(args[args.index('--database_path') + 1]) as db:
                                db.executescript('CREATE TABLE images(id INTEGER); INSERT INTO images VALUES(1),(2),(3);'
                                                 'CREATE TABLE two_view_geometries(rows INTEGER);'
                                                 'INSERT INTO two_view_geometries VALUES(10),(10);')
                            db.close()
                        if label in ('04-glomap', '04-colmap'):
                            model = Path(args[args.index('--output_path') + 1])
                            if backend == 'colmap':
                                model /= '0'
                            model.mkdir(parents=True)
                            for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
                                (model / name).write_bytes(b'fixture-model')
                        if label == '05-export-model':
                            model = Path(args[args.index('--output_path') + 1])
                            for name in ('cameras.txt', 'images.txt', 'points3D.txt'):
                                (model / name).write_text('fixture\n')
                        if label == '05-model-analysis':
                            (self.log_dir / (label + '.log')).write_text('Registered images: 3\nPoints: 30\nObservations: 90\n')
                with patch('tracker_core.find_binary', return_value=Path('colmap.exe')), \
                     patch('tracker_core.find_legacy_colmap', return_value=Path('colmap-legacy.exe')), \
                     patch('tracker_core.get_hardware', return_value={}), \
                     patch('vggt_runtime.locate_assets', side_effect=AssertionError('VGGT assets requested')):
                    result = NativePipeline(options).run(images)
                self.assertEqual(result['backend'], backend + ('-global-cpu' if backend == 'colmap' else '-cpu'))
                self.assertEqual(result['database']['images'], 3)
                self.assertTrue(model_ready(result))
                self.assertIn('03-matching', commands)
                self.assertIn('05-export-ply', commands)
                self.assertIn('04-' + backend, commands)
                self.assertNotIn('04-vggt-omega', commands)
                gpu_flag = '--SiftExtraction.use_gpu' if backend == 'glomap' else '--FeatureExtraction.use_gpu'
                self.assertEqual(commands['02-features'][commands['02-features'].index(gpu_flag) + 1], '0')
                # sqlite's transaction context does not close its connection;
                # collect the existing native readers before Windows cleanup.
                gc.collect()


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from qt_app import configure_application
        cls.app = QApplication.instance() or QApplication([])
        configure_application(cls.app)

    def test_task_accepts_vggt_with_recovered_saved_python_path(self):
        from qt_dialogs import TaskDialog
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
            video = Path(tmp) / 'clip.mov'
            video.write_bytes(b'already probed video fixture')
            dialog = TaskDialog(None, Options(backend='vggt-omega',
                image_scale=.5, python='archive/legacy-1.x/runtime/python/python.exe'))
            try:
                dialog.source.blockSignals(True)
                dialog.source.setPlainText(str(video))
                dialog.source.blockSignals(False)
                dialog.metadata = {str(video): dict(fps=30, width=640, height=360)}
                dialog.debounce.stop()
                dialog.blend.setChecked(False)
                with patch.object(app_paths, 'PYTHON', Path(sys.executable)), \
                     patch('vggt_runtime.locate_assets', return_value=(ROOT, ROOT)):
                    dialog.accept_task()
                self.assertIsNotNone(dialog.result_data, dialog.error.text())
                self.assertEqual(dialog.result_data['options'].python, str(Path(sys.executable).resolve()))
                self.assertEqual(dialog.result_data['options'].backend, 'vggt-omega')
                self.assertEqual(dialog.result_data['options'].image_scale, 1.0)
            finally:
                dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_solver_picker_has_two_choices_and_shows_only_relevant_controls(self):
        from qt_dialogs import TaskDialog
        dialog = TaskDialog(None, Options(backend='vggt-omega'),
                            blender_records=[dict(path='test.exe', version='Blender test')])
        dialog.show(); self.app.processEvents()
        try:
            self.assertEqual([dialog.backend.itemData(i) for i in range(dialog.backend.count())],
                             ['vggt-omega', 'glomap'])
            self.assertTrue(dialog.backend.isEnabled())
            self.assertFalse(dialog.smart_lens.isVisible())
            self.assertFalse(dialog.initial_focal.isVisible())
            self.assertFalse(dialog.scale.isVisible())
            self.assertFalse(dialog.lens_form.labelForField(dialog.scale).isVisible())
            self.assertEqual(dialog.quality_label.text(), '推理档位')
            self.assertFalse(dialog.sequence_size.isVisible())
            self.assertTrue(dialog.vggt_note.isVisible())
            dialog.quality.setCurrentIndex(1)
            self.assertIn('384', dialog.quality.toolTip())
            dialog.backend.setCurrentIndex(dialog.backend.findData('glomap'))
            self.app.processEvents()
            self.assertTrue(dialog.smart_lens.isVisible())
            self.assertTrue(dialog.scale.isVisible())
            self.assertEqual(dialog.quality_label.text(), '计算精度')
            self.assertFalse(dialog.sequence_size.isVisible())
            dialog.blend.setChecked(True)
            self.assertTrue(dialog.sequence_size.isVisible())
            dialog.scale.setCurrentIndex(2)
            self.assertEqual(dialog.scale.currentData(), .5)
            self.assertFalse(dialog.vggt_note.isVisible())
            self.assertEqual(dialog.backend.currentData(), 'glomap')
            dialog.smart_lens.setChecked(True)
            self.assertTrue(dialog.backend.isEnabled())
            dialog.backend.setCurrentIndex(dialog.backend.findData('vggt-omega'))
            self.assertFalse(dialog.smart_lens.isChecked())
            self.assertEqual(dialog.backend.currentData(), 'vggt-omega')
            self.assertFalse(dialog.scale.isVisible())
            self.assertTrue(dialog.sequence_size.isVisible())
            dialog.backend.setCurrentIndex(dialog.backend.findData('glomap'))
            self.assertEqual(dialog.scale.currentData(), .5)
        finally:
            dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_old_colmap_task_editor_offers_current_choices_without_mutating_record(self):
        from qt_dialogs import TaskDialog
        options = Options(backend='colmap')
        job = dict(path='old.mov', name='old COLMAP task', options=asdict(options))
        dialog = TaskDialog(None, options, job=job)
        try:
            self.assertEqual(dialog.backend.count(), 2)
            self.assertEqual(dialog.backend.currentData(), 'vggt-omega')
            self.assertEqual(job['options']['backend'], 'colmap')
            self.assertEqual(options.backend, 'colmap')
        finally:
            dialog.reject(); dialog.deleteLater(); self.app.processEvents()

    def test_main_window_migrates_default_and_persists_explicit_glomap_preference(self):
        from qt_app import MainWindow
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
            settings = Path(tmp) / 'settings.json'
            settings.write_text(json.dumps(dict(backend='colmap', smart_lens=True)), encoding='utf-8')
            window = MainWindow(config_dir=tmp, background=False)
            try:
                self.assertEqual(window.defaults.backend, 'vggt-omega')
                self.assertFalse(window.defaults.smart_lens)
                window.save_preferences()
                self.assertEqual(preferred_solver(json.loads(settings.read_text(encoding='utf-8'))), 'vggt-omega')
                window.defaults.backend = 'glomap'
                window.save_preferences()
            finally:
                window.poller.stop(); window.deleteLater(); self.app.processEvents()
            restored = MainWindow(config_dir=tmp, background=False)
            try:
                self.assertEqual(restored.defaults.backend, 'glomap')
                self.assertEqual(restored.defaults.python, str(app_paths.PYTHON.resolve()))
            finally:
                restored.poller.stop(); restored.deleteLater(); self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
