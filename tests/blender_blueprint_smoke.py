"""Blueprint nodes in Blender 4.5.7: separated objects, material nodes, switches and upgrade.

Run: blender -b --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_blueprint_smoke.py
"""

import hashlib
import json
from pathlib import Path
import shutil
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge import blender_nodes
from nte_bridge.blender_export import export_job, graph_dict, profile_manifest
from nte_bridge.core import compile_blueprint

OUT = ROOT / 'artifacts' / 'blender_blueprint_smoke'
CHECKS = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check(name):
    CHECKS.append(name)


def must_fail(action, text):
    try:
        action()
    except (ValueError, RuntimeError) as error:
        require(text in str(error), 'unexpected error: %s' % error)
        return
    raise AssertionError('expected failure containing ' + text)


def quad_mesh(name, rig, slots, offset, shape_keys=()):
    data = bpy.data.meshes.new(name + 'Data')
    verts, faces = [], []
    for index in range(len(slots)):
        base = len(verts)
        x = index * 1.5
        verts += [(x, 0, 0), (x + 1, 0, 0), (x + 1, 0, 1), (x, 0, 1)]
        faces.append((base, base + 1, base + 2, base + 3))
    data.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = offset
    obj.parent = rig
    modifier = obj.modifiers.new('Armature', 'ARMATURE')
    modifier.object = rig
    group = obj.vertex_groups.new(name='root')
    group.add(list(range(len(verts))), 1.0, 'REPLACE')
    for index, material in enumerate(slots):
        data.materials.append(material)
        data.polygons[index].material_index = index
    uv = data.uv_layers.new(name='UV0')
    for loop in uv.data:
        loop.uv = (0.25, 0.75)
    color = data.color_attributes.new('COL0', 'BYTE_COLOR', 'CORNER')
    for item in color.data:
        item.color = (0.5, 0.5, 0.5, 1.0)
    if shape_keys:
        obj.shape_key_add(name='Basis')
        for key in shape_keys:
            block = obj.shape_key_add(name=key)
            block.data[0].co.z += 0.1
    return obj


def image_file(path, value):
    image = bpy.data.images.new(path.stem, width=4, height=4, alpha=True)
    image.pixels = [value, value, value, 1.0] * 16
    image.file_format = 'PNG'
    image.filepath_raw = str(path)
    image.save()
    return image


