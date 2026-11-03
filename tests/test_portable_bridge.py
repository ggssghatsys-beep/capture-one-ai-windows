"""Package integrity and Session-switch guards; no application calls."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from c1ai import bridge_packages as packages
from c1ai.contracts import Refused
from c1ai.core import Core
from c1ai.formatting import compact_response
from c1ai.session_binding import SessionBinding
from c1ai.windows_backend import NativeError


ROOT = Path(__file__).resolve().parent.parent
INSTANCE = {'pid': 123, 'started_at': '456'}


def session(name='A', identifier='11111111-1111-1111-1111-111111111111'):
    return {'document_path': str((ROOT / (name + '.cosessiondb')).resolve()), 'document_uuid': identifier}


def test_follow_switch_and_return_invalidates_old_generation():
    binding = SessionBinding({})
    first = binding.accept(session(), INSTANCE)
    assert binding.accept(session(), INSTANCE) == first
    second = binding.accept(session('B'), INSTANCE)
    returned = binding.accept(session(), INSTANCE)
    assert len({first['generation'], second['generation'], returned['generation']}) == 3


def test_pinned_and_missing_native_identity_are_refused():
    binding = SessionBinding({'session_binding': 'pinned', 'session_path': session()['document_path']})
    binding.accept(session(), INSTANCE)
    with pytest.raises(NativeError, match='pinned'):
        binding.accept(session('B'), INSTANCE)
    with pytest.raises(NativeError, match='UUID'):
        SessionBinding({}).accept(session(identifier=None), INSTANCE)


def test_mcp_read_keeps_native_session_identity():
    binding = SessionBinding({}).accept(session(), INSTANCE)
    response = {'status': 'ok', 'target': {'variant': {'id': 'one'}},
                'values': {}, 'session_binding': binding, 'observation_token': 'one'}
    assert compact_response(response, command='read')['session_binding'] == binding


def test_missing_current_binding_cannot_validate_a_bound_module_token(tmp_path):
    target={'instance':{},'document':{'path':'session'},'image':{'path':'photo'},'variant':{},'layer':'Background'}
    current={'target':target,'values':{}}
    core=Core(SimpleNamespace(observe=lambda fields:current),state_dir=tmp_path)
    try:
        token=core.journal.observe({**current,'module':'curves','state':{},'session_binding':{'generation':'old'}})
        with pytest.raises(Refused) as failure:
            core._preflight({'command':'adjust','module':'curves','target':target,'observation_token':token})
        assert failure.value.code=='session_changed'
    finally:core.close()


@pytest.mark.parametrize('navigation', [False, True])
def test_stale_session_token_refused_before_any_write(tmp_path, navigation):
    binder = SessionBinding({})
    first = binder.accept(session(), INSTANCE)
    binder.accept(session('B'), INSTANCE)
    returned = binder.accept(session(), INSTANCE)
    target = {'instance': INSTANCE, 'document': {'path': first['path']},
              'image': {'path': 'photo.NEF'}, 'variant': {'id': 'one'}, 'layer': 'Background'}
    saved = {'target': target, 'values': {}, 'session_binding': first}
    current = {**saved, 'session_binding': returned}
    if navigation:
        saved['navigation'] = current['navigation'] = True
        backend = SimpleNamespace(image_action=lambda *args: {'_navigation_observation': current})
        request = {'command': 'images', 'action': 'select'}
    else:
        saved['module'] = 'curves'
        backend = SimpleNamespace(observe=lambda fields: current)
        request = {'command': 'adjust', 'module': 'curves'}
    core = Core(backend, state_dir=tmp_path)
    try:
        request.update(target=target, observation_token=core.journal.observe(saved))
        with pytest.raises(Refused) as refused:
            core._preflight(request)
        assert refused.value.code == ('stale_selection' if navigation else 'session_changed')
    finally:
        core.journal.close()


def package_bytes(*, extra=None):
    matrix = packages.read_matrix(ROOT)
    files = {'pyproject.toml': b'[project]\nversion="0.4.0a2"\n',
             'config/bridge-compatibility.json': json.dumps(matrix).encode(),
             'c1ai/__init__.py': b'# paired Python source\n'}
    if extra:
        files.update(extra)
    manifest = {'version': '0.4.0a2', 'files': {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr('release/' + name, content)
        archive.writestr('release/RELEASE-MANIFEST.json', json.dumps(manifest))
    data = output.getvalue()
    metadata = {**matrix, 'core_version': '0.4.0a2', 'package': {
        'name': 'source.zip', 'sha256': hashlib.sha256(data).hexdigest()}}
    return data, metadata


def test_prepare_and_verify_paired_source_package(tmp_path):
    data, metadata = package_bytes()
    project = packages.unpack_package(data, metadata, tmp_path / 'cache')
    assert packages.verify_package(project) == project
    (project / 'c1ai/__init__.py').write_text('changed')
    with pytest.raises(ValueError, match='changed'):
        packages.verify_package(project)


@pytest.mark.parametrize('extra', [{'/escape.py': b'bad'}, {'C1ai/__init__.py': b'collision'}])
def test_zip_absolute_suffix_and_case_collisions_refused(tmp_path, extra):
    data, metadata = package_bytes(extra=extra)
    with pytest.raises(ValueError):
        packages.unpack_package(data, metadata, tmp_path / 'cache')
    assert not (tmp_path / 'escape.py').exists()


def test_corrupt_archive_and_unknown_host_refused(tmp_path):
    data, metadata = package_bytes()
    with pytest.raises(ValueError, match='SHA256'):
        packages.unpack_package(data + b'corrupt', metadata, tmp_path / 'cache')
    matrix = packages.read_matrix(ROOT)
    host = {'platform': 'windows', 'architecture': 'x64',
            'capture_one_version': '99.0.0.0', 'runtime_major': 10}
    assert packages.match_adapter(matrix, host) is None


def test_download_source_restricted_to_project_releases(monkeypatch):
    monkeypatch.setattr(packages, 'urlopen', lambda *args, **kwargs: pytest.fail('must not request an untrusted URL'))
    with pytest.raises(ValueError, match='outside'):
        packages._fetch('https://example.com/source.zip', 1024)
