"""Basic/Advanced/Skin codecs over the single native model endpoint."""
from __future__ import annotations

import math
import re
import struct

from .native_images import fail

COMPONENTS = ("hue", "saturation", "lightness")
COLUMNS = {"hue": 3, "saturation": 4, "lightness": 5}
LIMITS = {"hue": 30, "saturation": 80, "lightness": 50}
# Native DeviceRGB + HueLow/High signatures; row order has no color meaning.
# Public purple/magenta names explicitly differ from the old bridge's aliases.
IDENTITIES = {
    "magenta": (255, 0, 168.909881591797, -7.11462593078613, 7.11462593078613),
    "purple": (170.93928527832, 0, 255, -17.8928756713867, 17.8928756713867),
    "blue": (0, 64.2907180786133, 255, -22.4273700714111, 22.4273700714111),
    "cyan": (0, 255, 236.14192199707, -11.8545951843262, 11.8545951843262),
    "green": (45.498851776123, 255, 0, -39.3857116699219, 39.3857116699219),
    "yellow": (255, 211.99267578125, 0, -5, 5),
    "orange": (255, 101.301910400391, 0, -6.01692199707031, 6.01692199707031),
    "red": (255, 0, 31.4708557128906, -10.195873260498, 10.195873260498),
    "all": (0, 128, 0, -2, 2),
}
PENDING = {"dispatching", "partial", "outcome_unknown"}


def _float(value):
    try:
        number = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, ValueError, TypeError, struct.error):
        fail("MIXER_VALUE_INVALID", "混色器参数不是有限原生数值。")
    if not math.isfinite(number):
        fail("MIXER_VALUE_INVALID", "混色器参数不是有限原生数值。")
    return number


def parse_basic(raw, engine):
    if str(engine) != "1300" or not isinstance(raw, str) or not raw.isascii() or not 0 < len(raw) <= 32768:
        fail("MIXER_PROFILE_UNQUALIFIED", "仅支持本机已核对的 Engine 1300 Basic 混色器。")
    parts = raw.split(";")
    if parts[-1] == "": parts.pop()
    if len(parts) != 9:
        fail("MIXER_PROFILE_UNQUALIFIED", "需要完整九行 Basic 参数；不补默认值。")
    signatures = {tuple(_float(v) for v in signature): color for color, signature in IDENTITIES.items()}
    matrix, colors, indexes = [], {}, {}
    for index, part in enumerate(parts):
        tokens = part.split(",")
        if len(tokens) != 18 or any(not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", v) for v in tokens):
            fail("MIXER_PROFILE_UNQUALIFIED", "Basic 参数行必须包含 18 个数值。")
        row = [_float(v) for v in tokens]
        color = signatures.get(tuple(row[6:11]))
        if (color is None or color in colors or row[0] != (0 if color == "all" else 1)
                or row[1] not in (0, 1) or row[2] != 1 or row[11:13] != [-100, 100]
                or row[13] != (0 if color == "all" else 15) or any(row[n] != 0 for n in (14, 15, 16, 17))):
            fail("MIXER_PROFILE_UNQUALIFIED", "Basic 原生色区签名、范围或启用结构不匹配。")
        values = {field: row[column] * (10 if field == "lightness" else 1) for field, column in COLUMNS.items()}
        if any(abs(values[field]) > LIMITS[field] for field in COMPONENTS) or color == "all" and row[5] != 0:
            fail("MIXER_PROFILE_UNQUALIFIED", "Basic 参数超出已核对范围。")
        matrix.append(row); colors[color] = values; indexes[color] = index
    if set(colors) != set(IDENTITIES):
        fail("MIXER_PROFILE_UNQUALIFIED", "Basic 色区不完整。")
    return {"mode": "basic", "engine": "1300", "matrix": matrix, "colors": colors, "indexes": indexes}


def observe_mixer(backend, reference=None):
    obs=backend.observe([])
    if reference is not None:_target(reference,obs)
    native=backend.read_model(['basic_mixer','advanced_mixer','skin_mixer'],obs)
    state=mixer_state(native,obs['native_context']['processing_engine'])
    return {**obs,'module':'color_mixer','native_values':native,'state':state,
        'evidence':{**obs['evidence'],'source':'c1_internal_model','persisted_only':False,
            'live_model_verified':True,'native_color_identity_verified':True}}


def mixer_state(values,engine):
    return {**parse_basic(values['basic_mixer'],engine),'advanced':values['advanced_mixer'],'skin':values['skin_mixer']}


def _target(reference,current):
    if reference['target']!=current['target']:fail('MIXER_TARGET_CHANGED','原生混色器目标已经改变。')


def apply_mixer(backend,patch,observation,**_):
    from .native_modules import require_candidate
    require_candidate(backend,observation)
    if patch['mode']!='basic':return apply_range(backend,patch,observation)
    color=patch['color'];rows=[row.split(',') for row in observation['native_values']['basic_mixer'].rstrip(';').split(';')]
    index=observation['state']['indexes'][color]
    for field in COMPONENTS:
        if field in patch:rows[index][COLUMNS[field]]=format(patch[field]/(10 if field=='lightness' else 1),'.12g')
    updated=';'.join(','.join(row) for row in rows)+';'
    receipt=backend.action('color_mixer.set',lambda:backend.write_model({'basic_mixer':updated},observation['native_values'],observation),{'patch':patch})
    state=parse_basic(receipt['values']['basic_mixer'],observation['native_context']['processing_engine'])
    expected=parse_basic(updated,observation['native_context']['processing_engine'])
    if state!=expected:fail('MIXER_READBACK_MISMATCH','原生混色器完整色区回读不一致。')
    for field in COMPONENTS:
        if field in patch and not math.isclose(state['colors'][color][field],patch[field],abs_tol=.0001,rel_tol=0):
            fail('MIXER_READBACK_MISMATCH','原生混色器回读不一致。')
    values={**observation['native_values'],**receipt['values']}
    state=mixer_state(values,observation['native_context']['processing_engine'])
    after={**observation,'state':state,'native_values':values}
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt}}


