"""One-off, reviewable source migration for bounded long-track workspaces."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'runtime/fastmap/fastmap'


def replace_span(text, start, end, replacement):
    a = text.index(start)
    b = text.index(end, a)
    return text[:a] + replacement + text[b:]


p = ROOT / 'container.py'
s = p.read_text(encoding='utf-8')
a = s.index('class PointPairs:')
prefix, body = s[:a], s[a:]
body = replace_span(body, '        # if point_pair_mask is None,',
                    '                # get data for the batch', '''        for point_pair_idx in self.index_batches(
            point_pair_mask, batch_size, randomize, infinite, drop_last
        ):
''')
# The former epoch/batch nesting is now a single generator loop.
a = body.index('                # get data for the batch')
b = body.index('\n\n@dataclass\nclass ImagePairs:', a)
body = body[:a] + '\n'.join(line[4:] if line.startswith('    ') else line
                            for line in body[a:b].split('\n')) + body[b:]
p.write_text(prefix + body, encoding='utf-8')

p = ROOT / 'point_pair.py'
s = p.read_text(encoding='utf-8')
s = s.replace('from fastmap.utils import to_homogeneous',
              'from fastmap.utils import to_homogeneous\nfrom fastmap.memory_policy import current_limit')
s = replace_span(s, '    ########## Get useful information', '\n\n@torch.no_grad()\ndef compute_ray_angle', '''    device = tracks.device
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
''')
p.write_text(s, encoding='utf-8')

p = ROOT / 'rel_t.py'
s = p.read_text(encoding='utf-8').replace('    compute_ray_angle,', '    compute_ray_angle,\n    image_pair_index,')
# Only the small per-batch index needs the long dtype required by scatter.
s = s.replace('image_pair_idx[batch_data.point_pair_idx]  # (B,)',
              'image_pair_idx[batch_data.point_pair_idx].long()  # (B,)')
s = s.replace('num_point_pairs = point_pairs.point_idx1.shape[0]',
              'num_point_pairs = point_pairs.num_point_pairs')
s = replace_span(s, '    t = _disambiguate_sign(\n', '    ##### Return #####\n    return t\n', '''    positive = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
    total = torch.zeros_like(positive)
    for batch in point_pairs.query(point_pair_mask):
        idx = image_pair_idx[batch.point_pair_idx].long()
        good = cheirality_check(batch.xy1, batch.xy2, R[idx], t[idx])
        positive.scatter_add_(0, idx, good.long())
        total.scatter_add_(0, idx, torch.ones_like(idx))
    t = torch.where((2 * positive > total)[:, None], t, -t)

''')
s = replace_span(s, '    ##### Find the image pairs with a non-empty set of inliers #####',
                 '    ##### Compute relative rotation matrix #####', '''    image_idx1, image_idx2, image_pair_idx = image_pair_index(point_pairs, num_images)
    num_image_pairs = len(image_idx1)

''')
s = replace_span(s, '    nonparallel_ray_mask = (', '    # vote\n', '''    nonparallel_ray_mask = torch.empty(point_pairs.num_point_pairs, device=device, dtype=torch.bool)
    for batch in point_pairs.query():
        idx = image_pair_idx[batch.point_pair_idx].long()
        nonparallel_ray_mask[batch.point_pair_idx] = compute_ray_angle(
            batch.xy1, batch.xy2, R[idx]) > ray_angle_thr
    logger.info(f"Nonparallel point pairs for voting: {nonparallel_ray_mask.sum().item()} / {point_pairs.num_point_pairs}")

''')
s = replace_span(s, '    ##### Compute all kinds of masks #####',
                 '    ##### Iteratively prune image pairs with too few connections #####', '''    # Evaluate geometry and accumulate support with bounded temporaries.
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

''')
s = replace_span(s, '    ##### Eliminate all point pairs not in valid image pairs #####',
                 '    ##### Build ImagePairs container #####', '''    inlier_count = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
    for indices in point_pairs.index_batches():
        idx = image_pair_idx[indices].long()
        selected = inlier_mask[indices] & valid_mask[idx]
        inlier_mask[indices] = selected
        inlier_count.scatter_add_(0, idx, selected.long())

''')
p.write_text(s, encoding='utf-8')

p = ROOT / 'epipolar.py'
s = p.read_text(encoding='utf-8').replace('from fastmap.timer import timer',
    'from fastmap.timer import timer\nfrom fastmap.point_pair import image_pair_index')
s = s.replace('        ]  # (num_valid_point_pairs_in_batch,)\n        batch_fundamental',
              '        ].long()  # (num_valid_point_pairs_in_batch,)\n        batch_fundamental')
s = s.replace('W /= point_pair_mask.to(W).sum()', 'W /= point_pair_mask.sum().to(W)')
s = s.replace('    image_mask: torch.Tensor,\n):\n    """Compute the epipolar error',
              '    image_mask: torch.Tensor,\n    threshold=None,\n    original_mask=None,\n):\n    """Compute the epipolar error')
s = s.replace('    error = torch.nan + torch.zeros(\n        num_point_pairs, device=device, dtype=dtype\n    )',
              '    error = torch.empty(\n        num_point_pairs, device=device, dtype=(dtype if threshold is None else torch.bool)\n    )')
s = replace_span(s, '        error[batch_data.point_pair_idx] = batch_error',
                 '    # return\n    return error', '''        valid = image_mask[batch_data.image_idx1] & image_mask[batch_data.image_idx2]
        batch_error[~valid] = torch.inf
        assert not torch.any(torch.isnan(batch_error))
        if threshold is None:
            error[batch_data.point_pair_idx] = batch_error
        else:
            error[batch_data.point_pair_idx] = (
                original_mask[batch_data.point_pair_idx] & (batch_error < threshold))

''')
s = replace_span(s, '    ##### Make sure the point pair mask only involves valid images #####',
                 '    # get number of image pairs\n', '''    for batch in point_pairs.query(point_pair_mask):
        assert torch.all(images.mask[batch.image_idx1] & images.mask[batch.image_idx2])
    original_point_pair_mask = point_pair_mask.clone()
    image_idx1, image_idx2, image_pair_idx = image_pair_index(
        point_pairs, images.num_images, point_pair_mask)

''')
s = s.replace('                error = _all_error(', '                point_pair_mask = _all_error(')
s = replace_span(s, '                    image_mask=images.mask,\n',
                 '                image_pair_mask = point_pair_count > 0', '''                    image_mask=images.mask,
                    threshold=thr,
                    original_mask=original_point_pair_mask,
                )
                point_pair_count = torch.zeros(num_image_pairs, device=device, dtype=torch.long)
                for indices in point_pairs.index_batches(point_pair_mask):
                    idx = image_pair_idx[indices].long()
                    point_pair_count.scatter_add_(0, idx, torch.ones_like(idx))
''')
s = s.replace('(~point_pair_mask).long().sum().item()',
              'point_pair_mask.numel() - point_pair_mask.sum().item()')
p.write_text(s, encoding='utf-8')
