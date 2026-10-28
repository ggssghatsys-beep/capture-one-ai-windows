"""Curve points use the common native model endpoint; no canvas input."""
import math
from .native_images import fail
CHANNELS=('rgb','red','green','blue','luma')


def observe_curves(backend,reference=None):
    obs=backend.observe([])
    if reference is not None and obs['target']!=reference['target']:fail('CURVE_TARGET_CHANGED','原生曲线目标已经改变。')
    values=backend.read_model(['curve_'+c for c in CHANNELS],obs)
    return {**obs,'module':'curves','state':{'channels':{c:values['curve_'+c] for c in CHANNELS}},'native_values':values,
        'evidence':{**obs['evidence'],'source':'c1_internal_model','native_numeric_readback':True,'live_model_verified':True}}


def apply_curve(backend,patch,observation,**_):
    from .native_modules import require_candidate
    require_candidate(backend,observation)
    field='curve_'+patch['channel'];points=patch['points']
    receipt=backend.action('curves.set',lambda:backend.write_model({field:points},observation['native_values'],observation),{'patch':patch})
    actual=receipt['values'][field]
    if len(actual)!=len(points) or any(not math.isclose(a,b,abs_tol=.00001,rel_tol=0) for p,q in zip(actual,points) for a,b in zip(p,q)):
        fail('CURVE_READBACK_MISMATCH','原生曲线回读不一致。')
    values={**observation['native_values'],**receipt['values']}
    after={**observation,'native_values':values,'state':{'channels':{c:values['curve_'+c] for c in CHANNELS}}}
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt,'native_numeric_readback':True}}


def reconcile_curve(backend,record):
    after=observe_curves(backend,record['before'])
    return {'outcome':'unknown','target':after['target'],'evidence':{'observation':after,'source':'c1_internal_model','replayed':False}}
