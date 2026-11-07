"""GitHub hotfix discovery and bounded, verified downloads; no GUI dependencies."""
import hashlib
from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid
import zipfile

from app_info import APP_VERSION, PROJECT_URL
from hotfix import safe_path, validate_package

REPOSITORY = 'DingBuDoa/Seedom'
RELEASES_URL = PROJECT_URL + '/releases'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=20'
MAX_PACKAGE_BYTES = 256 * 2**20
MAX_UNPACKED_BYTES = 512 * 2**20


class UpdateCancelled(Exception):
    pass


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('版本号必须采用主版本.次版本.补丁版本格式。')
    return tuple(map(int, value.split('.')))


def trusted_url(url, asset=False):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('更新地址必须是 GitHub HTTPS 地址。')
    host = (parsed.hostname or '').casefold()
    if host not in ('github.com', 'api.github.com') and not host.endswith('.githubusercontent.com'):
        raise ValueError('更新地址不属于 GitHub。')
    if asset and (host != 'github.com' or not parsed.path.casefold().startswith(f'/{REPOSITORY}/releases/download/'.casefold())):
        raise ValueError('更新文件必须来自 Seedom 仓库的 Release。')
    return url


class GithubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    trusted_url(url)
    request = Request(url, headers={'User-Agent': f'SeedomVFX/{APP_VERSION}',
                                   'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/') else 'application/octet-stream'})
    try:
        return build_opener(GithubRedirect()).open(request, timeout=20)
    except HTTPError as error:
        if error.code in (403, 429):
            raise RuntimeError('GitHub 请求受限，请稍后再试，或从发布页手动下载。') from error
        raise RuntimeError(f'GitHub 返回 HTTP {error.code}，请稍后重试。') from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError('无法连接 GitHub，请检查网络，或稍后手动检查更新。') from error


def read_remote_json(url, limit=2**20):
    with open_url(url) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError('更新清单过大。')
    return json.loads(content.decode('utf-8-sig'))


