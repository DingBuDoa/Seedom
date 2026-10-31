"""Convergence, bounded real solvers, warning propagation and precision controls."""
from contextlib import redirect_stdout, nullcontext
from dataclasses import asdict, replace
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'runtime/fastmap'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import torch
from fastmap.utils import ConvergenceManager
from fastmap.optimization import configure, policy
from fastmap.point_pair import point_pairs_from_tracks, image_pair_index
from fastmap.container import Images, Cameras
from fastmap import epipolar
from test_fastmap_memory import scene
from tracker_core import Options, Pipeline
from hardware import settings_signature
from workload import source_signature
from solver_quality import SOLVER_PROFILES
from task_queue import TaskQueue
from task_storage import restore_jobs
from task_progress import TaskProgress


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        configure('balanced')
        torch.set_num_threads(2)
        self.addCleanup(configure, 'balanced')

    def manager(self, **kwargs):
        self.now = 0.0
        return ConvergenceManager(warmup_steps=2, decay=0.0, convergence_window=5,
                                  clock=lambda: self.now, **kwargs)

    def test_constant_loss_and_zero_loss_both_stop(self):
        for value in (0.0, 0.00000244, 5.0):
            manager = self.manager()
            with redirect_stdout(io.StringIO()):
                for step in range(30):
                    _, stopped = manager.step(step, value)
                    if stopped:
                        break
            self.assertEqual(step, 6)
            self.assertEqual(manager.reason, 'plateau')

    def test_small_cumulative_improvement_uses_relative_scale(self):
        for scale in (1.0, 0.00000244):
            manager = self.manager(relative_tolerance=.01)
            with redirect_stdout(io.StringIO()):
                for step in range(30):
                    _, stopped = manager.step(step, scale * (1 - step * .00001))
                    if stopped:
                        break
            self.assertEqual(step, 6)
            self.assertEqual(manager.reason, 'plateau')
        # A real, cumulative gain above the threshold resets patience.
        manager = self.manager(relative_tolerance=.01)
        for step in range(20):
            self.assertFalse(manager.step(step, 1 - step * .004)[1])

    def test_iteration_budget_bounds_continually_improving_loss(self):
        manager = self.manager(max_steps=12)
        with redirect_stdout(io.StringIO()):
            for step in range(12):
                _, stopped = manager.step(step, 1/(step+1))
                self.assertEqual(stopped, step==11)
        self.assertEqual(manager.reason, 'iteration_limit')

    def test_clock_budget_and_shared_stage_deadline(self):
        manager = self.manager(max_seconds=2)
        self.assertFalse(manager.step(0, 5.0)[1])
        self.now = 2.01
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.step(1, 4.0)[1])
        self.assertEqual(manager.reason, 'time_limit')
        manager = self.manager(deadline=1)
        self.now = 1.01
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.step(0, 4.0)[1])
        self.assertEqual(manager.reason, 'stage_time_limit')

    def test_best_parameters_are_restored_with_matching_loss(self):
        parameter = torch.nn.Parameter(torch.tensor([3.0]))
        manager = self.manager(max_steps=4, parameters=[parameter])
        with redirect_stdout(io.StringIO()):
            for step, value in enumerate((3., 1., 2., 5.)):
                parameter.data.fill_(value)
                manager.step(step, value**2)
        manager.restore_best()
        self.assertEqual(parameter.item(), 1.0)
        self.assertEqual(manager.best_loss, 1.0)

    def test_nonfinite_loss_restores_valid_state_or_fails_if_none_exists(self):
        parameter = torch.nn.Parameter(torch.tensor([2.0]))
        manager = self.manager(parameters=[parameter])
        manager.step(0, 4.0)
        parameter.data.fill_(math.nan)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.step(1, math.nan)[1])
        manager.restore_best()
        self.assertEqual(parameter.item(), 2.0)
        self.assertEqual(manager.reason, 'nonfinite_loss')
        with self.assertRaises(FloatingPointError):
            self.manager(parameters=[parameter]).step(0, math.inf)

    def test_profiles_are_finite_and_ordered_and_reject_invalid_limits(self):
        values=[]
        for name in ('fast','balanced','high'):
            limits=configure(name)
            self.assertEqual({k: limits[k] for k in SOLVER_PROFILES[name]}, SOLVER_PROFILES[name])
            values.append(limits['max_iterations'])
            self.assertGreater(limits['epipolar_seconds'], limits['max_seconds'])
        self.assertEqual(values, sorted(values))
        for kwargs in ({'max_steps':0}, {'max_seconds':math.inf}, {'relative_tolerance':-1}):
            with self.assertRaises(ValueError): self.manager(**kwargs)

    def geometry(self):
        tracks, t = scene()
        pairs=point_pairs_from_tracks(tracks)
        R=torch.eye(3).repeat(5,1,1)
        image=Images(list(map(str,range(5))),torch.ones(5),torch.ones(5),torch.ones(5,dtype=torch.bool))
        camera=Cameras(torch.zeros(5,dtype=torch.long),torch.zeros(1),torch.zeros(1),torch.ones(1),torch.ones(1),torch.zeros(1),True)
        return pairs,R,t,image,camera

    def test_actual_epipolar_loop_honors_limit_and_returns_finite_geometry(self):
        pairs,R,t,images,camera=self.geometry()
        i,j,mapping=image_pair_index(pairs,5)
        limits=dict(policy(),max_iterations=5)
        stream=io.StringIO()
        with patch('fastmap.utils.policy',return_value=limits),redirect_stdout(stream):
            actual=epipolar.loop(R,t,torch.ones(1),i,j,torch.ones(len(i),dtype=torch.bool),pairs,mapping,
                                 torch.ones(pairs.num_point_pairs,dtype=torch.bool),camera.camera_idx,log_interval=1)
        self.assertIn('iteration_limit',stream.getvalue())
        self.assertTrue(all(torch.isfinite(x).all() for x in actual))
        torch.testing.assert_close(torch.det(actual[0]),torch.ones(5),atol=1e-5,rtol=1e-5)

    def test_full_epipolar_stage_cannot_restart_budget_in_next_irls_step(self):
        pairs,R,t,images,camera=self.geometry()
        now=[10.]
        calls=[]
        def optimized(**kwargs):
            calls.append(kwargs['deadline'])
            now[0]=kwargs['deadline']+1
            return kwargs['R_w2c'],kwargs['t_w2c'],kwargs['focal_scale']
        stream=io.StringIO()
        with patch('fastmap.epipolar.time.monotonic',side_effect=lambda: now[0]), \
             patch('fastmap.epipolar.loop',side_effect=optimized), \
             patch('fastmap.epipolar.timer',side_effect=lambda name:nullcontext()),redirect_stdout(stream):
            actual=epipolar.epipolar_adjustment(R,t,pairs,torch.ones(pairs.num_point_pairs,dtype=torch.bool),images,camera)
        self.assertEqual(len(calls),1)
        self.assertIn('stage_time_limit',stream.getvalue())
        self.assertTrue(all(torch.isfinite(x).all() for x in actual[:3]))


