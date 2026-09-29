"""One cache location for Blender jobs and their UE reports and working files."""
from pathlib import Path

import bpy

from .core import BridgeError
from .workflow import default_cache_root, legacy_default_job_root


def _directory(value):
    value = str(value).strip().strip('"').strip("'")
    if not value:
        raise BridgeError('请在高级设置选择缓存目录。')
    if value.startswith('//') and not bpy.data.filepath:
        raise BridgeError('未保存的 Blender 工程请使用绝对缓存目录。')
    path = Path(bpy.path.abspath(value)).expanduser()
    if not path.is_absolute():
        raise BridgeError('缓存目录需要绝对路径，或相对于已保存 Blender 工程的 // 路径。')
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise BridgeError('缓存目录指向了文件，请选择文件夹。')
    return path


def _context_path(value):
    value = str(value).strip().strip('"').strip("'")
    if not value or (value.startswith('//') and not bpy.data.filepath):
        return ''
    return bpy.path.abspath(value)


def cache_location(settings):
    """Resolve without writes; legacy custom non-C directories remain usable."""
    if settings.cache_root.strip():
        return _directory(settings.cache_root)
    legacy = settings.job_root.strip()
    if legacy and legacy != '//NTEBridgeJobs' and not legacy_default_job_root(legacy):
        try:
            old = _directory(legacy)
            if old.drive.casefold().removeprefix('\\\\?\\') != 'c:':
                return old.parent if old.name.casefold() == 'jobs' else old
        except BridgeError:
            pass
    automatic = default_cache_root(
        _context_path(settings.project_file), bpy.data.filepath,
        _context_path(settings.source_folder))
    if not automatic:
        raise BridgeError('请在高级设置选择缓存目录；不会自动使用 C 盘或系统临时目录。')
    return Path(automatic)


def ensure_cache(settings):
    root = cache_location(settings)
    if not settings.cache_root.strip():
        settings.cache_root = str(root)
    jobs = root / 'Jobs'
    settings.job_root = str(jobs)
    return jobs


def selected_manifest(settings):
    """Old-job sync and packaging may only write inside the active cache."""
    jobs = ensure_cache(settings).resolve()
    if not settings.last_manifest:
        raise BridgeError('请先在当前缓存目录导出或发送一次角色。')
    path = Path(bpy.path.abspath(settings.last_manifest)).resolve()
    if not path.is_relative_to(jobs):
        settings.last_manifest = ''
        settings.last_report = ''
        raise BridgeError('最近任务不在当前缓存目录，请重新导出或发送角色。')
    if not path.is_file():
        raise BridgeError('最近任务文件不存在，请重新导出或发送角色。')
    return path
