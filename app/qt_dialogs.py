"""Native Qt task configuration, Blender preferences, and bounded log windows."""
from dataclasses import asdict
from pathlib import Path
import queue
import threading

from PySide6.QtCore import QTimer, Qt, QUrl, Signal, QPoint
from PySide6.QtGui import QPixmap, QPainter, QColor, QPolygon, QFontMetrics
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QFormLayout, QGroupBox, QScrollArea, QWidget, QApplication,
    QDoubleSpinBox, QSpinBox, QPlainTextEdit, QFileDialog, QSizePolicy, QFrame,
    QButtonGroup, QStackedLayout)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
except ImportError:  # Keep the task dialog usable in minimal Qt installations.
    QMediaPlayer = QAudioOutput = QVideoSink = None

from tracker_core import Options, Pipeline, VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
from workload import video_info, image_sequence_info
from blender_runtime import discover_blenders, probe_blender
from qt_style import inset_numeric_fields
from app_info import SOLVER_CHOICES
from solver_quality import SOLVER_PROFILES, quality_note


def label(text, name='muted'):
    result = QLabel(text)
    result.setObjectName(name)
    result.setWordWrap(True)
    return result


def button(text, callback, primary=False):
    result = QPushButton(text)
    if primary:
        result.setObjectName('primary')
    result.clicked.connect(callback)
    return result


def add_blender_record(combo, record):
    version = str(record.get('version') or Path(record.get('path', '')).parent.name)
    combo.addItem(version, record)
    combo.setItemData(combo.count() - 1, record.get('path', ''), Qt.ItemDataRole.ToolTipRole)


def form_section(title):
    """Create one predictable card section for the task form.

    QGroupBox titles are painted in the widget margin and are easy to clip
    when a scroll area or a high DPI style recalculates geometry.  A normal
    title label inside a card keeps the visual hierarchy and row geometry
    entirely under our control.
    """
    card = QFrame()
    card.setObjectName('taskSection')
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(14, 12, 14, 14)
    card_layout.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName('taskSectionTitle')
    card_layout.addWidget(heading)
    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(9)
    form.setHorizontalSpacing(14)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    card_layout.addLayout(form)
    return card, form


class VideoInput(QFrame):
    """Read-only material path display.

    The source is chosen through the drop zone and file pickers.  Keeping a
    small display widget instead of an editor prevents accidental edits and
    removes the large text-box look from the task form while retaining the
    existing ``toPlainText``/``setPlainText`` API used by the dialog.
    """
    textChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName('sourceInput')
        self.setAcceptDrops(True)
        self._value = ''
        self._placeholder = '请选择视频或图像序列文件夹'
        self.setFixedHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.display = QLabel(self._placeholder)
        self.display.setObjectName('sourcePath')
        self.display.setWordWrap(False)
        self.display.setMinimumWidth(0)
        self.display.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.display.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.display)

    def setPlaceholderText(self, text):
        self._placeholder = str(text)
        if not self._value:
            self.display.setText('尚未选择素材')
            self.display.setToolTip(self._placeholder)

    def setPlainText(self, text):
        self._value = str(text or '').strip()
        paths = [line.strip() for line in self._value.splitlines() if line.strip()]
        if len(paths) > 1:
            shown = f'{paths[0]}  ·  另有 {len(paths) - 1} 个素材'
        else:
            shown = paths[0] if paths else '尚未选择素材'
        self.display.setToolTip(self._value or self._placeholder)
        self.display.setText(QFontMetrics(self.display.font()).elidedText(
            shown, Qt.TextElideMode.ElideMiddle, max(40, self.display.width())))
        self.display.setProperty('empty', not bool(self._value))
        self.display.style().unpolish(self.display)
        self.display.style().polish(self.display)
        self.textChanged.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._value:
            self.setPlainText(self._value)

    def toPlainText(self):
        return self._value

    def setReadOnly(self, _read_only):
        # Kept for compatibility with the edit-task path.
        return None

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            values = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            self.setPlainText('\n'.join(values))
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class NoWheelComboBox(QComboBox):
    """Do not change a selection merely because the pointer is over the field."""
    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class QualitySelector(QWidget):
    """Four fixed quality segments matching the task mockup."""
    selectionChanged = Signal()

    def __init__(self, entries):
        super().__init__()
        self._entries = list(entries)
        self._buttons = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(1)
        for index, (title, value) in enumerate(self._entries):
            control = QPushButton(title)
            # Reuse the same segmented selector used by the main task list.
            # Keeping one object name makes the task dialog and the home page
            # render identically and avoids a second, subtly different style.
            control.setObjectName('segment')
            control.setCheckable(True)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            control.setProperty('segmentIndex', index)
            control.clicked.connect(lambda checked, i=index: self._select(i))
            self._group.addButton(control, index)
            self._buttons.append(control)
            layout.addWidget(control, 1)
        if self._buttons:
            self._buttons[0].setChecked(True)
        self.setObjectName('segments')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _select(self, index):
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)
            self.selectionChanged.emit()

    def currentIndex(self):
        return next((i for i, control in enumerate(self._buttons) if control.isChecked()), -1)

    def currentData(self):
        index = self.currentIndex()
        return self._entries[index][1] if index >= 0 else None

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)

    def setOptionEnabled(self, index, enabled):
        if 0 <= index < len(self._buttons):
            self._buttons[index].setEnabled(bool(enabled))

    class _Model:
        """Small compatibility view for callers that used QComboBox.model()."""
        def __init__(self, buttons):
            self._buttons = buttons

        def item(self, index):
            return self._buttons[index] if 0 <= index < len(self._buttons) else None

    def model(self):
        return self._Model(self._buttons)


