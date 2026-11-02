"""Source package selection for the Windows launchers; never opens a C1 pipe."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from c1ai.bridge_packages import active_project, atomic_json, match_adapter, prepare_update, read_matrix, verify_package


def require_idle():
    if os.name != 'nt':
        raise RuntimeError('Windows launcher required')
    probe = subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
        "if(@(Get-Process -Name CaptureOne -ErrorAction SilentlyContinue).Count){exit 2}"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if probe.returncode:
        raise RuntimeError('C1 is still running; no package or bridge was changed')
    from c1ai.owner import connect_pipe, endpoint, win32
    try:
        handle = connect_pipe(endpoint())
    except OSError as error:
        if getattr(error, 'winerror', None) != 2:
            raise RuntimeError('Owner may be running or busy; finish pending operations and disconnect clients first') from error
    else:
        win32().CloseHandle(handle)
        raise RuntimeError('Owner is still running; finish pending operations and close it normally before changing versions')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--action', choices=('active', 'idle', 'prepare', 'activate'), required=True)
    parser.add_argument('--project', type=Path, default=PROJECT)
    parser.add_argument('--host-json')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    project = args.project.resolve()
    if args.action == 'active':
        result = {'project': str(active_project(project))}
    else:
        require_idle()
        if args.action == 'idle':
            result = {'status': 'idle'}
        else:
            host = json.loads(args.host_json or '{}')
            if args.action == 'prepare':
                result = prepare_update(project, host, force=args.force, current=active_project(project))
            else:
                candidate = args.candidate.resolve()
                if candidate.parent != (project / '.bridge-cache').resolve():
                    raise ValueError('Candidate is outside the local package cache')
                verify_package(candidate)
                adapter = match_adapter(read_matrix(candidate), host)
                if adapter is None or not (candidate / '.venv/Scripts/python.exe').is_file():
                    raise ValueError('Candidate needs a matching adapter and completed local environment')
                require_idle()
                atomic_json(project / 'config/bridge-selected.local.json', {'project': str(candidate), 'adapter': adapter['id']})
                atomic_json(project / '.bridge-cache/last-check.json', {'host': host, 'checked_at': time.time()})
                result = {'status': 'activated', 'project': str(candidate)}
    # Keep launcher output independent of Windows console code pages.
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
