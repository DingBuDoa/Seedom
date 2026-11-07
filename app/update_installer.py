"""Run from a local snapshot after the UI exits; install, back up and restart."""
import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

# The bundled Python uses an isolated ._pth and omits the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hotfix import apply, safe_path


def wait_for_exit(pid, timeout=120):
    if pid <= 0 or pid == os.getpid():
        raise ValueError('Invalid application process ID.')
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # The process has already exited.
                return
            raise OSError(ctypes.get_last_error(), 'Cannot wait for the application to exit.')
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise TimeoutError('Application is still running; no files were replaced.')
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise TimeoutError('Application is still running; no files were replaced.')


@contextmanager
def install_lock(target):
    lock_path = safe_path(target, '.updates/install.lock')
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+b') as stream:
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def restart(target):
    executable = safe_path(target, 'runtime/python/pythonw.exe')
    if not executable.is_file():
        executable = safe_path(target, 'runtime/python/python.exe')
    subprocess.Popen([str(executable), '-B', str(safe_path(target, 'app/launch.py'))],
                     cwd=str(target), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--wait-pid', required=True, type=int)
    parser.add_argument('--no-restart', action='store_true')
    args = parser.parse_args()
    target = args.target.resolve()
    log_path = Path(__file__).resolve().parent / 'installer.log'
    with log_path.open('a', encoding='utf-8', buffering=1) as log:
        try:
            print(f'{datetime.now().isoformat()} Waiting for application exit', file=log)
            wait_for_exit(args.wait_pid)
            with install_lock(target):
                backup = apply(args.package, target)
            print(f'UPDATE COMPLETE. Backup: {backup}', file=log)
            if not args.no_restart:
                restart(target)
            return 0
        except Exception:
            traceback.print_exc(file=log)
            if os.name == 'nt':
                ctypes.windll.user32.MessageBoxW(None,
                    f'更新未完成。请查看日志：\n{log_path}\n\n'
                    '已修改的文件会自动回退；如有需要，可运行 patch-backups 中的 Restore.bat。',
                    'SeedomVFX 更新失败', 16)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
