"""Read solver presets without importing PyTorch into the desktop process."""
import json
from paths import FASTMAP

_data = json.loads((FASTMAP / 'fastmap/optimization_profiles.json').read_text(encoding='utf-8'))
OPTIMIZATION_REVISION = _data['revision']
SOLVER_PROFILES = _data['profiles']


def quality_note(profile):
    limits = SOLVER_PROFILES[profile]
    return (f"FastMap 每步最多 {limits['max_iterations']:,} 次 / {limits['max_seconds']} 秒，"
            f"极线优化整体预算 {limits['epipolar_seconds'] // 60} 分钟；改善过小时提前结束。"
            '这是计算预算，不保证轨迹质量；COLMAP 使用 Ceres 停止条件。')
