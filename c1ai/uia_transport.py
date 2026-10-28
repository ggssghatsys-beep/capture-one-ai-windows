"""One hidden STA worker per bridge process, serialized NDJSON, no replay.

Only the bundled driver runs. EOF/timeout kills this owned child; the caller
decides whether a failed mutation needs reconciliation. No network listener.
"""
import atexit
from collections import deque
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


class UiaWorker:
    def __init__(self, host_path):
        self.host_path = Path(host_path)
        self.process = None
        self.responses = None
        self.stderr = deque(maxlen=32)
        self.lock = threading.Lock()
        self.starts = 0
        atexit.register(self.close)

    def _start(self, command):
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if sys.platform == 'win32': kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(command, **kwargs)
        self.starts += 1
        self.responses = queue.Queue()
        self.stderr.clear()
        process, responses, errors = self.process, self.responses, self.stderr
        def receive():
            try:
                while True:
                    line = process.stdout.readline(16 * 1024 * 1024 + 1)
                    if not line: break
                    responses.put(line)
            finally: responses.put(None)
        def receive_errors():
            for line in iter(process.stderr.readline, b''): errors.append(line[-2048:])
        threading.Thread(target=receive, daemon=True).start()
        threading.Thread(target=receive_errors, daemon=True).start()

    def request(self, command, payload, timeout):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self._start(command)
            try:
                self.process.stdin.write(payload + b'\n')
                self.process.stdin.flush()
                line = self.responses.get(timeout=timeout)
                if line is None or len(line) > 16 * 1024 * 1024 or not line.endswith(b'\n'):
                    raise OSError('UIA worker exited or returned an invalid frame')
                return subprocess.CompletedProcess(command, 0, line, b''.join(self.stderr))
            except queue.Empty as exc:
                self.close()
                raise subprocess.TimeoutExpired(command, timeout) from exc
            except Exception:
                self.close()
                raise

    def close(self):
        process, self.process = self.process, None
        if process is None: return
        if process.poll() is None:
            try: process.stdin.close()
            except OSError: pass
            try: process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            try: stream.close()
            except (OSError, ValueError): pass
