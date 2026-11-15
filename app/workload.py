"""Shared input metadata and cache accounting for execution and duration hints."""
from pathlib import Path
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess

from paths import BINARY_DIRS
from project_layout import cache_directory

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp', '.exr'}


def image_sort_key(path):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r'(\d+)', path.name)]


def source_signature(video, options):
    video = Path(video).resolve()
    if video.is_dir():
        files = sorted((path for path in video.iterdir()
                        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
                       key=image_sort_key)
        size = sum(path.stat().st_size for path in files)
        mtime_ns = max((path.stat().st_mtime_ns for path in files), default=video.stat().st_mtime_ns)
    else:
        stat = video.stat()
        files = None
        size, mtime_ns = stat.st_size, stat.st_mtime_ns
    signature = {'source': str(video), 'size': size, 'mtime_ns': mtime_ns,
                 'fps': float(options.fps), 'max_size': options.max_size, 'overlap': options.overlap}
    if files is not None:
        signature.update(input_type='image_sequence', image_count=len(files))
    # Preserve legacy cache identities when processing at the original size.
    if options.image_scale != 1:
        signature['image_scale'] = options.image_scale
    if getattr(options, 'initial_focal_length', 0):
        signature['initial_focal_length'] = options.initial_focal_length
    return signature


def video_scene(video, options):
    signature = source_signature(video, options)
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:8]
    return Path(options.output_root).resolve() / f'{Path(video).stem}_{key}'


def image_sequence_info(sequence):
    sequence = Path(sequence)
    if not sequence.is_dir():
        raise ValueError('图像序列目录不存在')
    files = sorted((path for path in sequence.iterdir()
                    if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
                   key=image_sort_key)
    if len(files) < 3:
        raise ValueError(f'图像序列至少需要 3 张图片，当前只有 {len(files)} 张')
    return {'frames': len(files), 'width': None, 'height': None, 'codec': 'image-sequence',
            'fps': 0, 'duration': 0, 'path': str(sequence.resolve())}


def video_info(video, fps):
    probe = subprocess.run([str(BINARY_DIRS['03 FFMPEG'] / 'ffprobe.exe'), '-v', 'error',
                            '-select_streams', 'v:0', '-show_entries',
                            'stream=nb_frames,avg_frame_rate,duration,width,height,codec_name:format=duration',
                            '-of', 'json', str(video)], capture_output=True, text=True, encoding='utf-8',
                           timeout=8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if probe.returncode:
        raise ValueError('无法读取视频信息')
    info = json.loads(probe.stdout)
    stream = info['streams'][0]
    def positive(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else 0
        except (ValueError, TypeError):
            return 0
    duration = positive(stream.get('duration')) or positive(info.get('format', {}).get('duration'))
    count = positive(stream.get('nb_frames'))
    numerator, _, denominator = str(stream.get('avg_frame_rate', '0/1')).partition('/')
    rate = positive(numerator) / (positive(denominator) or 1)
    if not duration and count and rate:
        duration = count / rate
    frames = duration * fps if fps else count or duration * rate
    if not math.isfinite(frames) or frames <= 0:
        raise ValueError('视频缺少有效帧数')
    return {'frames': frames, 'width': stream.get('width'), 'height': stream.get('height'),
            'codec': stream.get('codec_name'), 'fps': rate, 'duration': duration}


def describe_workload(source, options, existing_scene=False, resume_scene=None):
    # New video jobs own a fresh project; only an explicit resume can reuse it.
    scene = cache_directory(source) if existing_scene else (
        cache_directory(resume_scene) if resume_scene else Path(options.output_root) / '_new_job_')
    database = scene / 'database.db'
    ready = False
    images, pairs = 0, 0
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
            images = db.execute('SELECT COUNT(*) FROM images').fetchone()[0]
            pairs = db.execute('SELECT COUNT(*) FROM two_view_geometries WHERE rows>0').fetchone()[0]
            ready = existing_scene and pairs > 0
    work = {'extract': not existing_scene and not ((scene / '.frames-complete').exists()
                                                 and any((scene / 'images').glob('frame_*.jpg'))),
            'features': not ready and not ((scene / '.features-complete').exists() and database.is_file()),
            'matching': not ready and not (scene / '.matching-complete').exists()}
    if existing_scene:
        info = {'frames': images, 'width': None, 'height': None, 'codec': None}
    elif options.input_type == 'image_sequence':
        info = image_sequence_info(source)
    else:
        info = video_info(source, options.fps)
    return {**info, 'stages': work, 'existing_scene': existing_scene,
            'pairs': pairs if not work['matching'] else None}
