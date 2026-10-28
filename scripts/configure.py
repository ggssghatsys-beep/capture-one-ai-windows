"""Configure one Session without starting C1 or replacing private runtime state."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows is required')
    project = Path(__file__).resolve().parent.parent
    root = Path(os.environ['LOCALAPPDATA']) / 'CaptureOneAI'
    path = root / 'config.json'
    old = json.loads(path.read_text('utf-8-sig')) if path.exists() else {}
    session = args.session or (Path(old['session_path']) if old.get('session_path') else None)
    if session is None or session.suffix.lower() != '.cosessiondb' or not session.is_file():
        parser.error('Pass --session with an existing .cosessiondb file; Catalog is not qualified.')
    profile = json.loads((project / 'config/native-profile.json').read_text('utf-8'))
    desired = {**old, **profile, 'session_path': str(session.resolve())}
    # Do not rewrite an existing equivalent native configuration just to change its evidence label.
    keys = ('session_path', 'qualified_version', 'native_qualified_fields', 'qualified_modules',
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
                      'session_path': desired['session_path'], 'qualified_version': desired['qualified_version']},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
