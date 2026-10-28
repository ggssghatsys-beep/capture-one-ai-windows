"""Read-only recovery evidence; never resubmits an adjustment."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def _entries(node): return {e.get('K'):e.get('V') for e in node.findall('E')}


def numeric_table(value):
    return [[float(x) for x in row.split(',')] for row in value.split(';')]


def reconcile(backend, record):
    target=record['target']
    if not record.get('legacy'):
        command=record.get('request',{}).get('command')
        if command=='images' and record.get('request',{}).get('action') in ('clone','create_candidate'):
            from .native_images import recover_clone_navigation
            return recover_clone_navigation(backend,record)
        if command=='preview':
            from .native_preview import recover_export, recover_preview_navigation
            exports=[a for a in record.get('actions',[]) if a.get('name')=='preview.export']
            if len(exports)==1:
                return recover_export(backend,record,timeout=3)
            if not exports:
                actions=record.get('actions',[])
                dispatched=[a for a in actions if a.get('status')!='not_dispatched']
                if len(dispatched)==1 and dispatched[0].get('name')=='preview.open_export':
                    return recover_preview_navigation(backend,record)
        if command!='adjust' or not record.get('request',{}).get('patch'):
            return {'outcome':'unknown','target':target,'evidence':{'reason':'no dedicated recovery evidence for this operation'}}
        observed=backend.observe(list(record['before']['values']))
        baseline=record['before']['values']
        patch=record['request']['patch']
        wanted={**baseline,**patch}
        same=observed['target']==target
        outcome='applied_not_restored' if same and observed['values']==wanted else 'unknown'
        restoring=any(a.get('name','').startswith('restore:') for a in record.get('actions',[]))
        values=observed['values']
        if same and restoring and values.keys()==baseline.keys():
            if values==baseline:
                outcome='restored'
            elif all(values[field] in (value,patch.get(field,value)) for field,value in baseline.items()):
                outcome='partial'
        return {'outcome':outcome,'target':observed['target'],'evidence':{'scope':'control_readback',
                'observation':observed,'restoration_started':restoring,'model_verified':False}}
    recovery=backend.config.get('legacy_reconciliation',{})
    if recovery.get('operation_id')!=record['operation_id']:
        return {'outcome':'unknown','target':target,'evidence':{'reason':'no qualified legacy recovery data'}}
    source=Path(recovery['source_path']); raw=source.read_bytes()
    if (hashlib.sha256(raw).hexdigest()!=recovery['source_sha256']
            or record.get('source_sha256')!=recovery['source_sha256']
            or Path(record.get('source_path','')).resolve()!=source.resolve()):
        raise RuntimeError('Legacy source changed since import')
    old=json.loads(raw)
    if old.get('id')!=record['operation_id']: raise RuntimeError('Legacy operation id does not match its original receipt')
    expected=recovery.get('target',{})
    if (any(expected.get(key)!=target.get(key) for key in ('document','image','variant'))
            or old.get('before_state',{}).get('values',{}).get('selection',{}).get('name')!=target['variant']['label']
            or old.get('before_state',{}).get('window',{}).get('title')!=Path(target['document']['path']).stem):
        raise RuntimeError('Legacy receipt is not bound to this exact recovery target')
    apply=old.get('apply',{})
    accepted=(apply.get('accepted') is True and apply.get('identity_verified') is True
              and apply.get('before',{}).get('toggle')=='Off' and apply.get('after',{}).get('toggle')=='On'
              and old.get('error',{}).get('details',{}).get('possibly_mutated') is False)
    sidecar=Path(recovery['sidecar_path']); first_stat=sidecar.stat(); content=sidecar.read_bytes(); last_stat=sidecar.stat()
    if (first_stat.st_size,first_stat.st_mtime_ns)!=(last_stat.st_size,last_stat.st_mtime_ns):
        raise RuntimeError('Sidecar changed while reading recovery evidence')
    variants=[v for v in ET.fromstring(content).findall('VAR') if _entries(v).get('UUID','').lower()==target['variant']['id'].lower()]
    if len(variants)!=1: raise RuntimeError('Legacy variant UUID not unique in saved sidecar')
    wanted=numeric_table(old['requested_style_patch']['ColorCorrections'])
    matched=[_entries(sl) for sl in variants[0].findall('SL') if 'ColorCorrections' in _entries(sl)
             and numeric_table(_entries(sl)['ColorCorrections'])==wanted]
    after_dispatch=sidecar.stat().st_mtime>old['created_at']
    # Fixed migration audit records which writer processes were stopped. This
    # result concerns completed historical attachment, not current live HSL.
    audited=recovery.get('audit_scope')=='import_and_single_style_toggle_only'
    good=accepted and len(matched)==1 and after_dispatch and audited
    return {'outcome':'applied_not_restored' if good else 'unknown','target':target,
            'evidence':{'scope':'style_attachment_persisted','effective_mixer_parameters_verified':False,
                        'model_verified':False,'restored':False,'original_receipt_sha256':recovery['source_sha256'],
                        'sidecar_path':str(sidecar),'sidecar_sha256':hashlib.sha256(content).hexdigest(),
                        'sidecar_mtime':sidecar.stat().st_mtime,'variant_uuid':target['variant']['id'],
                        'unique_matching_style_count':len(matched),'all_numeric_values_equal':len(matched)==1,
                        'accepted_native_toggle':accepted,'old_writer_stop_evidence':recovery.get('stopped_processes',[])}}
