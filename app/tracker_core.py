"""Shared reconstruction pipeline for the desktop interface and CLI tests."""
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime
from pathlib import Path
import json
import math
import os
import re
import sqlite3
import shutil
import subprocess
import threading
import time
import uuid

from paths import (ROOT, APP, FASTMAP, PYTHON, OUTPUTS, BINARY_DIRS,
                   DEV_COLMAP, DEV_COLMAP_CUDA, RUNTIME_COLMAP, RUNTIME_COLMAP_CUDA,
                   DEV_GLOMAP, DEV_GLOMAP_CUDA, RUNTIME_GLOMAP, RUNTIME_GLOMAP_CUDA,
                   DEV_LEGACY_COLMAP, RUNTIME_LEGACY_COLMAP,
                   use_dev_cuda_colmap, runtime_environment, python_runtime_path)
from hardware import get_hardware, describe_hardware, settings_signature
from workload import source_signature, video_scene, describe_workload, image_sort_key, video_info
from native_paths import NativePaths
from project_layout import create_project, cache_directory, result_report, safe_name
from json_store import atomic_write_json
from task_progress import TaskProgress
from zoom_focal import prepare_auto_focal_database
from solver_quality import SOLVER_PROFILES, OPTIMIZATION_REVISION
from app_info import DEFAULT_SOLVER
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm', '.mts', '.m2ts', '.wmv', '.mpg', '.mpeg'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp', '.exr'}
HIDE_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class Cancelled(Exception):
    pass


@dataclass
class Options:
    backend: str = DEFAULT_SOLVER
    output_root: str = str(OUTPUTS)
    python: str = str(PYTHON)
    fps: float = 0
    max_size: int = 4096
    overlap: int = 15
    cuda_decode: bool = True
    color: bool = True
    camera_mode: str = 'radial'
    gpu_min_images: int = 50
    glomap_gpu: bool = False
    image_scale: float = 1.0
    # User supplied starting focal length in original image pixels. Zero keeps
    # COLMAP's automatic focal-length initialization.
    initial_focal_length: float = 0.0
    export_sequence: bool = False
    sequence_original: bool = False
    solver_quality: str = 'balanced'
    gpu_index: int = 0
    use_gpu: bool = True
    input_type: str = 'video'
    # Explicit opt-in for continuous focal-length changes.  A hard cut alone
    # stays on the legacy GLOMAP path; the two fields below remain only as
    # compatibility aliases for 1.x task JSON files.
    smart_lens: bool = False
    auto_focal: bool = False
    contains_cuts: bool = False
    rotation_only: bool = False

    def validate(self):
        for key in ('fps', 'image_scale', 'initial_focal_length'):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'{key} 必须是有限数值')
        for key in ('max_size', 'overlap', 'gpu_min_images', 'gpu_index'):
            if type(getattr(self, key)) is not int:
                raise ValueError(f'{key} 必须是整数')
        for key in ('cuda_decode', 'color', 'glomap_gpu', 'export_sequence', 'sequence_original', 'smart_lens', 'auto_focal', 'contains_cuts', 'rotation_only', 'use_gpu'):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f'{key} 必须是布尔值')
        for key in ('python', 'output_root'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f'{key} 路径不能为空')
        # ``fastmap`` is retained only as a migration alias.  New tasks can
        # select VGGT-Omega or standalone legacy GLOMAP. Explicit COLMAP
        # options remain readable for historical tasks and archived research.
        if self.backend == 'fastmap':
            self.backend = 'glomap'
            self.rotation_only = False
        if self.smart_lens and self.backend == 'colmap':
            self.backend = 'glomap'
        if self.backend == 'vggt-omega':
            # Neural reconstruction estimates per-frame intrinsics itself.
            self.smart_lens = self.auto_focal = self.contains_cuts = False
            self.initial_focal_length = 0.0
        if self.backend not in ('colmap', 'glomap', 'vggt-omega') or self.camera_mode not in ('radial', 'pinhole', 'calibrated'):
            raise ValueError('未知求解器或相机模式')
        if self.input_type not in ('video', 'image_sequence'):
            raise ValueError('未知输入类型')
        if self.input_type == 'image_sequence' and self.export_sequence:
            raise ValueError('图像序列输入不需要再次导出序列帧。')
        if self.rotation_only:
            raise ValueError('低视差仅旋转保护已随 FastMap 移除，请使用普通重建。')
        if not isinstance(self.solver_quality, str) or self.solver_quality not in SOLVER_PROFILES:
            raise ValueError('未知计算精度')
        if not 0 <= self.fps <= 240 or not 256 <= self.max_size <= 16384 or not 1 <= self.overlap <= 200:
            raise ValueError('抽帧率、图像尺寸或匹配范围超出允许范围')
        if not 2 <= self.gpu_min_images <= 1000000:
            raise ValueError('GPU 最少图像数需要不小于 2')
        if self.gpu_index < 0:
            raise ValueError('GPU 编号不能小于 0')
        if not 0.1 <= self.image_scale <= 1:
            raise ValueError('处理分辨率比例需要在 0.1 到 1 之间')
        if self.backend == 'vggt-omega':
            # Decode source-size frames once. The neural inference profile
            # owns model resizing; a saved GLOMAP scale must not downsample
            # the source again before VGGT sees it.
            self.image_scale = 1.0
        if self.initial_focal_length < 0:
            raise ValueError('初始焦距不能小于 0')
        return self


@dataclass
class SolverResult:
    model: Path
    backend: str
    wall_seconds: float
    database: dict
    metrics: dict
    gpu_active: bool
    rotation_only: bool = False


