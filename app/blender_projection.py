"""Check camera conversion with well-conditioned, in-frame projection probes."""
import math

from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


def camera_projection_error(scene, camera, colmap_pose, intrinsics):
    """Check ideal COLMAP projection without near-plane cloud singularities.

    Arbitrary cloud points can project tens of thousands of pixels off-screen.
    Dividing by their tiny depths amplifies Blender's float32 roundoff. Cover
    the image with reference rays at depths comparable to camera coordinates
    to test pose, axis conversion, focal length, principal point and aspect.
    """
    width, height, fx, fy, cx, cy = intrinsics
    if (not all(math.isfinite(v) for v in intrinsics)
            or min(width, height, fx, fy) <= 0
            or not all(math.isfinite(v) for row in colmap_pose for v in row)):
        raise ValueError('Invalid camera projection parameters')
    camera_to_world = colmap_pose.inverted()
    depth_scale = max(1.0, camera_to_world.translation.length)
    maximum = 0.0
    for depth in (depth_scale, depth_scale * 2):
        for u in (.1, .5, .9):
            for v in (.1, .5, .9):
                expected_x, expected_y = u * width, v * height
                local = Vector(((expected_x - cx) * depth / fx,
                                (expected_y - cy) * depth / fy, depth))
                world = camera_to_world @ local
                actual = world_to_camera_view(scene, camera, world)
                if not all(math.isfinite(value) for value in actual) or actual.z <= 0:
                    raise ValueError('Invalid Blender camera projection')
                maximum = max(maximum, abs(actual.x * width - expected_x),
                              abs((1 - actual.y) * height - expected_y))
    return maximum
