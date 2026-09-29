"""Shared project cooking with UE's iterative sandbox and verified asset selections."""
import json
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
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
_CACHE_MARKER = '.nte_bridge_cook_cache.json'
_WORK_MARKER = '.nte_bridge_cook_work.json'
_RESERVATION = '.cook_reservation.json'
_STATE = '.cook_state.json'


def _cache_identity(project_file):
    project = Path(project_file).resolve()
    return hashlib.sha256(os.path.normcase(str(project)).encode('utf-8')).hexdigest()


def cook_cache_directory(cache_root, project_file, character_folder=None):
    """One sandbox per project; character scope never changes its placement."""
    root = Path(cache_root)
    project = Path(project_file)
    if not root.is_absolute() or not project.is_absolute():
        raise BridgeError('缓存与 UE 工程需要绝对路径。')
    if character_folder is not None:
        package_path(character_folder, '角色烘焙文件夹')
    identity = _cache_identity(project)
    name = re.sub('[^A-Za-z0-9_-]', '_', project.stem)[:12]
    return root.resolve() / 'Cooks' / (name + '_' + identity[:16])


@contextmanager
def _workspace_lock(directory):
    """An OS lock is released even if its process dies; the file stays stable."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / '.cook.lock').open('a+b')
    locked = False
    try:
        try:
            # Windows byte locks also prohibit reading the locked byte. fstat
            # avoids touching it when another worker already owns this lock.
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
            raise BridgeError('同一工程正在烘焙或打包，请等待当前操作完成。') from error
        yield root
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _cache_metadata(root, request=None):
    marker = root / _CACHE_MARKER
    if not marker.is_file():
        if request is None or any((root / name).exists() for name in ('cooked', 'selections', 'temp', 'pending', 'current', 'previous')):
            raise BridgeError('缓存缺少桥接所有权标记，不会清理已有目录。')
        metadata = {'kind': 'NTEBridgeCookCache', 'schema_version': 2, 'directory': str(root),
                    'cache_id': _cache_identity(request['project_file']),
                    'project_file': request['project_file'], 'layout': 'project'}
        write_json(marker, metadata)
    else:
        metadata = json.loads(marker.read_text(encoding='utf-8'))
    if metadata.get('kind') != 'NTEBridgeCookCache' or metadata.get('schema_version') not in {1, 2} \
            or os.path.normcase(metadata.get('directory', '')) != os.path.normcase(str(root)) or not metadata.get('cache_id'):
        raise BridgeError('烘焙缓存所有权标记不匹配，不会改动其中的文件。')
    if request is not None and (metadata['schema_version'] != 2
            or metadata['cache_id'] != _cache_identity(request['project_file'])):
        raise BridgeError('这个缓存目录不是当前工程的共用缓存，请重新点击烘焙。')
    return metadata


def _work_path(root, name, metadata, must_exist=False):
    """Check the direct work directory, including read-only legacy reports."""
    allowed = {'current', 'pending', 'previous'} if metadata['schema_version'] == 1 else {'cooked', 'temp', 'selections'}
    if name not in allowed:
        raise BridgeError('无效的桥接工作目录。')
    path = root / name
    resolved = path.resolve()
    if resolved != path or resolved.parent != root or resolved == root:
        raise BridgeError('工作目录链接越过了角色缓存边界，不会清理：' + str(path))
    if path.exists():
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
        if not path.is_dir() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise BridgeError('工作目录不是普通的桥接缓存目录：' + str(path))
        marker = path / _WORK_MARKER
        owned = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {}
        if owned.get('kind') != 'NTEBridgeCookWork' or owned.get('cache_id') != metadata['cache_id']:
            raise BridgeError('工作目录没有匹配的桥接所有权标记，不会清理：' + str(path))
    elif must_exist:
        raise BridgeError('桥接工作目录不存在：' + str(path))
    return path


def _managed_directory(root, name, metadata):
    path = _work_path(root, name, metadata)
    if not path.exists():
        path.mkdir()
        write_json(path / _WORK_MARKER, {'kind': 'NTEBridgeCookWork', 'cache_id': metadata['cache_id']})
    return path


def selection_directory(report_path):
    """Called while the caller holds lock_cook_report."""
    path = Path(report_path).resolve()
    report = json.loads(path.read_text(encoding='utf-8-sig'))
    root = Path(report.get('cache_directory', path.parent)).resolve()
    metadata = _cache_metadata(root)
    if metadata['schema_version'] == 1:
        return _work_path(root, 'current', metadata, must_exist=True) / 'selections'
    return _managed_directory(root, 'selections', metadata)


def _remove_work(root, name, metadata):
    # The UE sandbox is never removed or mirrored by the bridge. UE -iterate
    # decides which packages need rewriting or deletion.
    if metadata['schema_version'] != 2 or name not in {'selections', 'temp'}:
        raise BridgeError('只能清理桥接的选择暂存和临时目录，不能清空工程烘焙输出。')
    path = _work_path(root, name, metadata)
    if path.exists():
        # Only this verified direct child is recursive. Never a caller path,
        # source Content directory, unknown folder, symlink or junction.
        # Keep ownership proof until all children are gone: Windows can keep a
        # commandlet log briefly open after its parent process has exited.
        marker = path / _WORK_MARKER
        marker_bytes = marker.read_bytes()
        for child in path.iterdir():
            if child.name == _WORK_MARKER:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=_retry_remove)
            else:
                _retry_io(child.unlink)
        _retry_io(marker.unlink)
        try:
            _retry_io(path.rmdir)
        except OSError:
            if path.is_dir():
                marker.write_bytes(marker_bytes)
            raise


def _retry_io(operation):
    for attempt in range(7):
        try:
            return operation()
        except OSError as error:
            if attempt == 6 or not isinstance(error, PermissionError) and getattr(error, 'winerror', 0) not in {5, 32, 33}:
                raise
            time.sleep(0.15 * (attempt + 1))


def _retry_remove(function, path, exc_info):
    if isinstance(exc_info[1], OSError):
        _retry_io(lambda: function(path))
    else:
        raise exc_info[1]


def _reservation(root):
    path = root / _RESERVATION
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def _reject_reservation(root):
    reservation = _reservation(root)
    if reservation and reservation.get('expires_at', 0) > time.time():
        raise BridgeError('同一工程已有等待启动的烘焙任务，请稍后再试。')
    if reservation:
        (root / _RESERVATION).unlink()


def prepare_cook_request(cache_root, request_data):
    """Reserve the UI-to-worker handoff before replacing the fixed request file."""
    request = validate_cook_request(request_data)
    root = cook_cache_directory(cache_root, request['project_file'], request['character_folder'])
    with _workspace_lock(root):
        _cache_metadata(root, request)
        _reject_reservation(root)
        request_path, report_path = root / 'cook_request.json', root / 'cook_report.json'
        write_json(request_path, request)
        token = uuid.uuid4().hex
        write_json(root / _RESERVATION, {'token': token, 'request_sha256': file_sha256(request_path),
                                       'expires_at': time.time() + 300, 'owner_pid': os.getpid()})
        return {'request_path': str(request_path), 'report_path': str(report_path),
                'request_token': token, 'cache_directory': str(root)}


def cancel_cook_request(request_path, request_token):
    root = Path(request_path).resolve().parent
    if not (root / _CACHE_MARKER).is_file():
        return False
    try:
        with _workspace_lock(root):
            _cache_metadata(root)
            reservation = _reservation(root)
            if reservation and reservation.get('token') == request_token:
                (root / _RESERVATION).unlink()
                return True
    except BridgeError:
        pass  # An already-running worker owns the reservation now; don't cancel it.
    return False


@contextmanager
def lock_cook_report(report_path):
    """Serialize all characters' cooking/packaging against the project sandbox."""
    report_path = Path(report_path).resolve()
    data = json.loads(report_path.read_text(encoding='utf-8-sig')) if report_path.is_file() else {}
    declared = data.get('cache_directory')
    if declared and not Path(declared).is_absolute():
        raise BridgeError('烘焙报告的缓存目录不是绝对路径。')
    root = Path(declared).resolve() if declared else report_path.parent
    if not (root / _CACHE_MARKER).is_file():
        if declared:
            raise BridgeError('烘焙报告对应的工程缓存所有权标记不存在。')
        yield
        return
    with _workspace_lock(root):
        _cache_metadata(root)
        _reject_reservation(root)
        yield


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
    if data.get('cache_directory'):
        cache = Path(data['cache_directory']).resolve()
        metadata = _cache_metadata(cache)
        if metadata['schema_version'] == 1:
            output = _work_path(cache, 'current', metadata, must_exist=True)
            marker = json.loads((output / _WORK_MARKER).read_text(encoding='utf-8'))
        else:
            output = _work_path(cache, 'cooked', metadata, must_exist=True)
            state_path = cache / _STATE
            marker = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
        if marker.get('cook_id') != data['cook_id'] or marker.get('committed') is not True \
                or not root.resolve().is_relative_to(output):
            raise BridgeError('工程已经重新烘焙或上次未完成，这份选择报告已过期。')
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


