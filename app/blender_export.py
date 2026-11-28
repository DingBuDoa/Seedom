"""Shared Blender invocation, with the existing importer arguments and file safeguards."""
import json
from pathlib import Path
from paths import APP, ROOT
from project_library import model_ready
from project_layout import result_directory


DEFAULT_POINT_RADIUS = .005


def default_point_radius(settings):
    radius = settings.get('blender_point_radius', DEFAULT_POINT_RADIUS)
    # Migrate the old oversized default, while preserving custom sizes.
    if settings.get('blender_point_size_revision', 0) < 2 and radius in (.05, .01):
        return DEFAULT_POINT_RADIUS
    return radius


def progress_event(text):
    if not text.startswith('[BLENDER_PROGRESS] '): return None
    try:
        data = json.loads(text[len('[BLENDER_PROGRESS] '):])
        if not isinstance(data['text'], str): return None
        return dict(type='blender_progress', text=data['text'],
                    percent=max(0, min(99, int(data['percent']))))
    except (ValueError, KeyError, TypeError):
        return None


def export_blender(pipeline, result, output, settings):
    if not model_ready(result):
        raise ValueError('请先成功生成点云（或仅旋转相机）及完整的 COLMAP TXT 模型。')
    model, output = Path(result['model']), Path(output)
    if output.exists():
        raise ValueError('为保留已有文件，请使用新的 Blender 文件名。')
    if not (ROOT / 'photogrammetry_importer/__init__.py').is_file():
        raise FileNotFoundError('缺少内置 photogrammetry_importer 插件。')
    pipeline.log_dir = Path(result['log_dir']) if result.get('log_dir') else result_directory(result) / 'blender-logs'
    pipeline.log_dir.mkdir(exist_ok=True)
    from blender_sequence import prepare_sequence, sequence_settings
    sequence_settings(settings)
    plates = None
    if settings.get('export_sequence', True) or settings.get('background_images'):
        plates = prepare_sequence(pipeline, result, settings)
    args = [settings['path'], '--background', '--disable-autoexec', '--python-exit-code', '1',
        '--python', APP / 'blender_import.py', '--', '--model', model, '--output', output,
        '--fps', str(settings['fps']), '--as-mesh', str(int(settings['as_mesh'])),
        '--draw-points', str(int(settings.get('draw_points', True))),
        '--suppress-distortion-warnings', str(int(settings['suppress_warnings'])),
        '--background-images', str(int(settings['background_images'])),
        '--point-radius', str(settings['point_radius'])]
    args += ['--frame-start-offset', str(int(settings.get('frame_start_offset', 0)))]
    if result.get('rotation_only'):
        args += ['--allow-empty-points']
    if plates:
        args += ['--images', plates[0], '--background-map', plates[1]]
    import uuid
    label = 'blender-import-' + uuid.uuid4().hex[:6]
    if result.get('layout_version') == 2:
        args += ['--report', pipeline.log_dir / (label + '.json')]
    # The same progress stream serves manual exports and queued automatic exports.
    callback = getattr(pipeline, 'callback', None)
    if callback:
        def receive(event):
            callback(event)
            if event['type'] == 'log':
                progress = progress_event(event['text'])
                if progress: callback(progress)
        pipeline.callback = receive
        callback(dict(type='blender_progress', text='正在启动 Blender，加载所选版本的设置…', percent=0))
    try:
        pipeline.command(args, label)
    finally:
        if callback: pipeline.callback = callback
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError(f'Blender 未生成有效文件：{output}')
    if plates:
        result['sequence'] = str(plates[0])
        result['images_dir'] = str(plates[0])
        from project_layout import result_report
        from json_store import atomic_write_json
        atomic_write_json(result_report(result), result)
    if not getattr(pipeline, 'defer_cache_cleanup', False):
        from blender_sequence import clean_solver_cache
        clean_solver_cache(pipeline, result)
    return str(output)
