from loguru import logger
import math
import torch
import torch.nn.functional as F

from fastmap.container import PointPairs, ImagePairs, Images
from fastmap.point_pair import (
    cheirality_check,
    compute_epipolar_error,
    compute_ray_angle,
    image_pair_index,
)
from fastmap.utils import to_homogeneous


@torch.no_grad()
def _rotation_between_two_vectors(v1: torch.Tensor, v2: torch.Tensor) -> torch.Tensor:
    """Compute the 3D rotation matrix that rotates v1 to v2.
    Args:
        v1: torch.Tensor float (..., 3,), the first vector
        v2: torch.Tensor float (..., 3,), the second vector
    Returns:
        R: torch.Tensor float (..., 3, 3), the rotation matrix
    """
    assert v1.shape[-1] == v2.shape[-1] == 3
    assert torch.allclose(
        v1.norm(dim=-1), torch.tensor(1.0, device=v1.device, dtype=v1.dtype)
    )
    assert torch.allclose(
        v2.norm(dim=-1), torch.tensor(1.0, device=v2.device, dtype=v2.dtype)
    )

    # Compute the axis of rotation (cross product of v1 and v2)
    w = torch.cross(v1, v2, dim=-1)
    w_norm = w.norm(dim=-1, keepdim=True)

    # Avoid division by zero in case of parallel vectors
    w = torch.where(w_norm > 1e-8, w / w_norm, w)

    # Compute the cosine of the angle
    cos_theta = torch.sum(v1 * v2, dim=-1, keepdim=True)
    # Ensure cos_theta is in valid range [-1, 1] to avoid NaNs from numerical errors
    cos_theta = torch.clamp(cos_theta, -1, 1)

    # Compute the sine of the angle
    sin_theta = torch.sqrt(1 - cos_theta**2)

    # Create the skew-symmetric cross-product matrix w_cross
    zero = torch.zeros_like(w[..., 0])
    w_cross = torch.stack(
        [
            zero,
            -w[..., 2],
            w[..., 1],
            w[..., 2],
            zero,
            -w[..., 0],
            -w[..., 1],
            w[..., 0],
            zero,
        ],
        dim=-1,
    ).reshape(w.shape[:-1] + (3, 3))

    # Compute the rotation matrix using Rodrigues' formula
    I = torch.eye(3, device=v1.device, dtype=v1.dtype).expand_as(w_cross)
    R = (
        I
        + sin_theta.unsqueeze(-1) * w_cross
        + (1 - cos_theta).unsqueeze(-1) * torch.matmul(w_cross, w_cross)
    )

    # Return the rotation matrix
    return R


@torch.no_grad()
def _fibonacci_spherical_cap(n: int, theta_max: float, device: str | torch.device):
    """
    Generate n points uniformly on a unit spherical cap centered around the north pole [0, 0, 1] defined by a maximum polar angle theta.

    Args:
        n (int): number of points to generate.
        theta_max (float): maximum angle from the north pole in degrees.
        device (str or torch.device): device on which to generate the points.

    Returns:
        points (float torch.Tensor): the sampled points on the spherical cap
    """
    # Convert the angle to radians
    theta_max_rad = torch.deg2rad(torch.tensor(theta_max))
    cos_theta_max = torch.cos(theta_max_rad)

    # Golden ratio
    phi = (1 + torch.sqrt(torch.tensor(5.0))) / 2

    # Fibonacci method for spherical cap
    indices = torch.arange(0, n, device=device, dtype=torch.float32)
    theta = torch.acos(1 - (1 - cos_theta_max) * (indices / n))
    phi = 2 * torch.pi * (indices / phi % 1)  # Longitude calculation modulo 1

    # Spherical to Cartesian conversion
    x = torch.sin(theta) * torch.cos(phi)
    y = torch.sin(theta) * torch.sin(phi)
    z = torch.cos(theta)

    # stack
    points = torch.stack([x, y, z], dim=-1)

    return points


