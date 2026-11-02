"""Verify the installed Windows MCP launcher without contacting a C1 owner."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import shutil

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check():
    project = Path(__file__).resolve().parent.parent
    parameters = StdioServerParameters(
        command=shutil.which('pwsh') or 'pwsh',
        args=['-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(project / 'MCP.ps1')],
        cwd=str(project), env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'})
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialization = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            expected = {'context', 'images', 'read', 'adjust', 'preview', 'operations', 'analyze_image'}
            if names != expected or not initialization.instructions:
                raise RuntimeError('Installed MCP tools or instructions are incomplete')
    print(json.dumps({'status': 'passed', 'tools': sorted(names),
                      'owner_started': False, 'c1_contacted': False, 'photo_writes': 0}))


async def check_registry():
    from c1ai.mcp_server import mcp
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    if names != {'context', 'images', 'read', 'adjust', 'preview', 'operations', 'analyze_image'}:
        raise RuntimeError('Tool registry incomplete')
    print(json.dumps({'status': 'passed_registry', 'tools': sorted(names),
                      'stdio_verified': False, 'c1_contacted': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry-only', action='store_true')
    args = parser.parse_args()
    asyncio.run(check_registry() if args.registry_only else check())
