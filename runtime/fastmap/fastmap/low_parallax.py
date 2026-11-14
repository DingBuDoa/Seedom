"""Low-parallax diagnostics for camera-motion reconstruction."""

from __future__ import annotations

import torch

from fastmap.point_pair import compute_ray_angle, image_pair_index


@torch.no_grad()
def analyze_parallax(point_pairs, point_pair_mask, image_pairs, num_images, max_samples=100000):
    """Estimate the triangulation-angle distribution from accepted matches.

    The angle is computed after relative rotation alignment. A very small
    distribution means the sequence is close to pure rotation, where metric
    depth is poorly constrained and focal estimates can become unstable.
    """
    image_idx1, image_idx2, image_pair_idx = image_pair_index(point_pairs, num_images)
    pair_keys = image_idx1 * num_images + image_idx2
    valid_keys = image_pairs.image_idx1 * num_images + image_pairs.image_idx2
    order = torch.argsort(valid_keys)
    sorted_keys = valid_keys[order]
    samples = []
    remaining = int(max_samples)
    for batch in point_pairs.query(point_pair_mask):
        selected = point_pair_mask[batch.point_pair_idx]
        if not torch.any(selected):
            continue
        global_indices = batch.point_pair_idx[selected]
        pair_indices = image_pair_idx[global_indices].long()
        keys = pair_keys[pair_indices]
        positions = torch.searchsorted(sorted_keys, keys)
        valid = positions < len(sorted_keys)
        if torch.any(valid):
            valid_positions = positions[valid]
            valid &= sorted_keys[valid_positions] == keys[valid]
        if not torch.any(valid):
            continue
        valid_positions = positions[valid]
        pair_indices = order[valid_positions]
        angles = compute_ray_angle(
            batch.xy1[selected][valid], batch.xy2[selected][valid],
            image_pairs.rotation[pair_indices]
        ).flatten()
        if len(angles) > remaining:
            angles = angles[:remaining]
        samples.append(angles.detach())
        remaining -= len(angles)
        if remaining <= 0:
            break
    if not samples:
        return {'samples': 0, 'median_deg': None, 'p90_deg': None, 'low_fraction': None, 'low': False}
    angles = torch.cat(samples)
    median = torch.quantile(angles, 0.5).item()
    p90 = torch.quantile(angles, 0.9).item()
    low_fraction = (angles < 1.0).float().mean().item()
    low = bool((p90 < 2.0 and low_fraction >= 0.65) or (median < 0.5 and low_fraction >= 0.8))
    return {
        'samples': int(angles.numel()),
        'median_deg': round(float(median), 4),
        'p90_deg': round(float(p90), 4),
        'low_fraction': round(float(low_fraction), 4),
        'low': low,
    }