def _cook_shared(request_path, request, editor, run_root, metadata, result, timeout):
    cook_id = result['cook_id']
    try:
        from .unreal_transport import _project_editor_running, REQUIRED_COMMANDLET_PLUGINS
        if _project_editor_running(request['project_file']):
            raise BridgeError('目标 UE 工程仍在打开；请保存并关闭该工程后再后台烘焙。')
        result['run_dir'] = str(run_root)
        catalog_path, runner = run_root / 'catalog.json', run_root / 'catalog_runner.py'
        module_root = str(Path(__file__).resolve().parent.parent)
        runner.write_text('import sys\nsys.path.insert(0, %r)\nfrom nte_bridge.unreal_catalog import run\nrun(%r, %r)\n' %
                          (module_root, str(request_path), str(catalog_path)), encoding='utf-8')
        result['phase'] = 'catalog'
        environment = dict(os.environ)
        temporary = _managed_directory(run_root, 'temp', metadata)
        environment.update({key: str(temporary) for key in ('TEMP', 'TMP', 'TMPDIR')})
        _run(_unreal_command_line([editor, request['project_file'], '-run=pythonscript',
              '-EnablePlugins=' + ','.join(REQUIRED_COMMANDLET_PLUGINS), '-script=' + runner.as_posix(),
              '-unattended', '-nop4', '-nosplash', '-nullrhi', '-NODEFAULTLOG', '-stdout',
              '-FullStdOutLogOutput']), run_root / 'catalog.log', timeout, env=environment)
        if not catalog_path.is_file():
            raise BridgeError('UE 未生成资产目录报告，请查看 catalog.log。')
        catalog = json.loads(catalog_path.read_text(encoding='utf-8-sig'))
        if catalog.get('success') is not True or catalog.get('request_sha256') != result['manifest_sha256'] \
                or Path(catalog.get('project_file', '')).resolve() != Path(request['project_file']):
            raise BridgeError('UE 资产扫描失败或报告已过期：' + '; '.join(catalog.get('errors', [])))
        if _project_editor_running(request['project_file']):
            raise BridgeError('目标 UE 工程在扫描后被打开，请关闭后重新烘焙。')
        result['phase'] = 'cook'
        output = _managed_directory(run_root, 'cooked', metadata)
        _inside(output, 'Windows')  # Reject an externally redirected platform root before UE writes.
        directory = _inside(Path(request['project_file']).parent / 'Content', request['character_folder'][len('/Game/'):])
        _run(_unreal_command_line([editor, request['project_file'], '-run=Cook', '-TargetPlatform=Windows',
              '-SkipZenStore', '-iterate', '-unattended', '-nop4', '-UTF8Output', '-CookDir=' + str(directory),
              '-OutputDir=' + str(output / '[Platform]'), '-NODEFAULTLOG', '-stdout', '-FullStdOutLogOutput']),
             run_root / 'cook-commandlet.log', timeout, env=environment)
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
    return result


