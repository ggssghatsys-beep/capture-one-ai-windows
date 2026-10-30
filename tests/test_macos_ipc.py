"""Real local sockets only; these checks do not certify Capture One editing."""
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from c1ai.journal import scope


def test_mac_scope_keeps_case_and_windows_scope_keeps_legacy_behavior():
    mac = {"instance": {"platform": "macos"}, "document": {"path": "/Photos/Session"},
           "image": {"path": "/Photos/A.RAW"}}
    other = {**mac, "image": {"path": "/Photos/a.RAW"}}
    assert scope(mac) != scope(other)
    windows = {"document": {"path": "C:/Photos/Session"}, "image": {"path": "C:/Photos/A.RAW"}}
    assert scope(windows) == ("c:\\photos\\session", "c:\\photos\\a.raw")


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin Unix socket credentials")
def test_owner_timeout_does_not_cancel_or_replay_and_receipts_remain_queryable():
    from c1ai.client import RequestUncertain, send_request
    from c1ai.macos_ipc import UnixSocketServer
    from c1ai.owner import OwnerAlreadyRunning

    class Dispatcher:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()
            self.count = 0

        def dispatch(self, request):
            self.count += 1
            self.started.set()
            assert self.release.wait(3)
            self.finished.set()
            return {"status": "completed"}

        def peek(self, request):
            return {"status": "completed" if self.finished.is_set() else "dispatching", "count": self.count}

        def close(self):
            pass

    core = Dispatcher()
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="c1ai-", dir="/tmp") as directory:
        root = Path(directory)
        server = UnixSocketServer(lambda: core, root)
        worker = threading.Thread(target=server.serve, args=(stop,))
        worker.start()
        try:
            assert server.ready.wait(2), server.failure
            assert (root / "owner.sock").stat().st_mode & 0o777 == 0o600
            with pytest.raises(OwnerAlreadyRunning):
                UnixSocketServer(lambda: pytest.fail("Second owner must not construct Core"), root).serve()
            with pytest.raises(RequestUncertain):
                send_request({"command": "read", "request_id": "slow"}, timeout=0.1,
                             autostart=False, address=str(root / "owner.sock"))
            assert core.started.is_set()
            receipt = send_request({"command": "operations", "action": "get", "request_id": "slow"},
                                   autostart=False, address=str(root / "owner.sock"))
            assert receipt == {"status": "dispatching", "count": 1}
            core.release.set()
            assert core.finished.wait(2)
            receipt = send_request({"command": "operations", "action": "get", "request_id": "slow"},
                                   autostart=False, address=str(root / "owner.sock"))
            assert receipt == {"status": "completed", "count": 1}
        finally:
            core.release.set()
            stop.set()
            worker.join(4)
        assert not worker.is_alive()
        assert server.failure is None
        assert not (root / "owner.sock").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS transport")
def test_owner_rejects_shared_state_directory(tmp_path):
    from c1ai.macos_ipc import secure_directory
    from c1ai.owner import PipeError

    tmp_path.chmod(0o755)
    with pytest.raises(PipeError):
        secure_directory(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin Unix socket credentials")
def test_display_analysis_bypasses_busy_edit_queue_without_using_journal():
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from PIL import Image
    from c1ai.client import send_request
    from c1ai.core import Core
    from c1ai.macos_ipc import UnixSocketServer

    class Backend:
        analysis_display_concurrent = True

    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    with tempfile.TemporaryDirectory(prefix="c1ai-", dir="/tmp") as directory:
        root=Path(directory);image=root/'sample.png';Image.new('RGB',(16,16),'gray').save(image)
        core=Core(Backend(),root)
        journal=core.journal
        class JournalGuard:
            def __getattr__(self,name):
                assert not threading.current_thread().name.startswith('c1ai-display'), 'Display touched the edit journal'
                return getattr(journal,name)
        core.journal=JournalGuard()
        def slow_write(request):
            entered.set();assert release.wait(5)
            return {'status':'completed'}
        core._write=slow_write
        server=UnixSocketServer(lambda:core,root)
        worker=threading.Thread(target=server.serve,args=(stop,));worker.start()
        clients=ThreadPoolExecutor(max_workers=2)
        def call(request):
            return send_request(request,autostart=False,address=str(root/'owner.sock'),timeout=5)
        try:
            assert server.ready.wait(2),server.failure
            editing=clients.submit(call,{'command':'images','action':'select','request_id':'blocked-select'})
            assert entered.wait(2)
            request={'command':'analyze_image','source':'file','path':str(image),
                     'precision':'display','include_image':'histogram','include_statistics':False}
            shown=call(request)
            assert shown['freshness']['state']=='ready' and not editing.done()
            assert Path(shown['artifact']['path']).parent==root/'analysis'/'display'
            assert core._analysis is None
            assert not core.is_display_request({'command':'adjust','precision':'display'})
            assert not core.is_display_request({**request,'action':'open_window'})
            exact=clients.submit(call,{**request,'precision':'exact'})
            with pytest.raises(TimeoutError):exact.result(timeout=.05)
            release.set()
            assert editing.result(timeout=2)['status']=='completed'
            full=exact.result(timeout=2)
            assert full['analysis']['precision']=='exact'
            assert full['analysis']['analysis_id']!=shown['analysis']['analysis_id']
            assert Path(full['artifact']['path']).parent==root/'analysis'
            saved=call({'command':'analyze_image','action':'get','precision':'display',
                        'analysis_id':shown['analysis']['analysis_id'],'include_statistics':False})
            assert saved['analysis']['pixel_sha256']==shown['analysis']['pixel_sha256']
        finally:
            release.set();clients.shutdown(wait=True);stop.set();worker.join(5)
        assert not worker.is_alive() and server.failure is None
