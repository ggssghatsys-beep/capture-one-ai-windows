"""Scopes follows the matched runtime and discards a previous Session's frame."""
import io
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from c1ai import bridge_packages, scopes_launch
from c1ai.analysis_service import AnalysisService
from c1ai.analysis_sources import NativeSource
from c1ai.contracts import digest
from c1ai.scopes_window import ScopeApplication, ScopeWindow
from c1ai.windows_backend import NativeError


def binding(name):
    return {'path': 'C:/Photos/' + name + '.cosessiondb', 'uuid': name,
            'pid': 123, 'started_at': '456', 'generation': name}


def test_api_window_uses_selected_scopes_script(tmp_path, monkeypatch):
    selected=tmp_path/'selected';selected.mkdir()
    (selected/'Scopes.ps1').write_text('# matched launcher')
    monkeypatch.setattr(bridge_packages,'active_project',lambda: selected)
    monkeypatch.setattr(scopes_launch.shutil,'which',lambda name: 'C:/PowerShell/pwsh.exe')
    calls=[]
    monkeypatch.setattr(scopes_launch.subprocess,'Popen',lambda args,**kw: calls.append((args,kw)))
    result=scopes_launch.open_window('parade')
    args,options=calls[0]
    assert args[0]=='C:/PowerShell/pwsh.exe' and str(selected/'Scopes.ps1') in args
    assert args[-2:]==['-Mode','parade'] and options['cwd']==selected
    assert result['window']=='open_requested'


def test_console_entry_forwards_to_selected_python(tmp_path,monkeypatch):
    original=tmp_path/'original';selected=tmp_path/'selected'
    python=selected/'.venv/Scripts/pythonw.exe'
    python.parent.mkdir(parents=True);python.touch()
    monkeypatch.setattr(bridge_packages,'installed_project',lambda: original)
    monkeypatch.setattr(bridge_packages,'active_project',lambda project: selected)
    calls=[]
    monkeypatch.setattr(scopes_launch.subprocess,'Popen',lambda args,**kw: calls.append((args,kw)))
    assert scopes_launch.forward_selected_window(['--mode','histogram'])
    assert calls[0][0]==[str(python),'-m','c1ai.scopes_window','--mode','histogram']


def test_same_photo_and_revision_in_another_binding_gets_new_stamp():
    observation={'target':{'image':{'path':'photo.NEF'}},'session_binding':binding('A'),
                 'native_context':{'process_revision':1}}
    source=NativeSource(SimpleNamespace(observe=lambda fields: observation),precision='display')
    before=source.observe()
    observation['session_binding']=binding('B')
    after=source.observe()
    assert before['key']!=after['key'] and after['session_binding']==binding('B')


@pytest.mark.parametrize('failure',['render','no_selection'])
def test_service_exposes_new_binding_even_while_image_is_unavailable(tmp_path,monkeypatch,failure):
    state={'binding':binding('A'),'ready':True}
    target={'image':{'path':'photo.NEF'},'variant':{'id':'one'}}
    png=io.BytesIO();Image.new('RGB',(4,4),'gray').save(png,format='PNG')
    class Source:
        name='native_binding_test'
        def observe(self):
            if not state['ready'] and failure=='no_selection':
                raise NativeError('SINGLE_SELECTION_REQUIRED','Select one photo',{'session_binding':state['binding']})
            return {'key':digest(state['binding']),'revision':1,'target':target,'session_binding':state['binding']}
        def acquire(self,stamp):
            if not state['ready']:raise ValueError('C1 is still rendering')
            return png.getvalue(),{'target':target,'session_binding':stamp['session_binding'],
                                   'photo_key':'one','revision':1,'source':'test'}
    service=AnalysisService(None,tmp_path)
    monkeypatch.setattr(service,'_source',lambda request: Source())
    first=service.dispatch({'include_statistics':False},lambda *args: None)
    assert first['freshness']['state']=='ready'
    service.reference_id=first['analysis']['analysis_id']
    state.update(binding=binding('B'),ready=False)
    waiting=service.dispatch({'include_statistics':False},lambda *args: None)
    assert waiting['current_session_binding']==binding('B')
    assert waiting['analysis']['session_binding']==binding('A')
    assert service.reference_id is None


def test_window_clears_old_frame_reference_and_roi_on_session_switch(tmp_path):
    app=QApplication.instance() or ScopeApplication([])
    settings=QSettings(str(tmp_path/'scopes.ini'),QSettings.Format.IniFormat)
    window=ScopeWindow(settings=settings);window.timer.stop()
    try:
        window.source_binding=binding('A');window.reference='old-reference'
        window.record={'analysis':{'session_binding':binding('A')}}
        window.displayed_id='old';window.canvas.setText('old chart')
        window.roi[0].blockSignals(True);window.roi[0].setValue(.2);window.roi[0].blockSignals(False)
        previous=window.generation
        result={'current_session_binding':binding('B'),'analysis':{'session_binding':binding('A')},
                'freshness':{'state':'stale'},'error':'Select one photo'}
        window.receive(previous,result)
        assert window.generation>previous and window.source_binding==binding('B')
        assert window.record is None and window.reference is None and window.displayed_id is None
        assert [box.value() for box in window.roi]==[0,0,1,1]
        assert window.canvas.text()!='old chart'
        current=window.generation
        window.receive(current,result)
        assert window.generation==current and window.record is None
        assert window.next_delay==1500
    finally:
        window.timer.stop();window.close()
