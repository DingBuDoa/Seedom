"""Verify a built update and install/restore it in a tiny disposable fixture."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from hotfix import apply, digest, restore, version
from updates import stage_package, validate_update


def verify(directory):
    document = validate_update(json.loads((directory / 'update.json').read_text(encoding='utf-8')))
    archive = directory / document['package']['name']
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError('ZIP CRC check failed.')
    with tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts') as temporary:
        temporary = Path(temporary)
        base = next(item for item in document['base_versions'] if item != document['version'])
        package = stage_package(archive, document, temporary / 'cache', current_version=base)
        target = temporary / 'installation'
        owned = ('config/settings.json', 'config/task-queue.json', 'config/task-history.json', 'output/project/user.blend')
        for name in ('Start.bat', 'app/launch.py', 'runtime/python/python.exe',
                     'runtime/fastmap/fastmap/container.py', *owned):
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'preserve')
        (target / 'app/app_info.py').write_text(f"APP_VERSION = '{base}'\n", encoding='utf-8')
        first = apply(package, target)
        assert version(target) == document['version']
        for entry in json.loads((package / 'PATCH-MANIFEST.json').read_text(encoding='utf-8'))['files']:
            assert digest(target / entry['path']) == entry['sha256']
        second = apply(package, target)
        restore(target, second); restore(target, first)
        assert version(target) == base
        for name in owned:
            assert (target / name).read_bytes() == b'preserve', name
    report = dict(status='passed', version=document['version'], base_version=base,
                  checks=['ZIP CRC', 'ZIP SHA-256', 'Payload SHA-256 and Python syntax',
                          'Real installation', 'Repeated installation', 'Successive rollback',
                          'Settings, queue, timing history and project preservation'])
    (directory / 'VALIDATION.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    verify(parser.parse_args().directory.resolve())
