"""Read-only acquisition probe. Saves private image bytes and diagnostics, never screenshots."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageCms


def describe(data):
    with Image.open(io.BytesIO(data)) as im:
        icc = im.info.get('icc_profile')
        result = dict(format=im.format, size=list(im.size), mode=im.mode,
                      encoded_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                      icc_present=bool(icc), icc_sha256=hashlib.sha256(icc).hexdigest() if icc else None,
                      bit_depth=data[24] if data.startswith(b'\x89PNG') else 8 if im.format == 'JPEG' else None)
        if icc:
            try:
                result['icc_description'] = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
            except Exception as error:
                result['icc_error'] = str(error)
        return result


def acquire_native(size=1600):
    from c1ai.client import call
    start = time.monotonic()
    before = call({'command': 'read', 'fields': []})
    if before.get('status') != 'ok':
        raise RuntimeError(json.dumps(before, ensure_ascii=False))
    response = call({'command': 'preview', 'kind': 'native', 'max_size': size,
                     'target': before['target'], 'observation_token': before['observation_token'],
                     'request_id': uuid.uuid4().hex})
    if response.get('status') != 'completed':
        raise RuntimeError(json.dumps(response, ensure_ascii=False))
    result = response['result']
    data = Path(result['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != result['image']['sha256']:
        raise RuntimeError('Native artifact hash mismatch')
    after = call({'command': 'read', 'fields': [], 'target': before['target']})
    if after.get('status') != 'ok':
        raise RuntimeError('Selection changed during acquisition: ' + json.dumps(after, ensure_ascii=False))
    return data, dict(source='capture_one_native_render', target=before['target'],
                      native_result=result, elapsed_seconds=time.monotonic()-start,
                      desktop_selection_verified=True, screenshot_used=False, export_used=False)


def acquire_pilot(port, variant=None, size=1600):
    # Probe only: never auto-select the latest capture or claim that a server revision proves a fresh render.
    base = f'http://127.0.0.1:{port}/'
    def request(name, params=None, post=False):
        query = urllib.parse.urlencode({**(params or {}), 'timestamp': int(time.time()*1000)})
        req = urllib.request.Request(base+name+'?'+query, data=b'' if post else None,
                                     headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = r.read(32*1024*1024+1)
            if len(data)>32*1024*1024:
                raise ValueError('Oversized protocol response')
            return data
    protocol = request('getProtocolVersion').decode().strip()
    session = request('connectToService', {'protocolVersion': '2.4'}, True).decode().strip().strip('"')
    if not session or session == '0':
        raise RuntimeError('Capture Pilot did not issue a session')
    state = json.loads(request('getServerState', {'sessionID': session}))
    evidence = dict(source='capture_pilot_probe', port=port, protocol=protocol,
                    state=state, desktop_selection_verified=False, latest_render_verified=False,
                    limitation='Select an explicit service variant ID; server updates are not render completion evidence.')
    if not variant:
        return None, evidence
    data = request('getImage', {'sessionID': session, 'id': variant, 'width': size,
                               'height': size, 'left': 0, 'top': 0, 'right': 0, 'bottom': 0})
    evidence['service_variant_id'] = variant
    return data, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['native', 'pilot'], default='native')
    parser.add_argument('--port', type=int)
    parser.add_argument('--variant', help='Exact Capture Pilot variant ID (manual selection only)')
    parser.add_argument('--size', type=int, default=1600, choices=range(128, 1601), metavar='128..1600')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = {'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'source_requested': args.source}
    try:
        if args.source == 'pilot' and not args.port:
            raise ValueError('--port required; do not assume a Mac service port on Windows')
        data, details = acquire_native(args.size) if args.source == 'native' else acquire_pilot(args.port, args.variant, args.size)
        evidence.update(details)
        if data:
            evidence['image'] = describe(data)
            sample = args.output/('sample.'+evidence['image']['format'].lower())
            sample.write_bytes(data)
            evidence['sample'] = str(sample.resolve())
        evidence['status'] = 'acquired' if data else 'service_list_only'
    except Exception as error:
        evidence.update(status='failed', error=str(error))
    (args.output/'probe.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return int(evidence['status'] == 'failed')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
