"""Numerical regressions for long-track expansion and bounded geometry passes."""
from pathlib import Path
import itertools
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/fastmap'))
import torch
import torch.nn.functional as F
from fastmap.container import Tracks, Images, Cameras
from fastmap.point_pair import (point_pairs_from_tracks, image_pair_index,
                                compute_ray_angle, compute_epipolar_error, cheirality_check)
from fastmap.rel_t import re_estimate_relative_t, _voting, _voting_level, _fibonacci_spherical_cap
from fastmap.epipolar import _all_error, _compute_fundamental_matrix, quadratic_form, epipolar_adjustment
from fastmap.utils import to_homogeneous


def scene(device='cpu', sizes=(5, 2, 4, 5, 3, 2, 4, 5)):
    torch.manual_seed(123)
    size = torch.tensor(sizes, device=device)
    start = size.cumsum(0) - size
    track = torch.repeat_interleave(torch.arange(len(size), device=device), size)
    image = torch.cat([torch.arange(n, device=device) for n in sizes])
    xyz = torch.randn(len(size), 3, device=device)
    xyz[:, 2] = xyz[:, 2].abs() + 4
    t = torch.zeros(max(sizes), 3, device=device)
    t[:, 0] = torch.arange(max(sizes), device=device) * -.4
    projected = xyz[track] + t[image]
    xy = projected[:, :2] / projected[:, 2:]
    xy += torch.randn_like(xy) * .0001
    tracks = Tracks(size, start, track, xy * 1000, xy, image,
                    torch.arange(len(image), device=device))
    return tracks, t


class FastMapMemoryTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)

    def devices(self):
        return ['cpu'] + (['cuda:0'] if torch.cuda.is_available() else [])

    def test_expansion_is_exact_including_long_tracks(self):
        for device in self.devices():
            tracks, _ = scene(device, sizes=(8, 2, 567, 4, 8))
            pairs = point_pairs_from_tracks(tracks)
            expected = []
            for size in sorted(set(tracks.track_size.tolist())):
                for start in tracks.track_start[tracks.track_size == size].tolist():
                    expected.extend(itertools.combinations(range(start, start + size), 2))
            expected = torch.tensor(expected, device=device)
            self.assertTrue(torch.equal(pairs.point_idx1, expected[:, 0]))
            self.assertTrue(torch.equal(pairs.point_idx2, expected[:, 1]))
            self.assertEqual(pairs.point_idx1.dtype, torch.int32)

    def test_query_packs_mask_and_preserves_order_and_epochs(self):
        for device in self.devices():
            pairs = point_pairs_from_tracks(scene(device)[0])
            mask = torch.arange(pairs.num_point_pairs, device=device) % 3 == 1
            expected = mask.nonzero().flatten()
            for drop in (False, True):
                batches = list(pairs.query(mask, batch_size=7, drop_last=drop))
                actual = torch.cat([b.point_pair_idx for b in batches])
                count = len(expected) // 7 * 7 if drop else len(expected)
                self.assertTrue(torch.equal(actual, expected[:count]))
                for batch in batches:
                    self.assertTrue(torch.equal(batch.xy1, pairs.xy[pairs.point_idx1[batch.point_pair_idx]]))
            shuffled = torch.cat([b.point_pair_idx for b in pairs.query(mask, randomize=True, batch_size=7)])
            self.assertTrue(torch.equal(shuffled.sort().values, expected))
            epoch_batches = (len(expected) + 6) // 7
            repeated = list(itertools.islice(pairs.query(mask, infinite=True, batch_size=7), epoch_batches * 2))
            self.assertTrue(torch.equal(torch.cat([b.point_pair_idx for b in repeated]), expected.repeat(2)))
            self.assertEqual(list(pairs.query(torch.zeros_like(mask))), [])
            with self.assertRaises(ValueError):
                list(pairs.query(batch_size=0))

    def test_mapping_matches_unique_for_masked_and_unmasked_data(self):
        for device in self.devices():
            pairs = point_pairs_from_tracks(scene(device)[0])
            full = torch.stack((pairs.image_idx[pairs.point_idx1], pairs.image_idx[pairs.point_idx2]), -1)
            for mask in (None, torch.arange(len(full), device=device) % 4 != 0):
                selected = full if mask is None else full[mask]
                expected, inverse = torch.unique(selected, dim=0, return_inverse=True)
                with patch('fastmap.container.current_limit', return_value=7):
                    i, j, mapping = image_pair_index(pairs, 5, mask)
                self.assertTrue(torch.equal(torch.stack((i, j), -1), expected))
                self.assertTrue(torch.equal(mapping if mask is None else mapping[mask], inverse))

    def test_chunked_geometry_and_voting_match_dense_result(self):
        for device in self.devices():
            pairs = point_pairs_from_tracks(scene(device)[0])
            R = torch.eye(3, device=device).repeat(5, 1, 1)
            i, j, mapping = image_pair_index(pairs, 5)
            rel_R = R[j] @ R[i].transpose(-1, -2)
            xy1, xy2 = pairs.xy[pairs.point_idx1], pairs.xy[pairs.point_idx2]
            nonparallel = compute_ray_angle(xy1, xy2, rel_R[mapping]) > 2
            # Dense and seven-element batches must choose the same voting candidates.
            dense_t = _voting(rel_R, pairs, mapping.long(), nonparallel)
            with patch('fastmap.container.current_limit', return_value=7):
                chunk_t = _voting(rel_R, pairs, mapping, nonparallel)
            torch.testing.assert_close(chunk_t, dense_t)
            front = cheirality_check(xy1, xy2, rel_R[mapping], dense_t[mapping])
            small = compute_ray_angle(xy1, xy2, rel_R[mapping]) < 2
            low = compute_epipolar_error(xy1, xy2, rel_R[mapping], dense_t[mapping]) < .01
            support = torch.zeros(len(i), device=device, dtype=torch.long)
            support.scatter_add_(0, mapping.long(), (low & ~small & front).long())
            valid = support >= 1
            expected_mask = low & (front | small) & valid[mapping]
            images = Images(list(map(str, range(5))), torch.ones(5, device=device),
                            torch.ones(5, device=device), torch.ones(5, device=device, dtype=torch.bool))
            with patch('fastmap.container.current_limit', return_value=7):
                result, mask = re_estimate_relative_t(pairs, R, images, min_num_inliers=1, min_degree=0)
            self.assertTrue(torch.equal(mask, expected_mask))
            self.assertTrue(torch.equal(result.image_idx1, i[valid]))
            torch.testing.assert_close(result.translation, dense_t[valid])

    def test_epipolar_errors_quadratic_form_and_pruning_match_dense(self):
        for device in self.devices():
            tracks, t = scene(device)
            pairs = point_pairs_from_tracks(tracks)
            R = torch.eye(3, device=device).repeat(5, 1, 1)
            camera = torch.zeros(5, device=device, dtype=torch.long)
            focal = torch.ones(1, device=device)
            valid = torch.tensor([True, True, False, True, True], device=device)
            a, b = pairs.image_idx[pairs.point_idx1], pairs.image_idx[pairs.point_idx2]
            fundamental = _compute_fundamental_matrix(a, b, R, t, focal, camera)
            x = F.normalize(to_homogeneous(pairs.xy[pairs.point_idx1]), dim=-1)
            y = F.normalize(to_homogeneous(pairs.xy[pairs.point_idx2]), dim=-1)
            expected = torch.einsum('bi,bij,bj->b', y, fundamental, x).abs()
            expected[~(valid[a] & valid[b])] = torch.inf
            original = torch.arange(len(a), device=device) % 3 != 0
            with patch('fastmap.container.current_limit', return_value=7):
                actual = _all_error(pairs, R, t, focal, camera, valid)
                pruned = _all_error(pairs, R, t, focal, camera, valid, .005, original)
            torch.testing.assert_close(actual, expected)
            self.assertTrue(torch.equal(pruned, original & (expected < .005)))
            i, j, mapping = image_pair_index(pairs, 5)
            image_F = _compute_fundamental_matrix(i, j, R, t, focal, camera)
            w = torch.einsum('bi,bj->bij', y, x).reshape(-1, 9)
            weights = 1 / ((w * image_F[mapping].reshape(-1, 9)).sum(-1).abs() + 1e-4)
            expected_W = torch.zeros(len(i), 9, 9, device=device, dtype=torch.float64)
            terms = (torch.einsum('bi,bj->bij', w, w) * weights[:, None, None]).double()
            expected_W.index_add_(0, mapping[original].long(), terms[original])
            expected_W /= original.sum()
            with patch('fastmap.container.current_limit', return_value=7):
                W = quadratic_form(len(i), pairs, mapping, image_F, original, torch.float64)
            torch.testing.assert_close(W, expected_W)


if __name__ == '__main__':
    unittest.main()
