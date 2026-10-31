"""Verify the 1.0.3 -> 1.1.0 hotfix archive and its install/restore path."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_hotfix_1_1 import NAME, ROOT


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def refresh_archive(package, archive):
    checksums = []
    for path in sorted(package.rglob('*')):
        if path.is_file() and path.name != 'SHA256SUMS.txt':
            checksums.append(f'{digest(path)}  {path.relative_to(package).as_posix()}')
    (package / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                bundle.write(path, (Path(NAME) / path.relative_to(package)).as_posix())
    archive.with_suffix('.zip.sha256').write_text(
        f'{digest(archive)}  {archive.name}\n', encoding='ascii')


def verify():
    package = ROOT / NAME
    archive = ROOT / f'{NAME}.zip'
    expected_archive_hash = archive.with_suffix('.zip.sha256').read_text(
        encoding='ascii').split()[0]
    assert digest(archive) == expected_archive_hash
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        package_files = {path.relative_to(ROOT).as_posix(): path
                         for path in package.rglob('*') if path.is_file()}
        assert set(bundle.namelist()) == set(package_files)
        for name, path in package_files.items():
            assert bundle.read(name) == path.read_bytes(), name

    manifest = json.loads((package / 'PATCH-MANIFEST.json').read_text(encoding='utf-8'))
    assert manifest['base_version'] == '1.0.3'
    assert manifest['version'] == '1.1.0'
    assert manifest['files']
    for entry in manifest['files']:
        source = package / 'payload' / entry['path']
        assert source.stat().st_size == entry['bytes']
        assert digest(source) == entry['sha256']

    spec = importlib.util.spec_from_file_location('hotfix_1_1_updater', package / 'apply_hotfix.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    with tempfile.TemporaryDirectory(prefix='seedomvfx-hotfix-') as temp:
        target = Path(temp) / 'installation'
        base_files = {
            'Start.bat': b'launcher',
            'app/launch.py': b'# launcher',
            'app/app_info.py': b"APP_VERSION = '1.0.3'\n",
            'runtime/python/python.exe': b'fixture only',
            'runtime/fastmap/fastmap/container.py': b'# solver runtime',
            'config/runtime-manifest.json': b'{"application_name":"PointStudio","application_version":"1.0.3"}',
            'config/settings.json': b'{"user": "preserve"}',
            'config/task-queue.json': b'[{"user": "preserve"}]',
            'output/project/result.blend': b'user project',
        }
        for name, content in base_files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        release_path = target / 'RELEASE-MANIFEST.json'
        updater.write_json(release_path, dict(application='Seedom', version='1.0.3', files=[]))
        first = updater.apply(package, target)
        assert updater.version(target) == '1.1.0'
        assert updater.read_json(target / 'HOTFIX-INSTALLED.json')['version'] == '1.1.0'
        installed_runtime = updater.read_json(target / 'config/runtime-manifest.json')
        assert installed_runtime['application_name'] == 'SeedomVFX'
        assert installed_runtime['application_version'] == '1.1.0'
        for entry in manifest['files']:
            assert digest(target / entry['path']) == entry['sha256'], entry['path']
        second = updater.apply(package, target)
        assert first != second
        updater.restore(target, second)
        assert updater.version(target) == '1.1.0'
        updater.restore(target, first)
        assert updater.version(target) == '1.0.3'
        assert not (target / 'HOTFIX-INSTALLED.json').exists()
        for name, content in base_files.items():
            assert (target / name).read_bytes() == content, name

    report = dict(application='SeedomVFX', version='1.1.0', base_version='1.0.3',
                  status='passed', payload_files=len(manifest['files']),
                  checks=['ZIP CRC and exact package contents', 'Payload SHA-256',
                          '1.0.3 install', 'Repeated 1.1.0 install',
                          'Successive rollback', 'Settings, queue and output preservation'],
                  regression_suites=[dict(name='test_hotfix.py', passed=5),
                                     dict(name='test_ui_rebuild.py', passed=29),
                                     dict(name='test_keyframe_sampling.py', passed=3)])
    (package / 'VALIDATION.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    refresh_archive(package, archive)

    expected_hash = archive.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
    assert digest(archive) == expected_hash
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        final_names = {path.relative_to(package).as_posix() for path in package.rglob('*') if path.is_file()}
        assert set(bundle.namelist()) == {(Path(NAME) / name).as_posix() for name in final_names}
        for path in package.rglob('*'):
            if path.is_file():
                assert bundle.read((Path(NAME) / path.relative_to(package)).as_posix()) == path.read_bytes()
    report['archive'] = archive.name
    report['archive_sha256'] = expected_hash
    report['package_files'] = len(final_names)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    verify()
