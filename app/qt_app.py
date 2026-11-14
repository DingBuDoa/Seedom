"""PySide6 desktop workspace. Existing solvers, reports and importers are reused."""
from collections import deque
from dataclasses import asdict, fields
from pathlib import Path
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

from PySide6.QtCore import (Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
                            QTimer, QSize, QUrl, QRect, QPoint, QEvent)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QFontDatabase, QDesktopServices, QAction, QKeySequence, QTextCursor, QPainter, QIcon
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTableView, QHeaderView, QAbstractItemView,
    QStackedWidget, QButtonGroup, QSplitter, QFormLayout, QGridLayout, QGroupBox, QScrollArea,
    QComboBox, QCheckBox, QFileDialog, QMessageBox,
    QMenu, QDialog, QPlainTextEdit, QProgressBar, QInputDialog, QStyledItemDelegate,
    QStyleOptionViewItem, QStyle, QSizePolicy, QToolTip, QLayout)

from tracker_core import Options, Pipeline, Cancelled
from paths import ROOT, APP, CONFIG, LOGS, OUTPUTS, BINARY_DIRS, DEV_COLMAP, saved_path, portable_path
from project_library import ProjectLibrary, model_ready
from project_layout import result_directory, result_report, blend_filename
from estimate import estimate_jobs
from hardware import get_hardware, describe_hardware, hardware_summary, system_information
from log_management import rotate_log
from task_queue import TaskQueue, MOVABLE_STATES
from qt_dialogs import TaskDialog, BlenderDialog, button, label
from blender_runtime import discover_blenders, probe_blender
from qt_style import (STYLE, icon, inset_numeric_fields, style_for_theme,
                      detect_system_theme, theme_color)
from qt_updates import UpdatePanel
from app_info import APP_NAME, APP_VERSION, SOLVER_CHOICES
from json_store import atomic_write_json
from task_storage import restore_jobs
from task_progress import live_remaining
from system_actions import execute_power_action, power_action_label

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
SOLVERS = {'colmap-cpu': 'COLMAP · 原生 Ceres CPU', 'fastmap': 'FastMap · 兼容模式',
           'glomap-gpu': 'GLOMap · 旧版本', 'glomap-cpu': 'GLOMap · 旧版本', 'glomap': 'GLOMap · 旧版本'}


class DetailValueLabel(QLabel):
    def __init__(self, text='—', parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setIndent(0)
        self.setWordWrap(False)
        self.setToolTip(text)
        self._update_elision()

    def setFullText(self, text):
        new_text = str(text or '—')
        changed = self.full_text != new_text
        self.full_text = new_text
        self.setToolTip('' if self.full_text == '—' else self.full_text)
        self._update_elision()
        if changed:
            self.updateGeometry()

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.full_text),
                     self.fontMetrics().height() + 4)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height() + 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, 'full_text'):
            self._update_elision()

    def _update_elision(self):
        self.setMinimumHeight(self.fontMetrics().height() + 4)
        width = max(0, self.contentsRect().width())
        self.setText(QFontMetrics(self.font()).elidedText(
            self.full_text, Qt.TextElideMode.ElideRight, width))

class DetailMessageLabel(QLabel):
    """Wrap diagnostic text and reserve its full height in the details panel."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.setFullText(text)

    def setFullText(self, text):
        self.setText(str(text))
        self.setToolTip(str(text))
        self._update_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._update_height()

    def _update_height(self):
        height = self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, self.contentsRect().width()), 16777215),
            Qt.TextFlag.TextWordWrap, self.text()).height() + 4
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)


def output_size(size):
    if size is None: return '—'
    for suffix, divisor in (('GiB', 2**30), ('MiB', 2**20), ('KiB', 2**10)):
        if size >= divisor: return f'{size / divisor:.1f} {suffix}'
    return f'{size} B'


def row_actions(row):
    pending = row['state'] in ('等待', '排队', '失败', '已停止') and row.get('queued')
    return [('edit', '编辑任务参数' if pending else '重命名', bool(pending or row.get('record') and not row['busy'])),
            ('play', '开始此任务', bool(pending and not row['busy'])),
            ('folder', '打开结果 / 输出目录', bool(row.get('directory'))),
            ('log', '查看任务日志', bool(row['result'] or row.get('scene_path'))),
            ('trash', '移入项目回收目录' if row.get('record') else '从队列移除',
             bool(row.get('record') and not row['busy'] or row.get('queued') and row['state'] not in ('运行中', '排队')))]


class TaskModel(QAbstractTableModel):
    headings = ['任务 / 素材', '求解方案', '处理设置', '输出大小', '创建时间', '状态 / 阶段', '操作']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headings)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headings[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return None
        row, col = self.rows[index.row()], index.column()
        if role == Qt.ItemDataRole.UserRole: return row
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 0 and row.get('queue_position'):
                return f"执行顺序：第 {row['queue_position']} 项\n{row['name']}\n{row['path']}"
            if col == 4: return '创建：' + (row['date'] or '旧记录未提供') + '\n完成：' + (row['result'].get('completed_at') or '尚未完成')
            if col == 3:
                size = row.get('bytes')
                return (f'输出大小：{output_size(size)}（{size:,} 字节）\n' if size is not None else '') + row['result'].get('model', '尚未生成点云')
            if col == 6: return '编辑 · 开始 · 输出目录 · 日志 · 移除（按任务状态启用）'
            return f"{row['name']}\n{row['path']}\n{row.get('error', '')}".strip()
        if role == SORT_ROLE:
            if col == 3: return row.get('bytes') if row.get('bytes') is not None else -1
            return row['date'] if col == 4 else row['state'] if col == 5 else row['cells'][col].casefold()
        if role == Qt.ItemDataRole.DisplayRole: return row['cells'][col]
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(theme_color('#1c1f26' if col == 0 else '#697382'))

    def replace(self, rows):
        self.beginResetModel(); self.rows = rows; self.endResetModel()


class TaskFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query, self.state = '', '全部'
        self.setSortRole(SORT_ROLE)

    def filterAcceptsRow(self, index, parent):
        row = self.sourceModel().rows[index]
        if self.query and self.query not in (row['name'] + ' ' + row['path'] + ' ' + row['cells'][1]).casefold():
            return False
        state = row['state']
        return (self.state == '全部' or self.state == '待处理' and state in ('等待', '排队')
                or self.state == '进行中' and state == '运行中'
                or self.state == '已完成' and state == '完成'
                or self.state == '异常' and state in ('失败', '已停止', '文件不完整'))


class TaskTableView(QTableView):
    """Reserve a fixed action rail while retaining Qt's native scrollbars."""
    def __init__(self):
        super().__init__()

    def update_sticky_actions(self):
        rail = getattr(self, 'sticky_actions', None)
        if rail:
            viewport = self.viewport().geometry()
            table_origin = self.mapTo(rail.parentWidget(), QPoint(0, 0))
            rail.setGeometry(QRect(table_origin + QPoint(viewport.right() + 1, 0),
                                   QSize(StickyActions.WIDTH, self.height())))
            rail.raise_()
            rail.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_sticky_actions()

    def updateGeometries(self):
        super().updateGeometries()
        self.setViewportMargins(0, self.horizontalHeader().height(), StickyActions.WIDTH, 0)
        self.update_sticky_actions()


class StickyActions(QWidget):
    """Render row actions in a fixed rail beside the horizontally scrolling table."""
    WIDTH = 142

    def __init__(self, table, callback, parent):
        super().__init__(parent)
        self.table, self.callback = table, callback
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)

    def map_from_table(self, widget, point):
        parent_point = widget.mapTo(self.parentWidget(), point)
        return self.mapFromParent(parent_point)

    def header_bottom(self):
        header = self.table.horizontalHeader()
        return self.map_from_table(header, QPoint(0, header.height())).y()

    def row_rect(self, row_number):
        index = self.table.model().index(row_number, 0)
        rect = self.table.visualRect(index)
        top = self.map_from_table(self.table.viewport(), rect.topLeft()).y()
        return QRect(0, top, self.width(), rect.height())

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme_color('#ffffff')))
        header_bottom = self.header_bottom()
        painter.fillRect(0, 0, self.width(), header_bottom, QColor(theme_color('#fafbfc')))
        action_left = 0
        painter.setPen(QColor(theme_color('#e3e5e9')))
        painter.drawLine(0, header_bottom - 1, self.width(), header_bottom - 1)
        painter.setPen(QColor(theme_color('#89929f')))
        painter.drawText(QRect(action_left + 12, 0, self.WIDTH - 20, header_bottom),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, '操作')
        model = self.table.model()
        row_count = model.rowCount()
        if row_count == 0:
            return
        delegate = self.table.itemDelegate()
        first = self.table.rowAt(0)
        last = self.table.rowAt(max(0, self.table.viewport().height() - 1))
        if first < 0:
            first = 0
        if last < first:
            last = row_count - 1
        last = min(last, row_count - 1)
        for row_number in range(first, last + 1):
            row_rect = self.row_rect(row_number)
            y, height = row_rect.top(), row_rect.height()
            if y + height <= 0 or y >= self.height(): continue
            index = model.index(row_number, 6)
            row = index.data(Qt.ItemDataRole.UserRole)
            if row is None: continue
            if self.table.selectionModel().isRowSelected(row_number, QModelIndex()):
                painter.fillRect(0, y, self.width(), height, QColor(theme_color('#eaf0ff')))
            painter.setPen(QColor(theme_color('#eef0f4')))
            painter.drawLine(0, y + height - 1, self.width(), y + height - 1)
            for (name, _, enabled), x in zip(row_actions(row),
                                              range(action_left + 6, action_left + 6 + 5 * 26, 26)):
                delegate.icons[name, enabled].paint(painter, QRect(x + 5, y + (height - 26) // 2 + 5, 16, 16))

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        point = event.position().toPoint()
        viewport_y = self.y() + point.y() - self.table.viewport().geometry().top()
        row_number = self.table.rowAt(viewport_y)
        if row_number < 0: return
        index = self.table.model().index(row_number, 6)
        row = index.data(Qt.ItemDataRole.UserRole)
        if row is None: return
        action_index = (event.position().toPoint().x() - 6) // 26
        if 0 <= action_index < 5:
            name, _, enabled = row_actions(row)[action_index]
            if enabled: QTimer.singleShot(0, lambda: self.callback(row, name))
        self.update()

    def mousePressEvent(self, event):
        if event.position().toPoint().y() < self.header_bottom():
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        viewport_y = self.y() + point.y() - self.table.viewport().geometry().top()
        row_number = self.table.rowAt(viewport_y)
        offset = event.position().toPoint().x() - 6
        action_index = offset // 26
        if row_number >= 0 and 0 <= action_index < 5:
            index = self.table.model().index(row_number, 6)
            row = index.data(Qt.ItemDataRole.UserRole)
            name, title, enabled = row_actions(row)[action_index]
            self.setToolTip(title + ('' if enabled else '（当前不可用）'))
        else:
            self.setToolTip('')
        super().mouseMoveEvent(event)


class TaskDelegate(QStyledItemDelegate):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.refresh_theme()

    def refresh_theme(self):
        self.icons = {(name, enabled): icon(name, theme_color('#697382' if enabled else '#c7ccd5'))
                      for name in ('edit', 'play', 'folder', 'log', 'trash') for enabled in (True, False)}

    def action_rects(self, option):
        return [QRect(option.rect.left() + 6 + index * 26, option.rect.center().y() - 13, 26, 26) for index in range(5)]

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if index.column() not in (0, 5, 6):
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ''
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        rect = option.rect.adjusted(12, 0, -12, 0)
        if index.column() == 6:
            for (name, _, enabled), target in zip(row_actions(row), self.action_rects(option)):
                self.icons[name, enabled].paint(painter, target.adjusted(5, 5, -5, -5))
        elif index.column() == 0:
            font = QFont(option.font); font.setWeight(QFont.Weight.Medium)
            painter.setFont(font); painter.setPen(QColor(theme_color('#1c1f26')))
            painter.drawText(rect.adjusted(0, 7, 0, -25), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(row['name'], Qt.TextElideMode.ElideRight, rect.width()))
            font.setPixelSize(11); font.setWeight(QFont.Weight.Normal)
            painter.setFont(font); painter.setPen(QColor(theme_color('#98a0ac')))
            painter.drawText(rect.adjusted(0, 27, 0, -5), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(row['path'], Qt.TextElideMode.ElideMiddle, rect.width()))
        else:
            state = row['state']
            color, fill = {'完成': ('#16865f', '#eaf7f1'), '运行中': ('#2f6bff', '#eaf0ff'),
                '失败': ('#d8443c', '#fdecea'), '已停止': ('#a77516', '#fff6e3'),
                '文件不完整': ('#d8443c', '#fdecea')}.get(state, ('#737c89', '#f0f2f5'))
            color, fill = theme_color(color), theme_color(fill)
            text = row.get('stage', '') if state == '运行中' else ('Blender 已生成' if state == '完成' and row.get('exports') else state)
            if state in MOVABLE_STATES and row.get('queue_position'):
                text += f" · {row['queue_position']}"
            if state == '完成' and row.get('export_error'): text = '完成 · 导出异常'
            text = text or state
            font = QFont(option.font); font.setPixelSize(12); painter.setFont(font)
            width = min(rect.width(), painter.fontMetrics().horizontalAdvance(text) + 16)
            badge = rect.adjusted(0, 15, 0, -15); badge.setWidth(width)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(fill)); painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor(color)); painter.drawText(badge.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, width - 16))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if index.column() == 6 and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            row = index.data(Qt.ItemDataRole.UserRole)
            for (name, _, enabled), target in zip(row_actions(row), self.action_rects(option)):
                if target.contains(event.position().toPoint()) and enabled:
                    QTimer.singleShot(0, lambda r=row, key=name: self.callback(r, key))
                    return True
        return super().editorEvent(event, model, option, index)

    def helpEvent(self, event, view, option, index):
        if index.column() == 6:
            row = index.data(Qt.ItemDataRole.UserRole)
            for (_, title, enabled), target in zip(row_actions(row), self.action_rects(option)):
                if target.contains(event.pos()):
                    QToolTip.showText(event.globalPos(), title + ('' if enabled else '（当前不可用）'), view)
                    return True
        return super().helpEvent(event, view, option, index)


