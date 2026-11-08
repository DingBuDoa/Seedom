"""Shared, restrained Qt styling and small vector icons."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor
from PySide6.QtSvg import QSvgRenderer
from pathlib import Path
from PySide6.QtWidgets import QAbstractSpinBox


def inset_numeric_fields(parent):
    # Qt owns the inner editor geometry; use text margins for a stable inset.
    for control in parent.findChildren(QAbstractSpinBox):
        control.lineEdit().setTextMargins(10, 0, 0, 0)


LIGHT_STYLE = '''
QWidget { color: #1c1f26; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QMainWindow, QWidget#workspace { background: #f4f5f7; }
QWidget#sidebar { background: #fbfbfc; border-right: 1px solid #e3e5e9; }
QWidget#header, QWidget#detail, QDialog { background: white; }
QWidget#detailSummary { background: transparent; }
QWidget#recentLogsPanel { background: transparent; }
QWidget#detailField { background: transparent; }
QWidget#header { border-bottom: 1px solid #e3e5e9; }
QLabel { background: transparent; border: none; }
QLabel#title { font-size: 20px; font-weight: 600; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#detailCaption { color: #89929f; font-size: 11px; }
QLabel#detailValue { color: #28313d; font-size: 12px; }
QLabel#detailStatus { background: #eaf0ff; color: #315fc5; border-radius: 9px; padding: 2px 8px; font-size: 11px; }
QLabel#detailStatus[state="完成"] { background: #e8f5ee; color: #19734f; }
QLabel#detailStatus[state="运行中"] { background: #fff4df; color: #94600b; }
QLabel#detailStatus[state="失败"], QLabel#detailStatus[state="文件不完整"] { background: #fdeceb; color: #b43c35; }
QLabel#detailMessage { color: #697382; font-size: 11px; }
QLabel#muted { color: #8b939f; font-size: 12px; }
QLabel#brand { font-family: "Segoe UI"; font-size: 18px; font-weight: 600; }
QLabel#mark { background: transparent; }
QLabel#banner { background: #fff8e6; border: 1px solid #f0dca8; border-radius: 6px; color: #836314; padding: 10px; }
QWidget#solverNotice { background: #fff8e6; border-bottom: 1px solid #f0dca8; }
QLabel#solverNoticeText { color: #836314; }
QPushButton, QToolButton { background: white; border: 1px solid #d3d6dc; border-radius: 6px; padding: 7px 12px; min-height: 18px; }
QPushButton:hover, QToolButton:hover { background: #f5f6f8; border-color: #bdc3ce; }
QPushButton:pressed { background: #eef0f4; }
QPushButton:disabled, QToolButton:disabled { color: #b4bac4; background: #fafbfc; border-color: #e5e8ed; }
QPushButton#primary { background: #2f6bff; border-color: #2f6bff; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #2559db; }
QPushButton#startQueue { background: #15845d; border-color: #15845d; color: white; font-weight: 700; }
QPushButton#startQueue:hover { background: #116c4b; }
QPushButton#startQueue:disabled { background: #a8c9bc; border-color: #a8c9bc; color: #f5f7ff; }
QPushButton#primary:disabled { background: #aebfe9; border-color: #aebfe9; color: #f5f7ff; }
QPushButton#danger { color: #d8443c; }
QPushButton#iconButton { padding: 0; }
QComboBox#sortBox { padding-left: 12px; padding-right: 36px; }
QComboBox#sortBox::drop-down { subcontrol-origin: border; subcontrol-position: top right; width: 30px; border: none; }
QPushButton#nav { border: 1px solid transparent; background: transparent; text-align: left; padding: 10px 14px; color: #626a76; }
QPushButton#nav:hover { background: #eff0f3; }
QPushButton#nav:checked { background: white; border-color: #e3e5e9; color: #1c1f26; font-weight: 600; }
QPushButton#segment { background: transparent; border: 0; color: #737c89; border-radius: 4px; padding: 0 12px; min-height: 0; }
QPushButton#segment:checked { background: white; color: #2f6bff; }
QWidget#segments { background: #ebedf1; border-radius: 6px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #d3d6dc; border-radius: 5px; padding: 7px 9px; selection-background-color: #dce6ff; selection-color: #1c1f26; }
QLineEdit QToolButton { background: transparent; border: none; padding: 0; min-height: 0; }
QLineEdit#taskSearch { padding-top: 0; padding-bottom: 0; }
QSpinBox, QDoubleSpinBox { padding-left: 12px; }
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit { border: none; padding: 0; background: transparent; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #2f6bff; }
QLineEdit:read-only { background: #fafbfc; color: #626a76; }
QPlainTextEdit#recentTaskLogs { background: #fafbfc; border: 1px solid #e3e5e9; border-radius: 5px; padding: 7px; color: #4d5868; font-size: 10px; }
QTextBrowser#updateNotes { background: transparent; border: none; color: #697382; padding: 0; }
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background: #f5f6f8; color: #a5adb9; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { width: 20px; border: none; }
QComboBox QAbstractItemView { background: white; border: 1px solid #e3e5e9; selection-background-color: #eaf0ff; selection-color: #1c1f26; padding: 4px; }
QTableView { background: white; border: 1px solid #e3e5e9; border-radius: 7px; gridline-color: #f0f1f4; selection-background-color: #eaf0ff; selection-color: #1c1f26; outline: 0; }
QTableView::item { border: none; padding: 0 12px; }
QTableView::item:selected { background: #eaf0ff; }
QHeaderView::section { background: #fafbfc; color: #89929f; font-size: 12px; font-weight: 400; border: none; border-bottom: 1px solid #e3e5e9; padding: 11px 12px; }
QHeaderView { background: #fafbfc; }
QGroupBox { background: white; border: 1px solid #e3e5e9; border-radius: 6px; margin-top: 12px; padding: 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; font-weight: 600; }
QCheckBox { spacing: 8px; padding: 5px 0; }
QCheckBox::indicator { width: 14px; height: 14px; }
QProgressBar { border: 0; border-radius: 2px; background: #e6e9ef; max-height: 4px; text-align: center; }
QProgressBar::chunk { background: #2f6bff; border-radius: 2px; }
QProgressBar#updateProgress { min-height: 8px; max-height: 8px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: #f5f6f8; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #ced3dc; min-height: 30px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #f5f6f8; height: 9px; margin: 0; }
QScrollBar::handle:horizontal { background: #ced3dc; min-width: 30px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QSplitter::handle { background: #e3e5e9; height: 1px; }
QMenu { background: white; border: 1px solid #e3e5e9; padding: 5px; }
QMenu::item { padding: 8px 24px 8px 12px; border-radius: 4px; }
QMenu::item:selected { background: #eaf0ff; }
QMenu::item:disabled { color: #b4bac4; }
QToolTip { background: #1c1f26; color: white; padding: 6px; border: none; }
QStatusBar { background: white; border-top: 1px solid #e3e5e9; color: #626a76; }
QDialogButtonBox { background: transparent; }
QWidget#aboutCanvas { background: #f4f5f7; }
QWidget#aboutCard { background: white; border: 1px solid #e3e5e9; border-radius: 10px; }
QLabel#aboutLogo { background: transparent; }
QLabel#aboutName { font-family: "Segoe UI"; font-size: 23px; font-weight: 600; }
QLabel#aboutSubtitle { color: #98a0ac; font-size: 12px; padding-top: 4px; }
QWidget#aboutMeta { border-top: 1px solid #e3e5e9; border-bottom: 1px solid #e3e5e9; }
QLabel#aboutRelease { color: #2f6bff; font-weight: 600; font-size: 12px; }
QLabel#aboutDate { color: #626a76; font-size: 12px; }
QLabel#aboutDescription { color: #7a8391; font-size: 12px; }
QLabel#aboutSection { font-size: 13px; font-weight: 600; }
QWidget#aboutProject { border-bottom: 1px solid #eef0f3; }
QLabel#aboutProjectName { color: #303744; font-size: 12px; font-weight: 600; }
QLabel#aboutPurpose { color: #98a0ac; font-size: 11px; }
QLabel#aboutVersion { color: #626a76; font-size: 11px; font-family: "Segoe UI", "Microsoft YaHei UI"; }
QLabel#aboutLink { font-size: 11px; }
QWidget#aboutRepositoryCard { background: #f0f5ff; border: 1px solid #dce7ff; border-radius: 8px; }
QLabel#aboutRepository { font-size: 12px; }
QLabel#aboutFootnote { color: #98a0ac; font-size: 11px; }
'''

_DARK_COLORS = {
    '#1c1f26': '#edf1f7', '#f4f5f7': '#171a21', '#fbfbfc': '#1f242d',
    '#e3e5e9': '#353d49', '#ffffff': '#252b35', '#fafbfc': '#202630',
    '#89929f': '#a6afbd', '#28313d': '#dce3ec', '#eaf0ff': '#263b66',
    '#315fc5': '#9bbcff', '#e8f5ee': '#1d4739', '#19734f': '#7ce0b1',
    '#fff4df': '#4a3920', '#94600b': '#f0c36d', '#fdeceb': '#4b292d',
    '#b43c35': '#ff9b91', '#697382': '#aeb8c7', '#8b939f': '#9aa6b6',
    '#fff8e6': '#443a20', '#f0dca8': '#6d5d31', '#836314': '#f2d27a',
    '#d3d6dc': '#46505e', '#f5f6f8': '#2b323d', '#bdc3ce': '#5c6878',
    '#eef0f4': '#303845', '#b4bac4': '#788394', '#e5e8ed': '#3c4552',
    '#2f6bff': '#78a7ff', '#2559db': '#5c8df0', '#15845d': '#28a879',
    '#116c4b': '#19865c', '#a8c9bc': '#487966', '#f5f7ff': '#f3f6fc',
    '#aebfe9': '#526b9e', '#d8443c': '#ff8178', '#626a76': '#a5afbf',
    '#737c89': '#a0aaba', '#ebedf1': '#2a313c', '#dce6ff': '#304b78',
    '#4d5868': '#b5bfce', '#f0f1f4': '#303743', '#eef0f3': '#303743',
    '#f0f5ff': '#202f4a', '#dce7ff': '#3c5275', '#7a8391': '#aeb8c6',
    '#303744': '#d7dee9', '#98a0ac': '#9ca8b8', '#a5adb9': '#7b8798',
    '#ced3dc': '#566272', '#e6e9ef': '#343d4a', '#eff0f3': '#2a313c',
    '#c7ccd5': '#657183', '#16865f': '#38c58d', '#eaf7f1': '#1d4739',
    '#fdecea': '#4b292d', '#a77516': '#d3a34e', '#fff6e3': '#4a3920',
    '#f0f2f5': '#303743', '#a8c9bc': '#487966',
}


def _style_suffix():
    style = 'QComboBox::down-arrow { image: url("' + (Path(__file__).parent / 'icons/chevron.svg').as_posix() + '"); width: 10px; height: 6px; }'
    for control in ('QSpinBox', 'QDoubleSpinBox'):
        style += f'{control} {{ padding-right: 24px; }}'
        for direction, position, name in (('up', 'top', 'chevron-up.svg'), ('down', 'bottom', 'chevron.svg')):
            style += f'{control}::{direction}-button {{ subcontrol-origin: border; subcontrol-position: {position} right; width: 22px; border: none; }}'
            style += f'{control}::{direction}-arrow {{ image: url("' + (Path(__file__).parent / 'icons' / name).as_posix() + '"); width: 8px; height: 5px; }'
    return style


STYLE = LIGHT_STYLE + _style_suffix()
_ACTIVE_THEME = 'light'


def set_active_theme(theme):
    global _ACTIVE_THEME
    _ACTIVE_THEME = 'dark' if theme == 'dark' else 'light'


def theme_color(value):
    return _DARK_COLORS.get(value.lower(), value) if _ACTIVE_THEME == 'dark' else value


def style_for_theme(theme):
    set_active_theme(theme)
    if theme != 'dark':
        return LIGHT_STYLE + _style_suffix()
    style = LIGHT_STYLE
    for source, target in _DARK_COLORS.items():
        style = style.replace(source, target)
    style = style.replace('white', '#252b35')
    return style + _style_suffix()


def detect_system_theme(app):
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return 'dark'
        if scheme == Qt.ColorScheme.Light:
            return 'light'
    except (AttributeError, TypeError):
        pass
    return 'dark' if app.palette().window().color().lightness() < 128 else 'light'

PATHS = {
    'up': '<path d="m6 10 6-6 6 6M12 4v16"/>',
    'down': '<path d="m6 14 6 6 6-6M12 4v16"/>',
    'tasks': '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    'settings': '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="white"/><circle cx="15" cy="12" r="2" fill="white"/><circle cx="9" cy="18" r="2" fill="white"/>',
    'about': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'play': '<path d="m8 5 11 7-11 7z"/>',
    'stop': '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    'folder': '<path d="M3 7V5h7l2 2h9v13H3z"/>',
    'edit': '<path d="m15 4 5 5M4 20l5-1L20 8l-5-5L4 14z"/>',
    'more': '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    'refresh': '<path d="M20 10a8 8 0 1 0-2 8M20 4v6h-6"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    'log': '<path d="M6 3h9l4 4v14H6zM15 3v5h4M9 12h7M9 16h7"/>',
    'pause': '<path d="M8 5v14M16 5v14"/>',
    'trash': '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
}


def icon(name, color='#697382'):
    path = PATHS.get(name, PATHS['tasks'])
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">{path}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
