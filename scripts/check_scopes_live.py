"""Focused real-C1 analysis check on one newly registered variant. No crop/viewer actions."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from c1ai.client import call


def main():
    root=Path('artifacts/scopes/live');root.mkdir(parents=True,exist_ok=True)
    log={'started_at':time.time(),'checks':[]}
    def save():
        (root/'native-edit-checks.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
    def ok(request):
        if request['command'] in {'adjust','images'}:request.setdefault('request_id',uuid.uuid4().hex)
        r=call(request)
        if r.get('status') not in {'ok','completed','control_confirmed'}:
            log['failure']={'request_id':request.get('request_id'),'result':r};save()
            raise RuntimeError('Check stopped; inspect saved receipt. No automatic write replay.')
        return r
    def read(module):return ok({'command':'read','module':module})
    def edit(module,patch):
        observed=read(module)
        return ok({'command':'adjust','module':module,'patch':patch,'target':observed['target'],
                   'observation_token':observed['observation_token']})
    def analysis(label):
        r=ok({'command':'analyze_image','include_histograms':True,'include_image':'preview'})
        (root/(label+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
        if r.get('artifact'):(root/(label+'.png')).write_bytes(Path(r['artifact']['path']).read_bytes())
        return r['analysis']
    initial=ok({'command':'read','fields':[]})
    log['return_target']=initial['target']
    candidate=ok({'command':'images','action':'create_candidate','target':initial['target'],
                  'observation_token':initial['observation_token']})
    log['candidate']=candidate['after']['target'];save()
    layers=read('layers')['state']['layers']
    edit('layers',{'action':'select','layer_ref':next(l for l in layers if l['is_image_layer'])})
    for module,patch in [('exposure',{'exposure':.9}),('white_balance',{'temperature':7000}),
                         ('curves',{'channel':'luma','points':[[0,0],[.5,.65],[1,1]]})]:
        old=read(module);before=analysis(module+'-before')
        start=time.monotonic();receipt=edit(module,patch);after=analysis(module+'-after')
        log['checks'].append({'module':module,'pixel_changed':before['pixel_sha256']!=after['pixel_sha256'],
            'revision_before':before['revision'],'revision_after':after['revision'],
            'mean_delta':after['region']['statistics']['luma']['mean_percent']-before['region']['statistics']['luma']['mean_percent'],
            'write_through_readback_analysis_and_png_seconds':time.monotonic()-start,
            'operation_id':receipt['operation_id']});save()
        edit(module,{'action':'paste','source_observation_token':old['observation_token']})
    layers=read('layers')['state']['layers']
    local=next((l for l in layers if not l['is_image_layer']),None)
    if local:
        edit('layers',{'action':'select','layer_ref':local})
        old=read('exposure');before=analysis('local-before')
        start=time.monotonic();receipt=edit('exposure',{'exposure':.8});after=analysis('local-after')
        log['checks'].append({'module':'local_exposure','pixel_changed':before['pixel_sha256']!=after['pixel_sha256'],
             'revision_before':before['revision'],'revision_after':after['revision'],
             'mean_delta':after['region']['statistics']['luma']['mean_percent']-before['region']['statistics']['luma']['mean_percent'],
             'write_through_readback_analysis_and_png_seconds':time.monotonic()-start,
             'operation_id':receipt['operation_id']});save()
        edit('exposure',{'action':'paste','source_observation_token':old['observation_token']})
    else:log['checks'].append({'module':'local_exposure','status':'skipped_no_existing_mask'})
    # Native UUID selection retains normal navigation guards.
    for label,uuid_ in [('return',initial['target']['variant']['id']),('test',log['candidate']['variant']['id']),('return-final',initial['target']['variant']['id'])]:
        listing=ok({'command':'images','action':'list','limit':200})
        ok({'command':'images','action':'select','variant_uuid':uuid_,'target':listing['target'],
            'observation_token':listing['observation_token']})
        frame=analysis('switch-'+label)
        log['checks'].append({'module':'selection','expected_variant':uuid_,
                             'observed_variant':frame['target']['variant']['id'],
                             'matching':frame['target']['variant']['id']==uuid_});save()
    log['finished_at']=time.time();log['status']='completed';save()
    print(json.dumps({'status':log['status'],'checks':log['checks']},ensure_ascii=True))


if __name__=='__main__':
    main()
