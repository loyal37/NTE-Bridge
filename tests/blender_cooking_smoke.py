"""Actual Blender 4.5.7 UI/state regression; UE and the packager are not launched."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge import blender_ui, blender_packaging
from nte_bridge.core import BridgeError, write_json
from nte_bridge.cooking import cancel_cook_request, cook_cache_directory, lock_cook_report, snapshot_cooked


def rejected(call, text):
    try:
        result = call()
    except (BridgeError, RuntimeError) as error:
        assert text in str(error), str(error)
    else:
        assert result == {'CANCELLED'}, result


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    settings = bpy.context.scene.nte_bridge
    output = ROOT / 'artifacts/cooking_ui_smoke' / uuid.uuid4().hex[:10]
    output.mkdir(parents=True)
    project = output / 'UE/HT.uproject'
    project.parent.mkdir()
    write_json(project, {'FileVersion': 3, 'EngineAssociation': '5.6'})
    settings.project_file = str(project.parent)  # One .uproject in the selected directory.
    settings.cache_root = str(output / '缓存 Cache')
    settings.source_folder = 'E:/NTE mods/078_Nitsa'
    settings.mesh_path = '/Game/Characters/Player/078_Nitsa/fire_phy/SM_Fire'
    settings.skeleton_path = '/Game/Characters/Player/078_Nitsa/SK_Nitsa'
    settings.physics_path = '/Game/Characters/Player/078_Nitsa/PH_Nitsa'
    part = settings.parts.add()
    part.material_path = '/Game/Characters/Player/078_Nitsa/MI_Original'
    original_material = settings.material_catalog.add()
    original_material.asset_path = part.material_path
    role = '/Game/Characters/Player/078_Nitsa'
    (project.parent / 'Content/Characters/Player/078_Nitsa').mkdir(parents=True)
    (project.parent / 'Content/Characters/Player/OtherRole').mkdir(parents=True)
    checks = []
    assert blender_packaging.default_character_folder(settings) == role
    assert blender_packaging.resolved_project(settings) == project
    settings.source_folder = 'E:/Renamed/OtherLocalFolder'
    assert blender_packaging.default_character_folder(settings) == role
    settings.mesh_path = '/Game/Custom/Model/SM_Test'
    assert blender_packaging.default_character_folder(settings) == '/Game/Custom/Model'
    settings.source_folder = 'E:/NTE mods/078_Nitsa'
    settings.mesh_path = role + '/fire_phy/SM_Fire'
    checks += ['default-whole-role-root-from-source-or-Player-path', 'custom-model-default-parent', 'project-directory-resolves-unique-uproject']

    settings.cook_use_custom = True
    settings.cook_folder = '/Game/Characters/Player/OtherRole'
    assert blender_packaging.character_folder(settings) == settings.cook_folder
    settings.source_folder = 'E:/NTE mods/079_Other'
    assert not settings.cook_use_custom
    settings.source_folder = 'E:/NTE mods/078_Nitsa'
    checks.append('custom-cook-folder-and-role-change-return-to-default')
    settings.engine_dir = str(output / 'No editor launched')
    captured = []

    def capture(operator, context, command, log_path):
        operator._settings = context.scene.nte_bridge
        captured.append((SimpleNamespace(_settings=operator._settings, _report=operator._report), command, Path(log_path)))
        return {'FINISHED'}

    assert not settings.last_manifest and not settings.mesh
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.cook() == {'FINISHED'}
    cook_operator, command, log = captured[-1]
    assert 'cook' in command and '--manifest' not in command and '--request' in command
    request_path = Path(command[command.index('--request') + 1])
    request = json.loads(request_path.read_text(encoding='utf-8'))
    assert request['project_file'] == str(project) and request['character_folder'] == role
    assert set(request['excluded_assets']) == {settings.skeleton_path, settings.physics_path, part.material_path}
    assert request_path.is_relative_to(Path(settings.cache_root) / 'Cooks')
    assert Path(command[0]).is_file() and 'python' in Path(command[0]).name.lower()
    checks.append('independent-cook-without-mesh-or-latest-send-uses-selected-cache-and-project')

    request_before = request_path.read_bytes()
    with patch.object(blender_ui._WorkerModal, '_launch') as second_launch:
        try:
            outcome = bpy.ops.nte_bridge.cook()
            assert outcome == {'CANCELLED'}
        except RuntimeError:
            pass
        second_launch.assert_not_called()
    assert request_path.read_bytes() == request_before
    cancel_cook_request(request_path, command[command.index('--request-token') + 1])
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.cook() == {'FINISHED'}
    cook_operator, command, log = captured[-1]
    assert Path(command[command.index('--request') + 1]) == request_path
    assert request_path.parent == cook_cache_directory(Path(settings.cache_root), project, role)
    cancel_cook_request(request_path, command[command.index('--request-token') + 1])
    checks += ['same-project-role-reuses-request-and-report-directory', 'active-reservation-rejects-second-cook-without-overwriting-request']

    settings.cook_use_custom = True
    settings.cook_folder = '/Game/Characters/Player/OtherRole'
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.cook() == {'FINISHED'}
    _, other_command, _ = captured[-1]
    other_request = Path(other_command[other_command.index('--request') + 1])
    assert other_request.parent != request_path.parent
    cancel_cook_request(other_request, other_command[other_command.index('--request-token') + 1])
    settings.cook_use_custom = False
    checks.append('different-role-cooks-use-separate-stable-directories')

    with patch.object(blender_ui._WorkerModal, '_launch', side_effect=OSError('fixture child launch failed')):
        rejected(lambda: bpy.ops.nte_bridge.cook(), 'fixture child launch failed')
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.cook() == {'FINISHED'}
    cook_operator, command, log = captured[-1]
    assert Path(command[command.index('--request') + 1]) == request_path
    cancel_cook_request(request_path, command[command.index('--request-token') + 1])
    checks.append('child-launch-failure-releases-cook-reservation')

    cooked = request_path.parent / 'cook-fixture/Windows'
    specs = [('SM_Body', 'SkeletalMesh'), ('T_Body', 'Texture2D'), ('T_Hair', 'Texture2D'),
             ('MI_Custom', 'MaterialInstanceConstant'), ('MI_Original', 'MaterialInstanceConstant'),
             ('M_Original', 'Material'), ('SK_Nitsa', 'Skeleton'), ('PH_Nitsa', 'PhysicsAsset')]
    catalog = {'assets': []}
    for name, kind in specs:
        asset = role + '/' + name
        relative = project.stem + '/Content/' + asset.removeprefix('/Game/')
        for suffix in ('.uasset', '.uexp'):
            path = cooked / (relative + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name + suffix).encode())
        catalog['assets'].append({'asset_path': asset, 'asset_type': kind})
    shared_path = cooked / (project.stem + '/Content/Common/T_Shared.uasset')
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_bytes(b'outside-role shared texture')
    catalog['assets'].append({'asset_path': '/Game/Common/T_Shared', 'asset_type': 'Texture2D'})
    report = snapshot_cooked(request, catalog, cooked, str(uuid.uuid4()), hashlib.sha256(request_path.read_bytes()).hexdigest())
    # Put a hidden dependency first to reproduce UE's large Engine dependency inventory.
    report['assets'].sort(key=lambda entry: (not entry['dependency'], entry['asset_path']))
    report_path = Path(command[command.index('--report') + 1])
    write_json(report_path, report)
    blender_ui.NTEBRIDGE_OT_cook._complete(cook_operator, 0)
    assert settings.cook_report == str(report_path)
    assert len(settings.cook_assets) == 9 and not any(entry.selected for entry in settings.cook_assets)
    assert sum(entry.packable for entry in settings.cook_assets) == 5
    assert all(entry.size_text and int(entry.size_bytes) > 0 for entry in settings.cook_assets)
    assert settings.cook_assets[0].dependency
    assert not settings.cook_assets[settings.cook_active_asset].dependency
    checks += ['cook-result-auto-loads-all-unchecked', 'original-material-skeleton-physics-and-original-mi-disabled']

    shared = next(entry for entry in settings.cook_assets if entry.asset_path == '/Game/Common/T_Shared')
    assert shared.dependency and not settings.cook_show_dependencies
    assert len(blender_packaging.visible_assets(settings)) == 8
    bpy.ops.nte_bridge.select_filtered_cooked(action='ALL')
    assert not shared.selected and sum(entry.selected for entry in settings.cook_assets) == 4
    bpy.ops.nte_bridge.select_filtered_cooked(action='NONE')
    settings.cook_show_dependencies = True
    assert len(blender_packaging.visible_assets(settings)) == 9
    bpy.ops.nte_bridge.select_filtered_cooked(action='ALL')
    assert shared.selected and sum(entry.selected for entry in settings.cook_assets) == 5
    settings.cook_show_dependencies = False
    bpy.ops.nte_bridge.select_filtered_cooked(action='NONE')
    assert shared.selected and sum(entry.selected for entry in settings.cook_assets) == 1
    shared.selected = False
    checks += ['outside-folder-dependencies-hidden-by-default', 'filtered-select-all-none-preserve-hidden-dependency-choice']

    settings.cook_type = 'Texture2D'
    settings.cook_search = 'hair'
    assert bpy.ops.nte_bridge.select_filtered_cooked(action='ALL') == {'FINISHED'}
    assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == [role + '/T_Hair']
    settings.cook_search = 'body'
    bpy.ops.nte_bridge.select_filtered_cooked(action='ALL')
    assert sum(entry.selected for entry in settings.cook_assets) == 2
    bpy.ops.nte_bridge.select_filtered_cooked(action='NONE')
    assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == [role + '/T_Hair']
    flags, order = blender_ui.NTEBRIDGE_UL_cooked_assets.filter_items(
        SimpleNamespace(bitflag_filter_item=1), bpy.context, settings, 'cook_assets')
    assert sum(flags) == 1 and not order
    settings.cook_search = ''
    settings.cook_type = 'OTHER'
    bpy.ops.nte_bridge.select_filtered_cooked(action='ALL')
    assert not any(entry.selected for entry in settings.cook_assets if not entry.packable)
    checks += ['search-and-type-filter-match-visible-list', 'select-all-none-only-affect-visible-packable-assets']

    assert settings.cook_export_directory == 'D:/Neverness to Everness Mod Loader/cook/packager/xg/HT/Content/Characters'
    settings.cook_export_directory = str(output / 'packager/xg/HT/Content/Characters')
    settings.package_output = str(output / 'Mod outputs')
    settings.packager_source = str(output / 'External packager')
    settings.mod_name = 'SelectionTest_P'
    settings.engine_dir = ''
    with patch.object(blender_ui._WorkerModal, '_launch', capture), \
            patch.object(blender_ui, 'detect_engine_dir', side_effect=AssertionError('Packing must not resolve an engine')):
        assert bpy.ops.nte_bridge.select_cooked_assets() == {'FINISHED'}
    pack_operator, command, log = captured[-1]
    assert 'package-selection' in command and '--engine-dir' not in command and '--manifest' not in command
    selection_path = Path(command[command.index('--selection') + 1])
    selection = json.loads(selection_path.read_text(encoding='utf-8'))
    assert selection['selected_assets'] == [role + '/T_Hair']
    assert selection['export_directory'] == str(Path(settings.cook_export_directory))
    assert selection['cook_report_sha256'] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert selection_path.is_relative_to(report_path.parent / 'current/selections')
    assert '--output-dir' in command and command[command.index('--output-dir') + 1] == settings.package_output
    checks += ['export-directory-and-explicit-selection-serialized', 'pack-selection-does-not-resolve-or-launch-ue']

    selection_directory = report_path.parent / 'current/selections'
    before_selections = {path.name for path in selection_directory.iterdir()}
    with lock_cook_report(report_path), patch.object(blender_ui._WorkerModal, '_launch') as busy_launch:
        rejected(lambda: bpy.ops.nte_bridge.select_cooked_assets(), '正在烘焙或打包')
        busy_launch.assert_not_called()
    assert {path.name for path in selection_directory.iterdir()} == before_selections
    checks.append('active-role-lock-rejects-selection-before-writing-or-launching')

    # A blocked row cannot be smuggled through programmatic property assignment.
    blocked = next(entry for entry in settings.cook_assets if not entry.packable)
    blocked.selected = True
    rejected(lambda: blender_packaging.selection_request(settings), '不可打包')
    blocked.selected = False
    checks.append('selection-validates-packability-before-launch')

    # Validate the dialog API used by the actual Blender version, without opening
    # an interactive window in background tests.
    assert 'confirm_text' in bpy.context.window_manager.invoke_props_dialog.__doc__
    assert 'title' in bpy.context.window_manager.invoke_props_dialog.__doc__
    assert hasattr(bpy.types, 'NTEBRIDGE_UL_cooked_assets')
    for pixels, scale in ((1600, 1.5), (1600, 2.0), (2560, 1.0)):
        context = SimpleNamespace(window=SimpleNamespace(width=pixels),
                                  preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=scale)))
        width = blender_packaging.asset_dialog_width(context)
        assert width <= 1150 and width * scale <= pixels - 40 * scale
    checks.append('native-asset-list-and-dialog-api-registered-on-4-5-7')
    checks += ['first-visible-role-asset-active-not-hidden-dependency', 'dialog-width-fits-window-at-high-ui-scale']

    previous_report_hash = settings.cook_report_sha256
    settings.engine_dir = str(output / 'No editor launched')
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.cook() == {'FINISHED'}
    recook_operator, recook_command, _ = captured[-1]
    assert not settings.cook_report and not settings.cook_assets
    assert Path(recook_command[recook_command.index('--request') + 1]) == request_path
    assert Path(recook_command[recook_command.index('--report') + 1]) == report_path
    cancel_cook_request(request_path, recook_command[recook_command.index('--request-token') + 1])
    report['cook_id'] = report['job_id'] = str(uuid.uuid4())
    write_json(report_path, report)
    blender_ui.NTEBRIDGE_OT_cook._complete(recook_operator, 0)
    assert settings.cook_report_sha256 != previous_report_hash
    assert not any(entry.selected for entry in settings.cook_assets)
    assert selection['cook_report_sha256'] != settings.cook_report_sha256
    checks.append('recook-clears-selection-and-reloads-new-generation-at-same-report-path')

    for change in ('project', 'folder', 'cache', 'role'):
        blender_packaging.load_cooked_assets(settings, report_path)
        settings.cook_assets[0].selected = True
        if change == 'project':
            settings.project_file = str(project)
        elif change == 'folder':
            settings.cook_use_custom = True
        elif change == 'cache':
            settings.cache_root = str(output / 'Another Cache')
        else:
            settings.source_folder = 'E:/NTE mods/080_New'
        assert not settings.cook_report and not settings.cook_assets
        settings.project_file = str(project.parent)
        settings.cache_root = str(output / '缓存 Cache')
        settings.source_folder = 'E:/NTE mods/078_Nitsa'
        settings.cook_use_custom = False
    checks.append('project-folder-cache-and-role-changes-clear-stale-selection')

    blender_packaging.load_cooked_assets(settings, report_path)
    report_path.write_bytes(report_path.read_bytes() + b'\n')
    rejected(lambda: blender_packaging.verified_cook(settings), '报告已改变')
    assert not settings.cook_report and not settings.cook_assets
    checks.append('changed-cook-report-rejected-and-selection-cleared')
    result = {'success': True, 'blender': bpy.app.version_string, 'checks': checks,
              'request': str(request_path), 'selection': str(selection_path)}
    write_json(output / 'result.json', result)
    write_json(ROOT / 'artifacts/cooking_ui_smoke/latest_result.json', result)
    print('NTE_BRIDGE_COOKING_UI_SMOKE=' + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
