"""Install this source checkout; never starts C1 or changes its permissions."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-scopes', action='store_true')
    parser.add_argument('--with-ai', action='store_true')
    parser.add_argument('--desktop-scopes', action='store_true', help='Also install scopes and create its desktop entry')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or sys.version_info < (3, 11):
        parser.error('Use native Apple Silicon Python 3.11+ (3.12 tested) on macOS')
    repo = Path(__file__).resolve().parents[1]
    environment = repo / '.venv'
    python = environment / 'bin/python'
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    # Existing environments must meet the same requirement as the installer.
    subprocess.run([str(python), '-c', 'import sys,platform; assert sys.version_info >= (3,11) and platform.machine() == "arm64"'], check=True)
    extras = ['mac']
    if args.with_scopes or args.desktop_scopes:
        extras.append('scopes')
    if args.with_ai:
        extras.append('ai')
    subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(repo) + '[' + ','.join(extras) + ']'], check=True)
    config = repo / 'config/mcp-server.local.json'
    config.write_text(json.dumps({'mcpServers': {'capture-one-ai': {'command': str(python),
        'args': ['-m', 'c1ai', 'mcp'], 'env': {'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}}}}, indent=2) + '\n')
    config.chmod(0o600)
    if args.desktop_scopes:
        subprocess.run([str(python), str(repo / 'scripts/create_macos_scopes_shortcut.py')], check=True)
    print(json.dumps({'installed': True, 'mcp_config': str(config), 'desktop_scopes': args.desktop_scopes,
        'native_bridge_changed': False, 'note': 'C1 and existing runtime configuration were not changed. See docs/INSTALL_MAC.md for native bridge setup.'}))


if __name__ == '__main__':
    main()
