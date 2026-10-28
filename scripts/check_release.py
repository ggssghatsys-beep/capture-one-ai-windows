"""Read-only stdio smoke check. Never adjusts, clones, selects or exports a photo."""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def data(result):
    return result.structuredContent or json.loads(next(c.text for c in result.content if c.type == 'text'))


async def check(require_live=False):
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'c1ai', 'mcp'],
        cwd=tempfile.gettempdir(), env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialization = await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'context', 'images', 'read', 'adjust', 'preview', 'operations'}
            assert initialization.instructions
            context = data(await session.call_tool('context', {}))
            if context.get('error', {}).get('code') == 'APP_INSTANCE_REQUIRED' or (
                    context.get('status') == 'ok' and not context.get('editing_ready')):
                if require_live:
                    raise RuntimeError(context.get('error'))
                print(json.dumps({'status': 'passed_transport', 'tools': 6,
                    'live_preview': 'skipped', 'reason': context.get('error', {}).get('code'),
                    'photo_writes': 0}))
                return
            assert context['status'] == 'ok', context.get('error')
            before = data(await session.call_tool('read', {'module': 'exposure'}))
            assert before['status'] == 'ok'
            preview = await session.call_tool('preview', {'kind': 'native', 'max_size': 800,
                'target': before['target'], 'observation_token': before['observation_token']})
            assert data(preview)['status'] == 'completed', data(preview).get('error')
            assert any(c.type == 'image' and c.mimeType == 'image/png' for c in preview.content)
            after = data(await session.call_tool('read', {'module': 'exposure', 'target': before['target']}))
            assert after['status'] == 'ok' and after['target'] == before['target'] and after['values'] == before['values']
            print(json.dumps({'status': 'passed', 'tools': 6, 'image_block': 'image/png',
                              'target_and_exposure_unchanged': True, 'photo_writes': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-live', action='store_true')
    asyncio.run(check(parser.parse_args().require_live))
