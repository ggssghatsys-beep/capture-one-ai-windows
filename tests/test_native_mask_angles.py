from copy import deepcopy
import pytest
from c1ai import native_modules as m
from c1ai.windows_backend import NativeError

PATCH={'action':'radial_gradient','center':[.3,.4],'radius_x':.2,'radius_y':.1,
       'falloff':.48,'angle':-.02,'inverted':False}

def mask(angle):
    return {'applicable':True,'buffered':False,'raw_dimensions':{'Width':1000,'Height':800},
            'radial':{'center':{'X':300,'Y':320},'radius_x':200,'radius_y':80,
                      'falloff':.48,'angle':angle,'fill':'CenterFromFullToNone'}}

@pytest.mark.parametrize('angle',[-.02,359.98,-360.02])
def test_equivalent_native_angles_match(angle):
    m.verify_mask_result(PATCH,{},mask(angle))

@pytest.mark.parametrize('change',[lambda a:a['radial'].update(angle=.2),
                                  lambda a:a['radial']['center'].update(X=301),
                                  lambda a:a['radial'].update(radius_x=210)])
def test_nonmatching_geometry_is_rejected(change):
    actual=mask(359.98);change(actual)
    with pytest.raises(NativeError):m.verify_mask_result(PATCH,{},actual)

def test_radial_reconcile_reads_receipt_without_replaying(monkeypatch):
    layer={'runtime_id':[1,2],'name':'face','is_image_layer':False}
    target={'instance':{},'document':{},'image':{},'variant':{},'layer':layer}
    before={'target':target,'state':{'layers':[layer],'selected_layer':layer,'mask':mask(0)}}
    after=deepcopy(before);after['state']['mask']=mask(359.98)
    monkeypatch.setattr(m,'observe_module',lambda backend,module:after)
    record={'request':{'module':'masks','patch':PATCH},'before':before,
            'actions':[{'name':'masks.radial_gradient','status':'completed','result':{'accepted':True}}]}
    assert m.reconcile_module(object(),record)['outcome']=='applied_not_restored'
    after['state']['mask']['radial']['center']['X']=301
    assert m.reconcile_module(object(),record)['outcome']=='unknown'

@pytest.mark.parametrize('generated,buffered,expected', [('Subject',False,'applied_not_restored'),
                                                       ('Background',False,'unknown'),
                                                       ('Subject',True,'unknown')])
def test_timed_out_ai_creation_requires_finished_matching_native_mask(monkeypatch,generated,buffered,expected):
    base={'runtime_id':[1,0],'name':'Background','is_image_layer':True}
    local={'runtime_id':[1,1],'name':'Subject 1','is_image_layer':False}
    target={'instance':{},'document':{},'image':{},'variant':{},'layer':'Background'}
    before={'target':target,'state':{'layers':[base],'selected_layer':base}}
    after={'target':dict(target,layer=local),'state':{'layers':[base,local],'selected_layer':local,
            'mask':{'applicable':True,'buffered':buffered}},'native_layers':{
            'layers':[{'selected':True,'generated':generated,'mask':'new-uuid'}]}}
    monkeypatch.setattr(m,'observe_module',lambda backend,module:after)
    record={'request':{'module':'masks','patch':{'action':'create_subject'}},'before':before,
            'actions':[{'name':'masks.create_subject','status':'outcome_unknown',
                        'error':{'code':'UI_TIMEOUT','dispatched':True}}]}
    assert m.reconcile_module(object(),record)['outcome']==expected

def test_luma_timeout_reconciles_only_exact_range_with_preserved_radial(monkeypatch):
    layer={'runtime_id':[1,2],'name':'face','is_image_layer':False}
    target={'instance':{},'document':{},'image':{},'variant':{},'layer':layer}
    before={'target':target,'state':{'layers':[layer],'selected_layer':layer,'mask':mask(0)}}
    after=deepcopy(before)
    after['state']['mask']['luma']={'RangeLo':0,'FalloffLo':0,'RangeHi':150,'FalloffHi':220,'Radius':8,'Sensitivity':50,'Invert':False}
    patch={'action':'luma_range','shadow':0,'shadow_falloff':0,'highlight':150,'highlight_falloff':220,'radius':8,'sensitivity':50,'inverted':False}
    record={'request':{'module':'masks','patch':patch},'before':before,
            'actions':[{'name':'masks.luma_range','status':'outcome_unknown','error':{'code':'UI_TIMEOUT','dispatched':True}}]}
    monkeypatch.setattr(m,'observe_module',lambda backend,module:after)
    assert m.reconcile_module(object(),record)['outcome']=='applied_not_restored'
    after['state']['mask']['submasks']=[{'radial':True,'linear':False,'luma':False,'blend':'bmDraw'},
                                      {'radial':False,'linear':False,'luma':True,'blend':'bmMultiply'}]
    assert m.reconcile_module(object(),record)['outcome']=='applied_not_restored'
    after['state']['mask']['submasks'].append({'radial':True,'linear':False,'luma':False,'blend':'bmDraw'})
    assert m.reconcile_module(object(),record)['outcome']=='unknown'
    after['state']['mask']['submasks']=[]
    after['state']['mask']['radial']['center']['X']=301
    assert m.reconcile_module(object(),record)['outcome']=='unknown'