class SolverIntegrationTests(unittest.TestCase):
    def test_quality_changes_history_identity_but_reuses_frame_and_match_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            video=Path(directory)/'clip.mp4'; video.write_bytes(b'video')
            options=Options()
            high=replace(options,solver_quality='high')
            self.assertNotEqual(settings_signature(options),settings_signature(high))
            self.assertEqual(source_signature(video,options),source_signature(video,high))
            with self.assertRaises(ValueError): replace(options,solver_quality='unknown').validate()

    def test_optimizer_warning_is_live_deduplicated_and_does_not_fail_queue_or_export(self):
        events=[]; live=[]
        with tempfile.TemporaryDirectory() as directory:
            class Solver(Pipeline):
                def run(self,path,existing_scene=False):
                    self.log_dir=Path(directory)
                    script=("import sys,time\n"+f"sys.path.insert(0,{str(ROOT/'runtime/fastmap')!r})\n"
                        "from fastmap.utils import ConvergenceManager\n"
                        "m=ConvergenceManager(0,0,100,max_steps=3,stage='epipolar')\n"
                        "for i in range(3): m.step(i,1/(i+1))\n"
                        "time.sleep(.1)\n")
                    self._command([sys.executable,'-u','-c',script],'04-fastmap')
                    return dict(status='complete',project_dir=directory,model=directory,solver_warnings=list(self.warnings))
            tasks=None
            def receive(event):
                events.append(event)
                if event['type']=='solver_warning':
                    live.append(tasks.pipeline.process.poll() is None)
            tasks=TaskQueue(receive,Solver,lambda result:None)
            iid=tasks.add('clip.mp4',Options())
            tasks.jobs[iid]['blender']=dict(version='5.2')
            with patch('blender_export.export_blender',return_value=str(Path(directory)/'test.blend')) as exported:
                tasks.start()
                deadline=time.monotonic()+15
                while tasks.running and time.monotonic()<deadline: time.sleep(.01)
                self.assertFalse(tasks.running)
                self.assertEqual(tasks.snapshot()[iid]['state'],'完成')
                self.assertEqual(live,[True])
                exported.assert_called_once()
            notice=next(e for e in events if e['type']=='solver_warning')
            self.assertTrue(notice['log_path'].endswith('04-fastmap.log'))
            self.assertIn('继续',notice['text'])
            self.assertIn('optimization_limit_epipolar',tasks.snapshot()[iid]['solver_warnings'][0]['code'])
            tracker=TaskProgress(Options(),lambda *a,**k:None)
            tracker.begin('epipolar')
            tracker.observe('04-fastmap','fastmap.epipolar:loop:1 - [Iter 500] loss=1e-6')
            self.assertIn('500/15000',tracker.detail)

    def test_old_queue_defaults_to_balanced_and_selected_quality_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            tasks=TaskQueue(lambda event:None)
            old=tasks.add('old.mp4',Options()); new=tasks.add('new.mp4',Options(solver_quality='high'))
            values=list(tasks.snapshot().values()); values[0]['options'].pop('solver_quality')
            path=Path(directory)/'queue.json'; path.write_text(json.dumps(values),encoding='utf-8')
            restored,warnings=restore_jobs(path)
            self.assertFalse(warnings)
            self.assertEqual(restored[old]['options']['solver_quality'],'balanced')
            self.assertEqual(restored[new]['options']['solver_quality'],'high')

    def test_precision_preset_and_advanced_override_are_saved_and_visible(self):
        from PySide6.QtWidgets import QApplication
        from qt_app import MainWindow,configure_application
        from qt_dialogs import TaskDialog
        app=QApplication.instance() or QApplication([]); configure_application(app)
        with tempfile.TemporaryDirectory() as directory:
            window=MainWindow(config_dir=directory,background=False)
            dialog=TaskDialog(window,Options(),job=dict(path=directory,scene=True,name='预算验证'))
            dialog.debounce.stop()
            dialog.quality.setCurrentIndex(1)
            self.assertEqual(dialog.solver_quality.currentData(),'high')
            self.assertEqual(dialog.max_size.value(),4096)
            self.assertIn('30,000',dialog.quality_note.text())
            dialog.quality.setCurrentIndex(3)
            self.assertEqual(dialog.solver_quality.currentData(),'fast')
            self.assertEqual(dialog.overlap.value(),5)
            dialog.solver_quality.setCurrentIndex(dialog.solver_quality.findData('balanced'))
            with patch.object(Pipeline,'preflight'):
                dialog.accept_task()
            self.assertEqual(dialog.result_data['options'].solver_quality,'balanced')
            window.default_controls['solver_quality'].setCurrentIndex(0)
            window.save_settings_clicked()
            self.assertEqual(json.loads(window.settings_path.read_text(encoding='utf-8'))['solver_quality'],'high')
            window.pages.setCurrentIndex(1); window.show(); app.processEvents()
            artifacts=ROOT/'tests/artifacts/optimizer'; artifacts.mkdir(parents=True,exist_ok=True)
            window.grab().save(str(artifacts/'settings-quality.png'))
            window.close()
            dialog=TaskDialog(None,Options(solver_quality='high'))
            dialog.show(); app.processEvents()
            dialog.grab().save(str(artifacts/'task-quality.png'))
            dialog.close()


if __name__=='__main__': unittest.main()
