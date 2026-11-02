"""Optional human viewer; analysis and editing never require an open window."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import subprocess
import shutil
import sys

VIEWS={'waveform':'亮度','parade':'RGB','histogram':'直方图','vectorscope':'矢量图'}


def forward_selected_window(argv):
    """Cover python -m and the console entry, in addition to Scopes.ps1."""
    if sys.platform != 'win32':return False
    from .bridge_packages import active_project, installed_project
    current=installed_project()
    project=active_project(current)
    if project.resolve()==current.resolve():return False
    python=project/'.venv/Scripts/pythonw.exe'
    if not python.is_file():raise ValueError('Matched scopes environment is not installed')
    subprocess.Popen([str(python),'-m','c1ai.scopes_window',*argv],cwd=project,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return True


def main():
    if forward_selected_window(sys.argv[1:]):return 0
    from .scopes_window import main as window_main
    return window_main()


def open_window(view=None):
    if view is not None and view not in VIEWS:
        raise ValueError('Unknown window view')
    if sys.platform not in {'win32','darwin'}:
        raise ValueError('Window launch is available on Windows and macOS')
    if sys.platform == 'win32':
        # API/open_window used to bypass Scopes.ps1 and keep the caller's old
        # Python environment after a bridge-package switch.
        from .bridge_packages import active_project
        project=active_project()
        launcher=project/'Scopes.ps1'
        if not launcher.is_file():raise ValueError('Matched package has no scopes launcher')
        args=[shutil.which('pwsh') or 'pwsh','-NoLogo','-NoProfile','-NonInteractive',
              '-WindowStyle','Hidden','-File',str(launcher)]
        if view:args+=['-Mode',view]
        subprocess.Popen(args,cwd=project,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,close_fds=True,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        return {'status':'ok','window':'open_requested','view':view or 'last_used',
                'analysis_requested':False,'note':'The matched scopes launcher shares the native bridge and current Session binding.'}
    if importlib.util.find_spec('PySide6') is None or importlib.util.find_spec('av') is None:
        raise ValueError('Install the scopes extra first: python -m pip install ".[scopes]"')
    python=Path(sys.executable).with_name('pythonw.exe') if sys.platform=='win32' else Path(sys.executable)
    if not python.is_file():raise ValueError('Python window launcher not found')
    args=[str(python),'-m','c1ai.scopes_window']
    if view:args+=['--mode',view]
    options=({'creationflags':subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
             if sys.platform=='win32' else {'start_new_session':True})
    subprocess.Popen(args,cwd=Path(__file__).resolve().parent.parent,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     close_fds=True, **options)
    # Asynchronous launch is not evidence of a current rendered frame.
    return {'status':'ok','window':'open_requested','view':view or 'last_used',
            'analysis_requested':False,'note':'The existing window is reused; request image statistics separately.'}
