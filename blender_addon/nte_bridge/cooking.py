"""Independent character-folder cooking and immutable, selectable cooked snapshots."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid

from .core import BridgeError, package_path, write_json
from .packaging import SIDECARS, _inside, _run, _unreal_command_line, file_sha256

PACKABLE_CLASSES = {
    'SkeletalMesh', 'StaticMesh', 'Texture2D', 'TextureCube', 'Texture2DArray', 'VolumeTexture',
    'MaterialInstanceConstant', 'Blueprint', 'AnimBlueprint', 'WidgetBlueprint',
    'AnimSequence', 'AnimMontage', 'BlendSpace', 'BlendSpace1D', 'AimOffsetBlendSpace',
    'AimOffsetBlendSpace1D', 'PoseAsset', 'DataAsset', 'DataTable', 'CurveFloat', 'CurveVector',
    'CurveLinearColor', 'CurveLinearColorAtlas',
}
FORBIDDEN_CLASSES = {'Material', 'Skeleton', 'PhysicsAsset'}


def validate_cook_request(data):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise BridgeError('不支持的烘焙请求格式。')
    value = data.get('project_file', '')
    project = Path(value)
    if not value or not project.is_absolute() or project.suffix.lower() != '.uproject' or not project.is_file():
        raise BridgeError('请选择存在的 UE .uproject 工程文件（绝对路径）。')
    folder = package_path(data.get('character_folder'), '角色烘焙文件夹')
    content = project.resolve().parent / 'Content'
    directory = _inside(content, folder[len('/Game/'):])
    if not directory.is_dir():
        raise BridgeError('UE 工程中没有这个角色文件夹：' + folder)
    excluded = data.get('excluded_assets', [])
    if not isinstance(excluded, list):
        raise BridgeError('原始引用排除清单必须为数组。')
    for asset in excluded:
        package_path(asset, '原始引用资源')
    return dict(data, project_file=str(project.resolve()), character_folder=folder,
                excluded_assets=sorted(set(excluded)))


def _packable(path, asset_type, excluded):
    if not path.startswith('/Game/'):
        return False, '引擎/插件依赖，仅供查看'
    if asset_type in FORBIDDEN_CLASSES:
        return False, '原材质、骨架和物理资产只作引用，不打包'
    if path.casefold() in excluded:
        return False, '角色资料中的原始引用资源，不打包'
    if asset_type not in PACKABLE_CLASSES:
        return False, '未支持或仅编辑器使用的资产类型：' + asset_type
    return True, ''


def _inventory(root):
    root = Path(root).resolve()
    files = []
    if not root.is_dir():
        raise BridgeError('烘焙目录不存在：' + str(root))
    for path in sorted(root.rglob('*')):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            actual = _inside(root, relative)
            files.append({'path': relative, 'bytes': actual.stat().st_size, 'sha256': file_sha256(actual)})
    return files


def snapshot_cooked(request, catalog, cooked_root, cook_id, request_hash):
    """Inventory every cooked file, including non-package auxiliary output."""
    project = Path(request['project_file'])
    root = Path(cooked_root).resolve()
    files = _inventory(root)
    by_path = {item['path'].casefold(): item for item in files}
    types = {item['asset_path'].casefold(): item for item in catalog['assets']}
    excluded = {path.casefold() for path in request.get('excluded_assets', [])}
    assigned, assets = set(), []
    prefixes = [(project.stem + '/Content/', '/Game/'), ('Engine/Content/', '/Engine/')]
    for item in files:
        relative = item['path']
        suffix = PurePosixPath(relative).suffix.lower()
        if suffix not in {'.uasset', '.umap'}:
            continue
        prefix = next(((physical, virtual) for physical, virtual in prefixes
                       if relative.casefold().startswith(physical.casefold())), None)
        if prefix is None:
            continue  # Kept explicitly in unassigned_files, never silently packed.
        physical, virtual = prefix
        path = virtual + relative[len(physical):-len(suffix)]
        metadata = types.get(path.casefold(), {})
        asset_type = metadata.get('asset_type', 'Unknown')
        allowed, reason = _packable(path, asset_type, excluded)
        base = relative[:-len(suffix)]
        extensions = (suffix,) + tuple(ext for ext in SIDECARS if ext != '.uasset')
        package_files = [by_path[(base + ext).casefold()] for ext in extensions
                         if (base + ext).casefold() in by_path]
        if item['bytes'] == 0 or suffix != '.uasset':
            allowed, reason = False, '主资产为空或不是支持的 .uasset 包'
        assigned.update(entry['path'].casefold() for entry in package_files)
        folder = request['character_folder']
        assets.append({'asset_path': path, 'asset_type': asset_type, 'files': package_files,
                       'bytes': sum(entry['bytes'] for entry in package_files),
                       'file_count': len(package_files), 'packable': allowed, 'reason': reason,
                       'default_selected': False,
                       'dependency': not (path == folder or path.startswith(folder + '/'))})
    if not assets:
        raise BridgeError('没有找到可浏览的烘焙资产。')
    return {'schema_version': 1, 'success': True, 'phase': 'complete', 'errors': [], 'warnings': [],
            'completed_utc': datetime.now(timezone.utc).isoformat(),
            'job_id': cook_id, 'cook_id': cook_id, 'manifest_sha256': request_hash,
            'project_file': str(project), 'character_folder': request['character_folder'],
            'excluded_assets': request.get('excluded_assets', []), 'cooked_root': str(root),
            'assets': sorted(assets, key=lambda asset: asset['asset_path'].casefold()), 'files': files,
            'unassigned_files': [item for item in files if item['path'].casefold() not in assigned]}


def load_cook_report(path, expected_sha256=None, verify_files=False):
    path = Path(path).resolve()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256)
            or file_sha256(path) != expected_sha256.lower()):
        raise BridgeError('烘焙报告已改变，请重新浏览并选择资产。')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 1 or data.get('success') is not True or data.get('errors'):
        raise BridgeError('需要一次成功的独立烘焙报告。')
    if not data.get('cook_id') or data.get('job_id') != data['cook_id']:
        raise BridgeError('烘焙报告身份无效。')
    package_path(data.get('character_folder'), '角色烘焙文件夹')
    if not re.fullmatch('[0-9a-fA-F]{64}', data.get('manifest_sha256', '')):
        raise BridgeError('烘焙请求指纹无效。')
    project = Path(data.get('project_file', ''))
    root = Path(data.get('cooked_root', ''))
    if not project.is_absolute() or project.suffix.lower() != '.uproject' or not root.is_absolute():
        raise BridgeError('烘焙报告必须包含绝对工程和输出目录。')
    files = data.get('files')
    if not isinstance(files, list) or not files:
        raise BridgeError('烘焙文件快照为空。')
    inventory = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise BridgeError('烘焙文件记录无效。')
        _inside(root, item['path'])
        if item['path'].casefold() in inventory or type(item.get('bytes')) is not int or item['bytes'] < 0 \
                or not re.fullmatch('[0-9a-fA-F]{64}', item.get('sha256', '')):
            raise BridgeError('烘焙文件快照有重复路径或无效指纹。')
        inventory[item['path'].casefold()] = item
    seen, assigned = set(), set()
    for asset in data.get('assets', []):
        asset_path = asset.get('asset_path', '')
        if not isinstance(asset_path, str) or asset_path.casefold() in seen:
            raise BridgeError('烘焙资产路径无效或重复。')
        seen.add(asset_path.casefold())
        if asset_path.startswith('/Game/'):
            package_path(asset_path)
            base = project.stem + '/Content/' + asset_path[len('/Game/'):]
        elif asset_path.startswith('/Engine/'):
            base = 'Engine/Content/' + asset_path[len('/Engine/'):]
        else:
            raise BridgeError('不支持的烘焙资产挂载路径：' + asset_path)
        entries = asset.get('files', [])
        if not entries:
            raise BridgeError('烘焙资产没有文件：' + asset_path)
        allowed_paths = {(base + extension).casefold() for extension in SIDECARS + ('.umap',)}
        actual_paths = set()
        for entry in entries:
            key = entry.get('path', '').casefold()
            if key not in allowed_paths or key in actual_paths or inventory.get(key) != entry:
                raise BridgeError('资产旁文件与完整快照不匹配：' + asset_path)
            actual_paths.add(key)
            assigned.add(key)
        expected_paths = allowed_paths.intersection(inventory)
        if actual_paths != expected_paths or not actual_paths.intersection({(base + '.uasset').casefold(), (base + '.umap').casefold()}):
            raise BridgeError('资产文件快照缺少主文件或旁文件：' + asset_path)
        if asset.get('bytes') != sum(entry['bytes'] for entry in entries) or asset.get('file_count') != len(entries):
            raise BridgeError('资产大小与文件数量不匹配：' + asset_path)
    if not seen:
        raise BridgeError('烘焙报告没有资产。')
    remaining = {item.get('path', '').casefold(): item for item in data.get('unassigned_files', [])}
    if remaining != {key: entry for key, entry in inventory.items() if key not in assigned}:
        raise BridgeError('烘焙报告遗漏了辅助文件。')
    if verify_files:
        actual = {item['path'].casefold(): item for item in _inventory(root)}
        if actual != inventory:
            raise BridgeError('烘焙目录中的文件已被增加、删除或修改，请重新烘焙后选择。')
    return data


def _editor(engine_dir):
    root = Path(engine_dir).resolve()
    engine = root if root.name.casefold() == 'engine' else root / 'Engine'
    editor = engine / 'Binaries/Win64/UnrealEditor-Cmd.exe'
    if not editor.is_file():
        raise BridgeError('所选 UE 安装没有 UnrealEditor-Cmd.exe。')
    version = json.loads((engine / 'Build/Build.version').read_text(encoding='utf-8-sig'))
    if (version.get('MajorVersion'), version.get('MinorVersion')) != (5, 6):
        raise BridgeError('当前桥接需要 UE 5.6。')
    return editor


def cook_character(request_path, engine_dir, report_path=None, timeout=1800):
    request_path = Path(request_path).resolve()
    report_path = Path(report_path or request_path.with_name('cook_report.json')).resolve()
    raw = json.loads(request_path.read_text(encoding='utf-8-sig'))
    if report_path in (request_path, Path(raw.get('project_file', '')).resolve()):
        raise BridgeError('烘焙报告不能覆盖请求或 UE 工程。')
    cook_id = str(uuid.uuid4())
    result = {'schema_version': 1, 'success': False, 'phase': 'validation', 'job_id': cook_id,
              'cook_id': cook_id, 'errors': [], 'assets': [], 'manifest_sha256': file_sha256(request_path)}
    write_json(report_path, result)
    try:
        request = validate_cook_request(raw)
        result.update(project_file=request['project_file'], character_folder=request['character_folder'])
        from .unreal_transport import _project_editor_running, REQUIRED_COMMANDLET_PLUGINS
        if _project_editor_running(request['project_file']):
            raise BridgeError('目标 UE 工程仍在打开；请保存并关闭该工程后再后台烘焙。')
        editor = _editor(engine_dir)
        run_root = Path(tempfile.mkdtemp(prefix='cook-', dir=request_path.parent))
        result['run_dir'] = str(run_root)
        catalog_path, runner = run_root / 'catalog.json', run_root / 'catalog_runner.py'
        module_root = str(Path(__file__).resolve().parent.parent)
        runner.write_text('import sys\nsys.path.insert(0, %r)\nfrom nte_bridge.unreal_catalog import run\nrun(%r, %r)\n' %
                          (module_root, str(request_path), str(catalog_path)), encoding='utf-8')
        result['phase'] = 'catalog'
        environment = dict(os.environ)
        temporary = run_root / 'temp'
        temporary.mkdir()
        environment.update({key: str(temporary) for key in ('TEMP', 'TMP', 'TMPDIR')})
        _run(_unreal_command_line([editor, request['project_file'], '-run=pythonscript',
              '-EnablePlugins=' + ','.join(REQUIRED_COMMANDLET_PLUGINS), '-script=' + runner.as_posix(),
              '-unattended', '-nop4', '-nosplash', '-nullrhi']), run_root / 'catalog.log', timeout, env=environment)
        if not catalog_path.is_file():
            raise BridgeError('UE 未生成资产目录报告，请查看 catalog.log。')
        catalog = json.loads(catalog_path.read_text(encoding='utf-8-sig'))
        if catalog.get('success') is not True or catalog.get('request_sha256') != result['manifest_sha256'] \
                or Path(catalog.get('project_file', '')).resolve() != Path(request['project_file']):
            raise BridgeError('UE 资产扫描失败或报告已过期：' + '; '.join(catalog.get('errors', [])))
        if _project_editor_running(request['project_file']):
            raise BridgeError('目标 UE 工程在扫描后被打开，请关闭后重新烘焙。')
        result['phase'] = 'cook'
        output = run_root / 'cooked'
        output.mkdir()
        directory = _inside(Path(request['project_file']).parent / 'Content', request['character_folder'][len('/Game/'):])
        _run(_unreal_command_line([editor, request['project_file'], '-run=Cook', '-TargetPlatform=Windows',
              '-SkipZenStore', '-unattended', '-nop4', '-UTF8Output', '-CookDir=' + str(directory),
              '-OutputDir=' + str(output / '[Platform]'), '-abslog=' + str(run_root / 'cook-engine.log')]),
             run_root / 'cook.log', timeout, env=environment)
        cooked_root = next((candidate for candidate in (output / 'Windows', output)
                            if (candidate / Path(request['project_file']).stem / 'Content').is_dir()), None)
        if cooked_root is None:
            raise BridgeError('烘焙没有生成预期的工程 Content 文件。')
        if file_sha256(request_path) != result['manifest_sha256']:
            raise BridgeError('烘焙过程中请求发生了改变。')
        result = snapshot_cooked(request, catalog, cooked_root, cook_id, result['manifest_sha256'])
        result.update(run_dir=str(run_root), request_file=str(request_path), catalog_report=str(catalog_path))
    except Exception as error:
        result.update(success=False)
        result['errors'].append(str(error))
    write_json(report_path, result)
    return result
