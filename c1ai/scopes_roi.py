"""Image-coordinate ROI picker, independent of C1's viewer zoom and pan."""
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget, QDialog, QVBoxLayout, QLabel, QPushButton, QDialogButtonBox


class RegionCanvas(QWidget):
    selected=Signal(list)

    def __init__(self,path,roi,parent=None):
        super().__init__(parent)
        self.pixmap=QPixmap(path);self.roi=list(roi);self.origin=None
        self.setMinimumSize(240,180)

    def image_rect(self):
        if self.pixmap.isNull():return QRectF()
        size=self.pixmap.size().scaled(self.size(),Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width()-size.width())/2,(self.height()-size.height())/2,size.width(),size.height())

    def point(self,p):
        r=self.image_rect()
        return [max(0.,min(1.,(p.x()-r.left())/r.width())),max(0.,min(1.,(p.y()-r.top())/r.height()))]

    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),Qt.GlobalColor.black)
        r=self.image_rect()
        if r.isEmpty():return
        painter.drawPixmap(r,self.pixmap,QRectF(self.pixmap.rect()))
        l,t,right,b=self.roi
        painter.setPen(QPen(Qt.GlobalColor.white,2))
        painter.drawRect(QRectF(r.x()+l*r.width(),r.y()+t*r.height(),(right-l)*r.width(),(b-t)*r.height()))

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and self.image_rect().contains(event.position()):
            self.origin=self.point(event.position())

    def mouseMoveEvent(self,event):
        if self.origin is not None:
            p=self.point(event.position());x,y=self.origin
            self.roi=[min(x,p[0]),min(y,p[1]),max(x,p[0]),max(y,p[1])];self.update()

    def mouseReleaseEvent(self,event):
        if self.origin is not None:
            self.mouseMoveEvent(event);self.origin=None
            if self.roi[2]>self.roi[0] and self.roi[3]>self.roi[1]:self.selected.emit(self.roi)


class RegionDialog(QDialog):
    applied=Signal(list,dict)

    def __init__(self,path,analysis,roi,parent=None):
        super().__init__(parent);self.analysis=analysis
        self.setWindowTitle('框选分析区域');self.resize(680,620)
        layout=QVBoxLayout(self)
        name=analysis['target'].get('variant') or {}
        label=name.get('label') or '图像文件'
        layer=analysis.get('mask_layer',{}).get('name','整图')
        self.note=QLabel(f'{label} · {layer}\n拖动框选；统计范围为框选区域与当前图层蒙版的交集。')
        self.note.setWordWrap(True);layout.addWidget(self.note)
        self.canvas=RegionCanvas(path,roi,self);layout.addWidget(self.canvas,1)
        reset=QPushButton('恢复整个画面');reset.clicked.connect(self.reset);layout.addWidget(reset)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('应用')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.apply);buttons.rejected.connect(self.reject);layout.addWidget(buttons)

    def reset(self):self.canvas.roi=[0,0,1,1];self.canvas.update()

    def apply(self):
        l,t,r,b=self.canvas.roi
        if l<r and t<b:self.applied.emit(self.canvas.roi,self.analysis)
