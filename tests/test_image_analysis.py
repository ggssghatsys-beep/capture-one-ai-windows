"""Independent known-pixel expectations, not screenshots of the editor."""
import io
import importlib.util
import numpy as np
import pytest
from PIL import Image, ImageCms

from c1ai.image_analysis import decode, analyze, compare, scope_image, POLICY
from c1ai.analysis_service import AnalysisService


def image(array,icc=None):
    out=io.BytesIO();Image.fromarray(array).save(out,format='PNG',icc_profile=icc)
    return out.getvalue()


def test_gray_ramp_values():
    ramp=np.tile(np.arange(256,dtype=np.uint8)[None,:,None],(4,1,3))
    p=decode(image(ramp));r=analyze(p)
    assert r['pixel_count']==1024
    assert r['statistics']['luma']['mean_percent']==pytest.approx(50)
    assert r['statistics']['luma']['percentiles_percent']['P50']==pytest.approx(50)
    assert r['statistics']['red']['near_white_percent']==pytest.approx(3/256*100)
    assert r['histograms']['red']==[4]*256
    assert r['horizontal_distribution'][0]['channels']['red']['mean_percent'] < 7
    assert r['horizontal_distribution'][-1]['channels']['red']['mean_percent'] > 93


def test_color_blocks_and_linear_definition():
    p=decode(image(np.array([[[255,0,0],[0,255,0],[0,0,255],[128,128,128]]],dtype=np.uint8)))
    assert analyze(p,[0,0,.25,1])['statistics']['luma']['mean_percent']==pytest.approx(21.26)
    grey=analyze(p,[.75,0,1,1])['statistics']
    assert grey['luma']['mean_percent']==pytest.approx(128/255*100)
    assert grey['linear_luminance']['mean_percent']==pytest.approx(21.58605,abs=.0001)


def test_roi_excludes_bright_half():
    a=np.zeros((20,40,3),dtype=np.uint8);a[:,20:]=255
    p=decode(image(a));whole=analyze(p);left=analyze(p,[0,0,.5,1]);right=analyze(p,[.5,0,1,1])
    assert whole['statistics']['luma']['mean_percent']==pytest.approx(50)
    assert left['statistics']['luma']['near_black_percent']==100
    assert right['statistics']['luma']['near_white_percent']==100
    assert left['roi_pixels']==[0,0,20,20]
    assert left['horizontal_distribution'][-1]['roi_x_normalized'][1]==1
    assert right['horizontal_distribution'][0]['image_x_normalized'][0]==.5


def test_icc_and_fixed_size():
    a=np.full((200,2000,3),128,dtype=np.uint8)
    icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    p=decode(image(a,icc))
    assert p.rgb.shape==(160,1600,3)
    assert p.metadata['color']['confirmation']=='embedded_icc_converted'
    assert decode(image(a)).metadata['color']['confirmation']=='assumed_sRGB_no_ICC'
    with pytest.raises(ValueError):analyze(p,[.8,0,.2,1])


