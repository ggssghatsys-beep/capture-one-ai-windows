"""Shared bounded snapshots for the window and MCP; no new server or edit path."""
from __future__ import annotations
from collections import OrderedDict
import copy
import io
from pathlib import Path
import re
import time
import uuid

from .analysis_sources import NativeSource, FileSource
from .contracts import digest
from .image_analysis import POLICY, COLOR_SPACES, policy_for, decode, analyze, compare, scope_image, roi_bounds, region_metadata, apply_mask


def failure_details(error):
    """Unwrap read-only renderer failures without changing the editing error contract."""
    details=getattr(error,'details',None)
    nested=details.get('error',{}) if isinstance(details,dict) else {}
    message=nested.get('message') or str(error)
    message=str(message).splitlines()[0]  # Avoid returning a full CLR stack for routine changes.
    transient=any(text in message for text in (
        'native preview still rendering; poll its id before starting another',
        'preview target or adjustments changed; render again','preview expired; render again',
        'Native renderer has not completed the observed revision','C1 is still rendering'))
    return {'error':message,'error_code':getattr(error,'code',None),
            **({'retry_after_ms':50} if transient else {})}


class AnalysisService:
    def __init__(self, backend, root):
        self.backend = backend
        self.root = Path(root)/'analysis'
        self.root.mkdir(parents=True,exist_ok=True)
        self.records = OrderedDict()
        self.latest = {}
        self.reference_id = None
        try:self._prune_disk()
        except OSError:pass  # Cache housekeeping must not block a valid analysis.

    def _source(self, request):
        scope=request.get('analysis_scope','image')
        if scope not in ('image','active_layer'):raise ValueError('Use analysis_scope image or active_layer')
        if request.get('source','native') == 'native':
            if request.get('path'):
                raise ValueError('Native source does not accept a file path')
            return NativeSource(self.backend,scope)
        if request.get('source') == 'file' and isinstance(request.get('path'),str):
            if scope!='image':raise ValueError('File source has no C1 layer mask')
            return FileSource(request['path'])
        raise ValueError('Use source native, or source file with path')

    def dispatch(self, request, check_target):
        action = request.get('action','refresh')
        if action not in {'refresh','status','get','reference','clear_reference'}:
            raise ValueError('Unknown analysis action')
        roi = request.get('roi')
        roi_bounds(roi,1600,1600)
        # JSON 0 and 0.0 must share a snapshot, especially window defaults vs MCP defaults.
        roi=[float(v) for v in (roi if roi is not None else [0,0,1,1])]
        space=request.get('color_space') or 'srgb'
        if space not in COLOR_SPACES:raise ValueError('Unsupported analysis color space')
        image = request.get('include_image','none')
        if 'include_statistics' in request and not isinstance(request['include_statistics'],bool):
            raise ValueError('include_statistics must be boolean')
        if image not in {'none','preview','waveform','parade','histogram','vectorscope'}:
            raise ValueError('Unsupported analysis image')
        if action=='clear_reference':
            self.reference_id=None
            return {'status':'ok','action':action,'reference_id':None}
        if action in {'get','reference'}:
            analysis_id = request.get('analysis_id')
            if analysis_id not in self.records:
                raise ValueError('Unknown or expired analysis_id; refresh to capture a new result')
            record = self.records[analysis_id]
            if request.get('color_space') and space!=record['pixels'].space:
                raise ValueError('Saved analysis has a fixed color space; refresh for another space')
            if request.get('roi') is not None and roi != record['result']['region']['roi_normalized']:
                raise ValueError('Saved analysis has a fixed ROI; refresh to analyze another region')
            if request.get('analysis_scope') is not None and request['analysis_scope']!=record['request'].get('analysis_scope','image'):
                raise ValueError('Saved analysis has a fixed scope; refresh to change it')
            if action=='reference':
                if not record['result']['region']['pixel_count']:raise ValueError('Cannot pin an empty analysis as reference')
                self.reference_id=analysis_id
            # A saved snapshot is never implicitly treated as current.
            source = self._source(record['request'])
        else:
            source = self._source(request)
            slot = digest([source.name,str(getattr(source,'path','')),roi,space,request.get('analysis_scope','image')])
            record = self.records.get(self.latest.get(slot))
        try:
            stamp = source.observe()
        except Exception as error:
            return self._response(record,'stale' if record else 'failed',request,**failure_details(error))
        if source.name != 'file':
            # Keep the existing refusal contract: a target mismatch must not return an image.
            check_target(request.get('target'), stamp['target'])
        changed = record is None or record['stamp'] != stamp['key']
        if action in {'get','reference','status'}:
            return self._response(record,'stale' if changed and record else 'waiting' if changed else 'ready',
                                  request,current_target=stamp['target'],current_revision=stamp['revision'])
        if not changed:
            # Window polling needs only a fresh stamp when it already has this exact artifact.
            if (request.get('known_analysis_id')==record['result']['analysis_id']
                    and record['result']['region']['pixel_count']>0
                    and request.get('include_statistics') is False and not request.get('reference_id')
                    and request.get('include_image','none') in record['artifacts']):
                return {'status':'ok','unchanged':True,'analysis_id':record['result']['analysis_id'],
                        'freshness':{'state':'ready','checked_at_unix':time.time()},'current_target':stamp['target']}
            return self._response(record,'ready',request,current_target=stamp['target'])
        start=time.monotonic()
        try:
            data,meta=source.acquire(stamp)
            acquired=time.monotonic()
            pixels=decode(data,space,raw=meta.get('pixel_buffer'))
            if '_mask_data' in meta:
                pixels=apply_mask(pixels,meta.pop('_mask_data'),meta.pop('_mask_geometry'),meta['geometry'])
            decoded=time.monotonic()
            region=region_metadata(pixels,roi)
            analyzed=time.monotonic()
            # Selection/edits can change while C1 renders or while NumPy computes.
            after=source.observe()
            if after['key'] != stamp['key']:
                return self._response(record,'stale' if record else 'waiting',request,
                                      current_target=after['target'],error='Selection or revision changed; late image discarded')
            analysis_id=uuid.uuid4().hex
            policy=policy_for(space)
            if pixels.mask_weights is not None:
                policy={**policy,'mask_weighting':'native grayscale coverage / 255',
                        'percentile_method':'weighted inverse CDF',
                        'scope_renderer':'coverage-weighted positional density; shared histogram and chroma counts'}
            result={**meta,**pixels.metadata,'analysis_id':analysis_id,'policy':policy,
                    'region':region,'statistics_state':'not_requested','elapsed_seconds':round(time.monotonic()-start,4),
                    'timing_seconds':{'acquire':round(acquired-start,4),'decode':round(decoded-acquired,4),
                                      'region':round(analyzed-decoded,4),'statistics':0.0}}
            new={'result':result,'pixels':pixels,'stamp':stamp['key'],
                 'request':{**{k:v for k,v in request.items() if k in {'source','path','analysis_scope'}},'roi':roi,'color_space':space},'artifacts':{}}
            self.records[analysis_id]=new
            self.latest[slot]=analysis_id
            self._prune()
            return self._response(new,'ready',request,current_target=after['target'])
        except Exception as error:
            return self._response(record,'stale' if record else 'failed',request,current_target=stamp['target'],**failure_details(error))

    def _ensure_statistics(self,record):
        if record['result']['statistics_state']=='ready':return False
        started=time.monotonic()
        result=record['result']
        result['region']=analyze(record['pixels'],result['region']['roi_normalized'])
        elapsed=time.monotonic()-started
        result['timing_seconds']['statistics']=round(elapsed,4)
        result['elapsed_seconds']=round(result['elapsed_seconds']+elapsed,4)
        result['statistics_state']='ready'
        return True

    def _ensure_vector(self,record):
        if 'vector' in record:return False
        from .vectorscope import calculate
        started=time.monotonic()
        record['vector']=calculate(record['pixels'],record['result']['region']['roi_normalized'])
        record['result']['vectorscope']=record['vector'][1]
        record['result']['timing_seconds']['vectorscope']=round(time.monotonic()-started,4)
        return True

    def _response(self,record,state,request,**extra):
        response={'status':'ok' if state=='ready' else state,'freshness':{'state':state,'checked_at_unix':time.time()},
                  'reference_id':self.reference_id,**extra}
        if not record:
            return response
        empty=record['result']['region']['pixel_count']==0
        if empty:
            response.update(status='empty' if state=='ready' else state,analysis=copy.deepcopy(record['result']),error='所选蒙版与 ROI 没有覆盖像素')
            response['freshness']['state']='empty' if state=='ready' else state
            if request.get('include_image')!='preview':return response
        # Window-only requests skip percentiles/zones. AI defaults remain complete.
        computed=False
        reference=request.get('reference_id')
        if not empty and (request.get('include_statistics') is not False or request.get('include_histograms') is True or reference):
            computed=self._ensure_statistics(record)
            if reference in self.records and self.records[reference]['result']['region']['pixel_count']:
                computed=self._ensure_statistics(self.records[reference]) or computed
        want_vector=not empty and (request.get('include_vectorscope') is True or request.get('include_image')=='vectorscope')
        if want_vector:
            computed=self._ensure_vector(record) or computed
            if reference in self.records and self.records[reference]['result']['region']['pixel_count']:computed=self._ensure_vector(self.records[reference]) or computed
        result=copy.deepcopy(record['result'])
        response['analysis']=result
        if reference and not empty:
            try:
                if reference not in self.records:
                    raise ValueError('Reference expired or belongs to another owner process')
                if not self.records[reference]['result']['region']['pixel_count']:
                    raise ValueError('Reference has no covered pixels')
                response['difference']=compare(result,self.records[reference]['result'])
                response['difference']['current_is_fresh']=state=='ready'
                if want_vector:
                    current=result['vectorscope'];old=self.records[reference]['result']['vectorscope']
                    response['difference']['vectorscope']={k:current[k]-old[k] for k in
                        ('mean_cb','mean_cr','mean_chroma_radius','near_neutral_percent')}
                    response['difference']['vectorscope_units']='Cb/Cr and radius in encoded projection units; neutral ratio in percentage points'
            except ValueError as error:
                response['difference']={'comparable':False,'reason':str(error)}
        if request.get('include_histograms') is not True:
            result['region'].pop('histograms',None)
        if not want_vector:result.pop('vectorscope',None)
        mode=request.get('include_image','none')
        created=False
        if mode!='none':
            try:
                created=mode not in record['artifacts']
                if created:
                    if mode=='preview':data=record['pixels'].png
                    else:
                        out=io.BytesIO()
                        scope_image(record['pixels'],record['result']['region']['roi_normalized'],mode,
                                    vector=record.get('vector'),histograms=record['result']['region'].get('histograms')).save(out,format='PNG',compress_level=1)
                        data=out.getvalue()
                    path=self.root/(result['analysis_id']+'-'+mode+'.png')
                    path.write_bytes(data)
                    record['artifacts'][mode]={'path':str(path),'sha256':__import__('hashlib').sha256(data).hexdigest(),
                                               'kind':mode,'analysis_id':result['analysis_id']}
                response['artifact']=record['artifacts'][mode]
            except Exception as error:
                response['image_error']=str(error)
        # A deferred numerical pass can also outlive the current edit/selection.
        if created or computed:
            try:
                now=self._source(record['request']).observe()
                if now['key']!=record['stamp']:
                    response.update(status='stale',current_target=now['target'])
                    response['freshness']={'state':'stale','checked_at_unix':time.time()}
            except Exception as error:
                response.update(status='stale',**failure_details(error))
                response['freshness']={'state':'stale','checked_at_unix':time.time()}
        if 'difference' in response and 'current_is_fresh' in response['difference']:
            response['difference']['current_is_fresh']=response['freshness']['state']=='ready'
        return response

    def _prune(self):
        while len(self.records)>12:
            victim=next(key for key in self.records if key!=self.reference_id)
            old=self.records.pop(victim)
            for artifact in old['artifacts'].values():
                path=Path(artifact['path'])
                if path.parent==self.root and path.name.startswith(victim+'-'):
                    try:path.unlink(missing_ok=True)
                    except OSError:pass
            self.latest={k:v for k,v in self.latest.items() if v!=victim}
        try:self._prune_disk()
        except OSError:pass

    def _prune_disk(self):
        # Only this module's UUID-named generated PNGs; never touch other owner files.
        files=[p for p in self.root.glob('*.png') if not p.is_symlink()
               and re.fullmatch(r'[0-9a-f]{32}-(preview|waveform|parade|histogram|vectorscope)\.png',p.name)]
        keep=set(self.records)
        for path in sorted(files,key=lambda p:p.stat().st_mtime,reverse=True):
            key=path.name[:32]
            if key in keep:continue
            if len(keep)<12:keep.add(key)
            else:path.unlink(missing_ok=True)
