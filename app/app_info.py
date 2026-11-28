"""Release metadata and the upstream projects included in this distribution."""
APP_NAME = '拾点VFX（SeedomVFX）'
APP_VERSION = '2.0.0-dev'
BUILD_DATE = '2026-11-28'
PROJECT_URL = 'https://github.com/DingBuDoa/Seedom'
DEFAULT_SOLVER = 'vggt-omega'
SOLVER_SELECTION_REVISION = 1
SOLVER_CHOICES = {
    'vggt-omega': 'VGGT-Omega',
    'glomap': 'GLOMAP',
}
SOLVER_TOOLTIPS = {
    'vggt-omega': '预测相机、焦距和深度。长视频分批处理，建议先检查轨迹效果。',
    'glomap': '传统特征匹配与几何重建，适合纹理清晰、相机有明显位移的素材。',
}


def preferred_solver(config):
    """Migrate the old default once, then preserve an explicit supported choice."""
    if config.get('solver_selection_revision') == SOLVER_SELECTION_REVISION:
        backend = config.get('backend')
        if isinstance(backend, str) and backend in SOLVER_CHOICES:
            return backend
    return DEFAULT_SOLVER

# Versions checked against bundled source/package metadata and executable help.
OPEN_SOURCE_PROJECTS = (
    ('VGGT-Omega', '0.0.1 · 48b23c8 · FAIR 非商业研究许可', '主要相机与深度预测模型', 'https://github.com/facebookresearch/vggt-omega'),
    ('GLOMAP', '本地 CUDA/CPU 构建', '传统几何重建', 'https://github.com/colmap/glomap'),
    ('COLMAP', '3.12 / 4.3.0.dev · CUDA/CPU', 'GLOMAP 配套特征与匹配工具；保留原生 BA 研究代码', 'https://github.com/colmap/colmap'),
    ('FFmpeg', '7.1.1 · essentials build', '视频解码、缩放与抽帧', 'https://github.com/FFmpeg/FFmpeg'),
    ('Photogrammetry Importer', '3.2.1 · 本地适配', '摄影测量数据导入', 'https://github.com/SBCV/Blender-Addon-Photogrammetry-Importer'),
)
