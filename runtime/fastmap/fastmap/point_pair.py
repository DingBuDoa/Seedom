from loguru import logger
import typing
import math
import torch
import torch.nn.functional as F

from fastmap.container import PointPairs, Tracks
from fastmap.utils import to_homogeneous
from fastmap.memory_policy import current_limit


@torch.no_grad()
def point_pairs_from_tracks(tracks: Tracks) -> PointPairs:
    """Get all the possible point pairs implied by the tracks
    Args:
        tracks: Tracks container
    Returns:
        point_pairs: PointPairs container
    """
    device = tracks.device
    assert torch.all(tracks.track_idx[1:] >= tracks.track_idx[:-1])
    num_point_pairs = int((tracks.track_size * (tracks.track_size - 1) // 2).sum())
    # Observation indices need 32 bits for ordinary scenes, not 64. This is an
    # exact integer representation, with an explicit overflow-safe fallback.
    index_dtype = (torch.int32 if tracks.num_points2d <= 2**31 else torch.int64)
    point_idx1 = torch.empty(num_point_pairs, dtype=index_dtype, device=device)
    point_idx2 = torch.empty_like(point_idx1)
    pointer = 0
    batch_size = current_limit('sparse_batch', device)
    for ts in sorted(tracks.track_size.unique().cpu().tolist()):
        if ts < 2:
            continue
        starts = tracks.track_start[tracks.track_size == ts]
        # Same lexicographic ordering as combinations(), without its cubic
        # intermediate or repeating every track's observations per combination.
        comb = torch.triu_indices(ts, ts, offset=1, device=device)
        per_track = comb.shape[1]
        count = len(starts) * per_track
        for offset in range(0, count, batch_size):
            flat = torch.arange(offset, min(offset + batch_size, count), device=device)
            start = starts[flat // per_track]
            which = flat % per_track
            end = pointer + len(flat)
            point_idx1[pointer:end] = (start + comb[0, which]).to(index_dtype)
            point_idx2[pointer:end] = (start + comb[1, which]).to(index_dtype)
            pointer = end
        logger.info(f"Found {count} point pairs for {len(starts)} tracks of size {ts}.")
    assert pointer == num_point_pairs
    result = PointPairs(
        xy_pixels=tracks.xy_pixels, xy=tracks.xy, image_idx=tracks.image_idx,
        keypoint_idx=tracks.keypoint_idx, track_idx=tracks.track_idx,
        point_idx1=point_idx1, point_idx2=point_idx2,
    )
    logger.info(f"Found {num_point_pairs} point pairs in total from {tracks.num_tracks} tracks; "
                f"compact indices, bounded geometry batches (up to {batch_size}).")
    return result


@torch.no_grad()
def image_pair_index(point_pairs, num_images, mask=None):
    """Build the same sorted image-pair mapping as unique(..., dim=0).

    The adjacency is image-sized; no point-pair-sized stack/sort or full
    observation gather is created. Only selected entries of mapping are valid.
    """
    device = point_pairs.device
    present = torch.zeros(num_images, num_images, device=device, dtype=torch.bool)
    for indices in point_pairs.index_batches(mask):
        i = point_pairs.image_idx[point_pairs.point_idx1[indices]]
        j = point_pairs.image_idx[point_pairs.point_idx2[indices]]
        assert torch.all(i < j)
        present[i, j] = True
    i, j = present.nonzero(as_tuple=True)
    lookup = (present.flatten().long().cumsum(0) - 1).reshape(num_images, num_images)
    dtype = torch.int32 if len(i) < 2**31 else torch.int64
    mapping = torch.full((point_pairs.num_point_pairs,), -1, device=device, dtype=dtype)
    for indices in point_pairs.index_batches(mask):
        a = point_pairs.image_idx[point_pairs.point_idx1[indices]]
        b = point_pairs.image_idx[point_pairs.point_idx2[indices]]
        mapping[indices] = lookup[a, b].to(dtype)
    return i, j, mapping


@torch.no_grad()
def compute_ray_angle(
    xy1: torch.Tensor,
    xy2: torch.Tensor,
    R: torch.Tensor,
):
    """Compute the angle between the rays of two calibrated 2D correspondences.

    Args:
        xy1: torch.Tensor float (..., 2), calibrated 2D coordinates of the first image
        xy2: torch.Tensor float (..., 2), calibrated 2D coordinates of the second image
        R: torch.Tensor float (..., 3, 3), relative rotation (from frame1 to frame2)
        t: torch.Tensor float (..., 3), relative translation (from frame1 to frame2)

    Returns:
        angle: torch.Tensor float (...,), the angle between the rays in degrees
    """
    # get unnormlized rays in second camera frame
    ray1 = torch.einsum("...ij,...j->...i", R, to_homogeneous(xy1))  # (..., 3)
    ray2 = to_homogeneous(xy2)  # (..., 3)

    # normalize the rays
    ray1 = F.normalize(ray1, p=2, dim=-1)  # (..., 3)
    ray2 = F.normalize(ray2, p=2, dim=-1)  # (..., 3)

    # compute the angle
    angle = torch.acos(
        (ray1 * ray2).sum(dim=-1).clamp(-1.0 + 1e-8, 1.0 - 1e-8)
    )  # (...,)
    angle = angle * 180.0 / math.pi  # (...,)

    # return
    return angle


@torch.no_grad()
def cheirality_check(
    xy1: torch.Tensor,
    xy2: torch.Tensor,
    R: torch.Tensor,
    t: torch.Tensor,
):
    """Check the cheirality of calibrated 2D correspondences. We do not triangulate the points, but instead check the cheirality of the rays. This is much faster than triangulating the points.

    Args:
        xy1: torch.Tensor float (..., 2), calibrated 2D coordinates of the first image
        xy2: torch.Tensor float (..., 2), calibrated 2D coordinates of the second image
        R: torch.Tensor float (..., 3, 3), relative rotation (from frame1 to frame2)
        t: torch.Tensor float (..., 3), relative translation (from frame1 to frame2)

    Returns:
        mask: torch.Tensor bool (...,), True if the point pair has positive depth
    """
    # get unnormlized rays in second camera frame
    ray1 = torch.einsum("...ij,...j->...i", R, to_homogeneous(xy1))  # (..., 3)
    ray2 = to_homogeneous(xy2)  # (..., 3)

    # compute the mask (note that here t is vector from 2 to 1 in camera 2 frame)
    n1 = torch.cross(ray1, t, dim=-1)  # (..., 3)
    n2 = torch.cross(ray2, t, dim=-1)  # (..., 3)
    n3 = torch.cross(ray1, ray2, dim=-1)  # (..., 3)
    mask = (
        ((n1 * n2).sum(dim=-1) > 0.0)
        & ((n1 * n3).sum(dim=-1) > 0.0)
        & ((n2 * n3).sum(dim=-1) > 0.0)
    )  # (...,)

    # return
    return mask


@torch.no_grad()
def compute_epipolar_error(
    xy1: torch.Tensor,
    xy2: torch.Tensor,
    R: torch.Tensor,
    t: torch.Tensor,
):
    """Compute epipolar error of calibrated 2D correspondences.

    Args:
        xy1: torch.Tensor float (..., 2), calibrated 2D coordinates of the first image
        xy2: torch.Tensor float (..., 2), calibrated 2D coordinates of the
        R: torch.Tensor float (..., 3, 3), relative rotation (from frame1 to frame2)
        t: torch.Tensor float (..., 3), relative translation (from frame1 to frame2)

    Returns:
        error: torch.Tensor float (...,), the epipolar error
    """
    xy_homo1 = F.normalize(to_homogeneous(xy1), p=2, dim=-1)  # (..., 3)
    xy_homo2 = F.normalize(to_homogeneous(xy2), p=2, dim=-1)  # (..., 3)
    Rx1 = torch.einsum(
        "...ij,...j->...i",
        R,
        xy_homo1,
    )  # (..., 3)
    line = torch.cross(t, Rx1, dim=-1)  # (..., 3)
    error = (line * xy_homo2).sum(dim=-1).abs()  # (...,)

    return error
