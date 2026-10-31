"""Build the offline SeedomVFX 1.0.3 -> 1.1.0 application hotfix."""
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'SeedomVFX-1.1.0-Hotfix-20261031'
VERSION = '1.1.0'
BASE_VERSION = '1.0.3'
PROJECT_URL = 'https://github.com/DingBuDoa/Seedom'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def payload_sources():
    for root_name in ('app', 'photogrammetry_importer'):
        root = ROOT / root_name
        for path in sorted(root.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix.lower() == '.pyc':
                continue
            if (root_name == 'app' and path.suffix.lower() not in ('.py', '.svg')
                    or root_name == 'photogrammetry_importer' and path.suffix.lower() != '.py'):
                continue
            yield path
    yield ROOT / 'README.md'
    yield ROOT / 'CHANGELOG-1.1.0.md'
    yield ROOT / 'config/runtime-manifest.json'


def build():
    destination = ROOT / NAME
    archive = ROOT / f'{NAME}.zip'
    if destination.exists() or archive.exists() or archive.with_suffix('.zip.sha256').exists():
        raise FileExistsError('Existing hotfix artifacts are preserved; remove or rename them before rebuilding.')
    destination.mkdir()
    entries = []
    for source in payload_sources():
        relative = source.relative_to(ROOT).as_posix()
        target = destination / 'payload' / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append(dict(path=relative, bytes=target.stat().st_size, sha256=digest(target)))

    manifest = dict(application='Seedom', display_name='拾点VFX（SeedomVFX）', base_version=BASE_VERSION,
                    base_versions=[BASE_VERSION, VERSION], version=VERSION,
                    build_date=date.today().isoformat(), patch_id=NAME,
                    project_url=PROJECT_URL, files=entries)
    (destination / 'PATCH-MANIFEST.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    shutil.copy2(ROOT / 'tools/apply_hotfix.py', destination / 'apply_hotfix.py')
    (destination / 'Apply-Hotfix.bat').write_text(
        '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
        'set "PS_TARGET=%~dp0.."\r\n'
        'if exist "%PS_TARGET%\\runtime\\python\\python.exe" goto install\r\n'
        'echo Move this entire hotfix folder inside the SeedomVFX installation folder.\r\n'
        'echo It must sit beside app, runtime and Start.bat.\r\npause\r\nexit /b 1\r\n'
        ':install\r\necho Target: "%PS_TARGET%"\r\n'
        'echo Close SeedomVFX, its solvers and Blender exports before continuing.\r\npause\r\n'
        '"%PS_TARGET%\\runtime\\python\\python.exe" -B "%~dp0apply_hotfix.py" --target "%PS_TARGET%"\r\n'
        'set "PS_RESULT=%ERRORLEVEL%"\r\npause\r\nexit /b %PS_RESULT%\r\n', encoding='ascii')
    instructions = f'''拾点VFX（SeedomVFX）1.1.0 热补丁（从 1.0.3 升级）
构建日期：{manifest['build_date']}
项目主页：{PROJECT_URL}

安装
1. 关闭 SeedomVFX、求解器和 Blender 导出进程。
2. 解压 ZIP，将整个 {NAME} 文件夹放进原软件安装目录。
   补丁文件夹应与 Start.bat、app、runtime 同级。
3. 双击补丁目录中的 Apply-Hotfix.bat，按提示继续。
4. 出现 UPDATE COMPLETE: 拾点VFX（SeedomVFX） 1.1.0 后，使用原来的 Start.bat 启动。
5. 在“设置 → 关于”确认版本为 1.1.0。

补丁只替换 PATCH-MANIFEST.json 列出的应用和 Blender 插件源码及说明文件，不包含运行库。
个人设置、任务队列、日志与 output 项目不会被覆盖。无需联网、无需安装 Python。
验证结果见补丁内 VALIDATION.json，单文件校验值见 SHA256SUMS.txt。

回退
安装前会校验全部补丁文件并备份被替换文件至原软件目录/patch-backups/1.1.0-时间戳/。
关闭软件后，运行备份内的 Restore.bat 回退。多次安装时按时间从新到旧回退。
'''
    (destination / '安装说明.txt').write_text(instructions, encoding='utf-8-sig')
    checksums = []
    for file in sorted(destination.rglob('*')):
        if file.is_file():
            checksums.append(f'{digest(file)}  {file.relative_to(destination).as_posix()}')
    (destination / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for file in sorted(destination.rglob('*')):
            if file.is_file():
                bundle.write(file, (Path(NAME) / file.relative_to(destination)).as_posix())
    archive.with_suffix('.zip.sha256').write_text(
        f'{digest(archive)}  {archive.name}\n', encoding='ascii')
    print(json.dumps(dict(folder=str(destination), zip=str(archive),
                          payload_files=len(entries), zip_bytes=archive.stat().st_size), ensure_ascii=False))
    return destination


if __name__ == '__main__':
    build()
