"""Offline, verified Seedom updates with transactional backups and rollback."""
import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import uuid


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                   or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
                   for part in name.split('/'))):
        raise ValueError(f'Unsafe path: {name}')
    relative = PurePosixPath(name)
    if relative.is_absolute():
        raise ValueError(f'Unsafe path: {name}')
    target = root.joinpath(*relative.parts)
    cursor = target
    while cursor != root:
        if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
            raise ValueError(f'Linked destinations are not supported: {cursor}')
        cursor = cursor.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes installation: {name}')
    return target


def version(target):
    tree = ast.parse((target / 'app/app_info.py').read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Cannot read Seedom version.')


def replace_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.hotfix-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore(target, backup):
    """Restore only files named by a verified backup receipt."""
    receipt = read_json(backup / 'receipt.json')
    if Path(receipt['target']).resolve() != target.resolve():
        raise ValueError('Backup belongs to another installation.')
    entries = receipt['files']
    for entry in entries:
        safe_path(target, entry['path'])
        if entry['existed']:
            original = safe_path(backup / 'files', entry['path'])
            if digest(original) != entry['sha256']:
                raise ValueError(f'Backup checksum mismatch: {entry["path"]}')
    # Restore version metadata last, so partially failed rollback never claims success.
    for entry in sorted(entries, key=lambda entry: entry['path'] == 'app/app_info.py'):
        destination = safe_path(target, entry['path'])
        if entry['existed']:
            replace_file(safe_path(backup / 'files', entry['path']), destination)
        else:
            destination.unlink(missing_ok=True)
    receipt['status'] = 'restored'
    write_json(backup / 'receipt.json', receipt)


def validate_package(package, current_version=None):
    """Validate all sources before backups, installation, or code execution."""
    package = Path(package).resolve()
    manifest = read_json(package / 'PATCH-MANIFEST.json')
    if manifest.get('application') not in ('Seedom', 'PointStudio') or not isinstance(manifest.get('version'), str):
        raise ValueError('Unsupported patch.')
    patch_version = manifest['version']
    declared_versions = manifest.get('base_versions')
    if declared_versions is None:
        base_version = manifest.get('base_version') or ('1.0.1' if patch_version == '1.0.2' else None)
        declared_versions = [base_version] if base_version else []
    supported_versions = {item for item in declared_versions if isinstance(item, str)}
    supported_versions.add(patch_version)
    if current_version is not None and current_version not in supported_versions:
        raise ValueError(f"This patch supports Seedom {', '.join(sorted(supported_versions))} only.")
    if manifest.get('schema_version', 1) != 1 or manifest.get('platform', 'windows-x64') != 'windows-x64':
        raise ValueError('Unsupported hotfix format or platform.')
    entries = manifest['files']
    if not isinstance(entries, list) or not entries:
        raise ValueError('Empty patch manifest.')
    seen = set()
    for entry in entries:
        name = entry['path']
        path = PurePosixPath(name)
        permitted = (name in ('README.md', 'UPDATE-STANDARD.md', 'UPDATE-TEMPLATE.json', 'LOGO.ico')
                     or (path.parent == PurePosixPath('.') and name.startswith('CHANGELOG-') and path.suffix == '.md')
                     or name == 'config/runtime-manifest.json'
                     or (bool(path.parts) and path.parts[0] == 'app' and path.suffix in ('.py', '.svg'))
                     or (bool(path.parts) and path.parts[0] == 'photogrammetry_importer' and path.suffix == '.py')
                     or name == 'runtime/fastmap/fastmap/optimization_profiles.json'
                     or (path.parent == PurePosixPath('runtime/fastmap/fastmap') and path.suffix == '.py'))
        if not permitted or name.casefold() in seen:
            raise ValueError(f'Unexpected patch destination: {name}')
        seen.add(name.casefold())
        source = safe_path(package / 'payload', name)
        if (type(entry.get('bytes')) is not int or entry['bytes'] < 0
                or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or source.stat().st_size != entry['bytes'] or digest(source) != entry['sha256']):
            raise ValueError(f'Patch checksum mismatch: {name}')
        if source.suffix == '.py':
            compile(source.read_bytes(), str(source), 'exec')
    if 'app/app_info.py' not in seen:
        raise ValueError('Incomplete patch manifest.')
    if version(package / 'payload') != patch_version:
        raise ValueError('Payload version differs from patch manifest.')
    return manifest


def apply(package, target):
    package, target = Path(package).resolve(), Path(target).resolve()
    manifest = validate_package(package, version(target))
    patch_version = manifest['version']
    entries = sorted(manifest['files'], key=lambda entry: entry['path'] == 'app/app_info.py')
    for name in ('Start.bat', 'app/launch.py', 'runtime/python/python.exe',
                 'runtime/fastmap/fastmap/container.py'):
        if not safe_path(target, name).is_file():
            raise ValueError(f'Not a complete Seedom installation: missing {name}')
    for entry in entries:
        safe_path(target, entry['path'])
    # Check every payload before creating a backup or changing the installation.
    backups = safe_path(target, 'patch-backups')
    backups.mkdir(exist_ok=True)
    backup = backups / (patch_version + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    backup.mkdir()
    modified = [entry['path'] for entry in entries] + ['HOTFIX-INSTALLED.json']
    release_manifest = safe_path(target, 'RELEASE-MANIFEST.json')
    release = read_json(release_manifest) if release_manifest.is_file() else None
    if release is not None:
        modified.append('RELEASE-MANIFEST.json')
    receipt = dict(target=str(target), patch=manifest['patch_id'], status='prepared', files=[])
    for name in modified:
        destination = safe_path(target, name)
        entry = dict(path=name, existed=destination.exists())
        if entry['existed']:
            original = safe_path(backup / 'files', name)
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, original)
            entry['sha256'] = digest(original)
        receipt['files'].append(entry)
    write_json(backup / 'receipt.json', receipt)
    shutil.copy2(Path(__file__), backup / 'apply_hotfix.py')
    (backup / 'Restore.bat').write_text(
        '@echo off\r\ncd /d "%~dp0"\r\n'
        '"..\\..\\runtime\\python\\python.exe" -B apply_hotfix.py --target "..\\.." --restore .\r\n'
        'pause\r\n', encoding='ascii')
    try:
        for entry in entries:
            replace_file(package / 'payload' / entry['path'], safe_path(target, entry['path']))
        write_json(safe_path(target, 'HOTFIX-INSTALLED.json'),
                   dict(application=manifest['application'], version=patch_version, patch_id=manifest['patch_id'],
                        installed_at=datetime.now().isoformat(), backup=str(backup), files=entries))
        if release is not None:
            # Keep unmodified base hashes; replace only the files supplied by this patch.
            updated = {entry['path']: entry for entry in release['files']}
            for entry in entries:
                updated[entry['path']] = entry
            installed = target / 'HOTFIX-INSTALLED.json'
            updated['HOTFIX-INSTALLED.json'] = dict(path='HOTFIX-INSTALLED.json',
                                                  bytes=installed.stat().st_size, sha256=digest(installed))
            release.update(application=manifest['application'], version=patch_version,
                           build_date=manifest['build_date'], hotfix=manifest['patch_id'],
                           files=sorted(updated.values(), key=lambda entry: entry['path']))
            release.update(file_count=len(updated), total_bytes=sum(entry['bytes'] for entry in updated.values()))
            write_json(release_manifest, release)
        for entry in entries:
            if digest(safe_path(target, entry['path'])) != entry['sha256']:
                raise ValueError(f'Installed checksum mismatch: {entry["path"]}')
        receipt['status'] = 'installed'
        write_json(backup / 'receipt.json', receipt)
    except BaseException:
        restore(target, backup)
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--restore', type=Path)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    target = args.target.resolve()
    print('Close SeedomVFX and all its solver / Blender export processes before updating.', flush=True)
    try:
        if args.restore:
            restore(target, args.restore.resolve())
            print('RESTORE COMPLETE')
        else:
            backup = apply(args.package, target)
            installed_manifest = read_json(args.package / 'PATCH-MANIFEST.json')
            display_name = installed_manifest.get('display_name', 'Seedom')
            print(f"UPDATE COMPLETE: {display_name} {installed_manifest['version']}\nBackup: {backup}\nStart with Start.bat")
        return 0
    except Exception as error:
        print(f'UPDATE FAILED: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