def validate_update(document):
    if not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1 or document.get('application') != 'Seedom':
        raise ValueError('不支持的更新清单格式。')
    if document.get('platform') != 'windows-x64':
        raise ValueError('更新包不适用于 Windows x64。')
    version_key(document['version'])
    bases = document.get('base_versions')
    if not isinstance(bases, list) or not bases:
        raise ValueError('更新清单缺少可升级的旧版本。')
    for value in bases:
        version_key(value)
        if version_key(value) > version_key(document['version']):
            raise ValueError('可升级的来源版本不能高于目标版本。')
    build_date = document.get('build_date')
    if not isinstance(build_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', build_date):
        raise ValueError('构建日期必须采用 YYYY-MM-DD 格式。')
    date.fromisoformat(build_date)
    package = document.get('package')
    if not isinstance(package, dict):
        raise ValueError('更新清单缺少热补丁。')
    name = package.get('name')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.zip', name):
        raise ValueError('热补丁 ZIP 文件名无效。')
    trusted_url(package['url'], asset=True)
    expected_path = f'/{REPOSITORY}/releases/download/v{document["version"]}/{name}'
    if urlsplit(package['url']).path.casefold() != expected_path.casefold():
        raise ValueError('热补丁地址与版本或文件名不一致。')
    if type(package.get('bytes')) is not int or not 0 < package['bytes'] <= MAX_PACKAGE_BYTES:
        raise ValueError('热补丁大小无效。')
    if not isinstance(package.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', package['sha256']):
        raise ValueError('热补丁缺少 SHA-256 校验值。')
    if not isinstance(document.get('notes'), str):
        raise ValueError('更新说明必须是文本。')
    return document


def check_for_updates(current_version=APP_VERSION):
    releases = read_remote_json(API_URL)
    if not isinstance(releases, list):
        raise ValueError('GitHub 发布列表格式无效。')
    newer = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            continue
        tag = release.get('tag_name', '')
        try:
            key = version_key(tag.removeprefix('v'))
        except (ValueError, AttributeError):
            continue
        if key > version_key(current_version):
            newer.append((key, release))
    for key, release in sorted(newer, key=lambda item: item[0], reverse=True):
        manifest_asset = next((asset for asset in release.get('assets', []) if asset.get('name') == 'update.json'), None)
        if manifest_asset is None:
            continue
        url = trusted_url(manifest_asset['browser_download_url'], asset=True)
        document = validate_update(read_remote_json(url))
        if version_key(document['version']) != key:
            raise ValueError('更新清单版本与 GitHub 发布标签不一致。')
        package = document['package']
        matching = next((asset for asset in release.get('assets', []) if asset.get('name') == package['name']), None)
        if (matching is None or matching.get('size') != package['bytes']
                or matching.get('browser_download_url') != package['url']):
            raise ValueError('GitHub 发布中未找到与清单匹配的热补丁。')
        if current_version in document['base_versions']:
            return dict(status='available', update=document)
    if newer:
        return dict(status='manual', version='.'.join(map(str, max(item[0] for item in newer))))
    return dict(status='current')


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled('已取消下载。')


def download_package(document, cache, progress=None, cancel=None):
    validate_update(document)
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    package = document['package']
    archive = cache / package['name']
    if archive.is_file():
        with archive.open('rb') as stream:
            if archive.stat().st_size == package['bytes'] and hashlib.file_digest(stream, 'sha256').hexdigest() == package['sha256']:
                return archive
    temporary = cache / (package['name'] + '.' + uuid.uuid4().hex + '.part')
    received, last_report = 0, 0.0
    checksum = hashlib.sha256()
    try:
        with open_url(package['url']) as response, temporary.open('xb') as output:
            while True:
                check_cancel(cancel)
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > package['bytes']:
                    raise ValueError('下载文件超过清单声明的大小。')
                checksum.update(chunk); output.write(chunk)
                now = time.monotonic()
                if progress and (now - last_report >= .1 or received == package['bytes']):
                    progress(received, package['bytes']); last_report = now
        check_cancel(cancel)
        if received != package['bytes'] or checksum.hexdigest() != package['sha256']:
            raise ValueError('热补丁下载校验失败，请重新下载。')
        os.replace(temporary, archive)
        return archive
    finally:
        temporary.unlink(missing_ok=True)


def stage_package(archive, document, cache, current_version=APP_VERSION, cancel=None):
    validate_update(document)
    archive = Path(archive)
    with archive.open('rb') as stream:
        if archive.stat().st_size != document['package']['bytes'] or hashlib.file_digest(stream, 'sha256').hexdigest() != document['package']['sha256']:
            raise ValueError('热补丁 ZIP 校验失败。')
    stage = Path(cache) / ('staged-' + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > 4000 or sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
                raise ValueError('热补丁解压内容超出标准限制。')
            seen = set()
            for info in infos:
                name = info.filename.rstrip('/')
                destination = safe_path(stage, name)
                if name.casefold() in seen or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('热补丁包含重复路径或符号链接。')
                seen.add(name.casefold())
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                check_cancel(cancel)
                with bundle.open(info) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output, 256 * 1024)
        roots = [path for path in stage.iterdir() if path.is_dir() and (path / 'PATCH-MANIFEST.json').is_file()]
        if len(roots) != 1 or len(list(stage.iterdir())) != 1:
            raise ValueError('ZIP 必须包含一个完整的热补丁目录。')
        manifest = validate_package(roots[0], current_version)
        if (manifest.get('schema_version') != 1 or manifest.get('platform') != 'windows-x64'
                or manifest['version'] != document['version']
                or manifest.get('build_date') != document['build_date']
                or set(manifest.get('base_versions', [])) != set(document['base_versions'])
                or manifest.get('patch_id') != roots[0].name):
            raise ValueError('包内清单与在线更新清单不一致。')
        check_cancel(cancel)
        return roots[0]
    except BaseException:
        shutil.rmtree(stage)
        raise


def prepare_update(document, cache, current_version=APP_VERSION, progress=None, cancel=None):
    if current_version not in document['base_versions']:
        raise ValueError('此热补丁不支持当前版本，请先安装所需的中间版本。')
    cache = Path(cache) / document['version']
    archive = download_package(document, cache, progress, cancel)
    return stage_package(archive, document, cache, current_version, cancel)
