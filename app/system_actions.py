"""Small, explicit Windows power actions used after a queue finishes."""
from __future__ import annotations

import os
import subprocess

POWER_ACTIONS = {'shutdown': '关机', 'sleep': '休眠'}


def power_action_label(action: str) -> str:
    return POWER_ACTIONS.get(action, POWER_ACTIONS['shutdown'])


def execute_power_action(action: str) -> None:
    """Request the selected system power action after queue state is saved."""
    if os.name != 'nt':
        raise OSError('完成队列自动关机/休眠仅支持 Windows。')
    command = (['rundll32.exe', 'powrprof.dll,SetSuspendState', '0', '1', '0']
               if action == 'sleep' else ['shutdown.exe', '/s', '/t', '0'])
    subprocess.Popen(command, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