def snapshot(objects):
    return {'objects': sorted(o.name for o in bpy.data.objects),
            'meshes': sorted(m.name for m in bpy.data.meshes),
            'materials': sorted(m.name for m in bpy.data.materials),
            'scenes': sorted(s.name for s in bpy.data.scenes),
            'geometry': {o.name: ([tuple(v.co) for v in o.data.vertices], [p.material_index for p in o.data.polygons],
                                  [s.material.name for s in o.material_slots], tuple(o.location))
                         for o in objects}}


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
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

    main_a, main_b = bpy.data.materials.new('MainA'), bpy.data.materials.new('MainB')
    coat_material = bpy.data.materials.new('CoatMaterial')
    belt_material = bpy.data.materials.new('BeltMaterial')
    textures = OUT / 'textures'
    textures.mkdir()
    diffuse = image_file(textures / 'coat_d.png', 0.8)
    for name, value in (('coat_m', 0.4), ('coat_n', 0.5), ('coat_ mask', 0.2), ('unrelated_d', 0.1)):
        bpy.data.images.remove(image_file(textures / (name + '.png'), value))
    coat_material.use_nodes = True
    nodes = coat_material.node_tree.nodes
    texture_node = nodes.new('ShaderNodeTexImage')
    texture_node.image = diffuse
    coat_material.node_tree.links.new(texture_node.outputs['Color'], nodes['Principled BSDF'].inputs['Base Color'])

    body = quad_mesh('Body', rig, [main_a, main_b], (0, 0, 0), ['Smile'])
    coat = quad_mesh('Coat_1', rig, [coat_material], (0, 0.5, 0))
    belt = quad_mesh('Belt_2', rig, [belt_material, belt_material], (0, -0.5, 0.2), ['Smile', 'BeltOnly'])
    reference = quad_mesh('Reference', rig, [main_a], (3, 0, 0))

    settings = bpy.context.scene.nte_bridge
    project = OUT / 'Project' / 'Blueprint.uproject'
    project.parent.mkdir()
    project.write_text('{"FileVersion":3,"EngineAssociation":"5.6"}', encoding='utf-8')
    settings.project_file = str(project)
    settings.mesh_path = '/Game/BP/SK_Body'
    settings.skeleton_path = '/Game/BP/SK_Body_Skeleton'
    settings.cache_root = str(OUT / 'cache')
    settings.mesh = body
    require(settings.graph is not None, 'choosing the mesh did not create the blueprint')
    for part in settings.parts:
        part.material_path = '/Game/BP/M_Main'
    tree = settings.graph
    main_node = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeObject' and n.is_main)
    output = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeOutput')
    require(main_node.outputs[0].is_linked and main_node.target == body, 'main object not wired to generate node')
    require([s.identifier for s in main_node.inputs] == [p.part_id for p in settings.parts],
            'main material inputs do not follow part identities')
    require(all(s.hide for s in main_node.inputs), 'auto-matched main inputs were not hidden')
    manifest = profile_manifest(settings)
    require(len(manifest['parts']) == 2 and not manifest['features'] and not manifest['materials'],
            'default blueprint changed the original-model workflow')
    check('default-blueprint-keeps-original-workflow')

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    belt.select_set(True)
    coat.select_set(True)
    require(bpy.ops.nte_bridge.blueprint_add_objects(mode='SWITCH') == {'FINISHED'}, 'quick switch failed')
    switch = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeSwitch')
    objects = {n.target.name: n for n in blender_nodes.object_nodes(tree)}
    require(switch.inputs[0].links[0].from_node == objects['Coat_1'] and
            switch.inputs[1].links[0].from_node == objects['Belt_2'], 'quick switch ignored trailing-number order')
    require(switch.custom_var_name == 'swapkey0' and switch.outputs[0].links[0].to_node == output,
            'switch variable or generate link missing')
    check('quick-switch-from-selection')
    switch.hotkey = 'alt 6'
    switch.comment = '外套'
    require(bpy.ops.nte_bridge.switch_option(tree_name=tree.name, node_name=switch.name, delta=1) == {'FINISHED'},
            'adding a switch option failed')
    require([s.name for s in switch.inputs] == ['选项_0', '选项_1', '选项_2'], 'switch option sockets incorrect')
    require(len(output.inputs) >= 3 and not output.inputs[-1].is_linked, 'generate node did not add an input')

    coat_node, belt_node = objects['Coat_1'], objects['Belt_2']
    require(not any(s.hide for s in coat_node.inputs) and [s.name for s in belt_node.inputs] ==
            ['BeltMaterial', 'BeltMaterial'], 'custom object material inputs incorrect')
    new_material = tree.nodes.new('NTEBridgeMaterial')
    require(new_material.source == 'NEW' and [s.name for s in new_material.inputs] ==
            ['BaseColor', 'ID_Tex', 'LightMap', 'NomralMap'], 'material instance did not expose parameter inputs')
    new_material.mi_name = 'MI_BP_Coat'
    require(new_material.resolved_path(settings) == '/Game/BP/MI_BP_Coat', 'instance does not default to the character root')
    new_material.parent_path = '/Game/BP/MI_Mother'
    tree.links.new(new_material.outputs[0], coat_node.inputs[0])
    require(bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new_material.name, action='PARENT') == {'FINISHED'},
            'parent parameter rows failed')

    def linked_textures():
        result = {}
        for row, socket in zip(new_material.params, new_material.inputs):
            if socket.is_linked:
                result[row.param] = socket.links[0].from_node
        return result

    base = linked_textures().get('BaseColor')
    require(base is not None and Path(base.file_path).name == 'coat_d.png',
            'BaseColor texture node was not created from the linked Blender diffuse')
    require(bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new_material.name, action='ADD') == {'FINISHED'},
            'adding a parameter failed')
    new_material.params[-1].param = 'SkilMask'
    require(new_material.inputs[-1].name == 'SkilMask' and new_material.params[-1].role == 'MASK',
            'renamed parameter did not update its input and role')
    require(bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=new_material.name, action='SUGGEST') == {'FINISHED'},
            'suffix suggestion failed')
    linked = {name: Path(node.file_path).name for name, node in linked_textures().items()}
    require(linked == {'BaseColor': 'coat_d.png', 'LightMap': 'coat_m.png', 'NomralMap': 'coat_n.png',
                       'SkilMask': 'coat_ mask.png'}, 'suffix suggestion linked wrong textures: %r' % linked)
    textures_nodes = [n for n in tree.nodes if n.bl_idname == 'NTEBridgeTexture']
    require(len(textures_nodes) == 4, 'texture nodes were duplicated')
    require([row.role for row in new_material.params] == ['BASE_COLOR', 'ID_TEX', 'LIGHT_MAP', 'NORMAL', 'MASK'],
            'texture roles not derived from parameters')
    require(linked_textures()['BaseColor'].asset_path(settings) == '/Game/BP/coat_d', 'texture does not default to character root')
    check('material-instance-inputs-texture-nodes-and-suffix-suggestion')

    original = tree.nodes.new('NTEBridgeMaterial')
    original.source = 'ORIGINAL'
    require(len(original.inputs) == 0, 'original material exposed parameter inputs')
    original.material_path = '/Game/BP/MI_Existing'
    for socket in belt_node.inputs:
        tree.links.new(original.outputs[0], socket)
    conflict = tree.links.new(base.outputs[0], new_material.inputs[1])
    must_fail(lambda: profile_manifest(settings), '不同用途')
    tree.links.remove(conflict)
    check('original-material-has-no-inputs-and-role-conflict-rejected')
    reroute = tree.nodes.new('NodeReroute')
    coat_link = switch.inputs[0].links[0]
    tree.links.remove(coat_link)
    tree.links.new(coat_node.outputs[0], reroute.inputs[0])
    tree.links.new(reroute.outputs[0], switch.inputs[0])

    manifest = profile_manifest(settings)
    part_objects = [part['object'] for part in manifest['parts']]
    require(part_objects == ['Body', 'Body', 'Coat_1', 'Belt_2', 'Belt_2'], 'export slot order incorrect: %r' % part_objects)
    require([part['source_slot'] for part in manifest['parts']] == list(range(5)), 'joined slots not contiguous')
    require([part.get('ue_slot_name') for part in manifest['parts']] ==
            [None, None, 'CoatMaterial', 'BeltMaterial', 'BeltMaterial'], 'custom UE slot names should be Blender slot names')
    require(len(manifest['materials']) == 1, 'unexpected material specs: %r' % manifest['materials'])
    spec = manifest['materials'][0]
    require(spec['kind'] == 'new' and spec['asset_path'] == '/Game/BP/MI_BP_Coat' and
            spec['parent_path'] == '/Game/BP/MI_Mother' and
            set(spec['textures']) == {'BaseColor', 'LightMap', 'NomralMap', 'SkilMask'},
            'new material spec incorrect: %r' % spec)
    require(spec['textures']['SkilMask'] == '/Game/BP/coat__mask', 'mask texture name incorrect')
    require([part['material_path'] for part in manifest['parts'][3:]] == ['/Game/BP/MI_Existing'] * 2,
            'referenced instance not assigned to belt slots')
    exported = {asset['asset_path']: asset['asset_type'] for asset in manifest['export_assets']}
    require(exported.get('/Game/BP/MI_BP_Coat') == 'MaterialInstanceConstant' and
            '/Game/BP/MI_Existing' not in exported, 'material export list incorrect')
    roles = {t['asset_path'].rsplit('/', 1)[-1]: t['role'] for t in manifest['textures']}
    require(roles == {'coat_d': 'BASE_COLOR', 'coat_m': 'LIGHT_MAP', 'coat_n': 'NORMAL', 'coat__mask': 'MASK'},
            'material textures incorrect: %r' % roles)
    require(manifest['mesh']['expected']['shape_keys'] == ['Smile', 'BeltOnly'], 'shape key union incorrect')
    feature = manifest['features'][0]
    coat_part, belt_parts = coat_node.slots[0].part_id, [slot.part_id for slot in belt_node.slots]
    require([state['parts'] for state in feature['states']] == [[coat_part], belt_parts, []] and
            feature['key'] == 'alt 6' and feature['label'] == '外套', 'switch feature incorrect')
    require('Reference' not in part_objects, 'unconnected object was exported')
    check('multi-object-manifest-materials-and-empty-option')

    before = snapshot([body, coat, belt, reference])
    path = export_job(bpy.context)
    require(snapshot([body, coat, belt, reference]) == before, 'export changed the source scene or leaked datablocks')
    job = json.loads(path.read_text(encoding='utf-8'))
    for texture in job['textures']:
        require((path.parent / texture['source_file']).is_file(), 'material texture not staged: ' + texture['asset_path'])
    existing_objects = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(path.parent / 'meshes' / 'mesh.fbx'))
    imported = [o for o in bpy.data.objects if o not in existing_objects]
    meshes = [o for o in imported if o.type == 'MESH']
    require(len(meshes) == 1, 'FBX should contain one joined mesh')
    joined = meshes[0]
    require([slot.material.name for slot in joined.material_slots] == [p['slot_key'] for p in job['parts']],
            'FBX material order differs from manifest')
    require(len(joined.data.vertices) == 20 and len(joined.data.polygons) == 5, 'joined geometry count incorrect')
    require(sorted(k.name for k in joined.data.shape_keys.key_blocks[1:]) == ['BeltOnly', 'Smile'], 'FBX shape keys incorrect')
    require(len(joined.data.uv_layers) == 1, 'joined FBX UV count changed')
    coat_index = [p['slot_key'] for p in job['parts']].index('NTE_' + coat_part.replace('-', ''))
    coat_face = next(face for face in joined.data.polygons if face.material_index == coat_index)
    require(abs(sum((joined.matrix_world @ joined.data.vertices[i].co).y for i in coat_face.vertices) / 4 - 0.5) < 1e-3,
            'joined object transform was not preserved')
    for obj in imported:
        bpy.data.objects.remove(obj, do_unlink=True)
    check('worker-joins-objects-in-recorded-slot-order')

    settings.last_report = str(OUT / 'fake_report.json')
    slots = {part['id']: part['source_slot'] for part in job['parts']}
    Path(settings.last_report).write_text(json.dumps({'success': True, 'slot_map': slots}), encoding='utf-8')
    require(blender_nodes._switch_ht_text(switch, settings) == '2,3+4（初始 0）',
            'HT string incorrect: ' + blender_nodes._switch_ht_text(switch, settings))
    check('switch-shows-ht-material-ids')

    ids = {n.name: n.node_id for n in tree.nodes if hasattr(n, 'node_id')}
    parts = [slot.part_id for node in blender_nodes.object_nodes(tree) for slot in node.slots]
    blend = OUT / 'blueprint.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    settings = bpy.context.scene.nte_bridge
    tree = settings.graph
    require({n.name: n.node_id for n in tree.nodes if hasattr(n, 'node_id')} == ids, 'node identities changed on reload')
    require([slot.part_id for node in blender_nodes.object_nodes(tree) for slot in node.slots] == parts,
            'part identities changed on reload')
    require(compile_blueprint(graph_dict(tree))['features'][0]['states'][1]['parts'] == belt_parts, 'reloaded blueprint differs')
    check('save-reload-keeps-node-and-part-ids')

    coat = bpy.data.objects['Coat_1']
    coat_node = next(n for n in blender_nodes.object_nodes(tree) if n.target == coat)
    extra = bpy.data.materials.new('CoatLining')
    coat.data.materials.append(extra)
    bpy.context.view_layer.update()
    require([s.name for s in coat_node.inputs] == ['CoatMaterial', 'CoatLining'] and
            coat_node.slots[0].part_id == coat_part and coat_node.inputs[0].is_linked,
            'slot edit did not sync inputs while keeping identity')
    coat.data.materials.pop()
    bpy.context.view_layer.update()
    require(len(coat_node.inputs) == 1 and coat_node.inputs[0].is_linked, 'removing a slot broke the linked input')
    check('material-inputs-follow-slot-edits')

    belt = bpy.data.objects['Belt_2']
    belt.data.uv_layers[0].name = 'UVMap'
    must_fail(lambda: profile_manifest(settings), 'UV 层')
    belt.data.uv_layers[0].name = 'UV0'
    modifier = belt.modifiers.new('Shrinkwrap', 'SHRINKWRAP')
    must_fail(lambda: profile_manifest(settings), '修改器')
    belt.modifiers.remove(modifier)
    belt.parent = None
    next(m for m in belt.modifiers if m.type == 'ARMATURE').object = None
    must_fail(lambda: profile_manifest(settings), '没有绑定')
    belt.parent = settings.armature
    next(m for m in belt.modifiers if m.type == 'ARMATURE').object = settings.armature
    material = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeMaterial' and n.source == 'NEW')
    material.parent_path = ''
    must_fail(lambda: profile_manifest(settings), '母材质')
    material.parent_path = '/Game/BP/MI_Mother'
    require(len(profile_manifest(settings)['parts']) == 5, 'restored blueprint no longer validates')
    check('join-preflight-rejects-uv-modifier-rig-and-parent-errors')

    # Upgrade a v0.3 graph: linked legacy part + cycle with hidden state + legacy group/output sockets.
    legacy = bpy.data.node_groups.new('Legacy', 'NTEBridgeTree')
    legacy.graph_id = 'legacy-graph'
    part_nodes = []
    for part in settings.parts:
        node = legacy.nodes.new('NTEBridgePart')
        node.part_id = part.part_id
        node.outputs.new('NTEBridgeSocket', '部件')
        part_nodes.append(node)
    group = legacy.nodes.new('NTEBridgeGroup')
    output_old = legacy.nodes.new('NTEBridgeOutput')
    for node in (group, output_old):
        for socket in list(node.inputs) + list(node.outputs):
            (node.inputs if not socket.is_output else node.outputs).remove(socket)
        node.inputs.new('NTEBridgeSocket', '部件 1')
    group.outputs.new('NTEBridgeSocket', '组合')
    cycle = legacy.nodes.new('NTEBridgeCycle')
    for index in range(2):
        state = cycle.states.add()
        state.state_id, state.label = 'state-%d' % index, '状态 %d' % index
        cycle.inputs.new('NTEBridgeSocket', state.label)
    cycle.outputs.new('NTEBridgeSocket', '功能')
    cycle.include_hidden, cycle.initial_state_id, cycle.key = True, '$hidden', 'K'
    legacy.links.new(part_nodes[1].outputs[0], group.inputs[0])
    legacy.links.new(group.outputs[0], cycle.inputs[1])
    legacy.links.new(cycle.outputs[0], output_old.inputs[0])
    old_tree = settings.graph
    settings.graph = legacy
    require(bpy.ops.nte_bridge.sync_blueprint() == {'FINISHED'}, 'legacy upgrade failed')
    kinds = sorted(n.bl_idname for n in legacy.nodes)
    require('NTEBridgePart' not in kinds and 'NTEBridgeCycle' not in kinds, 'legacy nodes remained: %r' % kinds)
    upgraded = next(n for n in legacy.nodes if n.bl_idname == 'NTEBridgeSwitch')
    plan = compile_blueprint(graph_dict(legacy))
    states = plan['features'][0]['states']
    require(upgraded.input_slot_count == 3 and upgraded.initial_option == 2 and upgraded.hotkey == 'K', 'cycle settings lost')
    require([s['parts'] for s in states] == [[], [settings.parts[1].part_id], []], 'legacy links not preserved: %r' % states)
    require(plan['objects'][plan['main']] == [p.part_id for p in settings.parts], 'upgraded main object incomplete')
    settings.graph = old_tree
    check('legacy-v0.3-graph-upgrades-with-links')

    result = {'success': True, 'blender': bpy.app.version_string, 'manifest': str(path), 'checks': CHECKS}
    (OUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_BLUEPRINT_SMOKE=' + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
