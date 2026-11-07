"""Nonblocking GitHub update controls shared by settings and the about action."""
from pathlib import Path
import queue
import threading

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QGroupBox, QVBoxLayout, QHBoxLayout, QCheckBox, QProgressBar

from app_info import APP_VERSION
from hotfix import safe_path
from qt_dialogs import button, label
from updates import RELEASES_URL, UpdateCancelled, check_for_updates, prepare_update


class UpdatePanel(QGroupBox):
    update_available = Signal(str)
    install_requested = Signal(object)

    def __init__(self, root, automatic=True, can_install=lambda: True, open_url=None):
        super().__init__('软件更新')
        self.root, self.can_install = Path(root), can_install
        self.available = self.package = None
        self.checking = self.downloading = False
        self.pending = queue.Queue()
        self.cancel_event = threading.Event()
        layout = QVBoxLayout(self); layout.setSpacing(10)
        self.automatic = QCheckBox('启动时自动检查 GitHub 更新')
        self.automatic.setChecked(automatic); layout.addWidget(self.automatic)
        self.status = label(f'当前版本 {APP_VERSION} · 通过 GitHub Release 获取热补丁。')
        self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        self.notes = label(''); self.notes.setTextFormat(Qt.TextFormat.PlainText)
        self.notes.hide(); layout.addWidget(self.notes)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        self.progress.hide(); layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.check_button = button('检查更新', self.check_updates); row.addWidget(self.check_button)
        self.action = button('下载更新', self.download_or_install, True)
        self.action.setEnabled(False); row.addWidget(self.action)
        self.cancel_button = button('取消下载', self.cancel_event.set)
        self.cancel_button.hide(); row.addWidget(self.cancel_button)
        if open_url:
            row.addWidget(button('打开 GitHub 发布页', lambda: open_url(RELEASES_URL)))
        row.addStretch(); layout.addLayout(row)
        layout.addWidget(label('更新前自动备份，安装失败自动回退。个人设置、任务记录和项目文件保留；安装需等待任务结束。'))
        self.timer = QTimer(self); self.timer.setInterval(100); self.timer.timeout.connect(self.poll)

    def refresh_actions(self):
        self.check_button.setEnabled(not self.checking and not self.downloading)
        self.action.setText('安装并重启' if self.package else '下载更新')
        self.action.setEnabled(bool(self.available and not self.checking and not self.downloading
                                   and (not self.package or self.can_install())))
        if self.package and not self.checking and not self.downloading:
            self.status.setText('热补丁已下载并通过校验，点击「安装并重启」完成更新。' if self.can_install()
                                else '热补丁已准备好，请等待当前任务结束后安装。')

    def check_updates(self):
        if self.checking or self.downloading:
            return
        self.checking = True; self.status.setText('正在检查 GitHub 更新…')
        self.refresh_actions(); self.timer.start()
        def work():
            try:
                self.pending.put(('checked', check_for_updates()))
            except Exception as error:
                self.pending.put(('check_error', str(error)))
        threading.Thread(target=work, daemon=True).start()

    def download_or_install(self):
        if self.checking or self.downloading or not self.available:
            return
        if self.package:
            if not self.can_install():
                self.status.setText('更新已准备好，请等待当前任务结束后安装。')
                return
            self.install_requested.emit(self.package)
            return
        self.downloading = True; self.cancel_event.clear()
        self.progress.setValue(0); self.progress.show(); self.cancel_button.show()
        self.status.setText('正在下载热补丁…'); self.refresh_actions(); self.timer.start()
        document = self.available
        def work():
            try:
                cache = safe_path(self.root, '.updates')
                package = prepare_update(document, cache,
                    progress=lambda received, total: self.pending.put(('progress', (received, total))),
                    cancel=self.cancel_event)
                self.pending.put(('prepared', package))
            except UpdateCancelled as error:
                self.pending.put(('download_error', str(error)))
            except Exception as error:
                self.pending.put(('download_error', f'更新准备失败：{error}'))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        while not self.pending.empty():
            kind, value = self.pending.get_nowait()
            if kind == 'checked':
                self.checking = False
                if value['status'] == 'available':
                    document = value['update']
                    if not self.available or self.available['version'] != document['version']:
                        self.package = None
                    self.available = document
                    self.status.setText(f"发现新版本 {document['version']} · 热补丁 {document['package']['bytes'] / 2**20:.2f} MiB")
                    self.notes.setText(document.get('notes', '')[:8000]); self.notes.setVisible(bool(self.notes.text()))
                    self.update_available.emit(document['version'])
                else:
                    self.available = self.package = None; self.notes.hide()
                    self.update_available.emit('')
                    self.status.setText(f'当前已是最新版本（{APP_VERSION}）。' if value['status'] == 'current'
                        else f"GitHub 已发布 {value['version']}，暂未提供适用于当前版本的标准热补丁，请前往发布页查看。")
            elif kind == 'check_error':
                self.checking = False; self.status.setText(f'检查更新失败：{value}')
            elif kind == 'progress':
                received, total = value
                self.progress.setValue(int(received / total * 100))
                self.status.setText(f'正在下载并校验：{received / 2**20:.2f} / {total / 2**20:.2f} MiB')
            elif kind in ('prepared', 'download_error'):
                self.downloading = False; self.progress.hide(); self.cancel_button.hide()
                if kind == 'prepared':
                    self.package = value
                    self.status.setText('热补丁已下载并通过校验，点击「安装并重启」完成更新。')
                else:
                    self.status.setText(value)
            self.refresh_actions()
        if not self.checking and not self.downloading:
            self.timer.stop()
