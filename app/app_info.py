"""Release metadata and the upstream projects included in this distribution."""
APP_NAME = '拾点VFX（SeedomVFX）'
APP_VERSION = '2.0.0-dev'
BUILD_DATE = '2026-11-14'
PROJECT_URL = 'https://github.com/DingBuDoa/Seedom'
SOLVER_CHOICES = {
    'colmap': 'COLMAP · Global Mapper（CUDA/CPU）',
}

# Versions checked against bundled source/package metadata and executable help.
OPEN_SOURCE_PROJECTS = (
    ('COLMAP', '4.3.0.dev · CUDA/CPU', '特征、匹配、Global Mapper 与原生动态焦距 BA', 'https://github.com/colmap/colmap'),
    ('FFmpeg', '7.1.1 · essentials build', '视频解码、缩放与抽帧', 'https://github.com/FFmpeg/FFmpeg'),
    ('Photogrammetry Importer', '3.2.1 · 本地适配', '摄影测量数据导入', 'https://github.com/SBCV/Blender-Addon-Photogrammetry-Importer'),
)
