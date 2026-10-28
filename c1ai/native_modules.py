"""Structured tools sharing the owner's target guards and action journal.

Saved settings are read-only diagnostic evidence, never an alternative writer.
"""
import copy
import json
import time
import hashlib
import math
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

TOOLS={'curves':'CurvesTool','levels':'LevelsTool','color_mixer':'SelectiveColorTool',
       'color_balance':'ColorBalanceTool','layers':'LocalAdjustmentsTool',
       'masks':'LocalAdjustmentsTool'}

CURVE_KEYS={'rgb':'ZGRADATIONCURVE','red':'ZGRADATIONCURVERED','green':'ZGRADATIONCURVEGREEN',
            'blue':'ZGRADATIONCURVEBLUE','luma':'ZGRADATIONCURVEY'}

def read_opacity(backend,obs):
    if obs['target']['layer']=='Background':
        return None,{'applicable':False,'reason':'image_layer_has_no_adjustment_opacity'}
    native=backend.read_native_parameters(['layer_opacity'],obs)
    return native['values']['layer_opacity'],{
        **native['parameters']['layer_opacity'],'applicable':True,'source':'c1_internal_model'}

def layer_reference(row):
    return {key:copy.deepcopy(row[key]) for key in ('runtime_id','name','is_image_layer','native_id','object_id') if key in row}

