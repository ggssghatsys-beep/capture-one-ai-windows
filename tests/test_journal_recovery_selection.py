import pytest
from c1ai.journal import Journal
from c1ai.contracts import Refused

@pytest.mark.parametrize('same_layer',[True,False])
def test_windows_recovery_selection_only_returns_to_pending_mask_layer(tmp_path,same_layer):
    journal=Journal(tmp_path)
    layer={'runtime_id':[1,2],'name':'face','is_image_layer':False}
    target={'instance':{'pid':1},'document':{'path':'C:/session.cosessiondb'},
            'image':{'path':'C:/photo.NEF'},'variant':{'id':'one'},'layer':layer}
    before={'target':target,'evidence':{'source':'c1_internal_model','model_verified':True}}
    pending,_=journal.begin({'command':'adjust','module':'masks','patch':{'action':'luma_range'},'request_id':'mask'},target,before)
    journal.update(pending['operation_id'],status='outcome_unknown')
    current=dict(target,layer='Background')
    selected=layer if same_layer else dict(layer,runtime_id=[1,3],name='other')
    request={'command':'adjust','module':'layers','patch':{'action':'select','layer_ref':selected},'request_id':'return'}
    observed={'target':current,'evidence':before['evidence']}
    try:
        if same_layer:
            record,created=journal.begin(request,current,observed)
            assert created and record['status']=='dispatching'
            assert journal.get(operation_id=pending['operation_id'])['status']=='outcome_unknown'
        else:
            with pytest.raises(Refused,match='unfinished structural'):journal.begin(request,current,observed)
    finally:journal.close()
