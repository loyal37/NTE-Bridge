"""Blender state for independent character cooking and explicit asset selection."""
import hashlib
from pathlib import Path, PurePosixPath
import sys

import bpy

from .blender_cache import cache_location, ensure_cache
from .core import BridgeError, package_path


TYPE_ITEMS = [('ALL', '全部类型', ''), ('SkeletalMesh', '骨骼网格', ''), ('Texture2D', '贴图', ''),
              ('MaterialInstanceConstant', '材质实例', ''), ('Blueprint', '蓝图', ''), ('OTHER', '其他类型', '')]
TYPE_LABELS = {'SkeletalMesh': '骨骼网格', 'Texture2D': '贴图', 'MaterialInstanceConstant': '材质实例',
               'Material': '母材质', 'Skeleton': '骨架', 'PhysicsAsset': '物理资产',
               'Blueprint': '蓝图', 'AnimBlueprint': '动画蓝图', 'WidgetBlueprint': '界面蓝图'}
TYPE_ICONS = {'SkeletalMesh': 'OUTLINER_OB_MESH', 'Texture2D': 'IMAGE_DATA',
              'MaterialInstanceConstant': 'MATERIAL', 'Material': 'MATERIAL', 'Skeleton': 'ARMATURE_DATA',
              'PhysicsAsset': 'PHYSICS', 'Blueprint': 'NODETREE', 'AnimBlueprint': 'NODETREE', 'WidgetBlueprint': 'NODETREE'}


def clear_cook(settings):
    settings.cook_report = ''
    settings.cook_report_sha256 = ''
    settings.cook_assets.clear()
    settings.cook_active_asset = 0


def resolved_project(settings):
    value = settings.project_file.strip().strip('"').strip("'")
    if not value or (value.startswith('//') and not bpy.data.filepath):
        raise BridgeError('请选择 UE 工程 .uproject 文件。')
    path = Path(bpy.path.abspath(value)).expanduser()
    if not path.is_absolute():
        raise BridgeError('UE 工程需要绝对路径。')
    path = path.resolve()
    if path.is_dir():
        choices = sorted(path.glob('*.uproject'))
        if len(choices) != 1:
            raise BridgeError('工程目录中不是唯一的 .uproject，请选择具体工程文件。')
        path = choices[0]
    if path.suffix.lower() != '.uproject' or not path.is_file():
        raise BridgeError('UE 工程文件不存在，请选择 .uproject 文件。')
    return path


def default_character_folder(settings):
    mesh = settings.mesh_path.strip().rstrip('/')
    if not mesh.startswith('/Game/'):
        return ''
    parts = PurePosixPath(mesh).parts
    source_name = Path(settings.source_folder.strip().strip('"').strip("'").rstrip('/\\')).name
    matches = [i for i, part in enumerate(parts[:-1]) if source_name and part.casefold() == source_name.casefold()]
    if matches:
        return str(PurePosixPath(*parts[:matches[-1] + 1]))
    if len(parts) > 5 and tuple(part.casefold() for part in parts[1:4]) == ('game', 'characters', 'player'):
        return str(PurePosixPath(*parts[:5]))
    return str(PurePosixPath(mesh).parent)


def character_folder(settings):
    folder = settings.cook_folder.strip().strip('"').strip("'").rstrip('/') if settings.cook_use_custom else default_character_folder(settings)
    if not folder:
        raise BridgeError('无法确定角色目录，请启用自定义 UE 烘焙目录并选择工程中的角色文件夹。')
    return package_path(folder, 'UE 烘焙目录')


def folder_from_directory(settings, directory):
    content = (resolved_project(settings).parent / 'Content').resolve()
    value = str(directory).strip().strip('"').strip("'")
    chosen = Path(bpy.path.abspath(value)).resolve() if value else None
    if chosen is None or not chosen.is_dir() or chosen == content or not chosen.is_relative_to(content):
        raise BridgeError('请选择当前 UE 工程 Content 内的一个角色文件夹。')
    return package_path('/Game/' + chosen.relative_to(content).as_posix(), 'UE 烘焙目录')


def size_label(value):
    size = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024


def _read_current_report(settings, path, expected_sha256=None):
    from .cooking import load_cook_report
    path = Path(path).resolve()
    root = cache_location(settings).resolve() / 'Cooks'
    if not path.is_relative_to(root):
        raise BridgeError('烘焙结果不在当前缓存目录，请重新烘焙。')
    report = load_cook_report(path, expected_sha256=expected_sha256, verify_files=False)
    if Path(report['project_file']).resolve() != resolved_project(settings):
        raise BridgeError('UE 工程已变更，请为当前工程重新烘焙。')
    if report['character_folder'].casefold() != character_folder(settings).casefold():
        raise BridgeError('角色烘焙目录已变更，请重新烘焙。')
    if not Path(report['cooked_root']).resolve().is_relative_to(root):
        raise BridgeError('烘焙输出不在当前缓存目录，请重新烘焙。')
    return report


