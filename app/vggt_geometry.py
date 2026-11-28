"""VGGT geometry and standard COLMAP exports, independent of torch/CUDA."""
from pathlib import Path
import struct

import numpy as np
from PIL import Image


def image_geometry(width, height, resolution):
    """Match upstream max_size preprocessing, retaining the crop transform."""
    crop_w, crop_h = width, height
    if height / width < .5:
        crop_w = min(width, max(1, round(height / .5)))
    elif height / width > 2:
        crop_h = min(height, max(1, round(width * 2)))
    left, top = (width - crop_w) // 2, (height - crop_h) // 2
    if crop_h >= crop_w:
        target_h, target_w = resolution, max(16, round(resolution * crop_w / crop_h / 16) * 16)
    else:
        target_w, target_h = resolution, max(16, round(resolution * crop_h / crop_w / 16) * 16)
    return (left, top, crop_w, crop_h), (target_w, target_h)


def load_images(paths, resolution):
    """PIL -> float RGB, equivalent to upstream ToTensor without torchvision."""
    arrays, geometry = [], []
    for path in paths:
        with Image.open(path) as source:
            if source.mode == 'RGBA':
                source = Image.alpha_composite(Image.new('RGBA', source.size, 'white'), source)
            image = source.convert('RGB')
        width, height = image.size
        crop, target = image_geometry(width, height, resolution)
        left, top, crop_w, crop_h = crop
        image = image.crop((left, top, left + crop_w, top + crop_h))
        image = image.resize(target, Image.Resampling.BICUBIC)
        arrays.append(np.asarray(image).copy())
        geometry.append((width, height, crop, target))
    if len({a.shape for a in arrays}) != 1:
        raise ValueError('VGGT 图像序列需要一致的画面比例；请先统一图片尺寸。')
    return np.stack(arrays), geometry


def original_intrinsics(intrinsics, geometry):
    width, height, (left, top, crop_w, crop_h), (target_w, target_h) = geometry
    fx, fy = intrinsics[0, 0] * crop_w / target_w, intrinsics[1, 1] * crop_h / target_h
    cx = intrinsics[0, 2] * crop_w / target_w + left
    cy = intrinsics[1, 2] * crop_h / target_h + top
    values = np.array([fx, fy, cx, cy], dtype=np.float64)
    if not np.isfinite(values).all() or min(fx, fy) <= 0:
        raise ValueError('VGGT 预测了无效焦距，请检查输入素材。')
    return width, height, values


def depth_grid(depth, confidence, intrinsics, extrinsic, step=8):
    height, width = depth.shape
    y, x = np.mgrid[0:height:step, 0:width:step]
    z, conf = depth[::step, ::step], confidence[::step, ::step]
    finite = np.isfinite(z) & (z > 1e-6) & np.isfinite(conf)
    if finite.sum() < 30:
        raise ValueError('VGGT 深度预测有效点不足。')
    valid = finite & (conf >= np.quantile(conf[finite], .5))
    camera = np.stack(((x - intrinsics[0, 2]) * z / intrinsics[0, 0],
                       (y - intrinsics[1, 2]) * z / intrinsics[1, 1], z), axis=-1)
    world = (camera - extrinsic[:3, 3]) @ extrinsic[:3, :3]
    valid &= np.isfinite(world).all(axis=-1)
    return world.reshape(-1, 3), valid.ravel(), (y.ravel(), x.ravel())


def fit_similarity(source, target):
    """Robust Sim(3) from corresponding overlap depth samples; reject degeneracy."""
    source, target = np.asarray(source, np.float64), np.asarray(target, np.float64)
    finite = np.isfinite(source).all(1) & np.isfinite(target).all(1)
    a, b = source[finite], target[finite]
    if len(a) < 30:
        raise ValueError('重叠帧有效对应点不足，无法连接 VGGT 分批结果；请分段处理。')
    keep = np.ones(len(a), bool)
    for _ in range(5):
        x, y = a[keep], b[keep]
        am, bm = x.mean(0), y.mean(0)
        xc, yc = x - am, y - bm
        variance = np.sum(xc * xc) / len(x)
        u, singular, vt = np.linalg.svd(yc.T @ xc / len(x))
        if variance < 1e-12 or singular[1] < max(singular[0] * 1e-6, 1e-12):
            raise ValueError('VGGT 重叠区域退化，无法确定尺度；请增加有效画面或分段处理。')
        signs = np.ones(3)
        signs[-1] = np.linalg.det(u @ vt)
        rotation = (u * signs) @ vt
        scale = float(np.dot(singular, signs) / variance)
        translation = bm - scale * rotation @ am
        residuals = np.linalg.norm(scale * (a @ rotation.T) + translation - b, axis=1)
        keep = residuals <= max(np.quantile(residuals, .8), 1e-9)
    spread = np.median(np.linalg.norm(b - np.median(b, axis=0), axis=1))
    error = float(np.median(residuals) / max(spread, 1e-9))
    if not np.isfinite(scale) or scale <= 0 or not np.isfinite(error) or error > .12:
        raise ValueError(f'VGGT 分批对齐不可靠（归一化误差 {error:.3f}）；请降低帧率或拆分镜头。')
    return scale, rotation, translation, error


