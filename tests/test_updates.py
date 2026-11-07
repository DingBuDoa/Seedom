"""GitHub release discovery, untrusted ZIPs, cancellation and real rollback."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import hotfix
import updates
from update_installer import install_lock, wait_for_exit


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(root, version='1.1.2', bases=None):
    bases = bases or ['1.1.1', version]
    name = f'SeedomVFX-{version}-Hotfix-20261107'
    package = root / name
    entries = []
    for name, data in [('app/app_info.py', f"APP_VERSION = '{version}'\n".encode()),
                       ('app/new_module.py', b'VALUE = 1\n')]:
        file = package / 'payload' / name
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
        entries.append(dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    manifest = dict(schema_version=1, application='Seedom', platform='windows-x64', version=version,
                    base_versions=bases, build_date='2026-11-07', patch_id=package.name, files=entries)
    write_json(package / 'PATCH-MANIFEST.json', manifest)
    archive = root / (package.name + '.zip')
    with zipfile.ZipFile(archive, 'w') as bundle:
        for file in package.rglob('*'):
            if file.is_file():
                bundle.write(file, (Path(package.name) / file.relative_to(package)).as_posix())
    document = dict(schema_version=1, application='Seedom', platform='windows-x64', version=version,
                    base_versions=bases, build_date='2026-11-07', notes='测试更新',
                    package=dict(name=archive.name, bytes=archive.stat().st_size,
                                 sha256=hotfix.digest(archive),
                                 url=f'https://github.com/DingBuDoa/Seedom/releases/download/v{version}/{archive.name}'))
    return package, archive, document


def installation(root, version='1.1.1'):
    for name in ('Start.bat', 'app/launch.py', 'runtime/python/python.exe',
                 'runtime/fastmap/fastmap/container.py', 'config/settings.json',
                 'config/task-history.json', 'config/task-queue.json', 'output/project/user.blend'):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(b'user-data')
    info = root / 'app/app_info.py'; info.write_text(f"APP_VERSION = '{version}'\n", encoding='utf-8')
    write_json(root / 'RELEASE-MANIFEST.json', dict(version=version, files=[
        dict(path='app/app_info.py', bytes=info.stat().st_size, sha256=hotfix.digest(info))]))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/artifacts')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_github_uses_highest_compatible_stable_release_and_verifies_asset(self):
        _, _, document = fixture(self.root)
        newer = dict(document, version='1.1.3', base_versions=['1.1.2', '1.1.3'],
                     package={**document['package'], 'name': 'next.zip',
                              'url': 'https://github.com/DingBuDoa/Seedom/releases/download/v1.1.3/next.zip'})
        def release(doc, **flags):
            return dict(tag_name='v' + doc['version'], draft=False, prerelease=False,
                        assets=[dict(name='update.json', browser_download_url=f"https://github.com/DingBuDoa/Seedom/releases/download/v{doc['version']}/update.json"),
                                dict(name=doc['package']['name'], size=doc['package']['bytes'], browser_download_url=doc['package']['url'])], **flags)
        releases = [release(newer), release(document), dict(tag_name='v9.0.0', prerelease=True)]
        def read(url):
            return releases if url == updates.API_URL else newer if '/v1.1.3/' in url else document
        with patch('updates.read_remote_json', side_effect=read):
            result = updates.check_for_updates('1.1.1')
            self.assertEqual(result['update']['version'], '1.1.2')
            self.assertEqual(updates.check_for_updates('1.1.3')['status'], 'current')
            self.assertEqual(updates.check_for_updates('1.0.0')['status'], 'manual')
        releases[1]['assets'][1]['size'] += 1
        with patch('updates.read_remote_json', side_effect=read), self.assertRaisesRegex(ValueError, '匹配'):
            updates.check_for_updates('1.1.1')

    def test_download_caches_verified_bytes_and_cancel_keeps_installation_untouched(self):
        _, archive, document = fixture(self.root)
        content = archive.read_bytes()
        progress = []
        with patch('updates.open_url', return_value=io.BytesIO(content)) as opened:
            saved = updates.download_package(document, self.root / 'downloads', lambda *args: progress.append(args))
            self.assertEqual(saved.read_bytes(), content)
            updates.download_package(document, self.root / 'downloads')
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(progress[-1], (len(content), len(content)))
        cancelled = threading.Event(); cancelled.set()
        with patch('updates.open_url', return_value=io.BytesIO(content)), self.assertRaises(updates.UpdateCancelled):
            updates.download_package(document, self.root / 'cancelled', cancel=cancelled)
        self.assertFalse(list((self.root / 'cancelled').glob('*')))
        with patch('updates.open_url', return_value=io.BytesIO(content[:-1])), self.assertRaisesRegex(ValueError, '校验'):
            updates.download_package(document, self.root / 'truncated')
        self.assertFalse(list((self.root / 'truncated').glob('*')))

    def test_real_stage_install_repeat_and_restore_preserve_user_data(self):
        _, archive, document = fixture(self.root)
        staged = updates.stage_package(archive, document, self.root / 'cache', current_version='1.1.1')
        target = self.root / 'installed'; installation(target)
        original_manifest = (target / 'RELEASE-MANIFEST.json').read_bytes()
        first = hotfix.apply(staged, target)
        self.assertEqual(hotfix.version(target), '1.1.2')
        second = hotfix.apply(staged, target)
        hotfix.restore(target, second); hotfix.restore(target, first)
        self.assertEqual(hotfix.version(target), '1.1.1')
        self.assertEqual((target / 'RELEASE-MANIFEST.json').read_bytes(), original_manifest)
        self.assertFalse((target / 'app/new_module.py').exists())
        for path in ('config/settings.json', 'config/task-history.json', 'config/task-queue.json', 'output/project/user.blend'):
            self.assertEqual((target / path).read_bytes(), b'user-data')

    def test_corruption_and_mismatched_manifest_are_rejected_before_backups(self):
        package, archive, document = fixture(self.root)
        with self.assertRaisesRegex(ValueError, '不一致'):
            updates.stage_package(archive, dict(document, base_versions=['1.1.1']), self.root / 'cache', '1.1.1')
        (package / 'payload/app/new_module.py').write_bytes(b'corrupted')
        target = self.root / 'installed'; installation(target)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            hotfix.apply(package, target)
        self.assertFalse((target / 'patch-backups').exists())
        self.assertEqual(hotfix.version(target), '1.1.1')

    def test_interrupted_copy_restores_old_version_and_added_files(self):
        package, _, _ = fixture(self.root)
        target = self.root / 'installed'; installation(target)
        original = hotfix.replace_file
        calls = []
        def fail_version_once(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError('interrupted')
            original(source, destination)
        with patch('hotfix.replace_file', side_effect=fail_version_once), self.assertRaisesRegex(OSError, 'interrupted'):
            hotfix.apply(package, target)
        self.assertEqual(calls[0].name, 'new_module.py')
        self.assertEqual(hotfix.version(target), '1.1.1')
        self.assertFalse((target / 'app/new_module.py').exists())
        self.assertFalse((target / 'HOTFIX-INSTALLED.json').exists())

    def test_traversal_links_duplicates_and_wrong_sources_are_rejected(self):
        _, archive, document = fixture(self.root)
        for name in ('../outside', 'C:/outside', 'app\\bad.py', '/absolute', 'app/./bad.py', 'app/NUL.py', 'app/trailing.'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                hotfix.safe_path(self.root, name)
        for attack in ('traversal', 'link', 'duplicate'):
            bad = self.root / (attack + '.zip')
            with zipfile.ZipFile(bad, 'w') as bundle:
                if attack == 'link':
                    info = zipfile.ZipInfo('patch/link'); info.external_attr = 0o120777 << 16
                    bundle.writestr(info, b'outside')
                elif attack == 'duplicate':
                    bundle.writestr('patch/app/A.py', b'a'); bundle.writestr('patch/app/a.py', b'b')
                else:
                    bundle.writestr('../outside', b'bad')
            altered = dict(document, package={**document['package'], 'bytes': bad.stat().st_size, 'sha256': hotfix.digest(bad)})
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                updates.stage_package(bad, altered, self.root / 'cache', '1.1.1')
        self.assertFalse((self.root.parent / 'outside').exists())
        self.assertFalse(list((self.root / 'cache').iterdir()))
        for url in ('http://github.com/DingBuDoa/Seedom/a', 'https://example.com/a',
                    'https://github.com/Other/Seedom/releases/download/v1.1.2/a.zip'):
            with self.assertRaises(ValueError):
                updates.trusted_url(url, asset=True)

    def test_installer_waits_for_application_and_rejects_concurrent_installers(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        try:
            with self.assertRaises(TimeoutError):
                wait_for_exit(child.pid, timeout=.05)
            child.terminate(); child.wait(timeout=5)
            wait_for_exit(child.pid, timeout=.5)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
        target = self.root / 'installed'; target.mkdir()
        with install_lock(target):
            with self.assertRaises(OSError):
                with install_lock(target):
                    pass

    def test_snapshot_installer_runs_with_the_isolated_bundled_python(self):
        package, _, _ = fixture(self.root)
        target = self.root / 'installed'; installation(target)
        snapshot = self.root / 'snapshot'; snapshot.mkdir()
        for name in ('hotfix.py', 'update_installer.py'):
            shutil.copyfile(ROOT / 'app' / name, snapshot / name)
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=5)
        result = subprocess.run([sys.executable, '-B', str(snapshot / 'update_installer.py'),
            '--target', str(target), '--package', str(package), '--wait-pid', str(child.pid), '--no-restart'],
            cwd=str(snapshot), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(hotfix.version(target), '1.1.2')
        self.assertIn('UPDATE COMPLETE', (snapshot / 'installer.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
