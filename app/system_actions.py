"""Small, explicit Windows power actions used after a queue finishes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess

POWER_ACTIONS = {'shutdown': '关机', 'sleep': '休眠'}

_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                ('fFlags', ctypes.c_uint16), ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]


def send_to_recycle_bin(paths) -> None:
    """Move files or folders to the Windows Recycle Bin so they can be restored."""
    if os.name != 'nt':
        raise OSError('移入回收站仅支持 Windows。')
    items = [str(Path(item).resolve()) for item in paths]
    if not items:
        return
    for item in items:
        if not Path(item).exists():
            raise FileNotFoundError(f'路径不存在：{item}')
    source = ctypes.create_unicode_buffer('\0'.join(items) + '\0')
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = _FO_DELETE
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    operation.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(result, f'无法移入回收站（代码 {result}）')
    if operation.fAnyOperationsAborted:
        raise OSError('移入回收站的操作被取消。')


def power_action_label(action: str) -> str:
    return POWER_ACTIONS.get(action, POWER_ACTIONS['shutdown'])


def execute_power_action(action: str) -> None:
    """Request the selected system power action after queue state is saved."""
    if os.name != 'nt':
        raise OSError('完成队列自动关机/休眠仅支持 Windows。')
    command = (['rundll32.exe', 'powrprof.dll,SetSuspendState', '0', '1', '0']
               if action == 'sleep' else ['shutdown.exe', '/s', '/t', '0'])
    subprocess.Popen(command, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
