"""Real solver-log replay, live child output, ETA math, and Qt integration."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from tracker_core import Options, Pipeline
from task_queue import TaskQueue
from task_progress import TaskProgress, FASTMAP_STAGES, stage_plan, live_remaining
from fastmap_progress import install_progress_reporting
from estimate import prediction_range, estimate_jobs
from hardware import settings_signature
from qt_app import MainWindow, configure_application
from PySide6.QtWidgets import QApplication


class ProgressTests(unittest.TestCase):
    def tracker(self, options=None):
        self.now = 100.0
        self.events = []
        return TaskProgress(options or Options(), lambda kind, **data: self.events.append(dict(type=kind, **data)),
                            clock=lambda: self.now)

    def feed(self, tracker, label, lines):
        for line in lines:
            self.now += .3
            tracker.observe(label, line)

    def test_stage_plans_follow_both_solvers_and_optional_outputs(self):
        self.assertEqual(len(stage_plan(Options())), 21)
        self.assertEqual(len(stage_plan(Options(backend='glomap'))), 19)
        full = stage_plan(Options(export_sequence=True), blender=True)
        self.assertEqual(len(full), 23)
        self.assertEqual([key for key, _ in full][-3:], ['sequence', 'publish', 'blender'])
        lean = dict(stage_plan(Options(camera_mode='pinhole', color=False)))
        self.assertNotIn('distortion', lean)
        self.assertNotIn('matrices', lean)
        self.assertNotIn('color', lean)
        tracker = TaskProgress(Options(), lambda *args, **kwargs: None, blender=True)
        tracker.coarse(5, '导出 Blender TXT 和点云')
        self.assertEqual(tracker.key, 'export_txt')
        tracker.command('05-export-ply')
        self.assertEqual(tracker.key, 'export_ply')
        tracker.coarse(5, '生成 Blender 场景')
        self.assertEqual(tracker.key, 'blender')

    def test_glomap_five_of_five_is_followed_by_final_ba(self):
        tracker = self.tracker(Options(backend='glomap'))
        self.feed(tracker, '04-glomap', [
            'Running global positioning ...',
            'Running bundle adjustment ...',
            'Global bundle adjustment iteration 3 / 3, stage 2 finished',
            'Running retriangulation ...', 'Triangulating image 567 / 567',
            'Global bundle adjustment iteration 1 / 5',
            'Global bundle adjustment iteration 5 / 5',
            'Running bundle adjustment ...', '[GPU] Bundle adjustment: CUDA_SPARSE, images=567',
        ])
        self.assertEqual(tracker.key, 'final_ba')
        self.assertEqual(self.events[-1]['text'], '最终全局 BA 优化')
        self.assertIsNone(self.events[-1]['fraction'])
        self.assertLess(self.events[-1]['overall'], 1)
        values = [event['overall'] for event in self.events]
        self.assertEqual(values, sorted(values))
        self.assertIn('剩余时间：等待', live_remaining(self.events[-1], self.now + 90))
        self.assertIn('未收到新日志', live_remaining(self.events[-1], self.now + 90))
        tracker.observe('04-glomap', 'Reconstruction done in 109.491 seconds')
        self.assertEqual(tracker.key, 'write')

    def test_real_glomap_and_fastmap_logs_replay_without_early_completion(self):
        # Local fixtures are optional; deterministic cases above/below always run.
        glomap_log = ROOT / 'output/14/log/04-glomap.log'
        if glomap_log.exists():
            tracker = self.tracker(Options(backend='glomap'))
            self.feed(tracker, '04-glomap', glomap_log.read_text(encoding='utf-8').splitlines())
            phases = {event['phase'] for event in self.events}
            self.assertTrue({'positioning', 'ba', 'triangulate', 'refine', 'final_ba', 'write'} <= phases)
            values = [event['overall'] for event in self.events]
            self.assertEqual(values, sorted(values))
            self.assertLess(max(values), 1)
        fastmap_log = ROOT / 'output/1445/log/04-fastmap.log'
        if fastmap_log.exists():
            tracker = self.tracker()
            self.feed(tracker, '04-fastmap', fastmap_log.read_text(encoding='utf-8').splitlines())
            phases = {event['phase'] for event in self.events}
            self.assertTrue({'read', 'distortion', 'focal', 'rotation', 'tracks', 'relative_t',
                             'translation', 'epipolar', 'sparse'} <= phases)

    def test_fastmap_rounds_counters_and_structured_timer_stages(self):
        from task_progress import FASTMAP_TIMERS
        tracker = self.tracker()
        for name in FASTMAP_TIMERS:
            self.now += 1
            tracker.observe('04-fastmap', '[POINTSTUDIO_STAGE] ' + json.dumps(dict(name=name)))
            if name == 'Global Epipolar Optimization':
                self.feed(tracker, '04-fastmap', [
                    'fastmap.epipolar:loop:1 - [Round 2 / 4] Starting IRLS step 2 / 3...',
                    'fastmap.epipolar:loop:2 - [Iter 500] loss=0.1',
                ])
                self.assertEqual(tracker.current, 4)
                self.assertEqual(tracker.total, 12)
                self.assertIn('2/4', self.events[-1]['detail'])
                self.assertIn('500', self.events[-1]['detail'])
        self.assertEqual([event['phase'] for event in self.events if not event['detail']],
                         [key for key, _ in FASTMAP_STAGES])
        self.assertEqual(tracker.key, 'write')

    def test_real_counters_and_live_eta_do_not_advance_without_evidence(self):
        tracker = self.tracker()
        tracker.frames = 200
        tracker.command('01-frames')
        self.now += 10
        tracker.observe('01-frames', 'frame= 50 fps=5.0')
        event = self.events[-1]
        self.assertEqual(event['fraction'], .25)
        self.assertIn('00:21–00:45', live_remaining(event, now=self.now))
        self.assertIn('00:16–00:40', live_remaining(event, now=self.now + 5))
        self.assertIn('未收到新日志', live_remaining(event, now=self.now + 60))
        self.assertNotIn('按当前速度', live_remaining(event, now=self.now + 60))
        self.assertEqual(event['fraction'], .25)
        self.feed(tracker, '02-features', ['Processed file [1/200]', 'Processed file [200/200]'])
        self.assertEqual(self.events[-1]['fraction'], 1)
        self.feed(tracker, '03-matching', ['Matching image [40/200]'])
        self.assertEqual(tracker.key, 'matching')
        self.assertEqual(self.events[-1]['current'], 40)
        tracker.begin('read')
        self.feed(tracker, '04-fastmap', ['[POINTSTUDIO_STAGE] {"name":"Sparse Reconstruction"}',
            '[POINTSTUDIO_STAGE] {"name":"Sparse batch","current":100,"total":1000}'])
        self.assertEqual(self.events[-1]['fraction'], .1)

    def test_history_countdown_expires_without_showing_negative_or_complete(self):
        tracker = self.tracker(Options(backend='glomap'))
        tracker.estimate = dict(lower=60, upper=120)
        tracker.begin('final_ba')
        event = self.events[-1]
        self.assertIn('00:30–01:30', live_remaining(event, now=130))
        self.assertIn('已超出原估时', live_remaining(event, now=300))
        self.assertLess(event['overall'], 1)
        event['phase'] = 'blender'
        self.assertNotIn('本任务剩余', live_remaining(event, now=130))

    def test_prediction_uses_local_hardware_and_actual_resume_workload(self):
        options = Options()
        work = dict(frames=100, stages={'extract': True}, existing_scene=False,
                    width=1920, height=1080, codec='h264', pairs=None)
        sample = dict(schema=2, backend='fastmap', frames=100, seconds=120,
                      hardware_key='local', settings=settings_signature(options), workload=work,
                      gpu_solver_active=True)
        self.assertEqual(prediction_range(work, options, {'fingerprint': 'local'}, [sample])['upper'], 240)
        self.assertIsNone(prediction_range(work, options, {'fingerprint': 'other'}, [sample]))
        self.assertIsNone(prediction_range(work, options, {'fingerprint': 'local'}, [dict(sample, gpu_solver_active=False)]))
        with patch('estimate.get_hardware', return_value={'fingerprint': 'local', 'cuda_usable': True}), \
             patch('estimate.read_samples', return_value=[sample]), \
             patch('estimate.describe_workload', return_value=work) as describe:
            text = estimate_jobs([dict(path='clip.mp4', scene=False, project_dir='existing')], options)
        describe.assert_called_once_with('clip.mp4', options, False, 'existing')
        self.assertIn('参考', text)

    def test_fastmap_reporting_preserves_batches_and_reports_completed_work(self):
        output, actions = [], []
        class Timer:
            def __init__(self, name): self.name = name
            def __enter__(self): actions.append('entered'); return self
            def __exit__(self, *args): actions.append('exited')
        batches = [SimpleNamespace(num_point_pairs=3), SimpleNamespace(num_point_pairs=7)]
        class Pairs:
            num_point_pairs = 10
            def query(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs
                yield from batches
        def write(value):
            output.append(json.loads(value.split(' ', 1)[1]))
            if output[-1]['name'] == 'Sparse Reconstruction': self.assertEqual(actions, [])
        install_progress_reporting(Timer, Pairs, write=write, clock=lambda: 0)
        pairs = Pairs()
        with Timer('Sparse Reconstruction'):
            with Timer('nested'):
                actual = list(pairs.query('mask', batch_size=42))
        self.assertIs(actual[0], batches[0])
        self.assertIs(actual[1], batches[1])
        self.assertEqual(pairs.args, ('mask',))
        self.assertEqual(pairs.kwargs, {'batch_size': 42})
        counts = [item['current'] for item in output if item['name'] == 'Sparse batch']
        self.assertEqual(counts, [3, 10])
        before = len(output)
        list(pairs.query())
        self.assertEqual(len(output), before)

    def test_child_output_reaches_queue_before_process_exit(self):
        events, live = [], []
        with tempfile.TemporaryDirectory() as directory:
            class Solver(Pipeline):
                def run(self, path, existing_scene=False):
                    self.log_dir = Path(directory)
                    lines = ['Running bundle adjustment ...', 'Running retriangulation ...',
                             'Triangulating image 5 / 10', 'Running bundle adjustment ...']
                    script = ('import time\n' + '\n'.join(f'print({line!r},flush=True); time.sleep(.03)' for line in lines))
                    self._command([sys.executable, '-u', '-c', script], '04-glomap')
                    return dict(status='complete')
            def receive(event):
                events.append(event)
                if event['type'] == 'task_progress' and event['phase'] == 'final_ba':
                    live.append(queue.pipeline.process.poll() is None)
            queue = TaskQueue(receive, Solver, lambda result: None)
            iid = queue.add('clip.mp4', Options(backend='glomap'))
            queue.start()
            deadline = time.monotonic() + 5
            while queue.running and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(queue.running)
            self.assertEqual(live, [True])
            self.assertEqual(queue.jobs[iid]['state'], '完成')


class ProgressDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_application(cls.app)

    def test_final_ba_and_blender_show_independent_progress_without_early_100(self):
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(config_dir=directory, background=False)
            window.show(); self.app.processEvents()
            iid = window.tasks.add('sample.mp4', Options(backend='glomap'), '进度显示验证')
            window.tasks.jobs[iid]['state'] = '运行中'
            window.tasks.running = True
            window.started = time.perf_counter()
            window.events.put(dict(type='job', iid=iid, state='运行中'))
            def receive(kind, **data): window.events.put(dict(type=kind, iid=iid, **data))
            tracker = TaskProgress(Options(backend='glomap'), receive, blender=True)
            tracker.begin('final_ba', '等待优化器收敛')
            window.poll(); self.app.processEvents()
            self.assertIn('最终全局 BA', window.state_label.text())
            self.assertEqual(window.phase_progress.maximum(), 0)
            self.assertLess(window.progress.value(), 1000)
            self.assertIn('剩余时间', window.progress_note.text())
            self.assertIn('最终全局 BA', window.tasks.jobs[iid]['stage'])
            # Stale estimates from before queue start cannot replace live status.
            previous = window.estimate_label.text()
            window.events.put(dict(type='estimate', generation=window.estimate_generation, text='stale'))
            window.poll(); self.assertEqual(window.estimate_label.text(), previous)
            artifacts = ROOT / 'tests/artifacts/progress'
            artifacts.mkdir(exist_ok=True)
            window.grab().save(str(artifacts / 'glomap-final-ba.png'))
            tracker.begin('blender', '保存 Blender 工程', 70, 100, 'blender')
            window.poll(); self.app.processEvents()
            self.assertEqual(window.phase_progress.value(), 700)
            self.assertLess(window.progress.value(), 1000)
            window.events.put(dict(type='job', iid=iid, state='完成'))
            window.poll()
            self.assertEqual(window.progress.value(), 1000)
            self.assertFalse(window.phase_progress.isVisible())
            window.tasks.running = False
            window.close()


if __name__ == '__main__':
    unittest.main()