def find_binary(folder, name, use_cuda=None):
    if use_cuda is None:
        use_cuda = use_dev_cuda_colmap()
    if folder == '01 GLOMAP' and name == 'colmap.exe' and use_cuda:
        for path in (RUNTIME_COLMAP_CUDA, DEV_COLMAP_CUDA):
            if path.is_file():
                return path
    for directory in (BINARY_DIRS[folder], BINARY_DIRS[folder] / 'bin'):
        path = directory / name
        if path.is_file():
            return path
    if folder == '01 GLOMAP' and name == 'colmap.exe':
        for path in ((DEV_COLMAP_CUDA, RUNTIME_COLMAP_CUDA) if use_cuda else
                     (DEV_COLMAP, RUNTIME_COLMAP)):
            if path.is_file():
                return path
    if folder in ('02 GLOMAP', '02 GLOMAP GPU') and name == 'glomap.exe':
        candidates = ((RUNTIME_GLOMAP_CUDA, DEV_GLOMAP_CUDA) if use_cuda else
                      (RUNTIME_GLOMAP, DEV_GLOMAP))
        for path in candidates:
            if path.is_file():
                return path
    raise FileNotFoundError(f'找不到 {folder}/{name}')


def find_legacy_colmap():
    """Find the COLMAP 3.12 CLI paired with the standalone GLOMAP build."""
    for path in (RUNTIME_LEGACY_COLMAP, DEV_LEGACY_COLMAP):
        if path.is_file():
            return path
    raise FileNotFoundError(
        '找不到 GLOMAP 配套的旧版 COLMAP；请在 runtime/glomap-cpu 中放置 colmap.exe 及其运行库。')


def model_complete(model):
    return all((model / n).is_file() and (model / n).stat().st_size > 0
               for n in ('cameras.bin', 'images.bin', 'points3D.bin'))


def resolve_model_directory(root):
    """Locate a COLMAP/GLOMAP model below an output directory.

    GLOMAP versions differ in whether ``--output_path`` receives the model
    files directly or creates a numbered reconstruction directory (usually
    ``0``).  Keep the solver output untouched and normalize both layouts for
    the rest of the export pipeline.
    """
    root = Path(root)
    if model_complete(root):
        return root
    if not root.is_dir():
        return root
    candidates = sorted((child for child in root.iterdir() if child.is_dir()),
                        key=lambda child: (child.name != '0', child.name.casefold()))
    for candidate in candidates:
        if model_complete(candidate):
            return candidate
    return root


def model_has_points(model):
    """Return whether the binary model contains at least one 3D point."""
    points = model / 'points3D.bin'
    # COLMAP writes an eight-byte point count even when the set is empty.
    return model_complete(model) and points.stat().st_size > 8


