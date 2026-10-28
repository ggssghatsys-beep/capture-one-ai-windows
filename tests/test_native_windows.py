"""Native window metadata doubles only; no desktop/application calls."""
from types import SimpleNamespace

import pytest

from c1ai import native_windows, windows_backend


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.fixture
def desktop(monkeypatch):
    state = SimpleNamespace(rows={
        10: {'pid': 123, 'title': 'Session', 'owner': 0, 'visible': True, 'enabled': False},
        20: {'pid': 123, 'title': 'Preferences', 'owner': 10, 'visible': True, 'enabled': False},
        30: {'pid': 123, 'title': 'Capture One', 'owner': 10, 'visible': True, 'enabled': True},
        40: {'pid': 999, 'title': 'Other app', 'owner': 0, 'visible': True, 'enabled': True},
    }, main_pid=123, start='456', enum_error=False)

    def process(hwnd, output):
        output._obj.value = state.main_pid if hwnd == 10 else state.rows[hwnd]['pid']
        return 1

    def caption(hwnd, output, limit):
        value = state.rows[hwnd]['title'][:limit - 1]
        output.value = value
        return len(value)

    def kind(hwnd, output, limit):
        output.value = 'WindowsForms10.Window.8.app'
        return len(output.value)

    def enumerate_windows(callback, unused):
        if state.enum_error:
            return False
        for hwnd in state.rows:
            if not callback(hwnd, 0):
                return False
        return True

    user = SimpleNamespace(EnumWindows=Function(enumerate_windows), GetWindowThreadProcessId=Function(process),
        GetWindowTextW=Function(caption), GetClassNameW=Function(kind),
        GetWindow=Function(lambda hwnd, flag: state.rows[hwnd]['owner']), GetParent=Function(lambda hwnd: 0),
        IsWindow=Function(lambda hwnd: hwnd in state.rows), IsWindowEnabled=Function(lambda hwnd: state.rows[hwnd]['enabled']),
        IsWindowVisible=Function(lambda hwnd: state.rows[hwnd]['visible']), IsIconic=Function(lambda hwnd: False))
    monkeypatch.setattr(native_windows.ctypes, 'WinDLL', lambda *args, **kwargs: user)
    monkeypatch.setattr(windows_backend, 'process_started', lambda pid: state.start)
    return state


@pytest.fixture
def main_discovery(desktop, monkeypatch):
    desktop.identity = {'pid': 123, 'started_at': '456', 'executable': 'C:/Program Files/Capture One/CaptureOne.exe'}
    desktop.identity_reads = 0
    desktop.change_process = False

    def identity(pid):
        desktop.identity_reads += 1
        result = dict(desktop.identity, pid=pid)
        if desktop.change_process and desktop.identity_reads > 1:
            result['started_at'] = 'new'
        return result

    monkeypatch.setattr(native_windows, '_process_identity', identity)
    monkeypatch.setattr(native_windows, '_executable_version', lambda path: {
        'version': '16.8.6.3784', 'version_source': 'FileVersionParts', 'file_version': '16.8.6.3784'})
    return desktop


def test_native_inventory_finds_owned_modal_without_uia(desktop):
    result = native_windows.native_window_inventory(123, 10, expected_started_at='456')
    assert [row['hwnd'] for row in result['windows']] == [10, 20, 30]
    assert [row['hwnd'] for row in result['modal_candidates']] == [20, 30]
    assert result['blocking_modal_possible'] is True
    assert result['enumeration_complete'] is True
    assert result['uia_queried'] is False and result['mutations_sent'] is False
    assert result['automatic_close_permitted'] is False


def test_nested_owner_chain_and_enabled_main_do_not_claim_modal(desktop):
    desktop.rows[30]['owner'] = 20
    desktop.rows[10]['enabled'] = True
    result = native_windows.native_window_inventory(123, 10)
    assert result['modal_candidates'][-1]['owner_chain'] == [20, 10]
    assert result['blocking_modal_possible'] is False
    assert all(row['uia_modal_verified'] is False for row in result['modal_candidates'])


