"""Configure automatic or pinned Session binding without connecting to C1."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', type=Path)
    parser.add_argument('--pinned', action='store_true', help='Keep the specified Session fixed instead of following C1')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows is required')
    project = Path(__file__).resolve().parent.parent
    root = Path(os.environ['LOCALAPPDATA']) / 'CaptureOneAI'
    path = root / 'config.json'
    old = json.loads(path.read_text('utf-8-sig')) if path.exists() else {}
    session = args.session or (Path(old['session_path']) if old.get('session_path') else None)
    if args.pinned and session is None:
        parser.error('--pinned requires --session with an existing .cosessiondb file')
    if session is not None and (session.suffix.lower() != '.cosessiondb' or not session.is_file()):
        # An old machine path is only a hint in auto mode; discard it on migration.
        if args.session is not None or args.pinned:
            parser.error('Pass an existing .cosessiondb file; Catalog is not qualified')
        session = None
    profile = json.loads((project / 'config/native-profile.json').read_text('utf-8'))
    desired = {**old, **profile, 'session_path': str(session.resolve()) if session else None,
               'session_binding': 'pinned' if args.pinned else 'auto'}
    # Do not rewrite an existing equivalent native configuration just to change its evidence label.
    keys = ('session_path', 'session_binding', 'qualified_version', 'native_qualified_fields', 'qualified_modules',
            'experimental_fields', 'experimental_modules')
    changed = any(old.get(k) != desired[k] for k in keys)
    if changed and not args.dry_run:
        from c1ai.owner import connect_pipe, endpoint, win32
        try:
            handle = connect_pipe(endpoint())
        except OSError as error:
            if getattr(error, 'winerror', None) not in (2, 231):
                raise
            if getattr(error, 'winerror', None) == 231:
                parser.error('Owner is busy. Complete pending edits before changing configuration.')
        else:
            win32().CloseHandle(handle)
            parser.error('Owner is already running. Configuration was not changed. See docs/INSTALL.md, configuration changes.')
        root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = root / ('config.backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
            backup.write_bytes(path.read_bytes())
        temporary = root / 'config.install.tmp'
        temporary.write_text(json.dumps(desired, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    print(json.dumps({'status': 'dry_run' if args.dry_run else 'configured' if changed else 'unchanged',
                      'session_path': desired['session_path'], 'session_binding':desired['session_binding'],
                      'qualified_version': desired['qualified_version']},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