def reconcile_mixer(backend,record):
    after=observe_mixer(backend)
    _target(record['before'],after)
    return {'outcome':'unknown','target':after['target'],'evidence':{'observation':after,'source':'c1_internal_model','replayed':False}}


def apply_range(backend,patch,before):
    mode=patch['mode'];field=mode+'_mixer';action=patch.get('action','update')
    old=before['native_values'][field]['ranges']
    if action!='add' and patch['index']>=len(old):fail('MIXER_RANGE_MISSING','指定的原生色区不存在。')
    payload={k:v for k,v in patch.items() if k!='mode'};payload['action']=action
    receipt=backend.action('color_mixer.'+mode+'.'+action,lambda:backend.write_model({field:payload},before['native_values'],before),{'patch':patch})
    actual=receipt['values'][field];rows=actual['ranges']
    expected_count=len(old)+(1 if action=='add' else -1 if action=='remove' else 0)
    if len(rows)!=expected_count:fail('MIXER_RANGE_READBACK','原生色区数量不符合请求。')
    index=len(old) if action=='add' else patch['index']
    if action!='remove':
        row=rows[index]
        if 'rgb' in patch and any(not math.isclose(a,b,abs_tol=.001,rel_tol=0) for a,b in zip(row['rgb'],patch['rgb'])):
            fail('MIXER_RANGE_READBACK','原生色区RGB回读不一致。')
        for key,value in patch.items():
            if key in ('mode','action','index','rgb'):continue
            if isinstance(value,bool):valid=row[key] is value
            elif key in ('saturation_low','saturation_high'):valid=_float(row[key])==_float(value)
            else:valid=math.isclose(row[key],value,abs_tol=.0001,rel_tol=0)
            if not valid:fail('MIXER_RANGE_READBACK','原生色区参数回读不一致：'+key)
        if action=='update':
            preserved=('hue','saturation','lightness','smoothness','uniformity_hue','uniformity_saturation','uniformity_lightness','enabled','inverted')
            if 'rgb' not in patch:preserved+=('hue_low','hue_high','saturation_low','saturation_high')
            for key in preserved:
                if key not in patch:
                    a,b=row[key],old[index][key]
                    if (isinstance(b,bool) and a is not b) or (not isinstance(b,bool) and not math.isclose(a,b,abs_tol=.0001,rel_tol=0)):
                        fail('MIXER_RANGE_READBACK','未请求的色区参数发生改变：'+key)
    unchanged_old=[r for i,r in enumerate(old) if not (action in ('update','remove') and i==index)]
    unchanged_new=[r for i,r in enumerate(rows) if not (action in ('update','add') and i==index)]
    if [{k:v for k,v in r.items() if k!='index'} for r in unchanged_old]!=[{k:v for k,v in r.items() if k!='index'} for r in unchanged_new]:
        fail('MIXER_RANGE_READBACK','未请求的原生色区发生改变。')
    if not -1<=actual['selected_index']<len(rows):fail('MIXER_RANGE_READBACK','原生选中色区索引越界。')
    values={**before['native_values'],**receipt['values']};state=mixer_state(values,before['native_context']['processing_engine'])
    return {'status':'control_confirmed','requested_result_verified':True,'after':{**before,'state':state,'native_values':values},
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt,'range_action':action}}
