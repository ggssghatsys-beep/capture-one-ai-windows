"""Optional human viewer; analysis and editing never require an open window."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import subprocess
import sys

VIEWS={'waveform':'亮度','parade':'RGB','histogram':'直方图','vectorscope':'矢量图'}


def open_window(view=None):
    if view is not None and view not in VIEWS:
        raise ValueError('Unknown window view')
    if sys.platform!='win32':
        raise ValueError('Window launch integration has only been qualified on Windows')
    if importlib.util.find_spec('PySide6') is None or importlib.util.find_spec('av') is None:
        raise ValueError('Install the scopes extra first: python -m pip install ".[scopes]"')
    python=Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():raise ValueError('Python window launcher not found')
    args=[str(python),'-m','c1ai.scopes_window']
    if view:args+=['--mode',view]
    subprocess.Popen(args,cwd=Path(__file__).resolve().parent.parent,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)
    # Asynchronous launch is not evidence of a current rendered frame.
    return {'status':'ok','window':'open_requested','view':view or 'last_used',
            'analysis_requested':False,'note':'The existing window is reused; request image statistics separately.'}
