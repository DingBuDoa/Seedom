"""Isolated VGGT-Omega inference; GUI cancellation terminates this process tree."""
import argparse
import gc
import json
from pathlib import Path
import sys
import time

import numpy as np

from json_store import atomic_write_json
from vggt_geometry import (ModelWriter, load_images, depth_grid, fit_camera_similarity,
                           transform_camera)
from workload import image_sort_key, IMAGE_EXTENSIONS


def progress(phase, text, current=None, total=None):
    print('[SEEDOM_VGGT] ' + json.dumps(dict(phase=phase, text=text, current=current,
                                            total=total), ensure_ascii=False), flush=True)


def infer(model, paths, resolution, device):
    import torch
    from vggt_omega.utils.pose_enc import encoding_to_camera
    rgb, geometry = load_images(paths, resolution)
    images = torch.from_numpy(rgb).permute(0, 3, 1, 2).to(device=device, dtype=torch.float32) / 255.
    with torch.inference_mode():
        predictions = model(images)
        extrinsics, intrinsics = encoding_to_camera(predictions['pose_enc'], images.shape[-2:])
    result = (extrinsics[0].float().cpu().numpy(), intrinsics[0].float().cpu().numpy(),
              predictions['depth'][0, ..., 0].float().cpu().numpy(),
              predictions['depth_conf'][0].float().cpu().numpy(), rgb, geometry)
    return result


def check_overlap(cameras, previous, start, overlap, frame_indices=None):
    # An alignment with plausible points but flipped cameras is still unusable.
    angles, positions = [], []
    for j in range(overlap):
        index = frame_indices[j] if frame_indices is not None else start + j
        old, new = previous[index]['camera'], cameras[j]
        if not np.isfinite(old).all() or not np.isfinite(new).all():
            raise ValueError('VGGT 重叠相机包含无效数值，停止拼接。')
        cosine = np.clip((np.trace(old[:3, :3] @ new[:3, :3].T) - 1) / 2, -1, 1)
        angle = float(np.degrees(np.arccos(cosine)))
        if angle > 15:
            raise ValueError(f'VGGT 第 {index+1} 帧重叠相机方向不一致（{angle:.2f}°），已停止拼接；请拆分镜头或降低处理帧率。')
        cloud = previous[index]['points'][previous[index]['valid']]
        if len(cloud) < 30 or not np.isfinite(cloud).all():
            raise ValueError('VGGT 重叠帧有效深度点不足，停止拼接。')
        spread = np.median(np.linalg.norm(cloud - np.median(cloud, axis=0), axis=1))
        old_center = -old[:3, :3].T @ old[:3, 3]
        new_center = -new[:3, :3].T @ new[:3, 3]
        position = float(np.linalg.norm(new_center - old_center) / max(spread, 1e-8))
        if position > .15:
            raise ValueError(f'VGGT 第 {index+1} 帧重叠相机位置不一致（归一化偏差 {position:.3f}），已停止拼接；请拆分镜头或降低处理帧率。')
        angles.append(angle)
        positions.append(position)
    return dict(max_rotation_degrees=max(angles), max_position_error=max(positions))


def larger_overlap(shared, window, accepted, previous):
    """Retry with more known context, leaving at least one new frame per batch."""
    maximum = min(window - 1, accepted)
    while maximum > shared and any(i not in previous for i in range(accepted - maximum, accepted)):
        maximum -= 1
    if shared >= maximum:
        return None
    return min(maximum, max(4, shared * 2))


def registration_points(points, predicted_camera, exported_camera):
    """Refresh overlap depth context without changing any exported camera pose.

    New depth predictions belong to the newly approved context. Express their
    camera-space rays in the authoritative exported camera's coordinates rather
    than accumulating old, context-dependent depth shapes across retries.
    This proxy is used only for the next registration; no output is rewritten.
    """
    camera_points = points @ predicted_camera[:3, :3].T + predicted_camera[:3, 3]
    return (camera_points - exported_camera[:3, 3]) @ exported_camera[:3, :3]


