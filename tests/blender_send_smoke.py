"""Exercise the actual export -> offline UE sync operator in Blender 4.5.7.

Requires the isolated fixture created by tests/blender_smoke.py and
tools/create_test_project.py. This never targets a user's UE project.
"""
import json
from pathlib import Path
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge import blender_ui


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
    nte_bridge.register()
    source = ROOT / 'artifacts/blender_smoke/synthetic_source.blend'
    project = (ROOT / 'artifacts/ue_smoke/NTEBridgeSmoke.uproject').resolve()
    assert source.is_file() and project.is_file(), 'Create isolated smoke fixtures first'
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    assert Path(settings.project_file).resolve() == project
    output = ROOT / 'artifacts/blender_send_smoke'
    output.mkdir(parents=True, exist_ok=True)
    old_job_directories = {p.name for p in (output / 'Jobs').glob('*') if p.is_dir()}
    # Model the first-use workflow: JSON discovery into entirely new UE folders.
    asset_root = '/Game/NTEBridgeAuto/Case_' + uuid.uuid4().hex[:10]
    source_folder = output / ('source_' + asset_root.rsplit('/', 1)[-1])
    source_folder.mkdir()
    mesh_path = asset_root + '/Meshes/SM_Auto'
    skeleton_path = asset_root + '/Rig/SK_Auto'
    physics_path = asset_root + '/Physics/PH_Auto'
    material_path = asset_root + '/Materials/MI_Shared'
    records = [{
        'Type': 'SkeletalMesh', 'Name': 'SM_Auto', 'Package': mesh_path,
        'Properties': {
            'Skeleton': {'ObjectName': "Skeleton'SK_Auto'", 'ObjectPath': skeleton_path + '.0'},
            'PhysicsAsset': {'ObjectName': "PhysicsAsset'PH_Auto'", 'ObjectPath': physics_path + '.0'}},
        'SkeletalMaterials': [{'MaterialSlotName': slot.material.name,
            'Material': {'ObjectName': "MaterialInstanceConstant'MI_Shared'", 'ObjectPath': material_path + '.0'}}
            for slot in settings.mesh.material_slots]},
        {'Type': 'MaterialInstanceConstant', 'Name': 'MI_Shared', 'Package': material_path, 'Properties': {}}]
    (source_folder / 'SM_Auto.json').write_text(json.dumps(records), encoding='utf-8')
    settings.bound_mesh = None
    assert bpy.ops.nte_bridge.refresh_slots() == {'FINISHED'}
    settings.create_placeholders = False
    settings.source_folder = str(source_folder)
    assert bpy.ops.nte_bridge.scan_character() == {'FINISHED'}
    assert settings.create_placeholders, 'Folder-based workflow did not enable missing references automatically'
    assert (settings.mesh_path, settings.skeleton_path, settings.physics_path) == (mesh_path, skeleton_path, physics_path)
    for texture in settings.textures:
        texture.asset_path = asset_root + '/Textures/T_' + texture.role
    settings.cache_root = str(output)
    settings.engine_dir = ''  # Resolve this exact .uproject association.
    settings.sync_mode = 'commandlet'
    settings.last_manifest = str(output / 'old-job-must-not-be-used.json')
    settings.last_report = 'old-report-must-not-be-used.json'
    # The current mesh is exported, rather than an earlier task's FBX.
    settings.mesh.data.shape_keys.key_blocks['Smile'].data[2].co.z += 0.05
    operators, phases = [], []
    original_launch = blender_ui._WorkerModal._launch

    def capture(operator, context, command, log_path):
        operators.append(operator)
        return original_launch(operator, context, command, log_path)

    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.send() == {'RUNNING_MODAL'}
    operator = operators[0]
    assert settings.busy and not settings.last_manifest and not settings.last_report
    assert not blender_ui.NTEBRIDGE_OT_send.poll(bpy.context), 'Concurrent send still enabled'
    deadline = time.monotonic() + 240
    terminal = None
    while time.monotonic() < deadline:
        if operator._phase not in phases:
            phases.append(operator._phase)
        if operator._process.poll() is None:
            time.sleep(0.1)
            continue
        terminal = operator.modal(bpy.context, SimpleNamespace(type='TIMER'))
        if terminal in ({'FINISHED'}, {'CANCELLED'}):
            break
        assert settings.busy, 'Send became idle between export and sync'
    else:
        operator._process.kill()
        raise AssertionError('One-button send timed out')
    assert terminal == {'FINISHED'}, settings.status
    assert not settings.busy and phases == ['export', 'sync']
    manifest_path = Path(settings.last_manifest)
    assert manifest_path.is_relative_to(output / 'Jobs')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    report = json.loads(Path(settings.last_report).read_text(encoding='utf-8'))
    assert report['success'] and report['job_id'] == manifest['job_id']
    assert Path(report['project_file']).resolve() == project
    assert len(report['slot_map']) == 2 and report['morph_targets'] == ['Smile']
    assets = {entry['asset_path']: entry for entry in report['assets']}
    for path in [mesh_path, skeleton_path, physics_path, material_path] + [t.asset_path for t in settings.textures]:
        assert path in assets and assets[path]['saved'], path
        assert (project.parent / 'Content' / (path.removeprefix('/Game/') + '.uasset')).is_file(), path
    assert assets[physics_path]['asset_type'] == 'PhysicsAsset'
    assert all(assets[path]['origin'] == 'game_placeholder' for path in [skeleton_path, physics_path, material_path])
    assert {item['asset_path'] for item in manifest['export_assets']}.isdisjoint({skeleton_path, physics_path, material_path})
    assert 'Blender 4.5.7' in (manifest_path.parent / 'export.log').read_text(encoding='utf-8')

    previous_job = manifest['job_id']
    previous_invocation = report['invocation_id']
    stale = manifest_path.parent / 'stale-unused-texture.png'
    stale.write_bytes(b'previous task')
    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        assert bpy.ops.nte_bridge.send() == {'RUNNING_MODAL'}
    operator = operators[-1]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if operator._process.poll() is None:
            time.sleep(0.1)
            continue
        terminal = operator.modal(bpy.context, SimpleNamespace(type='TIMER'))
        if terminal in ({'FINISHED'}, {'CANCELLED'}):
            break
    else:
        operator._process.kill()
        raise AssertionError('Repeated send timed out')
    assert terminal == {'FINISHED'}, settings.status
    assert Path(settings.last_manifest) == manifest_path and not stale.exists()
    assert json.loads(manifest_path.read_text(encoding='utf-8'))['job_id'] != previous_job
    repeated = json.loads(Path(settings.last_report).read_text(encoding='utf-8'))
    assert repeated['success'] and repeated['invocation_id'] != previous_invocation
    assert len(list(manifest_path.parent.glob('ue_run*.py'))) == 1
    assert len(list(manifest_path.parent.glob('ue_invocation*.json'))) == 1
    assert {p.name for p in manifest_path.parent.parent.iterdir() if p.is_dir()} == old_job_directories | {'current'}

    # A subsequent invalid current edit must not leave the successful task selected.
    settings.mesh.modifiers.new('Unsupported current edit', 'MIRROR')
    try:
        outcome = bpy.ops.nte_bridge.send()
        assert outcome == {'CANCELLED'}
    except RuntimeError:
        pass  # Blender raises for the operator's ERROR report.
    assert not settings.last_manifest and not settings.last_report and not settings.busy
    result = {'success': True, 'blender': bpy.app.version_string, 'phases': phases,
              'manifest': str(manifest_path), 'project': str(project),
              'checks': ['fresh-export-then-sync', 'exact-project', 'auto-engine-discovery',
                         'busy-through-both-phases', 'reject-concurrent-send',
                         'slots-and-morphs-imported', 'same-version-fbx-worker',
                         'failed-current-export-invalidates-previous-job',
                         'source-folder-enables-reference-placeholders',
                         'new-ue-folders-and-exact-asset-paths',
                         'repeat-send-replaces-job-and-invocation-in-place',
                         'skeleton-material-physics-created-outside-pack-list']}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_SEND_SMOKE=' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