def saved_settings(target):
    """One read-only SQLite snapshot of C1's own persisted combined settings."""
    from .windows_backend import NativeError
    with closing(sqlite3.connect(Path(target['document']['path']).as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        rows=list(db.execute('SELECT v.ZVARIANTUUID,v.ZPROCESSINGENGINE,l.* FROM ZVARIANT v '
             'JOIN ZVARIANTLAYER l ON l.Z_PK=v.ZCOMBINEDSETTINGS WHERE lower(v.ZVARIANTUUID)=?',
             (target['variant']['id'].lower(),)))
        if len(rows)!=1:raise NativeError('SAVED_SETTINGS_UNAVAILABLE','Saved combined settings must uniquely match selected UUID')
        row=dict(rows[0]);db.rollback()
    return row

def mask_snapshot(target):
    """Persisted mask references, explicitly not proof of segmentation quality."""
    source=Path(target['image']['path']);paths=list((source.parent/'CaptureOne').glob('Settings*/'+source.name+'.cos'))
    if len(paths)!=1:return {'available':False}
    path=paths[0];before=path.stat();raw=path.read_bytes();after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return {'available':False}
    variants=[v for v in ET.fromstring(raw).findall('VAR') if any(e.get('K')=='UUID' and
              e.get('V','').lower()==target['variant']['id'].lower() for e in v.findall('E'))]
    if len(variants)!=1:return {'available':False}
    # RLS/RL contains inherited portrait-retouch masks, not proof that a new
    # subject/background adjustment mask was generated. Keep sections explicit.
    masks=[];retouch=[]
    for group in variants[0]:
        if group.tag in ('E','DL','AL'):continue
        for node in ([group] if group.findall('E') else group):
            values={e.get('K'):e.get('V') for e in node.findall('E')}
            entry={'section':group.tag,'tag':node.tag,'entries':{k:v for k,v in values.items() if any(x in k.lower() for x in ('name','mask','uuid','opacity','enable'))}}
            (retouch if group.tag=='RLS' else masks).append(entry)
    mask_path=path.with_suffix('.comask')
    # Capture One sidecars are image-name.comask, not image-name.cos.comask.
    receipt={'available':True,'saved_layers':masks,'retouch_mask_references':retouch,
             'source':'persisted_sidecar','mask_file_exists':mask_path.is_file(),
             'new_adjustment_mask_verified':False}
    if mask_path.is_file():
        first=mask_path.stat();data=mask_path.read_bytes();last=mask_path.stat()
        if (first.st_size,first.st_mtime_ns)!=(last.st_size,last.st_mtime_ns):return {'available':False,'reason':'mask_file_changing'}
        receipt.update(mask_file_bytes=len(data),mask_file_sha256=hashlib.sha256(data).hexdigest())
    return receipt

def observe_module(backend,module):
    from .windows_backend import NativeError
    from .native_images import checked_call
    if module not in TOOLS:raise NativeError('UNSUPPORTED_MODULE',module)
    if module=='levels':
        from .native_levels import observe_levels
        return observe_levels(backend)
    if module=='curves':
        from .native_curves import observe_curves
        return observe_curves(backend)
    if module=='color_mixer':
        from .native_mixer import observe_mixer
        return observe_mixer(backend)
    obs=backend.observe([],allow_layer=True)
    if module in ('layers','masks'):
        native=obs.get('native_layers') or checked_call(backend,obs,'layers')
        if len(native['selected'])!=1:raise NativeError('SINGLE_LAYER_REQUIRED','One selected layer required')
        state={'layers':[layer_reference(n) for n in native['layers']],
               'selected_layer':layer_reference(native['selected'][0])}
        state['opacity'],obs['opacity_control']=read_opacity(backend,obs)
        selected=state['selected_layer']
        expected_layer='Background' if selected['is_image_layer'] else selected
        if obs['target']['layer']!=expected_layer:
            raise NativeError('TARGET_CHANGED','Layer inventory and selected target disagree')
        state['details']=[{k:n[k] for k in ('native_id','opacity','enabled') if k in n} for n in native['layers']]
        if module=='masks':
            state['native_masks']=[{'layer_id':n['native_id'],'generated':n.get('generated')} for n in native['layers'] if not n['is_image_layer']]
            state['mask']=backend.read_model(['mask_state'],obs)['mask_state']
        obs['native_layers']=native
        evidence={'source':'c1_internal_model','atomic':False,'layer_identity_native_readback':True,
                  'mask_pixels_verified':False,'opacity_source':obs['opacity_control'].get('source'),
                  'opacity_applicable':obs['opacity_control']['applicable']}
    else:
        native=backend.read_model(['wheel_'+w for w in ('master','shadows','midtones','highlights')],obs)
        state={'values':{prefix+'_'+axis:native['wheel_'+wheel][axis] for wheel,prefix in
            (('master','master'),('shadows','shadow'),('midtones','midtone'),('highlights','highlight'))
            for axis in (('x','y') if wheel=='master' else ('x','y','lightness'))}}
        obs['native_values']=native
        evidence={'source':'c1_internal_model','persisted_only':False,'live_model_verified':True,'model_verified':True}
    return {**obs,'module':module,'state':state,'evidence':{**obs['evidence'],**evidence}}

def require_candidate(backend,observation):
    from .windows_backend import NativeError,process_started
    from .contracts import MODULES
    capabilities=backend.capabilities_for(observation);module=observation['module']
    qualified=all(capabilities['fields'][f]['writable'] for f in MODULES[module]) if module in MODULES else capabilities['modules'][module]['writable']
    if not qualified:
        raise NativeError('MODULE_UNQUALIFIED','Structured writes require a qualified candidate and explicit module gate')
    if process_started(observation['target']['instance']['pid'])!=observation['target']['instance']['started_at']:
        raise NativeError('TARGET_CHANGED','Capture One instance changed')

def transition(before,after):
    return {'before':before['target'],'after':after['target'],'evidence':{
        'before_layers':before['state']['layers'],'after_layers':after['state']['layers'],
        'selected_layer':after['state']['selected_layer']}}

def adjust_module(backend,module,patch,observation):
    from .windows_backend import NativeError
    from .native_images import checked_call
    require_candidate(backend,observation)
    if module=='levels':
        from .native_levels import apply_levels
        return apply_levels(backend,patch,observation)
    if module=='curves':
        from .native_curves import apply_curve
        return apply_curve(backend,patch,observation)
    if module=='color_mixer':
        from .native_mixer import apply_mixer
        return apply_mixer(backend,patch,observation)
    if module=='color_balance':return adjust_balance(backend,patch,observation)
    if module=='layers' and patch['action']=='set_opacity':return adjust_opacity(backend,patch,observation)
    if module not in ('layers','masks'):
        raise NativeError('MODULE_ROUTE_UNAVAILABLE','This structured native adapter is not ready')
    action=patch['action']
    args={'command':'layer','action':action,'patch':patch}
    if module=='masks' and not action.startswith('create_'):args['before_mask']=observation['state']['mask']
    if 'layer_ref' in patch:
        ref=patch['layer_ref'];args['layer_id']=ref['native_id']
    receipt=backend.action(module+'.'+action,lambda:backend._model_call(args,observation),{'patch':patch})
    deadline=time.monotonic()+60
    while True:
        after=observe_module(backend,module)
        if not action.startswith('create_'):
            if module=='masks' and after['state']['mask'].get('buffered'):
                if time.monotonic()>=deadline:raise NativeError('MASK_PENDING','Native gradient is still being processed')
                time.sleep(.1);continue
            break
        rows=after['native_layers']['layers'];selected=next(n for n in rows if n['selected'])
        expected={'create_subject':'Subject','create_background':'Background'}.get(action)
        if len(rows)==len(observation['native_layers']['layers'])+1 and not selected['is_image_layer'] and (expected is None or selected.get('generated')==expected):break
        if time.monotonic()>=deadline:raise NativeError('LAYER_RESULT_PENDING','C1 has not finished the requested native layer')
        time.sleep(.15)
    old=observation['state']['layers'];new=after['state']['layers']
    if action.startswith('create_') and len(new)!=len(old)+1:raise NativeError('LAYER_RESULT_PENDING','C1 has not produced the requested native layer yet')
    if action=='set_enabled':
        actual=next(n for n in after['native_layers']['layers'] if n['native_id']==patch['layer_ref']['native_id'])
        if actual.get('enabled')!=patch['enabled']:raise NativeError('LAYER_READBACK_MISMATCH','Native layer enabled state differs')
    if module=='masks' and not action.startswith('create_'):
        verify_mask_result(patch,observation['state']['mask'],after['state']['mask'])
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,'target_transition':transition(observation,after),
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt,'mask_pixels_verified':False}}


