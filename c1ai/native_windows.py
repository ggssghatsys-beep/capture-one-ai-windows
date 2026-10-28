"""Bounded, read-only Win32 window evidence; never traverses a UIA provider."""
import ctypes
from ctypes import wintypes as W
from pathlib import Path


def _process_identity(pid):
    # Reuse the already tested, read-only QueryFullProcessImageName/GetProcessTimes
    # implementation; importing the module does not start its transport host.
    from .tangent_transport import process_identity
    return process_identity(pid)


def _executable_version(executable):
    """Numeric executable file resources, without loading the application DLL."""
    from .windows_backend import NativeError
    version = ctypes.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, ctypes.POINTER(W.DWORD)]
    version.GetFileVersionInfoSizeW.restype = W.DWORD
    version.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = W.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.UINT)]
    version.VerQueryValueW.restype = W.BOOL
    unused = W.DWORD()
    size = version.GetFileVersionInfoSizeW(executable, ctypes.byref(unused))
    if not 0 < size <= 1024 * 1024:
        raise NativeError('VERSION_UNAVAILABLE', 'Executable version resource is absent or unexpectedly large')
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(executable, 0, size, data):
        raise NativeError('VERSION_UNAVAILABLE', 'Executable version resource cannot be read')
    pointer, length = ctypes.c_void_p(), W.UINT()
    if not version.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        raise NativeError('VERSION_UNAVAILABLE', 'Fixed file version information is absent')
    if not pointer.value or length.value < 13 * ctypes.sizeof(W.DWORD):
        raise NativeError('VERSION_UNAVAILABLE', 'Fixed file version information is incomplete')
    fixed = ctypes.cast(pointer, ctypes.POINTER(W.DWORD * 13)).contents
    if fixed[0] != 0xFEEF04BD:
        raise NativeError('VERSION_UNAVAILABLE', 'Executable file version signature is invalid')
    parts = (fixed[2] >> 16, fixed[2] & 65535, fixed[3] >> 16, fixed[3] & 65535)
    if not any(parts):
        raise NativeError('VERSION_UNAVAILABLE', 'Executable has no nonzero numeric file version')
    text = '.'.join(str(part) for part in parts)
    return {'version': text, 'file_version': text, 'version_source': 'FileVersionParts'}


def find_native_main(configured_session_title, expected_executable=None, *, limit=2048):
    """Find one exact native C1 main window without a selected image or UIA.

    This identifies a window/process, not an open Session path or photo UUID.
    The owner must still establish those identities before enabling any write.
    """
    from .windows_backend import NativeError
    if (not isinstance(configured_session_title, str) or not configured_session_title
            or len(configured_session_title) >= 512 or type(limit) is not int or not 1 <= limit <= 4096):
        raise NativeError('INVALID_WINDOW_REQUEST', 'A bounded exact Session title and enumeration limit are required')
    expected = str(Path(expected_executable).resolve()) if expected_executable is not None else None
    u = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    u.EnumWindows.argtypes = [callback_type, W.LPARAM]
    u.EnumWindows.restype = W.BOOL
    u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    u.GetWindowThreadProcessId.restype = W.DWORD
    u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetWindow.argtypes = [W.HWND, W.UINT]
    u.GetWindow.restype = W.HWND
    u.GetParent.argtypes = [W.HWND]
    u.GetParent.restype = W.HWND
    for name in ('IsWindow', 'IsWindowVisible', 'IsWindowEnabled', 'IsIconic'):
        getattr(u, name).argtypes = [W.HWND]
        getattr(u, name).restype = W.BOOL
    matches, failures = [], []
    visited, truncated = 0, False

    def read_window(hwnd):
        if (not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd)
                or u.GetWindow(hwnd, 4) or u.GetParent(hwnd)):
            return None
        title = ctypes.create_unicode_buffer(512)
        size = u.GetWindowTextW(hwnd, title, len(title))
        if size >= len(title) - 1 or title.value != configured_session_title:
            return None
        pid = W.DWORD()
        if not u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value <= 0:
            raise NativeError('WINDOW_UNAVAILABLE', 'Matching main window process could not be read')
        return {'pid': pid.value, 'hwnd': int(hwnd), 'title': title.value, 'visible': True,
                'minimized': bool(u.IsIconic(hwnd)), 'enabled': bool(u.IsWindowEnabled(hwnd))}

    def collect(hwnd, unused):
        nonlocal visited, truncated
        visited += 1
        if visited > limit:
            truncated = True
            return False
        try:
            window = read_window(hwnd)
            if window is None:
                return True
            identity = _process_identity(window['pid'])
            executable = str(Path(identity['executable']).resolve())
            if Path(executable).name.casefold() not in {'captureone.exe', 'capture one.exe'}:
                return True
            if expected is not None and executable != expected:
                return True
            matches.append((window, identity))
        except Exception as error:
            failures.append({'hwnd': int(hwnd), 'code': getattr(error, 'code', 'WINDOW_READ_FAILED'), 'message': str(error)})
        return True

    callback = callback_type(collect)
    ctypes.set_last_error(0)
    succeeded = bool(u.EnumWindows(callback, 0))
    if not succeeded or truncated or failures:
        raise NativeError('MAIN_WINDOW_DISCOVERY_INCOMPLETE', 'Native main-window discovery was not complete',
                          {'truncated': truncated, 'read_errors': failures})
    if len(matches) != 1:
        raise NativeError('APP_INSTANCE_REQUIRED', 'Exactly one native Capture One main window must match the Session title and executable',
                          {'matching_windows': [row[0] for row in matches]})
    window, identity = matches[0]
    version = _executable_version(identity['executable'])
    fresh = read_window(window['hwnd'])
    if (fresh is None or any(fresh[key] != window[key] for key in ('pid', 'hwnd', 'title'))
            or _process_identity(window['pid']) != identity):
        raise NativeError('TARGET_CHANGED', 'Main window or process changed during native discovery')
    return {**fresh, 'exe': identity['executable'], 'started_at': identity['started_at'], **version,
            'identity_scope': 'native_window_process_and_cached_title_only', 'document_path_verified': False,
            'selected_target_verified': False, 'uia_queried': False}


