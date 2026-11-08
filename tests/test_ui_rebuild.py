"""Focused regression tests for real FFmpeg output and Qt/queue integration."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from paths import runtime_environment, BINARY_DIRS
os.environ.update(runtime_environment())
from tracker_core import Options, Pipeline, Cancelled, video_filters
from task_queue import TaskQueue
from task_storage import restore_jobs
from app_info import APP_VERSION
from workload import source_signature, video_scene, video_info
from hardware import settings_signature
from project_library import ProjectLibrary
from project_layout import create_project, safe_name, blend_filename
from qt_app import DetailValueLabel, MainWindow, configure_application
from qt_dialogs import TaskDialog, BlenderDialog
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QGroupBox, QPushButton, QScrollArea, QWidget, QMessageBox
from PySide6.QtGui import QColor
from PySide6.QtCore import QTimer, Qt, QPoint
from PySide6.QtTest import QTest


def wait_until(predicate, timeout=8, app=None):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline: raise AssertionError('operation timed out')
        if app: app.processEvents()
        time.sleep(.01)


class QueueTests(unittest.TestCase):
    def test_glomap_fallback_is_live_deduplicated_and_queue_continues(self):
        events = []; live = []
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as directory:
            class SolverPipeline(Pipeline):
                def run(self, path, existing_scene=False):
                    self.log_dir = Path(directory)
                    fallback = Path(path).stem == 'fallback'
                    lines = '[GPU] Global positioning: CUDA_SPARSE\n[GPU] Bundle adjustment: CUDA_SPARSE'
                    if fallback: lines += '\nFalling back to CPU\nFALLING BACK to CPU'
                    script = f'import time; print({lines!r}, flush=True); time.sleep(.3)'
                    self.command([sys.executable, '-u', '-c', script], '04-glomap')
                    return {'gpu_solver_active': self.verify_glomap_gpu(), 'status': 'complete',
                            'solver_warnings': list(self.warnings), 'project_dir': directory,
                            'model': str(Path(directory) / 'point could')}
            def receive(event):
                events.append(event)
                if event['type'] == 'solver_warning':
                    live.append(tasks.pipeline.process.poll() is None)
            tasks = TaskQueue(receive, SolverPipeline, lambda result: None)
            first = tasks.add('fallback.mp4', Options(backend='glomap'))
            second = tasks.add('gpu.mp4', Options(backend='glomap'))
            tasks.jobs[first]['blender'] = {'version': 'Blender 5.2', 'path': 'unused.exe'}
            def export(pipeline, result, output, settings):
                self.assertFalse(result['gpu_solver_active'])
                self.assertEqual(result['solver_warnings'][0]['code'], 'cpu_fallback')
                output.write_bytes(b'BLENDER-test')
                return str(output)
            with patch('blender_export.export_blender', side_effect=export) as exported:
                tasks.start(); wait_until(lambda: not tasks.running)
                exported.assert_called_once()
            self.assertEqual(live, [True])
            self.assertEqual(tasks.jobs[first]['state'], '完成')
            self.assertFalse(tasks.jobs[first]['result']['gpu_solver_active'])
            self.assertTrue((Path(directory) / 'fallback_5.2.blend').is_file())
            self.assertEqual(tasks.jobs[first]['solver_warnings'][0]['code'], 'cpu_fallback')
            self.assertEqual(tasks.jobs[second]['state'], '完成')
            self.assertTrue(tasks.jobs[second]['result']['gpu_solver_active'])
            self.assertFalse(tasks.jobs[second]['solver_warnings'])
            self.assertEqual(len([e for e in events if e['type'] == 'solver_warning']), 1)

    def test_glomap_partial_gpu_and_explicit_cpu(self):
        events = []
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as directory:
            pipeline = Pipeline(Options(backend='glomap'), events.append); pipeline.log_dir = Path(directory)
            log = pipeline.log_dir / '04-glomap.log'
            log.write_text('[GPU] Global positioning: CUDA_SPARSE\n', encoding='utf-8')
            self.assertFalse(pipeline.verify_glomap_gpu())
            self.assertFalse(pipeline.verify_glomap_gpu())
            self.assertEqual(len(pipeline.warnings), 1)
            self.assertEqual(pipeline.warnings[0]['code'], 'gpu_incomplete')
            self.assertIn('50', pipeline.warnings[0]['text'])
            self.assertIn('求解提醒', (pipeline.log_dir / 'task.log').read_text(encoding='utf-8'))
            for options, label in ((Options(backend='glomap', glomap_gpu=False), '04-glomap'),
                                   (Options(), '04-fastmap'), (Options(backend='glomap'), '02-features')):
                other = Pipeline(options, events.append); other.log_dir = Path(directory)
                other.command([sys.executable, '-c', 'print("Falling back to CPU")'], label)
                self.assertFalse(other.warnings)

    def test_priority_moves_groups_and_starts_subset_in_queue_order(self):
        calls = []
        class FakePipeline:
            def __init__(self, options, callback): pass
            def run(self, path, existing_scene=False):
                calls.append(Path(path).stem)
                return {'status': 'complete'}
        tasks = TaskQueue(lambda event: None, FakePipeline, lambda result: None)
        a, b, c, d = [tasks.add(f'{name}.mp4', Options()) for name in ('a', 'b', 'c', 'd')]
        self.assertFalse(tasks.move([a], -1))
        self.assertFalse(tasks.move([d], 1))
        self.assertTrue(tasks.move([b, c], 1))
        self.assertEqual(list(tasks.snapshot()), [a, d, b, c])
        self.assertTrue(tasks.move([b, c], -1))
        self.assertEqual(list(tasks.snapshot()), [a, b, c, d])
        self.assertTrue(tasks.move([c], -1))
        self.assertTrue(tasks.start([d, c, a]))
        wait_until(lambda: not tasks.running)
        self.assertEqual(calls, ['a', 'c', 'd'])
        self.assertEqual(tasks.jobs[b]['state'], '等待')
        self.assertFalse(tasks.can_move([a], 1))

    def test_duplicate_task_names_are_rejected_case_insensitively(self):
        tasks = TaskQueue(lambda event: None, Pipeline, lambda result: None)
        tasks.add('first.mp4', Options(), 'Scene')
        with self.assertRaisesRegex(ValueError, '已存在'):
            tasks.add('second.mp4', Options(), 'scene')
        with self.assertRaisesRegex(ValueError, '重复'):
            tasks.validate_names(['Batch · one', 'batch · ONE'])
        with self.assertRaisesRegex(ValueError, '已存在'):
            tasks.validate_names(['SCENE'])
        tasks.validate_names(['Renamed'], ignore_id=next(iter(tasks.jobs)))
        self.assertEqual(len(tasks.jobs), 1)

    def test_live_priority_pause_new_tasks_and_failure_continue_serially(self):
        entered = threading.Event(); release = threading.Event(); calls = []
        class FakePipeline:
            def __init__(self, options, callback): pass
            def run(self, path, existing_scene=False):
                name = Path(path).stem; calls.append(name)
                if name == 'a': entered.set(); release.wait(5)
                if name == 'b': raise RuntimeError('expected failure')
                return {'status': 'complete'}
        events = []
        tasks = TaskQueue(events.append, FakePipeline, lambda result: None)
        a, b, c, d = [tasks.add(f'{name}.mp4', Options()) for name in ('a', 'b', 'c', 'd')]
        tasks.start(); self.assertTrue(entered.wait(3))
        self.assertFalse(tasks.move([a], 1))
        self.assertTrue(tasks.move([c], -1))
        e = tasks.add('e.mp4', Options())
        self.assertEqual(tasks.jobs[e]['state'], '排队')
        self.assertTrue(tasks.move([e], -1)); self.assertTrue(tasks.move([e], -1))
        self.assertEqual(calls, ['a'])
        tasks.pause(True); release.set()
        wait_until(lambda: tasks.jobs[a]['state'] == '完成')
        self.assertEqual(calls, ['a'])
        tasks.pause(False); wait_until(lambda: not tasks.running)
        self.assertEqual(calls, ['a', 'c', 'e', 'b', 'd'])
        self.assertEqual(tasks.jobs[b]['state'], '失败')
        self.assertEqual(tasks.jobs[d]['state'], '完成')
        self.assertIn('4 项成功 · 1 项失败', events[-1]['text'])

    def test_actual_child_process_is_stopped(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as directory:
            class ProcessPipeline(Pipeline):
                def run(self, path, existing_scene=False):
                    self.log_dir = Path(directory)
                    self.command([sys.executable, '-u', '-c', 'import time; print("ready", flush=True); time.sleep(60)'], 'cancel-check')
                    return {}
            tasks = TaskQueue(lambda event: None, ProcessPipeline, lambda result: None)
            first = tasks.add('one.mp4', Options()); second = tasks.add('two.mp4', Options())
            tasks.start()
            wait_until(lambda: tasks.pipeline is not None and tasks.pipeline.process is not None)
            process = tasks.pipeline.process
            tasks.cancel(); wait_until(lambda: not tasks.running)
            self.assertIsNotNone(process.poll())
            self.assertEqual(tasks.jobs[first]['state'], '已停止')
            self.assertEqual(tasks.jobs[second]['state'], '等待')

    def test_auto_blender_failure_keeps_reconstruction_result(self):
        result = {'model': str(ROOT / 'tests/artifacts/model/sparse/0'), 'status': 'complete'}
        class FakePipeline:
            def __init__(self, options, callback): pass
            def run(self, path, existing_scene=False): return result
        tasks = TaskQueue(lambda event: None, FakePipeline, lambda result: None)
        iid = tasks.add('video.mp4', Options())
        tasks.jobs[iid]['blender'] = {'version': 'Blender 4.2.15', 'path': 'blender.exe'}
        with patch('blender_export.export_blender', side_effect=RuntimeError('export test failure')):
            tasks.start(); wait_until(lambda: not tasks.running)
        self.assertEqual(tasks.jobs[iid]['state'], '完成')
        self.assertEqual(tasks.jobs[iid]['result'], result)
        self.assertIn('export test failure', tasks.jobs[iid]['export_error'])

    def test_edit_wait_and_independent_options(self):
        events = queue.Queue(); first_started = threading.Event(); release = threading.Event(); calls = []
        class FakePipeline:
            def __init__(self, options, callback): self.options, self.callback = options, callback
            def run(self, path, existing_scene=False):
                calls.append((path, self.options.fps))
                if len(calls) == 1: first_started.set(); release.wait(5)
                return {'status': 'complete'}
        tasks = TaskQueue(events.put, FakePipeline, lambda result: None)
        first = tasks.add('one.mp4', Options(fps=10))
        second = tasks.add('two.mp4', Options(fps=20))
        self.assertTrue(tasks.start()); self.assertTrue(first_started.wait(3))
        self.assertIsNone(tasks.begin_edit(first))
        self.assertIsNotNone(tasks.begin_edit(second))
        release.set()
        wait_until(lambda: tasks.jobs[first]['state'] == '完成')
        self.assertEqual(len(calls), 1)
        options = dict(tasks.jobs[second]['options'], fps=12)
        tasks.end_edit({'options': options})
        wait_until(lambda: not tasks.running)
        self.assertEqual([fps for _, fps in calls], [10, 12])

    def test_cancel_waiting_queue_and_recover_pending(self):
        entered = threading.Event(); release = threading.Event()
        class FakePipeline:
            def __init__(self, options, callback): pass
            def run(self, path, existing_scene=False):
                entered.set(); release.wait(3)
                if self.cancelled.is_set(): raise Cancelled('stopped')
                return {}
            def cancel(self): self.cancelled.set(); release.set()
        tasks = TaskQueue(lambda event: None, FakePipeline, lambda result: None)
        first = tasks.add('one.mp4', Options()); second = tasks.add('two.mp4', Options())
        tasks.start(); self.assertTrue(entered.wait(3)); self.assertFalse(tasks.remove(first))
        tasks.pause(True); tasks.cancel(); wait_until(lambda: not tasks.running)
        self.assertEqual(tasks.jobs[first]['state'], '已停止')
        self.assertEqual(tasks.jobs[second]['state'], '等待')


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([]); configure_application(cls.app)
        cls.artifacts = ROOT / 'tests/artifacts'; cls.artifacts.mkdir(exist_ok=True)
        cls.video = cls.artifacts / 'test-24fps.mp4'
        subprocess.run([str(BINARY_DIRS['03 FFMPEG'] / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.video)], check=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.artifacts)
        self.window = MainWindow(config_dir=self.temp.name, background=False); self.window.show(); self.app.processEvents()

    def tearDown(self):
        self.window.poller.stop(); self.window.hide(); self.window.deleteLater(); self.app.processEvents(); self.temp.cleanup()

    def test_solver_notice_survives_finished_and_persists_without_modal(self):
        window = self.window
        directory = Path(self.temp.name)
        model = directory / 'point could'; model.mkdir()
        for name in ('cameras.txt', 'images.txt', 'points3D.txt'):
            (model / name).write_text('1 2 3\n', encoding='utf-8')
        class FallbackPipeline(Pipeline):
            def run(self, path, existing_scene=False):
                self.log_dir = directory
                self.observe_glomap_output('Falling back to CPU')
                (directory / '04-glomap.log').write_text('Falling back to CPU\n', encoding='utf-8')
                return dict(status='complete', model=str(model), project_dir=str(directory), source=path,
                            metrics={'points': 1}, gpu_solver_active=self.verify_glomap_gpu(),
                            solver_warnings=list(self.warnings))
        window.tasks.pipeline_factory = FallbackPipeline
        window.tasks.sample_writer = lambda result: None
        iid = window.tasks.add(self.video, Options(backend='glomap'), '回退提示验证')
        with patch.object(window, 'refresh_projects'), patch.object(window, 'schedule_estimate'):
            window.start_queue()
            wait_until(lambda: not window.tasks.running, app=self.app)
            wait_until(lambda: window.events.empty(), app=self.app)
        self.assertFalse(window.solver_notice.isHidden())
        self.assertIn('回退到 CPU', window.solver_notice_text.text())
        self.assertIn('继续求解和导出', window.solver_notice_text.text())
        self.assertEqual(window.solver_notice_log, str(directory / '04-glomap.log'))
        self.assertIsNone(QApplication.activeModalWidget())
        window.table.selectRow(0); self.app.processEvents()
        self.assertIn('回退到 CPU', window.detail_info.text())
        self.assertEqual(window.selected()['state'], '完成')
        window.pages.setCurrentIndex(1); self.app.processEvents()
        self.assertFalse(window.solver_notice.isHidden())
        window.solver_notice.hide()
        saved = json.loads((directory / 'task-queue.json').read_text(encoding='utf-8'))
        self.assertEqual(saved[0]['solver_warnings'][0]['code'], 'cpu_fallback')
        window.tasks.jobs.clear(); window.restore_tasks(); window.rebuild_rows()
        window.pages.setCurrentIndex(0); window.table.selectRow(0); self.app.processEvents()
        self.assertEqual(window.tasks.jobs[iid]['state'], '完成')
        self.assertFalse(window.tasks.jobs[iid]['result']['gpu_solver_active'])
        self.assertIn('回退到 CPU', window.detail_info.text())
        self.assertIn('拾点VFX（SeedomVFX） ' + APP_VERSION, window.windowTitle())

    def test_blender_export_progress_responsive_completion_and_cancel(self):
        window = self.window; project = create_project(self.temp.name, '导出响应验证')
        model = project / 'point could'; model.mkdir()
        for name in ('cameras.txt', 'images.txt', 'points3D.txt'):
            (model / name).write_text('1 2 3\n', encoding='utf-8')
        result = dict(layout_version=2, project_dir=str(project), model=str(model), status='complete',
                      metrics={'points': 1}, source=str(self.video), log_dir=str(project / 'log'))
        iid = window.tasks.add(self.video, Options(), '导出响应验证')
        window.tasks.jobs[iid].update(state='完成', result=result)
        window.rebuild_rows(); window.table.selectRow(0); self.app.processEvents()
        settings = dict(path='unused-blender.exe', version='Blender 5.2', fps=24, as_mesh=True,
                        draw_points=True, suppress_warnings=True, background_images=False, point_radius=.005)
        ticks = []; timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(time.monotonic()))

        def real_child(pipeline, args, label):
            output = args[args.index('--output') + 1]
            script = ('import json,time\nfrom pathlib import Path\n'
                      'for percent in (20,70):\n'
                      ' print("[BLENDER_PROGRESS] " + json.dumps(dict(percent=percent,text="phase-"+str(percent))),flush=True)\n'
                      ' time.sleep(.35)\n'
                      f'Path({str(output)!r}).write_bytes(b"BLENDER-test")\n')
            return pipeline._command([sys.executable, '-u', '-c', script], label)

        with patch.object(window, 'choose_blender', return_value=settings), \
             patch.object(window, 'refresh_projects'), patch('tracker_core.Pipeline.command', new=real_child):
            for cancelled in (False, True):
                output = project / ('cancelled.blend' if cancelled else 'complete.blend')
                with patch('qt_app.QFileDialog.getSaveFileName', return_value=(str(output), '')):
                    started = time.monotonic(); window.export_selected()
                self.assertLess(time.monotonic() - started, .25)
                pipeline = window.export_pipeline
                self.assertTrue(window.exporting); self.assertEqual(window.progress.maximum(), 0)
                ticks.clear(); timer.start()
                wait_until(lambda: window.state_label.text() == 'phase-20', app=self.app)
                self.assertEqual(window.progress.value(), 20)
                self.assertFalse(window.blend_button.isEnabled())
                window.nav_group.button(1).click(); self.app.processEvents()
                self.assertEqual(window.pages.currentIndex(), 1)
                window.nav_group.button(0).click()
                if cancelled: window.stop_button.click()
                else:
                    wait_until(lambda: window.state_label.text() == 'phase-70', app=self.app)
                    self.assertGreater(len(ticks), 10)
                    self.assertFalse(output.exists())
                wait_until(lambda: not window.exporting, app=self.app); timer.stop()
                self.assertEqual(pipeline.callback, window.events.put)
                self.assertIsNone(pipeline.process)
                self.assertEqual(output.exists(), not cancelled)
                self.assertTrue(window.blend_button.isEnabled())
                self.assertEqual(window.progress.maximum(), 5)
                self.assertEqual(window.progress.value(), 0 if cancelled else 5)
                self.assertIn('已停止' if cancelled else '已生成', window.state_label.text())

    def test_blender_point_size_default_migration(self):
        from blender_export import default_point_radius, progress_event
        self.assertEqual(default_point_radius({}), .005)
        self.assertEqual(default_point_radius({'blender_point_radius': .01}), .005)
        self.assertEqual(default_point_radius({'blender_point_radius': .05}), .005)
        self.assertEqual(default_point_radius({'blender_point_radius': .002}), .002)
        self.assertEqual(default_point_radius({'blender_point_radius': .01, 'blender_point_size_revision': 2}), .01)
        self.assertIsNone(progress_event('ordinary log line'))
        self.assertIsNone(progress_event('[BLENDER_PROGRESS] invalid'))
        self.assertIsNone(progress_event('[BLENDER_PROGRESS] {}'))
        self.assertEqual(progress_event('[BLENDER_PROGRESS] {"text":"save", "percent":100}')['percent'], 99)

    def test_ffmpeg_scale_and_legacy_cache_identity(self):
        options = Options(output_root=self.temp.name, fps=12, image_scale=.5, cuda_decode=False)
        pipeline = Pipeline(options)
        scene = pipeline.prepare_video(self.video)
        info = video_info(scene / '_cache/images/frame_000001.jpg', 0)
        self.assertEqual((info['width'], info['height']), (320, 180))
        self.assertEqual(len(list((scene / '_cache/images').glob('*.jpg'))), 12)
        original = replace(options, image_scale=1)
        signature = source_signature(self.video, original)
        self.assertNotIn('image_scale', signature)
        self.assertNotIn('image_scale', settings_signature(original))
        self.assertNotEqual(video_scene(self.video, options), video_scene(self.video, original))
        self.assertEqual(video_filters(options, original_size=True), 'fps=12')

    def test_named_project_cache_resume_and_no_collision(self):
        options = Options(output_root=self.temp.name, fps=12, image_scale=.5, cuda_decode=False)
        first = Pipeline(options); first.project_name = '镜头 11'
        scene = first.prepare_video(self.video)
        self.assertEqual(scene.name, '镜头 11')
        self.assertEqual({p.name for p in scene.iterdir()}, {'_cache', 'log'})
        marker = scene / '_cache/.frames-complete'
        timestamp = marker.stat().st_mtime_ns
        resumed = Pipeline(options); resumed.project_name = '镜头 11'; resumed.project_dir = scene
        self.assertEqual(resumed.prepare_video(self.video), scene)
        self.assertEqual(marker.stat().st_mtime_ns, timestamp)
        second = Pipeline(options); second.project_name = '镜头 11'
        self.assertEqual(second.prepare_video(self.video).name, '镜头 11 (2)')
        changed = Pipeline(replace(options, image_scale=.8)); changed.project_name = '镜头 11'; changed.project_dir = scene
        self.assertNotEqual(changed.prepare_video(self.video), scene)
        renamed = Pipeline(options); renamed.project_name = '镜头 12'; renamed.project_dir = scene
        self.assertEqual(renamed.prepare_video(self.video).name, '镜头 12')
        relocated = Pipeline(replace(options, output_root=str(Path(self.temp.name) / 'new-root')))
        relocated.project_name = '镜头 11'; relocated.project_dir = scene
        self.assertEqual(relocated.prepare_video(self.video).parent.name, 'new-root')
        self.assertEqual(safe_name('../CON:*'), '.._CON__')
        self.assertEqual(safe_name('NUL.txt'), '_NUL.txt')
        self.assertEqual(blend_filename('镜头 11', 'Blender 4.2.15 LTS'), '镜头 11_4.2.blend')

    def test_new_and_legacy_projects_scan_move_export_and_archive(self):
        root = Path(self.temp.name)
        project = create_project(root, '镜头 11')
        model = project / 'point could'; model.mkdir()
        for name in ('cameras.txt', 'images.txt', 'points3D.txt'):
            (model / name).write_text('# header\n1 2 3\n', encoding='utf-8')
        (project / '镜头 11_4.2.blend').write_bytes(b'blend data')
        (project / '_cache/database.db').write_bytes(b'cache excluded' * 1000)
        result = dict(layout_version=2, project_dir=str(project), name='镜头 11', model=str(model),
                      scene=str(project), source=str(self.video), status='complete', metrics={'points': 1},
                      completed_at='2026-10-24T11:00:00')
        (project / '_cache/result.json').write_text(json.dumps(result), encoding='utf-8')
        moved = root / '移过位置'; shutil.move(str(project), str(moved))
        legacy = root / 'legacy/results/fastmap/20261024_100000_123456'; (legacy / 'sparse/0').mkdir(parents=True)
        old = dict(result, model=str(legacy / 'sparse/0'), scene=str(root / 'legacy'))
        old.pop('layout_version'); old.pop('project_dir')
        (legacy / 'result.json').write_text(json.dumps(old), encoding='utf-8')
        library = ProjectLibrary(root / 'library.json')
        records, warnings = library.scan(root)
        local = [r for r in records if Path(r['directory']).is_relative_to(root)]
        self.assertFalse(warnings)
        self.assertEqual(len(local), 2)
        new = next(r for r in local if r['ready'])
        self.assertEqual(new['directory'], str(moved.resolve()))
        self.assertEqual(new['result']['model'], str(moved / 'point could'))
        self.assertEqual(new['result']['images_dir'], str(moved / '_cache/images'))
        self.assertEqual(len(new['exports']), 1)
        self.assertLess(new['bytes'], 1000)
        archived = library.archive(new)
        self.assertTrue((archived / '_cache/database.db').is_file())
        self.assertTrue((archived / '_cache/recovery.json').is_file())
        self.assertTrue(self.video.is_file())
        self.assertTrue((legacy / 'result.json').is_file())

    def test_search_toolbar_and_settings_alignment(self):
        window = self.window
        window.search.setText('11'); self.app.processEvents()
        for control in (window.sort_box, window.refresh_button, window.filters.button(0).parentWidget()):
            self.assertEqual(control.height(), window.search.height())
            self.assertEqual(control.mapTo(window, control.rect().center()).y(),
                             window.search.mapTo(window, window.search.rect().center()).y())
        window.grab().save(str(self.artifacts / 'workspace-aligned.png'))
        window.nav_group.button(1).click(); self.app.processEvents()
        self.assertEqual(set(window.default_controls), {'output_root', 'python', 'gpu_index'})
        settings_groups = [group.title() for group in window.pages.widget(1).findChildren(QGroupBox)]
        self.assertEqual(settings_groups, ['路径与环境', '软件更新', 'Blender 版本'])
        self.assertFalse(any('保留策略' in child.text() or '清理过期' in child.text()
                             for child in window.pages.widget(1).findChildren(QLabel)))
        self.assertTrue(window.blender_add_button.text().startswith('添加 Blender 安装目录'))
        blender_status = [child.text() for child in window.pages.widget(1).findChildren(QLabel)
                          if child.text().startswith(('检测到 ', '启动时自动检测 '))]
        self.assertEqual(len(blender_status), 1)
        self.assertTrue(blender_status[0].startswith(('检测到 ', '启动时自动检测 ')))
        self.assertFalse(any('默认 Blender FPS' in child.text() or '默认导出起始帧偏移' in child.text()
                             for child in window.pages.widget(1).findChildren(QLabel)))
        window.grab().save(str(self.artifacts / 'settings-aligned.png'))
        version_labels = [child.text() for child in window.findChildren(QLabel)
                          if child.text().startswith('版本 ')]
        self.assertIn('版本 ' + APP_VERSION, version_labels)

    def test_gpu_selection_is_detected_saved_and_used_for_new_tasks(self):
        window = self.window
        profile = dict(cpu='Test CPU', system_label='Windows 11', gpu_index=0, cuda_usable=True,
                       devices=[dict(index=0, name='NVIDIA GeForce RTX 3060', total_bytes=12 * 2**30),
                                dict(index=1, name='NVIDIA GeForce RTX 4090', total_bytes=24 * 2**30)])
        with patch('qt_app.get_hardware', side_effect=lambda *args, **kwargs: dict(profile, gpu_index=kwargs['gpu_index'])) as probe:
            window.check_environment()
            wait_until(lambda: window.gpu_choice.count() == 2, app=self.app)
            self.assertIn('12.0 GiB', window.gpu_choice.itemText(0))
            window.gpu_choice.setCurrentIndex(1)
            wait_until(lambda: 'GPU 1: RTX 4090' in window.hardware_label.full_text, app=self.app)
            self.assertEqual(probe.call_args.kwargs['gpu_index'], 1)
        with patch.object(Pipeline, 'preflight'):
            window.save_settings_clicked()
        self.assertEqual(window.defaults.gpu_index, 1)
        saved = json.loads(window.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['gpu_index'], 1)
        iid = window.tasks.add(self.video, window.defaults, '所选显卡测试')
        self.assertEqual(window.tasks.jobs[iid]['options']['gpu_index'], 1)
        window.persist_tasks()
        restored, warnings = restore_jobs(window.config_dir / 'task-queue.json')
        self.assertFalse(warnings)
        self.assertEqual(restored[iid]['options']['gpu_index'], 1)
        window.nav_group.button(1).click(); self.app.processEvents()
        window.grab().save(str(self.artifacts / 'settings-multiple-gpus.png'))
        window.update_gpu_choices(dict(devices=[profile['devices'][0]]))
        self.assertEqual(window.gpu_choice.currentData(), 1)
        self.assertIn('未检测到', window.gpu_choice.currentText())

    def test_update_ui_checks_downloads_and_installs_only_when_idle(self):
        window = self.window
        document = dict(version='1.1.2', notes='修复问题并改进界面。', package=dict(bytes=500000))
        with patch('qt_updates.check_for_updates', return_value=dict(status='available', update=document)):
            window.update_panel.check_updates()
            wait_until(lambda: not window.update_panel.checking, app=self.app)
        self.assertEqual(window.sidebar_update.text(), '更新至 1.1.2')
        self.assertFalse(window.sidebar_update.isHidden())
        self.assertTrue(window.update_panel.action.isEnabled())
        window.tasks.running = True
        package = Path(self.temp.name) / 'prepared-patch'; package.mkdir()
        with patch('qt_updates.prepare_update', return_value=package):
            window.update_panel.download_or_install()
            wait_until(lambda: not window.update_panel.downloading, app=self.app)
        self.assertEqual(window.update_panel.action.text(), '安装并重启')
        self.assertFalse(window.update_panel.action.isEnabled())
        with patch('qt_app.QMessageBox.question') as question:
            window.install_update(package)
            question.assert_not_called()
        window.tasks.running = False; window.update_busy()
        self.assertTrue(window.update_panel.action.isEnabled())
        with patch('qt_app.QMessageBox.question', return_value=QMessageBox.StandardButton.No), \
             patch('qt_app.subprocess.Popen') as start:
            window.install_update(package)
            start.assert_not_called()
        with patch('qt_app.ROOT', Path(self.temp.name)), \
             patch('qt_app.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
             patch('hotfix.validate_package'), patch.object(window, 'close') as closed, \
             patch('qt_app.subprocess.Popen') as start:
            window.install_update(package)
            args = start.call_args.args[0]
            self.assertEqual(args[args.index('--wait-pid') + 1], str(os.getpid()))
            self.assertEqual(args[args.index('--package') + 1], str(package))
            self.assertTrue(Path(args[2]).is_file())
            self.assertTrue((Path(args[2]).parent / 'hotfix.py').is_file())
            closed.assert_called_once()
        window.show_updates()
        self.app.processEvents()
        window.grab().save(str(self.artifacts / 'settings-update-ready.png'))
        window.update_panel.automatic.setChecked(False)
        with patch.object(Pipeline, 'preflight'):
            window.save_settings_clicked()
        self.assertFalse(json.loads(window.settings_path.read_text(encoding='utf-8'))['check_updates_on_startup'])

    def test_manually_added_blender_directory_is_validated_and_persisted(self):
        window = self.window
        blender_dir = Path(self.temp.name) / 'Custom Blender'
        blender_dir.mkdir()
        executable = blender_dir / 'blender.exe'; executable.write_bytes(b'blender')
        record = {'path': str(executable), 'version': 'Blender 5.2.0 Custom'}
        with patch('qt_app.QFileDialog.getExistingDirectory', return_value=str(blender_dir)), \
             patch('qt_app.probe_blender', return_value=record):
            window.add_blender_directory()
            wait_until(lambda: record in window.blender_records, app=self.app)
        saved = json.loads((Path(self.temp.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertIn(record, saved['blender_records'])
        self.assertIn(str(executable), saved['blender_paths'])
        self.assertEqual(saved['blender_path'], str(executable))

    def test_dialog_metadata_validation_and_actual_options(self):
        dialog = TaskDialog(self.window, Options(), paths=[self.video]); dialog.show()
        wait_until(lambda: bool(dialog.metadata), app=self.app)
        self.assertFalse(dialog.rate.model().item(1).isEnabled())
        self.assertFalse(dialog.rate.model().item(2).isEnabled())
        dialog.rate.setCurrentIndex(2); dialog.accept_task()
        self.assertIsNone(dialog.result_data)
        dialog.rate.setCurrentIndex(3); dialog.custom_rate.setValue(12); dialog.scale.setCurrentIndex(2)
        dialog.sequence.setChecked(True); dialog.sequence_size.setCurrentIndex(1)
        with patch.object(Pipeline, 'preflight'):
            dialog.accept_task()
        self.assertEqual(dialog.result_data['options'].fps, 12)
        self.assertEqual(dialog.result_data['options'].image_scale, .5)
        self.assertTrue(dialog.result_data['options'].export_sequence)
        self.assertTrue(dialog.result_data['options'].sequence_original)
        dialog.hide(); dialog.deleteLater()

    def test_duplicate_task_name_stays_in_dialog_with_inline_error(self):
        self.window.tasks.add(self.video, Options(), '已有任务')
        dialog = TaskDialog(self.window, Options(), paths=[self.video],
                            name_validator=self.window.tasks.validate_names)
        dialog.show(); wait_until(lambda: bool(dialog.metadata), app=self.app)
        dialog.name.setText('已有任务')
        dialog.accept_task()
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(dialog.result_data)
        self.assertIn('名称已存在', dialog.error.text())
        dialog.hide(); dialog.deleteLater()

    def test_task_blender_area_exposes_full_export_settings_and_frame_offset(self):
        long_path = 'C:/Program Files/Blender Foundation/Blender 5.2.0 DLSS Preview/custom build folder/blender.exe'
        record = {'path': long_path, 'version': 'Blender 5.2.0 DLSS Preview'}
        defaults = {'blender_frame_start_offset': 0, 'blender_as_mesh': True,
                    'blender_draw_points': True, 'blender_background_images': True,
                    'blender_suppress_warnings': True, 'blender_point_radius': .005,
                    'blender_fps': 30}
        dialog = TaskDialog(self.window, Options(), paths=[self.video],
                            blender_records=[record],
                            blender_settings=defaults)
        self.assertTrue(dialog.blender_group.isHidden())
        self.assertEqual(dialog.frame_start_offset.value(), 0)
        self.assertEqual(dialog.blender_note.text(), '检测到 1 个 Blender 版本。')
        dialog.blend.setChecked(True)
        self.assertFalse(dialog.blender_group.isHidden())
        self.assertEqual(dialog.blender_version.currentData(), record)
        self.assertEqual(dialog.blender_version.currentText(), record['version'])
        self.assertEqual(dialog.blender_version.currentData(Qt.ItemDataRole.ToolTipRole), long_path)
        self.assertTrue(any('第 31 帧' in widget.text()
                            for widget in dialog.blender_group.findChildren(QLabel)))
        dialog.show()
        wait_until(lambda: bool(dialog.metadata), app=self.app)
        self.assertLessEqual(dialog.blender_version.width(), dialog.blender_group.width() - 32)
        dialog.findChild(QScrollArea).verticalScrollBar().setValue(
            dialog.findChild(QScrollArea).verticalScrollBar().maximum())
        self.app.processEvents()
        dialog.grab().save(str(self.artifacts / 'task-dialog-long-blender-path.png'))
        dialog.points.setChecked(False)
        dialog.background.setChecked(False)
        dialog.warnings.setChecked(False)
        dialog.radius.setValue(.0123)
        dialog.blender_fps.setValue(24)
        dialog.frame_start_offset.setValue(30)
        with patch.object(Pipeline, 'preflight'):
            dialog.accept_task()
        blender = dialog.result_data['blender']
        self.assertEqual(blender['frame_start_offset'], 30)
        self.assertEqual(blender['fps'], 24)
        self.assertTrue(blender['as_mesh'])
        self.assertFalse(blender['draw_points'])
        self.assertFalse(blender['background_images'])
        self.assertFalse(blender['suppress_warnings'])
        self.assertEqual(blender['point_radius'], .0123)
        self.assertEqual(dialog.result_data['blender']['path'], record['path'])
        dialog.hide(); dialog.deleteLater()

    def test_manual_blender_export_dialog_offers_the_same_scene_options(self):
        record = {'path': 'C:/Blender/blender.exe', 'version': 'Blender 4.2.15'}
        settings = {'_blender_record': record, 'blender_paths': [record['path']],
                    'blender_as_mesh': True, 'blender_draw_points': True,
                    'blender_background_images': True, 'blender_suppress_warnings': True,
                    'blender_point_radius': .005, 'blender_frame_start_offset': 0}
        dialog = BlenderDialog(self.window, settings, fps=30)
        self.assertEqual(dialog.frame_start_offset.value(), 0)
        dialog.points.setChecked(False)
        dialog.background.setChecked(False)
        dialog.radius.setValue(.02)
        dialog.frame_start_offset.setValue(30)
        dialog.accept_settings()
        self.assertEqual(dialog.result_data['frame_start_offset'], 30)
        self.assertFalse(dialog.result_data['draw_points'])
        self.assertFalse(dialog.result_data['background_images'])
        self.assertEqual(dialog.result_data['point_radius'], .02)
        dialog.deleteLater()

    def test_manual_blender_export_uses_selected_version_and_remembers_it(self):
        records = [
            {'path': 'C:/Blender 4.2/blender.exe', 'version': 'Blender 4.2.15'},
            {'path': 'C:/Program Files/Blender Foundation/Blender 5.2.0 DLSS Preview/custom build folder/blender.exe',
             'version': 'Blender 5.2.0 DLSS Preview'},
        ]
        window = self.window
        window.blender_records = records
        window.config['blender_path'] = records[1]['path']

        def choose_version(dialog):
            self.assertEqual(dialog.blender_version.count(), 2)
            self.assertEqual(dialog.blender_version.currentData(), records[1])
            dialog.show(); self.app.processEvents()
            self.assertLessEqual(dialog.blender_version.width(), dialog.width() - 48)
            dialog.blender_version.showPopup(); self.app.processEvents()
            dialog.grab().save(str(self.artifacts / 'manual-blender-version-selection.png'))
            dialog.blender_version.hidePopup()
            dialog.blender_version.setCurrentIndex(0)
            self.assertEqual(dialog.blender_version.currentData(Qt.ItemDataRole.ToolTipRole), records[0]['path'])
            dialog.accept_settings()
            return dialog.result()

        with patch.object(BlenderDialog, 'exec', choose_version):
            chosen = window.choose_blender()
        self.assertEqual(chosen['path'], records[0]['path'])
        self.assertEqual(chosen['version'], records[0]['version'])
        self.assertEqual(chosen['paths'], [record['path'] for record in records])
        self.assertEqual(window.config['blender_path'], records[0]['path'])
        self.assertEqual(blend_filename('测试导出', chosen['version']), '测试导出_4.2.blend')

    def test_task_dialog_centers_on_parent_and_fits_available_screen(self):
        dialog = TaskDialog(self.window, Options(), paths=[self.video])
        dialog.show(); self.app.processEvents()
        parent_center = self.window.frameGeometry().intersected(
            dialog.screen().availableGeometry().adjusted(16, 16, -16, -16)).center()
        dialog_center = dialog.frameGeometry().center()
        self.assertLessEqual(abs(dialog_center.x() - parent_center.x()), 2)
        self.assertLessEqual(abs(dialog_center.y() - parent_center.y()), 2)
        self.assertLessEqual(dialog.width(), dialog.screen().availableGeometry().width() - 32)
        self.assertLessEqual(dialog.height(), dialog.screen().availableGeometry().height() - 32)
        dialog.grab().save(str(self.artifacts / 'task-dialog-centered.png'))
        dialog.hide(); dialog.deleteLater()

    def test_row_actions_remain_pinned_when_table_scrolls_horizontally(self):
        window = self.window
        window.resize(1020, 680); window.show(); self.app.processEvents()
        iid = window.tasks.add(self.video, Options(), '右侧工具滚动测试')
        for number in range(12):
            window.tasks.add(self.video, Options(), f'纵向滚动测试 {number + 1}')
        window.rebuild_rows(); window.table.selectRow(0)
        for column, width in enumerate((300, 240, 240, 180, 180, 220, 142)):
            window.table.setColumnWidth(column, width)
        self.app.processEvents()
        overlay = window.sticky_actions
        bar = window.table.horizontalScrollBar()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        QTest.qWait(60)
        self.app.processEvents()
        self.assertTrue(overlay.isVisible())
        self.assertEqual(overlay.width(), overlay.WIDTH)
        native_bar = window.table.verticalScrollBar()
        native_bar_rect = native_bar.geometry().translated(native_bar.mapTo(window.table_area, QPoint(0, 0)))
        self.assertLess(overlay.geometry().right(), native_bar_rect.left())
        self.assertLessEqual(native_bar_rect.right(), window.table_area.width())
        self.assertGreater(bar.maximum(), 0)
        self.assertFalse(window.table.isColumnHidden(1))
        self.assertFalse(window.table.isColumnHidden(4))
        self.assertGreater(window.table.verticalScrollBar().maximum(), 0)
        self.assertTrue(native_bar.isVisible())
        self.assertGreater(native_bar.maximum(), 0)
        status_left = window.table.horizontalHeader().sectionViewportPosition(5)
        self.assertGreaterEqual(status_left, 0)
        self.assertLessEqual(status_left + window.table.columnWidth(5), window.table.viewport().width())
        window.grab().save(str(self.artifacts / 'workspace-min-horizontal-scroll.png'))

    def test_vertical_scrollbar_stays_aligned_after_maximize_restore_resizes(self):
        window = self.window
        window.resize(1180, 760); window.show(); self.app.processEvents()
        for number in range(24):
            window.tasks.add(self.video, Options(), f'窗口缩放滚动条测试 {number + 1}')
        window.rebuild_rows()

        sizes = iter(((1180, 760), (860, 620), (1280, 780), (980, 700)))
        for state in ('maximized', 'restored', 'maximized', 'restored'):
            if state == 'maximized':
                window.showMaximized()
            else:
                window.showNormal()
                window.resize(*next(sizes))
            self.app.processEvents()
            QTest.qWait(60)
            self.app.processEvents()
            table_bar = window.table.verticalScrollBar()
            self.assertGreater(table_bar.maximum(), 0)
            self.assertTrue(table_bar.isVisible())
            self.assertEqual(window.sticky_actions.geometry().top(), window.table.geometry().top())
            self.assertEqual(window.sticky_actions.geometry().height(), window.table.height())
            table_bar_rect = table_bar.geometry().translated(table_bar.mapTo(window.table_area, QPoint(0, 0)))
            self.assertLess(window.sticky_actions.geometry().right(), table_bar_rect.left())
            self.assertLessEqual(table_bar_rect.right(), window.table_area.width())
            header = window.table.horizontalHeader()
            expected_header_bottom = header.mapTo(
                window.table_area, QPoint(0, header.height())).y() - window.sticky_actions.y()
            self.assertEqual(window.sticky_actions.header_bottom(), expected_header_bottom)
            cell = window.table.visualRect(window.table.model().index(0, 0))
            expected_row_top = window.table.viewport().mapTo(
                window.table_area, cell.topLeft()).y() - window.sticky_actions.y()
            self.assertEqual(window.sticky_actions.row_rect(0).top(), expected_row_top)
            self.assertGreaterEqual(expected_row_top, expected_header_bottom)
            first_row = window.table.rowAt(0)
            last_row = window.table.rowAt(window.table.viewport().height() - 1)
            rail_image = window.sticky_actions.grab().toImage()
            for row_number in range(first_row, last_row + 1):
                row_rect = window.sticky_actions.row_rect(row_number)
                icon_top = row_rect.top() + (row_rect.height() - 26) // 2 + 5
                if icon_top < 0 or icon_top + 16 > rail_image.height():
                    continue
                for action_number in range(5):
                    icon_left = 11 + action_number * 26
                    pixels = sum(
                        rail_image.pixelColor(x, y) != QColor('#ffffff')
                        for x in range(icon_left, icon_left + 16)
                        for y in range(icon_top, icon_top + 16)
                        if 0 <= x < rail_image.width() and 0 <= y < rail_image.height())
                    self.assertGreater(pixels, 0, f'row {row_number} action {action_number} is not painted')

    def test_row_actions_render_when_rows_do_not_fill_viewport(self):
        window = self.window
        window.resize(1500, 1000); window.show(); self.app.processEvents()
        for number in range(3):
            window.tasks.add(self.video, Options(), f'空白视口工具栏测试 {number + 1}')
        window.rebuild_rows(); self.app.processEvents()
        table, rail = window.table, window.sticky_actions
        self.assertGreater(table.viewport().height(), table.rowHeight(0) * table.model().rowCount())
        self.assertEqual(table.rowAt(table.viewport().height() - 1), -1)
        image = rail.grab().toImage()
        for row_number in range(table.model().rowCount()):
            row_rect = rail.row_rect(row_number)
            icon_top = row_rect.top() + (row_rect.height() - 26) // 2 + 5
            for action_number in range(5):
                icon_left = 11 + action_number * 26
                pixels = sum(
                    image.pixelColor(x, y) != QColor('#ffffff')
                    for x in range(icon_left, icon_left + 16)
                    for y in range(icon_top, icon_top + 16))
                self.assertGreater(pixels, 0, f'row {row_number} action {action_number} is missing')

    def test_selected_row_highlight_fills_gap_before_pinned_actions(self):
        window = self.window
        window.resize(1500, 800); self.app.processEvents()
        window.tasks.add(self.video, Options(), '整行高亮测试')
        window.rebuild_rows()
        table = window.table
        window.table.selectRow(0)
        self.app.processEvents()
        row_y = table.rowViewportPosition(0) + table.rowHeight(0) // 2
        action_y = window.sticky_actions.row_rect(0).center().y()
        header_bottom = table.horizontalHeader().mapTo(
            window.table_area, QPoint(0, table.horizontalHeader().height())).y() - window.sticky_actions.y()
        self.assertGreaterEqual(window.sticky_actions.row_rect(0).top(), header_bottom)
        viewport_image = table.viewport().grab().toImage()
        action_image = window.sticky_actions.grab().toImage()
        self.assertEqual(QColor(viewport_image.pixel(table.viewport().width() - 4, row_y)), QColor('#eaf0ff'))
        self.assertEqual(QColor(action_image.pixel(window.sticky_actions.width() - 4, action_y)), QColor('#eaf0ff'))
        table.clearSelection(); self.app.processEvents()
        viewport_image = table.viewport().grab().toImage()
        action_image = window.sticky_actions.grab().toImage()
        self.assertEqual(QColor(viewport_image.pixel(table.viewport().width() - 4, row_y)), QColor('#ffffff'))
        self.assertEqual(QColor(action_image.pixel(window.sticky_actions.width() - 4, action_y)), QColor('#ffffff'))

    def test_detail_diagnostic_wraps_without_elision_or_vertical_clipping(self):
        window = self.window
        window.tasks.add(self.video, Options(), '求解提醒换行测试')
        window.rebuild_rows(); window.table.selectRow(0)
        message = ('FastMap · 完成 · 重投影误差 2.402 px · 求解提醒：'
                   'FastMap 极线优化触及迭代上限，已保留当前求解结果。'
                   '请检查相机轨迹和点云，必要时提高求解精度或增加拍摄移动范围。')
        window.detail_info.setFullText(message)
        heights = []
        for width in (1360, 1020, 1360):
            window.resize(width, 800)
            QTest.qWait(30); self.app.processEvents()
            note = window.detail_info
            self.assertTrue(note.wordWrap())
            self.assertEqual(note.text(), message)
            self.assertGreaterEqual(note.height(), note.heightForWidth(note.width()))
            self.assertTrue(note.parentWidget().rect().contains(note.geometry()))
            heights.append(note.minimumHeight())
        self.assertGreater(heights[1], heights[0])
        self.assertEqual(heights[0], heights[2])
        window.resize(1020, 680); QTest.qWait(30); self.app.processEvents()
        window.grab().save(str(self.artifacts / 'detail-diagnostic-wrapped.png'))

    def test_unified_history_filters_persistence_and_responsive_ui(self):
        records, _ = ProjectLibrary().scan(str(ROOT / 'outputs'))
        self.window.records = {r['id']: r for r in records}
        iid = self.window.tasks.add(self.video, Options(fps=12, image_scale=.5), '回归测试 · ' + '长名称' * 30)
        self.window.rebuild_rows(); self.window.table.selectRow(0); self.app.processEvents()
        self.assertTrue(self.window.edit_button.isEnabled())
        self.assertEqual(self.window.source_path.toolTip(), str(self.video.resolve()))
        self.assertEqual(self.window.processing_value.toolTip(), '12 fps / 0.5× · fastmap · SIFT 4096 · 匹配 15')
        self.assertFalse(self.window.processing_value.wordWrap())
        narrow_value = DetailValueLabel()
        narrow_value.setFixedWidth(180)
        narrow_value.setFullText('原始帧率 / 0.8× · GLOMap GPU · SIFT 4096 · 匹配 15')
        self.assertTrue(narrow_value.text().endswith('…'))
        self.assertLessEqual(narrow_value.fontMetrics().horizontalAdvance(narrow_value.text()),
                             narrow_value.contentsRect().width())
        self.assertEqual(self.window.output_content.text(), '待生成')
        self.assertEqual(self.window.sequence_value.text(), '未输出')
        self.assertEqual(self.window.detail_status.text(), '等待')
        self.assertEqual(self.window.recent_logs.toPlainText(), '任务尚未开始，暂无日志。')
        self.assertNotIn('launcher.log', self.window.recent_logs.toPlainText())
        self.assertFalse(any(button.text() == '查看完整日志'
                             for button in self.window.findChildren(QPushButton)))
        self.assertIn('最近日志', self.window.recent_logs.parentWidget().findChildren(QLabel)[0].text())
        self.assertTrue(self.window.remove_button.isEnabled())
        fields = self.window.findChild(QWidget, 'detailSummary').findChildren(QWidget, 'detailField')
        self.app.processEvents()
        self.assertEqual(len(fields), 7)
        self.assertGreaterEqual(fields[1].geometry().left(), fields[0].geometry().right())
        self.assertEqual(fields[2].width(), fields[0].width())
        self.assertGreaterEqual(fields[3].geometry().left(), fields[2].geometry().right())
        self.assertGreaterEqual(fields[5].geometry().left(), fields[4].geometry().right())
        for width in (1020, 1360):
            self.window.resize(width, 680)
            self.app.processEvents()
            for field in fields:
                value = field.findChild(DetailValueLabel)
                self.assertGreaterEqual(value.height(), value.fontMetrics().height() + 4)
                self.assertTrue(field.rect().contains(value.geometry()))
                self.assertLessEqual(value.fontMetrics().horizontalAdvance(value.text()),
                                     value.contentsRect().width())
                self.assertTrue(field.parentWidget().rect().contains(field.geometry()))
            self.assertGreaterEqual(self.window.detail_info.height(),
                                    self.window.detail_info.fontMetrics().height() + 4)
        self.window.grab().save(str(self.artifacts / 'detail-text-wrapped.png'))
        selected_row = self.window.selected()
        selected_row['exports'] = [str(Path(self.temp.name) / 'existing_5.2.blend')]
        selected_row['result'] = {'model': str(Path(self.temp.name) / 'point could')}
        with patch.object(self.window, 'selected', return_value=selected_row):
            self.window.update_details()
        self.assertEqual(self.window.output_content.text(), '点云 · existing_5.2.blend')
        self.window.filters.button(1).setChecked(True); self.window.apply_filter()
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.search.setText('不存在的关键词'); self.app.processEvents(); self.assertEqual(self.window.proxy.rowCount(), 0)
        self.window.search.clear(); self.window.filters.button(0).setChecked(True); self.window.apply_filter()
        self.window.persist_tasks()
        saved = json.loads((Path(self.temp.name) / 'task-queue.json').read_text(encoding='utf-8'))
        self.assertEqual(saved[0]['options']['image_scale'], .5)
        self.window.tasks.jobs.clear(); self.window.restore_tasks(); self.assertIn(iid, self.window.tasks.jobs)
        ticks = []; timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
        finished = threading.Event()
        threading.Thread(target=lambda: (time.sleep(.25), finished.set()), daemon=True).start()
        wait_until(finished.is_set, app=self.app); timer.stop(); self.assertGreater(len(ticks), 5)
        self.window.resize(1020, 680); self.app.processEvents(); self.assertLessEqual(self.window.width(), 1020)
        self.window.grab().save(str(self.artifacts / 'workspace-small.png'))

    def test_status_bar_shows_compact_hardware_and_keeps_probe_details_in_settings(self):
        profile = dict(cpu='Intel(R) Core(TM) i7-14700K', system_label='Windows 11 25H2 (26200.1000)',
                       devices=[dict(name='NVIDIA GeForce RTX 5070 Ti', total_bytes=16 * 2**30)],
                       cuda_usable=True)
        window = self.window
        with patch('qt_app.get_hardware', return_value=profile), patch.object(window, 'schedule_estimate'):
            window.check_environment()
            wait_until(lambda: 'RTX 5070 Ti' in window.hardware_label.full_text, app=self.app)
        self.assertIn('CPU: Intel Core i7-14700K', window.hardware_label.full_text)
        self.assertIn('显存 16.0 GiB', window.hardware_label.full_text)
        self.assertIn('Windows 11 25H2', window.hardware_label.full_text)
        self.assertNotIn('实算', window.hardware_label.full_text)
        self.assertNotIn('已构建', window.hardware_label.full_text)
        self.assertIn('CUDA 实算通过', window.environment_info.text())
        self.assertIn('CUDA 实算通过', window.hardware_label.toolTip())
        for width in (1020, 1360):
            window.resize(width, 680); self.app.processEvents()
            self.assertLessEqual(window.hardware_label.fontMetrics().horizontalAdvance(window.hardware_label.text()),
                                 window.hardware_label.contentsRect().width())
            self.assertLess(window.hardware_label.geometry().right(), window.elapsed_label.geometry().left())
        window.grab().save(str(self.artifacts / 'status-bar-hardware.png'))

    def test_detail_header_keeps_status_next_to_short_and_long_names(self):
        window = self.window
        for width in (1020, 1360):
            window.resize(width, 680)
            for name in ('操场', '非常长的任务名称' * 30, '测试'):
                window.detail_title.setFullText(name)
                self.app.processEvents()
                title = window.detail_title.geometry()
                status = window.detail_status.geometry()
                self.assertGreater(title.width(), 0)
                self.assertEqual(status.left() - title.right() - 1, 8)
                self.assertLess(status.right(), window.edit_button.geometry().left())
                self.assertLessEqual(window.detail_title.fontMetrics().horizontalAdvance(window.detail_title.text()),
                                     window.detail_title.contentsRect().width())
        window.grab().save(str(self.artifacts / 'detail-header.png'))

    def test_detail_summary_refreshes_when_selecting_another_project(self):
        window = self.window
        ids = [window.tasks.add(self.video, Options(fps=fps), name)
               for fps, name in ((12, '项目甲'), (24, '项目乙'))]
        first, second = ids
        for iid, created, frames, points, seconds, sequence in (
                (first, '2026-10-24T17:43:00', 776, 55133, 402, '/甲/video'),
                (second, '2026-11-07T18:38:00', 120, 8021, 65, '')):
            window.tasks.jobs[iid].update(state='完成', created_at=created,
                result={'metrics': {'registered_images': frames, 'points': points},
                        'task_wall_seconds': seconds, 'sequence': sequence})
        window.rebuild_rows(); self.app.processEvents()
        window.table.selectRow(0); self.app.processEvents()
        self.assertEqual(window.created_value.text(), '2026-10-24 17:43')
        self.assertIn('776 帧 / 55,133 点', window.duration_value.text())
        self.assertEqual(window.sequence_value.text(), '已导出')
        window.table.selectRow(1); self.app.processEvents()
        self.assertEqual(window.created_value.text(), '2026-11-07 18:38')
        self.assertIn('120 帧 / 8,021 点', window.duration_value.text())
        self.assertEqual(window.sequence_value.text(), '未输出')

    def test_logs_and_three_navigation_pages(self):
        for index in range(3):
            self.window.nav_group.button(index).click(); self.app.processEvents(); self.assertEqual(self.window.pages.currentIndex(), index)
        self.window.append_log('一条长日志 ' * 50000)
        self.assertLessEqual(self.window.log_chars, 131072)
        self.window.open_logs(); self.app.processEvents(); self.assertTrue(self.window.log_dialog)
        self.window.log_dialog.close(); self.app.processEvents()
        # DeferredDelete is processed when the normal event loop runs.
        QTimer.singleShot(30, self.app.quit); self.app.exec(); self.assertIsNone(self.window.log_widget)

    def test_created_sort_output_size_and_real_row_action_click(self):
        first = self.window.tasks.add(self.video, Options(), '较早任务')
        second = self.window.tasks.add(self.video, Options(), '最新任务')
        self.window.tasks.jobs[first]['created_at'] = '2026-10-23T10:00:00'
        self.window.tasks.jobs[second]['created_at'] = '2026-10-24T10:00:00'
        self.window.rebuild_rows(); self.app.processEvents()
        self.assertEqual(self.window.sort_box.currentText(), '执行顺序')
        self.window.sort_box.setCurrentIndex(self.window.sort_box.findText('最新创建'))
        self.assertEqual(self.window.sort_box.currentText(), '最新创建')
        self.assertEqual(self.window.proxy.index(0, 0).data(Qt.ItemDataRole.UserRole)['id'], second)
        self.window.sort_box.setCurrentIndex(self.window.sort_box.findText('最早创建')); self.app.processEvents()
        self.assertEqual(self.window.sort_box.currentText(), '最早创建')
        self.assertEqual(self.window.proxy.index(0, 0).data(Qt.ItemDataRole.UserRole)['id'], first)
        self.window.sort_box.setCurrentIndex(self.window.sort_box.findText('最新创建'))
        point = QPoint(6 + 4 * 26 + 13,
                       self.window.sticky_actions.row_rect(0).center().y())
        QTest.mouseClick(self.window.sticky_actions, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertNotIn(second, self.window.tasks.jobs)
        self.assertIn(first, self.window.tasks.jobs)
        records, _ = ProjectLibrary().scan(str(ROOT / 'outputs'))
        self.assertTrue(all(record['bytes'] > 0 for record in records))
        self.assertTrue(all(record['created'] for record in records))

    def test_priority_arrow_clicks_persistence_and_start_all_with_selection(self):
        window = self.window
        a, b, c = [window.tasks.add(self.video, Options(), name) for name in ('任务 A', '任务 B', '任务 C')]
        window.rebuild_rows(); self.app.processEvents()
        self.assertFalse(window.priority_up.isEnabled()); self.assertFalse(window.priority_down.isEnabled())
        window.sort_box.setCurrentIndex(window.sort_box.findText('最新创建'))
        def select(iid):
            index = next(i for i in range(window.proxy.rowCount()) if window.proxy.index(i, 0).data(Qt.ItemDataRole.UserRole)['id'] == iid)
            window.table.selectRow(index); self.app.processEvents()
        select(c)
        QTest.mouseClick(window.priority_up, Qt.MouseButton.LeftButton); self.app.processEvents()
        self.assertEqual(list(window.tasks.snapshot()), [a, c, b])
        self.assertEqual(window.sort_box.currentText(), '执行顺序')
        self.assertEqual([window.proxy.index(i, 0).data(Qt.ItemDataRole.UserRole)['id'] for i in range(3)], [a, c, b])
        self.assertEqual(window.selected()['id'], c)
        QTest.mouseClick(window.priority_up, Qt.MouseButton.LeftButton); self.app.processEvents()
        self.assertEqual(list(window.tasks.snapshot()), [c, a, b])
        self.assertFalse(window.priority_up.isEnabled()); self.assertTrue(window.priority_down.isEnabled())
        QTest.mouseClick(window.priority_down, Qt.MouseButton.LeftButton); self.app.processEvents()
        self.assertEqual(list(window.tasks.snapshot()), [a, c, b])
        window.tasks.jobs.clear(); window.restore_tasks(); window.rebuild_rows(); select(c)
        self.assertEqual(list(window.tasks.snapshot()), [a, c, b])
        self.assertIn('第 2 项', window.detail_info.text())
        with patch.object(window.tasks, 'start', return_value=True) as start:
            QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
            start.assert_called_once_with(None)
        with patch.object(window.tasks, 'start', return_value=True) as start:
            window.row_action(window.selected(), 'play')
            start.assert_called_once_with([c])
        window.grab().save(str(self.artifacts / 'workspace-priority.png'))
        for width in (1020, 1360):
            window.resize(width, 800); self.app.processEvents()
            for control in (window.priority_up, window.priority_down, window.refresh_button):
                self.assertEqual(control.height(), window.search.height())
                self.assertLess(control.geometry().right(), window.width())

    def test_column_boundary_tracks_mouse_in_both_directions(self):
        header = self.window.table.horizontalHeader()
        self.app.processEvents()
        for section in (1, 2, 3, 4):
            for delta in (35, -20):
                original_name_width = header.sectionSize(0)
                original_width = header.sectionSize(section)
                original_boundary = header.sectionViewportPosition(section) + original_width
                start = QPoint(original_boundary - 1, header.height() // 2)
                end = start + QPoint(delta, 0)
                QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
                QTest.mouseMove(header.viewport(), end, 25)
                QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
                self.app.processEvents()
                boundary = header.sectionViewportPosition(section) + header.sectionSize(section)
                self.assertEqual(header.sectionSize(section), original_width + delta)
                self.assertEqual(boundary, original_boundary + delta)
                self.assertEqual(header.sectionSize(0), original_name_width)
        start = QPoint(header.sectionSize(0) - 1, header.height() // 2)
        end = start + QPoint(45, 0)
        QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(header.viewport(), end, 25)
        QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.app.processEvents()
        chosen_name_width = header.sectionSize(0)
        chosen_settings_width = header.sectionSize(2)
        self.assertEqual(chosen_name_width, original_name_width + 45)

    def test_window_resize_keeps_left_columns_anchored_and_stretches_status(self):
        table = self.window.table; header = table.horizontalHeader()
        for number in range(24):
            self.window.tasks.add(self.video, Options(), f'窗口缩放测试 {number + 1}')
        self.window.rebuild_rows(); self.app.processEvents()
        for section, delta in ((0, -80), (2, 25)):
            start = QPoint(header.sectionViewportPosition(section) + header.sectionSize(section) - 1, header.height() // 2)
            end = start + QPoint(delta, 0)
            original = header.sectionSize(section)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(header.viewport(), end, 25)
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.app.processEvents()
            self.assertEqual(header.sectionSize(section), original + delta)
        fixed_widths = [header.sectionSize(section) for section in range(5)]

        def assert_fitted():
            self.app.processEvents()
            self.assertEqual([header.sectionSize(section) for section in range(5)], fixed_widths)
            self.assertTrue(table.isColumnHidden(6))
            used = sum(fixed_widths)
            if table.viewport().width() >= used + header.minimumSectionSize():
                self.assertEqual(header.length(), table.viewport().width())
                self.assertEqual(table.horizontalScrollBar().maximum(), 0)
                self.assertGreaterEqual(header.sectionSize(5), header.minimumSectionSize())
            else:
                self.assertGreater(table.horizontalScrollBar().maximum(), 0)

        for width in (1550, 1700, 1200, 1190, 1020, 1360):
            self.window.resize(width, 800); self.app.processEvents()
            assert_fitted()
        self.window.showMaximized(); assert_fitted()
        self.window.showNormal(); assert_fitted()
        self.window.grab().save(str(self.artifacts / 'workspace-resizing.png'))


if __name__ == '__main__': unittest.main(verbosity=2)
