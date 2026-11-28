"""Background deliverables survive cache cleanup, re-export and format changes."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from blender_sequence import prepare_sequence, clean_solver_cache, SEQUENCE_FORMATS, SEQUENCE_SIZES
from blender_export import export_blender
from tracker_core import Options, Pipeline, Cancelled
from workload import source_signature
from json_store import atomic_write_json
from project_layout import result_report


class PlateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "素材's frames"
        self.source.mkdir()
        for i in (1, 2, 10):
            Image.new('RGB', (80, 40), (i * 10, 80, 140)).save(self.source / f'镜头 {i}.png')
        self.project = self.root / 'project'
        self.cache = self.project / '_cache'
        self.cache.mkdir(parents=True)
        model = self.project / 'point could'
        model.mkdir()
        for name in ('cameras.txt', 'images.txt', 'points3D.txt'):
            (model / name).write_text('1 1 1\n')
        self.pipeline = Pipeline(Options(input_type='image_sequence', use_gpu=False))
        self.pipeline.log_dir = self.project / 'log'
        self.pipeline.log_dir.mkdir()
        self.result = dict(status='complete', layout_version=2, cache_policy='discard_after_exports_v1', model=str(model),
            project_dir=str(self.project), source=str(self.source), images_dir=str(self.source),
            input_type='image_sequence', log_dir=str(self.pipeline.log_dir), metrics=dict(points=1),
            source_settings=source_signature(self.source, self.pipeline.options))
        atomic_write_json(self.cache / 'source.json', self.result['source_settings'])
        atomic_write_json(result_report(self.result), self.result)
        (self.cache / 'images').mkdir()
        (self.cache / 'images/frame.jpg').write_bytes(b'cached')
        (self.cache / 'database.db').write_bytes(b'cache')
        (self.cache / 'solve_test').mkdir()
        (self.cache / 'solve_test/temporary.bin').write_bytes(b'cache')

    def test_every_format_encodes_all_frames_in_natural_order_at_requested_size(self):
        for _, fmt in SEQUENCE_FORMATS:
            with self.subTest(format=fmt):
                directory, manifest = prepare_sequence(self.pipeline, self.result,
                    dict(sequence_format=fmt, sequence_resolution='half'))
                mapping = json.loads(manifest.read_text(encoding='utf-8'))['images']
                self.assertEqual(list(mapping), ['镜头 1.png', '镜头 2.png', '镜头 10.png'])
                self.assertEqual(len(list(directory.glob('frame_*.' + fmt))), 3)
                with Image.open(directory / mapping['镜头 2.png']) as image:
                    self.assertEqual(image.size, (40, 20))

    def test_completed_variant_reuses_without_original_and_different_variant_is_independent(self):
        first, manifest = prepare_sequence(self.pipeline, self.result, dict(sequence_format='png'))
        checksum = (first / 'frame_000001.png').read_bytes()
        second, _ = prepare_sequence(self.pipeline, self.result, dict(sequence_format='jpg', sequence_resolution='quarter'))
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'frame_000001.png').read_bytes(), checksum)
        shutil.rmtree(self.source)
        with patch.object(self.pipeline, 'command', side_effect=AssertionError('should reuse')):
            self.assertEqual(prepare_sequence(self.pipeline, self.result, dict(sequence_format='png')), (first, manifest))

    def test_height_limit_preserves_aspect_without_upscaling(self):
        directory, _ = prepare_sequence(self.pipeline, self.result, dict(sequence_resolution='720'))
        with Image.open(directory / 'frame_000001.jpg') as image:
            self.assertEqual(image.size, (80, 40))

    def test_video_resampling_uses_solve_fps_instead_of_blender_playback_fps(self):
        from tracker_core import find_binary
        video = self.root / 'clip.mkv'
        self.pipeline.command([find_binary('03 FFMPEG', 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=blue:s=80x40:r=10:d=1', '-c:v', 'ffv1', video], 'fixture-video')
        self.result.update(source=str(video), input_type='video',
            source_settings=source_signature(video, Options(fps=5)))
        directory, ready = prepare_sequence(self.pipeline, self.result, dict(sequence_resolution='half', fps=60))
        mapping = json.loads(ready.read_text(encoding='utf-8'))['images']
        self.assertEqual(len(mapping), 5)
        with Image.open(directory / 'frame_000001.jpg') as image:
            self.assertEqual(image.size, (40, 20))

    def test_cleanup_preserves_source_model_logs_plates_and_metadata(self):
        directory, _ = prepare_sequence(self.pipeline, self.result, {})
        self.result.update(sequence=str(directory), images_dir=str(directory), database=dict(images=3, pairs=2))
        clean_solver_cache(self.pipeline, self.result)
        self.assertEqual({p.name for p in self.cache.iterdir()}, {'source.json', 'result.json'})
        self.assertTrue(self.source.is_dir())
        self.assertTrue((directory / 'frame_000001.jpg').is_file())
        self.assertTrue((Path(self.result['model']) / 'points3D.txt').is_file())
        self.assertEqual(self.result['database']['images'], 3)
        self.assertTrue(json.loads(result_report(self.result).read_text())['cache_cleaned'])

    def test_legacy_project_cache_is_kept_for_existing_blender_files(self):
        self.result.pop('cache_policy')
        clean_solver_cache(self.pipeline, self.result)
        self.assertTrue((self.cache / 'images/frame.jpg').is_file())
        self.assertTrue((self.cache / 'database.db').is_file())

    def test_missing_original_after_cleanup_gives_actionable_error(self):
        self.result['images_dir'] = str(self.cache / 'images')
        clean_solver_cache(self.pipeline, self.result)
        shutil.rmtree(self.source)
        with self.assertRaisesRegex(FileNotFoundError, '原素材'):
            prepare_sequence(self.pipeline, self.result, {})

    def test_changed_export_size_requires_original_instead_of_resizing_an_old_half_size_plate(self):
        directory, _ = prepare_sequence(self.pipeline, self.result, dict(sequence_resolution='half'))
        self.result['images_dir'] = str(directory)
        shutil.rmtree(self.source)
        with self.assertRaisesRegex(FileNotFoundError, '原素材'):
            prepare_sequence(self.pipeline, self.result, dict(sequence_resolution='original'))

    def test_library_refresh_preserves_plate_variant_after_cache_cleanup(self):
        from project_library import ProjectLibrary
        directory, _ = prepare_sequence(self.pipeline, self.result, {})
        self.result.update(sequence=str(directory), images_dir=str(directory))
        clean_solver_cache(self.pipeline, self.result)
        with patch('project_library.OUTPUTS', self.root), patch('project_library.LEGACY_OUTPUTS', self.root):
            records, warnings = ProjectLibrary(self.root / 'library.json').scan(self.root)
        self.assertFalse(warnings)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['result']['images_dir'], str(directory))
        self.assertEqual(records[0]['result']['sequence'], str(directory))

    def test_cancelled_conversion_publishes_no_partial_plates_and_retains_cache(self):
        with patch.object(self.pipeline, 'command', side_effect=Cancelled('stop')):
            with self.assertRaises(Cancelled):
                prepare_sequence(self.pipeline, self.result, {})
        self.assertFalse(list((self.project / 'video').iterdir()))
        self.assertTrue((self.cache / 'database.db').is_file())

    def test_no_background_no_sequence_does_not_need_source(self):
        shutil.rmtree(self.source)
        output = self.project / 'scene.blend'
        def fake_blender(args, label):
            self.assertNotIn('--images', args)
            output.write_bytes(b'blend fixture')
        with patch.object(self.pipeline, 'command', side_effect=fake_blender):
            export_blender(self.pipeline, self.result, output, dict(path='blender.exe', fps=30,
                as_mesh=True, suppress_warnings=True, background_images=False, export_sequence=False, point_radius=.005))
        self.assertTrue(output.is_file())
        self.assertFalse((self.project / 'video').exists())
        self.assertFalse((self.cache / 'database.db').exists())

    def test_failed_blender_keeps_computation_cache(self):
        with patch.object(self.pipeline, 'command', side_effect=RuntimeError('blender failed')):
            with self.assertRaisesRegex(RuntimeError, 'blender failed'):
                export_blender(self.pipeline, self.result, self.project / 'bad.blend', dict(path='blender.exe',
                    fps=30, as_mesh=True, suppress_warnings=True, background_images=False,
                    export_sequence=False, point_radius=.005))
        self.assertTrue((self.cache / 'database.db').is_file())

    def test_queue_cleanup_waits_for_blender_and_preserves_cache_on_export_failure(self):
        from task_queue import TaskQueue
        result = self.result
        outer = self
        class Solver(Pipeline):
            def run(self, path, existing_scene=False):
                outer.assertTrue(self.defer_cache_cleanup)
                self.log_dir = outer.pipeline.log_dir
                return result
        finished = threading.Event()
        queue = TaskQueue(lambda event: finished.set() if event['type'] == 'finished' else None,
                          Solver, lambda result: None)
        iid = queue.add(self.source, Options(input_type='image_sequence'))
        queue.jobs[iid]['blender'] = dict(version='4.2')
        def failed_export(*args):
            self.assertTrue((self.cache / 'database.db').is_file())
            raise RuntimeError('export failed')
        with patch('blender_export.export_blender', side_effect=failed_export):
            queue.start()
            self.assertTrue(finished.wait(5))
        self.assertIn('export failed', queue.jobs[iid]['export_error'])
        self.assertTrue((self.cache / 'database.db').exists())
        finished.clear()
        queue.jobs[iid]['state'] = '等待'
        with patch('blender_export.export_blender', return_value='ok.blend'):
            queue.start()
            self.assertTrue(finished.wait(5))
        self.assertFalse((self.cache / 'database.db').exists())


class ExportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from qt_app import configure_application
        cls.app = QApplication.instance() or QApplication([])
        configure_application(cls.app)

    def test_task_and_manual_export_share_formats_sizes_and_background_toggle(self):
        from qt_dialogs import TaskDialog, BlenderDialog
        record = dict(path='D:/Blender/blender.exe', version='Blender 4.2')
        task = TaskDialog(None, Options(), blender_records=[record])
        manual = BlenderDialog(None, dict(_blender_record=record))
        try:
            task.show(); manual.show(); self.app.processEvents()
            self.assertFalse(task.sequence.isVisible())
            task.blend.setChecked(True)
            self.assertTrue(task.sequence.isVisible())
            for dialog in (task, manual):
                fields = dialog.sequence_fields
                self.assertEqual(fields.format.count(), len(SEQUENCE_FORMATS))
                self.assertEqual(fields.resolution.count(), len(SEQUENCE_SIZES))
                fields.enabled.setChecked(False)
                self.assertFalse(dialog.background.isChecked())
                self.assertFalse(fields.details.isVisible())
                fields.enabled.setChecked(True)
                dialog.background.setChecked(True)
                fields.format.setCurrentIndex(fields.format.findData('png'))
                fields.resolution.setCurrentIndex(fields.resolution.findData('quarter'))
            manual.accept_settings()
            self.assertEqual(manual.result_data['sequence_format'], 'png')
            self.assertEqual(manual.result_data['sequence_resolution'], 'quarter')
            with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as tmp:
                video = Path(tmp).resolve() / 'clip.mov'
                video.write_bytes(b'already probed fixture')
                task.source.blockSignals(True)
                task.source.setPlainText(str(video))
                task.source.blockSignals(False)
                task.metadata = {str(video): dict(fps=30, width=640, height=360)}
                task.debounce.stop()
                with patch.object(Pipeline, 'preflight'):
                    task.accept_task()
                self.assertIsNotNone(task.result_data, task.error.text())
                self.assertFalse(task.result_data['options'].export_sequence)
                self.assertTrue(task.result_data['blender']['export_sequence'])
                self.assertEqual(task.result_data['blender']['sequence_format'], 'png')
                self.assertEqual(task.result_data['blender']['sequence_resolution'], 'quarter')
        finally:
            task.reject(); manual.reject()
            task.deleteLater(); manual.deleteLater(); self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
