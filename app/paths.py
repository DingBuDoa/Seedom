"""All application-owned paths stay relative to this distribution directory."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
RUNTIME = ROOT / 'runtime'
FASTMAP = RUNTIME / 'fastmap'
GLOMAP = RUNTIME / 'glomap-cpu'
GLOMAP_GPU = RUNTIME / 'glomap-gpu'
CONFIG = ROOT / 'config'
LOGS = ROOT / 'logs'
OUTPUTS = ROOT / 'output'
LEGACY_OUTPUTS = ROOT / 'outputs'
CUDA = RUNTIME / 'cuda'
DEV_PYTHON = ROOT.parent.parent / 'archive' / 'legacy-1.x' / 'runtime' / 'python' / 'python.exe'
DEV_RUNTIME = ROOT.parent.parent / 'archive' / 'legacy-1.x' / 'runtime'
DEV_FFMPEG = DEV_RUNTIME / 'ffmpeg'
FFMPEG = RUNTIME / 'ffmpeg'
if not FFMPEG.is_dir() and DEV_FFMPEG.is_dir():
    FFMPEG = DEV_FFMPEG
BINARY_DIRS = {'01 GLOMAP': RUNTIME / 'colmap',
               # The name is retained for old settings, but points to the
               # native CUDA COLMAP bundle rather than the archived GLOMAP.
               '01 GLOMAP GPU': RUNTIME / 'colmap-cuda',
               '02 GLOMAP': GLOMAP,
               '02 GLOMAP GPU': GLOMAP_GPU,
               '03 FFMPEG': FFMPEG}
# The source checkout reuses the archived Qt/Python runtime until the 2.0
# runtime bundle is assembled. A packaged runtime still takes precedence.
PYTHON = RUNTIME / 'python' / 'python.exe'
if not PYTHON.is_file() and DEV_PYTHON.is_file():
    PYTHON = DEV_PYTHON
# Development builds stay outside the distributable runtime. They are used
# automatically when the native COLMAP executable has not been copied into a
# release package yet.
DEV_COLMAP = ROOT.parent / 'solvers' / 'colmap' / 'build-native' / 'src' / 'colmap' / 'exe' / 'colmap.exe'
DEV_COLMAP_CUDA = ROOT.parent / 'solvers' / 'colmap' / 'build-cuda' / 'src' / 'colmap' / 'exe' / 'colmap.exe'
RUNTIME_COLMAP = RUNTIME / 'colmap' / 'colmap.exe'
RUNTIME_COLMAP_CUDA = RUNTIME / 'colmap-cuda' / 'colmap.exe'
RUNTIME_GLOMAP = GLOMAP / 'glomap.exe'
RUNTIME_GLOMAP_CUDA = GLOMAP_GPU / 'glomap.exe'
DEV_GLOMAP = ROOT.parent.parent / 'archive' / 'legacy-1.x' / 'runtime' / 'colmap' / 'glomap.exe'
DEV_GLOMAP_CUDA = ROOT.parent.parent / 'archive' / 'legacy-1.x' / 'runtime' / 'glomap-gpu' / 'glomap.exe'


def use_dev_cuda_colmap():
    """Return whether the opt-in CUDA development solver should be used."""
    return os.environ.get('SEEDOM_CUDA_COLMAP', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def saved_path(value, previous_root=None):
    """Resolve portable paths and migrate application-owned legacy absolute paths."""
    path = Path(value)
    if not path.is_absolute():
        return ROOT / path
    if previous_root and isinstance(previous_root, (str, os.PathLike)):
        try:
            return ROOT / path.relative_to(Path(previous_root))
        except ValueError:
            pass
    # Older distributions persisted absolute paths without their install root.
    # Only relocate paths inside a named application directory, never media.
    for index in range(len(path.parts) - 1, -1, -1):
        if path.parts[index].casefold() in {ROOT.name.casefold(), 'glomap gui'}:
            return ROOT.joinpath(*path.parts[index + 1:])
    return path


def portable_path(value):
    path = Path(value).absolute()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def runtime_environment(cuda=None):
    # Keep solver DLL namespaces separate: original COLMAP and GPU Ceres differ.
    cuda = use_dev_cuda_colmap() if cuda is None else bool(cuda)
    path_entries = [str(CUDA)]
    if cuda and RUNTIME_COLMAP_CUDA.is_file():
        selected_colmap = RUNTIME_COLMAP_CUDA
    elif cuda and DEV_COLMAP_CUDA.is_file():
        selected_colmap = DEV_COLMAP_CUDA
    elif RUNTIME_COLMAP.is_file():
        selected_colmap = RUNTIME_COLMAP
    else:
        selected_colmap = DEV_COLMAP
    if selected_colmap.is_file():
        path_entries.append(str(selected_colmap.parent))
    # The development build keeps third-party DLLs in vcpkg. A packaged build
    # carries those DLLs beside colmap.exe and does not depend on the checkout.
    if selected_colmap in (DEV_COLMAP, DEV_COLMAP_CUDA):
        dev_vcpkg_bin = selected_colmap.parent.parent.parent.parent / 'vcpkg_installed' / 'x64-windows' / 'bin'
        if dev_vcpkg_bin.is_dir():
            path_entries.append(str(dev_vcpkg_bin))
    cuda_root = Path(os.environ.get('CUDA_PATH', r'D:\CUDA'))
    cuda_bin = cuda_root / 'bin'
    if cuda_bin.is_dir():
        path_entries.append(str(cuda_bin))
    return {'PYTHONNOUSERSITE': '1', 'PYTHONPATH': '',
            'TCL_LIBRARY': str(RUNTIME / 'python/tcl/tcl8.6'),
            'TK_LIBRARY': str(RUNTIME / 'python/tcl/tk8.6'),
            'PATH': os.pathsep.join(path_entries) + os.pathsep + os.environ.get('PATH', '')}
