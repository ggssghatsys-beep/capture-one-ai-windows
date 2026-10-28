"""Synthetic native receipts; never opens Capture One."""
from copy import deepcopy
import json
import pytest
from c1ai import native_modules as m
from c1ai.windows_backend import NativeError

LAYER={'runtime_id':[7,1,2],'name':'test','is_image_layer':False}
BASE={'runtime_id':[7,1,1],'name':'Image Layer','is_image_layer':True}
WINDOW={'pid':1,'hwnd':2,'title':'session','version':'16.8.6.3784'}

class Backend:
    def __init__(self):
        self.value=100;self.calls=[];self.after_mutator=None
        self.observation={'module':'layers','target':{'layer':deepcopy(LAYER)},
            'raw_state':{'window':WINDOW},'state':{'layers':[BASE,LAYER],
                'selected_layer':LAYER,'opacity':100}}
    def call(self,op,**args):
        self.calls.append((op,args))
        if op=='read':return {'window':WINDOW,'node':{'value':str(self.value),'patterns':['Value'],'enabled':True}}
        if op=='set':
            assert args['guard']['expected']['layer_opacity']=={'value':str(self.value)}
            self.value=args['value'];return {'window':WINDOW,'verified':True}
        raise AssertionError(op)
    def guard(self,obs):return {'bindings':{},'expected':{}}
    def action(self,name,func,intent):
        self.calls.append(('journal',name));return func()
    def after(self):
        obs=deepcopy(self.observation);obs['state']['opacity']=self.value
        if self.after_mutator:self.after_mutator(obs)
        return obs

def test_opacity_keeps_exact_layer_and_inventory(monkeypatch):
    b=Backend();monkeypatch.setattr(m,'observe_module',lambda backend,module:backend.after())
    result=m.adjust_opacity(b,{'action':'set_opacity','layer_ref':LAYER,'opacity':65},b.observation)
    assert result['requested_result_verified'] is True
    assert result['after']['state']['opacity']==65
    assert [c[0] for c in b.calls]==['read','journal','set']
    assert 'target_transition' not in result

@pytest.mark.parametrize('change',[
    lambda o:o['state']['layers'].append({'runtime_id':[7,1,3],'name':'other','is_image_layer':False}),
    lambda o:o['state'].update(selected_layer=BASE),
    lambda o:o['target'].update(layer='Background'),
])
def test_unrelated_layer_change_cannot_claim_opacity_success(monkeypatch,change):
    b=Backend();b.after_mutator=change
    monkeypatch.setattr(m,'observe_module',lambda backend,module:backend.after())
    with pytest.raises(NativeError,match='differs'):
        m.adjust_opacity(b,{'layer_ref':LAYER,'opacity':65},b.observation)

def test_original_image_layer_never_receives_opacity_write():
    b=Backend()
    with pytest.raises(NativeError):m.adjust_opacity(b,{'layer_ref':BASE,'opacity':65},b.observation)
    assert b.calls==[]

def test_stale_opacity_has_no_write():
    b=Backend();b.value=80
    with pytest.raises(NativeError):m.adjust_opacity(b,{'layer_ref':LAYER,'opacity':65},b.observation)
    assert all(c[0]=='read' for c in b.calls)

def test_retouch_mask_does_not_certify_new_subject_mask(tmp_path):
    source=tmp_path/'photo.NEF';source.write_bytes(b'raw')
    settings=tmp_path/'CaptureOne'/'Settings1680';settings.mkdir(parents=True)
    (settings/'photo.NEF.cos').write_text('<IMG><VAR><E K="UUID" V="ABC"/><RLS><RL><E K="FaceSculptingMaskUUID" V="OLD"/></RL></RLS></VAR></IMG>')
    (settings/'photo.NEF.comask').write_bytes(b'existing retouch pixels')
    result=m.mask_snapshot({'image':{'path':str(source)},'variant':{'id':'abc'}})
    assert result['saved_layers']==[]
    assert len(result['retouch_mask_references'])==1
    assert result['new_adjustment_mask_verified'] is False
    assert result['mask_file_exists'] is True

@pytest.mark.parametrize('status,expected',[('completed','applied_not_restored'),('outcome_unknown','unknown')])
def test_opacity_reconcile_needs_original_native_receipt(monkeypatch,status,expected):
    b=Backend();b.value=65
    for key in ('instance','document','image','variant'):b.observation['target'][key]={'id':key}
    monkeypatch.setattr(m,'observe_module',lambda backend,module:backend.after())
    record={'request':{'module':'layers','patch':{'action':'set_opacity','layer_ref':LAYER,'opacity':65}},
        'before':deepcopy(b.observation),'actions':[{'name':'layers.set_opacity','status':status,
        'result':{'accepted':True,'verified':True,'after':{'value':'65'}}}]}
    result=m.reconcile_module(b,record)
    assert result['outcome']==expected
    assert b.calls==[]
