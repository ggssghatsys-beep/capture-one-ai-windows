"""Create a small, allowlisted release without touching the active C1 bridge."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_FILES = [
    '.gitignore', '.gitattributes', 'README.md', 'AGENTS.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'third_party/openrv-web-MIT.txt', 'docs/SCOPES_REUSE.md',
    'pyproject.toml', 'requirements-runtime.txt', 'Install.ps1', 'Start-CaptureOne.ps1', 'Run.ps1',
    'config/native-profile.json', 'config/mcp-server.example.json',
    'docs/INSTALL.md', 'docs/AI_CLIENTS.md', 'docs/API.md', 'docs/COLOR_API.md',
    'docs/NAVIGATION_PREVIEW.md', 'docs/SUPPORT.md', 'docs/ARCHITECTURE.md',
    'docs/DEVELOPMENT.md', 'docs/MAC_PORT.md', 'docs/MAC_STATUS.md', 'docs/RELEASE_CHECKS.md',
    'c1ai/native/uia.ps1',
    'internal/StartupHook.cs', 'internal/NativeEngine.cs', 'internal/start.ps1', 'internal/build.ps1',
    'scripts/check_connection.py', 'scripts/configure.py', 'scripts/package_release.py',
    'scripts/check_release.py', 'scripts/check_macos.py', 'scripts/configure_macos_pilot.py',
    'Scopes.ps1', 'Setup-Shortcuts.ps1', 'scripts/probe_image_source.py',
    'scripts/check_scopes_live.py', 'docs/SCOPES.md', 'docs/SCOPES_VALIDATION.md', 'docs/AI_INSTALL_PROMPT.md',
    'AI.ps1', 'docs/AI_WINDOW.md', 'docs/INSTALL_MAC.md', 'docs/RELEASE_NOTES.md',
    '.github/workflows/publish-source.yml',
    'scripts/install_macos.py', 'scripts/create_macos_scopes_shortcut.py', 'scripts/build_macos_bridge.py',
    'internal/macos/NativeBridge.m', 'internal/macos/FoundationTypes.h',
    'internal/macos/GradientActions.inc', 'internal/macos/MaskCommands.inc',
    'internal/macos/ColorActions.inc', 'internal/macos/LayerActions.inc', 'internal/macos/ViewerCache.inc',
]


def public_files():
    paths = {ROOT / p for p in PUBLIC_FILES}
    for directory in ('c1ai', 'tests'):
        paths.update(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.ps1', '.js', '.json', '.icc'))
    for path in sorted(paths):
        if not path.is_file():
            raise FileNotFoundError(path.relative_to(ROOT))
    return sorted(paths)


def privacy_check(paths):
    # Scan actual content; schema fields called token are not credentials.
    sensitive = re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,}'
                           r'|sk-[A-Za-z0-9_-]{35,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    for path in paths:
        if path.suffix=='.icc':
            if path.read_bytes()[36:40]!=b'acsp':raise ValueError('Invalid ICC profile')
            continue
        content = path.read_text(encoding='utf-8-sig')
        if sensitive.search(content):
            raise ValueError('Potential credential in ' + str(path.relative_to(ROOT)))
        if re.search(r'/(?:Users)/[^/\s\"\']+/', content):
            raise ValueError('Personal home path in ' + str(path.relative_to(ROOT)))


def main():
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))['project']['version']
    name = 'CaptureOne-AI-' + version + '-Source'
    paths = public_files()
    privacy_check(paths)
    binaries = []
    for source in ('StartupHook', 'NativeEngine'):
        source_hash = hashlib.sha256((ROOT / 'internal' / (source + '.cs')).read_bytes()).hexdigest()
        binary = ROOT / 'internal/bin' / source_hash[:16] / ('C1AI.' + source + '.' + source_hash[:12] + '.dll')
        if binary.is_file():
            paths.append(binary)
            binaries.append(binary.relative_to(ROOT).as_posix())
    manifest = {'version': version, 'capture_one_version': '16.8.6.3784',
                'platforms': {'windows': {'capture_one_version': '16.8.6.3784'},
                              'macos': {'capture_one_version': '16.8.6.25', 'architecture': 'arm64', 'status': 'experimental'}},
                'macos_native_bridge': 'Build locally; no vendor or local native binary included',
                'compiled_bridge_included': len(binaries) == 2,
                'own_bridge_assemblies': binaries,
                'files': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(paths)}}
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    destination = output / (name + '.zip')
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(paths):
            archive.write(path, name + '/' + path.relative_to(ROOT).as_posix())
        archive.writestr(name + '/RELEASE-MANIFEST.json', json.dumps(manifest, indent=2) + '\n')
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(digest + '  ' + destination.name + '\n', encoding='utf-8')
    print(json.dumps({'path': str(destination), 'bytes': destination.stat().st_size,
                      'files': len(paths) + 1, 'sha256': digest,
                      'compiled_bridge_included': manifest['compiled_bridge_included']}))


if __name__ == '__main__':
    main()
