"""Retry-directory policy, cache replacement, and persisted settings."""
from pathlib import Path
from dataclasses import replace
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from tracker_core import Options, Pipeline
from workload import source_signature
from qt_app import MainWindow, configure_application
from task_queue import TaskQueue
from PySide6.QtWidgets import QApplication


class RetryDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'clip.mp4'
        self.video.write_bytes(b'original video')
        self.options = Options(output_root=str(self.root / 'output'))

    def pipeline(self, options=None, enabled=False):
        pipeline = Pipeline(options or self.options)
        pipeline.project_name = '测试任务'
        pipeline.reuse_failed_projects = enabled
        return pipeline

    def unfinished(self):
        pipeline = self.pipeline()
        signature = source_signature(self.video, self.options)
        project = pipeline.new_project(self.video, signature)
        (project / '_cache/source.json').write_text(json.dumps(signature), encoding='utf-8')
        (project / '_cache/.frames-complete').write_text('10')
        (project / '_cache/images').mkdir()
        (project / '_cache/images/frame_000001.jpg').write_bytes(b'image')
        (project / '_cache/database.db').write_bytes(b'database')
        return project, signature

    def test_opt_in_finds_failed_project_after_readding_without_changing_cache(self):
        project, signature = self.unfinished()
        timestamp = (project / '_cache/database.db').stat().st_mtime_ns
        resumed = self.pipeline(enabled=True)
        self.assertEqual(resumed.new_project(self.video, signature), project)
        self.assertEqual((project / '_cache/database.db').stat().st_mtime_ns, timestamp)
        self.assertFalse((project.parent / '测试任务 (2)').exists())
        # Turning the global setting off preserves the existing collision policy.
        independent = self.pipeline()
        self.assertEqual(independent.new_project(self.video, signature).name, '测试任务 (2)')

    def test_changed_parameters_rebuild_in_same_folder_and_preserve_old_cache_and_logs(self):
        project, _ = self.unfinished()
        (project / 'log/task.log').write_text('old failure', encoding='utf-8')
        (project / 'video').mkdir()
        (project / 'video/frame_000999.jpg').write_bytes(b'old sequence')
        changed = replace(self.options, image_scale=.5, overlap=8)
        resumed = self.pipeline(changed, enabled=True)
        signature = source_signature(self.video, changed)
        self.assertEqual(resumed.new_project(self.video, signature), project)
        self.assertFalse((project / '_cache/.frames-complete').exists())
        self.assertFalse((project / '_cache/database.db').exists())
        self.assertFalse((project / '_cache/source.json').exists())
        self.assertFalse((project / 'video').exists())
        history = list((project / '_cache/retry_history').iterdir())
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0] / 'database.db').read_bytes(), b'database')
        self.assertTrue((history[0] / 'images/frame_000001.jpg').is_file())
        self.assertTrue((history[0] / 'video/frame_000999.jpg').is_file())
        self.assertIn('old failure', (project / 'log/task.log').read_text(encoding='utf-8'))
        self.assertEqual(self.video.read_bytes(), b'original video')

    def test_completed_project_and_other_source_are_never_selected(self):
        project, signature = self.unfinished()
        (project / '_cache/result.json').write_text('{"status":"complete"}')
        self.assertNotEqual(self.pipeline(enabled=True).new_project(self.video, signature), project)
        (project / '_cache/result.json').unlink()
        (project / 'point could').mkdir()
        self.assertNotEqual(self.pipeline(enabled=True).new_project(self.video, signature), project)
        (project / 'point could').rmdir()
        other = self.root / 'other.mp4'
        other.write_bytes(b'original video')
        self.assertNotEqual(self.pipeline(enabled=True).new_project(other, source_signature(other, self.options)), project)

    def test_ui_setting_persists_and_applies_to_existing_queue(self):
        app = QApplication.instance() or QApplication([])
        configure_application(app)
        window = MainWindow(config_dir=self.root / 'config', background=False)
        self.assertFalse(window.reuse_failed_projects.isChecked())
        iid = window.tasks.add(self.video, self.options)
        window.reuse_failed_projects.setChecked(True)
        with patch.object(Pipeline, 'preflight'), patch.object(window, 'refresh_projects'):
            window.save_settings_clicked()
        self.assertTrue(window.tasks.reuse_failed_projects)
        pipeline = window.tasks._create_pipeline(iid, window.tasks.jobs[iid])
        self.assertTrue(pipeline.reuse_failed_projects)
        window.tasks.pipeline = None
        window.close()
        restored = MainWindow(config_dir=self.root / 'config', background=False)
        self.assertTrue(restored.reuse_failed_projects.isChecked())
        self.assertTrue(restored.tasks.reuse_failed_projects)
        restored.close()


if __name__ == '__main__':
    unittest.main()