class PreviewPlayButton(QPushButton):
    """Vector play/pause control that remains visible across Windows fonts."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('previewPlay')
        self.setFixedSize(54, 54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty('playing', False)

    def set_playing(self, playing):
        self.setProperty('playing', bool(playing))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        radius = min(self.width(), self.height()) // 2 - 1
        painter.setBrush(QColor(255, 255, 255, 45))
        painter.setPen(QColor(255, 255, 255, 170))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235))
        if self.property('playing'):
            painter.drawRect(center.x() - 8, center.y() - 10, 5, 20)
            painter.drawRect(center.x() + 3, center.y() - 10, 5, 20)
        else:
            painter.drawPolygon(QPolygon([
                QPoint(center.x() - 5, center.y() - 11),
                QPoint(center.x() + 10, center.y()),
                QPoint(center.x() - 5, center.y() + 11),
            ]))


class DropZone(QFrame):
    """Compact blue drag-and-drop area used by the task dialog."""
    def __init__(self, file_callback, folder_callback, sequence_callback, drop_callback=None):
        super().__init__()
        self.setObjectName('dropZone')
        self.setAcceptDrops(True)
        self.file_callback = file_callback
        self.folder_callback = folder_callback
        self.sequence_callback = sequence_callback
        self.drop_callback = drop_callback
        layout = QVBoxLayout(self); layout.setContentsMargins(14, 10, 14, 10); layout.setSpacing(8)
        top = QHBoxLayout(); top.setSpacing(10)
        icon_box = QLabel('↓'); icon_box.setObjectName('dropIcon'); icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_box.setFixedSize(42, 42); top.addWidget(icon_box)
        text = QVBoxLayout(); text.setSpacing(1)
        title = QLabel('拖入图片、视频或文件夹'); title.setObjectName('dropTitle')
        note = QLabel('单个素材直接处理，多个素材自动加入批量队列'); note.setObjectName('dropNote')
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        text.addWidget(title); text.addWidget(note)
        top.addLayout(text, 1)
        layout.addLayout(top)
        actions = QHBoxLayout(); actions.setSpacing(7); actions.addStretch()
        self.file_button = QPushButton('选择文件'); self.file_button.setObjectName('dropAction')
        self.folder_button = QPushButton('选择文件夹批量'); self.folder_button.setObjectName('dropAction')
        self.sequence_button = QPushButton('选择序列文件夹'); self.sequence_button.setObjectName('dropAction')
        for control in (self.file_button, self.folder_button, self.sequence_button):
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.file_button.clicked.connect(self.file_callback)
        self.folder_button.clicked.connect(self.folder_callback)
        self.sequence_button.clicked.connect(self.sequence_callback)
        actions.addWidget(self.file_button); actions.addWidget(self.folder_button); actions.addWidget(self.sequence_button)
        layout.addLayout(actions)

    def set_sequence_mode(self, enabled):
        self.file_button.setVisible(not enabled)
        self.folder_button.setVisible(not enabled)
        self.sequence_button.setVisible(enabled)

    def _drop_paths(self, event):
        if not event.mimeData().hasUrls():
            return False
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            return False
        if self.drop_callback:
            self.drop_callback(paths)
        event.acceptProposedAction()
        return True

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not self._drop_paths(event): super().dropEvent(event)


class TaskDialog(QDialog):
    def __init__(self, parent, options, job=None, paths=None, blender_records=None, blender_settings=None,
                 name_validator=None, hardware_profile=None):
        super().__init__(parent)
        self.setObjectName('taskDialog')
        self.setWindowTitle('编辑解算任务' if job else '新建解算任务')
        self.setFixedSize(1000, 870)
        self.result_data = None
        self.job = job
        self.name_validator = name_validator
        self.blender_records = list(blender_records or [])
        self.blender_settings = dict(blender_settings or {})
        self.blender_scan_pending = queue.Queue()
        self.hardware_profile = dict(hardware_profile or {})
        self.base = options
        self.metadata = {}
        self.pending = queue.Queue()
        self.generation = 0
        self.probing = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        head = QWidget()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(24, 20, 24, 14)
        hl.addWidget(label(self.windowTitle(), 'sectionTitle'))
        hl.addWidget(label('选择素材、处理方式和输出内容。'))
        root.addWidget(head)
        # The task form owns the only scroll area.  The preview is a fixed
        # side panel: a wide source frame can be letterboxed without stealing
        # width from the controls or moving fields when the window repaints.
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName('taskFormScroll')
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName('taskBody')
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 0, 18, 16)
        layout.setSpacing(12)
        scroll.setWidget(body)
        content_layout.addWidget(scroll, 1)
        preview = QWidget(); preview.setObjectName('taskPreview')
        preview.setFixedWidth(400)
        preview_layout = QVBoxLayout(preview); preview_layout.setContentsMargins(20, 22, 20, 20); preview_layout.setSpacing(12)
        preview_layout.addWidget(label('素材预览', 'sectionTitle'))
        self.preview_player = None
        self.preview_video = None
        self.preview_sink = None
        self.preview_image = None
        stage = QWidget(); stage.setObjectName('previewStage')
        # The preview is deliberately fixed: a wide source frame must be
        # letterboxed inside this canvas instead of stealing width from the
        # processing controls on the left.
        stage.setFixedSize(360, 205)
        stage_layout = QStackedLayout(stage)
        stage_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.preview_video = QLabel()
        self.preview_video.setObjectName('taskPreviewCanvas')
        self.preview_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_video.setScaledContents(False)
        stage_layout.addWidget(self.preview_video)
        overlay = QWidget(); overlay.setObjectName('previewOverlay')
        overlay_layout = QVBoxLayout(overlay); overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.addStretch()
        self.preview_message = QLabel('尚未选择素材')
        self.preview_message.setObjectName('previewMessage')
        self.preview_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(self.preview_message)
        self.preview_play = PreviewPlayButton()
        self.preview_play.setToolTip('播放 / 暂停预览')
        self.preview_play.clicked.connect(self.toggle_preview)
        play_row = QHBoxLayout(); play_row.addStretch(); play_row.addWidget(self.preview_play); play_row.addStretch()
        overlay_layout.addLayout(play_row)
        overlay_layout.addStretch()
        stage_layout.addWidget(overlay)
        overlay.raise_()
        preview_layout.addWidget(stage, 0, Qt.AlignmentFlag.AlignHCenter)
        if QMediaPlayer is not None and QVideoSink is not None:
            self.preview_audio = QAudioOutput(self)
            self.preview_player = QMediaPlayer(self)
            self.preview_sink = QVideoSink(self)
            self.preview_player.setAudioOutput(self.preview_audio)
            self.preview_player.setVideoOutput(self.preview_sink)
            self.preview_sink.videoFrameChanged.connect(self._preview_frame)
            self.preview_player.playbackStateChanged.connect(self._preview_state_changed)
            self.preview_player.errorOccurred.connect(lambda *_: self._preview_error())
        self.preview_play.setVisible(False)
        self.preview_info = label('选择素材后显示基本信息。')
        preview_layout.addWidget(self.preview_info)
        self.preview_acceleration = label('硬件加速：按任务设置', 'taskAcceleration')
        preview_layout.addWidget(self.preview_acceleration)
        preview_layout.addStretch()
        content_layout.addWidget(preview, 0)
        root.addWidget(content, 1)
        basic, form = form_section('基本信息')
        self.input_mode = NoWheelComboBox()
        self.input_mode.addItem('视频文件', 'video')
        self.input_mode.addItem('图像序列文件夹', 'image_sequence')
        saved_input_type = options.input_type or ('image_sequence' if job and Path(job.get('path', '')).is_dir() else 'video')
        mode_index = self.input_mode.findData(saved_input_type)
        self.input_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        self.input_mode.setToolTip('图像序列会直接作为输入图像目录，跳过 FFmpeg 视频拆分。')
        form.addRow('输入类型', self.input_mode)
        self.name = QLineEdit(job.get('name', '') if job else '')
        self.name.setPlaceholderText('留空使用素材名称')
        form.addRow('任务名称', self.name)
        self.source = VideoInput()
        self.source.setObjectName('sourceInput')
        self.source.setPlainText(job['path'] if job else '\n'.join(map(str, paths or [])))
        if job:
            self.source.setReadOnly(True)
        self.drop_zone = DropZone(
            self.browse, self.browse_folder, self.browse_sequence,
            lambda values: self.source.setPlainText('\n'.join(values)),
        )
        self.drop_zone.setEnabled(not bool(job))
        form.addRow(self.drop_zone)
        form.addRow('输入素材路径', self.source)
        self.video_note = label('正在读取素材信息…' if self.source.toPlainText() else '支持视频文件或图像序列文件夹')
        form.addRow('', self.video_note)
        self.output = QLineEdit(options.output_root)
        outrow = QHBoxLayout()
        outrow.addWidget(self.output)
        outrow.addWidget(button('浏览…', self.browse_output))
        form.addRow('输出根目录', outrow)
        layout.addWidget(basic)
        processing, form = form_section('处理设置')
        self.backend = NoWheelComboBox()
        for key, text in SOLVER_CHOICES.items(): self.backend.addItem(text, key)
        index = self.backend.findData(options.backend)
        self.backend.setCurrentIndex(index if index >= 0 else 0)
        form.addRow('求解方案', self.backend)
        self.smart_lens = QCheckBox('智能变焦/切镜优化（推荐开启）')
        # New tasks default to the user-facing smart mode.  Older task records
        # are migrated by task_storage and retain their previous choice.
        legacy_smart = bool(getattr(options, 'auto_focal', False) or
                            getattr(options, 'contains_cuts', False))
        self.smart_lens.setChecked(bool(getattr(options, 'smart_lens', legacy_smart)))
        self.smart_lens.setToolTip('自动识别变焦和切镜，并使用适合的相机内参优化。')
        form.addRow(self.smart_lens)
        acceleration_row = QHBoxLayout()
        self.acceleration = QCheckBox('启用 GPU 硬件加速')
        self.acceleration.setChecked(bool(options.use_gpu))
        self.acceleration.setToolTip('关闭后使用 CPU 完成特征、匹配和全局求解；开启后按所选显卡运行。')
        acceleration_row.addWidget(self.acceleration)
        self.gpu_choice = NoWheelComboBox()
        self.gpu_choice.setMinimumWidth(210)
        for fallback_index, device in enumerate(self.hardware_profile.get('devices', [])):
            index = device.get('index', fallback_index)
            total = device.get('total_bytes') or 0
            self.gpu_choice.addItem(f"GPU {index} · {device.get('name', '未知')} · {total / 2**30:.1f} GiB", index)
        if not self.gpu_choice.count():
            self.gpu_choice.addItem(f'GPU {options.gpu_index}（未检测）', options.gpu_index)
        selected_gpu = self.gpu_choice.findData(options.gpu_index)
        self.gpu_choice.setCurrentIndex(selected_gpu if selected_gpu >= 0 else 0)
        acceleration_row.addWidget(self.gpu_choice, 1)
        form.addRow('硬件加速', acceleration_row)
        self.quality = QualitySelector([
            ('默认', None), ('高', (4096, 15, 'high')),
            ('中', (2048, 10, 'balanced')), ('低', (1024, 5, 'fast')),
        ])
        form.addRow('计算精度', self.quality)
        self.quality_note = label(quality_note(options.solver_quality))
        self.quality.setToolTip(self.quality_note.text())
        self.rate = QualitySelector([
            ('默认', 0), ('30', 30), ('60', 60), ('自定义', -1),
        ])
        current = options.fps
        self.rate.setCurrentIndex(0 if not current else 1 if current == 30 else 2 if current == 60 else 3)
        self.custom_rate = QDoubleSpinBox()
        self.custom_rate.setRange(.01, 240)
        self.custom_rate.setDecimals(3)
        self.custom_rate.setValue(current or 24)
        self.custom_rate.setSuffix(' fps')
        raterow = QHBoxLayout()
        raterow.addWidget(self.rate, 1)
        raterow.addWidget(self.custom_rate)
        form.addRow('处理帧率', raterow)
        self.scale = QualitySelector([
            ('默认', 1.), ('0.8×', .8), ('0.5×', .5), ('0.2×', .2),
        ])
        index = next((i for i, value in enumerate((1., .8, .5, .2))
                      if abs(value - options.image_scale) < 1e-6), 0)
        self.scale.setCurrentIndex(index)
        form.addRow('处理分辨率', self.scale)
        self.size_note = label('视频由 FFmpeg 缩放；图像序列直接使用原始图片。')
        self.scale.setToolTip(self.size_note.text())
        layout.addWidget(processing)
        output, form = form_section('输出设置')
        self.sequence = QCheckBox('计算完成后导出 JPG 序列帧')
        self.sequence.setChecked(options.export_sequence)
        self.sequence_size = NoWheelComboBox()
        self.sequence_size.addItem('跟随处理分辨率', False)
        self.sequence_size.addItem('原始视频分辨率 · 重新抽帧', True)
        self.sequence_size.setCurrentIndex(int(options.sequence_original))
        form.addRow(self.sequence)
        form.addRow('序列帧尺寸', self.sequence_size)
        self.blend = QCheckBox('计算完成后生成 Blender 文件')
        self.blend.setChecked(bool(job and job.get('blender')))
        form.addRow(self.blend)
        self.blender_group, blender_form = form_section('Blender 导出设置')
        self.blender_note = label(f'检测到 {len(self.blender_records)} 个 Blender 版本。' if self.blender_records else '正在检测本机 Blender；完成后会显示版本列表。')
        blender_form.addRow('', self.blender_note)
        self.blender_version = NoWheelComboBox()
        self.blender_version.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.blender_version.setMinimumContentsLength(18)
        self.blender_version.setMinimumWidth(0)
        self.blender_version.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for record in self.blender_records:
            add_blender_record(self.blender_version, record)
        self.current_blender = (job or {}).get('blender') or {}
        preferred = self.current_blender.get('path') or self.blender_settings.get('blender_path')
        preferred_index = next((i for i, record in enumerate(self.blender_records)
                                if record['path'].casefold() == str(preferred or '').casefold()), -1)
        if preferred_index >= 0: self.blender_version.setCurrentIndex(preferred_index)
        elif self.blender_records: self.blender_version.setCurrentIndex(0)
        blender_form.addRow('Blender 版本', self.blender_version)
        self.blender_add = button('添加版本…', self.add_blender)
        blender_form.addRow('', self.blender_add)
        from blender_export import default_point_radius
        defaults = self.blender_settings
        self.points = QCheckBox('显示彩色点（工程内保存颜色，无需额外安装插件）')
        self.points.setChecked(bool(self.current_blender.get('draw_points', defaults.get('blender_draw_points', True))))
        self.mesh = QCheckBox('同时生成可编辑网格（用于几何节点及渲染）')
        self.mesh.setChecked(bool(self.current_blender.get('as_mesh', defaults.get('blender_as_mesh', True))))
        self.background = QCheckBox('预览和渲染使用抽帧背景')
        self.background.setChecked(bool(self.current_blender.get('background_images', defaults.get('blender_background_images', True))))
        self.warnings = QCheckBox('隐藏镜头畸变警告')
        self.warnings.setChecked(bool(self.current_blender.get('suppress_warnings', defaults.get('blender_suppress_warnings', True))))
        for field in (self.points, self.mesh, self.background, self.warnings): blender_form.addRow(field)
        self.radius = QDoubleSpinBox(); self.radius.setDecimals(4); self.radius.setRange(.0001, 100)
        self.radius.setValue(default_point_radius({'blender_point_radius': self.current_blender.get(
            'point_radius', defaults.get('blender_point_radius', .005)),
            'blender_point_size_revision': defaults.get('blender_point_size_revision', 0)}))
        blender_form.addRow('网格渲染点半径', self.radius)
        self.blender_fps = QDoubleSpinBox(); self.blender_fps.setDecimals(3); self.blender_fps.setRange(.01, 240)
        self.blender_fps.setValue(float(self.current_blender.get('fps', defaults.get('blender_fps', 30))))
        blender_form.addRow('Blender FPS', self.blender_fps)
        self.frame_start_offset = QSpinBox(); self.frame_start_offset.setRange(0, 10000)
        self.frame_start_offset.setValue(int(self.current_blender.get(
            'frame_start_offset', defaults.get('blender_frame_start_offset', 0))))
        self.frame_start_offset.setSuffix(' 帧')
        blender_form.addRow('导出起始帧偏移（前置空帧）', self.frame_start_offset)
        blender_form.addRow(label('0 = 从第 1 帧开始；设置 30 时，相机轨迹与背景合成从第 31 帧开始。'))
        self.blender_group.setVisible(self.blend.isChecked())
        form.addRow(self.blender_group)
        layout.addWidget(output)
        self.advanced_toggle = QPushButton('›  高级参数')
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle)
        advanced, form = form_section('高级参数')
        self.max_size = QSpinBox(); self.max_size.setRange(256, 16384); self.max_size.setValue(options.max_size)
        self.overlap = QSpinBox(); self.overlap.setRange(1, 200); self.overlap.setValue(options.overlap)
        self.gpu_min = QSpinBox(); self.gpu_min.setRange(2, 1000000); self.gpu_min.setValue(options.gpu_min_images)
        self.camera = NoWheelComboBox()
        self.camera.addItem('自动估计畸变（默认）', 'radial'); self.camera.addItem('近似针孔（无畸变）', 'pinhole')
        self.camera.setCurrentIndex(max(0, self.camera.findData(options.camera_mode)))
        self.rotation_only = QCheckBox('低视差保护：纯旋转时仅输出相机轨迹')
        self.rotation_only.setChecked(options.rotation_only)
        self.rotation_only.setToolTip('检测到近似纯旋转时跳过平移和点云三角化，只输出相机旋转动画并明确提示。')
        self.decode = QCheckBox('CUDA 视频解码（失败时沿用 CPU 解码回退）'); self.decode.setChecked(options.cuda_decode)
        self.color = QCheckBox('点云取色'); self.color.setChecked(options.color)
        self.solver_quality = NoWheelComboBox()
        for key, profile in SOLVER_PROFILES.items(): self.solver_quality.addItem(profile['title'], key)
        self.solver_quality.setCurrentIndex(self.solver_quality.findData(options.solver_quality))
        self.solver_quality.currentIndexChanged.connect(lambda: self.quality_note.setText(quality_note(self.solver_quality.currentData())))
        form.addRow('求解优化档位', self.solver_quality)
        for name, field in [('SIFT 最大尺寸', self.max_size), ('相邻匹配范围', self.overlap), ('GPU 最少图像', self.gpu_min), ('相机模型', self.camera)]:
            form.addRow(name, field)
        self.rotation_only.hide()
        for field in (self.decode, self.color):
            form.addRow(field)
        layout.addWidget(advanced)
        advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda checked: self.advanced_toggle.setText(('⌄' if checked else '›') + '  高级参数'))
        self.quality.selectionChanged.connect(self.apply_quality)
        self.rate.selectionChanged.connect(self.update_controls)
        self.scale.selectionChanged.connect(self.update_controls)
        self.backend.currentIndexChanged.connect(self.update_controls)
        self.input_mode.currentIndexChanged.connect(self.update_controls)
        self.input_mode.currentIndexChanged.connect(lambda: self.debounce.start())
        self.acceleration.toggled.connect(self.update_controls)
        self.sequence.toggled.connect(self.update_controls)
        self.rotation_only.setEnabled(False)
        self.blend.toggled.connect(self.blender_group.setVisible)
        foot = QWidget()
        foot.setObjectName('taskFooter')
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(24, 10, 24, 16)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet('color: #d8443c;'); self.error.hide()
        fl.addWidget(self.error)
        row = QHBoxLayout(); row.addWidget(label('每个任务独立保存参数')); row.addStretch()
        row.addWidget(button('取消', self.reject))
        self.confirm = button('保存修改' if job else '添加任务', self.accept_task, True)
        self.confirm.setDefault(True)
        row.addWidget(self.confirm)
        fl.addLayout(row); root.addWidget(foot)
        self.debounce = QTimer(self); self.debounce.setSingleShot(True); self.debounce.setInterval(350)
        self.debounce.timeout.connect(self.probe)

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry().adjusted(16, 16, -16, -16)
        # Keep one stable size on normal displays, but never open larger than
        # the usable screen (common on 1366x768 laptops and offscreen tests).
        width = min(1000, available.width())
        height = min(870, available.height())
        if self.width() != width or self.height() != height:
            self.setFixedSize(width, height)
        parent = self.parentWidget()
        if parent and parent.isVisible():
            parent_area = parent.frameGeometry().intersected(available)
            center = parent_area.center() if not parent_area.isEmpty() else available.center()
        else:
            center = available.center()
        x = center.x() - self.width() // 2
        y = center.y() - self.height() // 2
        x = min(max(x, available.left()), available.right() - self.width() + 1)
        y = min(max(y, available.top()), available.bottom() - self.height() + 1)
        self.move(x, y)
        self.source.textChanged.connect(lambda: self.debounce.start())
        self.poller = QTimer(self); self.poller.setInterval(100); self.poller.timeout.connect(self.receive); self.poller.start()
        self.update_controls()
        inset_numeric_fields(self)
        if self.source.toPlainText():
            self.debounce.start()

    def _preview_error(self):
        self.preview_play.setVisible(False)
        self.preview_message.setText('视频预览不可用\n仍可继续解算')
        self.preview_message.show()

    def _preview_frame(self, frame):
        image = frame.toImage()
        if image.isNull():
            return
        self.preview_image = image
        self._paint_preview()

    def _paint_preview(self):
        if self.preview_image is None or self.preview_image.isNull():
            return
        size = self.preview_video.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        pixmap = QPixmap.fromImage(self.preview_image).scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview_video.setPixmap(pixmap)

    def _preview_state_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.preview_play.set_playing(playing)
        self.preview_message.setVisible(not playing)

    def toggle_preview(self):
        if self.preview_player is None:
            return
        if self.preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.preview_player.pause()
        else:
            if self.preview_player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
                self.preview_player.setPosition(0)
            self.preview_player.play()

    def _clear_preview(self, message, playable=False):
        if self.preview_player is not None:
            self.preview_player.stop()
            self.preview_player.setSource(QUrl())
        self.preview_image = None
        self.preview_video.clear()
        self.preview_message.setText(message)
        self.preview_message.show()
        self.preview_play.setVisible(bool(playable and self.preview_player is not None))
        self.preview_play.set_playing(False)

    def _load_preview(self, path):
        if self.preview_player is None or not path:
            self._clear_preview('视频预览不可用\n仍可继续解算', False)
            return
        self.preview_player.stop()
        self.preview_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self.preview_image = None
        self.preview_video.clear()
        self.preview_message.parentWidget().raise_()
        self.preview_play.raise_()
        self.preview_play.setVisible(True)
        self.preview_play.set_playing(False)
        self.preview_message.setText('点击播放预览')
        self.preview_message.show()

    def paths(self):
        values = []
        sequence = self.input_mode.currentData() == 'image_sequence'
        for line in self.source.toPlainText().splitlines():
            path = Path(line.strip().strip('"'))
            if not line.strip():
                continue
            if sequence:
                values.append(path)
            elif path.is_dir() and not (self.job and self.job.get('scene')):
                values.extend(sorted((p for p in path.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS), key=lambda p: p.name.casefold()))
            else:
                values.append(path)
        return list(dict.fromkeys(str(p.resolve()) for p in values))

    def browse(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择视频', str(Path.home()), '视频 (*.mp4 *.mov *.MOV *.mkv *.avi *.m4v *.webm *.mts *.m2ts *.wmv *.mpg *.mpeg);;所有文件 (*)')
        if paths:
            self.source.setPlainText('\n'.join(paths))

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, '选择视频文件夹')
        if path:
            self.source.setPlainText(path)

    def browse_sequence(self):
        path = QFileDialog.getExistingDirectory(self, '选择图像序列文件夹')
        if path:
            self.source.setPlainText(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择输出目录', self.output.text())
        if path:
            self.output.setText(path)

    def add_blender(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 Blender', '', 'Blender (blender.exe)')
        if not path: return
        self.blender_add.setEnabled(False)
        self.blender_note.setText('正在验证 Blender 版本…')
        def work():
            try: self.blender_scan_pending.put(probe_blender(path))
            except Exception as error: self.blender_scan_pending.put(error)
        threading.Thread(target=work, daemon=True).start()

    def set_blender_records(self, records):
        preferred = (self.blender_version.currentData() or {}).get('path') or self.current_blender.get('path')
        self.blender_records[:] = records
        self.blender_version.clear()
        for record in self.blender_records:
            add_blender_record(self.blender_version, record)
        index = next((i for i, record in enumerate(self.blender_records)
                      if record['path'].casefold() == str(preferred or '').casefold()), 0 if self.blender_records else -1)
        self.blender_version.setCurrentIndex(index)
        self.blender_note.setText(f'检测到 {len(self.blender_records)} 个 Blender 版本。' if self.blender_records
                                  else '没有检测到 Blender，请检查安装位置或添加 blender.exe。')

    def probe(self):
        self.generation += 1
        generation = self.generation
        self.metadata = {}
        self.probing = True
        self.confirm.setEnabled(False)
        self.video_note.setText('正在读取视频信息…')
        try:
            paths = self.paths()
        except OSError as error:
            self.pending.put((generation, {}, str(error)))
            return
        def work():
            try:
                if self.job and self.job.get('scene'):
                    infos = {}
                elif self.input_mode.currentData() == 'image_sequence':
                    infos = {path: image_sequence_info(path) for path in paths}
                else:
                    infos = {path: video_info(path, 0) for path in paths}
                self.pending.put((generation, infos, '' if paths else '请选择输入素材。'))
            except Exception as error:
                self.pending.put((generation, {}, f'无法读取视频：{error}'))
        threading.Thread(target=work, daemon=True).start()

    def receive(self):
        try:
            blender_result = self.blender_scan_pending.get_nowait()
        except queue.Empty:
            blender_result = None
        if blender_result is not None:
            self.blender_add.setEnabled(True)
            if isinstance(blender_result, Exception):
                self.blender_note.setText('无法使用所选 Blender：' + str(blender_result))
            else:
                self.blender_records = [r for r in self.blender_records
                                        if r['path'].casefold() != blender_result['path'].casefold()]
                self.blender_records.append(blender_result)
                self.set_blender_records(self.blender_records)
                self.blender_version.setCurrentIndex(self.blender_version.count() - 1)
        while not self.pending.empty():
            generation, infos, error = self.pending.get_nowait()
            if generation != self.generation:
                continue
            self.probing = False
            self.metadata = infos
            self.confirm.setEnabled(True)
            if error:
                self.video_note.setText(error)
                self._clear_preview('尚未选择可预览素材')
            elif self.input_mode.currentData() == 'image_sequence' and len(infos) == 1:
                info = next(iter(infos.values()))
                self.video_note.setText(f"图像序列 · {int(info['frames'])} 张图片 · 将跳过视频拆分")
                self._clear_preview('图像序列\n直接读取')
                self.preview_info.setText(f"{int(info['frames'])} 张图片 · 原始尺寸")
            elif len(infos) == 1:
                path = next(iter(infos))
                info = next(iter(infos.values()))
                self.video_note.setText(f"{info['width']} × {info['height']}  ·  {info['fps']:.3f} fps  ·  {info['duration']:.1f} 秒")
                self._load_preview(path)
                self.preview_info.setText(f"{info['width']} × {info['height']} · {info['fps']:.3f} fps · {info['duration']:.1f} 秒")
            else:
                self.video_note.setText(f'已读取 {len(infos)} 个素材 · 每个素材创建一个独立任务')
                self._clear_preview(f'{len(infos)} 个素材')
                self.preview_info.setText('每个素材会创建一个独立任务。')
            self.update_controls()

    def apply_quality(self):
        values = self.quality.currentData()
        if values:
            self.max_size.setValue(values[0]); self.overlap.setValue(values[1])
            self.solver_quality.setCurrentIndex(self.solver_quality.findData(values[2]))

    def update_controls(self, *_):
        sequence_input = self.input_mode.currentData() == 'image_sequence'
        self.custom_rate.setVisible(self.rate.currentData() == -1)
        rates = [info['fps'] for info in self.metadata.values() if info['fps'] > 0]
        rate = min(rates) if rates else None
        for index, threshold in ((1, 30), (2, 60)):
            self.rate.setOptionEnabled(index, rate is not None and rate + .001 >= threshold)
        self.rate.setOptionEnabled(3, rate is not None and not sequence_input)
        if rate is not None:
            self.custom_rate.setMaximum(min(240, rate))
        self.rate.setEnabled(not sequence_input)
        self.custom_rate.setEnabled(not sequence_input)
        self.scale.setEnabled(not sequence_input)
        self.sequence.setEnabled(not sequence_input)
        self.sequence_size.setEnabled(self.sequence.isChecked() and not sequence_input)
        self.drop_zone.set_sequence_mode(sequence_input)
        self.source.setPlaceholderText('拖入图像序列文件夹，或输入目录路径（每行一个）' if sequence_input
                                       else '拖入视频或视频文件夹，或输入完整路径（每行一个）')
        self.acceleration.setEnabled(True)
        self.gpu_choice.setEnabled(self.acceleration.isChecked())
        self.decode.setEnabled(self.acceleration.isChecked() and not sequence_input)
        self.preview_acceleration.setText('GPU 硬件加速：已启用' if self.acceleration.isChecked() else 'GPU 硬件加速：已关闭，使用 CPU')
        # New tasks use the merged native COLMAP Global Mapper.  The old
        # backend aliases are migrated by Options.validate() and are not
        # exposed in this dialog.
        colmap = self.backend.currentData() == 'colmap'
        self.rotation_only.setEnabled(False)
        if self.rotation_only.isChecked():
            self.rotation_only.setChecked(False)
        self.color.setEnabled(colmap)
        self.camera.setEnabled(colmap)
        self.solver_quality.setEnabled(colmap)
        self.input_mode.setToolTip('图像序列会直接读取文件夹中的图片，跳过 FFmpeg 拆分。' if sequence_input else '视频输入会先由 FFmpeg 抽取为序列帧。')
        if len(self.metadata) == 1:
            info = next(iter(self.metadata.values()))
            scale = self.scale.currentData()
            width, height = info.get('width', 0) or 0, info.get('height', 0) or 0
            w, h = (width, height) if scale == 1 else (max(2, int(width * scale / 2) * 2), max(2, int(height * scale / 2) * 2))
            self.size_note.setText(f'处理尺寸 {w} × {h} · FFmpeg 缩放，SIFT 最大尺寸独立控制')
        elif sequence_input:
            self.size_note.setText('图像序列直接读取原始图片；如需缩放请先在外部处理。')

    def accept_task(self):
        try:
            if self.probing or self.debounce.isActive():
                raise ValueError('请等待素材信息读取完成。')
            paths = self.paths()
            if not paths:
                raise ValueError('请选择输入素材。')
            scene = bool(self.job and self.job.get('scene'))
            sequence_input = self.input_mode.currentData() == 'image_sequence'
            if not scene:
                if sequence_input:
                    if (set(paths) != set(self.metadata) or
                            any(not Path(p).is_dir() or not any(q.is_file() and q.suffix.lower() in IMAGE_EXTENSIONS for q in Path(p).iterdir()) for p in paths)):
                        raise ValueError('图像序列目录无效或没有可读取的图片。')
                elif (set(paths) != set(self.metadata) or
                      any(not Path(p).is_file() or Path(p).suffix.lower() not in VIDEO_EXTENSIONS for p in paths)):
                    raise ValueError('视频路径无效或无法读取，请检查输入素材。')
            if self.name_validator:
                prefix = self.name.text().strip()
                names = [prefix + (' · ' + Path(path).stem if prefix and len(paths) > 1 else '')
                         or Path(path).stem for path in paths]
                self.name_validator(names, ignore_id=(self.job or {}).get('id'))
            fps = self.rate.currentData()
            if fps == -1:
                fps = self.custom_rate.value()
            if not sequence_input and fps and (not self.metadata or any(info['fps'] + .001 < fps for info in self.metadata.values())):
                raise ValueError('处理帧率不能超过任一输入视频的原始帧率。')
            if not self.output.text().strip():
                raise ValueError('请选择输出目录。')
            selected_gpu = self.gpu_choice.currentData()
            options = Options(**{**asdict(self.base), 'backend': self.backend.currentData(),
                'output_root': str(Path(self.output.text().strip()).resolve()),
                'max_size': self.max_size.value(), 'overlap': self.overlap.value(),
                'gpu_min_images': self.gpu_min.value(), 'camera_mode': self.camera.currentData(),
                'cuda_decode': self.decode.isChecked() and self.acceleration.isChecked(), 'color': self.color.isChecked(),
                'glomap_gpu': False,
                'export_sequence': self.sequence.isChecked(), 'sequence_original': self.sequence_size.currentData(),
                'solver_quality': self.solver_quality.currentData(),
                'smart_lens': self.smart_lens.isChecked(),
                # Keep the legacy keys synchronized for 1.x task records.
                'auto_focal': self.smart_lens.isChecked(),
                'contains_cuts': self.smart_lens.isChecked(),
                'rotation_only': self.rotation_only.isChecked(),
                'use_gpu': self.acceleration.isChecked(), 'gpu_index': int(selected_gpu),
                'input_type': 'image_sequence' if sequence_input else 'video',
                'fps': 0 if sequence_input else fps, 'image_scale': 1.0 if sequence_input else self.scale.currentData()})
            Pipeline(options).preflight()
            blender = None
            if self.blend.isChecked():
                if not self.mesh.isChecked() and not self.points.isChecked():
                    raise ValueError('至少启用可编辑网格或彩色点显示其中一项。')
                record = self.blender_version.currentData()
                if not record:
                    raise ValueError('请先选择 Blender 版本；软件启动后会自动缓存本机版本。')
                settings = self.blender_settings
                current_blender = self.current_blender
                blender = dict(record, fps=self.blender_fps.value(), as_mesh=self.mesh.isChecked(),
                                draw_points=self.points.isChecked(), suppress_warnings=self.warnings.isChecked(),
                                background_images=self.background.isChecked(), point_radius=self.radius.value(),
                                frame_start_offset=self.frame_start_offset.value(),
                                paths=[r['path'] for r in self.blender_records])
            self.result_data = dict(paths=paths, name=self.name.text().strip(), options=options,
                                    auto_blender=self.blend.isChecked(), blender=blender)
            self.accept()
        except (ValueError, OSError) as error:
            self.error.setText(str(error)); self.error.show()


class BlenderDialog(QDialog):
    def __init__(self, parent, settings, fps=30, opening=False):
        super().__init__(parent)
        self.setWindowTitle('打开 Blender 工程' if opening else 'Blender 导出设置')
        self.resize(600, 500 if opening else 620)
        self.result_data = None
        self.settings = dict(settings)
        self.opening = opening
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(12)
        record = settings.get('_blender_record') or {}
        records = list(settings.get('blender_records') or [])
        if record.get('path') and not any(
                str(item.get('path', '')).casefold() == str(record['path']).casefold()
                for item in records):
            records.append(record)
        self.blender_version = NoWheelComboBox()
        self.blender_version.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.blender_version.setMinimumContentsLength(18)
        self.blender_version.setMinimumWidth(0)
        self.blender_version.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for item in records:
            add_blender_record(self.blender_version, item)
            if str(item.get('path', '')).casefold() == str(record.get('path', '')).casefold():
                self.blender_version.setCurrentIndex(self.blender_version.count() - 1)
        version_form = QFormLayout()
        version_form.addRow('Blender 版本', self.blender_version)
        layout.addLayout(version_form)
        layout.addWidget(label(f'检测到 {len(records)} 个 Blender 版本，悬停可查看程序路径。'
                               if records else '未检测到 Blender，请在设置中添加或重新扫描。'))
        self.mesh = QCheckBox('同时生成可编辑网格（用于几何节点及渲染）'); self.mesh.setChecked(settings.get('blender_as_mesh', True))
        self.points = QCheckBox('显示彩色点（工程内保存颜色，无需额外安装插件）'); self.points.setChecked(settings.get('blender_draw_points', True))
        self.background = QCheckBox('预览和渲染使用抽帧背景'); self.background.setChecked(settings.get('blender_background_images', True))
        self.warnings = QCheckBox('隐藏镜头畸变警告'); self.warnings.setChecked(settings.get('blender_suppress_warnings', True))
        from blender_export import default_point_radius
        self.radius = QDoubleSpinBox(); self.radius.setDecimals(4); self.radius.setRange(.0001, 100); self.radius.setValue(default_point_radius(settings))
        self.fps = QDoubleSpinBox(); self.fps.setDecimals(3); self.fps.setRange(.01, 240); self.fps.setValue(float(fps))
        self.frame_start_offset = QSpinBox(); self.frame_start_offset.setRange(0, 10000)
        self.frame_start_offset.setValue(int(settings.get('blender_frame_start_offset', 0)))
        self.frame_start_offset.setSuffix(' 帧')
        if not opening:
            group = QGroupBox('摄影测量场景'); form = QFormLayout(group)
            for field in (self.points, self.mesh, self.background, self.warnings): form.addRow(field)
            form.addRow('网格渲染点半径', self.radius); form.addRow('Blender FPS', self.fps)
            form.addRow('导出起始帧偏移（前置空帧）', self.frame_start_offset)
            form.addRow(label('0 = 从第 1 帧开始；设置 30 时，相机轨迹与背景合成从第 31 帧开始。'))
            layout.addWidget(group)
            layout.addWidget(label('从本软件打开 Blender 文件会自动加载内置摄影测量插件。\n相机背景引用项目 _cache/images 图像，请保留缓存目录。'))
        layout.addStretch()
        row = QHBoxLayout(); row.addStretch(); row.addWidget(button('取消', self.reject))
        self.confirm = button('打开文件' if opening else '确认导出设置', self.accept_settings, True)
        self.confirm.setEnabled(bool((self.blender_version.currentData() or {}).get('path')))
        row.addWidget(self.confirm); layout.addLayout(row)
        inset_numeric_fields(self)

    def accept_settings(self):
        record = self.blender_version.currentData()
        if not record or not record.get('path'):
            return
        if not self.opening and not self.mesh.isChecked() and not self.points.isChecked():
            return
        self.result_data = dict(record, fps=self.fps.value(), as_mesh=self.mesh.isChecked(),
            draw_points=self.points.isChecked(), suppress_warnings=self.warnings.isChecked(),
            background_images=self.background.isChecked(), point_radius=self.radius.value(),
            frame_start_offset=self.frame_start_offset.value(), paths=self.settings.get('blender_paths', []))
        self.accept()
