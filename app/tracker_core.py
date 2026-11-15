"""Shared reconstruction pipeline for the desktop interface and CLI tests."""
from dataclasses import dataclass
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
                   use_dev_cuda_colmap, runtime_environment)
from hardware import get_hardware, describe_hardware, settings_signature
from workload import source_signature, video_scene, describe_workload, image_sort_key
from native_paths import NativePaths
from project_layout import create_project, cache_directory, result_report, safe_name
from json_store import atomic_write_json
from task_progress import TaskProgress
from solver_quality import SOLVER_PROFILES, OPTIMIZATION_REVISION
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm', '.mts', '.m2ts', '.wmv', '.mpg', '.mpeg'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp', '.exr'}
HIDE_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class Cancelled(Exception):
    pass


@dataclass
class Options:
    backend: str = 'colmap'
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
    export_sequence: bool = False
    sequence_original: bool = False
    solver_quality: str = 'balanced'
    gpu_index: int = 0
    use_gpu: bool = True
    input_type: str = 'video'
    # User-facing camera analysis switch.  The two fields below remain only
    # as compatibility aliases for 1.x task JSON files.
    smart_lens: bool = True
    auto_focal: bool = False
    contains_cuts: bool = False
    rotation_only: bool = False

    def validate(self):
        for key in ('fps', 'image_scale'):
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
        # Legacy 1.x/early 2.0 task records are migrated to the native
        # COLMAP Global Mapper.  The merged global_mapper command is the
        # supported solver path for the current database schema.
        if self.backend in ('fastmap', 'glomap'):
            self.backend = 'colmap'
            self.rotation_only = False
        if self.backend != 'colmap' or self.camera_mode not in ('radial', 'pinhole', 'calibrated'):
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
        return self


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


def model_complete(model):
    return all((model / n).is_file() and (model / n).stat().st_size > 0
               for n in ('cameras.bin', 'images.bin', 'points3D.bin'))


def model_has_points(model):
    """Return whether the binary model contains at least one 3D point."""
    points = model / 'points3D.bin'
    # COLMAP writes an eight-byte point count even when the set is empty.
    return model_complete(model) and points.stat().st_size > 8


def database_counts(database):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return {'images': db.execute('SELECT COUNT(*) FROM images').fetchone()[0],
                'pairs': db.execute('SELECT COUNT(*) FROM two_view_geometries WHERE rows>0').fetchone()[0]}