@torch.no_grad()
def _disambiguate_sign(
    R: torch.Tensor,
    t: torch.Tensor,
    xy1: torch.Tensor,
    xy2: torch.Tensor,
    image_pair_idx: torch.Tensor,
    point_pair_mask: torch.Tensor | None,
):
    """Disambiguate the sign of the relative translation.
    Args:
        R: torch.Tensor float (num_image_pairs, 3, 3), the relative rotation
        t: torch.Tensor float (num_image_pairs, 3), the relative translation (up to sign) from camera 1 to camera 2
        xy1: torch.Tensor float (num_point_pairs, 2), the 2D coordinates of the first image
        xy2: torch.Tensor float (num_point_pairs, 2), the 2D coordinates of the second image
        point_pair_mask: torch.Tensor bool (num_point_pairs,), the mask indicating the inlier point pairs
        image_pair_idx: torch.Tensor long (num_point_pairs,), the index of the image pair for each point pair
    Returns:
        t: torch.Tensor float (num_image_pairs, 3), the relative translation from camera 1 to camera 2 with the correct sign
    """
    ##### Get some information #####
    num_image_pairs = R.shape[0]
    device = R.device

    ##### Get the data filtered by the inlier mask #####
    if point_pair_mask is None:
        point_pair_mask = torch.ones(
            xy1.shape[0], dtype=torch.bool, device=device
        )  # (num_point_pairs,)
    xy1 = xy1[point_pair_mask]  # (num_inliers, 2)
    xy2 = xy2[point_pair_mask]  # (num_inliers, 2)
    image_pair_idx = image_pair_idx[point_pair_mask]  # (num_inliers,)
    del point_pair_mask

    ##### Cheirality check #####
    # cheirality check
    cheirality_mask = cheirality_check(
        xy1=xy1, xy2=xy2, R=R[image_pair_idx], t=t[image_pair_idx]
    )  # (num_inliers,)

    ##### Compute the number of positive and negative depth #####
    pos_depth_count = torch.zeros(
        num_image_pairs, device=device, dtype=torch.long
    )  # (num_images*num_images,)
    neg_depth_count = torch.zeros(
        num_image_pairs, device=device, dtype=torch.long
    )  # (num_images*num_images,)
    pos_depth_count.scatter_reduce_(
        dim=0, index=image_pair_idx, src=cheirality_mask.long(), reduce="sum"
    )
    neg_depth_count.scatter_reduce_(
        dim=0, index=image_pair_idx, src=(~cheirality_mask).long(), reduce="sum"
    )

    ##### Disambiguate the sign #####
    t = torch.where(
        (pos_depth_count > neg_depth_count)[..., None], t, -t
    )  # (num_image_pairs, 3)

    ##### Return #####
    return t