def native_window_inventory(pid, main_hwnd, *, expected_started_at=None, limit=2048):
    """Return modal candidates, not a claim that arbitrary dialogs may be closed.

    GetWindowTextW reads another process's cached window caption; no WM_GETTEXT,
    UIA descendant walks, activation, input, or window-close messages are used.
    External helper-process dialogs are outside this same-process inventory.
    """
    from .windows_backend import NativeError, process_started
    if type(pid) is not int or pid <= 0 or type(main_hwnd) is not int or main_hwnd <= 0:
        raise NativeError('INVALID_WINDOW_REQUEST', 'A positive process ID and exact main HWND are required')
    if type(limit) is not int or not 1 <= limit <= 4096:
        raise NativeError('INVALID_WINDOW_REQUEST', 'Window enumeration limit must be between 1 and 4096')
    if expected_started_at is not None and process_started(pid) != expected_started_at:
        raise NativeError('TARGET_CHANGED', 'Application process changed before window inventory')
    u = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    u.EnumWindows.argtypes = [callback_type, W.LPARAM]
    u.EnumWindows.restype = W.BOOL
    u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    u.GetWindowThreadProcessId.restype = W.DWORD
    u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindow.argtypes = [W.HWND, W.UINT]
    u.GetWindow.restype = W.HWND
    u.GetParent.argtypes = [W.HWND]
    u.GetParent.restype = W.HWND
    for name in ('IsWindow', 'IsWindowEnabled', 'IsWindowVisible'):
        getattr(u, name).argtypes = [W.HWND]
        getattr(u, name).restype = W.BOOL

    def owner_pid(hwnd):
        value = W.DWORD()
        if not u.IsWindow(hwnd) or not u.GetWindowThreadProcessId(hwnd, ctypes.byref(value)):
            raise NativeError('WINDOW_UNAVAILABLE', 'A window disappeared during native inventory')
        return value.value

    if owner_pid(main_hwnd) != pid:
        raise NativeError('TARGET_CHANGED', 'Main HWND no longer belongs to the expected process')
    records, failures = [], []
    visited = 0
    truncated = False

    def collect(hwnd, unused):
        nonlocal visited, truncated
        visited += 1
        if visited > limit:
            truncated = True
            return False
        try:
            actual_pid = owner_pid(hwnd)
            if actual_pid != pid:
                return True
            title = ctypes.create_unicode_buffer(512)
            kind = ctypes.create_unicode_buffer(256)
            title_length = u.GetWindowTextW(hwnd, title, len(title))
            class_length = u.GetClassNameW(hwnd, kind, len(kind))
            records.append({'hwnd': int(hwnd), 'pid': actual_pid, 'title': title.value,
                            'class_name': kind.value, 'owner_hwnd': int(u.GetWindow(hwnd, 4) or 0),
                            'parent_hwnd': int(u.GetParent(hwnd) or 0),
                            'visible': bool(u.IsWindowVisible(hwnd)), 'enabled': bool(u.IsWindowEnabled(hwnd)),
                            'text_truncated': title_length >= len(title) - 1 or class_length >= len(kind) - 1})
        except Exception as error:
            failures.append({'hwnd': int(hwnd), 'code': getattr(error, 'code', 'WINDOW_READ_FAILED'), 'message': str(error)})
        return True

    callback = callback_type(collect)
    ctypes.set_last_error(0)
    succeeded = bool(u.EnumWindows(callback, 0))
    native_error = ctypes.get_last_error()
    if not succeeded and not truncated:
        raise NativeError('WINDOW_ENUMERATION_FAILED', 'Native window enumeration did not complete', {'win32_error': native_error})
    if owner_pid(main_hwnd) != pid:
        raise NativeError('TARGET_CHANGED', 'Main HWND changed during native inventory')
    if expected_started_at is not None and process_started(pid) != expected_started_at:
        raise NativeError('TARGET_CHANGED', 'Application process changed during native inventory')
    lookup = {row['hwnd']: row for row in records}
    main = lookup.get(main_hwnd)
    complete = succeeded and not truncated and not failures and main is not None
    candidates = []
    for row in records:
        if row['hwnd'] == main_hwnd or not row['visible']:
            continue
        chain, cursor = [], row['owner_hwnd']
        while cursor and cursor not in chain and len(chain) < 32:
            chain.append(cursor)
            if cursor == main_hwnd:
                candidates.append(dict(row, owner_chain=chain, uia_modal_verified=False))
                break
            cursor = lookup.get(cursor, {}).get('owner_hwnd', 0)
    main_enabled = main['enabled'] if main else None
    possible = (True if candidates else None) if main_enabled is False else (False if main_enabled is True else None)
    return {'pid': pid, 'main_hwnd': main_hwnd, 'process_started_at': expected_started_at,
            'windows': records, 'modal_candidates': candidates, 'main_enabled': main_enabled,
            'blocking_modal_possible': possible, 'enumeration_complete': complete, 'truncated': truncated,
            'read_errors': failures, 'visited_window_count': min(visited, limit),
            'scope': 'same_process_native_windows_and_owner_chains', 'external_helpers_included': False,
            'atomic': False, 'uia_queried': False, 'mutations_sent': False, 'automatic_close_permitted': False}
