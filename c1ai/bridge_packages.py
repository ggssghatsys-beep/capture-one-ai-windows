"""Versioned source packages: prepare first, activate only with C1/owner stopped.

No C1 calls, authentication migration or native build takes place in this module.
The only update source is this project's GitHub releases.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import sys
import time
import tomllib
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile

REPOSITORY = 'ggssghatsys-beep/capture-one-ai-windows'
RELEASES = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=10'
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024


def installed_project():
    project = Path(sys.prefix).parent
    return project if (project / 'config/bridge-compatibility.json').is_file() else Path(__file__).resolve().parent.parent


def active_project(project=None):
    project = Path(project or installed_project()).resolve()
    pointer = project / 'config/bridge-selected.local.json'
    if not pointer.exists():
        return project
    selected = json.loads(pointer.read_text('utf-8'))
    path = Path(selected['project']).resolve()
    if (path.parent != (project / '.bridge-cache').resolve() or len(path.name) != 64
            or any(c not in '0123456789abcdef' for c in path.name)):
        raise ValueError('Selected bridge project is outside the package cache')
    return verify_package(path)


def read_matrix(project):
    matrix = json.loads((Path(project) / 'config/bridge-compatibility.json').read_text('utf-8-sig'))
    if (matrix.get('schema') != 1 or matrix.get('protocol') != 1
            or matrix.get('color_policy') != 'native-srgb-v1'
            or type(matrix.get('revision')) is not int or matrix['revision'] < 1
            or not isinstance(matrix.get('adapters'), list)):
        raise ValueError('Unsupported bridge compatibility manifest')
    return matrix


def match_adapter(matrix, host):
    rows = [row for row in matrix.get('adapters', []) if all(
        row.get(key) == host.get(key) for key in ('platform', 'architecture', 'capture_one_version'))]
    if len(rows) > 1:
        raise ValueError('Ambiguous adapter entries')
    if not rows:
        return None
    row = rows[0]
    if row.get('runtime_major') != host.get('runtime_major'):
        return None
    return row


def package_version(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:a(\d+))?', value)
    if not match:
        raise ValueError('Unsupported package version')
    major, minor, patch, alpha = match.groups()
    return (int(major), int(minor), int(patch), 1 if alpha is None else 0, int(alpha or 0))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch(url, limit, *, api=False):
    parsed = urlparse(url)
    expected_path = f'/repos/{REPOSITORY}/releases' if api else f'/{REPOSITORY}/releases/download/'
    if (parsed.scheme != 'https' or parsed.netloc != ('api.github.com' if api else 'github.com')
            or not parsed.path.startswith(expected_path)):
        raise ValueError('Update URL is outside the project release repository')
    request = Request(url, headers={'User-Agent': 'CaptureOneAI-Bridge', 'Accept': 'application/json' if api else 'application/octet-stream'})
    with urlopen(request, timeout=5) as response:
        # GitHub release downloads redirect to its asset service.
        final = urlparse(response.url)
        if final.scheme != 'https' or final.hostname not in {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}:
            raise ValueError('Unexpected release download destination')
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Release asset exceeds size limit')
        return data


def unpack_package(data, metadata, cache):
    sha = hashlib.sha256(data).hexdigest()
    if sha != metadata['package']['sha256']:
        raise ValueError('Source package SHA256 mismatch')
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / sha
    if target.exists():
        # Never merge a second archive into an existing package.
        verify_package(target)
        return target
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=cache) as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2048 or sum(item.file_size for item in entries) > MAX_UNPACKED:
                raise ValueError('Release package exceeds extraction limits')
            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate ZIP members')
            components = []
            for item in entries:
                parts = PurePosixPath(item.filename).parts
                raw_parts = item.filename.rstrip('/').split('/')
                if (len(parts) < 2 or any(p in {'', '.', '..'} or ':' in p for p in raw_parts)
                        or '\\' in item.filename or item.filename.startswith('/')
                        or (item.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError('Unsafe ZIP member')
                components.append(parts[0])
            if len(set(components)) != 1:
                raise ValueError('Release must contain exactly one source root')
            prefix = components[0] + '/'
            manifest = json.loads(archive.read(prefix + 'RELEASE-MANIFEST.json'))
            files = manifest['files']
            actual = {name[len(prefix):] for name in names if not name.endswith('/')}
            if actual != set(files) | {'RELEASE-MANIFEST.json'}:
                raise ValueError('Release file list differs from its manifest')
            if (len({name.casefold() for name in actual}) != len(actual)
                    or 'RELEASE-MANIFEST.json' in files):
                raise ValueError('Release contains colliding file names')
            if manifest.get('version') != metadata['core_version']:
                raise ValueError('Core package version differs from update metadata')
            for relative, expected in files.items():
                if (not relative or relative.startswith('/') or '\\' in relative
                        or any(p in {'', '.', '..'} or ':' in p for p in relative.split('/'))):
                    raise ValueError('Invalid release file path')
                content = archive.read(prefix + relative)
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError('Release file checksum mismatch: ' + relative)
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            (root / 'RELEASE-MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')
        if read_matrix(root) != {k: v for k, v in metadata.items() if k not in {'package', 'core_version'}}:
            raise ValueError('Bundled bridge matrix differs from release metadata')
        if tomllib.loads((root / 'pyproject.toml').read_text('utf-8-sig'))['project']['version'] != metadata['core_version']:
            raise ValueError('Bundled Python core version mismatch')
        root.rename(target)
    return target


def verify_package(project):
    project = Path(project)
    manifest = json.loads((project / 'RELEASE-MANIFEST.json').read_text('utf-8'))
    for relative, expected in manifest['files'].items():
        parts = PurePosixPath(relative).parts
        if not parts or any(p in {'.', '..'} or ':' in p for p in parts) or '\\' in relative or relative.startswith('/'):
            raise ValueError('Invalid cached source path')
        path = project / relative
        if any(part.is_symlink() for part in (path, *path.parents)) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Cached source package changed: ' + relative)
    return project


def prepare_update(project, host, *, force=False, current=None):
    """Return a matched immutable source root. Activation is the launcher's job."""
    project = Path(project).resolve()
    current = Path(current or project).resolve()
    local = read_matrix(current)
    local_version = tomllib.loads((current / 'pyproject.toml').read_text('utf-8-sig'))['project']['version']
    local_match = match_adapter(local, host)
    check = project / '.bridge-cache/last-check.json'
    if not force and local_match and check.exists():
        previous = json.loads(check.read_text('utf-8'))
        if previous.get('host') == host and 0 <= time.time() - previous.get('checked_at', 0) < 86400:
            return {'status': 'cached', 'project': str(current), 'adapter': local_match['id']}
    best = None
    releases = json.loads(_fetch(RELEASES, 1024 * 1024, api=True))
    # A release only participates when it publishes the new compatibility asset.
    # Old releases cannot accidentally replace the native color fix.
    for release in [r for r in releases if not r.get('draft')][:5]:
        assets = release.get('assets', [])
        asset = next((a for a in assets if a['name'] == 'BRIDGE-COMPATIBILITY.json'), None)
        if not asset:
            continue
        metadata = json.loads(_fetch(asset['browser_download_url'], 256 * 1024))
        # Validate schema using the same predicates as the bundled manifest.
        if (metadata.get('schema') != 1 or metadata.get('protocol') != 1
                or metadata.get('color_policy') != 'native-srgb-v1'
                or type(metadata.get('revision')) is not int or metadata['revision'] < 1):
            continue
        adapter = match_adapter(metadata, host)
        if not adapter:
            continue
        version = package_version(metadata['core_version'])
        score = (version, metadata['revision'])
        if score <= (package_version(local_version), local['revision']):
            continue
        package = metadata.get('package', {})
        if not re.fullmatch('[0-9a-f]{64}', package.get('sha256', '')):
            raise ValueError('Invalid package checksum in release metadata')
        archive = next((a for a in assets if a['name'] == package.get('name')), None)
        if archive and (best is None or score > best[0]):
            best = (score, metadata, archive, adapter)
    if best is None:
        if not local_match:
            raise ValueError('No published verified adapter for this C1 version and architecture')
        atomic_json(check, {'host': host, 'checked_at': time.time()})
        return {'status': 'current', 'project': str(current), 'adapter': local_match['id']}
    _, metadata, archive, adapter = best
    destination = unpack_package(_fetch(archive['browser_download_url'], MAX_ARCHIVE), metadata, project / '.bridge-cache')
    return {'status': 'prepared', 'project': str(destination), 'adapter': adapter['id'],
            'core_version': metadata['core_version'], 'revision': metadata['revision']}
