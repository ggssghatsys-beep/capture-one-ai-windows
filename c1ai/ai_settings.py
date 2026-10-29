"""Local API preferences. Optional secrets use Windows DPAPI, never repository files."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_URL = 'https://api.openai.com/v1'


def normalize_url(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value.strip()):
        raise ValueError('API 地址格式无效。')
    parsed = urlsplit(value.strip().rstrip('/'))
    # Accessing .port validates malformed and out-of-range ports before any network call.
    _ = parsed.port
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ('https', 'http')):
        raise ValueError('API 地址必须是完整的 HTTPS 地址，不能带用户名、查询参数或密钥。')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程 API 必须使用 HTTPS；HTTP 仅允许本机服务。')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('保存密钥需要 Windows DPAPI；其他平台请仅在本次运行使用。')
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; user-scoped, not machine-scoped encryption.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(output.data)


def settings_path() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CaptureOneAI' / 'ai-settings.json'


def load_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    if not path.exists():
        return {'base_url': DEFAULT_URL, 'model': '', 'key': '', 'remember': False}
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('API 配置格式无效。')
    for field in ('base_url', 'model', 'effort', 'encrypted_credentials'):
        if field in value and not isinstance(value[field], str):
            raise ValueError('API 配置字段格式无效，请重新设置。')
    endpoint = normalize_url(value.get('base_url', DEFAULT_URL))
    key, warning = '', ''
    if value.get('encrypted_credentials'):
        try:
            secret = json.loads(_crypt(base64.b64decode(value['encrypted_credentials']), True))
            if not isinstance(secret, dict) or secret.get('base_url') != endpoint:
                raise ValueError('API 地址已变化，请重新输入密钥。')
            key = secret['key']
            if not isinstance(key, str):
                raise ValueError('保存的密钥格式无效。')
        except (OSError, ValueError, KeyError):
            key = ''
            warning = '保存的密钥无法解密或地址已变化，请重新输入。'
    return {'base_url': endpoint, 'model': value.get('model', ''), 'effort': value.get('effort', ''), 'key': key,
            'remember': bool(key), 'warning': warning}


def save_settings(base_url: str, model: str, key: str, remember: bool,
                  path: Path | None = None, *, effort: str = '') -> None:
    value = {'base_url': normalize_url(base_url), 'model': model.strip(), 'effort': effort.strip()}
    if remember and key:
        secret = json.dumps({'base_url': value['base_url'], 'key': key}).encode('utf-8')
        value['encrypted_credentials'] = base64.b64encode(_crypt(secret)).decode('ascii')
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(path)
