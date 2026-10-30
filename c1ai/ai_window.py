"""Optional Qt window for direct API-controlled Capture One editing."""
from __future__ import annotations

import asyncio
import base64
import threading
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget)

from .ai_agent import PhotoAgent, create_client, safe_error
from .ai_settings import DEFAULT_URL, load_settings, normalize_url, save_settings
from .ai_capabilities import list_models, discover_efforts


class DiscoveryWorker(QThread):
    result = Signal(str, object)
    def __init__(self, settings, mode, metadata=None, parent=None):
        super().__init__(parent)
        self.settings, self.mode, self.metadata = dict(settings), mode, metadata

    def run(self):
        try:
            with create_client(self.settings['base_url'], self.settings['key']) as client:
                value = (list_models(client) if self.mode == 'models' else
                         discover_efforts(client, self.settings['model'], self.metadata))
                self.result.emit(self.mode, value)
        except Exception as error:
            self.result.emit('error', safe_error(error))
        finally:
            self.settings['key'] = ''


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle('API 设置')
        self.resize(630, 380)
        self.discovery = None
        self.models = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.url = QLineEdit(settings.get('base_url', DEFAULT_URL))
        self.key = QLineEdit(settings.get('key', ''))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setCurrentText(settings.get('model', ''))
        self.model.lineEdit().setPlaceholderText('先读取模型，或填写服务提供的模型 ID')
        self.load_models = QPushButton('读取模型')
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(self.load_models)
        self.effort = QComboBox()
        self.effort.setEditable(True)
        self.effort.setPlaceholderText('由服务决定（不传参数）')
        self.effort.lineEdit().setPlaceholderText('由服务决定（不传参数）')
        self.effort.setCurrentText(settings.get('effort', ''))
        self.load_efforts = QPushButton('查询思考等级')
        effort_row = QHBoxLayout()
        effort_row.addWidget(self.effort, 1)
        effort_row.addWidget(self.load_efforts)
        self.remember = QCheckBox('在 macOS 钥匙串保存密钥' if sys.platform == 'darwin' else '在本机加密保存密钥（当前 Windows 用户）')
        self.remember.setChecked(settings.get('remember', False))
        form.addRow('API 地址', self.url)
        form.addRow('API 密钥', self.key)
        form.addRow('模型', model_row)
        form.addRow('思考等级', effort_row)
        form.addRow('', self.remember)
        layout.addLayout(form)
        note = QLabel('支持 OpenAI Responses API，以及实现同协议的服务。\n'
                      '开始后会向此地址发送照片预览、分析数值及必要的照片标识。\n'
                      '会消耗 API 额度；不会上传 RAW 原文件。查询等级可能发起极小文本探测。\n'
                      '等级留空表示采用服务默认值。模型列表不保证每个模型均支持视觉及工具。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.discovery_status = QLabel('模型和等级均从当前 API 查询，没有内置型号／等级清单。')
        self.discovery_status.setWordWrap(True)
        layout.addWidget(self.discovery_status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.value = dict(settings)
        self.original_url = self.url.text()
        self.url.editingFinished.connect(self.endpoint_changed)
        self.load_models.clicked.connect(lambda: self.discover('models'))
        self.load_efforts.clicked.connect(lambda: self.discover('efforts'))
        self.model.currentTextChanged.connect(self.model_changed)
        self.key.textEdited.connect(self.credentials_changed)

    def credentials_changed(self):
        # Capabilities are account-specific even when the endpoint/model ID is unchanged.
        self.models.clear()
        self.model.clear()
        self.model.setCurrentText('')
        self.effort.clear()
        self.effort.setCurrentText('')
        self.discovery_status.setText('密钥已变化，请重新读取此账号的模型和思考等级。')

    def model_changed(self):
        self.effort.clear()
        self.effort.setCurrentText('')
        self.discovery_status.setText('模型已变化，请查询其思考等级；留空采用服务默认值。')

    def endpoint_changed(self):
        if self.url.text().strip().rstrip('/') != self.original_url.strip().rstrip('/'):
            self.key.clear()  # Never send a remembered credential to a newly entered provider.
            self.original_url = self.url.text()
            self.models.clear()
            self.model.clear()
            self.model.setCurrentText('')
            self.effort.clear()
            self.effort.setCurrentText('')

    def discover(self, mode):
        if self.discovery:
            return
        try:
            value = {'base_url': normalize_url(self.url.text()), 'key': self.key.text().strip(),
                     'model': self.model.currentText().strip()}
            if not value['key'] or (mode == 'efforts' and not value['model']):
                raise ValueError('请填写 API 密钥；查询等级还需要先选择模型。')
        except ValueError as error:
            self.discovery_status.setText(str(error))
            return
        self.discovery_status.setText('正在从服务读取…')
        for widget in (self.url, self.key, self.model, self.effort, self.load_models, self.load_efforts, self.buttons):
            widget.setEnabled(False)
        self.discovery = DiscoveryWorker(value, mode, self.models.get(value['model']), self)
        self.discovery.result.connect(self.discovered)
        self.discovery.finished.connect(self.discovery_finished)
        self.discovery.start()

    def discovered(self, mode, value):
        if mode == 'error':
            self.discovery_status.setText(value)
        elif mode == 'models':
            previous = self.model.currentText()
            self.models = {row['id']: row for row in value}
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(list(self.models))
            self.model.setCurrentText(previous if previous in self.models else '')
            self.model.blockSignals(False)
            self.model_changed()
            self.discovery_status.setText(f'服务返回 {len(self.models)} 个模型。请选择视觉模型，再查询思考等级。')
        else:
            previous = self.effort.currentText()
            self.effort.clear()
            self.effort.addItem('')
            self.effort.addItems(value['values'])
            self.effort.setCurrentText(previous if previous in value['values'] else '')
            self.discovery_status.setText(value['source'])

    def discovery_finished(self):
        worker = self.discovery
        self.discovery = None
        for widget in (self.url, self.key, self.model, self.effort, self.load_models, self.load_efforts, self.buttons):
            widget.setEnabled(True)
        if worker:
            worker.deleteLater()

    def reject(self):
        if self.discovery:
            self.discovery_status.setText('正在等待服务响应，完成后可关闭。')
            return
        super().reject()

    def save(self):
        try:
            url = normalize_url(self.url.text())
            value = {'base_url': url, 'model': self.model.currentText().strip(),
                     'effort': self.effort.currentText().strip(),
                     'key': self.key.text().strip(), 'remember': self.remember.isChecked()}
            if not value['model'] or not value['key']:
                raise ValueError('请填写密钥与模型；也可以取消后稍后再设置。')
            save_settings(value['base_url'], value['model'], value['key'], value['remember'], effort=value['effort'])
        except (ValueError, OSError, RuntimeError) as error:
            QMessageBox.warning(self, '无法保存', str(error))
            return
        self.value = value
        self.accept()


class Worker(QThread):
    event = Signal(str, object)
    def __init__(self, agent, settings, prompt='', edit=False, test=False, parent=None):
        super().__init__(parent)
        self.agent, self.settings, self.prompt = agent, dict(settings), prompt
        self.edit, self.test = edit, test
        self.stopped = threading.Event()

    def run(self):
        try:
            with create_client(self.settings['base_url'], self.settings['key']) as client:
                if self.test:
                    self.agent.check_stop(self.stopped)
                    options = {'reasoning': {'effort': self.settings['effort']}} if self.settings.get('effort') else {}
                    response = client.responses.create(model=self.settings['model'], input='Reply OK.',
                                                       max_output_tokens=128, store=False, **options)
                    self.agent.check_stop(self.stopped)
                    self.event.emit('status', 'API 已连接；图片与工具能力将在实际任务中验证。'
                                    if response.status == 'completed' else 'API 已响应，但测试输出未完成。')
                else:
                    asyncio.run(self.agent.run(self.prompt, client=client, model=self.settings['model'],
                        edit=self.edit, stopped=self.stopped, emit=self.event.emit,
                        effort=self.settings.get('effort', '')))
        except Exception as error:
            self.event.emit('error', safe_error(error))
        finally:
            self.settings['key'] = ''


class AIWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Capture One · AI 修图')
        self.resize(920, 700)
        self.setMinimumSize(600, 480)
        self.agent = PhotoAgent()
        self.worker = None
        self.picture = QPixmap()
        try:
            self.settings = load_settings()
        except (ValueError, OSError):
            self.settings = {'base_url': DEFAULT_URL, 'model': '', 'key': '', 'remember': False,
                             'warning': '配置无法读取，请重新设置 API。'}
        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        toolbar = QHBoxLayout()
        self.configure = QPushButton('API 设置')
        self.test = QPushButton('测试连接')
        self.clear = QPushButton('新对话')
        self.scopes = QPushButton('波形图')
        self.target = QLabel('开始时读取 C1 当前选中照片')
        for widget in (self.configure, self.test, self.clear, self.scopes):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        layout.addWidget(self.target)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preview = QLabel('当前照片的原生预览将在这里显示')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(200, 180)
        self.preview.setStyleSheet('background:#202124;color:#aaa;')
        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        splitter.addWidget(self.preview)
        splitter.addWidget(self.chat)
        splitter.setSizes([400, 450])
        splitter.splitterMoved.connect(self.show_picture)
        layout.addWidget(splitter, 1)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText('例如：保留肤色，把照片调成温暖、自然的秋日氛围。')
        self.prompt.setMaximumHeight(95)
        layout.addWidget(self.prompt)
        actions = QHBoxLayout()
        self.analyze = QPushButton('仅分析')
        self.edit = QPushButton('在副本修图')
        self.stop = QPushButton('停止')
        self.stop.setEnabled(False)
        for widget in (self.analyze, self.edit, self.stop):
            actions.addWidget(widget)
        actions.addStretch()
        self.usage = QLabel('')
        actions.addWidget(self.usage)
        layout.addLayout(actions)
        self.status = QLabel(self.settings.get('warning') or '填写 API 后即可使用。修改只落在副本；图片预览和工具数据会发送给所填服务。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.configure.clicked.connect(self.edit_settings)
        self.test.clicked.connect(lambda: self.start(test=True))
        self.analyze.clicked.connect(lambda: self.start(edit=False))
        self.edit.clicked.connect(lambda: self.start(edit=True))
        self.stop.clicked.connect(self.cancel)
        self.clear.clicked.connect(self.new_conversation)
        self.scopes.clicked.connect(self.open_scopes)

    def edit_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # New providers/models start without prior conversation content.
            connection_changed = any(self.settings.get(key) != dialog.value.get(key)
                                     for key in ('base_url', 'key', 'model'))
            self.settings = dialog.value
            if connection_changed:
                self.agent.reset(clear_candidate=False)
                self.chat.clear()
            self.status.setText('配置已保存。测试连接会消耗少量 API 额度。')

    def open_request(self, request):
        # The per-user window endpoint accepts only a request to show the window.
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def new_conversation(self):
        self.agent.reset()
        self.chat.clear()
        self.picture = QPixmap()
        self.preview.clear()
        self.preview.setText('开始时重新读取当前照片')
        self.target.setText('新对话：修图时会创建新副本')

    def open_scopes(self):
        from .scopes_launch import open_window
        try:
            open_window()
            self.status.setText('已请求打开波形图。')
        except Exception:
            self.status.setText('无法打开波形图，请使用 Scopes.ps1 检查依赖。')

    def start(self, edit=False, test=False):
        if self.worker and self.worker.isRunning():
            return
        if not self.settings.get('key') or not self.settings.get('model'):
            self.edit_settings()
            if not self.settings.get('key') or not self.settings.get('model'):
                return
        prompt = self.prompt.toPlainText().strip()
        if not test and not prompt:
            self.status.setText('请先输入修图要求或想分析的问题。')
            return
        if not test:
            self.picture = QPixmap()
            self.preview.clear()
            self.preview.setText('正在读取当前照片…')
            self.target.setText('正在核对 C1 当前选图')
            self.usage.clear()
            self.append('\n你：' + prompt + '\n\nAI：')
            self.prompt.clear()
        self.worker = Worker(self.agent, self.settings, prompt, edit, test, self)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.finished)
        self.set_running(True)
        self.status.setText('连接 API…' if test else '读取 C1 当前照片…')
        self.worker.start()

    def set_running(self, running):
        for button in (self.configure, self.test, self.clear, self.analyze, self.edit):
            button.setEnabled(not running)
        self.stop.setEnabled(running)

    def cancel(self):
        if self.worker:
            self.worker.stopped.set()
            self.stop.setEnabled(False)
            self.status.setText('正在停止；已发出的 C1 操作会等待结果，不再派发下一步。')

    def finished(self):
        self.set_running(False)
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()

    def append(self, text):
        # Plain text only: model output cannot load remote resources or create clickable actions.
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.chat.setTextCursor(cursor)
        self.chat.ensureCursorVisible()

    def on_event(self, kind, value):
        if kind == 'delta':
            self.append(value)
        elif kind == 'target':
            self.target.setText(value)
        elif kind == 'image':
            picture = QPixmap()
            if picture.loadFromData(base64.b64decode(value['data'])):
                self.picture = picture
                self.show_picture()
        elif kind in ('status', 'error'):
            self.status.setText(value)
            if kind == 'error':
                self.append('\n\n' + value + '\n')
        elif kind == 'tool':
            label = value['name'] + ((' · ' + value['module']) if value.get('module') else '')
            self.status.setText('执行：' + label)
        elif kind == 'tool_result':
            self.append('\n〔' + value['name'] + '：' + str(value.get('status') or '已返回') + '〕\n')
        elif kind == 'usage':
            self.usage.setText(f"{value['requests']} 次请求 · 输入 {value['input_tokens']} / 输出 {value['output_tokens']} tokens")

    def show_picture(self, *_):
        if not self.picture.isNull():
            self.preview.setPixmap(self.picture.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'picture'):
            self.show_picture()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel()
            self.status.setText('正在停止。等待当前请求结束后即可关闭，避免丢失正在执行的结果。')
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName('CaptureOneAI')
    app.setApplicationName('AI')
    from .scopes_window import SingleWindow
    instance = SingleWindow()  # Existing per-user locking; application directory separates it from Scopes.
    if not instance.claim({}):
        return
    font = 'PingFang SC' if sys.platform == 'darwin' else 'Microsoft YaHei UI'
    app.setStyleSheet('QWidget { font-family: "'+font+'"; font-size: 12px; } QPushButton { padding: 6px 12px; }')
    window = AIWindow()
    instance.listen(window)
    window.show()
    app.exec()


if __name__ == '__main__':
    main()