def verified_cook(settings):
    if not settings.cook_report or not settings.cook_report_sha256:
        raise BridgeError('请先烘焙角色目录，再选择本次要导出的资产。')
    try:
        return _read_current_report(settings, bpy.path.abspath(settings.cook_report), settings.cook_report_sha256)
    except (BridgeError, OSError, ValueError, KeyError):
        clear_cook(settings)
        raise


def load_cooked_assets(settings, report_path):
    path = Path(report_path).resolve()
    report = _read_current_report(settings, path)
    clear_cook(settings)
    settings.cook_report = str(path)
    settings.cook_report_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    for asset in report['assets']:
        entry = settings.cook_assets.add()
        entry.name = asset['asset_path']
        entry.asset_path = asset['asset_path']
        entry.asset_type = asset['asset_type']
        entry.size_bytes = str(asset['bytes'])
        entry.size_text = size_label(asset['bytes'])
        entry.packable = bool(asset['packable'])
        entry.dependency = bool(asset.get('dependency', False))
        entry.reason = asset.get('reason', '')
        entry.selected = False
    settings.cook_active_asset = next((index for index, entry in enumerate(settings.cook_assets)
                                      if asset_visible(settings, entry)), 0)
    return report


def asset_visible(settings, entry):
    if entry.dependency and not settings.cook_show_dependencies:
        return False
    search = settings.cook_search.strip().casefold()
    if search and search not in (entry.asset_path + ' ' + entry.asset_type + ' ' + TYPE_LABELS.get(entry.asset_type, '')).casefold():
        return False
    category = settings.cook_type
    if category == 'ALL':
        return True
    if category == 'Blueprint':
        return entry.asset_type in ('Blueprint', 'AnimBlueprint', 'WidgetBlueprint')
    if category == 'OTHER':
        return entry.asset_type not in ('SkeletalMesh', 'Texture2D', 'MaterialInstanceConstant', 'Blueprint', 'AnimBlueprint', 'WidgetBlueprint')
    return entry.asset_type == category


def visible_assets(settings):
    return [entry for entry in settings.cook_assets if asset_visible(settings, entry)]


def asset_dialog_width(context):
    scale = max(0.1, float(context.preferences.system.ui_scale))
    window_width = context.window.width if context.window else 1150 * scale
    return max(280, min(1150, int(window_width / scale) - 60))


def selection_request(settings):
    report = verified_cook(settings)
    choices = {entry['asset_path']: entry for entry in report['assets']}
    selected = [entry.asset_path for entry in settings.cook_assets if entry.selected]
    if not selected:
        raise BridgeError('请至少勾选一个可打包资产。')
    if len(set(selected)) != len(selected) or any(path not in choices or not choices[path]['packable'] for path in selected):
        raise BridgeError('选择中包含不可打包或不属于本次烘焙的资产。')
    destination = settings.cook_export_directory.strip().strip('"').strip("'")
    if not destination or (destination.startswith('//') and not bpy.data.filepath):
        raise BridgeError('请选择烘焙资产导出目录。')
    export = Path(bpy.path.abspath(destination)).expanduser()
    if not export.is_absolute():
        raise BridgeError('烘焙资产导出目录需要绝对路径。')
    return {'schema_version': 1, 'cook_report': str(Path(settings.cook_report).resolve()),
            'cook_report_sha256': settings.cook_report_sha256, 'selected_assets': selected,
            'export_directory': str(export.resolve())}


def worker_python():
    """Packaging needs ordinary Python and the packager, not an installed UE."""
    candidates = [Path(sys.prefix) / 'bin/python.exe', Path(sys.prefix) / 'bin/python3',
                  Path(bpy.app.binary_path).parent / ('%d.%d' % bpy.app.version[:2]) / 'python/bin/python.exe']
    for path in candidates:
        if path.is_file():
            return path
    raise BridgeError('未找到 Blender 自带 Python，无法启动打包进程。')


def cook_excluded_assets(settings):
    result = {settings.skeleton_path, settings.physics_path}
    # The discovered catalog identifies original references. A manually entered
    # part material can be a new custom MI and must remain selectable.
    result.update(entry.asset_path for entry in settings.material_catalog)
    return sorted(path for path in result if path and path.startswith('/Game/'))
