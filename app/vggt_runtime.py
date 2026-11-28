"""Locate optional VGGT assets without importing torch in the desktop process."""
import json
import os
from pathlib import Path

from paths import ROOT, CONFIG, RUNTIME, python_runtime_path

UPSTREAM_REVISION = '48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6'
CHECKPOINT_NAME = 'vggt_omega_1b_512.pt'
CHECKPOINT_SHA256 = 'c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934'
CHECKPOINT_BYTES = 4576706117


def locate_assets():
    config = {}
    manifest = CONFIG / 'vggt-runtime.json'
    if manifest.is_file():
        try:
            config = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(config, dict):
                raise ValueError('需要 JSON 对象')
        except (OSError, ValueError) as exc:
            raise ValueError(f'VGGT 配置无法读取：{manifest}：{exc}') from exc
    development = ROOT.parents[1] / 'third_party'

    def select(key, environment, candidates, check):
        explicit = os.environ.get(environment) or config.get(key)
        if explicit:
            if not isinstance(explicit, str):
                raise ValueError(f'VGGT {key} 配置必须是路径字符串。')
            path = Path(explicit).expanduser()
            path = path if path.is_absolute() else ROOT / path
            if check(path):
                return path.resolve()
            raise FileNotFoundError(f'VGGT {key} 路径无效：{path}')
        for path in candidates:
            if check(path):
                return path.resolve()
        raise FileNotFoundError('VGGT-Omega 尚未配置。请参照 docs/VGGT-OMEGA.md 放置源码和模型权重。')

    source = select('source', 'SEEDOM_VGGT_SOURCE',
                    (RUNTIME / 'vggt-omega', development / 'vggt-omega'),
                    lambda p: (p / 'vggt_omega/models/vggt_omega.py').is_file())
    checkpoint = select('checkpoint', 'SEEDOM_VGGT_CHECKPOINT',
                        (RUNTIME / 'models/VGGT-Omega' / CHECKPOINT_NAME,
                         development / 'models/VGGT-Omega' / CHECKPOINT_NAME),
                        lambda p: p.is_file() and p.stat().st_size == CHECKPOINT_BYTES)
    return source, checkpoint


def profile(quality):
    # Bound activation memory. Every source frame is inferred, never interpolated.
    return {'fast': (384, 8), 'balanced': (512, 12), 'high': (512, 20)}[quality]


def worker_args(options, images, output):
    source, checkpoint = locate_assets()
    resolution, window = profile(options.solver_quality)
    from paths import APP
    # The embedded Python ignores PYTHONPATH and the script directory.
    bootstrap = (f"import runpy,sys; sys.path.insert(0,{str(APP)!r}); "
                 f"runpy.run_path({str(APP / 'vggt_worker.py')!r},run_name='__main__')")
    return [str(python_runtime_path(options.python)), '-X', 'utf8', '-u', '-c', bootstrap,
            '--source', str(source), '--checkpoint', str(checkpoint),
            '--images', str(images), '--output', str(output),
            '--device', f'cuda:{options.gpu_index}' if options.use_gpu else 'cpu',
            '--resolution', str(resolution), '--window', str(window),
            '--color', '1' if options.color else '0']
