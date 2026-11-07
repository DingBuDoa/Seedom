"""Probe the selected runtime in a child process, without retaining a GUI CUDA context."""
from pathlib import Path
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time

from paths import APP, runtime_environment

_cache = {}
_lock = threading.Lock()
POLICY_VERSION = 'adaptive-memory-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def settings_signature(options):
    keys = ('fps', 'max_size', 'overlap', 'cuda_decode', 'camera_mode')
    values = {key: getattr(options, key) for key in keys}
    values['policy_version'] = POLICY_VERSION
    if options.gpu_index:
        values['gpu_index'] = options.gpu_index
    if options.image_scale != 1:
        values['image_scale'] = options.image_scale
    if options.export_sequence:
        values['export_sequence'] = True
        values['sequence_original'] = options.sequence_original
    if options.backend == 'fastmap':
        values['color'] = options.color
        from solver_quality import OPTIMIZATION_REVISION
        values.update(solver_quality=options.solver_quality, optimization_revision=OPTIMIZATION_REVISION)
    elif options.glomap_gpu:
        values['gpu_min_images'] = options.gpu_min_images
    return values


def system_information():
    """Read CPU and OS identity without importing a GPU runtime."""
    cpu = platform.processor()
    system = platform.platform()
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                cpu = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
        version = sys.getwindowsversion()
        release = '11' if version.build >= 22000 else platform.release()
        system = f'Windows {release} ({version.build})'
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
                display_version = winreg.QueryValueEx(key, 'DisplayVersion')[0]
                revision = winreg.QueryValueEx(key, 'UBR')[0]
            system = f'Windows {release} {display_version} ({version.build}.{revision})'
        except OSError:
            pass
    return {'cpu': cpu.strip() or '未知', 'os': platform.platform(), 'system_label': system}


def probe_local(gpu_index=0):
    machine = platform.node()
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as key:
                machine += winreg.QueryValueEx(key, 'MachineGuid')[0]
        except OSError:
            pass
    try:
        import psutil
        ram = psutil.virtual_memory().total
    except ImportError:
        ram = 0
    profile = {**system_information(), 'gpu_index': gpu_index, 'machine_key': digest(machine), 'threads': os.cpu_count(),
               'ram_bytes': ram, 'python': platform.python_version(),
               'cuda_available': False, 'cuda_usable': False, 'devices': []}
    try:
        import torch
        profile.update(torch=torch.__version__, cuda_runtime=torch.version.cuda)
        profile['cuda_available'] = torch.cuda.is_available()
        for index in range(torch.cuda.device_count()):
            prop = torch.cuda.get_device_properties(index)
            profile['devices'].append({'index': index, 'name': prop.name,
                                      'total_bytes': prop.total_memory,
                                      'capability': [prop.major, prop.minor]})
        if profile['cuda_available']:
            torch.cuda.set_device(gpu_index)
            free, total = torch.cuda.mem_get_info(gpu_index)
            profile.update(free_bytes=free, total_bytes=total)
            # is_available alone does not establish that this build supports the GPU.
            value = torch.ones(1, device=f'cuda:{gpu_index}') + 1
            profile['cuda_usable'] = value.item() == 2
            del value
    except Exception as error:
        profile['error'] = str(error)[-400:]
    stable = {key: value for key, value in profile.items()
              if key not in ('free_bytes', 'error', 'cuda_usable', 'system_label', 'gpu_index')}
    if gpu_index:
        stable['gpu_index'] = gpu_index
    profile['fingerprint'] = digest(stable)
    return profile


def get_hardware(python, force=False, gpu_index=0):
    path = str(Path(python).resolve())
    cache_key = (path, gpu_index)
    with _lock:
        cached = _cache.get(cache_key)
        if not force and cached and time.monotonic() - cached[0] < 60:
            return dict(cached[1])
        environment = os.environ.copy()
        environment.update(runtime_environment())
        try:
            result = subprocess.run([path, '-X', 'utf8', str(APP / 'hardware.py'), '--probe', '--gpu-index', str(gpu_index)],
                                    env=environment, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                raise RuntimeError(result.stderr[-400:] or '硬件检测进程失败')
            profile = json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired, RuntimeError) as error:
            return {**system_information(), 'gpu_index': gpu_index, 'error': str(error), 'cuda_usable': False}
        _cache[cache_key] = (time.monotonic(), profile)
        return dict(profile)


def describe_hardware(profile):
    devices = profile.get('devices', [])
    index = profile.get('gpu_index', 0)
    gpu = next((device for device in devices if device.get('index', 0) == index), None)
    if gpu is None:
        return 'CUDA 不可用 · ' + profile.get('error', '未检测到兼容 NVIDIA GPU')
    text = f"GPU {index} · {gpu['name']} · 显存 {gpu['total_bytes']/2**30:.1f} GiB"
    if 'free_bytes' in profile:
        text += f" / 检测时可用 {profile['free_bytes']/2**30:.1f} GiB"
    text += ' · CUDA 实算通过' if profile.get('cuda_usable') else ' · CUDA 实算未通过'
    return text


def hardware_summary(profile):
    """Keep screenshot diagnostics short; memory is total VRAM, not live usage."""
    cpu = profile.get('cpu') or '未知'
    for marker in ('(R)', '(TM)', '®', '™'):
        cpu = cpu.replace(marker, '')
    cpu = ' '.join(cpu.replace(' CPU', '').split())
    devices = profile.get('devices', [])
    index = profile.get('gpu_index', 0)
    gpu = next((device for device in devices if device.get('index', 0) == index), None)
    if gpu is not None:
        name = gpu.get('name') or '未知'
        name = name.replace('NVIDIA GeForce ', '').replace('NVIDIA ', '')
        total = gpu.get('total_bytes')
        memory = f'{total / 2**30:.1f} GiB' if isinstance(total, (int, float)) and total > 0 else '未知'
        prefix = f'GPU {index}' if len(devices) > 1 or index else 'GPU'
        graphics = f'{prefix}: {name} · 显存 {memory}'
    else:
        status = '待检测' if profile.get('pending') else '检测失败' if profile.get('error') else '未检测到 CUDA 显卡'
        graphics = f'GPU: {status} · 显存 —'
    return f"CPU: {cpu} | {graphics} | {profile.get('system_label') or profile.get('os') or '系统未知'}"


if __name__ == '__main__' and '--probe' in sys.argv:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--gpu-index', type=int, default=0)
    print(json.dumps(probe_local(parser.parse_args().gpu_index), ensure_ascii=False))
