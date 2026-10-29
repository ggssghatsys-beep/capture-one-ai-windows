"""Independent scopes window. All pixels and numerical results come from the shared owner."""
from __future__ import annotations
import argparse
import json
from collections import deque
from pathlib import Path
import sys
import time
from .scopes_launch import VIEWS

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSettings, QLockFile, QStandardPaths
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
                               QComboBox,QFileDialog,QDoubleSpinBox,QDialog,QMenu,QToolButton,QSizePolicy,QStyle)


def refresh_delay_ms(result):
    """A superseded frame is normal during dragging, not a disconnected source."""
    if result.get('retry_after_ms')==50:return 50
    state=result.get('freshness',{}).get('state','failed')
    error=str(result.get('error') or '')
    superseded=any(text in error for text in (
        'Selection or revision changed', 'Native renderer has not completed the observed revision',
        'C1 is still rendering', 'NATIVE_RENDER_PENDING',
        'native preview still rendering; poll its id before starting another',
        'preview target or adjustments changed; render again', 'preview expired; render again',
        'native target changed'))
    if superseded or state in ('waiting','stale') and not error:return 50
    if 'main form not ready' in error:return 200
    if state=='failed' or error:return 1500
    return 100


class Worker(QThread):
    result = Signal(int,dict)

    def __init__(self, generation, request):
        super().__init__()
        self.generation,self.request=generation,request

    def run(self):
        from .client import call
        started=time.perf_counter()
        try:
            result=call(self.request,timeout=45)
        except Exception as error:
            result={'status':'failed','freshness':{'state':'failed'},'error':str(error)}
        result['_window_request_seconds']=time.perf_counter()-started
        self.result.emit(self.generation,result)