def database_focal_prior_ratio(database):
    """Return the fraction of cameras with a reliable focal-length prior.

    COLMAP 4.x stores the calibration origin in ``cameras.source`` rather
    than the old ``prior_focal_length`` column.  A value above GUESS (2) is a
    trusted prior.  Supporting both schemas keeps old task caches readable.
    """
    try:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
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
        filename = '04-glomap.log' if self.options.backend == 'glomap' else '04-colmap.log'
        warning = dict(code=code, text=text, log_path=str(self.log_dir / filename))
        self.warnings.append(warning)
        self.emit('log', text=f"[求解提醒] {text} 日志：{warning['log_path']}\n")
        self.emit('solver_warning', **warning)

    def observe_glomap_output(self, line):
        if self.options.backend == 'glomap' and self.options.glomap_gpu and 'falling back' in line.casefold():
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

    def run(self, source, existing_scene=False):
        task_started = time.perf_counter()
        self.cuda_enabled = bool(self.options.use_gpu and
                                 (use_dev_cuda_colmap() or RUNTIME_COLMAP_CUDA.is_file() or DEV_COLMAP_CUDA.is_file()))
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
            if o.camera_mode == 'calibrated':
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
        database = cache / 'database.db'
        colmap = find_binary('01 GLOMAP', 'colmap.exe', use_cuda=self.cuda_enabled)
        extraction_device = 'CUDA' if self.cuda_enabled else 'CPU'
        extraction_gpu = '1' if self.cuda_enabled else '0'
        extraction_index = str(o.gpu_index) if self.cuda_enabled else '-1'
        self.emit('stage', index=2, total=5, text=f'{extraction_device} 特征提取')
        shared_ready = existing_scene and database.is_file() and database_counts(database)['pairs'] > 0
        if shared_ready:
            self.emit('log', text='[复用] 已有匹配数据库；本次只执行求解和导出。\n')
        elif not ((cache / '.features-complete').exists() and database.is_file()):
            # COLMAP 4.x moved the device controls from the Sift-specific
            # option groups to the command-wide FeatureExtraction group.
            self.command([colmap, 'feature_extractor', '--database_path', database, '--image_path', images,
                          '--ImageReader.single_camera', '1', '--FeatureExtraction.use_gpu', extraction_gpu,
                          '--FeatureExtraction.gpu_index', extraction_index,
                          '--FeatureExtraction.max_image_size', str(o.max_size)], '02-features')
            (cache / '.features-complete').write_text('complete', encoding='utf-8')
        self.emit('stage', index=3, total=5, text=f'{extraction_device} 序列匹配')
        if not shared_ready and not (cache / '.matching-complete').exists():
            self.command([colmap, 'sequential_matcher', '--database_path', database,
                          '--FeatureMatching.use_gpu', extraction_gpu, '--FeatureMatching.gpu_index', extraction_index,
                          '--SequentialMatching.overlap', str(o.overlap)], '03-matching')
            (cache / '.matching-complete').write_text('complete', encoding='utf-8')
        counts = database_counts(database)
        if counts['images'] < 3 or counts['pairs'] < 2:
            raise RuntimeError(f'有效图像/匹配不足：{counts}')
        # ``smart_lens`` is the public switch.  The fallback keeps 1.x option
        # objects operational if a caller constructs one without migration.
        smart_lens = bool(getattr(o, 'smart_lens', o.auto_focal or o.contains_cuts))
        if smart_lens:
            self.emit('log', text=(
                '[智能镜头] 使用原生 COLMAP 动态焦距 BA；'
                '数据库保持原始 Schema，不再复制或修改 database.db。\n'))
        if o.backend == 'colmap' and smart_lens:
            focal_prior_ratio = database_focal_prior_ratio(database)
            # Global Mapper needs a reliable focal-length prior to obtain a
            # usable initial camera configuration.  Run the native calibrator
            # on every COLMAP task so retries and pre-existing caches follow
            # the same path; it preserves stronger USER/EXIF priors.
            self.emit('log', text=(
                f'[焦距校准] 当前可信焦距先验为 {focal_prior_ratio:.0%}；'
                '运行 COLMAP 原生 view_graph_calibrator。\n'))
            self.command([
                colmap, 'view_graph_calibrator', '--database_path', database,
                '--log_target', 'stderr',
            ], '04-focal-calibration', cwd=colmap.parent)
            calibrated_ratio = database_focal_prior_ratio(database)
            if calibrated_ratio < 0.5:
                self.warn_solver(
                    'focal_prior_missing',
                    'COLMAP 未能建立足够的焦距先验，当前视频可能存在低视差或匹配不足；'
                    '已停止全局定位，请改用固定焦距或检查镜头运动。',
                )
                raise RuntimeError(
                    f'焦距校准未完成（可信先验 {calibrated_ratio:.0%}），无法安全开始全局定位。')
            self.emit('log', text=(
                f'[焦距校准] 完成，可信焦距先验为 {calibrated_ratio:.0%}。\n'))
        mode = 'colmap-global-cuda' if self.cuda_enabled else 'colmap-global-cpu'
        run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        result = self.project_dir / '_cache' / ('solve_' + run_id)
        self.emit('stage', index=4, total=5, text=f'{mode} 相机与点云求解')
        started = time.perf_counter()
        # Global Mapper can accelerate global positioning with CUDA.  Ceres
        # Bundle Adjustment is reported separately and may still fall back to
        # CPU when the bundled Ceres was built without CUDA/cuDSS.
        gpu_solver_active = self.cuda_enabled
        global_error = None
        result.mkdir(parents=True)
        sparse = result / 'sparse'
        sparse.mkdir()
        # COLMAP 4.x merged the global mapper into the main executable.  Keep
        # all command-line options in its own namespace; legacy GLOMAP flags
        # must never reach this executable.
        args = [colmap, 'global_mapper', '--database_path', database, '--image_path', images,
                '--output_path', sparse, '--log_target', 'stderr',
                '--GlobalMapper.gp_use_gpu', '1' if self.cuda_enabled else '0',
                '--GlobalMapper.gp_gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                '--GlobalMapper.ba_ceres_use_gpu', '1' if self.cuda_enabled else '0',
                '--GlobalMapper.ba_gpu_index', str(o.gpu_index) if self.cuda_enabled else '-1',
                '--GlobalMapper.track_min_num_views_per_track', '2',
                '--GlobalMapper.max_angular_reproj_error_deg', '16',
                '--GlobalMapper.min_tri_angle_deg', '1']
        if self.cuda_enabled:
            self.emit('log', text=(
                '[求解器] 使用原生 COLMAP Global Mapper；全局定位与中等规模 BA 使用 CUDA，'
                '超大规模 BA 若 Ceres 未带 cuDSS 会自动使用 CPU 稀疏求解。\n'))
        else:
            self.emit('log', text='[求解器] 使用原生 COLMAP Global Mapper + CPU。\n')
        if smart_lens:
            args += ['--GlobalMapper.ba_refine_focal_length', '1',
                     '--GlobalMapper.ba_refine_principal_point', '0',
                     '--GlobalMapper.ba_dynamic_focal_length', '1',
                     '--GlobalMapper.ba_dynamic_focal_first_order_weight', '1.0',
                     '--GlobalMapper.ba_dynamic_focal_second_order_weight', '0']
            self.emit('log', text='[智能镜头] 已启用 COLMAP 原生动态焦距 BA；不修改输入数据库。\n')
        else:
            args += ['--GlobalMapper.ba_refine_focal_length', '0',
                     '--GlobalMapper.ba_dynamic_focal_length', '0']
            self.emit('log', text='[智能镜头] 已关闭，使用固定焦距 Global Mapper。\n')
        try:
            self.command(args, '04-colmap', cwd=colmap.parent)
        except RuntimeError as exc:
            global_error = exc
            self.emit('log', text=(
                '[求解器] Global Mapper 未完成有效模型，准备自动切换原生 COLMAP 增量式解算。\n'))
        elapsed = time.perf_counter() - started
        model = result / 'sparse/0'
        if o.backend == 'colmap' and not model_has_points(model):
            fallback_root = result / 'incremental'
            fallback_root.mkdir(parents=True, exist_ok=True)
            mapper_args = [
                colmap, 'mapper', '--database_path', database,
                '--image_path', images, '--output_path', fallback_root,
                '--log_target', 'stderr',
                '--Mapper.multiple_models', '0',
                '--Mapper.min_model_size', '3',
                '--Mapper.init_min_tri_angle', '1',
                '--Mapper.init_max_error', '16',
                '--Mapper.filter_max_reproj_error', '16',
            ]
            if self.cuda_enabled:
                # The incremental fallback has its own GPU controls.  Without
                # these flags COLMAP silently defaults to CPU BA even when the
                # primary Global Mapper was launched with CUDA enabled.
                mapper_args += [
                    '--Mapper.ba_use_gpu', '1',
                    '--Mapper.ba_gpu_index', str(o.gpu_index),
                    '--Mapper.ba_min_num_images_gpu_solver', str(o.gpu_min_images),
                ]
            if smart_lens:
                mapper_args += [
                    '--Mapper.ba_refine_focal_length', '1',
                    '--Mapper.ba_refine_principal_point', '0',
                    '--Mapper.ba_dynamic_focal_length', '1',
                    '--Mapper.ba_dynamic_focal_first_order_weight', '1.0',
                    '--Mapper.ba_dynamic_focal_second_order_weight', '0',
                ]
            else:
                mapper_args += [
                    '--Mapper.ba_refine_focal_length', '0',
                    '--Mapper.ba_dynamic_focal_length', '0',
                ]
            self.emit('log', text=(
                '[求解器] 全局定位未生成有效模型或点云为空，已自动切换原生 COLMAP 增量式解算；'
                '这不会重新提取特征或匹配。\n'))
            self.command(mapper_args, '04-colmap-fallback', cwd=colmap.parent)
            model = fallback_root / '0'
            if model_complete(model):
                self.emit('log', text='[求解器] 原生 COLMAP 增量式解算成功，继续导出。\n')
        if not model_complete(model):
            if global_error is not None:
                raise RuntimeError(
                    'Global Mapper 和原生 COLMAP 增量式解算均未生成完整模型；'
                    f'最后检查路径：{model}') from global_error
            raise RuntimeError(f'未生成完整模型：{model}')
        rotation_only_output = o.backend == 'fastmap' and (result / 'rotation-only.json').is_file()
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
        sequence = None
        if o.export_sequence and o.input_type == 'image_sequence':
            self.emit('log', text='[图像序列] 输入已经是序列帧，跳过再次导出。\n')
        if o.export_sequence and o.input_type != 'image_sequence':
            if existing_scene:
                manifest = cache / 'source.json'
                original = json.loads(manifest.read_text(encoding='utf-8')).get('source') if manifest.is_file() else None
                if not original or not Path(original).is_file():
                    raise ValueError('导出序列帧需要原视频，请恢复 source.json 中记录的素材。')
            else:
                original = source
            self.emit('stage', index=5, total=5, text='导出视频序列帧')
            sequence = self.project_dir / 'video'
            sequence.mkdir(exist_ok=True)
            args = [find_binary('03 FFMPEG', 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error',
                    '-stats', '-y', '-i', original, '-map', '0:v:0', '-an']
            filters = video_filters(o, original_size=o.sequence_original)
            if filters:
                args += ['-vf', filters]
            self.command(args + ['-qscale:v', '2', sequence / 'frame_%06d.jpg'], '06-video-sequence')
            self.check_cancel()
        # Publish a shallow, complete model only after all required exports succeed.
        self.progress.begin('publish')
        delivered_model = self.project_dir / 'point could'
        shutil.move(str(model), str(delivered_model))
        model = delivered_model
        summary = {'backend': mode, 'source': str(source), 'scene': str(scene), 'model': str(model),
                   'layout_version': 2, 'project_dir': str(self.project_dir),
                   'name': self.project_name or Path(source).stem,
                   'log_dir': str(self.log_dir), 'images_dir': str(images),
                   'task_wall_seconds': time.perf_counter() - task_started, 'existing_scene': existing_scene,
                   'mapping_wall_seconds': elapsed, 'database': counts, 'metrics': metrics,
                   'gpu_solver_active': gpu_solver_active, 'solver_warnings': list(self.warnings), 'status': 'complete',
                   'rotation_only': rotation_only_output,
                   'hardware': hardware, 'estimate_settings': settings_signature(o), 'workload': workload,
                   'phase_seconds': self.progress.timings(),
                   'completed_at': datetime.now().isoformat(timespec='seconds')}
        if smart_lens:
            summary['smart_lens'] = True
            summary['focal_mode'] = 'native_dynamic_ba' if o.backend == 'colmap' else 'glomap_intrinsics'
        summary['optimization_policy'] = 'COLMAP native Global Mapper'
        if sequence is not None:
            summary['sequence'] = str(sequence)
        if o.image_scale != 1:
            summary['image_scale'] = o.image_scale
        atomic_write_json(result_report(summary), summary)
        self.emit('complete', **summary)
        return summary


def video_filters(options, original_size=False):
    """One filter definition for reconstruction and delivered sequences."""
    filters = [f'fps={options.fps:g}'] if options.fps else []
    if not original_size and options.image_scale != 1:
        scale = options.image_scale
        filters.append(f'scale=max(2\\,trunc(iw*{scale:g}/2)*2):max(2\\,trunc(ih*{scale:g}/2)*2)')
    return ','.join(filters)
