"""Provider-reported model/effort discovery. No baked-in model or effort catalogue."""
from __future__ import annotations

import re

from openai import APIStatusError


def list_models(client) -> list[dict]:
    page = client.models.list()
    # The standard /models response is unpaginated. Preserve extension metadata.
    return sorted((item.model_dump(exclude_none=True) for item in page.data), key=lambda row: row['id'])


def _values(value) -> list[str]:
    if not isinstance(value, list) or not 0 < len(value) <= 24:
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', v)))


def metadata_efforts(metadata: dict) -> list[str]:
    """Accept explicit provider capability fields only; never infer from a model name."""
    for name in ('supported_reasoning_efforts', 'reasoning_efforts'):
        result = _values(metadata.get(name))
        if result:
            return result
    capabilities = metadata.get('capabilities')
    reasoning = (capabilities.get('reasoning') if isinstance(capabilities, dict) else None) or metadata.get('reasoning')
    if isinstance(reasoning, dict):
        for name in ('efforts', 'supported_efforts'):
            result = _values(reasoning.get(name))
            if result:
                return result
        effort = reasoning.get('effort')
        if isinstance(effort, dict):
            return _values(effort.get('enum'))
    return []


def error_efforts(body) -> list[str]:
    if not isinstance(body, dict):
        return []
    error = body.get('error', body)
    if not isinstance(error, dict):
        return []
    message = str(error.get('message', ''))
    param = str(error.get('param', ''))
    if param not in ('reasoning.effort', 'reasoning_effort') and 'reasoning.effort' not in message:
        return []
    result = _values(error.get('supported_values') or error.get('allowed_values'))
    if result:
        return result
    # Parse only the supported-values clause; never display raw provider errors/headers.
    match = re.search(r'(?:supported|allowed)\s+values(?:\s+are)?\s*:?\s*(.+)', message, re.I)
    if match:
        return _values(re.findall(r"['\"]([a-z][a-z0-9_-]{0,30})['\"]", match.group(1)))
    return []


def discover_efforts(client, model: str, metadata: dict | None = None) -> dict:
    values = metadata_efforts(metadata or {})
    if values:
        return {'values': values, 'source': '服务模型元数据', 'verified': False}
    try:
        detail = client.models.retrieve(model).model_dump(exclude_none=True)
        values = metadata_efforts(detail)
        if values:
            return {'values': values, 'source': '服务模型详情', 'verified': False}
    except APIStatusError as error:
        if error.status_code not in (400, 404, 405):
            raise
    # /models normally contains identity/ownership, not reasoning capabilities.
    # A deliberately invalid value asks the server validator to enumerate its choices.
    # Some compatible servers ignore unknown fields and may execute this tiny text request.
    try:
        client.responses.create(model=model, input='Reply OK.', max_output_tokens=16, store=False,
                                reasoning={'effort': '__c1ai_capability_query__'})
    except APIStatusError as error:
        if error.status_code not in (400, 422):
            raise
        values = error_efforts(error.body)
        if values:
            return {'values': values, 'source': '服务校验返回（未逐项验证）', 'verified': False}
    return {'values': [], 'source': '服务未返回等级信息；保留服务默认值或手动填写后测试', 'verified': False}
