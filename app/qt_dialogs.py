"""Native Qt task configuration, Blender preferences, and bounded log windows."""
from dataclasses import asdict
from pathlib import Path
import queue
import threading

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QFormLayout, QGroupBox, QScrollArea, QWidget,
    QDoubleSpinBox, QSpinBox, QPlainTextEdit, QFileDialog, QSizePolicy, QFrame)

from tracker_core import Options, Pipeline, VIDEO_EXTENSIONS
from workload import video_info
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


class VideoInput(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText('拖入视频或文件夹，或手动输入完整路径（每行一个）')
        self.setMaximumHeight(82)

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


class TaskDialog(QDialog):
    def __init__(self, parent, options, job=None, paths=None, blender_records=None, blender_settings=None,
                 name_validator=None):
        super().__init__(parent)
        self.setWindowTitle('编辑任务' if job else '添加任务')
        self.resize(680, 760)
        self.setMinimumSize(520, 480)
        self.result_data = None
        self.job = job
        self.name_validator = name_validator
        self.blender_records = list(blender_records or [])
        self.blender_settings = dict(blender_settings or {})
        self.blender_scan_pending = queue.Queue()
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
        hl.addWidget(label('设置素材与处理参数，任务将加入工作区。'))
        root.addWidget(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 0, 18, 16)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        basic = QGroupBox('基本信息')
        form = QFormLayout(basic)
        form.setSpacing(10)
        self.name = QLineEdit(job.get('name', '') if job else '')
        self.name.setPlaceholderText('留空使用视频文件名；批量添加时作为名称前缀')
        form.addRow('任务名称', self.name)
        self.source = VideoInput()
        self.source.setPlainText(job['path'] if job else '\n'.join(map(str, paths or [])))
        if job:
            self.source.setReadOnly(True)
        form.addRow('输入素材', self.source)
        selectors = QHBoxLayout()
        selectors.addWidget(button('选择视频…', self.browse))
        selectors.addWidget(button('选择文件夹…', self.browse_folder))
        selectors.addStretch()
        if not job:
            form.addRow('', selectors)
        self.video_note = label('正在读取视频信息…' if self.source.toPlainText() else '支持 MP4、MOV、MKV 等常见视频格式')
        form.addRow('', self.video_note)
        self.output = QLineEdit(options.output_root)
        outrow = QHBoxLayout()
        outrow.addWidget(self.output)
        outrow.addWidget(button('浏览…', self.browse_output))
        form.addRow('输出根目录', outrow)
        layout.addWidget(basic)
        processing = QGroupBox('处理设置')
        form = QFormLayout(processing)
        form.setSpacing(10)
        self.backend = QComboBox()
        for key, text in SOLVER_CHOICES.items(): self.backend.addItem(text, key)
        self.backend.setCurrentIndex(0 if options.backend == 'fastmap' else 1)
        form.addRow('求解方案', self.backend)
        self.quality = QComboBox()
        for title, values in [('默认 · 沿用当前参数', None), ('高 · SIFT 4096 / 匹配 15', (4096, 15, 'high')),
                              ('中 · SIFT 2048 / 匹配 10', (2048, 10, 'balanced')), ('低 · SIFT 1024 / 匹配 5', (1024, 5, 'fast'))]:
            self.quality.addItem(title, values)
        form.addRow('计算精度', self.quality)
        self.quality_note = label(quality_note(options.solver_quality))
        form.addRow('', self.quality_note)
        self.rate = QComboBox()
        for title, value in [('默认 · 原始帧率', 0), ('30 帧 / 秒', 30), ('60 帧 / 秒', 60), ('自定义', -1)]:
            self.rate.addItem(title, value)
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
        self.scale = QComboBox()
        for title, value in [('默认 · 原始尺寸', 1.), ('0.8 倍', .8), ('0.5 倍', .5), ('0.2 倍', .2)]:
            self.scale.addItem(title, value)
        index = self.scale.findData(options.image_scale)
        if index < 0:
            self.scale.addItem(f'{options.image_scale:g} 倍', options.image_scale)
            index = self.scale.count() - 1
        self.scale.setCurrentIndex(index)
        form.addRow('处理分辨率', self.scale)
        self.size_note = label('尺寸缩放由 FFmpeg 完成；SIFT 最大尺寸是独立参数。')
        form.addRow('', self.size_note)
        layout.addWidget(processing)
        output = QGroupBox('输出设置')
        form = QFormLayout(output)
        self.sequence = QCheckBox('计算完成后导出 JPG 序列帧')
        self.sequence.setChecked(options.export_sequence)
        self.sequence_size = QComboBox()
        self.sequence_size.addItem('跟随处理分辨率', False)
        self.sequence_size.addItem('原始视频分辨率 · 重新抽帧', True)
        self.sequence_size.setCurrentIndex(int(options.sequence_original))
        form.addRow(self.sequence)
        form.addRow('序列帧尺寸', self.sequence_size)
        self.blend = QCheckBox('计算完成后生成 Blender 文件')
        self.blend.setChecked(bool(job and job.get('blender')))
        form.addRow(self.blend)
        self.blender_group = QGroupBox('Blender 导出设置（首次启动会自动缓存版本）')
        blender_form = QFormLayout(self.blender_group)
        blender_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.blender_note = label(f'检测到 {len(self.blender_records)} 个 Blender 版本。' if self.blender_records else '正在检测本机 Blender；完成后会显示版本列表。')
        blender_form.addRow('', self.blender_note)
        self.blender_version = QComboBox()
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
        form.addRow(label('项目文件夹以任务名命名；点云在 point could，日志在 log，可选序列帧在 video。\nBlender 文件直接放在项目文件夹，名称含版本号；必要的求解缓存统一存入 _cache。'))
        layout.addWidget(output)
        self.advanced_toggle = QPushButton('›  高级参数')
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle)
        advanced = QGroupBox('FastMap / GLOMAP 参数')
        form = QFormLayout(advanced)
        self.max_size = QSpinBox(); self.max_size.setRange(256, 16384); self.max_size.setValue(options.max_size)
        self.overlap = QSpinBox(); self.overlap.setRange(1, 200); self.overlap.setValue(options.overlap)
        self.gpu_min = QSpinBox(); self.gpu_min.setRange(2, 1000000); self.gpu_min.setValue(options.gpu_min_images)
        self.camera = QComboBox()
        self.camera.addItem('自动估计畸变（默认）', 'radial'); self.camera.addItem('近似针孔（无畸变）', 'pinhole')
        self.camera.setCurrentIndex(max(0, self.camera.findData(options.camera_mode)))
        self.decode = QCheckBox('CUDA 视频解码（失败时沿用 CPU 解码回退）'); self.decode.setChecked(options.cuda_decode)
        self.color = QCheckBox('FastMap 点云取色'); self.color.setChecked(options.color)
        self.gpu = QCheckBox('GLOMAP 使用 CUDA / cuDSS 求解'); self.gpu.setChecked(options.glomap_gpu)
        self.solver_quality = QComboBox()
        for key, profile in SOLVER_PROFILES.items(): self.solver_quality.addItem(profile['title'], key)
        self.solver_quality.setCurrentIndex(self.solver_quality.findData(options.solver_quality))
        self.solver_quality.currentIndexChanged.connect(lambda: self.quality_note.setText(quality_note(self.solver_quality.currentData())))
        form.addRow('FastMap 优化档位', self.solver_quality)
        for name, field in [('SIFT 最大尺寸', self.max_size), ('相邻匹配范围', self.overlap), ('GPU 最少图像', self.gpu_min), ('相机模型', self.camera)]:
            form.addRow(name, field)
        for field in (self.decode, self.color, self.gpu):
            form.addRow(field)
        layout.addWidget(advanced)
        advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda checked: self.advanced_toggle.setText(('⌄' if checked else '›') + '  高级参数'))
        self.quality.currentIndexChanged.connect(self.apply_quality)
        self.rate.currentIndexChanged.connect(self.update_controls)
        self.scale.currentIndexChanged.connect(self.update_controls)
        self.backend.currentIndexChanged.connect(self.update_controls)
        self.sequence.toggled.connect(self.update_controls)
        self.blend.toggled.connect(self.blender_group.setVisible)
        foot = QWidget()
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
            return
        available = screen.availableGeometry().adjusted(16, 16, -16, -16)
        width = min(680, available.width())
        height = min(760, available.height())
        self.setMinimumSize(min(520, width), min(480, height))
        self.resize(width, height)
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

    def paths(self):
        values = []
        for line in self.source.toPlainText().splitlines():
            path = Path(line.strip().strip('"'))
            if not line.strip():
                continue
            if path.is_dir() and not (self.job and self.job.get('scene')):
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
                infos = {} if self.job and self.job.get('scene') else {path: video_info(path, 0) for path in paths}
                self.pending.put((generation, infos, '' if paths else '请选择至少一个视频。'))
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
            elif len(infos) == 1:
                info = next(iter(infos.values()))
                self.video_note.setText(f"{info['width']} × {info['height']}  ·  {info['fps']:.3f} fps  ·  {info['duration']:.1f} 秒")
            else:
                self.video_note.setText(f'已读取 {len(infos)} 个视频 · 每个视频创建一个独立任务')
            self.update_controls()

    def apply_quality(self):
        values = self.quality.currentData()
        if values:
            self.max_size.setValue(values[0]); self.overlap.setValue(values[1])
            self.solver_quality.setCurrentIndex(self.solver_quality.findData(values[2]))

    def update_controls(self, *_):
        self.custom_rate.setVisible(self.rate.currentData() == -1)
        rates = [info['fps'] for info in self.metadata.values() if info['fps'] > 0]
        rate = min(rates) if rates else None
        for index, threshold in ((1, 30), (2, 60)):
            self.rate.model().item(index).setEnabled(rate is not None and rate + .001 >= threshold)
        self.rate.model().item(3).setEnabled(rate is not None)
        if rate is not None:
            self.custom_rate.setMaximum(min(240, rate))
        self.sequence_size.setEnabled(self.sequence.isChecked())
        self.gpu.setEnabled(self.backend.currentData() == 'glomap')
        self.color.setEnabled(self.backend.currentData() == 'fastmap')
        self.camera.setEnabled(self.backend.currentData() == 'fastmap')
        self.solver_quality.setEnabled(self.backend.currentData() == 'fastmap')
        if len(self.metadata) == 1:
            info = next(iter(self.metadata.values()))
            scale = self.scale.currentData()
            width, height = info.get('width', 0) or 0, info.get('height', 0) or 0
            w, h = (width, height) if scale == 1 else (max(2, int(width * scale / 2) * 2), max(2, int(height * scale / 2) * 2))
            self.size_note.setText(f'处理尺寸 {w} × {h} · FFmpeg 缩放，SIFT 最大尺寸独立控制')

    def accept_task(self):
        try:
            if self.probing or self.debounce.isActive():
                raise ValueError('请等待视频信息读取完成。')
            paths = self.paths()
            if not paths:
                raise ValueError('请选择至少一个视频。')
            scene = bool(self.job and self.job.get('scene'))
            if not scene and (set(paths) != set(self.metadata) or any(not Path(p).is_file() or Path(p).suffix.lower() not in VIDEO_EXTENSIONS for p in paths)):
                raise ValueError('视频路径无效或无法读取，请检查输入素材。')
            if self.name_validator:
                prefix = self.name.text().strip()
                names = [prefix + (' · ' + Path(path).stem if prefix and len(paths) > 1 else '')
                         or Path(path).stem for path in paths]
                self.name_validator(names, ignore_id=(self.job or {}).get('id'))
            fps = self.rate.currentData()
            if fps == -1:
                fps = self.custom_rate.value()
            if fps and (not self.metadata or any(info['fps'] + .001 < fps for info in self.metadata.values())):
                raise ValueError('处理帧率不能超过任一输入视频的原始帧率。')
            if not self.output.text().strip():
                raise ValueError('请选择输出目录。')
            options = Options(**{**asdict(self.base), 'backend': self.backend.currentData(),
                'output_root': str(Path(self.output.text().strip()).resolve()), 'fps': fps,
                'max_size': self.max_size.value(), 'overlap': self.overlap.value(),
                'gpu_min_images': self.gpu_min.value(), 'camera_mode': self.camera.currentData(),
                'cuda_decode': self.decode.isChecked(), 'color': self.color.isChecked(),
                'glomap_gpu': self.gpu.isChecked(), 'image_scale': self.scale.currentData(),
                'export_sequence': self.sequence.isChecked(), 'sequence_original': self.sequence_size.currentData(),
                'solver_quality': self.solver_quality.currentData()})
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
        self.blender_version = QComboBox()
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