def fit_camera_similarity(source_points, target_points, source_cameras, target_cameras):
    """Align shared camera poses, estimating scale from their predicted depths.

    Depth predictions can change shape with the window context. Fitting their
    clouds alone may rotate/translate an otherwise consistent camera trajectory.
    Shared camera orientations fix rotation; their centers fix translation.
    Robust corresponding ray depths supply scale even for stationary cameras.
    This changes one common world coordinate system, never individual poses.
    """
    if not (len(source_points) == len(target_points) == len(source_cameras) == len(target_cameras)):
        raise ValueError('VGGT 重叠帧数据数量不一致。')
    if len(source_cameras) < 2:
        raise ValueError('VGGT 相机对齐至少需要两帧重叠。')
    source_cameras, target_cameras = np.asarray(source_cameras), np.asarray(target_cameras)
    if not np.isfinite(source_cameras).all() or not np.isfinite(target_cameras).all():
        raise ValueError('VGGT 重叠相机包含无效数值。')
    rotations = np.array([b[:3, :3].T @ a[:3, :3]
                          for a, b in zip(source_cameras, target_cameras)])
    # Project the orientation consensus onto SO(3), excluding reflections.
    u, _, vt = np.linalg.svd(rotations.mean(axis=0))
    rotation = u @ np.diag([1., 1., np.linalg.det(u @ vt)]) @ vt
    source_centers = np.array([-c[:3, :3].T @ c[:3, 3] for c in source_cameras])
    target_centers = np.array([-c[:3, :3].T @ c[:3, 3] for c in target_cameras])
    sources, targets, frame_scales = [], [], []
    for a, b, ca, cb in zip(source_points, target_points, source_centers, target_centers):
        a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
        finite = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1)
        a, b = a[finite], b[finite]
        rays_a, rays_b = (a - ca) @ rotation.T, b - cb
        denominator = np.sum(rays_a * rays_a, axis=1)
        valid = denominator > 1e-12
        ratios = np.sum(rays_a[valid] * rays_b[valid], axis=1) / denominator[valid]
        ratios = ratios[np.isfinite(ratios) & (ratios > 0)]
        if len(ratios) < 30:
            raise ValueError('VGGT 重叠帧有效深度对应点不足，无法确定尺度。')
        frame_scales.append(np.median(ratios))
        sources.append(a)
        targets.append(b)
    scale = float(np.median(frame_scales))
    translation = np.median(target_centers - scale * (source_centers @ rotation.T), axis=0)
    a, b = np.concatenate(sources), np.concatenate(targets)
    residuals = np.linalg.norm(scale * (a @ rotation.T) + translation - b, axis=1)
    spread = np.median(np.linalg.norm(b - np.median(b, axis=0), axis=1))
    error = float(np.median(residuals) / max(spread, 1e-9))
    if not np.isfinite(error) or error > .12:
        raise ValueError(f'VGGT 相机约束对齐不可靠（归一化误差 {error:.3f}）。')
    return scale, rotation, translation, error


def transform_camera(extrinsic, scale, rotation, translation):
    # X_global = s * Q * X_local + b; OpenCV camera-from-world convention.
    output = np.array(extrinsic, dtype=np.float64, copy=True)
    output[:3, :3] = extrinsic[:3, :3] @ rotation.T
    output[:3, 3] = scale * extrinsic[:3, 3] - output[:3, :3] @ translation
    return output