def test_reference_contract_and_delta(tmp_path):
    file=tmp_path/'test.png';file.write_bytes(image(np.full((8,8,3),64,dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state')
    request={'source':'file','path':str(file),'action':'refresh'}
    old=service.dispatch(request,lambda a,b:None)['analysis']
    service.dispatch({'action':'reference','analysis_id':old['analysis_id']},lambda a,b:None)
    file.write_bytes(image(np.full((8,8,3),192,dtype=np.uint8)))
    r=service.dispatch({**request,'reference_id':old['analysis_id']},lambda a,b:None)
    assert r['difference']['statistics']['luma']['mean_percent']==pytest.approx(128/255*100)
    invalid={**r['analysis'],'photo_key':'another photo'}
    with pytest.raises(ValueError,match='photo_key'):compare(invalid,old)
    assert service.dispatch({'action':'get','analysis_id':old['analysis_id']},lambda a,b:None)['freshness']['state']=='stale'


def test_reference_render_basis_tracks_source_and_profiles_not_job_versions():
    pixels=decode(image(np.full((4,4,3),128,dtype=np.uint8)))
    base={'photo_key':'photo','source':'capture_one_native_render','policy':{},'analysis_size':[4,4],
          'color':{},'crop_signature':'crop','region':analyze(pixels),'analysis_id':'old'}
    # Old exact snapshots remain comparable with the explicitly named native job.
    assert compare(base,{**base,'render_basis':{'sample_source':'native_job'}})['statistics']['red']['mean_percent']==0
    stamp={'proof_profile':'proof-a','display_profile':'display-a','job_change_version':1,'renderer_change_version':2}
    viewer={**base,'sample_source':'viewer_cache','viewer_stamp':stamp}
    assert compare(viewer,{**viewer,'viewer_stamp':{**stamp,'job_change_version':3,'renderer_change_version':4}})['statistics']['red']['mean_percent']==0
    for changed in (base,
                    {**viewer,'viewer_stamp':{**stamp,'proof_profile':'proof-b'}},
                    {**viewer,'render_basis':{'sample_source':'viewer_cache','proof_profile':'proof-a','display_profile':'display-b'}},
                    {**viewer,'viewer_stamp':{}}):
        with pytest.raises(ValueError,match='render_basis'):compare(viewer,changed)


def test_late_result_not_published(tmp_path,monkeypatch):
    file=tmp_path/'test.png';data=image(np.zeros((8,8,3),dtype=np.uint8));file.write_bytes(data)
    service=AnalysisService(None,tmp_path/'state')
    from c1ai.analysis_sources import FileSource
    original=FileSource.acquire
    def changed(self,stamp):
        response=original(self,stamp)
        file.write_bytes(image(np.full((8,8,3),255,dtype=np.uint8)))
        return response
    monkeypatch.setattr(FileSource,'acquire',changed)
    r=service.dispatch({'source':'file','path':str(file)},lambda a,b:None)
    assert r['freshness']['state']=='waiting'
    assert not service.records


def test_source_unavailable_keeps_old_result_and_recovers(tmp_path):
    file=tmp_path/'test.png';data=image(np.full((4,4,3),100,dtype=np.uint8));file.write_bytes(data)
    service=AnalysisService(None,tmp_path/'state');request={'source':'file','path':str(file)}
    first=service.dispatch(request,lambda a,b:None)
    file.unlink()
    lost=service.dispatch(request,lambda a,b:None)
    assert lost['freshness']['state']=='stale'
    assert lost['analysis']['analysis_id']==first['analysis']['analysis_id']
    file.write_bytes(data)
    recovered=service.dispatch(request,lambda a,b:None)
    assert recovered['freshness']['state']=='ready'
    assert recovered['analysis']['pixel_sha256']==first['analysis']['pixel_sha256']


def test_saved_roi_is_not_silently_reinterpreted(tmp_path):
    file=tmp_path/'test.png';file.write_bytes(image(np.zeros((4,4,3),dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state')
    first=service.dispatch({'source':'file','path':str(file)},lambda a,b:None)
    with pytest.raises(ValueError,match='fixed ROI'):
        service.dispatch({'action':'get','analysis_id':first['analysis']['analysis_id'],'roi':[0,0,.5,1]},lambda a,b:None)


def test_window_and_ai_default_roi_share_one_analysis(tmp_path):
    file=tmp_path/'test.png';file.write_bytes(image(np.zeros((4,4,3),dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state');req={'source':'file','path':str(file)}
    ai=service.dispatch(req,lambda a,b:None)
    window=service.dispatch({**req,'roi':[0.,0.,1.,1.]},lambda a,b:None)
    assert ai['analysis']['analysis_id']==window['analysis']['analysis_id']


def test_window_defers_statistics_but_ai_gets_same_pixels_once(tmp_path,monkeypatch):
    from unittest.mock import Mock
    import c1ai.analysis_service as module
    file=tmp_path/'test.png';file.write_bytes(image(np.full((8,8,3),64,dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state');req={'source':'file','path':str(file)}
    counted=Mock(wraps=analyze);monkeypatch.setattr(module,'analyze',counted)
    light=service.dispatch({**req,'include_statistics':False,'include_image':'histogram'},lambda a,b:None)
    snapshot=light['analysis'];key=snapshot['analysis_id']
    assert light['freshness']['state']=='ready' and counted.call_count==0
    assert snapshot['statistics_state']=='not_requested' and 'statistics' not in snapshot['region']
    full=service.dispatch({'action':'get','analysis_id':key,'include_histograms':True},lambda a,b:None)
    assert full['analysis']['pixel_sha256']==snapshot['pixel_sha256']
    assert full['analysis']['statistics_state']=='ready'
    assert full['analysis']['region']['statistics']['red']['mean_percent']==pytest.approx(64/255*100)
    assert full['analysis']['region']['histograms']['red'][64]==64
    again=service.dispatch({**req,'known_analysis_id':key,'include_image':'histogram'},lambda a,b:None)
    assert 'statistics' in again['analysis']['region'] and counted.call_count==1
    assert again['artifact']==light['artifact']
    # Both references can start as lightweight snapshots; compare fills them on demand.
    file.write_bytes(image(np.full((8,8,3),192,dtype=np.uint8)))
    new=service.dispatch({**req,'include_statistics':False},lambda a,b:None)['analysis']
    delta=service.dispatch({'action':'get','analysis_id':new['analysis_id'],'reference_id':key},lambda a,b:None)
    assert delta['difference']['statistics']['red']['mean_percent']==pytest.approx(128/255*100)
    assert counted.call_count==2


def test_deferred_statistics_detect_edit_during_calculation(tmp_path,monkeypatch):
    import c1ai.analysis_service as module
    file=tmp_path/'test.png';file.write_bytes(image(np.full((8,8,3),64,dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state')
    light=service.dispatch({'source':'file','path':str(file),'include_statistics':False},lambda a,b:None)
    def changed(pixels,roi):
        file.write_bytes(image(np.full((8,8,3),192,dtype=np.uint8)))
        return analyze(pixels,roi)
    monkeypatch.setattr(module,'analyze',changed)
    result=service.dispatch({'action':'get','analysis_id':light['analysis']['analysis_id']},lambda a,b:None)
    assert result['freshness']['state']=='stale'
    assert result['analysis']['region']['statistics']['red']['mean_percent']==pytest.approx(64/255*100)


def test_histogram_compression_is_one_decade_independent_of_population():
    from c1ai.image_analysis import histogram_height
    expected=[0,0.27875360095,0.74036268949,1]
    assert np.allclose(histogram_height([0,10,50,100],100),expected)
    assert np.allclose(histogram_height([0,100000,500000,1000000],1000000),expected)


def test_analysis_color_spaces_convert_pixels_and_separate_snapshots(tmp_path):
    from c1ai.image_analysis import profile_bytes
    red=image(np.full((4,4,3),[255,0,0],dtype=np.uint8))
    p3=decode(red,'display_p3');adobe=decode(red,'adobe_rgb')
    # Independent sRGB primary conversion expectations (8-bit relative colorimetric).
    assert np.allclose(p3.rgb[0,0],[234,51,35],atol=2)
    assert np.allclose(adobe.rgb[0,0],[219,0,0],atol=2)
    wide=image(np.full((4,4,3),[0,255,0],dtype=np.uint8),profile_bytes('display_p3'))
    assert np.array_equal(decode(wide,'display_p3').rgb[0,0],[0,255,0])
    assert decode(p3.png,'display_p3').metadata['color']['confirmation']=='embedded_icc_converted'
    file=tmp_path/'test.png';file.write_bytes(red);service=AnalysisService(None,tmp_path/'state')
    req={'source':'file','path':str(file)}
    a=service.dispatch(req,lambda a,b:None)['analysis']
    b=service.dispatch({**req,'color_space':'display_p3'},lambda a,b:None)['analysis']
    assert a['analysis_id']!=b['analysis_id']
    with pytest.raises(ValueError):compare(a,b)


def test_native_bgra_rows_channels_match_png():
    rgb=np.array([[[255,0,0],[0,255,0]],[[0,0,255],[90,120,150]]],dtype=np.uint8)
    rgba=np.concatenate([rgb,np.full((2,2,1),255,dtype=np.uint8)],axis=2)
    raw=rgba[:,:,[2,1,0,3]].tobytes()
    meta={'width':2,'height':2,'stride':8,'format':'BGRA8','bit_depth':8,'row_order':'top-down'}
    p=decode(raw,raw=meta)
    assert p.metadata['pixel_sha256']==decode(image(rgb)).metadata['pixel_sha256']
    assert analyze(p)==analyze(decode(image(rgb)))
    with pytest.raises(ValueError):decode(raw[:-1],raw=meta)


def test_display_and_exact_snapshots_never_change_precision_on_get(tmp_path):
    file=tmp_path/'test.png';file.write_bytes(image(np.full((100,1000,3),90,dtype=np.uint8)))
    service=AnalysisService(None,tmp_path/'state');request={'source':'file','path':str(file),'include_statistics':False}
    display=service.dispatch({**request,'precision':'display'},lambda a,b:None)['analysis']
    exact=service.dispatch(request,lambda a,b:None)['analysis']
    assert display['analysis_size']==[640,64] and exact['analysis_size']==[1000,100]
    assert display['precision']=='display' and exact['precision']=='exact'
    assert display['analysis_id']!=exact['analysis_id']
    with pytest.raises(ValueError,match='fixed precision'):
        service.dispatch({'action':'get','analysis_id':display['analysis_id'],'precision':'exact'},lambda a,b:None)
    saved=service.dispatch({'action':'get','analysis_id':display['analysis_id'],'precision':'display'},lambda a,b:None)['analysis']
    assert saved['pixel_sha256']==display['pixel_sha256'] and saved['precision']=='display'
    with pytest.raises(ValueError,match='policy'):compare(exact,saved)


def test_display_waveform_and_parade_preserve_position_without_av(monkeypatch):
    import builtins
    real_import=builtins.__import__
    def no_av(name,*args,**kwargs):
        if name=='av':raise AssertionError('display must not initialize PyAV')
        return real_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',no_av)
    rgb=np.zeros((40,80,3),dtype=np.uint8);rgb[:,40:]=255
    pixels=decode(image(rgb),precision='display')
    canvas=np.asarray(scope_image(pixels,mode='waveform'))
    assert canvas[618:623,360:760].max()>canvas[65:70,360:760].max()
    assert canvas[65:70,1400:1800].max()>canvas[618:623,1400:1800].max()
    # ROI selects the white half; no leftover black-half trace.
    roi=np.asarray(scope_image(pixels,[.5,0,1,1],mode='waveform'))
    assert roi[65:70,360:1700].max()>200 and roi[618:622,360:1700].max()<100
    pixels=decode(image(np.full((40,80,3),[240,128,64],dtype=np.uint8)),precision='display')
    parade=np.asarray(scope_image(pixels,mode='parade'))
    for channel,(low,high) in enumerate(((94,102),(338,347),(479,488))):
        x=112+int((channel+.5)*1888/3)
        y=int(parade[65:623,x-5:x+5,channel].max(axis=1).argmax()+65)
        assert low<=y<=high


@pytest.mark.skipif(not importlib.util.find_spec('av'),reason='PyAV not installed')
def test_parade_channels_use_rgb_order():
    p=decode(image(np.full((80,80,3),[255,128,64],dtype=np.uint8)))
    canvas=np.array(scope_image(p,mode='parade'))
    for channel,(minimum,maximum) in enumerate(((65,70),(340,347),(480,487))):
        x=112+int((channel+.5)*1888/3)
        trace=canvas[65:623,x-5:x+5,channel]
        y=int(trace.max(axis=1).argmax()+65)
        assert minimum<=y<=maximum


@pytest.mark.skipif(not importlib.util.find_spec('av'),reason='PyAV not installed')
def test_ffmpeg_waveform_preserves_horizontal_position():
    a=np.zeros((80,80,3),dtype=np.uint8);a[:,40:]=255
    p=decode(image(a));canvas=np.array(scope_image(p,mode='waveform'))
    # Avoid axis lines at 0/100. In the black half trace is at the bottom,
    # while the white half trace is at the top (opposite corner stays dark).
    assert canvas[618:623,360:760].max()>canvas[65:70,360:760].max()
    assert canvas[65:70,1400:1800].max()>canvas[618:623,1400:1800].max()
    assert np.array_equal(canvas[:,:,0],canvas[:,:,1]) is False  # axis has neutral blue-gray labels
    assert canvas[618:623,360:760,0].max()==canvas[618:623,360:760,1].max()
