"""Guarded opt-in original editing; no Capture One or private registry used."""
import copy
import json
from unittest.mock import Mock

import pytest

from c1ai.core import Core
from c1ai.windows_backend import WindowsBackend


@pytest.fixture
def system(tmp_path):
    backend = WindowsBackend.__new__(WindowsBackend)
    backend.state_dir = tmp_path
    backend.config = {'qualified_version': 'test', 'native_qualified_fields': ['exposure'],
                      'qualified_modules': ['curves', 'layers']}
    backend.action_hook = None
    observation = {'target': {'instance': {'pid': 123, 'started_at': 'one', 'version': 'test'},
        'document': {'path': 'C:/test/session.cosessiondb'}, 'image': {'path': 'C:/test/photo.NEF'},
        'variant': {'id': 'original', 'label': 'photo.NEF'}, 'layer': 'Background'},
        'values': {}, 'session_binding': {'uuid': 'session-one', 'generation': 'binding-one'},
        'evidence': {'source': 'c1_internal_model', 'model_verified': True}}
    backend.observe = Mock(side_effect=lambda fields: copy.deepcopy(observation))
    backend._model_call = Mock(side_effect=AssertionError('Authorization must not change C1'))
    core = Core(backend, tmp_path)
    yield core, backend, observation
    core.journal.db.close()


def prepare(core, mode='original', request_id='authorize-one', observed=None):
    observed = observed or core.dispatch({'command': 'read', 'fields': []})
    request = {'command': 'images', 'action': 'prepare_edit', 'request_id': request_id,
               'target': observed['target'], 'observation_token': observed['observation_token']}
    if mode is not None:
        request['edit_mode'] = mode
    return core.dispatch(request)


def test_original_opt_in_preserves_identity_and_parameters_and_is_journaled(system):
    core, backend, observation = system
    assert not backend.capabilities_for(observation)['fields']['exposure']['writable']
    result = prepare(core)
    assert result['status'] == 'completed'
    assert result['after']['target'] == observation['target']
    assert result['result']['photo_parameters_changed'] is False
    record = core.journal.get(request_id='authorize-one')
    assert record['actions'][0]['name'] == 'images.authorize_original'
    assert record['actions'][0]['status'] == 'completed'
    registration = json.loads((backend.state_dir/'candidates.json').read_text())['original']
    assert registration['edit_mode'] == 'original'
    assert registration['operation_id'] == record['operation_id']
    assert registration['session_uuid'] == 'session-one'
    assert backend.capabilities_for(observation)['fields']['exposure']['writable']
    assert backend.capabilities_for(observation)['modules']['curves']['writable']
    backend._model_call.assert_not_called()


@pytest.mark.parametrize('mode', [None, '', 'current', True])
def test_mode_must_be_explicit(system, mode):
    core, backend, _ = system
    result = prepare(core, mode)
    assert result['status'] == 'refused'
    assert result['error']['code'] == 'invalid_edit_mode'
    assert not (backend.state_dir/'candidates.json').exists()


@pytest.mark.parametrize('change', ['variant', 'image', 'document', 'binding'])
def test_stale_authorization_never_registers_new_target(system, change):
    core, backend, observation = system
    observed = core.dispatch({'command': 'read', 'fields': []})
    if change == 'binding':
        observation['session_binding']['generation'] = 'different'
    elif change == 'variant':
        observation['target']['variant']['id'] = 'other'
    else:
        observation['target'][change]['path'] = 'C:/test/other'
    assert prepare(core, observed=observed)['status'] == 'refused'
    assert not (backend.state_dir/'candidates.json').exists()


def test_authorization_does_not_transfer_across_session_uuid_or_photo(system):
    core, backend, observation = system
    assert prepare(core)['status'] == 'completed'
    for changed in ('session', 'image', 'variant', 'version'):
        other = copy.deepcopy(observation)
        if changed == 'session': other['session_binding']['uuid'] = 'another-session'
        elif changed == 'image': other['target']['image']['path'] = 'other.NEF'
        elif changed == 'variant': other['target']['variant']['id'] = 'other'
        else: other['target']['instance']['version'] = 'unqualified'
        assert not backend.capabilities_for(other)['fields']['exposure']['writable']


def test_same_request_id_does_not_repeat_authorization(system):
    core, backend, _ = system
    observed = core.dispatch({'command': 'read', 'fields': []})
    first = prepare(core, observed=observed)
    registry = (backend.state_dir/'candidates.json').read_bytes()
    second = prepare(core, observed=observed)
    assert second['operation_id'] == first['operation_id']
    assert second['existing'] is True
    assert (backend.state_dir/'candidates.json').read_bytes() == registry
    assert len(core.journal.get(request_id='authorize-one')['actions']) == 1


def test_copy_mode_routes_to_existing_clone_without_authorizing_original(system):
    core, backend, _ = system
    backend.image_action = Mock(return_value={'status': 'completed'})
    assert prepare(core, 'copy')['status'] == 'completed'
    assert backend.image_action.call_args.args[0] == 'clone'
    assert not (backend.state_dir/'candidates.json').exists()


def test_unsupported_platform_does_not_inherit_original_mode(system):
    core, backend, _ = system
    backend.original_edit_supported = False
    assert prepare(core)['error']['code'] == 'unsupported'


def test_existing_copy_registration_is_preserved(system):
    core, backend, observation = system
    target = observation['target']
    old = {'document': target['document'], 'image': target['image'],
           'source_variant': {'id': 'source'}, 'qualification': 'native_clone_uuid_and_image_identity'}
    path = backend.state_dir/'candidates.json'
    path.write_text(json.dumps({'original': old, 'unrelated': {'preserve': True}}))
    data = path.read_bytes()
    assert prepare(core)['status'] == 'completed'
    assert path.read_bytes() == data
    assert backend.capabilities_for(observation)['edit_target']['mode'] == 'copy'