def verify_mask_result(patch,before,after):
    from .windows_backend import NativeError
    action=patch['action'];ok=after.get('applicable') is True and not after.get('buffered')
    close=lambda a,b: isinstance(a,(int,float)) and math.isclose(a,b,abs_tol=.01,rel_tol=1e-6)
    if action in ('clear','fill'):ok=ok and after.get('content_revision')!=before.get('content_revision')
    elif action=='select_submask':ok=ok and after.get('selected_submask')==patch['index']
    elif action=='combine':
        expected={'add':'bmDraw','subtract':'bmErase','intersect':'bmMultiply'}[patch['mode']]
        new=after.get('submasks',[]);old=before.get('submasks',[])
        ok=ok and len(new)>len(old) and new[-1]['blend']==expected
    elif action=='luma_range':
        mapping={'shadow':'RangeLo','highlight':'RangeHi','shadow_falloff':'FalloffLo','highlight_falloff':'FalloffHi','radius':'Radius','sensitivity':'Sensitivity'}
        luma=after.get('luma') or {};ok=ok and all(close(luma.get(v),patch[k]) for k,v in mapping.items()) and luma.get('Invert')==patch['inverted']
    elif action=='linear_gradient':
        linear=after.get('linear') or {};size=after.get('raw_dimensions',{})
        ok=ok and close(linear.get('midpoint'),patch['midpoint'])
        for point in ('start','stop'):
            value=linear.get(point,{})
            ok=ok and all(close(value.get(axis),patch[point][i]*size.get(dimension,0)) for i,axis,dimension in ((0,'x','Width'),(1,'y','Height')))
    elif action=='radial_gradient':
        radial=after.get('radial') or {};size=after.get('raw_dimensions',{})
        ok=ok and close(radial.get('radius_x'),patch['radius_x']*size.get('Width',0)) and close(radial.get('radius_y'),patch['radius_y']*size.get('Height',0)) and close(radial.get('falloff'),patch['falloff'])
        ok=ok and close(radial.get('angle'),patch['angle']) and radial.get('fill')==('CenterFromNoneToFull' if patch['inverted'] else 'CenterFromFullToNone')
        ok=ok and all(close((radial.get('center') or {}).get(axis),patch['center'][i]*size.get(dimension,0)) for i,axis,dimension in ((0,'X','Width'),(1,'Y','Height')))
    elif action in ('invert','copy','feather'):
        ok=ok and (after.get('content_revision')!=before.get('content_revision') or action=='feather' and patch['radius']==0)
    if not ok:raise NativeError('MASK_READBACK_MISMATCH','Native mask result differs from requested action',{'action':action,'before':before,'after':after})


MODULE_FIELDS={
    'exposure':('exposure','contrast','brightness','saturation'), 'white_balance':('temperature','tint'),
    'hdr':('highlight_recovery','shadow_recovery','white_recovery','black_recovery'),
    'curves':tuple('curve_'+c for c in ('rgb','red','green','blue','luma')),
    'levels':tuple('levels_'+c for c in ('rgb','red','green','blue')),
    'color_mixer':('basic_mixer','advanced_mixer','skin_mixer'),
    'color_balance':tuple('wheel_'+w for w in ('master','shadows','midtones','highlights'))}