def rotation_quaternion(matrix):
    """Stable wxyz quaternion, including 180-degree rotations (COLMAP convention)."""
    r = np.asarray(matrix, np.float64)
    k = np.array([
        [r[0, 0]-r[1, 1]-r[2, 2], r[0, 1]+r[1, 0], r[0, 2]+r[2, 0], r[2, 1]-r[1, 2]],
        [r[0, 1]+r[1, 0], r[1, 1]-r[0, 0]-r[2, 2], r[1, 2]+r[2, 1], r[0, 2]-r[2, 0]],
        [r[0, 2]+r[2, 0], r[1, 2]+r[2, 1], r[2, 2]-r[0, 0]-r[1, 1], r[1, 0]-r[0, 1]],
        [r[2, 1]-r[1, 2], r[0, 2]-r[2, 0], r[1, 0]-r[0, 1], r.trace()],
    ]) / 3
    _, vectors = np.linalg.eigh(k)
    q = vectors[:, -1][[3, 0, 1, 2]]
    return -q if q[0] < 0 else q


class ModelWriter:
    """Stream bounded per-frame points; never keep a video's dense depths in RAM.

    Predicted depth samples are single-view points, not triangulated tracks.
    Observation coordinates are real projections and their reprojection error is
    deliberately absent from the summary: zero would imply measured accuracy.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.points = 0
        self.images = 0
        self.streams = []

    def __enter__(self):
        try:
            for name, mode in [('cameras.txt', 'w'), ('images.txt', 'w'), ('points3D.txt', 'w'),
                               ('cameras.bin', 'wb'), ('images.bin', 'wb'), ('points3D.bin', 'wb'),
                               ('points.rgb', 'wb')]:
                stream = (self.directory / name).open(mode, **({'encoding': 'utf-8'} if mode == 'w' else {}))
                self.streams.append(stream)
            for stream in self.streams[:3]:
                stream.write('# VGGT-Omega predicted cameras / single-view depth samples\n')
            for stream in self.streams[3:6]:
                stream.write(struct.pack('<Q', 0))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def add_frame(self, name, extrinsic, geometry, intrinsics, points, colors, pixels):
        self.images += 1
        image_id = self.images
        width, height, parameters = original_intrinsics(intrinsics, geometry)
        q, t = rotation_quaternion(extrinsic[:3, :3]), extrinsic[:3, 3]
        ct, it, pt, cb, ib, pb, rgb = self.streams
        ct.write(f'{image_id} PINHOLE {width} {height} ' + ' '.join(map(str, parameters)) + '\n')
        cb.write(struct.pack('<iiQQ4d', image_id, 1, width, height, *parameters))
        it.write(f'{image_id} ' + ' '.join(map(str, [*q, *t])) + f' {image_id} {name}\n')
        ib.write(struct.pack('<i7di', image_id, *q, *t, image_id))
        ib.write(name.encode('utf-8') + b'\0' + struct.pack('<Q', len(points)))
        (_, _, (left, top, crop_w, crop_h), (target_w, target_h)) = geometry
        x, y = pixels
        x = x * crop_w / target_w + left
        y = y * crop_h / target_h + top
        observations = []
        for index, (point, color, px, py) in enumerate(zip(points, colors, x, y)):
            self.points += 1
            point_id = self.points
            ib.write(struct.pack('<ddq', float(px), float(py), point_id))
            pb.write(struct.pack('<Q3d3BdQii', point_id, *point, *color, 0., 1, image_id, index))
            rgb.write(struct.pack('<3f3B', *point, *color))
            pt.write(f'{point_id} ' + ' '.join(map(str, [*point, *color])) + f' 0 {image_id} {index}\n')
            observations.append(f'{px} {py} {point_id}')
        it.write(' '.join(observations) + '\n')

    def __exit__(self, kind, value, traceback):
        for stream in self.streams:
            stream.close()
        if kind is None and len(self.streams) == 7:
            for name, count in [('cameras.bin', self.images), ('images.bin', self.images), ('points3D.bin', self.points)]:
                with (self.directory / name).open('r+b') as stream:
                    stream.write(struct.pack('<Q', count))
            with (self.directory / 'points3D.ply').open('wb') as stream:
                stream.write(('ply\nformat binary_little_endian 1.0\nelement vertex ' + str(self.points) +
                              '\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\n'
                              'property uchar green\nproperty uchar blue\nend_header\n').encode('ascii'))
                with (self.directory / 'points.rgb').open('rb') as data:
                    import shutil
                    shutil.copyfileobj(data, stream)
            (self.directory / 'points.rgb').unlink()