class ScopeWindow(QWidget):
    def __init__(self, path=None, settings=None):
        super().__init__()
        self.settings=settings if settings is not None else QSettings('CaptureOneAI','Scopes')
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True)
        self.setWindowTitle('Capture One · 图像分析')
        self.resize(640,320);self.setMinimumSize(320,160)
        self.generation=0; self.worker=None; self.paused=False; self.pending=None
        self.path=path; self.record=None; self.reference=None; self.pixmap=QPixmap();self.displayed_id=None
        self.next_delay=100;self.latency_log=deque(maxlen=32);self.artifact_key=None;self.artifact_kind=None
        self.setStyleSheet('QWidget {background:#151b24;color:#e5edf7;font-size:12px;} '
                          'QPushButton,QToolButton,QComboBox,QDoubleSpinBox {background:#263242;padding:3px;border:1px solid #41516a;border-radius:3px;}'
                          'QPushButton:checked {background:#315679;border:1px solid #83b9e8;}')
        layout=QVBoxLayout(self);layout.setContentsMargins(4,4,4,4);layout.setSpacing(3)
        bar=QHBoxLayout();bar.setSpacing(3);layout.addLayout(bar)
        self.mode=QComboBox();self.mode.addItems(list(VIEWS.values()));self.mode.setMaximumWidth(90)
        saved_mode=self.settings.value('view','waveform')
        self.mode.setCurrentIndex(list(VIEWS).index(saved_mode) if saved_mode in VIEWS else 0)
        self.mode.setToolTip('亮度波形 / RGB Parade / 直方图 / 矢量图与肤色参考')
        self.mode.currentIndexChanged.connect(self.mode_changed);bar.addWidget(self.mode)
        self.color_space=QComboBox()
        for label,key in [('sRGB','srgb'),('Adobe RGB','adobe_rgb'),('Display P3','display_p3')]:self.color_space.addItem(label,key)
        self.color_space.setCurrentIndex(max(0,self.color_space.findData(self.settings.value('color_space','srgb'))))
        self.color_space.setMaximumWidth(110);self.color_space.setToolTip('分析色彩空间；实际转换像素，源 ICC 假设见更多')
        self.color_space.currentIndexChanged.connect(self.color_changed);bar.addWidget(self.color_space)
        self.pause=QPushButton();self.pause.setFixedWidth(27);self.pause.setToolTip('暂停 / 继续更新')
        self.pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        self.pause.clicked.connect(self.toggle_pause);bar.addWidget(self.pause)
        refresh=QPushButton();refresh.setFixedWidth(27);refresh.setToolTip('手动刷新')
        refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh.clicked.connect(lambda:self.tick(True));bar.addWidget(refresh)
        self.topmost=QPushButton('置顶');self.topmost.setCheckable(True);self.topmost.setFixedWidth(42)
        self.topmost.setToolTip('保持在其他普通窗口上方；不会持续抢占键盘焦点')
        self.topmost.toggled.connect(self.set_topmost);bar.addWidget(self.topmost)
        self.topmost.setChecked(self.settings.value('stay_on_top',True,type=bool))
        self.status_dot=QLabel('●');self.status_dot.setFixedWidth(12);bar.addWidget(self.status_dot)
        more=QToolButton();more.setText('⋯');more.setToolTip('更多');more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu=QMenu(more)
        self.autostart=menu.addAction('随 C1 配套启动器打开');self.autostart.setCheckable(True)
        self.autostart.setChecked(self.settings.value('autostart',False,type=bool))
        self.autostart.toggled.connect(self.set_autostart)
        menu.addSeparator()
        menu.addAction('区域、数值与状态…',self.show_details)
        menu.addAction('固定当前参考',self.pin);menu.addAction('清除参考',self.clear_reference)
        menu.addSeparator();menu.addAction('打开图像文件…',self.open_file);menu.addAction('跟随 C1',self.follow_native)
        more.setMenu(menu);bar.addWidget(more)
        self.canvas=QLabel();self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMinimumSize(1,1);self.canvas.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
        layout.addWidget(self.canvas,1)
        self.details=QDialog(self);self.details.setWindowTitle('区域、数值与状态');self.details.resize(560,280)
        details_layout=QVBoxLayout(self.details)
        roi_bar=QHBoxLayout();details_layout.addLayout(roi_bar);roi_bar.addWidget(QLabel('ROI 左 / 上 / 右 / 下'))
        self.roi=[]
        for value in (0,0,1,1):
            box=QDoubleSpinBox();box.setRange(0,1);box.setDecimals(3);box.setSingleStep(.05);box.setValue(value)
            box.valueChanged.connect(self.changed);self.roi.append(box);roi_bar.addWidget(box)
        self.identity=QLabel('等待读取当前照片');self.identity.setWordWrap(True);details_layout.addWidget(self.identity)
        self.state=QLabel('等待更新');self.state.setWordWrap(True);details_layout.addWidget(self.state)
        self.stats=QLabel();self.stats.setWordWrap(True);details_layout.addWidget(self.stats)
        self.note=QLabel();self.note.setWordWrap(True);details_layout.addWidget(self.note)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.tick);self.timer.start(50)

    def show_details(self):
        self.details.show();self.details.raise_()
        self.displayed_id=None
        if self.record and self.record.get('analysis'):
            request={'action':'get','analysis_id':self.record['analysis']['analysis_id'],
                     'include_vectorscope':self.mode.currentIndex()==list(VIEWS).index('vectorscope')}
            if self.reference:request['reference_id']=self.reference
            self.send(request)
        else:self.tick(True)

    def color_changed(self,*args):
        self.settings.setValue('color_space',self.color_space.currentData())
        self.changed()

    def set_topmost(self,enabled):
        geometry=self.saveGeometry();visible=self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,enabled)
        self.restoreGeometry(geometry)
        if visible:self.show()
        self.topmost.setToolTip('置顶已开启，点击关闭' if enabled else '点击保持在其他窗口上方')
        self.settings.setValue('stay_on_top',enabled)
        self.settings.sync()

    def mode_changed(self,*args):
        self.settings.setValue('view',list(VIEWS)[self.mode.currentIndex()])
        self.settings.sync()
        self.changed()

    def set_autostart(self,enabled):
        self.settings.setValue('autostart',enabled);self.settings.sync()

    def open_request(self,request):
        # Viewer state only: no Capture One edits or arbitrary commands.
        mode=request.get('mode')
        if mode in VIEWS:
            self.mode.setCurrentIndex(list(VIEWS).index(mode))
        path=request.get('file')
        if (path is None or isinstance(path,str)) and path!=self.path:
            self.path=path;self.changed()
        self.settings.sync()
        self.autostart.setChecked(self.settings.value('autostart',False,type=bool))
        self.showNormal();self.raise_()

    def changed(self,*args):
        self.generation+=1
        self.displayed_id=None
        self.state.setText('等待新请求；当前图表为上一份结果')
        self.next_delay=0
        if not self.paused:self.timer.start(180)

    def toggle_pause(self):
        self.paused=not self.paused;self.generation+=1
        self.timer.stop();self.next_delay=0
        if not self.paused:self.timer.start(0)
        self.pause.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay if self.paused else QStyle.StandardPixmap.SP_MediaPause))
        self.status_dot.setStyleSheet('color:#d9ad66' if self.paused else 'color:#8295aa')
        self.status_dot.setToolTip('已暂停' if self.paused else '等待更新')
        self.state.setText('已暂停；保留的快照不代表当前调整' if self.paused else '等待更新')

    def open_file(self):
        path,_=QFileDialog.getOpenFileName(self,'选择分析图像','','Image (*.png *.jpg *.jpeg *.tif *.tiff *.webp *.bmp)')
        if path:self.path=path;self.changed()

    def follow_native(self):
        self.path=None;self.changed()

    def send(self,request):
        if self.worker is not None:
            self.pending=request
            return
        self.timer.stop()
        self.worker=Worker(self.generation,{'command':'analyze_image',**request})
        self.worker.result.connect(self.receive)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def finished(self):
        worker=self.worker;self.worker=None
        worker.deleteLater()
        if self.pending:
            pending=self.pending;self.pending=None
            self.send(pending)
        elif not self.paused:
            self.timer.start(self.next_delay)

    def tick(self,force=False):
        if self.worker is not None or self.paused and not force:return
        request={'action':'refresh','source':'file' if self.path else 'native',
                 'include_statistics':self.details.isVisible(),
                 'include_vectorscope':self.mode.currentIndex()==list(VIEWS).index('vectorscope'),
                 'include_image':list(VIEWS)[self.mode.currentIndex()],
                 'roi':[v.value() for v in self.roi],'color_space':self.color_space.currentData()}
        if self.path:request['path']=self.path
        if self.reference and self.details.isVisible():request['reference_id']=self.reference
        if self.displayed_id and not force:request['known_analysis_id']=self.displayed_id
        self.send(request)

    def pin(self):
        if self.record and self.record.get('analysis'):
            self.displayed_id=None
            self.send({'action':'reference','analysis_id':self.record['analysis']['analysis_id'],'include_statistics':False})

    def clear_reference(self):
        self.reference=None;self.displayed_id=None;self.send({'action':'clear_reference'})

    def receive(self,generation,result):
        if generation!=self.generation:return
        received=time.perf_counter()
        if result.get('action')=='clear_reference':
            self.state.setText('已清除参考')
            self.stats.setText(self.stats.text().split('\n参考差值：')[0])
            return
        if result.get('reference_id'):self.reference=result['reference_id']
        state=result.get('freshness',{}).get('state','failed')
        delay=refresh_delay_ms(result)
        self.next_delay=delay
        names={'ready':'可用','waiting':'等待更新','stale':'已过期：保留旧结果','failed':'连接或分析失败'}
        detail=result.get('error') or result.get('image_error') or ''
        self.state.setText(names.get(state,state)+((' · '+str(detail)) if detail else ''))
        self.status_dot.setStyleSheet('color:'+{'ready':'#86c99a','waiting':'#d9ad66','stale':'#d9ad66','failed':'#ef8585'}.get(state,'#ef8585'))
        self.status_dot.setToolTip(self.state.text())
        data=result.get('analysis')
        if not data:
            if not result.get('unchanged'):self.record_latency(result,received,delay)
            return
        self.record=result
        target=data['target'];label=(target.get('variant') or {}).get('label') or Path(target['image']['path']).name
        revision=data.get('revision');size=data['analysis_size']
        self.identity.setText(f"{label} · {data['source']} · {size[0]} × {size[1]} · 修订 {revision}\n分析编号 {data['analysis_id']}")
        stats=data['region'].get('statistics',{}).get('luma')
        line='详细数值按需计算，与当前图表使用同一份像素快照'
        if stats:
            p=stats['percentiles_percent']
            line=f"亮度 P1 {p['P1']:.2f}% / P5 {p['P5']:.2f}% / P50 {p['P50']:.2f}% / P95 {p['P95']:.2f}% / P99 {p['P99']:.2f}%   近黑 {stats['near_black_percent']:.3f}% · 近白 {stats['near_white_percent']:.3f}%"
        diff=result.get('difference')
        if diff:
            line+='\n参考差值：'+(f"亮度均值 {diff['statistics']['luma']['mean_percent']:+.2f} 个百分点" if 'statistics' in diff else diff['reason'])
        self.stats.setText(line)
        self.note.setText('8 位预览 · '+('无 ICC，假定 sRGB；色彩未独立确认' if data['color']['confirmation'].startswith('assumed') else '已按嵌入 ICC 转换')+' → '+data['color']['analysis_space']+' · 近白比例不代表 RAW 高光不可恢复')
        artifact=result.get('artifact')
        if artifact:
            key=(artifact['path'],artifact['sha256'])
            if key!=self.artifact_key:
                loaded=QPixmap(artifact['path'])
                # Hide explanatory plot headers/footers in the compact viewer only.
                self.pixmap=loaded.copy(0,52,2048,620) if loaded.width()==2048 and loaded.height()==720 else loaded
                self.artifact_kind=artifact['kind'];self.paint_pixmap();self.artifact_key=key
            self.displayed_id=data['analysis_id'] if state=='ready' else None
        self.record_latency(result,received,delay)

    def record_latency(self,result,received,delay):
        # Bounded local diagnostics, no image bytes. Idle polls are omitted.
        data=result.get('analysis',{})
        row={'received_at_unix':time.time(),'request_seconds':result.get('_window_request_seconds'),
             'widget_update_seconds':time.perf_counter()-received,
             'state':result.get('freshness',{}).get('state'),'next_check_ms':delay,
             'analysis_id':data.get('analysis_id'),'stages':data.get('timing_seconds'),
             'error':str(result.get('error') or '')[:500]}
        self.latency_log.append(row)
        try:
            from .owner import state_directory
            (state_directory()/'scopes-latency.json').write_text(
                json.dumps(list(self.latency_log),ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:pass

    def paint_pixmap(self):
        if not self.pixmap.isNull():self.canvas.setPixmap(self.pixmap.scaled(self.canvas.size(),Qt.AspectRatioMode.KeepAspectRatio if self.artifact_kind=='vectorscope' else Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self,event):
        super().resizeEvent(event);self.paint_pixmap()

    def closeEvent(self,event):
        self.timer.stop()
        if self.worker is not None:
            self.paused=True;self.generation+=1;self.pending=None
            self.state.setText('正在结束本次只读取图，完成后关闭')
            self.worker.finished.connect(self.close);event.ignore()
        else:event.accept()


class SingleWindow:
    """One viewer per user; a short local message reopens the existing window."""
    def __init__(self):
        import hashlib
        root=QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        Path(root).mkdir(parents=True,exist_ok=True)
        self.name='CaptureOneAI-Scopes-'+hashlib.sha256(root.encode()).hexdigest()[:20]
        self.lock=QLockFile(str(Path(root)/'scopes-window.lock'))
        self.lock.setStaleLockTime(0)
        self.server=None;self.sockets=set()

    def claim(self,request):
        if self.lock.tryLock(0):return True
        import time
        for attempt in range(20):
            socket=QLocalSocket();socket.connectToServer(self.name)
            if socket.waitForConnected(100):
                socket.write(json.dumps(request).encode()+b'\n');socket.flush()
                if not socket.waitForBytesWritten(500) and socket.bytesToWrite():
                    raise RuntimeError('Could not deliver the window request')
                socket.disconnectFromServer()
                return False
            if self.lock.tryLock(0):return True
            time.sleep(.05)
        raise RuntimeError('Analysis window is starting or not responding; no duplicate was opened')

    def listen(self,window):
        QLocalServer.removeServer(self.name)  # Only the lock owner removes a stale endpoint.
        self.server=QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(self.name):raise RuntimeError(self.server.errorString())
        def accept():
            while self.server.hasPendingConnections():
                socket=self.server.nextPendingConnection();self.sockets.add(socket)
                buffer=bytearray()
                def read(socket=socket,buffer=buffer):
                    buffer.extend(bytes(socket.readAll()))
                    if len(buffer)>8192:socket.abort();return
                    if b'\n' not in buffer:return
                    try:
                        request=json.loads(bytes(buffer).split(b'\n',1)[0])
                        if isinstance(request,dict):window.open_request(request)
                    except (ValueError,TypeError):pass
                    socket.disconnectFromServer()
                def release(socket=socket):
                    self.sockets.discard(socket);socket.deleteLater()
                socket.readyRead.connect(read);socket.disconnected.connect(release)
                QTimer.singleShot(2000,socket,socket.abort)
                read()
        self.server.newConnection.connect(accept)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--file')
    parser.add_argument('--mode',choices=list(VIEWS))
    parser.add_argument('--if-enabled',action='store_true')
    parser.add_argument('--autostart',choices=['on','off'])
    parser.add_argument('--configure-only',action='store_true')
    args=parser.parse_args()
    app=QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName('CaptureOneAI');app.setApplicationName('Scopes')
    settings=QSettings('CaptureOneAI','Scopes')
    if args.autostart:
        settings.setValue('autostart',args.autostart=='on');settings.sync()
    if args.configure_only or args.if_enabled and not settings.value('autostart',False,type=bool):return 0
    instance=SingleWindow();request={'mode':args.mode,'file':args.file}
    if not instance.claim(request):return 0
    window=ScopeWindow(args.file,settings=settings)
    if args.mode:window.mode.setCurrentIndex(list(VIEWS).index(args.mode))
    instance.listen(window);window.show()
    return app.exec()


if __name__=='__main__':
    raise SystemExit(main())