def apply_module_action(backend,module,patch,before,source):
    from .windows_backend import NativeError
    from .contracts import MODULES
    require_candidate(backend,{**before,'module':module})
    fields=MODULE_FIELDS[module];values=before.get('native_values',before['values'])
    if not set(fields)<=values.keys():raise NativeError('INCOMPLETE_MODULE','Read the complete destination module first')
    args={'command':'module','module':module,'action':patch['action'],'before':{k:values[k] for k in fields}}
    if source is not None:
        source_values=source.get('native_values',source['values'])
        if source.get('module',module)!=module or not set(fields)<=source_values.keys():raise NativeError('WRONG_SOURCE','Read the complete source module before pasting')
        if source['native_context']['processing_engine']!=before['native_context']['processing_engine']:raise NativeError('ENGINE_MISMATCH','Source and destination processing engines differ')
        args['values']={k:source_values[k] for k in fields}
    receipt=backend.action(module+'.'+patch['action'],lambda:backend._model_call(args,before),{'patch':patch})
    # The native call already read the complete result on C1's UI thread.
    # Decode that receipt instead of reading the same module a second time.
    native=receipt['values']
    if receipt['layer_uuid']!=before['native_context']['layer_uuid']:raise NativeError('TARGET_CHANGED','Module action changed the active layer')
    if module in MODULES:after={**before,'values':native}
    else:
        if module in ('curves','levels'):
            prefix='curve_' if module=='curves' else 'levels_';state={'channels':{k[len(prefix):]:v for k,v in native.items()}}
        elif module=='color_mixer':
            from .native_mixer import mixer_state
            state=mixer_state(native,before['native_context']['processing_engine'])
        else:
            state={'values':{prefix+'_'+axis:native['wheel_'+wheel][axis] for wheel,prefix in
                (('master','master'),('shadows','shadow'),('midtones','midtone'),('highlights','highlight'))
                for axis in (('x','y') if wheel=='master' else ('x','y','lightness'))}}
        after={**before,'native_values':native,'state':state,'values':{module:state}}
    if source is not None:
        def matches(a,b):
            if isinstance(a,dict) and isinstance(b,dict):return all(k in a and matches(a[k],v) for k,v in b.items() if k!='selected_index')
            if isinstance(a,list) and isinstance(b,list):return len(a)==len(b) and all(matches(x,y) for x,y in zip(a,b))
            if type(a) in (int,float) and type(b) in (int,float):return math.isclose(a,b,abs_tol=.001,rel_tol=1e-6)
            return a==b
        if not matches(after.get('native_values',after['values']),args['values']):raise NativeError('PASTE_READBACK_MISMATCH','Native module paste readback differs')
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt,'reset_to_c1_defaults':patch['action']=='reset','render_verified':False}}


def adjust_opacity(backend,patch,before):
    from .windows_backend import NativeError
    if before['state']['selected_layer']!=patch['layer_ref'] or patch['layer_ref']['is_image_layer']:
        raise NativeError('ADJUSTMENT_LAYER_REQUIRED','Select the exact adjustment layer before setting opacity')
    value,node=before['state']['opacity'],before['opacity_control']
    if node.get('applicable') is not True or node.get('available') is not True:
        raise NativeError('STALE_LAYER_OPACITY','Native opacity is unavailable for the selected layer')
    result=backend.action('layers.set_opacity',lambda:backend.write_native_parameter(
        'layer_opacity',value,patch['opacity'],before),
        {'patch':patch,'before_opacity':value,'target':before['target']})
    state={**before['state'],'opacity':result['after']['value']}
    after={**before,'state':state,'values':{'layers':state},
           'opacity_control':{**result['after'],'applicable':True,'source':'c1_internal_model'},
           'evidence':{**before['evidence'],'opacity_source':'c1_internal_model','atomic':False,
                       'layer_inventory_reused':True,'layer_inventory_unchanged_verified':False}}
    context=copy.deepcopy(before['native_context'])
    for row in context['layers']:
        if row['selected'] and not row['is_image_layer']:row['opacity']=result['after']['value']
    after['native_context']=context
    after['native_layers']={'layers':context['layers'],'selected':[row for row in context['layers'] if row['selected']]}
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','native_result':result,
                    'target_guard_verified':True,'native_parameter_readback_verified':True,
                    'model_verified':True,'render_verified':False}}

def native_instance(observation):
    return {key:observation['target']['instance'][key] for key in ('pid','started_at')}

def balance_targets(patch):
    prefix={'master':'master','shadows':'shadow','midtones':'midtone','highlights':'highlight'}[patch['wheel']]
    return {prefix+'_'+key:value for key,value in patch.items() if key!='wheel'}