@torch.no_grad()
def _voting_level(
    R: torch.Tensor,
    t: torch.Tensor,
    candidates: torch.Tensor,
    point_pairs: PointPairs,
    image_pair_idx: torch.Tensor,
    point_pair_mask: torch.Tensor,
    batch_size: int = 4096 * 8,
    epipolar_error_thr: float = 0.01,
) -> torch.Tensor:
    """One level of relative t voting given candidates. The candidates are centered around [0, 0, 1] and need to be rotated to the correct direction according to the current estimate of relative t.

    Args:
        R: torch.Tensor float (num_image_pairs, 3, 3), the relative rotation
        t: torch.Tensor float (num_image_pairs, 3), the relative translation (up to sign) from camera 1 to camera 2 in world frame
        candidates: torch.Tensor float (num_candidates, 3), the candidate unit vectors
        point_pairs: PointPairs container
        image_pair_idx: torch.Tensor long (num_point_pairs,), the index of the image pair for each point pair
        point_pair_mask: torch.Tensor bool (num_point_pairs,), the mask indicating the point pairs to consider. If None, all point pairs are considered.
        batch_size: int, the number of point pairs to process simultaneously
        epipolar_error_thr: float, the threshold for the epipolar error to consider a point pair as inlier. It will be used to compute the temperature for the voting. It will be slightly larger than the actual threshold to account for the error introduced by the discretization of the candidate vectors.

    Returns:
        t: torch.Tensor float (num_image_pairs, 3), the new relative translation (up to sign) from camera 1 to camera 2 in world frame. Note that not all pairs are valid. Telling which pairs are valid is the job of the caller of this function.
    """
    ##### Get some information #####
    num_image_pairs = R.shape[0]
    num_candidates = candidates.shape[0]
    num_point_pairs = point_pairs.num_point_pairs
    dtype = R.dtype
    device = R.device

    ##### Initialize buffers #####
    # initialize the buffer for votes
    votes = torch.zeros(
        num_image_pairs, num_candidates, device=device, dtype=dtype
    )  # (num_image_pairs, num_candidates)

    ##### Rotate the candidates #####
    # get the rotation matrix from [0., 0., 1.] to the current t
    rotation_matrix = _rotation_between_two_vectors(
        v1=torch.tensor([0.0, 0.0, 1.0], device=device, dtype=dtype).expand_as(t),
        v2=t,
    )  # (num_image_pairs, 3, 3)

    # rotate all the candidate vectors (differently for different image pairs)
    candidates = torch.einsum(
        "bij,nj->bni", rotation_matrix, candidates
    )  # (num_image_pairs, num_candidates, 3)

    # prevent misuse
    del rotation_matrix

    ##### Compute the temperature for voting #####
    # we want a temperature such that the vote decays to 0.5 when the epipolar error is epipolar_error_thr
    temperature = -epipolar_error_thr / math.log(0.5)
    logger.info(
        f"Using temperature {temperature} for epipolar error threshold {epipolar_error_thr}"
    )
    assert abs(math.exp(-epipolar_error_thr / temperature) - 0.5) < 0.00001

    ##### Compute the votes #####

    # loop
    logger.info("Voting level started.")
    for batch_data in point_pairs.query(
        point_pair_mask=point_pair_mask, batch_size=batch_size
    ):
        # make sure the first image index is smaller than the second
        assert torch.all(batch_data.image_idx1 < batch_data.image_idx2)

        # get data for the batch
        batch_image_pair_idx = image_pair_idx[batch_data.point_pair_idx].long()  # (B,)
        batch_R = R[batch_image_pair_idx]  # (B, 3, 3)
        batch_candidates = candidates[batch_image_pair_idx]  # (B, num_candidates, 3)

        # convert xy to homogeneous coordinates and normalize
        batch_xy_homo1 = F.normalize(
            to_homogeneous(batch_data.xy1), p=2, dim=-1
        )  # (B, 3)
        batch_xy_homo2 = F.normalize(
            to_homogeneous(batch_data.xy2), p=2, dim=-1
        )  # (B, 3)

        # compute the error
        batch_Rx1 = torch.einsum("bij,bj->bi", batch_R, batch_xy_homo1)  # (B, 3)
        batch_line = torch.cross(
            batch_candidates,
            batch_Rx1[:, None],
            dim=-1,
        )  # (B, num_candidates, 3)
        batch_error = torch.einsum(
            "bi,bni->bn", batch_xy_homo2, batch_line
        ).abs()  # (B, num_candidates)

        # compute the votes
        batch_votes = torch.exp(-batch_error / temperature)  # (B, num_candidates)

        # aggregate the votes
        votes.scatter_reduce_(
            dim=0,
            index=batch_image_pair_idx[:, None].expand(-1, num_candidates),
            src=batch_votes,
            reduce="sum",
        )

    ##### Get the full matrix of t #####
    t = candidates[torch.arange(num_image_pairs, device=device), votes.argmax(dim=-1)]
    logger.info("Voting level done.")

    ##### Return #####
    return t


