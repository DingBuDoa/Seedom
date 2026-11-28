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
    QButtonGroup, QStackedLayout, QSlider)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
except ImportError:  # Keep the task dialog usable in minimal Qt installations.
    QMediaPlayer = QAudioOutput = QVideoSink = None

try:
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:  # Qt Multimedia Widgets is optional in minimal installs.
    QVideoWidget = None

from tracker_core import Options, Pipeline, VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
from workload import video_info, image_sequence_info
from blender_runtime import discover_blenders, probe_blender
from qt_style import inset_numeric_fields
from app_info import SOLVER_CHOICES, SOLVER_TOOLTIPS, DEFAULT_SOLVER
from solver_quality import quality_note


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


class BlenderSequenceFields(QWidget):
    """Shared automatic/manual export controls with a single background policy."""
    def __init__(self, settings, background, parent=None):
        super().__init__(parent)
        from blender_sequence import SEQUENCE_FORMATS, SEQUENCE_SIZES
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.enabled = QCheckBox('保存背景序列帧')
        self.enabled.setToolTip('序列帧保存在项目 video 文件夹，用于 Blender 背景；不影响解算精度。')
        self.enabled.setChecked(settings.get('export_sequence', True) or background.isChecked())
        form.addRow(self.enabled)
        self.details = QWidget()
        detail_form = QFormLayout(self.details)
        detail_form.setContentsMargins(0, 0, 0, 0)
        detail_form.setHorizontalSpacing(14)
        detail_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.format = NoWheelComboBox()
        self.resolution = NoWheelComboBox()
        for title, value in SEQUENCE_FORMATS:
            self.format.addItem(title, value)
        for title, value in SEQUENCE_SIZES:
            self.resolution.addItem(title, value)
        for field, key, default in ((self.format, 'sequence_format', 'jpg'),
                                     (self.resolution, 'sequence_resolution', 'original')):
            index = field.findData(settings.get(key, default))
            field.setCurrentIndex(max(0, index))
        self.resolution.setToolTip('保持原始画面比例；按高度限制的尺寸不会放大原素材。')
        detail_form.addRow('图像格式', self.format)
        detail_form.addRow('输出尺寸', self.resolution)
        for field in (self.format, self.resolution):
            detail_form.labelForField(field).setMinimumWidth(68)
        detail_form.addRow(background)
        form.addRow(self.details)
        self.details.setVisible(self.enabled.isChecked())
        def toggle(enabled):
            self.details.setVisible(enabled)
            if not enabled:
                background.setChecked(False)
        self.enabled.toggled.connect(toggle)
        background.toggled.connect(lambda checked: self.enabled.setChecked(True) if checked else None)

    def values(self):
        return dict(export_sequence=self.enabled.isChecked(), sequence_format=self.format.currentData(),
                    sequence_resolution=self.resolution.currentData())


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
        # The control now sits below the native video surface on a light
        # panel, so a white-on-transparent button would disappear into the
        # background.  Use the app blue for a stable, high-contrast control.
        painter.setBrush(QColor(47, 107, 255, 235))
        painter.setPen(QColor(47, 107, 255, 255))
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
        self.preview_stage = QWidget(); self.preview_stage.setObjectName('previewStage')
        # Keep the preview width fixed so it never squeezes the form.  The
        # height is adjusted from the source aspect ratio after probing.
        self.preview_stage.setFixedWidth(360)
        self.preview_stage.setMinimumHeight(205)
        self.preview_stage.setMaximumHeight(360)
        self.preview_stage.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.preview_stage.setFixedHeight(205)
        stage_layout = QStackedLayout(self.preview_stage)
        stage_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        # QVideoWidget lets the Qt multimedia backend render the original
        # source directly.  On Windows this uses the system video surface and
        # hardware decoder when the codec/driver support it, avoiding a
        # second FFmpeg transcode and a per-frame QImage conversion.
        self.preview_video = QVideoWidget() if QVideoWidget is not None else QLabel()
        self.preview_video.setObjectName('taskPreviewCanvas')
        if QVideoWidget is not None:
            self.preview_video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self.preview_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.preview_video.setScaledContents(False)
        stage_layout.addWidget(self.preview_video)
        # QVideoWidget uses a native video surface on Windows.  A child
        # overlay placed on top of it can be hidden by that surface, so keep
        # status text and playback controls in a normal row below the video.
        self.preview_message = QLabel('尚未选择素材')
        self.preview_message.setObjectName('previewMessage')
        self.preview_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_play = PreviewPlayButton()
        self.preview_play.setToolTip('播放 / 暂停预览')
        self.preview_play.clicked.connect(self.toggle_preview)
        self.preview_seek = QSlider(Qt.Orientation.Horizontal)
        self.preview_seek.setObjectName('previewSeek')
        self.preview_seek.setRange(0, 0)
        self.preview_seek.setEnabled(False)
        self.preview_seek.setToolTip('拖动定位预览')
        self.preview_seek.sliderMoved.connect(self._preview_seek)
        self.preview_time = QLabel('00:00 / 00:00')
        self.preview_time.setObjectName('previewTime')
        self.preview_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        preview_layout.addWidget(self.preview_stage, 0, Qt.AlignmentFlag.AlignHCenter)
        preview_controls = QWidget()
        preview_controls.setObjectName('previewControls')
        controls_layout = QVBoxLayout(preview_controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(self.preview_message)
        seek_row = QHBoxLayout()
        seek_row.setContentsMargins(0, 0, 0, 0)
        seek_row.setSpacing(8)
        seek_row.addWidget(self.preview_seek, 1)
        seek_row.addWidget(self.preview_time)
        controls_layout.addLayout(seek_row)
        play_row = QHBoxLayout()
        play_row.setContentsMargins(0, 0, 0, 0)
        play_row.addStretch()
        play_row.addWidget(self.preview_play)
        play_row.addStretch()
        controls_layout.addLayout(play_row)
        preview_layout.addWidget(preview_controls)
        if QMediaPlayer is not None and (QVideoWidget is not None or QVideoSink is not None):
            self.preview_audio = QAudioOutput(self)
            self.preview_player = QMediaPlayer(self)
            self.preview_player.setAudioOutput(self.preview_audio)
            if QVideoWidget is not None:
                self.preview_player.setVideoOutput(self.preview_video)
            else:
                self.preview_sink = QVideoSink(self)
                self.preview_player.setVideoOutput(self.preview_sink)
                self.preview_sink.videoFrameChanged.connect(self._preview_frame)
            self.preview_player.playbackStateChanged.connect(self._preview_state_changed)
            self.preview_player.durationChanged.connect(self._preview_duration_changed)
            self.preview_player.positionChanged.connect(self._preview_position_changed)
            self.preview_player.errorOccurred.connect(lambda *_: self._preview_error())
        self.preview_play.setVisible(False)
        self.preview_info = QFrame()
        self.preview_info.setObjectName('previewInfoCard')
        info_layout = QVBoxLayout(self.preview_info)
        info_layout.setContentsMargins(14, 9, 14, 9)
        info_layout.setSpacing(0)
        self.preview_info_values = {}
        for index, (key, title) in enumerate((
                ('resolution', '分辨率'), ('fps', '源帧率'),
                ('frames', '预估总帧数'), ('duration', '时长'))):
            if index:
                separator = QFrame()
                separator.setObjectName('previewInfoSeparator')
                separator.setFixedHeight(1)
                info_layout.addWidget(separator)
            row = QWidget()
            row.setObjectName('previewInfoRow')
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            caption = QLabel(title)
            caption.setObjectName('previewInfoLabel')
            value = QLabel('—')
            value.setObjectName('previewInfoValue')
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row_layout.addWidget(caption)
            row_layout.addWidget(value, 1)
            info_layout.addWidget(row)
            self.preview_info_values[key] = value
        preview_layout.addWidget(self.preview_info)
        self.preview_acceleration = label('硬件加速：按任务设置', 'taskAcceleration')
        preview_layout.addWidget(self.preview_acceleration)
        preview_layout.addStretch()
        content_layout.addWidget(preview, 0)
        root.addWidget(content, 1)
        basic, form = form_section('01 · 输入素材')
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
        # Metadata is shown in the fixed preview panel on the right.  Keep a
        # hidden compatibility label because older task integrations accessed
        # ``video_note`` while probing a source.
        self.video_note = label('正在读取素材信息…' if self.source.toPlainText() else '支持视频文件或图像序列文件夹')
        self.video_note.hide()
        self.output = QLineEdit(options.output_root)
        outrow = QHBoxLayout()
        outrow.setSpacing(8)
        outrow.addWidget(self.output)
        outrow.addWidget(button('浏览…', self.browse_output))
        self.output.setToolTip('每个任务会在此目录下创建独立的项目文件夹。')
        layout.addWidget(basic)
        processing, form = form_section('02 · 解算设置')
        self.backend = NoWheelComboBox()
        for key, text in SOLVER_CHOICES.items():
            self.backend.addItem(text, key)
            self.backend.setItemData(self.backend.count() - 1, SOLVER_TOOLTIPS[key], Qt.ItemDataRole.ToolTipRole)
        index = self.backend.findData(options.backend)
        self.backend.setCurrentIndex(index if index >= 0 else self.backend.findData(DEFAULT_SOLVER))
        form.addRow('求解方案', self.backend)
        if options.backend == 'colmap':
            archived_note = label('原任务使用 COLMAP，保存后将使用当前选择。')
            archived_note.setWordWrap(True)
            form.addRow(archived_note)
        self.vggt_note = label('实验性研究模型 · 建议先测试单镜头短片')
        self.vggt_note.setWordWrap(True)
        self.vggt_note.setToolTip('本地预测相机、焦距和深度；长视频分批对齐，尚无全局 BA 或回环优化。使用 FAIR 非商业研究许可。')
        form.addRow(self.vggt_note)
        self.smart_lens = QCheckBox('视频包含连续变焦（实验性）')
        # Continuous zoom uses the legacy GLOMAP segmented-focal workflow.
        # It must not be routed to the newer COLMAP mapper.
        legacy_smart = bool(getattr(options, 'auto_focal', False) or
                            False)
        self.smart_lens.setChecked(bool(getattr(options, 'smart_lens', legacy_smart)))
        self.smart_lens.setToolTip('仅在同一镜头存在连续推拉变焦时勾选；使用旧版 GLOMAP 分段焦距流程。普通镜头或只有切镜时请关闭。')
        form.addRow(self.smart_lens)
        self.initial_focal = QDoubleSpinBox()
        self.initial_focal.setRange(0, 100000)
        self.initial_focal.setDecimals(1)
        self.initial_focal.setSingleStep(10)
        self.initial_focal.setValue(float(getattr(options, 'initial_focal_length', 0)))
        self.initial_focal.setSuffix(' px')
        self.initial_focal.setToolTip('输入原始素材像素坐标中的初始焦距；0 = 自动估计。它只作为起点，不会锁定焦距。')
        form.addRow('初始焦距（可选）', self.initial_focal)
        self.lens_form = form
        acceleration_row = QHBoxLayout()
        self.acceleration = QCheckBox('启用 GPU 硬件加速')
        self.acceleration.setChecked(bool(options.use_gpu))
        self.acceleration.setToolTip('关闭后使用 CPU；开启后使用设置中选择的显卡。')
        acceleration_row.addWidget(self.acceleration)
        acceleration_row.addStretch()
        form.addRow('硬件加速', acceleration_row)
        self.quality = QualitySelector([
            ('默认', None), ('高', (4096, 15, 'high')),
            ('中', (2048, 10, 'balanced')), ('低', (1024, 5, 'fast')),
        ])
        form.addRow('计算精度', self.quality)
        self.quality_label = form.labelForField(self.quality)
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
        output, form = form_section('03 · 导出')
        form.addRow('保存位置', outrow)
        form.addRow(label('默认保存点云和相机数据。'))
        self.blend = QCheckBox('同时导出 Blender 工程')
        self.blend.setChecked(bool(job and job.get('blender')))
        form.addRow(self.blend)
        self.blender_group = QWidget()
        blender_form = QFormLayout(self.blender_group)
        blender_form.setContentsMargins(0, 6, 0, 0)
        blender_form.setVerticalSpacing(10)
        blender_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
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
        self.blender_add = button('添加版本…', self.add_blender)
        version_row = QHBoxLayout()
        version_row.setSpacing(8)
        version_row.addWidget(self.blender_version, 1)
        version_row.addWidget(self.blender_add)
        blender_form.addRow('Blender 版本', version_row)
        from blender_export import default_point_radius
        defaults = self.blender_settings
        self.points = QCheckBox('显示彩色点云')
        self.points.setChecked(bool(self.current_blender.get('draw_points', defaults.get('blender_draw_points', True))))
        self.mesh = QCheckBox('生成可编辑网格')
        self.mesh.setChecked(bool(self.current_blender.get('as_mesh', defaults.get('blender_as_mesh', True))))
        self.background = QCheckBox('用于相机预览和渲染背景')
        self.background.setChecked(bool(self.current_blender.get('background_images', defaults.get('blender_background_images', True))))
        self.warnings = QCheckBox('隐藏镜头畸变警告')
        self.warnings.setChecked(bool(self.current_blender.get('suppress_warnings', defaults.get('blender_suppress_warnings', True))))
        self.sequence_fields = BlenderSequenceFields({
            'export_sequence': self.current_blender.get('export_sequence', defaults.get('blender_export_sequence', True)),
            'sequence_format': self.current_blender.get('sequence_format', defaults.get('blender_sequence_format', 'jpg')),
            'sequence_resolution': self.current_blender.get('sequence_resolution', defaults.get('blender_sequence_resolution', 'original')),
        }, self.background)
        self.sequence = self.sequence_fields.enabled
        self.sequence_size = self.sequence_fields.resolution
        blender_form.addRow(self.sequence_fields)
        scene_group = QWidget()
        scene_form = QFormLayout(scene_group)
        scene_form.setContentsMargins(0, 0, 0, 0)
        scene_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for field in (self.points, self.mesh, self.warnings): scene_form.addRow(field)
        self.radius = QDoubleSpinBox(); self.radius.setDecimals(4); self.radius.setRange(.0001, 100)
        self.radius.setValue(default_point_radius({'blender_point_radius': self.current_blender.get(
            'point_radius', defaults.get('blender_point_radius', .005)),
            'blender_point_size_revision': defaults.get('blender_point_size_revision', 0)}))
        scene_form.addRow('渲染点半径', self.radius)
        self.blender_fps = QDoubleSpinBox(); self.blender_fps.setDecimals(3); self.blender_fps.setRange(.01, 240)
        self.blender_fps.setValue(float(self.current_blender.get('fps', defaults.get('blender_fps', 30))))
        blender_form.addRow('Blender FPS', self.blender_fps)
        self.frame_start_offset = QSpinBox(); self.frame_start_offset.setRange(0, 10000)
        self.frame_start_offset.setValue(int(self.current_blender.get(
            'frame_start_offset', defaults.get('blender_frame_start_offset', 0))))
        self.frame_start_offset.setSuffix(' 帧')
        blender_form.addRow('前置空帧', self.frame_start_offset)
        self.frame_start_offset.setToolTip('0 从第 1 帧开始；30 从第 31 帧开始，相机和背景同步偏移。')
        more_scene = QCheckBox('更多场景选项')
        more_scene.toggled.connect(scene_group.setVisible)
        scene_group.hide()
        blender_form.addRow(more_scene)
        blender_form.addRow(scene_group)
        self.blender_group.setVisible(self.blend.isChecked())
        form.addRow(self.blender_group)
        layout.addWidget(output)
        self.quality.selectionChanged.connect(self.apply_quality)
        self.rate.selectionChanged.connect(self.update_controls)
        self.scale.selectionChanged.connect(self.update_controls)
        self.backend.currentIndexChanged.connect(self.update_controls)
        self.smart_lens.toggled.connect(self.update_controls)
        self.input_mode.currentIndexChanged.connect(self.update_controls)
        self.input_mode.currentIndexChanged.connect(lambda: self.debounce.start())
        self.acceleration.toggled.connect(self.update_controls)
        self.sequence.toggled.connect(self.update_controls)
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
        self.update_controls()

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

    @staticmethod
    def _preview_time_text(milliseconds):
        total_seconds = max(0, int(milliseconds or 0) // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f'{hours}:{minutes:02d}:{seconds:02d}'
        return f'{minutes:02d}:{seconds:02d}'

    def _preview_duration_changed(self, duration):
        duration = max(0, int(duration or 0))
        self.preview_seek.setRange(0, duration)
        self.preview_seek.setEnabled(duration > 0)
        self.preview_time.setText(
            f'{self._preview_time_text(self.preview_player.position() if self.preview_player else 0)} / '
            f'{self._preview_time_text(duration)}')

    def _preview_position_changed(self, position):
        position = max(0, int(position or 0))
        if not self.preview_seek.isSliderDown():
            self.preview_seek.blockSignals(True)
            self.preview_seek.setValue(position)
            self.preview_seek.blockSignals(False)
        self.preview_time.setText(
            f'{self._preview_time_text(position)} / '
            f'{self._preview_time_text(self.preview_player.duration() if self.preview_player else 0)}')

    def _preview_seek(self, position):
        if self.preview_player is not None:
            self.preview_player.setPosition(int(position))

    def _preview_error(self):
        # Some platform backends report a recoverable audio/codec warning
        # while the video stream is still usable.  Keep the control visible
        # whenever a source is loaded so the user can retry playback.
        source_loaded = (self.preview_player is not None and
                         not self.preview_player.source().isEmpty())
        self.preview_play.setVisible(source_loaded)
        self.preview_seek.setEnabled(False)
        self.preview_message.setText('视频预览不可用\n仍可继续解算')
        self.preview_message.show()

    def _set_preview_source(self, path, message='点击播放预览'):
        if self.preview_player is None:
            self._clear_preview('视频预览不可用\n仍可继续解算', False)
            return
        self.preview_player.stop()
        self.preview_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self.preview_image = None
        self.preview_seek.setRange(0, 0)
        self.preview_seek.setEnabled(False)
        self.preview_time.setText('00:00 / 00:00')
        if isinstance(self.preview_video, QLabel):
            self.preview_video.clear()
        self.preview_message.parentWidget().raise_()
        self.preview_play.raise_()
        self.preview_play.setVisible(True)
        self.preview_play.set_playing(False)
        self.preview_message.setText(message)
        self.preview_message.show()

    def _preview_frame(self, frame):
        image = frame.toImage()
        if image.isNull():
            return
        self.preview_image = image
        self._paint_preview()

    def _paint_preview(self):
        if not isinstance(self.preview_video, QLabel):
            return
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
        if isinstance(self.preview_video, QLabel):
            self.preview_video.clear()
        self.preview_message.setText(message)
        self.preview_message.show()
        self.preview_play.setVisible(bool(playable and self.preview_player is not None))
        self.preview_play.set_playing(False)
        self.preview_seek.setRange(0, 0)
        self.preview_seek.setEnabled(False)
        self.preview_time.setText('00:00 / 00:00')

    def _load_preview(self, path):
        if self.preview_player is None or not path:
            self._clear_preview('视频预览不可用\n仍可继续解算', False)
            return
        # Feed the original file to Qt Multimedia.  The platform backend can
        # seek and decode it directly, including MOV files, without creating a
        # cached proxy or doing an extra CPU-side conversion.
        self._set_preview_source(Path(path).resolve())

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

    def _resize_preview_stage(self, info=None):
        """Give portrait sources a taller canvas while keeping landscape compact."""
        width = int((info or {}).get('width') or 0)
        height = int((info or {}).get('height') or 0)
        if width > 0 and height > 0:
            canvas_width = max(1, self.preview_stage.width() or 360)
            target_height = round(canvas_width * height / width)
            target_height = max(205, min(360, target_height))
        else:
            target_height = 205
        self.preview_stage.setFixedHeight(target_height)
        self._paint_preview()

    @staticmethod
    def _duration_text(seconds):
        try:
            total = max(0, int(round(float(seconds or 0))))
        except (TypeError, ValueError):
            total = 0
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'

    def _set_preview_details(self, info=None, sequence=False, count=0):
        """Update the right-hand metadata card without putting details in the form."""
        self._resize_preview_stage(info)
        values = {key: '—' for key in self.preview_info_values}
        if info:
            width, height = info.get('width'), info.get('height')
            if width and height:
                values['resolution'] = f'{int(width)} × {int(height)}'
            frames = info.get('frames')
            if frames:
                values['frames'] = f'{int(round(float(frames))):,} 帧'
            if sequence:
                values['fps'] = '—'
                values['duration'] = '—'
            else:
                fps = info.get('fps')
                if fps:
                    values['fps'] = f'{float(fps):g} fps'
                if info.get('duration'):
                    values['duration'] = self._duration_text(info['duration'])
        elif count > 1:
            values['frames'] = f'{count} 个素材'
        for key, text in values.items():
            self.preview_info_values[key].setText(text)

    def probe(self):
        self.generation += 1
        generation = self.generation
        self.metadata = {}
        self.probing = True
        self.confirm.setEnabled(False)
        self.video_note.setText('正在读取视频信息…')
        self._set_preview_details()
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
                self._set_preview_details()
            elif self.input_mode.currentData() == 'image_sequence' and len(infos) == 1:
                info = next(iter(infos.values()))
                self.video_note.setText(f"图像序列 · {int(info['frames'])} 张图片 · 将跳过视频拆分")
                self._clear_preview('图像序列\n直接读取')
                self._set_preview_details(info, sequence=True)
            elif len(infos) == 1:
                path = next(iter(infos))
                info = next(iter(infos.values()))
                self.video_note.setText(f"{info['width']} × {info['height']}  ·  {info['fps']:.3f} fps  ·  {info['duration']:.1f} 秒")
                self._load_preview(path)
                self._set_preview_details(info)
            else:
                self.video_note.setText(f'已读取 {len(infos)} 个素材 · 每个素材创建一个独立任务')
                self._clear_preview(f'{len(infos)} 个素材')
                self._set_preview_details(count=len(infos))
            self.update_controls()

    def apply_quality(self):
        if self.backend.currentData() == 'vggt-omega':
            self.update_controls()
            return
        values = self.quality.currentData()
        if values:
            self.quality_note.setText(
                f'任务级预设：SIFT {values[0]}，匹配范围 {values[1]}，求解档位 {values[2]}。')
            self.quality.setToolTip(self.quality_note.text())
        else:
            self.quality_note.setText(quality_note(self.base.solver_quality))
            self.quality.setToolTip(self.quality_note.text())

    def update_controls(self, *_):
        sequence_input = self.input_mode.currentData() == 'image_sequence'
        vggt = self.backend.currentData() == 'vggt-omega'
        self.vggt_note.setVisible(vggt)
        if vggt and self.smart_lens.isChecked():
            blocked = self.smart_lens.blockSignals(True)
            self.smart_lens.setChecked(False)
            self.smart_lens.blockSignals(blocked)
        self.smart_lens.setEnabled(not vggt)
        self.initial_focal.setEnabled(not vggt)
        self.lens_form.setRowVisible(self.smart_lens, not vggt)
        self.lens_form.setRowVisible(self.initial_focal, not vggt)
        self.lens_form.setRowVisible(self.scale, not vggt)
        self.quality_label.setText('推理档位' if vggt else '计算精度')
        if vggt:
            self.smart_lens.setToolTip('VGGT-Omega 自行预测每帧焦距，不使用 GLOMAP 分段变焦。')
            self.quality.setToolTip('低：384 像素、每批 8 帧；中：512 像素、12 帧；高：512 像素、20 帧。默认沿用当前预设；显存不足时缩小批次。')
        else:
            self.smart_lens.setToolTip('仅在同一镜头存在连续推拉变焦时勾选；使用旧版 GLOMAP 分段焦距流程。')
            self.quality.setToolTip(quality_note(self.base.solver_quality))
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
        self.scale.setEnabled(not sequence_input and not vggt)
        self.drop_zone.set_sequence_mode(sequence_input)
        self.source.setPlaceholderText('拖入图像序列文件夹，或输入目录路径（每行一个）' if sequence_input
                                       else '拖入视频或视频文件夹，或输入完整路径（每行一个）')
        self.acceleration.setEnabled(True)
        self.preview_acceleration.setText('GPU 硬件加速：已启用' if self.acceleration.isChecked() else 'GPU 硬件加速：已关闭，使用 CPU')
        # VGGT predicts its own focal lengths. The experimental segmented
        # zoom option belongs only to the explicitly selected legacy GLOMAP.
        self.backend.setEnabled(True)
        self.backend.setToolTip(SOLVER_TOOLTIPS.get(self.backend.currentData(), ''))
        self.input_mode.setToolTip('图像序列会直接读取文件夹中的图片，跳过 FFmpeg 拆分。' if sequence_input else '视频输入会先由 FFmpeg 抽取为序列帧。')
        if len(self.metadata) == 1:
            info = next(iter(self.metadata.values()))
            scale = 1.0 if vggt else self.scale.currentData()
            width, height = info.get('width', 0) or 0, info.get('height', 0) or 0
            w, h = (width, height) if scale == 1 else (max(2, int(width * scale / 2) * 2), max(2, int(height * scale / 2) * 2))
            self.size_note.setText(f'处理尺寸 {w} × {h} · ' +
                                   ('模型输入按计算档位缩放' if vggt else 'FFmpeg 缩放，SIFT 最大尺寸独立控制'))
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
            # GPU identity and advanced solver defaults are managed in Settings;
            # a task only opts
            # into hardware acceleration and keeps the selected index.
            selected_gpu = int(getattr(self.base, 'gpu_index', 0))
            quality_values = self.quality.currentData()
            if quality_values:
                max_size, overlap, solver_quality = quality_values
            else:
                max_size = self.base.max_size
                overlap = self.base.overlap
                solver_quality = self.base.solver_quality
            options = Options(**{**asdict(self.base), 'backend': self.backend.currentData(),
                'output_root': str(Path(self.output.text().strip()).resolve()),
                'max_size': max_size, 'overlap': overlap,
                'gpu_min_images': self.base.gpu_min_images, 'camera_mode': self.base.camera_mode,
                'cuda_decode': self.base.cuda_decode and self.acceleration.isChecked(), 'color': self.base.color,
                'glomap_gpu': False,
                'export_sequence': False,
                'sequence_original': False,
                'solver_quality': solver_quality,
                'initial_focal_length': self.initial_focal.value(),
                'smart_lens': self.smart_lens.isChecked(),
                # Keep the legacy keys synchronized for 1.x task records.
                'auto_focal': self.smart_lens.isChecked(),
                'contains_cuts': False,
                'rotation_only': False,
                'use_gpu': self.acceleration.isChecked(), 'gpu_index': int(selected_gpu),
                'input_type': 'image_sequence' if sequence_input else 'video',
                'fps': 0 if sequence_input else fps,
                'image_scale': 1.0 if sequence_input or self.backend.currentData() == 'vggt-omega' else self.scale.currentData()})
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
                                paths=[r['path'] for r in self.blender_records],
                                **self.sequence_fields.values())
            self.result_data = dict(paths=paths, name=self.name.text().strip(), options=options,
                                    auto_blender=self.blend.isChecked(), blender=blender)
            self.accept()
        except (ValueError, OSError) as error:
            self.error.setText(str(error)); self.error.show()


class BlenderDialog(QDialog):
    def __init__(self, parent, settings, fps=30, opening=False):
        super().__init__(parent)
        self.setWindowTitle('打开 Blender 工程' if opening else 'Blender 导出设置')
        self.resize(600, 500 if opening else 740)
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
        self.mesh = QCheckBox('生成可编辑网格'); self.mesh.setChecked(settings.get('blender_as_mesh', True))
        self.points = QCheckBox('显示彩色点云'); self.points.setChecked(settings.get('blender_draw_points', True))
        self.background = QCheckBox('用于相机预览和渲染背景'); self.background.setChecked(settings.get('blender_background_images', True))
        self.warnings = QCheckBox('隐藏镜头畸变警告'); self.warnings.setChecked(settings.get('blender_suppress_warnings', True))
        from blender_export import default_point_radius
        self.radius = QDoubleSpinBox(); self.radius.setDecimals(4); self.radius.setRange(.0001, 100); self.radius.setValue(default_point_radius(settings))
        self.fps = QDoubleSpinBox(); self.fps.setDecimals(3); self.fps.setRange(.01, 240); self.fps.setValue(float(fps))
        self.frame_start_offset = QSpinBox(); self.frame_start_offset.setRange(0, 10000)
        self.frame_start_offset.setValue(int(settings.get('blender_frame_start_offset', 0)))
        self.frame_start_offset.setSuffix(' 帧')
        if not opening:
            group = QGroupBox('摄影测量场景'); form = QFormLayout(group)
            self.sequence_fields = BlenderSequenceFields({
                'export_sequence': settings.get('blender_export_sequence', True),
                'sequence_format': settings.get('blender_sequence_format', 'jpg'),
                'sequence_resolution': settings.get('blender_sequence_resolution', 'original'),
            }, self.background)
            form.addRow(self.sequence_fields)
            for field in (self.points, self.mesh, self.warnings): form.addRow(field)
            form.addRow('网格渲染点半径', self.radius); form.addRow('Blender FPS', self.fps)
            form.addRow('前置空帧', self.frame_start_offset)
            self.frame_start_offset.setToolTip('0 从第 1 帧开始；30 从第 31 帧开始，相机和背景同步偏移。')
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(group); layout.addWidget(scroll, 1)
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
            frame_start_offset=self.frame_start_offset.value(), paths=self.settings.get('blender_paths', []),
            **(self.sequence_fields.values() if not self.opening else {}))
        self.accept()
