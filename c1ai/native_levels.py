"""Levels codec, using C1's same model getter/setter as other adjustments."""
import math
from .native_images import fail

CHANNELS=('rgb','red','green','blue')
FIELDS=('input_black','input_white','midtone','output_black','output_white')


def observe_levels(backend):
    obs=backend.observe([])
    values=backend.read_model(['levels_'+c for c in CHANNELS],obs)
    return {**obs,'module':'levels','state':{'channels':{c:values['levels_'+c] for c in CHANNELS}},'native_values':values,
        'evidence':{**obs['evidence'],'source':'c1_internal_model','live_model_verified':True,
            'black_white_units':'0..255','midtone_units':'-1..1'}}


def apply_levels(backend,patch,before):
    from .native_modules import require_candidate
    require_candidate(backend,before)
    field='levels_'+patch['channel']
    wanted={**before['native_values'][field],**{k:v for k,v in patch.items() if k!='channel'}}
    result=backend.action('levels.set',lambda:backend.write_model({field:wanted},before['native_values'],before),{'patch':patch})
    actual=result['values'][field]
    if any(not math.isclose(actual[k],wanted[k],abs_tol=1e-4,rel_tol=0) for k in FIELDS):
        fail('LEVELS_READBACK_MISMATCH','原生色阶回读与请求不一致。')
    values={**before['native_values'],**result['values']}
    after={**before,'native_values':values,'state':{'channels':{c:values['levels_'+c] for c in CHANNELS}}}
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':result}}
