"""Build a standard offline/online Seedom hotfix and its GitHub update.json."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from app_info import APP_NAME, APP_VERSION, BUILD_DATE, PROJECT_URL
from hotfix import validate_package
from updates import validate_update, version_key


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def payload_sources(root, version):
    for directory, suffixes in (('app', {'.py', '.svg'}), ('photogrammetry_importer', {'.py'})):
        for source in sorted((root / directory).rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix in suffixes:
                yield source
    for source in sorted((root / 'runtime/fastmap/fastmap').glob('*')):
        if source.is_file() and (source.suffix == '.py' or source.name == 'optimization_profiles.json'):
            yield source
    for name in ('README.md', 'UPDATE-STANDARD.md', 'UPDATE-TEMPLATE.json',
                 f'CHANGELOG-{version}.md', 'config/runtime-manifest.json'):
        yield root / name


def write_publish_documents(output, version, base_versions, root=ROOT):
    for name in ('UPDATE-STANDARD.md', 'UPDATE-TEMPLATE.json'):
        shutil.copyfile(root / name, output / name)
    shutil.copyfile(root / 'docs/updates/update.schema.json', output / 'update.schema.json')
    (output / '发布指引.md').write_text(f'''# 发布 SeedomVFX {version}

1. 打开 {PROJECT_URL}/releases/new ，创建标签 `v{version}`。
2. 版本标题填写“拾点VFX（SeedomVFX）{version}”，说明参考补丁内的更新日志。
3. 上传本目录的热补丁 ZIP，再上传 `update.json`。可同时上传 `.zip.sha256` 和 `VALIDATION.json`。
4. 确认附件齐全后发布正式 Release；不要标记为预发布。

支持版本 `{'、'.join(base_versions)}` 的用户直接安装本次累积补丁；后续版本可在软件中检查并安装热补丁。
`update.json` 是 Release 附件，不需要把它放入源码仓库根目录。
这些文件只在本地生成，打包工具不会上传、发布或修改 GitHub。
后续版本的流程见本目录 `UPDATE-STANDARD.md`。
''', encoding='utf-8')


def build(output, base_versions, root=ROOT):
    root, output = Path(root), Path(output)
    from hotfix import version as installed_version
    version = installed_version(root)
    version_key(version)
    bases = list(dict.fromkeys([*base_versions, version]))
    for base in bases:
        version_key(base)
    name = f'SeedomVFX-{version}-Hotfix-{BUILD_DATE.replace("-", "")}'
    folder, archive = output / name, output / (name + '.zip')
    if folder.exists() or archive.exists() or (output / 'update.json').exists():
        raise FileExistsError('输出目录已有同名更新文件，请选择新的目录；不会覆盖旧补丁。')
    output.mkdir(parents=True, exist_ok=True); folder.mkdir()
    entries = []
    for source in payload_sources(root, version):
        if source.is_symlink():
            raise ValueError(f'Patch input must be a real file: {source}')
        relative = source.relative_to(root).as_posix()
        destination = folder / 'payload' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entries.append(dict(path=relative, bytes=destination.stat().st_size, sha256=digest(destination)))
    manifest = dict(schema_version=1, application='Seedom', display_name=APP_NAME,
                    platform='windows-x64', version=version, base_versions=bases,
                    build_date=BUILD_DATE, patch_id=name, project_url=PROJECT_URL, files=entries)
    write_json(folder / 'PATCH-MANIFEST.json', manifest)
    shutil.copy2(root / 'app/hotfix.py', folder / 'apply_hotfix.py')
    (folder / 'Apply-Hotfix.bat').write_bytes((
        '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
        'set "SEEDOM_TARGET=%~dp0.."\r\n'
        'if not exist "%SEEDOM_TARGET%\\runtime\\python\\python.exe" (\r\n'
        '  echo Put this whole hotfix folder beside Start.bat, app and runtime.\r\n'
        '  pause\r\n  exit /b 1\r\n)\r\n'
        'echo Close SeedomVFX and all solver / Blender export processes first.\r\npause\r\n'
        '"%SEEDOM_TARGET%\\runtime\\python\\python.exe" -B "%~dp0apply_hotfix.py" --target "%SEEDOM_TARGET%"\r\n'
        'set "SEEDOM_EXIT=%ERRORLEVEL%"\r\npause\r\nexit /b %SEEDOM_EXIT%\r\n').encode('ascii'))
    instructions = f'''# {APP_NAME} {version} 热补丁

构建日期：{BUILD_DATE}。支持升级的版本：{'、'.join(bases)}。
项目主页：{PROJECT_URL}

## 手动安装

1. 关闭软件及其求解器、Blender 导出进程。
2. 解压 ZIP，将整个 `{name}` 文件夹放入原软件目录，与 `Start.bat`、`app`、`runtime` 同级。
3. 双击补丁目录内的 `Apply-Hotfix.bat`，按提示继续。
4. 提示 `UPDATE COMPLETE` 后，用原来的 `Start.bat` 启动，在关于页确认版本 {version}。

不要只复制单个 Python 文件。无需重新下载运行库、安装 Python 或联网。

## 后续在线更新

安装本次补丁后，在设置 → 软件更新检查 GitHub、下载热补丁，待任务结束后点击“安装并重启”。
启动自动检查可以在设置中关闭；不会在任务执行中自动替换文件。

## 备份与回退

更新前会校验每个 payload 文件。被替换文件备份到原软件目录 `patch-backups/{version}-时间戳/`。
关闭软件后，运行对应备份中的 `Restore.bat` 回退；多次更新时按时间从新到旧回退。
个人设置、任务队列、耗时参考、日志与 output 项目不会被覆盖。
断电或强制终止后，请先运行最近备份内的 `Restore.bat`，再启动软件。
更新下载和安装日志位于原软件目录 `.updates/`，请保留需要用于回退的 `patch-backups/`。

修改内容见 payload/CHANGELOG-{version}.md，后续发布规范见 payload/UPDATE-STANDARD.md。
'''
    (folder / '安装说明.md').write_text(instructions, encoding='utf-8-sig')
    validate_package(folder, base_versions[0])
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for source in sorted(folder.rglob('*')):
            if source.is_file():
                bundle.write(source, (Path(name) / source.relative_to(folder)).as_posix())
    checksum = digest(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    document = dict(schema_version=1, application='Seedom', platform='windows-x64',
                    version=version, base_versions=bases, build_date=BUILD_DATE,
                    notes=(root / f'CHANGELOG-{version}.md').read_text(encoding='utf-8-sig'),
                    package=dict(name=archive.name,
                                 url=f'{PROJECT_URL}/releases/download/v{version}/{archive.name}',
                                 bytes=archive.stat().st_size, sha256=checksum))
    validate_update(document); write_json(output / 'update.json', document)
    write_publish_documents(output, version, bases, root)
    print(json.dumps(dict(version=version, folder=str(folder), zip=str(archive),
                          update_json=str(output / 'update.json'), payload_files=len(entries),
                          zip_bytes=archive.stat().st_size), ensure_ascii=False))
    return folder, archive, document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-version', action='append', required=True,
                        help='可重复指定，表示实际验证过的升级来源版本')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'release-assets' / APP_VERSION)
    args = parser.parse_args()
    build(args.output_dir.resolve(), args.base_version)


if __name__ == '__main__':
    main()
