"""Solver log progress and live estimates; no CUDA imports or synthetic ticking."""
import json
import math
import re
import time
from solver_quality import SOLVER_PROFILES


FASTMAP_STAGES = [
    ('read', '读取匹配数据库'), ('distortion', '估计镜头畸变'),
    ('matrices', '优化基础矩阵与单应矩阵'), ('focal', '估计焦距'),
    ('calibrate', '标定匹配点'), ('pose', '分解相对相机姿态'),
    ('rotation', '全局旋转对齐'), ('tracks', '建立特征轨迹与点对'),
    ('relative_t', '估计相对平移'), ('translation', '全局位置对齐'),
    ('epipolar', '全局极线优化'), ('color', '等待点云取色'),
    ('sparse', '三角化与筛选点云'), ('write', '写入重建模型'),
]
GLOMAP_STAGES = [
    ('read', '读取匹配数据库'), ('preprocess', '预处理图像与匹配'),
    ('calibration', '视图图标定'), ('pose', '估计相对相机姿态'),
    ('rotation', '全局旋转平均'), ('tracks', '建立特征轨迹'),
    ('positioning', '全局位置求解'), ('ba', '第一轮全局 BA 优化'),
    ('triangulate', '重新三角化点云'), ('refine', '优化三角化结果'),
    ('final_ba', '最终全局 BA 优化'), ('write', '写入重建模型'),
]
FASTMAP_TIMERS = dict(zip(
    ('Read COLMAP Database', 'Distortion', 'Re-Estimate Matrices', 'Focal Length',
     'Calibrate Matches', 'Relative Pose Decomposition', 'Global Rotation Alignment',
     'Build Tracks', 'Re-Estimate Relative Translation', 'Global Translation Alignment',
     'Global Epipolar Optimization', 'Wait for Color Reading', 'Sparse Reconstruction', 'Write Results'),
    (key for key, _ in FASTMAP_STAGES)))


def stage_plan(options, blender=False):
    stages = [('frames', '抽取视频帧'), ('features', '提取图像特征'), ('matching', '匹配相邻图像')]
    solver = list(FASTMAP_STAGES if options.backend == 'fastmap' else GLOMAP_STAGES)
    if options.backend == 'fastmap':
        if options.camera_mode in ('pinhole', 'calibrated'):
            solver = [(key, title) for key, title in solver if key != 'distortion']
        if options.camera_mode == 'pinhole':
            solver = [(key, title) for key, title in solver if key != 'matrices']
        if options.camera_mode == 'calibrated':
            solver = [(key, title) for key, title in solver if key != 'focal']
        if not options.color:
            solver = [(key, title) for key, title in solver if key != 'color']
    stages += solver + [('export_txt', '导出相机与点云模型'), ('export_ply', '导出 PLY 点云'),
                        ('analyze', '检查重建结果')]
    if options.export_sequence:
        stages.append(('sequence', '导出视频序列帧'))
    stages.append(('publish', '整理项目与保存结果'))
    if blender:
        stages.append(('blender', '生成 Blender 工程'))
    return stages


