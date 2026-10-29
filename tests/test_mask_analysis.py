import io
import numpy as np
import pytest
from PIL import Image
from c1ai.image_analysis import decode,apply_mask,analyze,scope_image,region_metadata,compare
from c1ai.vectorscope import calculate

def png(data):
    out=io.BytesIO();Image.fromarray(np.array(data,dtype=np.uint8)).save(out,format='PNG');return out.getvalue()

def test_native_mask_weights_exclude_background_and_preserve_feather():
    rgb=decode(png([[[255,0,0],[0,0,255],[0,255,0]]]))
    p=apply_mask(rgb,png([[255,128,0]]),{'width':3},{'width':3})
    a=analyze(p);total=1+128/255
    assert a['pixel_count']==2 and a['effective_pixel_count']==pytest.approx(total)
    assert a['statistics']['red']['mean_percent']==pytest.approx(100/total)
    assert a['statistics']['blue']['mean_percent']==pytest.approx(100*(128/255)/total)
    assert a['histograms']['green'][0]==pytest.approx(total)
    grid,v=calculate(p)
    assert grid.sum()==pytest.approx(total) and v['population']==pytest.approx(total)
    assert v['sample_pixel_count']==2
    assert analyze(p,[0,0,1/3,1])['statistics']['red']['mean_percent']==100
    assert region_metadata(p,[2/3,0,1,1])['pixel_count']==0
    for mode in ('waveform','parade','histogram','vectorscope'):
        assert scope_image(p,mode=mode).width>0
    with pytest.raises(ValueError,match='geometry'):
        apply_mask(rgb,png([[255,128,0]]),{'width':3},{'width':4})

def test_masked_waveform_has_no_false_black_trace_outside_mask():
    p=decode(png(np.full((30,60,3),128)))
    mask=np.zeros((30,60),dtype=np.uint8);mask[:,:30]=255
    p=apply_mask(p,png(mask),{},{});scope=np.asarray(scope_image(p,mode='waveform'))
    # Left half contains a midtone trace; excluded right half has neither midtone nor black pixels.
    assert scope[335:350,200:700].max()>200
    assert scope[335:350,1300:1700].max()<100
    assert scope[610:621,1300:1700].max()<100

def test_same_area_different_mask_refused():
    base={'photo_key':'a','source':'native','policy':{},'analysis_size':[2,1],'color':{},'crop_signature':'x',
          'analysis_scope':'active_layer','sample_mask_sha256':'left'}
    with pytest.raises(ValueError,match='mask'):
        compare(base,{**base,'sample_mask_sha256':'right'})

def test_region_picker_letterbox_coordinates(tmp_path):
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QPointF
    from c1ai.scopes_roi import RegionCanvas
    app=QApplication.instance() or QApplication([])
    path=tmp_path/'portrait.png';path.write_bytes(png(np.full((400,200,3),100)))
    canvas=RegionCanvas(str(path),[0,0,1,1]);canvas.resize(600,400)
    rect=canvas.image_rect()
    assert rect.left()==200 and rect.width()==200
    assert canvas.point(QPointF(250,100))==[.25,.25]
    assert canvas.point(QPointF(400,400))==[1.,1.]
    canvas.close()


def test_empty_mask_never_falls_back_or_reports_stale_snapshot_fresh(tmp_path):
    import hashlib
    from c1ai.analysis_service import AnalysisService
    class Backend:
        revision='1'
        def observe(self,fields):
            target={'image':{'path':'test.raw'},'variant':{'id':'v','label':'test'},'layer':{'native_id':'m'}}
            return {'target':target,'native_context':{'process_revision':self.revision,'target':{'document_uuid':'d'},
                'image_path':'test.raw','layers':[{'name':'empty','native_id':'m','selected':True}]}}
        def preview(self,request,observation):
            mask=request['kind']=='mask'
            data=png([[0,0]]) if mask else bytes([30,60,90,255]*2)
            path=tmp_path/('mask.png' if mask else 'photo.bgra');path.write_bytes(data)
            image={'width':2,'height':1,'stride':8,'format':'BGRA8','row_order':'top-down','bit_depth':8,
                   'sha256':hashlib.sha256(data).hexdigest()}
            return {'status':'completed','revision':self.revision,'path':str(path),'image':image,'preview_id':'p','geometry':{}}
    backend=Backend();service=AnalysisService(backend,tmp_path/'state')
    req={'analysis_scope':'active_layer','include_image':'vectorscope'}
    r=service.dispatch(req,lambda a,b:None);aid=r['analysis']['analysis_id']
    assert r['freshness']['state']=='empty' and r['analysis']['region']['pixel_count']==0
    assert 'artifact' not in r and 'vectorscope' not in r['analysis']
    with pytest.raises(ValueError,match='empty'):
        service.dispatch({'action':'reference','analysis_id':aid},lambda a,b:None)
    preview=service.dispatch({'action':'get','analysis_id':aid,'include_image':'preview','include_statistics':False},lambda a,b:None)
    assert preview['freshness']['state']=='empty' and preview['artifact']['kind']=='preview'
    cached=service.dispatch({**req,'include_image':'preview','known_analysis_id':aid,'include_statistics':False},lambda a,b:None)
    assert cached['freshness']['state']=='empty'
    backend.revision='2'
    stale=service.dispatch({'action':'get','analysis_id':aid,'include_image':'preview'},lambda a,b:None)
    assert stale['freshness']['state']=='stale'
