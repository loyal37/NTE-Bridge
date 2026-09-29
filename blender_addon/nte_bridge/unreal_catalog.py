"""Read actual on-disk UE asset classes and dependencies without importing assets."""
import hashlib
import json
import os
from pathlib import Path

from .core import BridgeError, write_json


def run(request_path, report_path):
    import unreal
    request_path = Path(request_path).resolve()
    result = {'schema_version': 1, 'success': False, 'errors': [], 'assets': []}
    try:
        request = json.loads(request_path.read_text(encoding='utf-8-sig'))
        actual = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
        expected = Path(request['project_file']).resolve()
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            raise BridgeError('资产扫描连接了不同的 UE 工程。')
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        registry.search_all_assets(True)
        folder = request['character_folder']
        rows = registry.get_assets_by_path(folder, recursive=True, include_only_on_disk_assets=True)
        pending = {str(asset.package_name) for asset in rows}
        if not pending:
            raise BridgeError('所选角色文件夹没有已保存的 UE 资产：' + folder)
        options = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True, include_hard_package_references=True,
            include_searchable_names=False, include_soft_management_references=False,
            include_hard_management_references=False)
        visited, assets = set(), []
        while pending:
            package = pending.pop()
            if package in visited or package.startswith('/Script/'):
                continue
            visited.add(package)
            records = registry.get_assets_by_package_name(package, include_only_on_disk_assets=True)
            classes = sorted({str(record.asset_class_path.asset_name) for record in records})
            dependencies = sorted({str(item) for item in registry.get_dependencies(package, options)})
            assets.append({'asset_path': package, 'asset_type': classes[0] if len(classes) == 1 else 'Unknown',
                           'classes': classes, 'dependencies': dependencies,
                           'dependency': not (package == folder or package.startswith(folder + '/'))})
            pending.update(item for item in dependencies if item not in visited and not item.startswith('/Script/'))
        result.update(success=True, project_file=str(actual), character_folder=folder,
                      request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
                      assets=sorted(assets, key=lambda item: item['asset_path'].casefold()))
    except Exception as error:
        result['errors'].append(str(error))
    write_json(report_path, result)
    return result
