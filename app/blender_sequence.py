"""Persistent Blender plates, independent from disposable solver images."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from json_store import atomic_write_json
from project_layout import result_directory, result_report

SEQUENCE_FORMATS = (('JPG · 体积小', 'jpg'), ('PNG · 无损', 'png'),
                    ('WebP · 无损', 'webp'), ('TIFF · 无损', 'tif'))
SEQUENCE_SIZES = (('原始尺寸', 'original'), ('原始尺寸的 1/2', 'half'),
                  ('原始尺寸的 1/4', 'quarter'), ('原始尺寸的 1/8', 'eighth'),
                  ('最高 2160p', '2160'), ('最高 1440p', '1440'),
                  ('最高 1080p', '1080'), ('最高 720p', '720'), ('最高 480p', '480'))


def sequence_settings(settings):
    fmt = settings.get('sequence_format', 'jpg')
    size = settings.get('sequence_resolution', 'original')
    if fmt not in dict((value, title) for title, value in SEQUENCE_FORMATS):
        raise ValueError('未知 Blender 序列帧格式。')
    if size not in dict((value, title) for title, value in SEQUENCE_SIZES):
        raise ValueError('未知 Blender 序列帧尺寸。')
    return fmt, size


def resize_filter(size):
    if size == 'original':
        return ''
    if size in ('half', 'quarter', 'eighth'):
        factor = {'half': 2, 'quarter': 4, 'eighth': 8}[size]
        return f'scale=max(1\\,trunc(iw/{factor})):max(1\\,trunc(ih/{factor}))'
    return f'scale=-1:min(ih\\,{int(size)})'


def prepare_sequence(pipeline, result, settings):
    """Reuse a completed plate variant, or recreate it from the original input.

    Variants never overwrite plates used by an earlier .blend export. All
    conversions complete in a private staging directory before being published.
    """
    from tracker_core import find_binary, IMAGE_EXTENSIONS
    from workload import image_sort_key
    fmt, size = sequence_settings(settings)
    project = result_directory(result).resolve()
    metadata = result.get('source_settings') or {}
    manifest = project / '_cache/source.json'
    if not metadata and manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
    identity = dict(source=metadata or result.get('source'), model=result['model'], format=fmt, size=size)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    target = project / 'video' / f'{fmt}-{size}-{key}'
    ready = target / 'sequence.json'
    if ready.is_file():
        data = json.loads(ready.read_text(encoding='utf-8'))
        if data.get('images') and all((target / name).is_file() for name in data['images'].values()):
            return target, ready
        raise ValueError(f'背景序列不完整，请恢复或移走此目录后重新导出：{target}')
    if target.exists():
        raise ValueError(f'背景序列缺少完成记录，请移走此目录后重新导出：{target}')

    source = Path(metadata.get('source') or result.get('source') or '')
    cached = Path(result['images_dir']) if result.get('images_dir') else None
    input_type = metadata.get('input_type', result.get('input_type', 'video'))
    cached_input = False
    if not source.exists():
        if (cached and cached.is_dir() and cached.resolve() == project / '_cache/images'
                and metadata.get('image_scale', result.get('image_scale', 1)) == 1):
            source = cached
            cached_input = True
        else:
            raise FileNotFoundError('生成 Blender 背景序列需要原素材。请恢复原视频/图像序列，或关闭序列帧和背景。')
    if not cached_input and metadata.get('size') is not None and source.is_file():
        stat = source.stat()
        if stat.st_size != metadata['size'] or stat.st_mtime_ns != metadata.get('mtime_ns', stat.st_mtime_ns):
            raise ValueError('原视频已更改，无法保证背景与相机轨迹同步，请恢复解算时的原素材。')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ('.staging-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        pipeline.check_cancel()
        pipeline.emit('blender_progress', percent=0, text='正在准备 Blender 背景序列…')
        args = [find_binary('03 FFMPEG', 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-stats', '-y']
        names = None
        if source.is_dir():
            if not cached_input and input_type != 'image_sequence':
                raise ValueError('原视频路径无效，无法生成 Blender 背景序列。')
            files = sorted((p for p in source.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=image_sort_key)
            if not files:
                raise ValueError('原图像序列为空。')
            names = [p.name for p in files]
            listing = staging / 'input.ffconcat'
            # FFmpeg concat escaping is distinct from shell quoting.
            listing.write_text('ffconcat version 1.0\n' + ''.join(
                "file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n" for p in files), encoding='utf-8')
            args += ['-f', 'concat', '-safe', '0', '-i', listing]
        else:
            args += ['-i', source]
        args += ['-map', '0:v:0', '-an']
        filters = []
        fps = metadata.get('fps', result.get('sampling_fps', 0))
        if not source.is_dir() and fps:
            filters.append(f'fps={float(fps):g}')
        scaling = resize_filter(size)
        if scaling:
            filters.append(scaling)
        if filters:
            args += ['-vf', ','.join(filters)]
        if source.is_dir():
            args += ['-fps_mode', 'passthrough']
        if fmt == 'jpg':
            args += ['-q:v', '2']
        elif fmt == 'png':
            args += ['-compression_level', '3']
        elif fmt == 'webp':
            args += ['-c:v', 'libwebp', '-lossless', '1']
        elif fmt == 'tif':
            args += ['-compression_algo', 'deflate']
        pipeline.command(args + [staging / ('frame_%06d.' + fmt)], '06-blender-sequence-' + key)
        pipeline.check_cancel()
        files = sorted(staging.glob('frame_*.' + fmt))
        if not files or (names is not None and len(files) != len(names)):
            raise RuntimeError('Blender 背景序列不完整。')
        if names is None:
            names = [f'frame_{i:06d}.jpg' for i in range(1, len(files) + 1)]
        data = dict(format=fmt, resolution=size, images=dict(zip(names, [p.name for p in files])))
        listing = staging / 'input.ffconcat'
        listing.unlink(missing_ok=True)
        atomic_write_json(staging / 'sequence.json', data)
        staging.rename(target)
        return target, ready
    finally:
        if staging.is_dir():
            shutil.rmtree(staging)


def clean_solver_cache(pipeline, result):
    """Remove only application-owned heavy cache after every required export succeeds."""
    if (result.get('layout_version') != 2 or result.get('status') != 'complete'
            or result.get('cache_policy') != 'discard_after_exports_v1'):
        return
    from project_library import model_ready
    if not model_ready(result):
        return
    project = result_directory(result).resolve()
    cache = project / '_cache'
    if not cache.is_dir() or cache.is_symlink() or cache.is_junction():
        return
    # Never remove a source/model/plate path, even for a malformed legacy result.
    protected = [Path(result[k]).resolve() for k in ('source', 'model', 'sequence') if result.get(k)]
    if result.get('source_settings', {}).get('source'):
        protected.append(Path(result['source_settings']['source']).resolve())
    removed = []
    for child in cache.iterdir():
        if child.name in ('source.json', 'result.json'):
            continue
        resolved = child.resolve()
        if any(p == resolved or resolved in p.parents for p in protected):
            continue
        if child.is_symlink() or child.is_junction():
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed.append(child.name)
        except OSError as error:
            pipeline.emit('log', text=f'[缓存清理] {child.name} 暂未删除：{error}\n')
    if result.get('images_dir') and not Path(result['images_dir']).is_dir():
        result['images_dir'] = ''
    result['cache_cleaned'] = True
    atomic_write_json(result_report(result), result)
    if removed:
        pipeline.emit('log', text='[缓存清理] 已清理计算图像、数据库及中间结果，保留模型、日志和导出素材。\n')
