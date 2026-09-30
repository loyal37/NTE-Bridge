"""Actual Blender 4.5.7 -> offline UE 5.6 send of a separated-object blueprint.

Requires tests/unreal_blueprint_fixture.py to have run on the isolated
artifacts/ue_smoke project. Never targets a user's UE project.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
sys.path.insert(0, str(ROOT / 'tests'))
import nte_bridge
from nte_bridge import blender_nodes, blender_ui
from blender_blueprint_smoke import image_file, quad_mesh

OUT = ROOT / 'artifacts' / 'blender_blueprint_send_smoke'
PROJECT = (ROOT / 'artifacts/ue_smoke/NTEBridgeSmoke.uproject').resolve()
CONTENT = PROJECT.parent / 'Content'
CHECKS = []


def file_hash(package):
    path = CONTENT / (package.removeprefix('/Game/') + '.uasset')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''


def send(settings):
    operators = []
    original = blender_ui._WorkerModal._launch

    def capture(operator, context, command, log_path):
        operators.append(operator)
        return original(operator, context, command, log_path)

    with patch.object(blender_ui._WorkerModal, '_launch', capture):
        started = bpy.ops.nte_bridge.send()
    assert started == {'RUNNING_MODAL'}, started
    operator = operators[0]
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        if operator._process.poll() is None:
            time.sleep(0.2)
            continue
        terminal = operator.modal(bpy.context, SimpleNamespace(type='TIMER'))
        if terminal in ({'FINISHED'}, {'CANCELLED'}):
            report = json.loads(Path(settings.last_report).read_text(encoding='utf-8')) if settings.last_report else {}
            return terminal, report
    operator._process.kill()
    raise AssertionError('send timed out')


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
    assert PROJECT.is_file() and (CONTENT / 'BPTest/MI_Mother.uasset').is_file(), 'Run unreal_blueprint_fixture.py first'
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = 'Armature'
    bpy.ops.object.mode_set(mode='EDIT')
    rig.data.edit_bones[0].name = 'root'
    bpy.ops.object.mode_set(mode='OBJECT')
    textures = OUT / 'textures'
    textures.mkdir()
    diffuse = image_file(textures / 'coat_d.png', 0.8)
    for name, value in (('coat_m', 0.4), ('coat_n', 0.5), ('coat_ mask', 0.2)):
        bpy.data.images.remove(image_file(textures / (name + '.png'), value))
    coat_material = bpy.data.materials.new('CoatMaterial')
    coat_material.use_nodes = True
    texture_node = coat_material.node_tree.nodes.new('ShaderNodeTexImage')
    texture_node.image = diffuse
    coat_material.node_tree.links.new(texture_node.outputs['Color'],
                                      coat_material.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
    belt_material = bpy.data.materials.new('BeltMaterial')
    body = quad_mesh('Body', rig, [bpy.data.materials.new('MainA'), bpy.data.materials.new('MainB')], (0, 0, 0), ['Smile'])
    coat = quad_mesh('Coat_1', rig, [coat_material], (0, 0.5, 0))
    belt = quad_mesh('Belt_2', rig, [belt_material, belt_material], (0, -0.5, 0.2), ['Smile', 'BeltOnly'])

    root = '/Game/BPSend/Case_' + uuid.uuid4().hex[:10]
    settings = bpy.context.scene.nte_bridge
    settings.project_file = str(PROJECT)
    settings.mesh_path = root + '/SK_Body'
    settings.skeleton_path = root + '/SK_Body_Skeleton'
    settings.create_placeholders = True
    settings.cache_root = str(OUT)
    settings.engine_dir = ''
    settings.sync_mode = 'commandlet'
    settings.mesh = body
    for part in settings.parts:
        part.material_path = root + '/M_Main'
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    coat.select_set(True)
    belt.select_set(True)
    assert bpy.ops.nte_bridge.blueprint_add_objects(mode='SWITCH') == {'FINISHED'}
    tree = settings.graph
    switch = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeSwitch')
    switch.hotkey, switch.comment, switch.input_slot_count = 'alt 6', '外套', 3
    nodes = {n.target.name: n for n in blender_nodes.object_nodes(tree)}
    new = tree.nodes.new('NTEBridgeMaterial')
    new.mi_name, new.parent_path = 'MI_BPCoat', '/Game/BPTest/MI_Mother'
    tree.links.new(new.outputs[0], nodes['Coat_1'].inputs[0])
    assert bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new.name, action='PARENT') == {'FINISHED'}
    assert bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new.name, action='ADD') == {'FINISHED'}
    new.params[-1].param = 'SkilMask'
    assert bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new.name, action='SUGGEST') == {'FINISHED'}
    existing = tree.nodes.new('NTEBridgeMaterial')
    existing.source, existing.material_path = 'ORIGINAL', '/Game/BPTest/MI_Existing'
    for socket in nodes['Belt_2'].inputs:
        tree.links.new(existing.outputs[0], socket)
    existing_hash = file_hash('/Game/BPTest/MI_Existing')
    mother_hash = file_hash('/Game/BPTest/MI_Mother')

    terminal, report = send(settings)
    assert terminal == {'FINISHED'}, settings.status
    manifest = json.loads(Path(settings.last_manifest).read_text(encoding='utf-8'))
    assert report['success'] and report['slot_map'] == {p['id']: p['source_slot'] for p in manifest['parts']}, report['slot_map']
    assert [p['object'] for p in manifest['parts']] == ['Body', 'Body', 'Coat_1', 'Belt_2', 'Belt_2']
    assert report['morph_targets'] == ['BeltOnly', 'Smile'], report['morph_targets']
    assert [report['slot_names'][p['id']] for p in manifest['parts']] ==         ['M_Main', 'M_Main', 'CoatMaterial', 'BeltMaterial', 'BeltMaterial'], report['slot_names']
    instance_path = root + '/MI_BPCoat'
    instances = report['material_instances']
    assert len(instances) == 1 and instances[0]['asset_path'] == instance_path
    assert instances[0]['parent_path'] == '/Game/BPTest/MI_Mother'
    assert set(instances[0]['textures']) == {'BaseColor', 'LightMap', 'NomralMap', 'SkilMask'}
    assets = {a['asset_path']: a for a in report['assets']}
    assert assets[instance_path]['origin'] == 'mod' and assets[instance_path]['saved']
    assert not assets['/Game/BPTest/MI_Existing']['changed']
    for texture in instances[0]['textures'].values():
        assert assets[texture]['saved'] and (CONTENT / (texture.removeprefix('/Game/') + '.uasset')).is_file(), texture
    assert report['texture_settings'][root + '/coat__mask'] == {'compression': 'BC7', 'srgb': False, 'role': 'MASK'}
    assert report['texture_settings'][root + '/coat_n']['compression'] == 'NORMALMAP'
    assert file_hash('/Game/BPTest/MI_Existing') == existing_hash and file_hash('/Game/BPTest/MI_Mother') == mother_hash
    assert report['features_applied'] is False and len(manifest['features']) == 1
    assert blender_nodes._switch_ht_text(switch, settings) == '2,3+4（初始 0）', blender_nodes._switch_ht_text(switch, settings)
    CHECKS.extend(['separated-objects-joined-into-one-skeletal-mesh', 'slot-map-matches-recorded-order',
                   'custom-slots-use-blender-slot-names',
               'shape-key-union-imported', 'new-instance-created-with-parent-and-textures', 'mask-texture-bc7-linear',
               'existing-instance-and-mother-untouched', 'switch-ht-string-from-actual-report'])
    first_instance_hash = file_hash(instance_path)

    light = next(socket for row, socket in zip(new.params, new.inputs) if row.param == 'LightMap')
    light_texture = light.links[0].from_node
    tree.links.remove(light.links[0])
    terminal, report = send(settings)
    assert terminal == {'FINISHED'}, settings.status
    assert set(report['material_instances'][0]['textures']) == {'BaseColor', 'NomralMap', 'SkilMask'}
    assert file_hash(instance_path) != first_instance_hash
    CHECKS.append('resend-updates-managed-instance-overrides')
    updated_hash = file_hash(instance_path)

    assert bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new.name, action='ADD') == {'FINISHED'}
    new.params[-1].param = 'NotAParam'
    tree.links.new(light_texture.outputs[0], new.inputs[-1])
    terminal, report = send(settings)
    assert terminal == {'CANCELLED'} and any('NotAParam' in e and '没有贴图参数' in e for e in report['errors']), report['errors']
    assert report['mutation_started'] is False and file_hash(instance_path) == updated_hash
    new.params.remove(len(new.params) - 1)
    blender_nodes.sync_material_node(new)
    CHECKS.append('missing-parent-parameter-rejected-before-mutation')

    new.mi_folder, new.mi_name = '/Game/BPTest', 'MI_Existing'
    for socket in nodes['Belt_2'].inputs:
        tree.links.new(new.outputs[0], socket)
    tree.nodes.remove(existing)
    terminal, report = send(settings)
    assert terminal == {'CANCELLED'} and any('不是桥接创建' in e for e in report['errors']), report['errors']
    assert report['mutation_started'] is False and file_hash('/Game/BPTest/MI_Existing') == existing_hash
    CHECKS.append('user-instance-at-target-path-not-overwritten')

    result = {'success': True, 'blender': bpy.app.version_string, 'project': str(PROJECT), 'asset_root': root,
              'instance': instance_path, 'checks': CHECKS}
    (OUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_BLUEPRINT_SEND=' + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
