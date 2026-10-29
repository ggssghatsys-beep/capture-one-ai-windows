"""Focused controller checks; fake providers are NOT evidence of live model support."""
import asyncio
from copy import deepcopy
import json
import os
import threading
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from c1ai.ai_agent import PhotoAgent, AgentStopped, identity, result_data, api_content
from c1ai.ai_settings import load_settings, save_settings, normalize_url

TARGET = {'instance': {'pid': 1}, 'document': {'path': 'session'},
          'image': {'path': 'image'}, 'variant': {'id': 'variant'}, 'layer': {'name': 'Background'}}


def result(data, image=False):
    content = [TextContent(type='text', text=json.dumps(data))]
    if image:
        content.append(ImageContent(type='image', data='aW1hZ2U=', mimeType='image/png'))
    return CallToolResult(content=content, structuredContent=data)


class Tools:
    def __init__(self):
        self.calls = []
        self.target = deepcopy(TARGET)
        self.adjust_status = 'control_confirmed'

    async def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == 'images':
            self.target['variant'] = {'id': 'candidate'}
            return result({'status': 'completed', 'after': {'target': deepcopy(self.target)}})
        if name == 'adjust':
            return result({'status': self.adjust_status, 'target': self.target, 'request_id': args['request_id']})
        if name == 'preview':
            return result({'status': 'completed'}, image=True)
        return result({'status': 'ok', 'target': deepcopy(self.target), 'observation_token': 'observed'})


def run(awaitable):
    return asyncio.run(awaitable)


def noop(*args):
    pass


def test_readonly_and_copy_reuse_with_image_payload():
    tools = Tools()
    agent = PhotoAgent(tools)
    stop = threading.Event()
    observed, preview = run(agent.prepare(False, noop, stop))
    assert any(c['type'] == 'input_image' for c in api_content(preview))
    denied = run(agent.execute('adjust', {'target': observed['target']}, noop, stop))
    assert result_data(denied)['status'] == 'refused'
    assert not any(name == 'adjust' for name, _ in tools.calls)
    run(agent.prepare(True, noop, stop))
    run(agent.prepare(True, noop, stop))
    assert sum(name == 'images' for name, _ in tools.calls) == 1


def test_selection_guard_and_unsafe_routes():
    tools = Tools()
    agent = PhotoAgent(tools)
    stop = threading.Event()
    run(agent.prepare(False, noop, stop))
    for name, args in [('images', {'action': 'next'}), ('preview', {'kind': 'viewer'}),
                       ('analyze_image', {'source': 'file', 'path': 'private'}),
                       ('operations', {'action': 'reconcile'})]:
        assert result_data(run(agent.execute(name, args, noop, stop)))['status'] == 'refused'
    tools.target['variant'] = {'id': 'other'}
    with pytest.raises(AgentStopped, match='切换'):
        run(agent.execute('read', {'fields': []}, noop, stop))
    with pytest.raises(AgentStopped, match='另一张'):
        run(agent.execute('read', {'target': tools.target}, noop, stop))


def test_uncertain_write_and_cancel_never_replay():
    tools = Tools()
    agent = PhotoAgent(tools)
    stop = threading.Event()
    run(agent.prepare(True, noop, stop))
    tools.adjust_status = 'outcome_unknown'
    with pytest.raises(AgentStopped, match='禁止重发'):
        run(agent.execute('adjust', {'target': tools.target}, noop, stop))
    assert sum(name == 'adjust' for name, _ in tools.calls) == 1
    stop.set()
    with pytest.raises(AgentStopped, match='停止'):
        run(agent.execute('adjust', {'target': tools.target}, noop, stop))
    assert sum(name == 'adjust' for name, _ in tools.calls) == 1


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI only')
def test_encrypted_key_bound_to_endpoint_and_forget(tmp_path):
    path = tmp_path / 'settings.json'
    key = 'test-only-secret-not-a-real-api-key'
    save_settings('https://api.example.com/v1', 'account-model', key, True, path, effort='provider-level')
    assert key not in path.read_text('utf-8')
    assert load_settings(path)['key'] == key
    assert load_settings(path)['effort'] == 'provider-level'
    tampered = json.loads(path.read_text('utf-8'))
    tampered['base_url'] = 'https://another.example.com/v1'
    path.write_text(json.dumps(tampered), 'utf-8')
    assert load_settings(path)['key'] == ''
    save_settings('https://api.example.com/v1', 'account-model', key, False, path)
    assert 'encrypted_credentials' not in json.loads(path.read_text('utf-8'))
    assert load_settings(path)['key'] == ''
    with pytest.raises(ValueError):
        normalize_url('http://example.com/v1')


def test_discovery_from_metadata_and_validator_without_catalog():
    pytest.importorskip('openai')
    from c1ai.ai_capabilities import metadata_efforts, error_efforts, discover_efforts
    assert metadata_efforts({'id': 'any-model'}) == []
    assert metadata_efforts({'capabilities': {'reasoning': {'efforts': ['novel', 'deep']}}}) == ['novel', 'deep']
    assert error_efforts({'error': {'param': 'reasoning.effort',
        'message': "Unsupported value. Supported values are: 'light', 'deep'."}}) == ['light', 'deep']
    assert error_efforts({'error': {'param': 'model', 'message': "Supported values are: 'secret'."}}) == []
    client = SimpleNamespace(models=SimpleNamespace(retrieve=lambda model:
        SimpleNamespace(model_dump=lambda **kwargs: {'id': model})),
        responses=SimpleNamespace(create=lambda **kwargs: None))
    assert discover_efforts(client, 'unlisted-model')['values'] == []


def test_responses_loop_returns_images_and_effort_to_provider():
    from c1ai.ai_agent import ALLOWED
    class ProviderItem:
        type = 'function_call'
        name, call_id, arguments = 'read', 'call_1', '{"fields":[]}'
        def model_dump(self, **kwargs):
            return {'type': self.type, 'name': self.name, 'call_id': self.call_id, 'arguments': self.arguments}
    class Responses:
        def __init__(self):
            self.inputs = []
        def stream(self, **kwargs):
            self.inputs.append(deepcopy(kwargs))
            number = len(self.inputs)
            response = SimpleNamespace(status='completed', usage=None,
                output=[ProviderItem()] if number == 1 else [], output_text='分析完成')
            class Stream:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def __iter__(self): return iter([])
                def get_final_response(self): return response
            return Stream()
    class TestTools(Tools):
        async def definitions(self):
            return [SimpleNamespace(name=name, description='', inputSchema={'type': 'object'}) for name in ALLOWED]
    responses = Responses()
    agent = PhotoAgent(TestTools())
    text = run(agent.run('看一下', client=SimpleNamespace(responses=responses), model='from-api',
        effort='from-server', edit=False, stopped=threading.Event(), emit=noop))
    assert text == '分析完成'
    assert responses.inputs[0]['reasoning'] == {'effort': 'from-server'}
    assert any(item['type'] == 'input_image' for item in responses.inputs[0]['input'][0]['content'])
    assert 'adjust' not in [tool['name'] for tool in responses.inputs[0]['tools']]
    assert responses.inputs[1]['input'][-1]['type'] == 'function_call_output'
