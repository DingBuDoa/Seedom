"""Prepare a safe, segmented-camera COLMAP database for variable focal length.

The normal pipeline keeps one camera for all video frames.  When automatic
focal length is enabled we clone that camera for short, consecutive frame
segments and point each segment's images at its own camera record.  FastMap
and GLOMAP can then optimize one focal length per segment while the original
database remains untouched for retries and for the legacy fixed-focal path.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import statistics
import re
import sqlite3


FRAME_NAME = re.compile(r"(?:^|[/\\])frame_(\d+)", re.IGNORECASE)


def _frame_number(name: str, fallback: int) -> int:
    match = FRAME_NAME.search(name)
    return int(match.group(1)) if match else fallback


def _cut_boundaries(image_rows, images_dir):
    """Return frame indices where a hard visual cut is likely.

    This deliberately uses a conservative low-resolution luminance change
    test.  Ordinary camera motion and zooms usually change gradually; a hard
    cut produces a much larger adjacent-frame difference.  The result is a
    hint for database grouping, never a replacement for user review.
    """
    if images_dir is None:
        return []
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return []
    samples = []
    for _, _, name in image_rows:
        path = Path(images_dir) / name
        try:
            with Image.open(path) as image:
                current = image.convert('L').resize((32, 18), Image.Resampling.BILINEAR)
                samples.append(current.copy())
        except (OSError, ValueError):
            samples.append(None)
    differences = []
    for index in range(1, len(samples)):
        previous, current = samples[index - 1], samples[index]
        if previous is None or current is None:
            differences.append(0.0)
            continue
        differences.append(ImageStat.Stat(ImageChops.difference(previous, current)).mean[0] / 255.0)
    if len(differences) < 3:
        return []
    median = statistics.median(differences)
    mad = statistics.median(abs(value - median) for value in differences) or 0.01
    threshold = max(0.35, median + 6.0 * mad)
    boundaries = []
    for index, value in enumerate(differences, 1):
        if value < threshold:
            continue
        if boundaries and index - boundaries[-1] < 5:
            if value > differences[boundaries[-1] - 1]:
                boundaries[-1] = index
            continue
        boundaries.append(index)
    return boundaries


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a SQLite database with its WAL contents, if any, consistently."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    if temporary.exists():
        temporary.unlink()
    source_db = sqlite3.connect(str(source))
    destination_db = sqlite3.connect(str(temporary))
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()
    temporary.replace(destination)


def _detach_rig_metadata(db: sqlite3.Connection) -> bool:
    """Remove stale single-camera rig records from a derived database.

    COLMAP creates a rig/frame pair even for a normal single-camera video.
    Once images are reassigned to cloned temporal cameras, that rig only
    describes camera 1.  GLOMAP then aborts when it sees another camera ID
    in a frame (for example: ``Sensor (CAMERA, 14) not found in the rig``).
    The derived database does not need rig metadata: without it GLOMAP treats
    each image camera as an ordinary camera, while the source database stays
    untouched for retries and the fixed-focal path.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    # Delete children first in case a bundled SQLite build enforces FKs.
    present = False
    for table in ('rig_sensors', 'frames', 'rigs'):
        if table in tables:
            db.execute(f'DELETE FROM {table}')
            present = True
    return present


def prepare_auto_focal_database(database, output, report_path=None, target_frames=30,
                                images_dir=None, detect_cuts=False):
    """Create a segmented-camera database and return a JSON-compatible report.

    ``target_frames`` is a regularization interval, not a frame extraction
    setting. Every original camera is preserved and each segment receives a
    camera with the same initial intrinsics. When ``detect_cuts`` is enabled,
    conservative visual cut boundaries are used in addition to the interval.
    ``images.camera_id`` changes in the derived database and stale single-
    camera rig/frame metadata is detached so GLOMAP can load the segments.
    """
    source = Path(database).resolve()
    destination = Path(output).resolve()
    if source == destination:
        raise ValueError('自动焦距数据库不能覆盖原始 database.db')
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        target_frames = max(5, int(target_frames))
    except (TypeError, ValueError):
        target_frames = 30

    _copy_database(source, destination)
    report = {
        'source': str(source),
        'database': str(destination),
        'target_frames': target_frames,
        'segments': [],
        'images': 0,
        'cameras_before': 0,
        'cameras_after': 0,
        'detect_cuts': bool(detect_cuts),
        'cut_boundaries': [],
        'rig_metadata_detached': False,
    }
    with sqlite3.connect(str(destination)) as db:
        # The original database contains one implicit rig for camera 1.  It
        # cannot describe the cloned cameras created below, so detach it
        # before GLOMAP loads the derived database.
        report['rig_metadata_detached'] = _detach_rig_metadata(db)
        cameras = {
            row[0]: row for row in db.execute(
                'SELECT camera_id, model, width, height, params, prior_focal_length FROM cameras ORDER BY camera_id'
            )
        }
        report['cameras_before'] = len(cameras)
        grouped = defaultdict(list)
        rows = db.execute(
            'SELECT image_id, name, camera_id FROM images ORDER BY camera_id, name, image_id'
        ).fetchall()
        for fallback, row in enumerate(rows):
            grouped[row[2]].append((fallback, row[0], row[1]))

        next_segment = 0
        for camera_id, image_rows in grouped.items():
            if camera_id not in cameras:
                raise RuntimeError(f'images 引用了不存在的 camera_id：{camera_id}')
            image_rows.sort(key=lambda item: (_frame_number(item[2], item[0]), item[2], item[1]))
            cut_points = _cut_boundaries(image_rows, images_dir) if detect_cuts else []
            report['cut_boundaries'].extend(cut_points)
            boundaries = sorted(set([0, *cut_points, len(image_rows)]))
            segments = []
            for begin, end in zip(boundaries, boundaries[1:]):
                shot = image_rows[begin:end]
                for offset in range(0, len(shot), target_frames):
                    segments.append(shot[offset:offset + target_frames])
            for segment_index, segment in enumerate(segments):
                if not segment:
                    continue
                original = cameras[camera_id]
                # Reuse the original ID for the first segment.  COLMAP and
                # FastMap expect the image-referenced camera IDs to start at
                # 1; cloning every segment would leave camera 1 unused.
                if segment_index == 0:
                    segmented_camera_id = camera_id
                else:
                    cursor = db.execute(
                        'INSERT INTO cameras (model, width, height, params, prior_focal_length) VALUES (?, ?, ?, ?, ?)',
                        original[1:]
                    )
                    segmented_camera_id = cursor.lastrowid
                db.executemany(
                    'UPDATE images SET camera_id = ? WHERE image_id = ?',
                    [(segmented_camera_id, row[1]) for row in segment]
                )
                report['segments'].append({
                    'index': next_segment,
                    'source_camera_id': camera_id,
                    'camera_id': segmented_camera_id,
                    'first_image': segment[0][2],
                    'last_image': segment[-1][2],
                    'image_count': len(segment),
                })
                next_segment += 1
        db.commit()

        report['images'] = len(rows)
        report['cameras_after'] = db.execute('SELECT COUNT(*) FROM cameras').fetchone()[0]

    if report_path is not None:
        report_file = Path(report_path)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