def test_bound_limit_reports_incomplete_without_absence_claim(desktop):
    result = native_windows.native_window_inventory(123, 10, limit=1)
    assert result['truncated'] is True and result['enumeration_complete'] is False
    assert result['modal_candidates'] == [] and result['blocking_modal_possible'] is None


@pytest.mark.parametrize('change', ['pid', 'start', 'enumeration'])
def test_changed_identity_and_failed_enumeration_refuse(desktop, change):
    if change == 'pid':
        desktop.main_pid = 999
    elif change == 'start':
        desktop.start = 'other'
    else:
        desktop.enum_error = True
    with pytest.raises(windows_backend.NativeError):
        native_windows.native_window_inventory(123, 10, expected_started_at='456')


def test_hidden_and_external_windows_are_not_modal_candidates(desktop):
    desktop.rows[20]['visible'] = False
    desktop.rows[30]['owner'] = 40
    result = native_windows.native_window_inventory(123, 10)
    assert result['modal_candidates'] == []
    assert result['external_helpers_included'] is False
    assert result['blocking_modal_possible'] is None


def test_find_main_works_without_selected_image_or_uia(main_discovery):
    result = native_windows.find_native_main('Session', main_discovery.identity['executable'])
    assert result['hwnd'] == 10 and result['pid'] == 123 and result['started_at'] == '456'
    assert result['version'] == '16.8.6.3784' and result['enabled'] is False
    assert result['uia_queried'] is False and result['document_path_verified'] is False
    assert result['selected_target_verified'] is False


def test_same_title_owned_modal_is_not_mistaken_for_main(main_discovery):
    main_discovery.rows[20]['title'] = 'Session'
    result = native_windows.find_native_main('Session')
    assert result['hwnd'] == 10


@pytest.mark.parametrize('change', ['duplicate', 'wrong_executable', 'wrong_name', 'hidden', 'title', 'process_change', 'limited'])
def test_native_main_requires_complete_unique_verified_window(main_discovery, change):
    expected = main_discovery.identity['executable']
    limit = 2048
    title = 'Session'
    if change == 'duplicate':
        main_discovery.rows[50] = dict(main_discovery.rows[10])
    elif change == 'wrong_executable':
        expected = 'C:/Other/CaptureOne.exe'
    elif change == 'wrong_name':
        main_discovery.identity['executable'] = 'C:/NotCapture.exe'
        expected = None
    elif change == 'hidden':
        main_discovery.rows[10]['visible'] = False
    elif change == 'title':
        title = 'Different Session'
    elif change == 'process_change':
        main_discovery.change_process = True
    else:
        limit = 1
    with pytest.raises(windows_backend.NativeError):
        native_windows.find_native_main(title, expected, limit=limit)


def test_fixed_file_version_is_read_without_loading_capture_one(monkeypatch):
    import ctypes
    from ctypes import wintypes as W
    fixed = (W.DWORD * 13)()
    fixed[0] = 0xFEEF04BD
    fixed[2] = (16 << 16) | 8
    fixed[3] = (6 << 16) | 3784

    def query(data, path, pointer, length):
        assert path == '\\'
        pointer._obj.value = ctypes.addressof(fixed)
        length._obj.value = ctypes.sizeof(fixed)
        return True

    version = SimpleNamespace(GetFileVersionInfoSizeW=Function(lambda *args: 256),
        GetFileVersionInfoW=Function(lambda *args: True), VerQueryValueW=Function(query))
    monkeypatch.setattr(native_windows.ctypes, 'WinDLL', lambda name, **kwargs: version)
    result = native_windows._executable_version('C:/CaptureOne.exe')
    assert result['version'] == '16.8.6.3784' and result['version_source'] == 'FileVersionParts'
    fixed[0] = 0
    with pytest.raises(windows_backend.NativeError, match='signature'):
        native_windows._executable_version('C:/CaptureOne.exe')
