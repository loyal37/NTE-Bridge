"""Reusable bridge-owned scratch directories; never keep per-operation copies."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from .core import BridgeError, write_json

MARKER = '.nte_bridge_workspace.json'


def _plain_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise BridgeError('工作目录包含重定向链接：' + str(path))
    if path.exists() and (not path.is_dir() or getattr(path.lstat(), 'st_file_attributes', 0)
                          & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise BridgeError('工作目录不是普通文件夹：' + str(path))
    return path


def retry_io(operation):
    for attempt in range(7):
        try:
            return operation()
        except OSError as error:
            if attempt == 6 or not isinstance(error, PermissionError) and getattr(error, 'winerror', 0) not in {5, 32, 33}:
                raise
            time.sleep(0.15 * (attempt + 1))


def _retry_remove(function, path, exc_info):
    if isinstance(exc_info[1], OSError):
        retry_io(lambda: function(path))
    else:
        raise exc_info[1]


def owned_directory(path, kind, *, reset=False):
    path = _plain_directory(path)
    marker = path / MARKER
    expected = {'schema_version': 1, 'kind': kind, 'directory': str(path)}
    if path.exists():
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != expected:
            raise BridgeError('已有目录缺少匹配的桥接标记，不会覆盖：' + str(path))
    else:
        path.mkdir(parents=True)
        write_json(marker, expected)
    if reset:
        for child in path.iterdir():
            if child.name == MARKER:
                continue
            # This direct owned tree is scratch only. Python does not traverse
            # Windows junctions when rmtree removes them.
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=_retry_remove)
            else:
                retry_io(child.unlink)
    return path


@contextmanager
def directory_lock(path, message='工作目录正在使用，请等待当前操作完成。'):
    root = _plain_directory(path)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.nte_bridge.lock').open('a+b') as stream:
        locked = False
        try:
            try:
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                raise BridgeError(message) from error
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def check_background_use(path):
    """A worker may outlive a crashed Blender that held the operation lock."""
    invocation = Path(path) / 'ue_invocation.json'
    if invocation.is_file():
        pending = json.loads(invocation.read_text(encoding='utf-8'))
        if pending.get('stage') == 'pending' and pending.get('project_file'):
            from .unreal_transport import _project_editor_running
            if _project_editor_running(pending['project_file']):
                raise BridgeError('上次 UE 同步尚未结束，请等待完成；若已中断，请关闭目标 UE 后重试。')
    if os.name != 'nt':
        return
    command = ('Get-CimInstance Win32_Process -Filter "Name = \'blender.exe\' OR Name = \'python.exe\' '
               'OR Name = \'pythonw.exe\' OR Name = \'UnrealEditor-Cmd.exe\'" '
               '| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress')
    reply = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if reply.returncode:
        raise BridgeError('无法检查后台任务，尚未覆盖导出缓存。')
    rows = json.loads(reply.stdout or '[]')
    rows = [rows] if isinstance(rows, dict) else rows
    target = str(Path(path).resolve()).replace('\\', '/').casefold() + '/'
    for row in rows:
        if row['ProcessId'] != os.getpid() and target in (row.get('CommandLine') or '').replace('\\', '/').casefold():
            raise BridgeError('后台任务仍在使用这些导出文件，请等待它完成后再发送。')