def cook_character(request_path, engine_dir, report_path=None, timeout=1800, request_token=None):
    """Incrementally cook into the project's one shared UE sandbox in place."""
    request_path = Path(request_path).resolve()
    root = request_path.parent
    report_path = Path(report_path or root / 'cook_report.json').resolve()
    with _workspace_lock(root):
        raw = json.loads(request_path.read_text(encoding='utf-8-sig'))
        if report_path in (request_path, Path(raw.get('project_file', '')).resolve()) \
                or any(report_path.is_relative_to(root / name) for name in ('cooked', 'selections', 'temp', 'pending', 'current', 'previous')):
            raise BridgeError('烘焙报告不能覆盖请求、UE 工程或工作目录。')
        request_hash = file_sha256(request_path)
        reservation = _reservation(root)
        if request_token:
            if not reservation or reservation.get('token') != request_token or reservation.get('request_sha256') != request_hash:
                raise BridgeError('烘焙启动预约已失效，请重新点击烘焙。')
            (root / _RESERVATION).unlink()
        else:
            _reject_reservation(root)
        cook_id = str(uuid.uuid4())
        result = {'schema_version': 1, 'success': False, 'phase': 'validation', 'job_id': cook_id,
                  'cook_id': cook_id, 'errors': [], 'assets': [], 'manifest_sha256': request_hash,
                  'cache_directory': str(root)}
        try:
            request = validate_cook_request(raw)
            metadata = _cache_metadata(root, request)
            # Validate before invalidating or touching a usable prior sandbox.
            editor = _editor(engine_dir)
            from .unreal_transport import _project_editor_running
            if _project_editor_running(request['project_file']):
                raise BridgeError('目标 UE 工程仍在打开；请保存并关闭该工程后再后台烘焙。')
            for name in ('cooked', 'temp', 'selections'):
                _work_path(root, name, metadata)
            # UE writes in place. Mark the old generation invalid before the
            # first process, so a partial failure can never be packaged as success.
            write_json(root / _STATE, {'cook_id': cook_id, 'committed': False})
            for name in ('selections', 'temp'):
                _remove_work(root, name, metadata)
            result.update(project_file=request['project_file'], character_folder=request['character_folder'])
            write_json(report_path, result)
            result = _cook_shared(request_path, request, editor, root, metadata, result, timeout)
            result['cache_directory'] = str(root)
            if result.get('success'):
                write_json(report_path, result)
                write_json(root / _STATE, {'cook_id': cook_id, 'committed': True})
                return result
        except Exception as error:
            result.update(success=False, assets=[])
            result.setdefault('errors', []).append(str(error))
        if result.get('phase') in {'catalog', 'cook'}:
            name = 'catalog.log' if result['phase'] == 'catalog' else 'cook-commandlet.log'
            result['diagnostic_log'] = str(root / name)
        write_json(report_path, result)
        return result