@torch.no_grad()
def _voting(
    R: torch.Tensor,
    point_pairs: PointPairs,
    image_pair_idx: torch.Tensor,
    point_pair_mask: torch.Tensor,
    num_candidates: int = 512,
    epipolar_error_thr: float = 0.01,
):
    """Estimate the relative translation given the global rotation and calibrated 2D correspondences with brute force voting.

    Args:
        R: torch.Tensor float (num_image_pairs, 3, 3), the relative rotation
        point_pairs: PointPairs container
        image_pair_idx: torch.Tensor long (num_point_pairs,), the index of the image pair for each point pair
        point_pair_mask: torch.Tensor bool (num_point_pairs,), the mask indicating the point pairs to consider. If None, all point pairs are considered.
        num_candidates: int, the number of candidate unit vectors to consider
        epipolar_error_thr: float, the threshold for the epipolar error to consider a point pair as inlier. It will be used to compute the temperature for the voting. It will be slightly larger than the actual threshold to account for the error introduced by the discretization of the candidate vectors.

    Returns:
        t: torch.Tensor float (num_image_pairs, 3), the relative translation from camera 1 to camera 2 in world frame.
    """
    ##### Get some information #####
    num_image_pairs = R.shape[0]
    dtype, device = R.dtype, R.device

    ##### Initialize relative t #####
    t = torch.zeros(
        num_image_pairs, 3, device=device, dtype=dtype
    )  # (num_image_pairs, 3)
    t[..., -1] = 1.0

    ##### Vote hierarchically #####
    theta_max = 90.0
    while theta_max > 1.0:
        logger.info(f"Current level theta_max: {theta_max:.2f} degrees")

        # generate candidates
        candidates = _fibonacci_spherical_cap(
            n=num_candidates, theta_max=theta_max, device=device
        )  # (num_candidates, 3)

        # vote
        t = _voting_level(
            R=R,
            t=t,
            candidates=candidates,
            point_pairs=point_pairs,
            image_pair_idx=image_pair_idx,
            point_pair_mask=point_pair_mask,
            epipolar_error_thr=epipolar_error_thr,
        )  # (num_image_pairs, 3)

        # half the theta_max
        theta_max *= 0.5

    ##### Disambiguate the sign #####
    positive = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
    total = torch.zeros_like(positive)
    for batch in point_pairs.query(point_pair_mask):
        idx = image_pair_idx[batch.point_pair_idx].long()
        good = cheirality_check(batch.xy1, batch.xy2, R[idx], t[idx])
        positive.scatter_add_(0, idx, good.long())
        total.scatter_add_(0, idx, torch.ones_like(idx))
    t = torch.where((2 * positive > total)[:, None], t, -t)

    ##### Return #####
    return t