def clock_text(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


class TaskProgress:
    def __init__(self, options, callback, clock=time.monotonic, blender=False):
        self.options, self.callback, self.clock = options, callback, clock
        self.plan = stage_plan(options, blender)
        self.positions = {key: i for i, (key, _) in enumerate(self.plan)}
        self.key = None
        self.started = self.phase_started = clock()
        self.last_sent = -math.inf
        self.last_log = self.started
        self.current = self.total = None
        self.detail = ''
        self.durations = {}
        self.estimate = None
        self.frames = None
        self.ba_round = 0
        self.rate_kind = None
        self.counter_started = self.started
        self.sampled_at = self.started
        self.overall = 0.0

    def begin(self, key, detail=None, current=None, total=None, rate_kind=None, force=False):
        if key not in self.positions:
            return
        # Nested timers and late stderr lines cannot rewind the overall flow.
        if self.key and self.positions[key] < self.positions[self.key]:
            return
        now = self.clock()
        changed = key != self.key
        if changed:
            if self.key:
                self.durations[self.key] = self.durations.get(self.key, 0) + now - self.phase_started
            self.key, self.phase_started = key, now
            self.current = self.total = None
            self.rate_kind = None
            self.detail = ''
            self.counter_started = now
        if detail is not None:
            self.detail = detail
        if current is not None and total and total > 0:
            if self.rate_kind is not None and (rate_kind != self.rate_kind or total != self.total
                                               or current < self.current):
                self.counter_started = now
            if self.rate_kind == rate_kind and self.total == total and current < self.current:
                current = self.current
            self.current, self.total = max(0, min(current, total)), total
            self.rate_kind = rate_kind
            self.sampled_at = now
        if changed or force or current == total and total or now - self.last_sent >= .2:
            self.last_sent = now
            fraction = self.current / self.total if self.total else None
            position = self.positions[key]
            self.overall = max(self.overall, (position + (fraction or 0)) / len(self.plan))
            self.callback('task_progress', phase=key, text=dict(self.plan)[key], detail=self.detail,
                          step=position + 1, steps=len(self.plan), fraction=fraction,
                          current=self.current, total=self.total, rate_kind=self.rate_kind,
                          phase_started=self.phase_started, task_started=self.started,
                          counter_started=self.counter_started, sampled_at=self.sampled_at,
                          last_log=self.last_log, estimate=self.estimate,
                          overall=min(.999, self.overall))

    def coarse(self, index, text):
        if index == 1:
            self.begin('frames')
        elif index == 2:
            self.begin('features')
        elif index == 3:
            self.begin('matching')
        elif index == 4:
            self.begin('read', '启动求解器')
        elif '序列帧' in text:
            self.begin('sequence')
        elif text.startswith('生成 Blender'):
            self.begin('blender')
        elif index == 5:
            self.begin('export_txt')

    def command(self, label):
        key = {'01-frames': 'frames', '01-frames-cpu': 'frames', '02-features': 'features',
               '03-matching': 'matching', '04-fastmap': 'read', '04-glomap': 'read',
               '05-export-model': 'export_txt', '05-export-ply': 'export_ply',
               '05-model-analysis': 'analyze', '06-video-sequence': 'sequence'}.get(label)
        if key and key != self.key:
            self.begin(key)

    def observe(self, label, line):
        self.last_log = self.clock()
        if line.startswith('[复用]') and self.key in ('frames', 'features', 'matching'):
            self.begin(self.key, '使用已完成缓存', 1, 1, force=True)
        if label in ('01-frames', '01-frames-cpu', '06-video-sequence'):
            match = re.search(r'frame=\s*(\d+)', line)
            if match:
                current = int(match[1])
                self.begin('sequence' if label == '06-video-sequence' else 'frames',
                           f'已输出 {current} 帧', current, self.frames, 'frames')
        elif label in ('02-features', '03-matching'):
            match = re.search(r'(?:Processed file|Matching image)\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]', line)
            if match:
                self.begin('features' if label == '02-features' else 'matching',
                           f'{match[1]} / {match[2]} 张图像', int(match[1]), int(match[2]), 'images')
        elif label == '04-fastmap':
            self._fastmap(line)
        elif label == '04-glomap':
            self._glomap(line)

    def _fastmap(self, line):
        if line.startswith('[POINTSTUDIO_OPTIMIZATION] '):
            try:
                data = json.loads(line.split(' ', 1)[1])
                reasons = {'plateau': '改善已小，结束本步优化', 'iteration_limit': '触及迭代上限，使用最佳参数',
                           'time_limit': '触及单步时间预算，使用最佳参数',
                           'stage_time_limit': '触及阶段时间预算，继续后续处理',
                           'nonfinite_loss': '数值异常，恢复最佳有效参数'}
                if self.key and data.get('reason') in reasons:
                    self.begin(self.key, reasons[data['reason']], force=True)
            except (ValueError, TypeError, KeyError):
                pass
            return
        if line.startswith('[POINTSTUDIO_STAGE] '):
            try:
                data = json.loads(line.split(' ', 1)[1])
                key = FASTMAP_TIMERS.get(data.get('name'))
                if key:
                    self.begin(key)
                elif data.get('name') == 'Sparse batch':
                    self.begin('sparse', f"{data['current']} / {data['total']} 个点对",
                               int(data['current']), int(data['total']), 'point_pairs')
            except (ValueError, TypeError, KeyError):
                pass
            return
        # These module tags also support logs from older bundled workers.
        modules = {'database': 'read', 'distortion': 'distortion', 'fundamental': 'matrices',
                   'homography': 'matrices', 'focal': 'focal', 'decompose': 'pose',
                   'rotation': 'rotation', 'track': 'tracks', 'point_pair': 'tracks',
                   'rel_t': 'relative_t', 'translation': 'translation',
                   'epipolar': 'epipolar', 'sparse': 'sparse'}
        tag = re.search(r'fastmap\.(\w+):', line)
        key = modules.get(tag[1]) if tag else None
        if key and key != self.key:
            self.begin(key)
        match = re.search(r'\[Round\s+(\d+)\s*/\s*(\d+)\].*IRLS step\s+(\d+)\s*/\s*(\d+)', line)
        if match:
            round_, rounds, step, steps = map(int, match.groups())
            self.begin('epipolar', f'第 {round_}/{rounds} 轮 · IRLS {step}/{steps}',
                       (round_ - 1) * steps + step - 1, rounds * steps, 'irls', force=True)
        match = re.search(r'Current level theta_max:\s*([\d.]+)', line)
        if match:
            theta = float(match[1])
            if theta <= 0:
                return
            level = max(0, min(6, round(math.log2(90 / theta))))
            self.begin('relative_t', f'平移投票第 {level + 1}/7 层', level, 7, 'voting', force=True)
        match = re.search(r'\[Iter\s+(\d+)', line)
        if match and key == self.key:
            maximum = SOLVER_PROFILES[self.options.solver_quality]['max_iterations']
            self.begin(self.key, f'{self.detail.split(" · 迭代")[0]} · 迭代 {match[1]}/{maximum}（上限，可提前结束）'.strip(' ·'))

    def _glomap(self, line):
        headers = {'preprocessing': 'preprocess', 'view graph calibration': 'calibration',
                   'relative pose estimation': 'pose', 'rotation averaging': 'rotation',
                   'track establishment': 'tracks', 'global positioning': 'positioning',
                   'retriangulation': 'triangulate'}
        match = re.search(r'Running (.*?)\s*\.\.\.', line)
        if match:
            name = match[1].strip().lower()
            if name == 'bundle adjustment':
                self.ba_round += 1
                self.begin('ba' if self.ba_round == 1 else 'final_ba', '等待优化器收敛')
            elif name in headers:
                self.begin(headers[name])
        match = re.search(r'(Loading Images?|Loading Image Pair|Triangulating image)\s+(\d+)\s*/\s*(\d+)', line)
        if match:
            key = 'triangulate' if match[1] == 'Triangulating image' else 'read'
            kind = 'image_pairs' if 'Pair' in match[1] else 'images'
            self.begin(key, f'{match[2]} / {match[3]}' + (' 个图像对' if kind == 'image_pairs' else ' 张图像'),
                       int(match[2]), int(match[3]), kind)
        match = re.search(r'Global bundle adjustment iteration\s+(\d+)\s*/\s*(\d+)(.*)', line)
        if match:
            current, total, suffix = int(match[1]), int(match[2]), match[3]
            stage = re.search(r'stage\s+(\d+)\s+finished', suffix)
            if stage:
                completed = (current - 1) * 2 + min(2, int(stage[1]))
                self.begin('ba', f'第 {current}/{total} 轮 · 子步骤 {stage[1]}/2 完成',
                           completed, total * 2, 'ba_steps')
            elif self.key in ('triangulate', 'refine'):
                # These lines announce iteration start, not completion.
                self.begin('refine', f'第 {current}/{total} 轮局部 BA', current - 1, total, 'ba_rounds')
        if 'Ceres Solver Report:' in line and self.key:
            match = re.search(r'Iterations:\s*(\d+).*Termination:\s*(\w+)', line)
            if match:
                self.begin(self.key, f'本次优化 {match[1]} 次迭代 · {match[2]}', force=True)
        if 'Reconstruction done in' in line:
            self.begin('write', '保存相机、轨迹与点云')

    def timings(self):
        timings = dict(self.durations)
        if self.key:
            timings[self.key] = timings.get(self.key, 0) + self.clock() - self.phase_started
        return timings


def live_remaining(event, now=None, last_log=None):
    """Update only elapsed time; observed counters/history drive all estimates."""
    now = time.monotonic() if now is None else now
    elapsed = max(0, now - event['phase_started'])
    quiet = max(0, now - (last_log if last_log is not None else event.get('last_log', now)))
    notes = [f'本阶段已用 {clock_text(elapsed)}']
    if quiet >= 15:
        notes.append(f'{clock_text(quiet)} 未收到新日志（等待求解器输出）')
    estimate = event.get('estimate')
    if estimate and event.get('phase') != 'blender':
        spent = max(0, now - event['task_started'])
        lower, upper = estimate['lower'] - spent, estimate['upper'] - spent
        if upper > 0:
            notes.append(f'本任务剩余约 {clock_text(max(0, lower))}–{clock_text(upper)}（本机历史粗估，不含 Blender）')
        else:
            notes.append('已超出原估时，正在等待新进度')
    else:
        current, total = event.get('current'), event.get('total')
        sampled_at = event.get('sampled_at', now)
        measured = max(0, sampled_at - event.get('counter_started', event['phase_started']))
        if current and total and 0 < current < total and measured >= 2 and now - sampled_at < 30:
            remaining = measured / current * (total - current)
            since = max(0, now - sampled_at)
            if remaining * 1.5 > since:
                notes.append(f'本阶段剩余约 {clock_text(max(0, remaining * .7 - since))}–{clock_text(remaining * 1.5 - since)}（按当前速度）')
            else:
                notes.append('剩余时间：等待下一次进度更新')
        elif current == total and total:
            notes.append('计数已完成，等待本阶段收尾')
        else:
            notes.append('剩余时间：等待可用进度或本机参考记录')
    return ' · '.join(notes)
