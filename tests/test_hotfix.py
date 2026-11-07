"""Public repository link and offline hotfix recovery regressions."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'tools'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import apply_hotfix as updater


class HotfixTests(unittest.TestCase):
    def fixture(self, directory):
        target, package = Path(directory) / 'installed', Path(directory) / 'patch'
        for name in ('Start.bat', 'app/launch.py', 'runtime/python/python.exe',
                     'runtime/fastmap/fastmap/container.py', 'config/settings.json', 'output/project/user.blend'):
            file = target / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'user-owned-data')
        info = target / 'app/app_info.py'
        info.write_text("APP_VERSION = '1.0.1'\n", encoding='utf-8')
        manifest = dict(application='PointStudio', version='1.0.1', build_date='2026-10-24', files=[
            dict(path='app/app_info.py', bytes=info.stat().st_size, sha256=updater.digest(info))])
        updater.write_json(target / 'RELEASE-MANIFEST.json', manifest)
        entries = []
        for name, text in [('app/app_info.py', "APP_VERSION = '1.0.2'\n"),
                           ('app/task_progress.py', '# new progress module\n')]:
            file = package / 'payload' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding='utf-8')
            entries.append(dict(path=name, bytes=file.stat().st_size, sha256=updater.digest(file)))
        updater.write_json(package / 'PATCH-MANIFEST.json', dict(application='PointStudio', version='1.0.2',
            patch_id='test-1.0.2', build_date='2026-10-24', files=entries))
        return target, package

    def test_install_repeat_and_restore_keep_user_data_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target, package = self.fixture(directory)
            before = (target / 'RELEASE-MANIFEST.json').read_bytes()
            first = updater.apply(package, target)
            self.assertEqual(updater.version(target), '1.0.2')
            self.assertEqual(updater.read_json(target / 'RELEASE-MANIFEST.json')['version'], '1.0.2')
            for entry in updater.read_json(target / 'RELEASE-MANIFEST.json')['files']:
                self.assertEqual(updater.digest(target / entry['path']), entry['sha256'])
            second = updater.apply(package, target)
            self.assertNotEqual(first, second)
            updater.restore(target, second)
            self.assertEqual(updater.version(target), '1.0.2')
            updater.restore(target, first)
            self.assertEqual(updater.version(target), '1.0.1')
            self.assertFalse((target / 'app/task_progress.py').exists())
            self.assertFalse((target / 'HOTFIX-INSTALLED.json').exists())
            self.assertEqual((target / 'RELEASE-MANIFEST.json').read_bytes(), before)
            for name in ('config/settings.json', 'output/project/user.blend'):
                self.assertEqual((target / name).read_bytes(), b'user-owned-data')

    def test_corrupt_payload_is_rejected_before_any_install_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            target, package = self.fixture(directory)
            (package / 'payload/app/task_progress.py').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                updater.apply(package, target)
            self.assertEqual(updater.version(target), '1.0.1')
            self.assertFalse((target / 'patch-backups').exists())

    def test_copy_error_rolls_back_partial_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            target, package = self.fixture(directory)
            original = updater.replace_file
            count = 0
            def fail_once(source, destination):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError('simulated interrupted copy')
                original(source, destination)
            with patch.object(updater, 'replace_file', side_effect=fail_once):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    updater.apply(package, target)
            self.assertEqual(updater.version(target), '1.0.1')
            self.assertFalse((target / 'app/task_progress.py').exists())
            backup = next((target / 'patch-backups').iterdir())
            self.assertEqual(updater.read_json(backup / 'receipt.json')['status'], 'restored')

    def test_bad_backup_or_destination_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            target, package = self.fixture(directory)
            backup = updater.apply(package, target)
            (backup / 'files/app/app_info.py').write_bytes(b'broken backup')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                updater.restore(target, backup)
            self.assertEqual(updater.version(target), '1.0.2')
            for name in ('../output/project/user.blend', 'C:/outside', 'app\\other.py'):
                with self.assertRaises(ValueError):
                    updater.safe_path(target, name)

    def test_about_has_version_and_clickable_project_repository(self):
        from PySide6.QtWidgets import QApplication, QLabel
        from qt_about import about_page
        from qt_app import configure_application
        from app_info import PROJECT_URL, APP_VERSION
        app = QApplication.instance() or QApplication([])
        configure_application(app)
        page = about_page(lambda path: None)
        page.resize(960, 1100); page.show(); app.processEvents()
        link = page.findChild(QLabel, 'aboutRepository')
        self.assertIn(PROJECT_URL, link.text())
        self.assertIn(APP_VERSION, page.findChild(QLabel, 'aboutRelease').text())
        with patch('qt_about.QDesktopServices.openUrl') as opened:
            link.linkActivated.emit(PROJECT_URL)
            self.assertEqual(opened.call_args.args[0].toString(), PROJECT_URL)
        artifacts = ROOT / 'tests/artifacts/hotfix'
        artifacts.mkdir(parents=True, exist_ok=True)
        page.grab().save(str(artifacts / 'about-1.1.0.png'))
        page.close(); page.deleteLater(); app.processEvents()


if __name__ == '__main__':
    unittest.main()