def database_counts(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return {'images': db.execute('SELECT COUNT(*) FROM images').fetchone()[0],
                'pairs': db.execute('SELECT COUNT(*) FROM two_view_geometries WHERE rows>0').fetchone()[0]}


def database_focal_prior_ratio(database):
    """Return the fraction of cameras with a reliable focal-length prior.

    COLMAP 4.x stores the calibration origin in ``cameras.source`` rather
    than the old ``prior_focal_length`` column.  A value above GUESS (2) is a
    trusted prior.  Supporting both schemas keeps old task caches readable.
    """
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(cameras)')}
            total = db.execute('SELECT COUNT(*) FROM cameras').fetchone()[0]
            if total <= 0:
                return 0.0
            if 'source' in columns:
                trusted = db.execute(
                    'SELECT COUNT(*) FROM cameras WHERE source > 2').fetchone()[0]
            elif 'prior_focal_length' in columns:
                trusted = db.execute(
                    'SELECT COUNT(*) FROM cameras WHERE prior_focal_length != 0').fetchone()[0]
            else:
                return 0.0
            return float(trusted) / float(total)
    except (OSError, sqlite3.Error):
        return 0.0


class Pipeline:
    def __init__(self, options, callback=None):
        self.options = options
        self.callback = callback or (lambda event: None)
        self.cancelled = threading.Event()
        self.process = None
        self.lock = threading.Lock()
        self.log_dir = None
        self.project_name = None
        self.project_dir = None
        self.reuse_failed_projects = False
        self._pending_logs = []
        self.warnings = []
        self.workload = None
        self.progress = TaskProgress(options, self.emit)
        self.cuda_enabled = bool(use_dev_cuda_colmap() and options.use_gpu)

    def warn_solver(self, code, text):
        if any(item['code'] == code for item in self.warnings):
            return
        filename = '04-glomap.log' if (self.options.smart_lens or self.options.backend == 'glomap') else '04-colmap.log'
        if self.options.backend == 'vggt-omega':
            filename = '04-vggt-omega.log'
        warning = dict(code=code, text=text, log_path=str(self.log_dir / filename))
        self.warnings.append(warning)
        self.emit('log', text=f"[求解提醒] {text} 日志：{warning['log_path']}\n")
        self.emit('solver_warning', **warning)

    def observe_glomap_output(self, line):
        if self.options.use_gpu and 'falling back' in line.casefold():
            self.progress.estimate = None
            self.progress.phase_estimates = {}
            if self.progress.key:
                self.progress.begin(self.progress.key, force=True)
            self.warn_solver('cpu_fallback', 'GLOMap 已回退到 CPU，任务将继续求解和导出，处理速度可能降低。')

    def observe_colmap_output(self, line):
        lowered = line.casefold()
        if ('compiled without cuda support' in lowered
                or 'falling back to cpu-based' in lowered
                or 'falling back to cpu based' in lowered):
            self.warn_solver(
                'colmap_cpu_fallback',
                '当前 COLMAP 的 Ceres 未编译 CUDA/cuDSS，Bundle Adjustment 已回退到 CPU；任务会继续，但显卡不会参与该阶段。',
            )

    def observe_fastmap_output(self, line):
        if '[LOW_PARALLAX]' in line:
            try:
                payload = json.loads(line.split('[LOW_PARALLAX]', 1)[1].strip())
                if payload.get('low'):
                    self.warn_solver(
                        'low_parallax',
                        '检测到低视差/近似纯旋转运动；普通模式的点云深度可能不稳定，'
                        '仅旋转模式会只输出相机旋转轨迹。',
                    )
            except (ValueError, TypeError, AttributeError):
                pass
        if not line.startswith('[POINTSTUDIO_OPTIMIZATION] '):
            return
        try:
            event = json.loads(line.split(' ', 1)[1])
            stage, reason = event['stage'], event['reason']
            stages = {'rotation': '旋转对齐', 'translation': '位置对齐', 'distortion': '畸变优化', 'epipolar': '极线优化'}
            reasons = {'iteration_limit': '迭代上限', 'time_limit': '单步时间预算',
                       'stage_time_limit': '阶段总时间预算', 'nonfinite_loss': '数值异常保护'}
            if stage in stages and reason in reasons:
                self.warn_solver('optimization_limit_' + stage,
                    f'FastMap {stages[stage]}触及{reasons[reason]}，已保留本步最佳有效参数，继续后续处理；请检查生成轨迹质量。')
        except (ValueError, TypeError, KeyError):
            return

    def verify_glomap_gpu(self):
        mapping_log = (self.log_dir / '04-glomap.log').read_text(encoding='utf-8')
        self.observe_glomap_output(mapping_log)
        if any(item['code'] == 'cpu_fallback' for item in self.warnings):
            return False
        active = all(message in mapping_log for message in (
            '[GPU] Global positioning: CUDA_SPARSE', '[GPU] Bundle adjustment: CUDA_SPARSE'))
        if not active:
            self.warn_solver('gpu_incomplete',
                f'GLOMap 未确认 GP / BA 均启用 GPU，可能有阶段使用 CPU（GPU 最少图像数：{self.options.gpu_min_images}）。请查看求解日志。')
        return active

    def emit(self, kind, **data):
        if kind == 'stage':
            # Emit the legacy event first so the richer progress event wins in UI.
            self.callback({'type': kind, **data})
            self.progress.coarse(data['index'], data['text'])
            return
        if kind == 'log':
            self._pending_logs.append(data['text'])
        if self.log_dir and self._pending_logs:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / 'task.log').open('a', encoding='utf-8') as stream:
                stream.writelines(self._pending_logs)
            self._pending_logs.clear()
        self.callback({'type': kind, **data})

    def new_project(self, source, signature=None):
        """Resume this job, or an unfinished matching project when enabled."""
        project = Path(self.project_dir) if self.project_dir else None
        manifest = project / '_cache/source.json' if project else None
        reusable = False
        requested_name = safe_name(self.project_name or Path(source).stem)
        same_destination = (project is not None and project.parent.resolve() == Path(self.options.output_root).resolve()
                            and re.fullmatch(re.escape(requested_name) + r'(?: \(\d+\))?', project.name))
        if same_destination and manifest.is_file() and not (project / '_cache/result.json').exists() and not (project / 'point could').exists():
            try:
                reusable = json.loads(manifest.read_text(encoding='utf-8')) == signature
            except (OSError, ValueError):
                pass
        if not reusable and self.reuse_failed_projects and signature is not None:
            root = Path(self.options.output_root).resolve()
            candidates = []
            if same_destination:
                candidates.append(project)
            if root.is_dir():
                candidates.extend(sorted((p for p in root.iterdir()
                    if re.fullmatch(re.escape(requested_name) + r'(?: \(\d+\))?', p.name)),
                    key=lambda p: p.stat().st_mtime_ns, reverse=True))
            for candidate in dict.fromkeys(candidates):
                cache = candidate / '_cache'
                # Successful deliverables and linked directories never qualify.
                if (not candidate.is_dir() or candidate.is_symlink() or cache.is_symlink()
                        or (cache / 'result.json').exists() or (candidate / 'point could').exists()
                        or any(candidate.glob('*.blend'))):
                    continue
                try:
                    previous = json.loads((cache / 'source.json').read_text(encoding='utf-8'))
                    same_source = (Path(previous['source']).resolve() == Path(signature['source']).resolve()
                                   and all(previous.get(key) == signature.get(key) for key in ('size', 'mtime_ns')))
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                if not same_source:
                    continue
                if previous != signature:
                    self.preserve_retry_cache(candidate)
                project, reusable = candidate, True
                self.emit('log', text=f'[重试] 复用未完成项目目录：{project}\n')
                break
        if not reusable:
            project = create_project(self.options.output_root, self.project_name or Path(source).stem)
        self.project_dir = project
        self.project_reused = reusable
        self.log_dir = project / 'log'
        self.log_dir.mkdir(exist_ok=True)
        self.emit('scene', path=str(project))
        return project

    def preserve_retry_cache(self, project):
        """Retain incompatible generated data inside the project's private cache."""
        cache = project / '_cache'
        destination = cache / 'retry_history' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
        destination.mkdir(parents=True)
        for name in ('source.json', 'images', 'database.db', 'database.db-wal', 'database.db-shm',
                     # Legacy 1.x focal-cache artifacts are never read now,
                     # but move them aside on retry so stale files cannot be
                     # mistaken for the native COLMAP database.
                     'database-auto-focal.db', 'auto-focal.json',
                     '.frames-complete', '.features-complete', '.matching-complete'):
            path = cache / name
            if path.exists() or path.is_symlink():
                path.rename(destination / name)
        # A partial sequence from the previous attempt must not leave extra frames.
        if (project / 'video').exists():
            (project / 'video').rename(destination / 'video')
        self.emit('log', text='[重试] 处理参数已变化，旧缓存已保存在 _cache/retry_history；本次重新抽帧与匹配。\n')

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled('任务已停止；已完成的图像和数据库保留。')

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            child = self.process
            if child is not None and child.poll() is None:
                if os.name == 'nt':
                    try:
                        result = subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                creationflags=HIDE_WINDOW, timeout=20)
                        if result.returncode and child.poll() is None:
                            self.kill_children(child)
                    except (OSError, subprocess.TimeoutExpired):
                        if child.poll() is None:
                            self.kill_children(child)
                else:
                    child.terminate()

    @staticmethod
    def kill_children(child):
        # Some restricted Windows sessions deny taskkill but allow process handles.
        try:
            import psutil
        except ImportError:
            psutil = None
        try:
            if psutil is None:
                return
            descendants = psutil.Process(child.pid).children(recursive=True)
            for process in reversed(descendants):
                try:
                    process.kill()
                except psutil.Error:
                    pass
        except Exception:
            pass
        finally:
            if child.poll() is None:
                child.kill()

    def command(self, args, label, cwd=None, env=None):
        if Path(args[0]).name.lower() in ('colmap.exe', 'glomap.exe', 'gpu_solver_probe.exe'):
            with NativePaths() as native:
                native_env = os.environ.copy()
                native_env.update(runtime_environment(cuda=self.cuda_enabled))
                native_env.update(env or {})
                mapped, native_cwd, native_env = native.command(args, cwd or ROOT, native_env)
                if mapped != [str(arg) for arg in args]:
                    self.emit('log', text='[路径兼容] 原生工具通过英文目录别名访问项目；原文件和输出位置不变。\n')
                return self._command(mapped, label, native_cwd, native_env)
        return self._command(args, label, cwd, env)

    def _command(self, args, label, cwd=None, env=None):
        self.check_cancel()
        self.progress.command(label)
        args = [str(a) for a in args]
        log_path = self.log_dir / f'{label}.log'
        if log_path.exists():
            log_path.rename(log_path.with_stem(log_path.stem + '_' + uuid.uuid4().hex[:6]))
        self.emit('log', text=f'\n[{label}] {subprocess.list2cmdline(args)}\n')
        process_env = os.environ.copy()
        process_env.update(runtime_environment(cuda=self.cuda_enabled))
        process_env.update({'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8',
                            'OMP_NUM_THREADS': '4', 'OPENBLAS_NUM_THREADS': '4', 'NO_COLOR': '1',
                            'LOGURU_COLORIZE': 'False'})
        if env:
            process_env.update(env)
        started = time.perf_counter()
        worker_error = None
        with log_path.open('w', encoding='utf-8', buffering=1) as log:
            log.write(subprocess.list2cmdline(args) + '\n')
            with self.lock:
                self.check_cancel()
                child = subprocess.Popen(args, cwd=str(cwd or ROOT), env=process_env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                                         errors='replace', bufsize=1, creationflags=HIDE_WINDOW)
                self.process = child
            try:
                for line in child.stdout:
                    line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
                    log.write(line)
                    self.emit('log', text=line)
                    self.progress.observe(label, line)
                    if label == '04-vggt-omega' and line.startswith('[VGGT_ERROR] '):
                        worker_error = line[len('[VGGT_ERROR] '):].strip()
                    if label == '04-glomap':
                        self.observe_glomap_output(line)
                    elif label.startswith('04-colmap'):
                        self.observe_colmap_output(line)
                    elif label == '04-fastmap':
                        self.observe_fastmap_output(line)
                exit_code = child.wait()
            finally:
                try:
                    if child.poll() is None:
                        self.kill_children(child)
                        child.wait(timeout=10)
                finally:
                    child.stdout.close()
                    with self.lock:
                        self.process = None
            self.check_cancel()
            if exit_code:
                if worker_error:
                    raise RuntimeError(f'{worker_error} 日志：{log_path}')
                if label == '04-fastmap':
                    with log_path.open(encoding='utf-8') as stream:
                        cuda_oom = any('CUDA out of memory' in line for line in stream)
                    if cuda_oom:
                        message = ('FastMap 显存不足。请关闭其他占用显存的程序，或在高级参数降低抽帧 FPS、SIFT 最大尺寸和相邻匹配范围；'
                                   f'也可切换 GLOMAP CUDA/cuDSS。日志：{log_path}')
                        self.emit('log', text=f'\n[显存不足] {message}\n')
                        raise RuntimeError(message)
                raise RuntimeError(f'{label} 失败，退出码 {exit_code}。日志：{log_path}')
        return time.perf_counter() - started

    def preflight(self):
        o = self.options
        o.validate()
        if o.backend == 'vggt-omega':
            from vggt_runtime import locate_assets
            locate_assets()
            o.python = str(python_runtime_path(o.python))
            if not Path(o.python).is_file():
                raise FileNotFoundError(f'找不到 VGGT 推理使用的 Python 运行库：{o.python}')
        elif o.smart_lens or o.backend == 'glomap':
            # Continuous zoom deliberately stays on the old GLOMAP toolchain.
            find_legacy_colmap()
            find_binary('02 GLOMAP GPU' if self.cuda_enabled else '02 GLOMAP',
                        'glomap.exe', use_cuda=self.cuda_enabled)
        else:
            find_binary('01 GLOMAP', 'colmap.exe', use_cuda=self.cuda_enabled)

    def prepare_video(self, video):
        o = self.options
        video = Path(video).resolve()
        if not video.is_file() or video.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f'不支持的视频：{video}')
        signature = source_signature(video, o)
        scene = self.new_project(video, signature)
        if self.project_reused and self.workload is not None:
            try:
                self.workload = describe_workload(video, o, False, scene)
            except Exception:
                pass
        cache = cache_directory(scene)
        manifest = cache / 'source.json'
        if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != signature:
            raise RuntimeError(f'输入设置与已有场景不一致：{scene}')
        manifest.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding='utf-8')
        images = cache / 'images'
        images.mkdir(exist_ok=True)
        self.emit('stage', index=1, total=5, text='抽取视频帧')
        marker = cache / '.frames-complete'
        if marker.exists() and any(images.glob('frame_*.jpg')):
            self.emit('log', text='[复用] 视频帧已完成。\n')
        else:
            args = [find_binary('03 FFMPEG', 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-stats', '-y']
            if self.cuda_enabled and o.cuda_decode:
                args += ['-hwaccel', 'cuda', '-hwaccel_device', str(o.gpu_index)]
            args += ['-i', video, '-map', '0:v:0', '-an']
            filters = video_filters(o)
            if filters:
                args += ['-vf', filters]
            args += ['-qscale:v', '2', images / 'frame_%06d.jpg']
            try:
                self.command(args, '01-frames')
            except RuntimeError:
                if not (self.cuda_enabled and o.cuda_decode):
                    raise
                self.emit('log', text='[提示] CUDA 解码失败，改用 CPU 解码；求解设置不变。\n')
                index = args.index('-hwaccel')
                del args[index:index + 4]
                self.command(args, '01-frames-cpu')
            count = sum(1 for _ in images.glob('frame_*.jpg'))
            if count < 3:
                raise RuntimeError(f'仅生成 {count} 帧，需要至少 3 帧')
            marker.write_text(str(count), encoding='utf-8')
        return scene

    def prepare_image_sequence(self, sequence):
        """Create a project around an existing image directory.

        The source directory is passed directly to the solver. FFmpeg is not
        called and the source images are not duplicated into the private cache.
        """
        sequence = Path(sequence).resolve()
        if not sequence.is_dir():
            raise ValueError(f'图像序列目录不存在：{sequence}')
        images = sorted((path for path in sequence.iterdir()
                         if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
                        key=image_sort_key)
        if len(images) < 3:
            raise ValueError(f'图像序列至少需要 3 张图片，当前只有 {len(images)} 张。')
        signature = source_signature(sequence, self.options)
        scene = self.new_project(sequence, signature)
        cache = cache_directory(scene)
        manifest = cache / 'source.json'
        if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != signature:
            raise RuntimeError(f'输入设置与已有场景不一致：{scene}')
        manifest.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding='utf-8')
        self.emit('stage', index=1, total=5, text=f'使用图像序列（{len(images)} 张）')
        self.emit('log', text=f'[图像序列] 直接使用 {sequence}，跳过视频拆分。\n')
        return scene

    def _solve_native(self, images, cache, existing_scene):
        o = self.options
        database = cache / 'database.db'
        dynamic_zoom = bool(o.smart_lens)
        use_glomap = dynamic_zoom or o.backend == 'glomap'
        # Zoom and explicitly selected GLOMAP use the legacy paired COLMAP
        # for extraction/matching.  A normal COLMAP task uses the current
        # executable and its newer option namespaces.
        if use_glomap:
            colmap = find_legacy_colmap()
            mapper_binary = find_binary('02 GLOMAP GPU' if self.cuda_enabled else '02 GLOMAP',
                                        'glomap.exe', use_cuda=self.cuda_enabled)
        else:
            colmap = find_binary('01 GLOMAP', 'colmap.exe', use_cuda=self.cuda_enabled)
            mapper_binary = None
        extraction_device = 'CUDA' if self.cuda_enabled else 'CPU'
        extraction_gpu = '1' if self.cuda_enabled else '0'
        extraction_index = str(o.gpu_index) if self.cuda_enabled else '-1'
        self.emit('stage', index=2, total=5, text=f'{extraction_device} 特征提取')
        shared_ready = existing_scene and database.is_file() and database_counts(database)['pairs'] > 0
        if shared_ready:
            self.emit('log', text='[复用] 已有匹配数据库；本次只执行求解和导出。\n')
        elif not ((cache / '.features-complete').exists() and database.is_file()):
            if use_glomap:
                # GLOMAP 1.x is paired with COLMAP 3.12 and uses the
                # SiftExtraction/SiftMatching option namespaces.
                feature_args = [colmap, 'feature_extractor', '--database_path', database, '--image_path', images,
                                '--ImageReader.single_camera', '1', '--SiftExtraction.use_gpu', extraction_gpu,
                                '--SiftExtraction.gpu_index', extraction_index,
                                '--SiftExtraction.max_image_size', str(o.max_size)]
            else:
                feature_args = [colmap, 'feature_extractor', '--database_path', database, '--image_path', images,
                                '--ImageReader.single_camera', '1', '--FeatureExtraction.use_gpu', extraction_gpu,
                                '--FeatureExtraction.gpu_index', extraction_index,
                                '--FeatureExtraction.max_image_size', str(o.max_size)]
            if o.initial_focal_length > 0:
                width = (workload or {}).get('width')
                height = (workload or {}).get('height')
                if not width or not height:
                    try:
                        first_image = next(path for path in sorted(Path(images).iterdir())
                                           if path.suffix.lower() in IMAGE_EXTENSIONS)
                        info = video_info(first_image, 0)
                        width, height = info.get('width'), info.get('height')
                    except (StopIteration, OSError, ValueError, KeyError):
                        width = height = None
                if width and height:
                    scale = float(o.image_scale)
                    width, height = float(width) * scale, float(height) * scale
                    focal = float(o.initial_focal_length) * scale
                    cx, cy = width / 2.0, height / 2.0
                    if o.camera_mode == 'pinhole':
                        camera_model, params = 'PINHOLE', f'{focal:g},{cx:g},{cy:g}'
                    else:
                        camera_model, params = 'SIMPLE_RADIAL', f'{focal:g},{cx:g},{cy:g},0'
                    feature_args += ['--ImageReader.camera_model', camera_model,
                                     '--ImageReader.camera_params', params]
                    self.emit('log', text=(
                        f'[焦距先验] 使用初始焦距 {o.initial_focal_length:g} px '
                        f'（处理尺寸 {int(width)}×{int(height)}）。\n'))
                else:
                    self.emit('log', text='[焦距先验] 无法读取图像尺寸，已回退自动估计。\n')
            self.command(feature_args, '02-features')
            (cache / '.features-complete').write_text('complete', encoding='utf-8')
        self.emit('stage', index=3, total=5, text=f'{extraction_device} 序列匹配')
        if not shared_ready and not (cache / '.matching-complete').exists():
            if use_glomap:
                matching_args = [colmap, 'sequential_matcher', '--database_path', database,
                                 '--SiftMatching.use_gpu', extraction_gpu,
                                 '--SiftMatching.gpu_index', extraction_index,
                                 '--SequentialMatching.overlap', str(o.overlap)]
            else:
                matching_args = [colmap, 'sequential_matcher', '--database_path', database,
                                 '--FeatureMatching.use_gpu', extraction_gpu,
                                 '--FeatureMatching.gpu_index', extraction_index,
                                 '--SequentialMatching.overlap', str(o.overlap)]
            self.command(matching_args, '03-matching')
            (cache / '.matching-complete').write_text('complete', encoding='utf-8')
        counts = database_counts(database)
        if counts['images'] < 3 or counts['pairs'] < 2:
            raise RuntimeError(f'有效图像/匹配不足：{counts}')
        # ``smart_lens`` enables the dynamic-focal path.  Build a derived
        # temporal-camera database after extraction/matching so the solver can
        # optimize one focal parameter per segment instead of forcing one
        # shared camera calibration across the entire clip.
        dynamic_zoom = bool(getattr(o, 'smart_lens', o.auto_focal))
        run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        result = self.project_dir / '_cache' / ('solve_' + run_id)
        result.mkdir(parents=True)
        solver_database = database
        dynamic_focal_report = None
        if dynamic_zoom:
            solver_database = result / 'database_dynamic.db'
            dynamic_focal_report = result / 'dynamic_focal_segments.json'
            self.emit('log', text=(
                '[连续变焦] 正在生成临时分段数据库：30 帧分析窗口，5 帧上下文重叠；'
                '每帧只归属一个分段，原始 database.db 保持不变。\n'))
            focal_report = prepare_auto_focal_database(
                database,
                solver_database,
                report_path=dynamic_focal_report,
                target_frames=30,
                context_frames=5,
                images_dir=images,
                detect_cuts=True,
            )
            self.emit('log', text=(
                f"[连续变焦] 已建立 {len(focal_report['segments'])} 个焦距段，"
                f"数据库相机数 {focal_report['cameras_before']} → "
                f"{focal_report['cameras_after']}；"
                f"检测到 {len(focal_report['cut_boundaries'])} 个疑似切镜点。\n"))
            self.emit('log', text=(
                '[连续变焦] 将使用旧版 GLOMAP 的分段相机内参和 Bundle Adjustment；'
                '不调用新版 COLMAP 或 view_graph_calibrator。\n'))
        started = time.perf_counter()
        gpu_solver_active = self.cuda_enabled
        global_error = None
        if use_glomap:
            mode = ('glomap-segmented-cuda' if self.cuda_enabled else 'glomap-segmented-cpu') if dynamic_zoom else ('glomap-cuda' if self.cuda_enabled else 'glomap-cpu')
            self.emit('stage', index=4, total=5,
                      text=('GLOMAP 分段变焦（实验性）' if dynamic_zoom else 'GLOMAP 相机与点云求解'))
            model = result / 'glomap'
            args = [mapper_binary, 'mapper', '--database_path', solver_database if dynamic_zoom else database,
                    '--image_path', images, '--output_path', model,
                    '--log_to_stderr', '1', '--output_format', 'bin',
                    '--skip_view_graph_calibration', '0',
                    '--skip_relative_pose_estimation', '0',
                    '--skip_rotation_averaging', '0',
                    '--skip_global_positioning', '0',
                    '--skip_bundle_adjustment', '0',
                    '--skip_retriangulation', '0',
                    '--GlobalPositioning.use_gpu', '1' if self.cuda_enabled else '0',
                    '--GlobalPositioning.gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                    '--GlobalPositioning.min_num_images_gpu_solver', str(o.gpu_min_images),
                    '--BundleAdjustment.use_gpu', '1' if self.cuda_enabled else '0',
                    '--BundleAdjustment.gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                    '--BundleAdjustment.min_num_images_gpu_solver', str(o.gpu_min_images),
                    # Keep ordinary reconstruction fixed-focal.  Refining
                    # intrinsics is only appropriate for the experimental
                    # segmented zoom path and is prohibitively expensive on
                    # large tracks when enabled for every task.
                    '--BundleAdjustment.optimize_intrinsics', '1' if dynamic_zoom else '0',
                    # Prevent a pathological BA pass from consuming the
                    # machine indefinitely.  The zoom path keeps a larger
                    # budget because it has one focal parameter per segment.
                    '--BundleAdjustment.max_num_iterations', '100' if dynamic_zoom else '50']
            self.emit('log', text=(
                '[求解器] 使用旧版 GLOMAP 分段变焦流程（实验性）；' if dynamic_zoom
                else '[求解器] 使用独立 GLOMAP 完整流程；'
            ) + f'全局定位与 Bundle Adjustment 使用{"CUDA" if self.cuda_enabled else "CPU"}。\n')
            try:
                self.command(args, '04-glomap', cwd=mapper_binary.parent)
            except RuntimeError as exc:
                global_error = exc
            model = resolve_model_directory(model)
        else:
            mode = 'colmap-global-cuda' if self.cuda_enabled else 'colmap-global-cpu'
            self.emit('stage', index=4, total=5, text='COLMAP Global Mapper 相机与点云求解')
            sparse = result / 'sparse'
            sparse.mkdir()
            args = [colmap, 'global_mapper', '--database_path', database,
                    '--image_path', images, '--output_path', sparse,
                    '--log_target', 'stderr',
                    '--GlobalMapper.gp_use_gpu', '1' if self.cuda_enabled else '0',
                    '--GlobalMapper.gp_gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                    '--GlobalMapper.ba_ceres_use_gpu', '1' if self.cuda_enabled else '0',
                    '--GlobalMapper.ba_gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                    '--GlobalMapper.track_min_num_views_per_track', '2',
                    '--GlobalMapper.max_angular_reproj_error_deg', '16',
                    '--GlobalMapper.min_tri_angle_deg', '1',
                    '--GlobalMapper.ba_refine_focal_length', '1',
                    '--GlobalMapper.ba_dynamic_focal_length', '0']
            self.emit('log', text=(
                '[求解器] 使用原生 COLMAP Global Mapper；'
                f'全局定位与 Bundle Adjustment 使用{"CUDA" if self.cuda_enabled else "CPU"}。\n'))
            try:
                self.command(args, '04-colmap', cwd=colmap.parent)
            except RuntimeError as exc:
                global_error = exc
            model = resolve_model_directory(sparse)
        elapsed = time.perf_counter() - started
        if not model_complete(model):
            if global_error is not None:
                if dynamic_zoom:
                    message = 'GLOMAP 分段变焦解算未生成完整模型；'
                elif use_glomap:
                    message = 'GLOMAP 完整流程未生成完整模型；'
                else:
                    message = 'COLMAP Global Mapper 未生成完整模型；'
                raise RuntimeError(message + f'最后检查路径：{model}') from global_error
            raise RuntimeError(f'未生成完整模型：{model}')
        rotation_only_output = False
        self.emit('stage', index=5, total=5,
                  text='导出 Blender TXT 和相机轨迹' if rotation_only_output else '导出 Blender TXT 和点云')
        self.command([colmap, 'model_converter', '--input_path', model, '--output_path', model, '--output_type', 'TXT'], '05-export-model')
        if rotation_only_output:
            # COLMAP's PLY converter/analyzer rejects an empty point set on
            # some bundled builds. A valid empty PLY keeps the model layout
            # stable while the camera-only Blender path remains usable.
            ply = model / 'points3D.ply'
            ply.write_text('ply\nformat ascii 1.0\nelement vertex 0\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n', encoding='ascii')
            registered = sum(1 for line in (model / 'images.txt').read_text(encoding='utf-8').splitlines()
                             if line.strip() and not line.startswith('#') and len(line.split()) >= 10)
            metrics = {'registered_images': registered, 'points': 0, 'observations': 0}
            self.emit('log', text='[仅旋转] 已生成相机旋转轨迹；点云为空，不提供尺度和三维点。\n')
        else:
            self.command([colmap, 'model_converter', '--input_path', model, '--output_path', model / 'points3D.ply', '--output_type', 'PLY'], '05-export-ply')
            self.command([colmap, 'model_analyzer', '--path', model], '05-model-analysis')
            analysis = (self.log_dir / '05-model-analysis.log').read_text(encoding='utf-8')
            metrics = {}
            for name, pattern, cast in (
                ('registered_images', r'Registered images: (\d+)', int),
                ('points', r'Points: (\d+)', int),
                ('observations', r'Observations: (\d+)', int),
                ('mean_reprojection_error_px', r'Mean reprojection error: ([0-9.eE+-]+)', float),
            ):
                match = re.search(pattern, analysis)
                if match:
                    metrics[name] = cast(match.group(1))
            registered_images = int(metrics.get('registered_images') or 0)
            points = int(metrics.get('points') or 0)
            # A complete COLMAP directory can still contain a technically
            # valid model with only a handful of cameras and one point.  That
            # is unusable for a Blender reconstruction and previously caused
            # the task to be reported as completed with an empty-looking file.
            if registered_images < 3 or points < 10:
                raise RuntimeError(
                    f'重建结果无效：仅注册 {registered_images} 个相机、{points} 个三维点。'
                    '通常表示镜头几乎没有平移视差、存在切镜，或匹配不足；请检查素材或分段处理。')
        if metrics.get('mean_reprojection_error_px') is not None and metrics.get('mean_reprojection_error_px', 0) > 2:
            self.emit('log', text='[质量提示] 平均重投影误差超过 2 像素，建议检查相机轨迹和点云，再决定是否用于正式镜头。\n')
        return SolverResult(model, mode, elapsed, counts, metrics, gpu_solver_active, rotation_only_output)

    def _solve_vggt(self, images, cache):
        from vggt_runtime import worker_args
        run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        model = cache / ('solve_' + run_id) / 'vggt-omega'
        started = time.perf_counter()
        self.emit('log', text='[求解器] VGGT-Omega（实验性）：预测每帧相机、焦距与深度；跳过 SIFT 提取与匹配。\n')
        self.command(worker_args(self.options, images, model), '04-vggt-omega')
        report = json.loads((model / 'vggt-report.json').read_text(encoding='utf-8'))
        metrics = report['metrics']
        if not model_complete(model) or metrics['registered_images'] < 3 or metrics['points'] < 10:
            raise RuntimeError('VGGT-Omega 未生成有效相机与点云。')
        self.progress.begin('analyze', '已检查相机、深度采样点与模型文件')
        device = 'cuda' if self.cuda_enabled else 'cpu'
        mode = 'vggt-omega-' + device
        counts = dict(images=metrics['registered_images'], pairs=0, cameras=metrics['registered_images'])
        # The summary is deliberately not reported as a triangulated SfM model.
        self.emit('log', text='[质量提示] VGGT 点云来自预测深度，尺度为相对尺度；长片尚无全局 BA 或回环优化。\n')
        return SolverResult(model, mode, time.perf_counter() - started, counts, metrics, self.cuda_enabled)

    def run(self, source, existing_scene=False):
        task_started = time.perf_counter()
        # Normalize legacy aliases and enforce the zoom→GLOMAP restriction
        # before choosing the CUDA runtime.
        self.options.validate()
        # Keep GPU detection aligned with the executable selected below.
        if self.options.backend == 'vggt-omega':
            # torch checks the chosen CUDA device in the isolated worker.
            # Availability of COLMAP/GLOMAP binaries is irrelevant here.
            cuda_solver_available = True
        elif self.options.smart_lens or self.options.backend == 'glomap':
            cuda_solver_available = (RUNTIME_GLOMAP_CUDA.is_file() or
                                     DEV_GLOMAP_CUDA.is_file())
        else:
            cuda_solver_available = (use_dev_cuda_colmap() or
                                     RUNTIME_COLMAP_CUDA.is_file() or
                                     DEV_COLMAP_CUDA.is_file())
        self.cuda_enabled = bool(self.options.use_gpu and cuda_solver_available)
        self.preflight()
        o = self.options
        hardware = get_hardware(o.python, force=True, gpu_index=o.gpu_index)
        if o.use_gpu and not any(device['index'] == o.gpu_index for device in hardware.get('devices', [])):
            raise RuntimeError(f'所选 GPU {o.gpu_index} 不可用，请在设置中重新检测并选择显卡。')
        if o.use_gpu and not self.cuda_enabled:
            raise RuntimeError('GPU 求解器未安装或不可用，请使用 Start-CUDA.bat，或关闭硬件加速改用 CPU。')
        self.check_cancel()
        self.emit('log', text='[硬件] ' + describe_hardware(hardware) + '\n')
        # Metadata collection must not prevent a valid native-solver run.
        try:
            workload = describe_workload(source, o, existing_scene, self.project_dir)
        except Exception:
            workload = None
        if workload:
            self.workload = workload
            self.progress.frames = round(workload['frames'])
            try:
                from estimate import prediction_range, phase_prediction_ranges
                self.progress.estimate = prediction_range(workload, o, hardware)
                self.progress.phase_estimates = phase_prediction_ranges(workload, o, hardware)
            except Exception:
                pass
        if existing_scene:
            scene = Path(source).resolve()
            cache = cache_directory(scene)
            images = cache / 'images'
            if not images.is_dir() or not any(images.iterdir()):
                raise ValueError('场景目录需要包含 images 文件夹和图像')
            self.new_project(source)
            self.emit('stage', index=1, total=5, text='复用已有场景')
        else:
            if o.camera_mode == 'calibrated' and o.backend != 'vggt-omega':
                raise ValueError('已标定模式需要先准备含准确内参的场景数据库')
            if o.input_type == 'image_sequence':
                scene = self.prepare_image_sequence(source)
                images = Path(source).resolve()
            else:
                scene = self.prepare_video(source)
                images = None
            workload = self.workload or workload
            if workload:
                try:
                    from estimate import prediction_range, phase_prediction_ranges
                    self.progress.estimate = prediction_range(workload, o, hardware)
                    self.progress.phase_estimates = phase_prediction_ranges(workload, o, hardware)
                except Exception:
                    pass
            cache = cache_directory(scene)
            if images is None:
                images = cache / 'images'
        if o.backend == 'vggt-omega':
            solved = self._solve_vggt(images, cache)
        else:
            solved = self._solve_native(images, cache, existing_scene)
        model = solved.model
        # Publish a shallow, complete model only after all required exports succeed.
        self.progress.begin('publish')
        delivered_model = self.project_dir / 'point could'
        shutil.move(str(model), str(delivered_model))
        model = delivered_model
        summary = {'backend': solved.backend, 'source': str(source), 'scene': str(scene), 'model': str(model),
                   'layout_version': 2, 'project_dir': str(self.project_dir),
                   'name': self.project_name or Path(source).stem,
                   'log_dir': str(self.log_dir), 'images_dir': str(images),
                   'task_wall_seconds': time.perf_counter() - task_started, 'existing_scene': existing_scene,
                   'mapping_wall_seconds': solved.wall_seconds, 'database': solved.database, 'metrics': solved.metrics,
                   'gpu_solver_active': solved.gpu_active, 'solver_warnings': list(self.warnings), 'status': 'complete',
                   'rotation_only': solved.rotation_only,
                   'hardware': hardware, 'estimate_settings': settings_signature(o), 'workload': workload,
                   'phase_seconds': self.progress.timings(),
                   'completed_at': datetime.now().isoformat(timespec='seconds')}
        summary['smart_lens'] = o.smart_lens
        summary['focal_mode'] = ('vggt_predicted_per_frame' if o.backend == 'vggt-omega' else 'glomap_segmented_focal' if o.smart_lens
                                 else ('colmap_global_fixed' if o.backend == 'colmap' else 'glomap_view_graph'))
        summary['optimization_policy'] = (
            'VGGT-Omega feedforward + overlapping-window Sim3 (experimental)' if o.backend == 'vggt-omega' else
            'standalone GLOMAP segmented focal (experimental)' if o.smart_lens else
            ('native COLMAP Global Mapper' if o.backend == 'colmap' else 'standalone GLOMAP complete pipeline'))
        if o.image_scale != 1:
            summary['image_scale'] = o.image_scale
        manifest = cache / 'source.json'
        summary['source_settings'] = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else {}
        summary['input_type'] = o.input_type
        summary['sampling_fps'] = o.fps
        summary['cache_policy'] = 'discard_after_exports_v1'
        atomic_write_json(result_report(summary), summary)
        if not getattr(self, 'defer_cache_cleanup', False):
            from blender_sequence import clean_solver_cache
            clean_solver_cache(self, summary)
        self.emit('complete', **summary)
        return summary


def video_filters(options, original_size=False):
    """One filter definition for reconstruction and delivered sequences."""
    filters = [f'fps={options.fps:g}'] if options.fps else []
    if not original_size and options.image_scale != 1:
        scale = options.image_scale
        filters.append(f'scale=max(2\\,trunc(iw*{scale:g}/2)*2):max(2\\,trunc(ih*{scale:g}/2)*2)')
    return ','.join(filters)