@torch.no_grad()
def re_estimate_relative_t(
    point_pairs: PointPairs,
    R_w2c: torch.Tensor,
    images: Images,
    num_candidates: int = 512,
    epipolar_error_thr: float = 0.01,
    ray_angle_thr: float = 2.0,
    min_num_inliers: int = 4,
    min_degree: int = 2,
):
    """Estimate the relative translation given the global rotation and calibrated 2D correspondences. Also update the mask in the Images container if some images are sparsely connected to the rest of the graph.

    Args:
        point_pairs: PointPairs container
        R_w2c: torch.Tensor float (num_images, 3, 3), w2c global rotation matrices for each image
        images: Images container
        num_candidates: int, the number of candidate unit vectors to consider
        epipolar_error_thr: float, the threshold for the epipolar error to consider a point pair as inlier.
        ray_angle_thr: float, if a point pair has a ray angle smaller than this threshold, it is considered as an inlier even if it fails the cheirality check
        min_num_inliers: int, the minimum number of inlier point pairs for the image pair to be considered
        min_degree: int, the minimum graph degree for an image to be considered as valid

    Returns:
        image_pairs: ImagePairs container, the number of image pairs is determined by how many pairs have enough inlier point pairs
        inlier_mask: torch.Tensor bool (num_point_pairs,), the mask indicating the inlier point pairs
        In place modification: the mask in the Images container is updated
    """
    ##### Get some information #####
    num_images = R_w2c.shape[0]
    device = R_w2c.device
    image_mask = images.mask.clone()  # (num_images,)

    image_idx1, image_idx2, image_pair_idx = image_pair_index(point_pairs, num_images)
    num_image_pairs = len(image_idx1)

    ##### Compute relative rotation matrix #####
    R = R_w2c[image_idx2] @ R_w2c[image_idx1].transpose(
        -1, -2
    )  # (num_image_pairs, 3, 3)

    ##### Voting #####
    # do not consider the point pairs that have almost parallel rays
    nonparallel_ray_mask = torch.empty(point_pairs.num_point_pairs, device=device, dtype=torch.bool)
    for batch in point_pairs.query():
        idx = image_pair_idx[batch.point_pair_idx].long()
        nonparallel_ray_mask[batch.point_pair_idx] = compute_ray_angle(
            batch.xy1, batch.xy2, R[idx]) > ray_angle_thr
    logger.info(f"Nonparallel point pairs for voting: {nonparallel_ray_mask.sum().item()} / {point_pairs.num_point_pairs}")

    # vote
    t = _voting(
        R=R,
        point_pairs=point_pairs,
        image_pair_idx=image_pair_idx,
        point_pair_mask=nonparallel_ray_mask,
        num_candidates=num_candidates,
        epipolar_error_thr=epipolar_error_thr,
    )  # (num_image_pairs, 3)
    del nonparallel_ray_mask

    # Evaluate geometry and accumulate support with bounded temporaries.
    inlier_mask = torch.empty(point_pairs.num_point_pairs, device=device, dtype=torch.bool)
    support_count = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
    for batch in point_pairs.query():
        idx = image_pair_idx[batch.point_pair_idx].long()
        batch_R, batch_t = R[idx], t[idx]
        low = compute_epipolar_error(batch.xy1, batch.xy2, batch_R, batch_t) < epipolar_error_thr
        front = cheirality_check(batch.xy1, batch.xy2, batch_R, batch_t)
        small = compute_ray_angle(batch.xy1, batch.xy2, batch_R) < ray_angle_thr
        inlier_mask[batch.point_pair_idx] = low & (front | small)
        support_count.scatter_add_(0, idx, (low & ~small & front).long())
    valid_mask = support_count >= min_num_inliers
    del support_count

    ##### Iteratively prune image pairs with too few connections #####
    # clone to avoid in-place modification
    image_mask = image_mask.clone()  # (num_images,)

    # mask out already invalid images
    valid_mask &= image_mask[image_idx1] & image_mask[image_idx2]  # (num_image_pairs,)

    # initialize the count of valid image pairs
    num_valid_images = image_mask.long().sum().item()
    logger.info(
        f"Number of valid images before pruning: {image_mask.long().sum().item()} out of {num_images}"
    )
    logger.info(
        f"Number of valid image pairs before pruning: {valid_mask.long().sum().item()}"
    )

    # prune images until no more images can be pruned
    while True:
        # compute the degree of each image
        degree = torch.zeros(
            num_images, device=device, dtype=torch.long
        )  # (num_images,)
        degree.scatter_reduce_(
            dim=0,
            index=image_idx1,
            src=valid_mask.long(),
            reduce="sum",
        )
        degree.scatter_reduce_(
            dim=0,
            index=image_idx2,
            src=valid_mask.long(),
            reduce="sum",
        )

        # prune images with degree less than min_degree
        image_mask &= degree >= min_degree  # (num_images,)

        # update valid mask for image pairs
        valid_mask &= (
            image_mask[image_idx1] & image_mask[image_idx2]
        )  # (num_image_pairs,)

        # check if continue
        if image_mask.long().sum().item() == num_valid_images:
            break
        else:
            logger.info(
                f"Pruned {num_valid_images - image_mask.long().sum().item()} images due to insufficient connections"
            )
            num_valid_images = image_mask.long().sum().item()

        # prevent misuse
        del degree

    # prevent misuse
    del num_valid_images

    logger.info(
        f"Number of valid image pairs after pruning: {valid_mask.long().sum().item()}"
    )
    logger.info(
        f"Number of valid images after pruning: {image_mask.long().sum().item()} out of {num_images}"
    )

    inlier_count = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
    for indices in point_pairs.index_batches():
        idx = image_pair_idx[indices].long()
        selected = inlier_mask[indices] & valid_mask[idx]
        inlier_mask[indices] = selected
        inlier_count.scatter_add_(0, idx, selected.long())

    ##### Build ImagePairs container #####
    image_pairs = ImagePairs(
        image_idx1=image_idx1[valid_mask],  # (num_valid_image_pairs,)
        image_idx2=image_idx2[valid_mask],  # (num_valid_image_pairs,)
        rotation=R[valid_mask],  # (num_valid_image_pairs, 3, 3)
        translation=t[valid_mask],  # (num_valid_image_pairs, 3)
        num_inliers=inlier_count[valid_mask],  # (num_valid_image_pairs,)
    )

    ##### Update image mask #####
    images.mask &= image_mask
    del image_mask

    ##### Return #####
    return (
        image_pairs,
        inlier_mask,
    )
