"""C1's async bitmap renderer, polled without blocking its UI thread."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .windows_backend import NativeError


def preview(backend, request, observation):
    started = time.monotonic()
    payload = {'command': 'read', 'kind': request.get('kind', 'native'),
               'max_size': request.get('max_size', 1600)}
    raw=request.get('pixel_buffer') is True
    if raw:payload['pixel_buffer']=True
    if request.get('action') == 'map_points':
        result = backend._model_call({**payload, 'scope': 'preview_map',
            'preview_id': request['preview_id'], 'points': request['points'],
            'direction': request.get('direction', 'preview_to_raw')}, observation)
        return {'status': 'completed', **result, 'model_verified': True}
    result = backend._model_call({**payload, 'scope': 'preview_start'}, observation)
    preview_id = result['preview_id']
    while True:
        result = backend._model_call({**payload, 'scope': 'preview_result',
                                      'preview_id': preview_id}, observation)
        if result['phase'] == 'ready':
            break
        if time.monotonic() - started > 25:
            raise NativeError('NATIVE_RENDER_PENDING', 'C1 is still rendering; a subsequent render reuses the same job',
                              {'preview_id': preview_id})
        time.sleep(.02 if raw else .08)
    artifact = result['artifact']
    extension='.bgra' if raw else '.png'
    root = (backend.state_dir / ('native-pixels' if raw else 'native-previews')).resolve()
    path = Path(artifact['path'])
    if path.is_symlink() or path.resolve().parent != root or path.name != artifact['sha256'] + extension:
        raise NativeError('INVALID_NATIVE_PREVIEW_PATH', 'Native preview path is outside the owner cache')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact['sha256']:
        raise NativeError('NATIVE_PREVIEW_CHANGED', 'Native preview changed after rendering')
    # Native previews are already sized for AI input. Keep eight, without generating JPEG proxies.
    old = sorted((p for p in root.glob('*'+extension) if len(p.stem) == 64
                  and all(c in '0123456789abcdef' for c in p.stem) and p != path
                  and not p.is_symlink() and p.resolve().parent == root),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    for obsolete in old[7:]:
        try:
            obsolete.unlink()
        except OSError:
            pass  # Cache cleanup must not invalidate a successful native render.
    return {'status': 'completed', 'source': 'capture_one_native_mask' if payload['kind'] == 'mask'
            else 'capture_one_native_render', 'path': str(path), 'image': artifact,
            'preview_id': preview_id, 'revision': result['revision'], 'geometry': result['geometry'],
            'target': observation['target'], 'render_verified': True, 'model_verified': True,
            'current_adjustments_verified': True, 'export_required': False, 'focus_used': False,
            'elapsed_seconds': round(time.monotonic() - started, 4),
            'color_profile': 'C1 internal preview processing; embedded output ICC not independently qualified'}