def adjust_balance(backend,patch,before):
    from .windows_backend import NativeError
    field='wheel_'+patch['wheel'];wanted={**before['native_values'][field],**{k:v for k,v in patch.items() if k!='wheel'}}
    receipt=backend.action('color_balance.set',lambda:backend.write_model({field:wanted},before['native_values'],before),{'patch':patch})
    actual=receipt['values'][field]
    if any(not math.isclose(actual[k],v,abs_tol=.0001,rel_tol=0) for k,v in wanted.items()):
        raise NativeError('NATIVE_COLOR_MISMATCH','Native wheel readback differs from requested position',receipt)
    values={**before['native_values'],**receipt['values']}
    prefix={'master':'master','shadows':'shadow','midtones':'midtone','highlights':'highlight'}[patch['wheel']]
    state={'values':{**before['state']['values'],**{prefix+'_'+axis:actual[axis] for axis in actual if not (prefix=='master' and axis=='lightness')}}}
    after={**before,'native_values':values,'state':state}
    return {'status':'control_confirmed','requested_result_verified':True,'after':after,
        'evidence':{'source':'c1_internal_model','model_verified':True,'native_receipt':receipt}}


def recover_module(backend,record):
    """Recovery is read-only; uncertain edits are never replayed automatically."""
    return reconcile_module(backend,record)


def reconcile_module(backend,record):
    """Read a known operation's result; never repeat a native command."""
    module=record['request']['module'];before=record['before'];after=observe_module(backend,module)
    evidence={'observation':after,'source':'c1_internal_model','replayed':False}
    response={'outcome':'unknown','target':after['target'],'evidence':evidence}
    if any(before['target'][k]!=after['target'][k] for k in ('instance','document','image','variant')):return response
    actions=record.get('actions',[]);patch=record['request']['patch'];action=patch.get('action')
    if module in ('layers','masks') and action in ('create_empty','create_subject','create_background'):
        old=before['state']['layers'];new=after['state']['layers'];selected=after['state']['selected_layer']
        from collections import Counter
        old_names=Counter(n['name'] for n in old);new_names=Counter(n['name'] for n in new)
        receipts=[a for a in actions if a['name']==module+'.'+action and a['status']=='completed' and a.get('result',{}).get('accepted') is True]
        native=next((n for n in after['native_layers']['layers'] if n['selected']),{})
        expected={'create_subject':'Subject','create_background':'Background'}.get(action)
        if (len(receipts)==1 and len(new)==len(old)+1 and not (old_names-new_names)
            and selected['name'] not in old_names and not selected['is_image_layer'] and native.get('mask')
            and (expected is None or native.get('generated')==expected)):
            response.update(outcome='applied_not_restored',target_transition=transition(before,after))
        return response
    if after['target']==before['target'] and after['state']==before['state'] and all(a['status']=='not_dispatched' for a in actions):
        response['outcome']='not_applied'
    return response


def prepare_tool(backend,module,observation):
    from .windows_backend import NativeError
    from .native_images import checked_call
    if module not in TOOLS: raise NativeError('UNSUPPORTED_TOOL',module)
    selector={'automation_id':'HeaderSite','control_type':'Button',
              'ancestors':[{'automation_id':'expander','class_name':'Expander'},
                           {'class_name':TOOLS[module],'control_type':'Custom'}]}
    node=checked_call(backend,observation,'read',selector=selector)['node']
    if node.get('toggle') not in ('On','Off'):
        raise NativeError('TOOL_STATE_UNAVAILABLE','Tool header needs a readable TogglePattern')
    changed=node['toggle']=='Off'
    if changed:
        guard=backend.guard(observation)
        guard['bindings']['tool_header']=selector
        guard['expected']['tool_header']={'toggle':'Off'}
        backend.action('tools.expand.'+module,lambda:checked_call(backend,observation,'set',
            selector=selector,mode='toggle',value=True,guard=guard,post_guard=backend.guard(observation)),
            {'module':module,'before_toggle':'Off'})
    after=checked_call(backend,observation,'read',selector=selector)['node']
    if after.get('toggle')!='On':raise NativeError('TOOL_NOT_EXPANDED','Expanded header did not read back')
    backend.action('tools.reveal.'+module,lambda:checked_call(backend,observation,'reveal_tool',
        selector={'class_name':TOOLS[module],'control_type':'Custom'},guard=backend.guard(observation)),
        {'module':module,'image_modified':False})
    return {'status':'completed','tool':module,'changed':changed,'expanded_verified':True,
            'target':observation['target'],'image_parameters_unchanged_verified':False}