def run(args):
    started = time.perf_counter()
    import torch
    sys.path.insert(0, str(Path(args.source).resolve()))
    from vggt_omega.models import VGGTOmega
    device = torch.device(args.device)
    if device.type == 'cuda':
        if not torch.cuda.is_available() or device.index >= torch.cuda.device_count():
            raise ValueError(f'所选 GPU {device.index} 不可用，请检查设置中的显卡选择。')
        torch.cuda.set_device(device)
        free, total_memory = torch.cuda.mem_get_info(device)
        if free < 5.5 * 2**30:
            raise ValueError('VGGT-Omega 当前可用显存不足 5.5 GiB，请关闭其他显存占用或使用 CPU。')
        # This uses the selected card, never the default GPU 0.
        if free < 8 * 2**30:
            args.window = min(args.window, 8)
            args.resolution = min(args.resolution, 384)
        print(f'[VGGT] GPU {device.index}: {torch.cuda.get_device_name(device)}, '
              f'free={free / 2**30:.1f} GiB, total={total_memory / 2**30:.1f} GiB', flush=True)
    else:
        args.window = min(args.window, 8)
        args.resolution = min(args.resolution, 384)
        torch.set_num_threads(min(4, torch.get_num_threads()))
        print('[VGGT] 使用 CPU 推理；1B 模型耗时可能较长。', flush=True)
    files = sorted((p for p in Path(args.images).iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=image_sort_key)
    if len(files) < 3:
        raise ValueError('VGGT-Omega 至少需要 3 张图像。')
    progress('omega_load', '加载本地 VGGT-Omega 权重')
    # mmap + meta + assign avoids allocating a second 4.6 GB model in RAM.
    state = torch.load(args.checkpoint, map_location='cpu', weights_only=True, mmap=True)
    with torch.device('meta'):
        model = VGGTOmega(autocast=device.type == 'cuda')
    model.load_state_dict(state, strict=True, assign=True)
    model = model.eval().to(device)
    del state
    gc.collect()
    print(f'[VGGT] images={len(files)}, resolution={args.resolution}, window={args.window}, overlap=4', flush=True)
    print('[VGGT] 多批结果是局部预测加重叠对齐，尚无全局 BA 或回环优化。建议先测试单镜头短片。', flush=True)
    overlap = 4
    preferred_overlap, stable_alignments, alignment_retries = overlap, 0, 0
    anchor = None
    tried_anchors = set()
    start, accepted, batches = 0, 0, 0
    previous = {}
    alignments = []
    point_limit = max(1, min(2000, 250000 // len(files)))
    with ModelWriter(args.output) as writer:
        while start < len(files):
            capacity = args.window - int(anchor is not None)
            end = min(start + capacity, len(files))
            batch_indices = ([anchor] if anchor is not None else []) + list(range(start, end))
            progress('omega_infer', f'推理第 {start + 1}–{end} 帧 / {len(files)} 帧', accepted, len(files))
            try:
                cameras, intrinsics, depths, confidence, rgb, geometry = infer(
                    model, [files[i] for i in batch_indices], args.resolution, device)
            except torch.OutOfMemoryError:
                # Keep the same resolution after the first batch so overlap
                # pixels remain actual correspondences. Never silently use CPU.
                gc.collect()
                if device.type == 'cuda':
                    torch.cuda.empty_cache()
                if args.window > overlap + 1:
                    args.window = max(overlap + 1, args.window // 2)
                    maximum = args.window - 1 - int(anchor is not None)
                    preferred_overlap = min(preferred_overlap, maximum)
                    start = max(0, accepted - min(accepted - start, maximum))
                    print(f'[VGGT] 显存不足，减小每批帧数至 {args.window}，重试当前批次。', flush=True)
                    continue
                if not accepted and args.resolution > 256:
                    args.resolution = 384 if args.resolution > 384 else 256
                    print(f'[VGGT] 显存不足，推理分辨率降至 {args.resolution}，重试。', flush=True)
                    continue
                raise RuntimeError('VGGT-Omega 显存不足；已尝试缩小批次，请关闭其他显存占用或使用 CPU。') from None
            if not np.isfinite(cameras).all() or not np.isfinite(intrinsics).all():
                raise ValueError('VGGT 相机预测包含无效数值，停止导出。')
            grids = [depth_grid(depths[j], confidence[j], intrinsics[j], camera)
                     for j, camera in enumerate(cameras)]
            shared = accepted - start
            shared_positions = [j for j, index in enumerate(batch_indices) if index < accepted]
            scale, rotation, translation = 1., np.eye(3), np.zeros(3)
            if shared:
                source_points, target_points = [], []
                for j in shared_positions:
                    new_points, new_valid, _ = grids[j]
                    old = previous[batch_indices[j]]
                    mask = new_valid & old['valid']
                    source_points.append(new_points[mask])
                    target_points.append(old['points'][mask])
                try:
                    scale, rotation, translation, error = fit_camera_similarity(
                        source_points, target_points, cameras[shared_positions],
                        [previous[batch_indices[j]]['camera'] for j in shared_positions])
                    aligned = np.stack([transform_camera(c, scale, rotation, translation) for c in cameras])
                    checks = check_overlap(aligned[shared_positions], previous, start, len(shared_positions),
                                           [batch_indices[j] for j in shared_positions])
                except ValueError as exc:
                    retry_overlap = larger_overlap(shared, capacity, accepted, previous)
                    candidates = [i for i in sorted(previous) if i < start and i not in tried_anchors]
                    if retry_overlap is None and candidates and len(tried_anchors) < 2:
                        # Preserve the previous context's first reference image,
                        # dropping an interior frame to stay within the same GPU
                        # batch budget. It is an anchor, never an extra export.
                        anchor = candidates[0]
                        tried_anchors.add(anchor)
                        retry_overlap = min(args.window - 2, accepted - anchor - 1)
                        print(f'[VGGT] 保留第 {anchor+1} 帧作为连接参考，重试当前边界。', flush=True)
                    if retry_overlap is None:
                        raise ValueError(f'VGGT 在第 {accepted+1} 帧连接失败'
                                         f'（重叠参考第 {start+1}–{accepted} 帧）：{exc} '
                                         '已尝试增加重叠范围，仍无法可靠连接；请拆分镜头或降低处理帧率。') from exc
                    start = accepted - retry_overlap
                    alignment_retries += 1
                    print(f'[VGGT] 重叠对齐未通过：{exc} 增加重叠至 {retry_overlap} 帧，自动重试；未写入当前批次。', flush=True)
                    progress('omega_infer', f'自动重试：增加至 {retry_overlap} 帧重叠', accepted, len(files))
                    del depths, confidence, rgb, grids, cameras, intrinsics
                    continue
                cameras = aligned
                stable_alignments = stable_alignments + 1 if error < .04 else 0
                preferred_overlap = max(overlap, shared // 2) if stable_alignments >= 2 else max(overlap, shared)
                if stable_alignments >= 2:
                    stable_alignments = 0
                alignments.append(dict(frame=start + 1, scale=scale, normalized_error=error,
                                       overlap=len(shared_positions), anchor=anchor + 1 if anchor is not None else None, **checks))
                print(f'[VGGT] 相机约束重叠对齐：frame={start + 1}, scale={scale:.5f}, '
                      f'error={error:.5f}, rotation={checks["max_rotation_degrees"]:.3f}°, '
                      f'position={checks["max_position_error"]:.5f}', flush=True)
            for j, (points, valid, pixels) in enumerate(grids):
                points = scale * (points @ rotation.T) + translation
                index = batch_indices[j]
                # With an OOM-reduced window some retained frames are shared.
                # Keep their already-exported poses rather than replacing them
                # with this window's slightly different overlap predictions.
                if index >= accepted:
                    previous[index] = dict(points=points, valid=valid, camera=cameras[j])
                if index < accepted:
                    old = previous[index]
                    old['points'] = registration_points(points, cameras[j], old['camera'])
                    old['valid'] = valid
                    continue
                indices = np.flatnonzero(valid)
                if len(indices) > point_limit:
                    indices = indices[np.linspace(0, len(indices) - 1, point_limit, dtype=int)]
                indices = indices[:max(0, 250000 - writer.points)]
                y, x = (axis[indices] for axis in pixels)
                colors = rgb[j, y, x] if args.color else np.full((len(x), 3), 200, dtype=np.uint8)
                writer.add_frame(files[index].name, cameras[j], geometry[j], intrinsics[j],
                                 points[indices], colors, (x, y))
            accepted, batches = end, batches + 1
            tried_anchors = {anchor} if anchor is not None else set()
            progress('omega_infer', f'已处理 {accepted} / {len(files)} 帧', accepted, len(files))
            del depths, confidence, rgb, grids, cameras, intrinsics
            if end == len(files):
                break
            start = end - min(preferred_overlap, args.window - 1 - int(anchor is not None))
            # Retain one bounded window for a context retry. Already-exported
            # shared poses remain authoritative; registration depths use the
            # last approved context without rewriting any exported points.
            previous = {k: v for k, v in previous.items() if k >= end - args.window or k == anchor}
        progress('omega_write', '保存相机、彩色点云与导出模型')
    metrics = dict(registered_images=writer.images, points=writer.points,
                   observations=writer.points, predicted_depth=True)
    if writer.points < 10:
        raise ValueError('VGGT 有效深度点不足，无法生成可用模型。')
    atomic_write_json(Path(args.output) / 'vggt-report.json', dict(
        metrics=metrics, device=str(device), window=args.window, resolution=args.resolution,
        batches=batches, overlap=overlap, alignments=alignments, alignment_method='camera-anchored-depth-scale',
        alignment_retries=alignment_retries, adaptive_overlap=True,
        registration_context='rolling-depth-proxy-fixed-exported-poses',
        peak_gpu_allocated_bytes=torch.cuda.max_memory_allocated(device) if device.type == 'cuda' else None,
        mapping_wall_seconds=time.perf_counter() - started,
        model='VGGT-Omega-1B-512', upstream_revision='48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6',
        point_kind='confidence-filtered single-view depth samples',
        limitations='Arbitrary scale; overlapping-window alignment without global BA or loop closure.'))
    print(f'[VGGT] 完成：{writer.images} 个相机，{writer.points} 个深度采样点。', flush=True)


def main():
    parser = argparse.ArgumentParser()
    for name in ('source', 'checkpoint', 'images', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--device', default='cuda:0')
    parser.add_argument('--resolution', type=int, choices=(256, 384, 512), default=512)
    parser.add_argument('--window', type=int, choices=range(5, 25), default=12)
    parser.add_argument('--color', type=int, choices=(0, 1), default=1)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:
        print(f'[VGGT_ERROR] {type(exc).__name__}: {exc}', flush=True)
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