class MainWindow(QMainWindow):
    def __init__(self, config_dir=None, background=True):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} {APP_VERSION} · 相机求解与点云')
        self.setWindowIcon(QIcon(str(ROOT / 'LOGO.ico')))
        self.resize(1360, 870); self.setMinimumSize(1020, 680)
        self.config_dir = Path(config_dir) if config_dir else CONFIG
        self.config_dir.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(exist_ok=True); OUTPUTS.mkdir(exist_ok=True)
        self.settings_path = self.config_dir / 'settings.json'
        try:
            self.config = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if not isinstance(self.config, dict): self.config = {}
        except (OSError, ValueError): self.config = {}
        self.blender_records = [record for record in self.config.get('blender_records', [])
                                if isinstance(record, dict) and record.get('path') and record.get('version')]
        self.blender_pending = queue.Queue()
        self.blender_discovery_running = False
        self.active_task_dialog = None
        self.environment_generation = 0
        known = {f.name for f in fields(Options)}
        values = {key: value for key, value in self.config.items() if key in known}
        settings_error = None
        try:
            self.defaults = Options(**values).validate()
            for key in ('output_root', 'python'):
                setattr(self.defaults, key, str(saved_path(getattr(self.defaults, key), self.config.get('distribution_root'))))
        except (ValueError, TypeError) as error:
            self.defaults = Options(); settings_error = str(error)
        if Path(self.defaults.output_root).resolve() == ROOT / 'outputs':
            self.defaults.output_root = str(OUTPUTS)
        self.library = ProjectLibrary(self.config_dir / 'project-library.json')
        self.events = queue.Queue()
        if settings_error:
            self.events.put(dict(type='log', text=f'[设置恢复] 处理参数无效，已使用默认值：{settings_error}\n'))
        self.tasks = TaskQueue(self.events.put)
        self.tasks.reuse_failed_projects = self.config.get('reuse_failed_projects') is True
        self.records = {}
        self.project_loading = False
        self.project_refresh_pending = False
        self.export_pipeline = None
        self.exporting = False
        self.started = 0
        self.active_progress = None
        self.active_progress_iid = None
        self.progress_log_at = 0
        self.estimate_generation = 0
        self.log_lines = deque(); self.log_chars = 0
        self.log_dialog = self.log_widget = None
        self.recent_log_signature = None
        self.last_scene = None
        self.background = background
        self._theme_mode = self.config.get('theme_mode', 'auto')
        if self._theme_mode not in ('auto', 'light', 'dark'):
            self._theme_mode = 'auto'
        self.apply_theme()
        color_scheme_changed = getattr(QApplication.instance().styleHints(), 'colorSchemeChanged', None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(self.system_theme_changed)
        self.restore_tasks()
        self.build()
        self.rebuild_rows()
        self.poller = QTimer(self); self.poller.setInterval(100); self.poller.timeout.connect(self.poll); self.poller.start()
        self.estimate_timer = QTimer(self); self.estimate_timer.setSingleShot(True); self.estimate_timer.setInterval(300)
        self.estimate_timer.timeout.connect(self.estimate)
        if background:
            QTimer.singleShot(100, self.refresh_projects)
            QTimer.singleShot(300, self.check_environment)
            QTimer.singleShot(120, self.cache_blender_versions)
            if self.update_panel.automatic.isChecked():
                QTimer.singleShot(3000, self.update_panel.check_updates)

    @property
    def busy(self):
        return self.tasks.running or self.exporting

    def build(self):
        central = QWidget(); shell = QHBoxLayout(central); shell.setContentsMargins(0, 0, 0, 0); shell.setSpacing(0)
        self.setCentralWidget(central)
        side = QWidget(); side.setObjectName('sidebar'); side.setFixedWidth(196)
        sl = QVBoxLayout(side); sl.setContentsMargins(12, 22, 12, 16); sl.setSpacing(4)
        brand = QHBoxLayout(); brand.setContentsMargins(6, 0, 0, 0); brand.setSpacing(10)
        mark = QLabel(); mark.setObjectName('mark'); mark.setFixedSize(46, 46); mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(QIcon(str(ROOT / 'LOGO.ico')).pixmap(QSize(46, 46)))
        brand.addWidget(mark)
        names = QVBoxLayout(); names.setSpacing(1)
        names.addWidget(label('拾点VFX', 'brand'))
        names.addWidget(label('SeedomVFX'))
        brand.addLayout(names); brand.addStretch(); sl.addLayout(brand); sl.addSpacing(25)
        self.pages = QStackedWidget(); self.nav_group = QButtonGroup(self)
        for i, (text, name) in enumerate((('任务', 'tasks'), ('设置', 'settings'), ('关于', 'about'))):
            nav = QPushButton('  ' + text); nav.setIcon(icon(name)); nav.setIconSize(QSize(17, 17)); nav.setObjectName('nav'); nav.setCheckable(True)
            self.nav_group.addButton(nav, i); sl.addWidget(nav)
        self.settings_dirty = False
        self.nav_group.idClicked.connect(self.navigate_page)
        self.nav_group.button(0).setChecked(True)
        sl.addStretch()
        self.sidebar_update = button('发现新版本', lambda: self.show_updates())
        self.sidebar_update.hide(); sl.addWidget(self.sidebar_update)
        sl.addWidget(label(f'版本 {APP_VERSION}'))
        shell.addWidget(side)
        main = QWidget(); right = QVBoxLayout(main); right.setContentsMargins(0, 0, 0, 0); right.setSpacing(0)
        self.solver_notice = QWidget(); self.solver_notice.setObjectName('solverNotice')
        notice = QHBoxLayout(self.solver_notice); notice.setContentsMargins(20, 12, 20, 12)
        self.solver_notice_text = label('', 'solverNoticeText'); self.solver_notice_text.setTextFormat(Qt.TextFormat.PlainText)
        notice.addWidget(self.solver_notice_text, 1)
        self.solver_notice_log = ''
        notice.addWidget(button('查看日志', lambda: self.open_path(self.solver_notice_log)))
        notice.addWidget(button('关闭提醒', self.solver_notice.hide))
        self.solver_notice.hide(); right.addWidget(self.solver_notice); right.addWidget(self.pages, 1)
        shell.addWidget(main, 1)
        self.build_workspace(); self.build_settings(); self.build_about(); inset_numeric_fields(self)
        self.hardware_label = DetailValueLabel(hardware_summary({**system_information(), 'pending': True}))
        self.hardware_label.setObjectName('muted')
        self.hardware_label.setMinimumWidth(0)
        self.hardware_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.elapsed_label = label('00:00')
        self.statusBar().addWidget(self.hardware_label, 1); self.statusBar().addPermanentWidget(self.elapsed_label)
        self.statusBar().setSizeGripEnabled(True)
        add_action = QAction(self); add_action.setShortcut(QKeySequence('Ctrl+N')); add_action.triggered.connect(self.add_task); self.addAction(add_action)

    def build_workspace(self):
        page = QWidget(); page.setObjectName('workspace'); layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0); self.pages.addWidget(page)
        header = QWidget(); header.setObjectName('header'); hl = QVBoxLayout(header); hl.setContentsMargins(24, 22, 24, 16); hl.setSpacing(17)
        top = QHBoxLayout(); titles = QVBoxLayout(); titles.setSpacing(4)
        titles.addWidget(label('任务工作区', 'title')); self.summary_label = label('统一管理重建任务、点云与 Blender 场景'); titles.addWidget(self.summary_label)
        top.addLayout(titles, 1)
        self.add_button = button('添加任务', self.add_task, True); self.add_button.setIcon(icon('plus', 'white')); top.addWidget(self.add_button)
        hl.addLayout(top)
        tools = QHBoxLayout(); tools.setSpacing(8)
        self.start_button = button('开始队列', self.start_queue, True); self.start_button.setObjectName('startQueue'); self.start_button.setIcon(icon('play', 'white')); self.start_button.setMinimumWidth(118); self.start_button.setFixedHeight(36); tools.addWidget(self.start_button)
        self.start_button.setToolTip('按执行顺序依次处理全部待执行任务')
        self.pause_button = button('暂停队列', self.toggle_pause); self.pause_button.setIcon(icon('pause')); tools.addWidget(self.pause_button)
        self.stop_button = button('停止', self.stop); self.stop_button.setIcon(icon('stop')); tools.addWidget(self.stop_button)
        saved_power_action = self.config.get('queue_power_action', 'shutdown')
        self.auto_poweroff = QCheckBox(f'完成任务后：{power_action_label(saved_power_action)}')
        self.auto_poweroff.setChecked(self.config.get('auto_poweroff_after_queue') is True)
        self.auto_poweroff.setToolTip('队列自然完成后执行设置中的关机或休眠动作；手动停止不会触发。')
        self.auto_poweroff.stateChanged.connect(self.queue_behavior_changed)
        tools.addWidget(self.auto_poweroff)
        tools.addStretch(); tools.addWidget(label('Ctrl + N  添加任务')); hl.addLayout(tools)
        filters = QHBoxLayout(); filters.setSpacing(12)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索任务名称、素材或求解方案'); self.search.setClearButtonEnabled(True); self.search.setMaximumWidth(300)
        self.search.setObjectName('taskSearch'); self.search.setFixedHeight(34)
        self.search.addAction(icon('search'), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.apply_filter); filters.addWidget(self.search, 1)
        segments = QWidget(); segments.setObjectName('segments'); seg = QHBoxLayout(segments); seg.setContentsMargins(3, 3, 3, 3); seg.setSpacing(1)
        segments.setFixedHeight(34)
        self.filters = QButtonGroup(self)
        for i, text in enumerate(('全部', '待处理', '进行中', '已完成', '异常')):
            control = QPushButton(text); control.setObjectName('segment'); control.setCheckable(True); self.filters.addButton(control, i); seg.addWidget(control)
            control.setFixedHeight(28)
        self.filters.button(0).setChecked(True); self.filters.idClicked.connect(self.apply_filter)
        filters.addWidget(segments); filters.addStretch()
        right_tools = QHBoxLayout(); right_tools.setContentsMargins(0, 0, 0, 0); right_tools.setSpacing(10)
        self.sort_box = QComboBox(); self.sort_box.setObjectName('sortBox'); self.sort_box.setFixedSize(148, 34); self.sort_box.setToolTip('任务排序')
        self.sort_box.setPlaceholderText('按表头排序')
        for title, column, order in [('执行顺序', -1, Qt.SortOrder.AscendingOrder), ('最新创建', 4, Qt.SortOrder.DescendingOrder), ('最早创建', 4, Qt.SortOrder.AscendingOrder),
                ('名称 A → Z', 0, Qt.SortOrder.AscendingOrder), ('输出大小', 3, Qt.SortOrder.DescendingOrder)]:
            self.sort_box.addItem(title, (column, order))
        self.sort_box.setCurrentIndex(0)
        self.sort_box.currentIndexChanged.connect(self.apply_sort); right_tools.addWidget(self.sort_box); right_tools.addSpacing(6)
        self.priority_up = button('', lambda: self.move_selected(-1)); self.priority_down = button('', lambda: self.move_selected(1))
        for control, direction, title in ((self.priority_up, 'up', '上移选中任务，提高执行优先级'),
                                          (self.priority_down, 'down', '下移选中任务，降低执行优先级')):
            control.setObjectName('iconButton'); control.setIcon(icon(direction)); control.setFixedSize(34, 34)
            control.setToolTip(title + '；自动切换到执行顺序'); control.setEnabled(False); right_tools.addWidget(control)
        self.refresh_button = button('', self.refresh_projects); self.refresh_button.setObjectName('iconButton'); self.refresh_button.setIcon(icon('refresh')); self.refresh_button.setToolTip('刷新历史结果'); self.refresh_button.setFixedSize(36, 34); right_tools.addWidget(self.refresh_button)
        more = button('', lambda: None); more.setObjectName('iconButton'); more.setIcon(icon('more')); more.setFixedSize(36, 34); more.setToolTip('工作区操作')
        menu = QMenu(more); menu.addAction('添加已有项目目录…', self.import_projects); menu.addAction('打开默认输出目录', lambda: self.open_path(self.defaults.output_root)); menu.addAction('运行日志…', self.open_logs)
        more.setMenu(menu); right_tools.addWidget(more); filters.addLayout(right_tools); hl.addLayout(filters); layout.addWidget(header)
        self.banner = label('', 'banner'); self.banner.hide(); layout.addWidget(self.banner)
        split = QSplitter(Qt.Orientation.Vertical); split.setChildrenCollapsible(False)
        area = QWidget(); al = QVBoxLayout(area); al.setContentsMargins(24, 16, 24, 12); al.setSpacing(8)
        self.table_area = QWidget(); self.table_area.setObjectName('taskTableArea')
        table_layout = QHBoxLayout(self.table_area); table_layout.setContentsMargins(0, 0, 0, 0); table_layout.setSpacing(0)
        self.table = TaskTableView(); self.model = TaskModel(self); self.proxy = TaskFilter(self); self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy); self.table.setItemDelegate(TaskDelegate(self.table, self.row_action))
        self.table.setViewportMargins(0, 0, StickyActions.WIDTH, 0)
        self.sticky_actions = StickyActions(self.table, self.row_action, self.table_area)
        self.table.sticky_actions = self.sticky_actions
        table_layout.addWidget(self.table, 1)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False); self.table.setWordWrap(False); self.table.setSortingEnabled(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.table.verticalHeader().hide(); self.table.verticalHeader().setDefaultSectionSize(58)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setMinimumSectionSize(75)
        for column, width in enumerate((300, 110, 125, 110, 125, 125, 142)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnHidden(6, True)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self.sort_indicator_changed)
        self.table.viewport().installEventFilter(self)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalScrollBar().valueChanged.connect(lambda *_: self.sticky_actions.update())
        self.table.horizontalScrollBar().valueChanged.connect(lambda *_: self.sticky_actions.update())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.sticky_actions.update())
        self.table.selectionModel().selectionChanged.connect(self.update_details)
        self.table.doubleClicked.connect(self.row_double_click)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        al.addWidget(self.table_area, 1)
        self.empty = QLabel(self.table.viewport()); self.empty.setObjectName('muted'); self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty.setText('还没有任务\n\n点击「添加任务」选择视频，或从右上角菜单加入已有项目目录')
        self.table.viewport().installEventFilter(self)
        self.estimate_label = label('预估任务时长：添加视频后自动估算'); al.addWidget(self.estimate_label)
        self.state_label = label('就绪', 'sectionTitle'); al.addWidget(self.state_label)
        self.progress = QProgressBar(); self.progress.setRange(0, 5); self.progress.setValue(0); self.progress.setTextVisible(False); al.addWidget(self.progress)
        self.phase_progress = QProgressBar(); self.phase_progress.setTextVisible(False)
        self.phase_progress.setToolTip('当前阶段的实际计数；无确定总数时显示等待动画。')
        self.phase_progress.hide(); al.addWidget(self.phase_progress)
        self.progress_note = label(''); self.progress_note.setWordWrap(True)
        self.progress_note.hide(); al.addWidget(self.progress_note)
        split.addWidget(area)
        detail = self.build_task_details()
        split.addWidget(detail); split.setStretchFactor(0, 1); split.setStretchFactor(1, 0); split.setSizes([450, detail.minimumHeight()])
        layout.addWidget(split, 1)

    def build_task_details(self):
        """Build the title/status row, two-column summary, and recent logs."""
        detail = QWidget(); detail.setObjectName('detail'); dl = QVBoxLayout(detail); dl.setContentsMargins(20, 12, 20, 14); dl.setSpacing(10)
        dl.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row = QHBoxLayout(); row.setSpacing(8)
        self.detail_title = DetailValueLabel('任务详情'); self.detail_title.setObjectName('sectionTitle')
        self.detail_title.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        row.addWidget(self.detail_title)
        self.detail_status = label('未选择', 'detailStatus'); row.addWidget(self.detail_status)
        row.addStretch(1)
        self.edit_button = button('编辑参数', self.edit_task); self.edit_button.setIcon(icon('edit')); row.addWidget(self.edit_button)
        self.result_button = button('打开输出目录', self.open_result); self.result_button.setIcon(icon('folder')); row.addWidget(self.result_button)
        self.blend_button = button('生成 Blender 文件…', self.export_selected); row.addWidget(self.blend_button)
        self.remove_button = button('移除任务', self.remove_selected); self.remove_button.setObjectName('danger'); row.addWidget(self.remove_button)
        self.more_button = button('更多', lambda: None); self.more_button.setMenu(self.selection_menu()); self.more_button.hide()
        dl.addLayout(row)

        def detail_field(name, title):
            cell = QWidget(); cell.setObjectName('detailField'); cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0); cell_layout.setSpacing(2)
            cell.setMinimumWidth(0); cell.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            caption = label(title, 'detailCaption')
            value = DetailValueLabel('—')
            value.setObjectName('detailValue')
            cell_layout.addWidget(caption); cell_layout.addWidget(value)
            caption.ensurePolished(); value.ensurePolished()
            caption.setMinimumHeight(caption.fontMetrics().height() + 2)
            cell.setMinimumHeight(caption.minimumHeight() + cell_layout.spacing() + value.minimumHeight())
            setattr(self, name, value)
            return cell

        content = QHBoxLayout(); content.setSpacing(24)
        summary = QWidget(); summary.setObjectName('detailSummary')
        summary.setMinimumWidth(0); summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        grid = QGridLayout(summary); grid.setContentsMargins(0, 3, 0, 0); grid.setHorizontalSpacing(20); grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        fields = (('source_path', '输入素材'), ('output_path', '输出目录'),
                  ('processing_value', '处理参数'), ('output_content', '输出内容'),
                  ('sequence_value', '序列帧'), ('created_value', '创建时间'),
                  ('duration_value', '耗时 / 结果'))
        for index, (name, title) in enumerate(fields):
            grid.addWidget(detail_field(name, title), index // 2, index % 2)
        self.detail_info = DetailMessageLabel('选择任务以查看处理状态和结果。')
        self.detail_info.setObjectName('detailMessage')
        grid.addWidget(self.detail_info, 4, 0, 1, 2)
        content.addWidget(summary, 5)

        logs_panel = QWidget(); logs_panel.setObjectName('recentLogsPanel'); logs_layout = QVBoxLayout(logs_panel)
        logs_panel.setMinimumWidth(0); logs_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        logs_layout.setContentsMargins(0, 0, 0, 0); logs_layout.setSpacing(5)
        logs_layout.addWidget(label('最近日志', 'detailCaption'))
        self.recent_logs = QPlainTextEdit(); self.recent_logs.setObjectName('recentTaskLogs'); self.recent_logs.setReadOnly(True)
        self.recent_logs.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap); self.recent_logs.setFont(QFont('Consolas', 9))
        self.recent_logs.setMaximumBlockCount(500); logs_layout.addWidget(self.recent_logs, 1)
        content.addWidget(logs_panel, 5); dl.addLayout(content, 1)
        detail.ensurePolished()
        detail.setMinimumHeight(max(220, dl.minimumSize().height()))
        return detail

    def eventFilter(self, watched, event):
        if hasattr(self, 'empty') and watched is self.table.viewport():
            self.empty.setGeometry(self.table.viewport().rect())
            if hasattr(self, 'sticky_actions'): self.sticky_actions.update()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'sticky_actions'): self.table.update_sticky_actions()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, 'sticky_actions'):
            QTimer.singleShot(0, self.table.update_sticky_actions)

    def build_settings(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); body = QWidget(); body.setObjectName('workspace'); scroll.setWidget(body)
        self.pages.addWidget(scroll); layout = QVBoxLayout(body); layout.setContentsMargins(28, 24, 28, 24); layout.setSpacing(16)
        layout.addWidget(label('设置', 'title')); layout.addWidget(label('管理本地运行环境、默认路径与日志。处理参数请在新建任务时设置。'))
        self.default_controls = {}
        def settings_form(title):
            group = QGroupBox(title); form = QFormLayout(group)
            form.setVerticalSpacing(14); form.setHorizontalSpacing(32)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return group, form
        group, form = settings_form('路径与环境')
        for key, title in [('output_root', '默认输出目录'), ('python', 'FastMap Python')]:
            control = QLineEdit(getattr(self.defaults, key)); self.default_controls[key] = control
            row = QHBoxLayout(); row.addWidget(control, 1); row.addWidget(button('浏览…', lambda checked=False, k=key: self.browse_default(k))); form.addRow(title, row)
        self.gpu_choice = QComboBox()
        self.gpu_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.gpu_choice.setMinimumContentsLength(24)
        self.gpu_choice.addItem(f'GPU {self.defaults.gpu_index}（等待检测）', self.defaults.gpu_index)
        self.default_controls['gpu_index'] = self.gpu_choice
        form.addRow('处理显卡', self.gpu_choice)
        form.addRow(label('选择用于视频 CUDA 解码、特征提取、匹配及 FastMap 的 NVIDIA 显卡。COLMAP 2.0 的相机与点云 BA 使用 CPU/Ceres。'))
        self.gpu_choice.currentIndexChanged.connect(self.check_environment)
        self.reuse_failed_projects = QCheckBox('失败任务重试时复用原项目目录')
        self.reuse_failed_projects.setChecked(self.tasks.reuse_failed_projects)
        form.addRow(self.reuse_failed_projects)
        retry_note = label('启用后，重试同名、同素材的未完成任务时复用项目目录；已完成项目始终另建目录。')
        retry_note.setWordWrap(True)
        form.addRow(retry_note)
        self.power_action = QComboBox()
        self.power_action.addItem('关机', 'shutdown')
        self.power_action.addItem('休眠', 'sleep')
        saved_action = self.config.get('queue_power_action', 'shutdown')
        saved_index = self.power_action.findData(saved_action)
        self.power_action.setCurrentIndex(saved_index if saved_index >= 0 else 0)
        self.power_action.currentIndexChanged.connect(self.update_queue_poweroff_label)
        self.update_queue_poweroff_label()
        form.addRow('完成队列后', self.power_action)
        form.addRow(label('主界面勾选“完成任务后：关机”或“完成任务后：休眠”后，队列自然完成时执行此动作；停止或异常中断不会执行。'))
        self.environment_info = label('使用随软件提供的独立运行环境。'); form.addRow(self.environment_info)
        form.addRow(button('重新检测运行环境', self.check_environment)); layout.addWidget(group)
        group, form = settings_form('求解默认项')
        self.auto_focal_default = QCheckBox('新建任务默认启用自动焦距（分段估计）')
        self.auto_focal_default.setChecked(self.defaults.auto_focal)
        self.auto_focal_default.setToolTip('适合变焦或希区柯克变焦素材；关闭时继续使用原来的单一焦距解算。新建任务中仍可单独调整。')
        self.default_controls['auto_focal'] = self.auto_focal_default
        form.addRow(self.auto_focal_default)
        form.addRow(label('开启后让 FastMap 或 COLMAP 原生 Ceres 优化变焦焦距；连续单镜头会加入平滑约束，是否包含切镜在新建任务中选择。原始数据库会保留。'))
        layout.addWidget(group)
        self.update_panel = UpdatePanel(ROOT, automatic=self.config.get('check_updates_on_startup', True) is not False,
                                       can_install=lambda: not self.busy,
                                       open_url=lambda url: QDesktopServices.openUrl(QUrl(url)))
        self.update_panel.update_available.connect(self.notify_update)
        self.update_panel.install_requested.connect(self.install_update)
        layout.addWidget(self.update_panel)
        group, form = settings_form('界面主题')
        self.theme_choice = QComboBox()
        self.theme_choice.addItem('自动（跟随系统）', 'auto')
        self.theme_choice.addItem('浅色', 'light')
        self.theme_choice.addItem('深色', 'dark')
        index = self.theme_choice.findData(self._theme_mode)
        self.theme_choice.setCurrentIndex(index if index >= 0 else 0)
        self.theme_choice.currentIndexChanged.connect(self.theme_choice_changed)
        form.addRow('主题', self.theme_choice)
        form.addRow(label('自动模式会随系统明暗主题变化。'))
        layout.addWidget(group)
        group, form = settings_form('Blender 版本')
        self.blender_cache_label = label(f'检测到 {len(self.blender_records)} 个 Blender 版本。' if self.blender_records else '启动时自动检测 Blender 版本。'); form.addRow('Blender 版本', self.blender_cache_label)
        row = QHBoxLayout(); row.setSpacing(8)
        self.blender_add_button = button('添加 Blender 安装目录…', self.add_blender_directory)
        row.addWidget(self.blender_add_button, 1)
        row.addWidget(button('重新扫描', self.cache_blender_versions))
        form.addRow(row); layout.addWidget(group)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(button('保存设置', self.save_settings_clicked, True)); layout.addLayout(row)
        self.settings_note = label(''); layout.addWidget(self.settings_note); layout.addStretch()
        for control in self.default_controls.values():
            if isinstance(control, QLineEdit): control.textChanged.connect(self.mark_settings_dirty)
            elif isinstance(control, QComboBox): control.currentIndexChanged.connect(self.mark_settings_dirty)
            elif isinstance(control, QCheckBox): control.stateChanged.connect(self.mark_settings_dirty)
        self.reuse_failed_projects.stateChanged.connect(self.mark_settings_dirty)
        self.power_action.currentIndexChanged.connect(self.mark_settings_dirty)
        self.update_panel.automatic.stateChanged.connect(self.mark_settings_dirty)
        self.theme_choice.currentIndexChanged.connect(self.mark_settings_dirty)
        for group in body.findChildren(QGroupBox):
            form = group.layout()
            if not isinstance(form, QFormLayout):
                continue
            for index in range(form.rowCount()):
                item = form.itemAt(index, QFormLayout.ItemRole.LabelRole)
                if item and item.widget(): item.widget().setMinimumWidth(190)

    def build_about(self):
        from qt_about import about_page
        self.pages.addWidget(about_page(self.open_path, lambda: self.show_updates(check=True)))

    def selected_theme(self):
        mode = self.theme_choice.currentData() if hasattr(self, 'theme_choice') else self._theme_mode
        return detect_system_theme(QApplication.instance()) if mode == 'auto' else mode

    def apply_theme(self):
        app = QApplication.instance()
        app.setStyleSheet(style_for_theme(self.selected_theme()))
        if hasattr(self, 'table'):
            delegate = self.table.itemDelegate()
            if hasattr(delegate, 'refresh_theme'):
                delegate.refresh_theme()
            self.table.viewport().update()
            self.sticky_actions.update()

    def system_theme_changed(self, *_):
        if (self.theme_choice.currentData() if hasattr(self, 'theme_choice') else self._theme_mode) == 'auto':
            self.apply_theme()

    def theme_choice_changed(self, _index=0):
        self._theme_mode = self.theme_choice.currentData()
        self.apply_theme()
        self.mark_settings_dirty()

    def navigate_page(self, index):
        current = self.pages.currentIndex()
        if current == 1 and index != current and self.settings_dirty:
            choice = self.prompt_unsaved_settings('设置页面有未保存的改动，要保存后再离开吗？')
            if choice == QMessageBox.StandardButton.Cancel:
                self.nav_group.button(current).setChecked(True)
                return
            if choice == QMessageBox.StandardButton.Save:
                if not self.save_settings_clicked():
                    self.nav_group.button(current).setChecked(True)
                    return
            else:
                self.restore_settings_controls()
        self.pages.setCurrentIndex(index)

    def mark_settings_dirty(self, *_):
        self.settings_dirty = True

    def prompt_unsaved_settings(self, message):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle('设置尚未保存')
        dialog.setText(message)
        dialog.setStandardButtons(QMessageBox.StandardButton.Save |
                                  QMessageBox.StandardButton.Discard |
                                  QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Save)
        dialog.button(QMessageBox.StandardButton.Save).setText('保存')
        dialog.button(QMessageBox.StandardButton.Discard).setText('放弃')
        dialog.button(QMessageBox.StandardButton.Cancel).setText('取消')
        return dialog.exec()

    def restore_settings_controls(self):
        controls = list(self.default_controls.values())
        for control in controls:
            control.blockSignals(True)
        self.reuse_failed_projects.blockSignals(True)
        self.power_action.blockSignals(True)
        self.update_panel.automatic.blockSignals(True)
        try:
            self.default_controls['output_root'].setText(str(self.defaults.output_root))
            self.default_controls['python'].setText(str(self.defaults.python))
            index = self.gpu_choice.findData(self.defaults.gpu_index)
            if index >= 0: self.gpu_choice.setCurrentIndex(index)
            self.reuse_failed_projects.setChecked(self.tasks.reuse_failed_projects)
            saved_action = self.config.get('queue_power_action', 'shutdown')
            index = self.power_action.findData(saved_action)
            self.power_action.setCurrentIndex(index if index >= 0 else 0)
            self.update_panel.automatic.setChecked(self.config.get('check_updates_on_startup', True) is not False)
            saved_theme = self.config.get('theme_mode', 'auto')
            index = self.theme_choice.findData(saved_theme)
            self.theme_choice.setCurrentIndex(index if index >= 0 else 0)
        finally:
            for control in controls:
                control.blockSignals(False)
            self.reuse_failed_projects.blockSignals(False)
            self.power_action.blockSignals(False)
            self.update_panel.automatic.blockSignals(False)
        self.update_queue_poweroff_label()
        self._theme_mode = self.theme_choice.currentData()
        self.apply_theme()
        self.settings_dirty = False

    def notify_update(self, version):
        self.sidebar_update.setText(f'更新至 {version}')
        self.sidebar_update.setVisible(bool(version))

    def show_updates(self, check=False):
        self.nav_group.button(1).click()
        self.pages.widget(1).ensureWidgetVisible(self.update_panel)
        if check:
            self.update_panel.check_updates()

    def install_update(self, package):
        if self.busy:
            self.update_panel.status.setText('请等待当前任务结束后安装更新。')
            return
        if QMessageBox.question(self, '安装热补丁',
                '软件将关闭并安装已校验的热补丁，完成后自动重启。\n'
                '被替换文件会自动备份，个人设置、任务记录和项目文件保留。\n\n现在安装？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            from hotfix import safe_path, validate_package
            validate_package(package, APP_VERSION)
            self.persist_tasks(); self.save_preferences()
            snapshot = safe_path(ROOT, '.updates/installer-' + uuid.uuid4().hex)
            snapshot.mkdir(parents=True)
            for name in ('hotfix.py', 'update_installer.py'):
                shutil.copyfile(APP / name, snapshot / name)
            executable = ROOT / 'runtime/python/python.exe'
            subprocess.Popen([str(executable), '-B', str(snapshot / 'update_installer.py'),
                '--target', str(ROOT), '--package', str(package), '--wait-pid', str(os.getpid())],
                cwd=str(snapshot), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.close()
        except (OSError, ValueError, SyntaxError) as error:
            self.show_error(error)

    def selection_menu(self):
        menu = QMenu(self)
        self.selection_actions = {}
        for key, title, callback in [('open_blend', '打开 Blender 文件…', self.open_blend), ('logs', '打开任务日志目录', self.open_task_logs),
            ('log_window', '查看运行日志…', self.open_logs), ('source', '打开素材位置', self.open_source), ('sequence', '打开导出序列帧', self.open_sequence),
            ('rename', '重命名', self.rename_selected), ('copy', '复制结果路径', self.copy_result), ('remove', '从队列移除所选任务', self.remove_selected),
            ('archive', '移入项目回收目录…', self.archive_selected)]:
            action = menu.addAction(title, callback); self.selection_actions[key] = action
        return menu

    def selected_rows(self):
        return [self.proxy.index(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in self.table.selectionModel().selectedRows()]

    def selected(self):
        rows = self.selected_rows(); return rows[0] if len(rows) == 1 else None

    def rebuild_rows(self):
        selected = [row['id'] for row in self.selected_rows()]
        records_by_model = {str(Path(r['result']['model']).resolve()): r for r in self.records.values()}
        rows, seen = [], set()
        jobs = self.tasks.snapshot()
        ordered_ids = [iid for iid, job in jobs.items() if job['state'] == '运行中']
        ordered_ids += [iid for iid, job in jobs.items() if job['state'] in (('排队',) if self.tasks.running else MOVABLE_STATES)]
        positions = {iid: index + 1 for index, iid in enumerate(ordered_ids)}
        for iid, job in jobs.items():
            result = job.get('result', {}); record = records_by_model.get(str(Path(result['model']).resolve())) if result.get('model') else None
            if record: seen.add(record['id'])
            options = job['options']; backend = options['backend']
            if backend == 'glomap': backend = 'colmap-cpu'
            row = {**job, 'result': result, 'record': record, 'exports': record['exports'] if record else [],
                   'date': job.get('created_at', '') or (record.get('created', '') if record else result.get('completed_at', '')), 'solver': backend,
                   'bytes': record.get('bytes') if record else None, 'queued': True, 'queue_position': positions.get(iid)}
            if record: row['name'] = record['name']
            if row['state'] == '完成' and not (record['ready'] if record else model_ready(result)):
                row['state'] = '文件不完整'
            row['settings'] = f"{options['fps']:g} fps" if options['fps'] else '原始帧率'
            row['settings'] += f" / {options.get('image_scale', 1):g}×"
            rows.append(row)
        for record in self.records.values():
            if record['id'] in seen: continue
            result = record['result']
            work = result.get('workload') or {}
            settings = result.get('estimate_settings') or {}
            fps = settings.get('fps', 0)
            rows.append(dict(id=record['id'], name=record['name'], path=record['source'], result=result,
                record=record, state='完成' if record['ready'] else '文件不完整', exports=record['exports'],
                date=record.get('created', record['completed']), solver=result.get('backend', '未知'),
                bytes=record.get('bytes'), queued=False,
                settings=(f'{fps:g} fps' if fps else '原始帧率') + f" / {settings.get('image_scale', 1):g}×"))
        rows.sort(key=lambda row: 0 if row['state'] == '运行中' else
                  1 if row.get('queued') and row['state'] in MOVABLE_STATES else 2)
        for row in rows:
            metrics = row['result'].get('metrics', {})
            row['busy'] = self.busy
            directory = (row['record']['directory'] if row.get('record') else
                         str(result_directory(row['result'])) if row['result'].get('model') else
                         row.get('scene_path') or row.get('options', {}).get('output_root'))
            row['directory'] = directory if directory and Path(directory).is_dir() else None
            display_date = row['date'][5:16].replace('T', ' ') if row['date'] else '—'
            row['cells'] = [row['name'], SOLVERS.get(row['solver'], row['solver']), row['settings'], output_size(row.get('bytes')),
                            display_date, row['state'], '']
        self.model.replace(rows)
        for index in range(self.proxy.rowCount()):
            if self.proxy.index(index, 0).data(Qt.ItemDataRole.UserRole)['id'] in selected:
                self.table.selectionModel().select(self.proxy.index(index, 0), self.table.selectionModel().SelectionFlag.Select | self.table.selectionModel().SelectionFlag.Rows)
        self.summary_label.setText(f'{len(rows)} 项任务与历史结果')
        self.update_empty(); self.update_details(); self.update_busy()

    def apply_filter(self, *_):
        self.proxy.beginFilterChange()
        self.proxy.query = self.search.text().strip().casefold(); self.proxy.state = self.filters.checkedButton().text()
        self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows); self.update_empty(); self.update_details()

    def apply_sort(self, *_):
        if hasattr(self, 'table') and self.sort_box.currentData() is not None:
            column, order = self.sort_box.currentData()
            self.table.sortByColumn(column, order)

    def sort_indicator_changed(self, column, order):
        index = next((i for i in range(self.sort_box.count()) if self.sort_box.itemData(i)[0] == column and self.sort_box.itemData(i)[1] == order), -1)
        self.sort_box.blockSignals(True); self.sort_box.setCurrentIndex(index); self.sort_box.blockSignals(False)

    def update_priority_buttons(self):
        ids = [row['id'] for row in self.selected_rows()]
        self.priority_up.setEnabled(not self.exporting and self.tasks.can_move(ids, -1))
        self.priority_down.setEnabled(not self.exporting and self.tasks.can_move(ids, 1))

    def move_selected(self, direction):
        ids = [row['id'] for row in self.selected_rows()]
        if self.exporting or not self.tasks.move(ids, direction): return
        self.sort_box.setCurrentIndex(0)
        self.persist_tasks(); self.rebuild_rows()
        selected = self.table.selectionModel().selectedRows()
        if selected: self.table.scrollTo(selected[0])
        self.state_label.setText('执行顺序已保存；当前任务完成后按新顺序继续。' if self.tasks.running else
                                '执行顺序已保存；点击「开始队列」依次处理全部待执行任务。')

    def row_action(self, row, action):
        for index in range(self.proxy.rowCount()):
            if self.proxy.index(index, 0).data(Qt.ItemDataRole.UserRole)['id'] == row['id']:
                self.table.selectRow(index)
                break
        current = self.selected()
        if not current or current['id'] != row['id']: return
        available = {key: enabled for key, _, enabled in row_actions(current)}
        if not available.get(action): return
        if action == 'edit':
            if current.get('queued') and current['state'] in ('等待', '排队', '失败', '已停止'): self.edit_task()
            else: self.rename_selected()
        elif action == 'play': self.start_queue([current['id']])
        elif action == 'folder': self.open_path(current['directory'])
        elif action == 'log': self.open_selected_log_view()
        elif action == 'trash':
            if current.get('record'): self.archive_selected()
            else: self.remove_selected()

    def open_selected_log_view(self):
        row = self.selected()
        if not row: return
        if row['state'] == '运行中': self.open_logs(); return
        path = Path(self.task_log_path())
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('任务日志 · ' + row['name']); dialog.resize(940, 580)
        layout = QVBoxLayout(dialog); layout.setContentsMargins(20, 16, 20, 20)
        head = QHBoxLayout(); head.addWidget(label('任务日志', 'sectionTitle')); head.addStretch()
        head.addWidget(button('打开完整日志目录', lambda: self.open_path(path))); layout.addLayout(head)
        layout.addWidget(label(str(path)))
        preview = QPlainTextEdit(); preview.setReadOnly(True); preview.setFont(QFont('Consolas', 10)); preview.document().setMaximumBlockCount(1500)
        preview.setPlainText('正在读取最近的日志…'); layout.addWidget(preview, 1)
        pending = queue.Queue()
        def work():
            try:
                files = sorted(path.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
                pieces = []
                for file in reversed(files):
                    with file.open('rb') as stream:
                        stream.seek(max(0, file.stat().st_size - 16384))
                        text = stream.read(16384).decode('utf-8', errors='replace')
                        pieces.append(f'\n── {file.name} ──\n' + text)
                pending.put(''.join(pieces) or '尚无日志文件。')
            except OSError as error: pending.put(str(error))
        timer = QTimer(dialog); timer.setInterval(100)
        def receive():
            if not pending.empty():
                text = pending.get_nowait()
                # Bound line length as well as total memory to keep wrapping responsive.
                text = '\n'.join(line[:2000] + ('…' if len(line) > 2000 else '') for line in text.splitlines())
                preview.setPlainText(text); preview.verticalScrollBar().setValue(preview.verticalScrollBar().maximum()); timer.stop()
        timer.timeout.connect(receive); timer.start(); threading.Thread(target=work, daemon=True).start(); dialog.show()

    def update_empty(self):
        self.empty.setVisible(self.proxy.rowCount() == 0)
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setText('没有符合筛选条件的任务\n\n试试其他关键词或状态' if self.model.rows else '还没有任务\n\n点击「添加任务」选择视频，或从右上角菜单加入已有项目目录')

    def update_details(self, *_):
        row = self.selected(); rows = self.selected_rows()
        self.update_priority_buttons()
        self.detail_title.setFullText(row['name'] if row else f'已选择 {len(rows)} 项任务' if rows else '任务详情')
        self.detail_title.setToolTip(row['name'] if row else '')
        self.edit_button.setEnabled(bool(row and row['id'] in self.tasks.jobs and row['state'] in ('等待', '排队', '失败', '已停止')))
        result = row['result'] if row else {}
        self.blend_button.setEnabled(bool(row and model_ready(result) and not self.busy))
        self.result_button.setEnabled(bool(row and result.get('model')))
        self.remove_button.setEnabled(bool(rows and all(r['id'] in self.tasks.jobs and r['state'] not in ('运行中', '排队') for r in rows)))
        self.more_button.setEnabled(bool(rows))
        self.detail_status.setText(row['state'] if row else '未选择')
        self.detail_status.setProperty('state', row['state'] if row else '未选择')
        self.detail_status.style().unpolish(self.detail_status); self.detail_status.style().polish(self.detail_status)
        if row:
            source = row.get('path', '')
            directory = row.get('directory') or (str(result_directory(result)) if result.get('model') else '')
            self.source_path.setFullText(source)
            self.output_path.setFullText(directory or ('处理中…' if row['state'] == '运行中' else '—'))
            if directory: self.output_path.setToolTip(directory)
            options = row.get('options') or {}
            params = f"{row.get('settings', '—')} · {row.get('solver', '—')}"
            if options: params += f" · SIFT {options.get('max_size', '—')} · 匹配 {options.get('overlap', '—')}"
            self.processing_value.setFullText(params)
            exports = row.get('exports', [])
            blend_names = [Path(item if isinstance(item, str) else item.get('path', '')).name
                           for item in exports if isinstance(item, str) or isinstance(item, dict) and item.get('path')]
            if not blend_names and row.get('blender') and row['state'] == '完成':
                blend_names = [f"{row['name']}_{row['blender'].get('version', '')}.blend"]
            content = '点云' + (f" · {blend_names[-1]}" if blend_names else '') if result.get('model') else ('Blender 场景' if row.get('scene_path') else '待生成')
            self.output_content.setFullText(content)
            sequence = result.get('sequence') or (str(Path(directory) / 'video') if options.get('export_sequence') and directory else '')
            self.sequence_value.setFullText('已导出' if result.get('sequence') else '生成中' if sequence and row['state'] == '运行中' else '未输出')
            if sequence: self.sequence_value.setToolTip(sequence)
            created = row.get('date', '')
            self.created_value.setFullText(created.replace('T', ' ')[:16] if created else '—')
            if created: self.created_value.setToolTip(created)
            metrics = result.get('metrics', {}); error = metrics.get('mean_reprojection_error_px')
            duration = result.get('task_wall_seconds', result.get('mapping_wall_seconds'))
            if duration is None and row['state'] == '运行中' and self.busy:
                duration = max(0, time.perf_counter() - self.started)
            elapsed_text = f'{duration / 60:.1f} 分钟' if isinstance(duration, (int, float)) else '—'
            if metrics: elapsed_text += f" · {metrics.get('registered_images', '—')} 帧 / {metrics.get('points', 0):,} 点"
            self.duration_value.setFullText(elapsed_text)
            info = f"{row.get('cells', ['', '未知'])[1]} · {row['state']}"
            if row.get('queue_position'): info += f" · 执行顺序：第 {row['queue_position']} 项"
            if metrics:
                if isinstance(error, (int, float)): info += f' · 重投影误差 {error:.3f} px'
            if row.get('error'): info += ' · ' + row['error']
            if row.get('export_error'): info += ' · Blender 导出失败：' + row['export_error']
            for warning in row.get('solver_warnings', result.get('solver_warnings', [])):
                info += ' · 求解提醒：' + warning['text']
            self.detail_info.setFullText(info)
            warning_logs = '\n'.join(w['log_path'] for w in row.get('solver_warnings', result.get('solver_warnings', [])))
            if warning_logs:
                self.detail_info.setToolTip(f'{info}\n\n{warning_logs}')
            self.refresh_recent_logs(row)
        else:
            for name in ('source_path', 'output_path', 'processing_value', 'output_content', 'sequence_value', 'created_value', 'duration_value'):
                getattr(self, name).setFullText('—')
            self.detail_info.setFullText('可批量移除未排队的任务。' if rows else '选择任务以查看处理状态和结果。')
            self.recent_logs.setPlainText('选择任务后显示最近日志。')
            self.recent_log_signature = None
        for key, action in self.selection_actions.items():
            enabled = bool(row)
            if key == 'open_blend': enabled = bool(row and row.get('exports') and not self.busy)
            elif key == 'sequence': enabled = bool(result.get('sequence'))
            elif key == 'archive': enabled = bool(row and row.get('record') and not self.busy)
            elif key == 'rename': enabled = bool(row and row['state'] not in ('运行中', '排队'))
            elif key == 'remove': enabled = bool(rows and all(r['id'] in self.tasks.jobs and r['state'] not in ('运行中', '排队') for r in rows))
            elif key == 'copy': enabled = bool(result.get('model'))
            action.setEnabled(enabled)

    def refresh_recent_logs(self, row):
        log_dir = self.task_log_path(fallback=False)
        files = sorted(log_dir.glob('*.log'), key=lambda path: path.stat().st_mtime_ns, reverse=True)[:4] if log_dir and log_dir.is_dir() else []
        signature = (row['id'], tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in files))
        if signature == self.recent_log_signature:
            return
        snippets = []
        for path in reversed(files):
            try:
                size = path.stat().st_size
                with path.open('rb') as stream:
                    stream.seek(max(0, size - 8192))
                    excerpt = stream.read(8192).decode('utf-8', errors='replace').splitlines()[-24:]
                snippets.extend(f'[{path.name}] {line[:300]}' for line in excerpt)
            except OSError:
                continue
        placeholder = '任务尚未开始，暂无日志。' if row['state'] in ('等待', '排队') else '尚无任务日志。'
        self.recent_logs.setPlainText('\n'.join(snippets) or placeholder)
        self.recent_logs.verticalScrollBar().setValue(self.recent_logs.verticalScrollBar().maximum())
        self.recent_log_signature = signature

    def context_menu(self, position):
        index = self.table.indexAt(position)
        if index.isValid() and not self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.table.selectRow(index.row())
        if self.selected_rows(): self.more_button.menu().exec(self.table.viewport().mapToGlobal(position))

    def row_double_click(self, _):
        row = self.selected()
        if row and row['id'] in self.tasks.jobs and row['state'] in ('等待', '排队', '失败', '已停止'): self.edit_task()
        elif row: self.open_result()

    def add_task(self, paths=None):
        if isinstance(paths, bool): paths = None
        self.ensure_blender_versions()
        dialog = TaskDialog(self, self.defaults, paths=paths,
                            blender_records=self.blender_records, blender_settings=self.config,
                            name_validator=self.tasks.validate_names)
        self.active_task_dialog = dialog
        try: accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally: self.active_task_dialog = None
        if not accepted: return
        data = dialog.result_data; blender = data.get('blender') if data['auto_blender'] else None
        if data['auto_blender'] and not blender: return
        names = [data['name'] + (' · ' + Path(path).stem if data['name'] and len(data['paths']) > 1 else '')
                 or Path(path).stem for path in data['paths']]
        self.tasks.validate_names(names)
        if blender:
            self.remember_blender_settings(blender)
        for path in data['paths']:
            name = data['name']
            if name and len(data['paths']) > 1: name += ' · ' + Path(path).stem
            iid = self.tasks.add(path, data['options'], name)
            if blender: self.tasks.jobs[iid]['blender'] = blender
        self.config['last_video_directory'] = str(Path(data['paths'][0]).parent)
        self.persist_tasks(); self.rebuild_rows(); self.schedule_estimate()

    def edit_task(self):
        row = self.selected()
        if not row or row['id'] not in self.tasks.jobs: return
        job = self.tasks.begin_edit(row['id'])
        if not job: return
        self.update_busy()
        try:
            self.ensure_blender_versions()
            dialog = TaskDialog(self, Options(**job['options']), job=job,
                                blender_records=self.blender_records, blender_settings=self.config,
                                name_validator=self.tasks.validate_names)
            self.active_task_dialog = dialog
            try: accepted = dialog.exec() == QDialog.DialogCode.Accepted
            finally: self.active_task_dialog = None
            if accepted:
                data = dialog.result_data
                blender = data.get('blender') if data['auto_blender'] else None
                if data['auto_blender'] and not blender: return
                name = data['name'] or Path(job['path']).stem
                self.tasks.validate_names([name], ignore_id=row['id'])
                if blender:
                    self.remember_blender_settings(blender)
                self.tasks.end_edit(dict(name=name,
                    options=asdict(data['options']), blender=blender, state=job['state'], error=''))
            else: self.tasks.end_edit()
        finally:
            if self.tasks.editing: self.tasks.end_edit()
            self.persist_tasks(); self.rebuild_rows(); self.schedule_estimate()

    def start_queue(self, ids=None):
        if self.busy: return
        if isinstance(ids, bool): ids = None
        if self.tasks.start(ids):
            self.active_progress = None
            self.phase_progress.hide(); self.progress_note.hide()
            self.estimate_generation += 1
            self.started = time.perf_counter(); self.progress.setRange(0, 5); self.progress.setValue(0); self.state_label.setText('正在启动任务队列…'); self.rebuild_rows(); self.persist_tasks()
        else: QMessageBox.information(self, '没有待处理任务', '请添加新任务，或选择等待、失败、已停止的任务。')

    def toggle_pause(self):
        self.tasks.pause(not self.tasks.paused); self.update_busy()

    def stop(self):
        if self.exporting and self.export_pipeline:
            threading.Thread(target=self.export_pipeline.cancel, daemon=True).start()
        else: self.tasks.cancel()
        self.stop_button.setEnabled(False); self.state_label.setText('正在停止子进程，已完成的缓存会保留…')

    def update_busy(self):
        self.start_button.setEnabled(not self.busy)
        self.pause_button.setEnabled(self.tasks.running)
        self.pause_button.setText('继续队列' if self.tasks.paused else '暂停队列')
        self.stop_button.setEnabled(self.busy)
        self.update_panel.refresh_actions()
        self.update_priority_buttons()
        editing = bool(self.tasks.editing)
        self.banner.setVisible(self.tasks.running and (self.tasks.paused or editing))
        self.banner.setText('正在编辑任务：当前计算继续，完成后将等待编辑结束再启动下一项。' if editing else '队列已暂停：当前计算继续完成，点击「继续队列」启动下一项。')

    def refresh_projects(self):
        if self.project_loading:
            self.project_refresh_pending = True; return
        self.project_loading = True; self.refresh_button.setEnabled(False)
        output = self.defaults.output_root
        def work():
            try:
                records, warnings = self.library.scan(output); self.events.put(dict(type='projects', records=records, warnings=warnings))
            except Exception as error: self.events.put(dict(type='projects', records=[], warnings=[str(error)]))
        threading.Thread(target=work, daemon=True).start()

    def import_projects(self):
        path = QFileDialog.getExistingDirectory(self, '添加已有项目或输出目录', self.defaults.output_root)
        if not path: return
        try: self.library.add_root(path); self.refresh_projects()
        except (ValueError, OSError) as error: self.show_error(error)

    def remove_selected(self):
        rows = self.selected_rows()
        if not rows or any(r['id'] not in self.tasks.jobs or r['state'] in ('运行中', '排队') for r in rows): return
        for row in rows: self.tasks.remove(row['id'])
        self.persist_tasks(); self.rebuild_rows(); self.schedule_estimate()

    def rename_selected(self):
        row = self.selected()
        if not row or row['state'] in ('运行中', '排队'): return
        name, ok = QInputDialog.getText(self, '重命名', '显示名称（不更改文件目录）', text=row['name'])
        if not ok or not name.strip(): return
        try:
            if row['id'] in self.tasks.jobs:
                self.tasks.validate_names([name.strip()], ignore_id=row['id'])
            elif any(other['id'] != row['id'] and other['name'].casefold() == name.strip().casefold()
                     for other in self.model.rows):
                raise ValueError('任务名称已存在，请更换名称后再重命名。')
            if row.get('record'): self.library.rename(row['record'], name); row['record']['name'] = name.strip()
            if row['id'] in self.tasks.jobs: self.tasks.rename(row['id'], name.strip()); self.persist_tasks()
            self.rebuild_rows()
        except (OSError, ValueError) as error: self.show_error(error)

    def archive_selected(self):
        row = self.selected()
        if not row or not row.get('record') or self.busy: return
        if QMessageBox.question(self, '移入项目回收目录', f"将「{row['name']}」移入输出目录的 .trash 回收文件夹？\n项目文件与缓存一并保留，原视频保留。可依据 recovery.json 恢复。") != QMessageBox.StandardButton.Yes: return
        try:
            destination = self.library.archive(row['record'])
            if row['id'] in self.tasks.jobs: self.tasks.remove(row['id']); self.persist_tasks()
            self.records.pop(row['record']['id'], None); self.rebuild_rows(); self.refresh_projects(); self.append_log(f'[项目回收] {destination}\n')
        except (ValueError, OSError) as error: self.show_error(error)

    def open_path(self, path):
        path = Path(path)
        if path.exists():
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))): self.show_error(f'无法打开：{path}')
        else: self.show_error(f'文件或目录不存在，可能已被移动：\n{path}')

    def open_result(self):
        row = self.selected()
        if row and row['result'].get('model'): self.open_path(row['record']['directory'] if row.get('record') else result_directory(row['result']))

    def open_source(self):
        row = self.selected()
        if row: self.open_path(Path(row['path']) if Path(row['path']).is_dir() else Path(row['path']).parent)

    def open_sequence(self):
        row = self.selected()
        if row and row['result'].get('sequence'): self.open_path(row['result']['sequence'])

    def copy_result(self):
        row = self.selected()
        if row: QApplication.clipboard().setText(row['result'].get('model', ''))

    def task_log_path(self, fallback=True):
        row = self.selected()
        if row:
            result = row['result']
            if result.get('log_dir') and Path(result['log_dir']).is_dir(): return Path(result['log_dir'])
            if result.get('model'):
                run = Path(result['model']).parent.parent
                scene = result.get('scene')
                if scene:
                    path = Path(scene) / 'logs' / f"{result.get('backend', 'unknown')}_{run.name}"
                    if path.exists(): return path
            scene = row.get('scene_path')
            if scene and (Path(scene) / 'log').exists(): return Path(scene) / 'log'
            if scene and (Path(scene) / 'logs').exists(): return Path(scene) / 'logs'
            project_dir = row.get('project_dir')
            if project_dir and (Path(project_dir) / 'log').is_dir(): return Path(project_dir) / 'log'
            pipeline = self.export_pipeline or self.tasks.pipeline
            if row['id'] == self.active_progress_iid and pipeline and pipeline.log_dir:
                return pipeline.log_dir
            if not fallback: return None
        elif not fallback:
            return None
        pipeline = self.export_pipeline or self.tasks.pipeline
        return pipeline.log_dir if pipeline and pipeline.log_dir else LOGS

    def open_task_logs(self): self.open_path(self.task_log_path())

    def choose_blender(self, opening=False, preferred=None, fps=None):
        self.ensure_blender_versions()
        if preferred and not any(record['path'].casefold() == str(preferred).casefold()
                                 for record in self.blender_records):
            try:
                record = probe_blender(preferred)
                self.blender_records.append(record)
            except (OSError, ValueError):
                pass
        selected = next((record for record in self.blender_records
                         if preferred and record['path'].casefold() == str(preferred).casefold()), None)
        if selected is None:
            selected = next((record for record in self.blender_records
                             if record['path'].casefold() == str(self.config.get('blender_path', '')).casefold()), None)
        if selected is None and self.blender_records:
            selected = self.blender_records[0]
        if selected is None:
            raise FileNotFoundError('未检测到 Blender。请在设置中添加 blender.exe 后重试。')
        chosen = dict(selected, fps=float(fps or self.config.get('blender_fps', 30)),
                      as_mesh=self.config.get('blender_as_mesh', True),
                      draw_points=self.config.get('blender_draw_points', True),
                      suppress_warnings=self.config.get('blender_suppress_warnings', True),
                      background_images=self.config.get('blender_background_images', True),
                      point_radius=float(self.config.get('blender_point_radius', .005)),
                      frame_start_offset=int(self.config.get('blender_frame_start_offset', 0)),
                      paths=[record['path'] for record in self.blender_records])
        if not opening:
            settings = {**self.config, 'blender_paths': chosen['paths'],
                        'blender_records': self.blender_records, '_blender_record': selected}
            dialog = BlenderDialog(self, settings, fps=chosen['fps'])
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            chosen = dialog.result_data
        self.config.update(blender_path=chosen['path'], blender_paths=chosen['paths'],
                           blender_records=self.blender_records)
        if not opening:
            self.remember_blender_settings(chosen)
        self.save_preferences()
        return chosen

    def remember_blender_settings(self, settings):
        self.config.update(blender_path=settings['path'], blender_paths=settings.get('paths', []),
            blender_records=self.blender_records, blender_fps=settings['fps'],
            blender_as_mesh=settings['as_mesh'], blender_draw_points=settings['draw_points'],
            blender_suppress_warnings=settings['suppress_warnings'],
            blender_background_images=settings['background_images'],
            blender_point_radius=settings['point_radius'],
            blender_frame_start_offset=settings.get('frame_start_offset', 0))

    def export_selected(self):
        row = self.selected()
        if not row or not model_ready(row['result']) or self.busy: return
        try: chosen = self.choose_blender()
        except OSError as error: self.show_error(error); return
        if not chosen: return
        result = row['result']; model = Path(result['model'])
        suggested = result_directory(result) / blend_filename(row['name'], chosen['version'])
        if suggested.exists(): suggested = suggested.with_stem(suggested.stem + '_' + time.strftime('%H%M%S'))
        output, _ = QFileDialog.getSaveFileName(self, '保存新的 Blender 场景', str(suggested), 'Blender (*.blend)')
        if not output: return
        if not output.lower().endswith('.blend'): output += '.blend'
        if Path(output).exists(): self.show_error('请使用新的文件名，已有 Blender 文件不会覆盖。'); return
        from blender_export import export_blender
        pipeline = Pipeline(Options(), self.events.put); self.export_pipeline = pipeline; self.exporting = True
        self.started = time.perf_counter(); self.progress.setRange(0, 0)
        self.active_progress = None; self.phase_progress.hide(); self.progress_note.hide()
        self.state_label.setText('正在启动 Blender…'); self.update_busy(); self.update_details()
        def work():
            try:
                saved = export_blender(pipeline, result, output, chosen)
                self.events.put(dict(type='blender_saved', model=str(model), output=saved, blender=chosen))
                self.events.put(dict(type='export_finished', text='Blender 文件已生成', success=True))
            except Cancelled:
                self.events.put(dict(type='export_finished', text='Blender 导出已停止，原有结果保留', success=False))
            except Exception as error:
                self.events.put(dict(type='log', text=traceback.format_exc())); self.events.put(dict(type='export_finished', text=f'Blender 导出失败：{error}'))
        threading.Thread(target=work, daemon=True).start()

    def open_blend(self):
        row = self.selected()
        if not row or not row.get('exports') or self.busy: return
        blend = row['exports'][0]; settings = self.library.data['export_settings'].get(blend, {})
        if not Path(blend).is_file(): self.show_error('Blender 文件不存在，请刷新结果列表。'); return
        try:
            chosen = self.choose_blender(opening=True, preferred=settings.get('path'))
            if chosen: subprocess.Popen([chosen['path'], '--python', str(APP / 'blender_open.py'), '--', '--blend', blend], cwd=str(ROOT))
        except (OSError, ValueError) as error: self.show_error(error)

    def append_log(self, text):
        rotate_log(LOGS / 'session.log', len(text.encode('utf-8')))
        try:
            with (LOGS / 'session.log').open('a', encoding='utf-8') as stream: stream.write(text)
        except OSError: pass
        preview = ''.join(line if len(line) < 2000 else line[:2000] + '…[完整内容见日志文件]\n' for line in text[-131072:].splitlines(keepends=True))
        self.log_lines.append(preview); self.log_chars += len(preview)
        while self.log_chars > 131072 or len(self.log_lines) > 1024:
            self.log_chars -= len(self.log_lines.popleft())
        if self.log_widget:
            cursor = self.log_widget.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(preview)
            excess = self.log_widget.document().characterCount() - 1 - 131072
            if excess > 0:
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, excess)
                cursor.removeSelectedText()
            self.log_widget.verticalScrollBar().setValue(self.log_widget.verticalScrollBar().maximum())

    def open_logs(self):
        if self.log_dialog:
            self.log_dialog.show(); self.log_dialog.raise_(); return
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('运行日志 · ' + APP_NAME); dialog.resize(960, 580)
        layout = QVBoxLayout(dialog); layout.setContentsMargins(20, 16, 20, 20)
        row = QHBoxLayout(); row.addWidget(label('运行日志', 'sectionTitle')); row.addStretch(); row.addWidget(button('打开日志目录', self.open_task_logs)); layout.addLayout(row)
        layout.addWidget(label('仅预览最近 128 KiB；完整日志持续保存到磁盘。历史任务请打开对应日志目录。'))
        text = QPlainTextEdit(); text.setReadOnly(True); text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth); text.setFont(QFont('Consolas', 10)); text.document().setMaximumBlockCount(1500)
        text.setPlainText(''.join(self.log_lines) or '尚无运行日志。'); layout.addWidget(text, 1)
        self.log_dialog, self.log_widget = dialog, text
        dialog.destroyed.connect(self.log_destroyed); dialog.show()

    def log_destroyed(self, *_): self.log_dialog = self.log_widget = None

    def schedule_estimate(self):
        self.estimate_generation += 1
        if self.background: self.estimate_timer.start()

    def estimate(self):
        jobs = [job for job in self.tasks.snapshot().values() if job['state'] in ('等待', '排队', '失败', '已停止')]
        if not jobs: self.estimate_label.setText('预估任务时长：没有待处理任务'); return
        generation = self.estimate_generation
        self.estimate_label.setText('预估任务时长：正在读取本机历史与视频信息…')
        def work():
            try:
                # Different tasks have independent settings; estimate matching groups separately.
                groups = {}
                for job in jobs: groups.setdefault(json.dumps(job['options'], sort_keys=True), []).append(job)
                texts = [estimate_jobs(group, Options(**json.loads(key))) for key, group in groups.items()]
                text = texts[0] if len(texts) == 1 else f'{len(groups)} 组独立参数 · ' + '；'.join(texts)
            except Exception as error: text = f'预估任务时长：暂不可用（{error}）'
            self.events.put(dict(type='estimate', generation=generation, text=text))
        threading.Thread(target=work, daemon=True).start()

    def check_environment(self):
        python = self.default_controls['python'].text()
        gpu_index = self.gpu_choice.currentData()
        self.environment_generation += 1
        generation = self.environment_generation
        self.environment_info.setText('正在检测运行环境…')
        def work():
            try:
                profile = get_hardware(python, force=True, gpu_index=gpu_index)
                text = describe_hardware(profile)
                colmap_ready = (BINARY_DIRS['01 GLOMAP'] / 'colmap.exe').is_file() or DEV_COLMAP.is_file()
                text += ' · 原生 COLMAP ' + ('已就绪' if colmap_ready else '待构建')
            except Exception as error:
                profile = {**system_information(), 'gpu_index': gpu_index, 'error': str(error)}
                text = '环境检测失败：' + str(error)
            self.events.put(dict(type='environment', generation=generation, profile=profile,
                                 text=text, summary=hardware_summary(profile)))
        threading.Thread(target=work, daemon=True).start()

    def update_gpu_choices(self, profile):
        selected = self.gpu_choice.currentData()
        self.gpu_choice.blockSignals(True)
        try:
            self.gpu_choice.clear()
            for fallback_index, device in enumerate(profile.get('devices', [])):
                index = device.get('index', fallback_index)
                self.gpu_choice.addItem(f"GPU {index} · {device['name']} · {device['total_bytes'] / 2**30:.1f} GiB", index)
            index = self.gpu_choice.findData(selected)
            if index < 0:
                self.gpu_choice.addItem(f'GPU {selected}（未检测到，请检查驱动或重新选择）', selected)
                index = self.gpu_choice.count() - 1
            self.gpu_choice.setCurrentIndex(index)
        finally:
            self.gpu_choice.blockSignals(False)

    def add_blender_directory(self):
        directory = QFileDialog.getExistingDirectory(self, '选择 Blender 安装目录')
        if not directory:
            return
        root = Path(directory)
        candidates = [root / 'blender.exe', *root.glob('*/blender.exe'), *root.glob('*/*/blender.exe')]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            self.show_error('所选目录中没有找到 blender.exe。请选择 Blender 安装目录。')
            return
        self.blender_add_button.setEnabled(False)
        self.blender_cache_label.setText('正在验证所选 Blender 版本…')
        def work():
            try:
                result = probe_blender(executable)
            except Exception as error:
                result = error
            self.blender_pending.put(('manual', result))
        threading.Thread(target=work, daemon=True).start()

    def cache_blender_versions(self):
        if self.blender_discovery_running:
            return
        self.blender_discovery_running = True
        settings = dict(self.config)
        if not self.blender_records:
            self.blender_cache_label.setText('正在扫描本机 Blender 版本…')
        def work():
            try:
                self.blender_pending.put(discover_blenders(settings))
            except Exception as error:
                self.blender_pending.put(error)
        threading.Thread(target=work, daemon=True).start()

    def ensure_blender_versions(self):
        """Use the startup cache; a first task may synchronously finish discovery."""
        if self.blender_records:
            return
        try:
            records = discover_blenders(dict(self.config))
        except Exception:
            records = []
        if records:
            self.blender_records = records
            self.config.update(blender_records=records,
                               blender_paths=[record['path'] for record in records],
                               blender_path=records[0]['path'])
            self.save_preferences()
        elif self.blender_records:
            self.blender_records = [record for record in self.blender_records
                                    if Path(record['path']).is_file()]
        if hasattr(self, 'blender_cache_label'):
            self.blender_cache_label.setText(f'检测到 {len(self.blender_records)} 个 Blender 版本。'
                                             if self.blender_records else '未检测到 Blender，请安装后重新扫描。')

    def poll(self):
        dirty = False; chunks = []
        try:
            blender_result = self.blender_pending.get_nowait()
        except queue.Empty:
            blender_result = None
        manual_blender_result = isinstance(blender_result, tuple) and len(blender_result) == 2 and blender_result[0] == 'manual'
        if manual_blender_result:
            self.blender_add_button.setEnabled(True)
            result = blender_result[1]
            if isinstance(result, Exception):
                self.blender_cache_label.setText(f'添加失败：{result}')
                self.show_error(result)
            else:
                records = [record for record in self.blender_records
                           if record['path'].casefold() != result['path'].casefold()]
                records.append(result)
                previous = self.blender_records
                self.blender_records = records
                self.config.update(blender_records=records,
                                   blender_paths=[record['path'] for record in records],
                                   blender_path=result['path'])
                try:
                    self.save_preferences()
                except OSError as error:
                    self.blender_records = previous
                    self.show_error(f'Blender 已验证，但设置保存失败：{error}')
                else:
                    self.blender_cache_label.setText(f'检测到 {len(records)} 个 Blender 版本；已保存 {result["version"]}。')
                    if self.active_task_dialog:
                        self.active_task_dialog.set_blender_records(records)
        elif blender_result is not None:
            self.blender_discovery_running = False
            if isinstance(blender_result, list):
                if blender_result:
                    found_paths = {record['path'].casefold() for record in blender_result}
                    self.blender_records = blender_result + [record for record in self.blender_records
                        if record['path'].casefold() not in found_paths and Path(record['path']).is_file()]
                    preferred = self.config.get('blender_path')
                    selected = next((r for r in self.blender_records
                                     if r['path'].casefold() == str(preferred or '').casefold()), self.blender_records[0])
                    self.config.update(blender_records=self.blender_records,
                                       blender_paths=[r['path'] for r in self.blender_records],
                                       blender_path=selected['path'])
                    try: self.save_preferences()
                    except OSError: pass
                else:
                    self.blender_records = [record for record in self.blender_records
                                            if Path(record['path']).is_file()]
                if hasattr(self, 'blender_cache_label'):
                    self.blender_cache_label.setText(
                        f'检测到 {len(self.blender_records)} 个 Blender 版本。'
                        if self.blender_records else '未检测到 Blender，请安装后重新扫描。')
            elif isinstance(blender_result, Exception):
                chunks.append('[Blender 检测] ' + str(blender_result) + '\n')
            if self.active_task_dialog:
                self.active_task_dialog.set_blender_records(self.blender_records)
        deadline = time.perf_counter() + .012
        for _ in range(500):
            if time.perf_counter() >= deadline: break
            try: event = self.events.get_nowait()
            except queue.Empty: break
            kind, iid = event['type'], event.get('iid')
            if kind == 'log':
                chunks.append(event['text'])
                if iid == self.active_progress_iid:
                    self.progress_log_at = time.monotonic()
            elif kind == 'solver_warning':
                job = self.tasks.snapshot().get(iid, {})
                self.solver_notice_text.setText(f"求解提醒 · {job.get('name', 'COLMAP')}\n{event['text']}")
                self.solver_notice_log = event['log_path']
                self.solver_notice_text.setToolTip(event['log_path'])
                self.solver_notice.show()
                self.persist_tasks(); dirty = True
            elif kind == 'stage':
                if not self.active_progress or iid != self.active_progress_iid:
                    self.state_label.setText(event['text'] + f" · 阶段 {event['index']}/{event['total']}")
                    self.progress.setRange(0, 5)
                    self.progress.setValue(event['index'] - 1)
                with self.tasks.condition:
                    if iid in self.tasks.jobs: self.tasks.jobs[iid].update(stage=event['text'], progress=event['index'] - 1)
                dirty = True
            elif kind == 'task_progress':
                previous_iid = self.active_progress_iid
                self.active_progress, self.active_progress_iid = event, iid
                self.progress_log_at = max(event['last_log'], self.progress_log_at if iid == previous_iid else 0)
                detail = ' · ' + event['detail'] if event['detail'] else ''
                self.state_label.setText(f"阶段 {event['step']}/{event['steps']} · {event['text']}{detail}")
                self.progress.setRange(0, 1000); self.progress.setValue(round(event['overall'] * 1000))
                self.progress.setToolTip('按流程阶段推进，不代表耗时百分比。')
                fraction = event['fraction']
                self.phase_progress.setRange(0, 0 if fraction is None else 1000)
                if fraction is not None: self.phase_progress.setValue(round(fraction * 1000))
                self.phase_progress.show(); self.progress_note.show()
                with self.tasks.condition:
                    if iid in self.tasks.jobs:
                        self.tasks.jobs[iid].update(stage=event['text'] + detail, progress=event['overall'] * 5)
                dirty = True
            elif kind == 'blender_progress':
                self.state_label.setText(event['text'])
                percent = event['percent']
                self.progress.setRange(0, 100 if percent else 0)
                if percent: self.progress.setValue(percent)
                with self.tasks.condition:
                    if iid in self.tasks.jobs:
                        self.tasks.jobs[iid]['stage'] = event['text']; dirty = True
            elif kind == 'scene':
                self.last_scene = event['path']
                with self.tasks.condition:
                    if iid in self.tasks.jobs: self.tasks.jobs[iid]['scene_path'] = event['path']
                self.persist_tasks()
            elif kind == 'job':
                dirty = True
                if event.get('state') == '运行中':
                    self.active_progress = None; self.active_progress_iid = iid
                    self.progress_log_at = time.monotonic()
                    self.phase_progress.hide(); self.progress_note.hide()
                    self.estimate_label.setText('当前任务剩余时间将按阶段进度实时更新；此估时不含后续排队任务。')
                    self.estimate_generation += 1
                elif iid == self.active_progress_iid:
                    self.active_progress = None; self.phase_progress.hide()
                    if event.get('state') == '完成':
                        self.progress.setRange(0, 1000); self.progress.setValue(1000)
                    self.progress_note.setText(event.get('state', ''))
                if 'result' in event:
                    result = event['result']; job = self.tasks.snapshot().get(iid, {})
                    report = result_report(result)
                    try:
                        self.library.data['names'][str(report.resolve())] = job.get('name', Path(result['source']).stem)
                        if job.get('created_at'): self.library.data['created'][str(report.resolve())] = job['created_at']
                        self.library.add_root(job.get('options', {}).get('output_root', self.defaults.output_root)); self.library.save()
                    except (ValueError, OSError) as error: chunks.append(f'[项目名称保存失败] {error}\n')
                self.persist_tasks()
            elif kind == 'complete':
                if not self.active_progress:
                    self.progress.setRange(0, 5); self.progress.setValue(5)
                metrics = event.get('metrics', {})
                chunks.append(f"[完成] {metrics.get('registered_images', '?')} 注册帧 / {metrics.get('points', '?')} 点\n{event['model']}\n")
            elif kind in ('finished', 'export_finished'):
                self.active_progress = None; self.phase_progress.hide(); self.progress_note.hide()
                if kind == 'export_finished':
                    self.progress.setRange(0, 5)
                    self.exporting = False; self.export_pipeline = None
                    self.progress.setValue(5 if event.get('success') else 0)
                self.state_label.setText(event['text']); self.persist_tasks(); self.refresh_projects(); self.schedule_estimate(); dirty = True
                if kind == 'finished' and event.get('completed') and self.auto_poweroff.isChecked():
                    action = self.power_action.currentData() or self.config.get('queue_power_action', 'shutdown')
                    self.state_label.setText(f"{event['text']} · 即将{power_action_label(action)}")
                    QTimer.singleShot(800, lambda a=action: self.perform_queue_power_action(a))
            elif kind == 'projects':
                self.project_loading = False; self.refresh_button.setEnabled(True)
                self.records = {r['id']: r for r in event['records']}
                if event['warnings']: chunks.append('[项目读取] ' + '\n'.join(event['warnings']) + '\n')
                dirty = True
                if self.project_refresh_pending: self.project_refresh_pending = False; self.refresh_projects()
            elif kind == 'blender_saved':
                try: self.library.record_export(event['model'], event['output'], event['blender'])
                except OSError as error: chunks.append(f'[Blender 已保存，记录失败] {error}\n')
                chunks.append(f"[Blender] 已保存 {event['output']}\n")
            elif kind == 'environment':
                if event.get('generation', self.environment_generation) != self.environment_generation:
                    continue
                if 'profile' in event:
                    self.update_gpu_choices(event['profile'])
                self.hardware_label.setFullText(event['summary'])
                self.hardware_label.setToolTip(f"{event['summary']}\n{event['text']}")
                self.environment_info.setText(event['text']); self.schedule_estimate()
            elif kind == 'estimate' and event['generation'] == self.estimate_generation and not self.busy:
                self.estimate_label.setText(event['text']); self.estimate_label.setToolTip(event['text'])
            elif kind == 'queue_wait': self.update_busy()
        if chunks: self.append_log(''.join(chunks))
        if dirty: self.rebuild_rows()
        if self.busy:
            elapsed = int(time.perf_counter() - self.started); self.elapsed_label.setText(f'{elapsed // 60:02}:{elapsed % 60:02}')
            if self.active_progress:
                note = live_remaining(self.active_progress, last_log=self.progress_log_at)
                if self.progress_note.text() != note: self.progress_note.setText(note)

    def browse_default(self, key):
        control = self.default_controls[key]
        path = QFileDialog.getExistingDirectory(self, '默认输出目录', control.text()) if key == 'output_root' else QFileDialog.getOpenFileName(self, '选择 Python', control.text(), 'Python (python.exe)')[0]
        if path: control.setText(path)

    def read_defaults(self):
        values = asdict(self.defaults)
        for key, control in self.default_controls.items():
            if isinstance(control, QCheckBox): values[key] = control.isChecked()
            elif isinstance(control, QComboBox): values[key] = control.currentData()
            elif isinstance(control, QLineEdit): values[key] = control.text().strip()
            else: values[key] = control.value()
        options = Options(**values); Pipeline(options).preflight()
        if not options.output_root: raise ValueError('默认输出目录不能为空。')
        return options

    def save_preferences(self):
        data = {**self.config, **asdict(self.defaults), 'distribution_root': str(ROOT)}
        data.pop('log_retention_days', None)
        data['blender_records'] = self.blender_records
        for key in ('python', 'output_root'): data[key] = portable_path(data[key])
        data['backend_label'] = SOLVER_CHOICES[self.defaults.backend]
        data['camera_label'] = '近似针孔（无畸变）' if self.defaults.camera_mode == 'pinhole' else '自动估计畸变（默认）'
        if hasattr(self, 'auto_poweroff'):
            data['auto_poweroff_after_queue'] = self.auto_poweroff.isChecked()
        if hasattr(self, 'power_action'):
            data['queue_power_action'] = self.power_action.currentData() or 'shutdown'
        data['theme_mode'] = self.config.get('theme_mode', self._theme_mode)
        atomic_write_json(self.settings_path, data)
        self.config = data

    def queue_behavior_changed(self, _state=0):
        """Persist the main-window opt-in without requiring a settings visit."""
        try:
            self.save_preferences()
        except OSError as error:
            self.state_label.setText(f'自动关机设置保存失败：{error}')

    def update_queue_poweroff_label(self, _index=0):
        action = self.power_action.currentData() if hasattr(self, 'power_action') else self.config.get('queue_power_action', 'shutdown')
        self.auto_poweroff.setText(f'完成任务后：{power_action_label(action)}')

    def perform_queue_power_action(self, action):
        if self.busy or not self.auto_poweroff.isChecked():
            return
        try:
            self.save_preferences()
            execute_power_action(action)
        except OSError as error:
            self.auto_poweroff.setChecked(False)
            self.state_label.setText(f'未执行{power_action_label(action)}：{error}')
            self.append_log(f'[队列完成动作] {error}\n')

    def save_settings_clicked(self):
        try:
            self.defaults = self.read_defaults()
            self.config.update(reuse_failed_projects=self.reuse_failed_projects.isChecked(),
                               check_updates_on_startup=self.update_panel.automatic.isChecked(),
                               auto_poweroff_after_queue=self.auto_poweroff.isChecked(),
                               queue_power_action=self.power_action.currentData() or 'shutdown',
                               theme_mode=self.theme_choice.currentData() or 'auto')
            self.save_preferences()
            self.tasks.reuse_failed_projects = self.reuse_failed_projects.isChecked()
            self.settings_dirty = False
            self.settings_note.setText('设置已保存。路径、处理显卡与运行环境偏好将用于后续新建的任务。'); self.refresh_projects()
            return True
        except (OSError, ValueError) as error: self.show_error(error)
        return False

    def persist_tasks(self):
        try:
            data = list(self.tasks.snapshot().values())
            for job in data:
                for key in ('python', 'output_root'): job['options'][key] = portable_path(job['options'][key])
                for key in ('project_dir', 'scene_path'):
                    if job.get(key): job[key] = portable_path(job[key])
            atomic_write_json(self.config_dir / 'task-queue.json', data)
        except OSError as error: self.state_label.setText(f'任务记录保存失败：{error}')

    def restore_tasks(self):
        jobs, warnings = restore_jobs(self.config_dir / 'task-queue.json', self.config.get('distribution_root'))
        self.tasks.jobs.update(jobs)
        for warning in warnings:
            self.events.put(dict(type='log', text=f'[任务恢复] {warning}\n'))

    def show_error(self, error): QMessageBox.warning(self, '无法完成操作', str(error))

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, '任务仍在运行', '请先停止任务，并等待后台进程退出后关闭。'); event.ignore(); return
        if self.update_panel.downloading:
            self.update_panel.cancel_event.set()
            self.update_panel.status.setText('正在取消下载，完成后关闭软件…')
            QTimer.singleShot(250, self.close)
            event.ignore(); return
        if self.settings_dirty:
            choice = self.prompt_unsaved_settings('设置页面有未保存的改动，要保存后退出吗？')
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore(); return
            if choice == QMessageBox.StandardButton.Save and not self.save_settings_clicked():
                event.ignore(); return
            if choice == QMessageBox.StandardButton.Discard:
                self.restore_settings_controls()
        try:
            self.persist_tasks()
            # Save current defaults using the explicit settings action; preserve unfinished form edits.
            self.save_preferences()
        except OSError as error:
            self.show_error(error); event.ignore(); return
        self.poller.stop(); event.accept()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME + ' · Qt workspace')
    parser.add_argument('--check-ui', action='store_true'); parser.add_argument('--smoke-test', type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure_application(app)
    window = MainWindow(); window.show()
    if args.check_ui or args.smoke_test:
        app.processEvents()
        assert window.pages.count() == 3
        if args.smoke_test:
            window.refresh_projects()
            deadline = time.monotonic() + 10
            while window.project_loading and time.monotonic() < deadline:
                app.processEvents(); time.sleep(.01)
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            for index, suffix in ((0, ''), (1, '-settings'), (2, '-about')):
                window.pages.setCurrentIndex(index); window.nav_group.button(index).setChecked(True); app.processEvents()
                window.grab().save(str(args.smoke_test.with_name(args.smoke_test.stem + suffix + '.png')))
        print('QT_UI_PASS', window.width(), window.height(), flush=True)
        window.poller.stop(); window.hide(); return
    sys.exit(app.exec())


def configure_application(app):
    app.setApplicationName(APP_NAME); app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(str(ROOT / 'LOGO.ico')))
    # The offscreen test platform has no OS font discovery on Windows.
    if not QFontDatabase.families():
        for name in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf', 'consola.ttf'):
            QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name))
    app.setStyle('Fusion'); app.setFont(QFont('Microsoft YaHei UI', 9)); app.setStyleSheet(STYLE)


if __name__ == '__main__': main()
