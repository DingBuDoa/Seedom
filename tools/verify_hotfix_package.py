"""Verify the actual ZIP and install/restore its payload in a disposable fixture."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_hotfix import NAME, PAYLOAD, ROOT


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    package = ROOT / NAME
    archive = ROOT / (NAME + '.zip')
    expected = archive.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
    assert digest(archive) == expected, 'Archive SHA256 mismatch'
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None, 'ZIP CRC mismatch'
        files = [p for p in package.rglob('*') if p.is_file()]
        entries = {p.relative_to(ROOT).as_posix(): p for p in files}
        assert len(bundle.namelist()) == len(entries), 'Unexpected ZIP entries'
        assert set(bundle.namelist()) == set(entries), 'ZIP file set mismatch'
        for name, source in entries.items():
            assert bundle.read(name) == source.read_bytes(), name
    for line in (package / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        checksum, name = line.split('  ', 1)
        assert digest(package / name) == checksum, name
    manifest = json.loads((package / 'PATCH-MANIFEST.json').read_text(encoding='utf-8'))
    assert [e['path'] for e in manifest['files']] == list(PAYLOAD)
    for entry in manifest['files']:
        assert digest(ROOT / entry['path']) == entry['sha256'], entry['path']
        assert digest(package / 'payload' / entry['path']) == entry['sha256'], entry['path']

    spec = importlib.util.spec_from_file_location('packaged_hotfix', package / 'apply_hotfix.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    with tempfile.TemporaryDirectory(prefix='seedom-package-') as directory:
        target = Path(directory) / 'installation'
        initial = {
            'Start.bat': b'baseline launcher',
            'app/launch.py': b'# baseline launcher',
            'app/app_info.py': b"APP_VERSION = '1.0.1'\n",
            'runtime/python/python.exe': b'fixture only, never executed',
            'runtime/fastmap/fastmap/container.py': b'# baseline container',
            'config/settings.json': b'{"user": "preserved"}',
            'output/user-project/user.blend': b'user project preserved',
        }
        for name, content in initial.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        backups = [updater.apply(package, target), updater.apply(package, target)]
        assert backups[0] != backups[1]
        assert updater.version(target) == '1.0.2'
        for entry in manifest['files']:
            assert digest(target / entry['path']) == entry['sha256'], entry['path']
        updater.restore(target, backups[1])
        assert updater.version(target) == '1.0.2'
        updater.restore(target, backups[0])
        assert updater.version(target) == '1.0.1'
        for name, content in initial.items():
            assert (target / name).read_bytes() == content, name
        for entry in manifest['files']:
            if entry['path'] not in initial:
                assert not (target / entry['path']).exists(), entry['path']
        assert not (target / 'HOTFIX-INSTALLED.json').exists()
    report = dict(status='passed', archive=archive.name, sha256=expected,
                  payload_files=len(PAYLOAD), archive_files=len(entries),
                  checks=['ZIP CRC', 'ZIP and folder byte identity', 'Package checksums',
                          'Payload and current source identity', 'Actual packaged updater installation',
                          'Repeated installation', 'Two successive restores',
                          'New JSON and Python modules removed on restore', 'User settings and project preserved'])
    report_path = ROOT / 'tests/artifacts/hotfix/package-verification.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    verify()
