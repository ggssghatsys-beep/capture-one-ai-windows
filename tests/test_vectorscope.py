import io
import numpy as np
import pytest
from PIL import Image
from c1ai.image_analysis import decode,scope_image
from c1ai.vectorscope import calculate,skin_reference
from c1ai.analysis_service import AnalysisService


def png(rgb):
    out=io.BytesIO();Image.fromarray(np.asarray(rgb,dtype=np.uint8)).save(out,format='PNG');return out.getvalue()


def test_neutral_and_reference():
    pixels=decode(png(np.full((10,10,3),128)))
    grid,data=calculate(pixels)
    assert grid[256,256]==100 and grid.sum()==100
    assert data['near_neutral_percent']==100 and data['chroma_weighted_direction_degrees'] is None
    assert data['reference_sector_percent_of_chromatic'] is None
    assert skin_reference('srgb')['angle_degrees']==pytest.approx(123)
    assert skin_reference('display_p3')['skin_detection'] is False


def test_primary_positions_roi_and_white_dots():
    pixels=decode(png([[[255,0,0],[0,0,255]]]))
    _,red=calculate(pixels,[0,0,.5,1]);_,blue=calculate(pixels,[.5,0,1,1])
    # Independent BT.601 primary coordinates, within half a 513-grid cell.
    assert red['mean_cb']==pytest.approx(-.169,abs=.0012)
    assert red['mean_cr']==pytest.approx(.5,abs=.0012)
    assert blue['mean_cb']==pytest.approx(.5,abs=.0012)
    assert blue['mean_cr']==pytest.approx(-.081,abs=.0012)
    assert sum(red['hue_histogram_counts'])==red['population']==1
    canvas=np.asarray(scope_image(pixels,mode='vectorscope'))
    assert canvas.shape==(720,720,3)
    assert (canvas[90:125,260:290,:].min(axis=2)>200).any()


def test_vector_snapshot_and_reference_share_counts(tmp_path):
    file=tmp_path/'colors.png';file.write_bytes(png(np.full((8,8,3),[255,0,0])))
    service=AnalysisService(None,tmp_path/'state');req={'source':'file','path':str(file),'include_image':'vectorscope','include_statistics':False}
    first=service.dispatch(req,lambda a,b:None)
    assert first['analysis']['statistics_state']=='not_requested'
    summary=first['analysis']['vectorscope'];key=first['analysis']['analysis_id']
    assert summary['population']==64
    saved=service.dispatch({'action':'get','analysis_id':key,'include_vectorscope':True,'reference_id':key},lambda a,b:None)
    assert saved['analysis']['vectorscope']==summary
    assert saved['difference']['vectorscope']['mean_cb']==0
    assert saved['analysis']['pixel_sha256']==first['analysis']['pixel_sha256']
    assert 'density' not in summary
