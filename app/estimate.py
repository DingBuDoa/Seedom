"""Conservative ranges from comparable local runs; never benchmark promises."""
import json
import math
import statistics

from paths import CONFIG
from hardware import get_hardware, settings_signature
from workload import describe_workload
from json_store import atomic_write_json

HISTORY = CONFIG / 'task-history.json'


def read_samples():
    try:
        samples = json.loads(HISTORY.read_text(encoding='utf-8'))
        return samples if isinstance(samples, list) else []
    except (OSError, ValueError):
        return []


def save_sample(result):
    # Preserve untagged older records, but never use them as local calibration.
    samples = read_samples()
    samples.append({'schema': 2, 'backend': result['backend'],
                    'frames': result['database']['images'],
                    'seconds': result['task_wall_seconds'] if 'task_wall_seconds' in result else result['mapping_wall_seconds'],
                    'existing_scene': result.get('existing_scene', True),
                    'hardware_key': result.get('hardware', {}).get('fingerprint'),
                    'settings': result.get('estimate_settings'),
                    'workload': result.get('workload'),
                    'phase_seconds': result.get('phase_seconds', {}),
                    'gpu_solver_active': result.get('gpu_solver_active')})
    atomic_write_json(HISTORY, samples[-80:])


def comparable(sample, backend, hardware_key, settings, workload):
    if not isinstance(sample, dict) or sample.get('schema') != 2 or not hardware_key:
        return False
    if sample.get('backend') != backend or sample.get('hardware_key') != hardware_key or sample.get('settings') != settings:
        return False
    if backend in ('fastmap', 'glomap-gpu') and sample.get('gpu_solver_active') is not True:
        return False
    for key in ('frames', 'seconds'):
        if not isinstance(sample.get(key), (int, float)) or not math.isfinite(sample[key]) or sample[key] <= 0:
            return False
    old = sample.get('workload')
    if not isinstance(old, dict):
        return False
    if any(old.get(key) != workload.get(key) for key in ('stages', 'existing_scene', 'width', 'height', 'codec')):
        return False
    # Frame scaling is only a rough approximation within a similar scene size.
    ratio = workload['frames'] / sample['frames']
    if not 0.5 <= ratio <= 2:
        return False
    if workload.get('pairs') is not None:
        if not isinstance(old.get('pairs'), (int, float)) or old['pairs'] <= 0:
            return False
        density_ratio = (workload['pairs'] / workload['frames']) / (old['pairs'] / sample['frames'])
        if not 0.5 <= density_ratio <= 2:
            return False
    return True


def duration_text(seconds):
    # Avoid implying second-level precision for an uncertain estimate.
    if seconds >= 60:
        return f'{math.ceil(seconds/60)} 分钟'
    return f'{max(5, math.ceil(seconds/5)*5)} 秒'


def prediction_range(workload, options, hardware, samples=None):
    """Structured task duration, shared by queue previews and live countdowns."""
    backend = 'fastmap' if options.backend == 'fastmap' else ('glomap-gpu' if options.glomap_gpu else 'glomap-cpu')
    settings = settings_signature(options)
    samples = read_samples() if samples is None else samples
    matching = [sample for sample in samples if comparable(
        sample, backend, hardware.get('fingerprint'), settings, workload)][-5:]
    if not matching:
        return None
    predictions = [sample['seconds'] * workload['frames'] / sample['frames'] for sample in matching]
    median = statistics.median(predictions)
    low, high = (.5, 2.0) if len(matching) < 3 else (.7, 1.5)
    return dict(lower=min(median * low, min(predictions) * .8),
                upper=max(median * high, max(predictions) * 1.2), samples=len(matching))


def phase_prediction_ranges(workload, options, hardware, samples=None):
    """Reuse solver timings independently of decoding, outputs and cache state."""
    from task_progress import FASTMAP_STAGES, GLOMAP_STAGES
    backend = 'fastmap' if options.backend == 'fastmap' else ('glomap-gpu' if options.glomap_gpu else 'glomap-cpu')
    settings = settings_signature(options)
    ignored = {'fps', 'cuda_decode', 'export_sequence', 'sequence_original'}
    solver_settings = {key: value for key, value in settings.items() if key not in ignored}
    samples = read_samples() if samples is None else samples
    values = {}
    phases = dict(FASTMAP_STAGES if options.backend == 'fastmap' else GLOMAP_STAGES)
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get('settings'), dict):
            continue
        if {key: value for key, value in sample['settings'].items() if key not in ignored} != solver_settings:
            continue
        old_work = sample.get('workload')
        if not isinstance(old_work, dict):
            continue
        # These fields determine preparation cost, not the solver's phase cost.
        solver_work = dict(workload, **{key: old_work.get(key)
                           for key in ('stages', 'existing_scene', 'codec')})
        if not comparable(dict(sample, settings=settings), backend,
                          hardware.get('fingerprint'), settings, solver_work):
            continue
        timings = sample.get('phase_seconds')
        if not isinstance(timings, dict):
            continue
        for phase in phases:
            seconds = timings.get(phase)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
                continue
            values.setdefault(phase, []).append(seconds * workload['frames'] / sample['frames'])
    result = {}
    for phase, predictions in values.items():
        predictions = predictions[-5:]
        median = statistics.median(predictions)
        low, high = (.5, 2.0) if len(predictions) < 3 else (.7, 1.5)
        result[phase] = dict(lower=min(median * low, min(predictions) * .8),
                             upper=max(median * high, max(predictions) * 1.2), samples=len(predictions))
    return result


def estimate_jobs(jobs, options):
    if not jobs:
        return '预估任务时长：添加视频后自动估算'
    hardware = get_hardware(options.python, gpu_index=options.gpu_index)
    if not hardware.get('fingerprint'):
        return '预估任务时长：暂不可用（硬件检测未完成或失败）'
    if not hardware.get('cuda_usable') and (options.backend == 'fastmap' or options.glomap_gpu):
        return '预估任务时长：暂不可用（CUDA 实算未通过，请检查运行环境）'
    samples = read_samples()
    lower, upper, unknown, unreadable, least_samples = 0.0, 0.0, 0, 0, 5
    for job in jobs:
        try:
            workload = describe_workload(job['path'], options, job['scene'], job.get('project_dir'))
            prediction = prediction_range(workload, options, hardware, samples)
            if not prediction:
                unknown += 1
                continue
            least_samples = min(least_samples, prediction['samples'])
            lower += prediction['lower']
            upper += prediction['upper']
        except Exception:
            unreadable += 1
    notes = []
    if lower:
        notes.append(f'本机同参数相近规模记录；至少 {least_samples} 次参考')
        notes.append('粗估，非保证' if least_samples >= 3 else '样本少，误差可能较大')
    if unknown:
        notes.append(f'{unknown} 项未校准：需本机同参数、相近规模和缓存状态的完成记录')
    if unreadable:
        notes.append(f'{unreadable} 项无法读取素材信息')
    if not lower:
        return '总时长尚未校准；运行后按实际进度估算当前阶段（' + '；'.join(notes) + '）'
    qualifier = '可估部分约' if unknown or unreadable else '约'
    return f'预估任务时长：{qualifier} {duration_text(lower)}–{duration_text(upper)}（' + '；'.join(notes) + '）'
