"""Two image sources. The native adapter uses existing identity and rendering checks."""
from __future__ import annotations
import hashlib
from pathlib import Path
import time

from .contracts import digest


class NativeSource:
    name = 'capture_one_native_render'

    def __init__(self, backend, analysis_scope='image', precision='exact'):
        self.backend = backend
        self.analysis_scope=analysis_scope
        self.precision=precision
        self.max_size=640 if precision=='display' else 1600

    def observe(self):
        # Mac provides a native identity/revision read without saving or re-reading layers.
        observe=getattr(self.backend,'analysis_observe',None)
        obs = observe() if observe is not None else self.backend.observe([])
        revision = obs['native_context'].get('process_revision')
        if revision is None:
            raise ValueError('Native engine needs the scopes update: process revision unavailable')
        viewer=obs['native_context'].get('viewer_stamp') if self.precision=='display' else None
        return {'key': digest([obs['target'],revision,viewer]), 'revision': revision,
                'target': obs['target'], 'observation': obs}

    def acquire(self, stamp):
        # Read-only rendering; same backend + expected instance/document/variant/layer as preview.
        native = stamp['observation']['native_context']
        request={'kind':'native','max_size':self.max_size,'pixel_buffer':True}
        if self.precision=='display' and native.get('bridge_target'):request['live_sample']=True
        result = self.backend.preview(request, stamp['observation'])
        if result.get('status') != 'completed' or result.get('revision') != stamp['revision']:
            raise ValueError('Native renderer has not completed the observed revision')
        data = Path(result['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != result['image']['sha256']:
            raise ValueError('Native image bytes changed after render')
        meta={'source':self.name, 'target':stamp['target'],
                      'native_timing_seconds':result['image'].get('timing_seconds'),
                      'photo_key':[native.get('target',{}).get('document_uuid',stamp['target'].get('document',{}).get('path')),
                                   native.get('image_path',stamp['target']['image']['path'])],
                      'revision':stamp['revision'], 'render_id':result['preview_id'],
                      'geometry':result['geometry'], 'crop_applied':True,
                      'crop_evidence':result.get('crop_evidence','C1 VariantGeometryTransformer CropRawConstrained + renderer'),
                      'crop_signature':digest(result['geometry']),
                      'captured_at_unix':time.time(), 'render_completed':True,
                      'export_used':False, 'screenshot_used':False,
                      'analysis_scope':'image','selected_layer':stamp['target']['layer']}
        if result.get('sample_source'):
            meta.update(sample_source=result['sample_source'],sample_limitation=result.get('sample_limitation'),
                        viewer_stamp=result.get('viewer_stamp'))
        meta['sample_source']=result.get('sample_source') or 'native_job'
        meta['render_basis']={'sample_source':meta['sample_source']}
        if meta['sample_source']=='viewer_cache':
            viewer=result.get('viewer_stamp') or {}
            for key in ('proof_profile','display_profile'):
                if not isinstance(viewer.get(key),str):
                    raise ValueError('Viewer render basis is missing profile evidence')
                meta['render_basis'][key]=viewer[key]
        if result['image'].get('format')=='BGRA8':
            meta['pixel_buffer']={k:result['image'][k] for k in ('width','height','stride','format','row_order','bit_depth')}
        if self.analysis_scope=='active_layer' and stamp['target']['layer']!='Background':
            # The Windows engine currently accepts raw pixels only for photo renders.
            # A Mac bridge target identifies the adapter that also supports raw masks.
            mask_request={'kind':'mask','max_size':self.max_size}
            if native.get('bridge_target'):mask_request['pixel_buffer']=True
            mask=self.backend.preview(mask_request,stamp['observation'])
            if mask.get('status')!='completed' or mask.get('revision')!=stamp['revision']:
                raise ValueError('Native renderer has not completed the observed revision')
            mask_data=Path(mask['path']).read_bytes()
            if hashlib.sha256(mask_data).hexdigest()!=mask['image']['sha256']:
                raise ValueError('Native mask bytes changed after render')
            selected=(native['layer_state']['selected_layer'] if 'layer_state' in native else
                      next(row for row in native['layers'] if row['selected']))
            meta.update(analysis_scope='active_layer',mask_layer=selected,
                        mask_render_id=mask['preview_id'],_mask_data=mask_data,_mask_geometry=mask['geometry'])
            if mask['image'].get('format')=='BGRA8':
                meta['_mask_pixel_buffer']={k:mask['image'][k] for k in ('width','height','stride','format','row_order','bit_depth')}
        return data,meta


class FileSource:
    name = 'file'

    def __init__(self, path):
        self.path = Path(path).resolve()
        if self.path.suffix.lower() not in {'.png','.jpg','.jpeg','.tif','.tiff','.bmp','.webp'}:
            raise ValueError('File source requires an image preview, not RAW or a Session')

    def observe(self):
        stat = self.path.stat()
        if not self.path.is_file() or stat.st_size > 64*1024*1024:
            raise ValueError('Expected an image file up to 64 MiB')
        return {'key':digest([str(self.path),stat.st_size,stat.st_mtime_ns]),
                'revision':str(stat.st_mtime_ns), 'target':{'image':{'path':str(self.path)},'variant':None}}

    def acquire(self, stamp):
        return self.path.read_bytes(), {'source':self.name,'target':stamp['target'],
            'photo_key':str(self.path),'revision':stamp['revision'],'render_id':None,
            'geometry':None,'crop_applied':None,'crop_evidence':'File contents as supplied; no claim about C1 crop',
            'crop_signature':'file_contents','captured_at_unix':time.time(),'render_completed':None,
            'export_used':False,'screenshot_used':False}
