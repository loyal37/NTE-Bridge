"""Small persistent packaging preferences, separate from disposable cook/jobs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

import bpy

from .blender_cache import cache_location
from .core import BridgeError

FIELDS = ('package_output', 'mod_name', 'cook_export_directory', 'packager_source',
          'cook_search', 'cook_type', 'cook_show_dependencies')
_muted = 0


@contextmanager
def suspend():
    global _muted
    _muted += 1
    try:
        yield
    finally:
        _muted -= 1


def _location(settings):
    from .blender_packaging import character_folder, resolved_project
    project = str(resolved_project(settings))
    blend = str(Path(bpy.data.filepath).resolve()) if bpy.data.filepath else '<unsaved>'
    identity = [os.path.normcase(project), os.path.normcase(blend)]
    key = hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()[:24]
    return cache_location(settings) / 'Settings' / (key + '.json'), identity, character_folder(settings).casefold()


def _read(path, identity):
    if not path.is_file():
        return {'schema_version': 1, 'identity': identity, 'roles': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or data.get('identity') != identity or not isinstance(data.get('roles'), dict):
        raise BridgeError('保存的打包设置与当前工程不匹配。')
    return data


def saved_choices(settings):
    try:
        path, identity, scope = _location(settings)
        return _read(path, identity)['roles'].get(scope, {})
    except (BridgeError, OSError, ValueError):
        return {}


def remember(settings, *, selection=True):
    if _muted:
        return
    try:
        path, identity, scope = _location(settings)
    except (BridgeError, OSError, ValueError):
        return  # The UE project / role is not configured yet.
    try:
        data = _read(path, identity)
        record = data['roles'].setdefault(scope, {})
        record.update({name: getattr(settings, name) for name in FIELDS})
        if selection and settings.cook_report:
            record['selected_assets'] = [entry.asset_path for entry in settings.cook_assets
                                         if entry.selected and entry.packable]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    except (BridgeError, OSError, ValueError) as error:
        settings.status = '保存打包设置失败：' + str(error)


def changed(settings, context):
    remember(settings, selection=False)


def selection_changed(entry, context):
    settings = getattr(entry.id_data, 'nte_bridge', None)
    if settings is not None:
        remember(settings)


def restore_choices(settings):
    record = saved_choices(settings)
    with suspend():
        for name in FIELDS:
            if name in record:
                try:
                    setattr(settings, name, record[name])
                except (TypeError, ValueError):
                    pass
        if 'selected_assets' in record:
            selected = set(record['selected_assets'])
            for entry in settings.cook_assets:
                entry.selected = entry.packable and entry.asset_path in selected


def restore_session(settings):
    """Reopen the latest valid cook even if the blend was saved before cooking."""
    from .blender_packaging import character_folder, load_cooked_assets, resolved_project, verified_cook
    from .cooking import cook_cache_directory
    restore_choices(settings)
    try:
        if settings.cook_report:
            try:
                verified_cook(settings)
            except (BridgeError, OSError, ValueError, KeyError):
                pass
        if not settings.cook_report:
            report = cook_cache_directory(cache_location(settings), resolved_project(settings),
                                          character_folder(settings)) / 'cook_report.json'
            if report.is_file():
                load_cooked_assets(settings, report)
    except (BridgeError, OSError, ValueError, KeyError):
        pass  # Remembered choices survive until a matching successful recook.
