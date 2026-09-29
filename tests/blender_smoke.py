"""Run: blender -b --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_smoke.py"""

import hashlib
import json
from pathlib import Path
import sys
import uuid

import bpy
from io_scene_fbx import parse_fbx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge.blender_export import export_job, graph_dict, profile_manifest
from nte_bridge.core import compile_graph


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def snapshot():
    mesh = bpy.context.scene.nte_bridge.mesh
    rig = bpy.context.scene.nte_bridge.armature
    return {'objects': sorted(o.name for o in bpy.data.objects),
            'meshes': sorted(o.name for o in bpy.data.meshes),
            'armatures': sorted(o.name for o in bpy.data.armatures),
            'materials': sorted(o.name for o in bpy.data.materials),
            'shape_keys': sorted(o.name for o in bpy.data.shape_keys),
            'scenes': sorted(o.name for o in bpy.data.scenes),
            'vertices': [tuple(v.co) for v in mesh.data.vertices],
            'shape': [tuple(v.co) for v in mesh.data.shape_keys.key_blocks['Smile'].data],
            'shape_value': mesh.data.shape_keys.key_blocks['Smile'].value,
            'slots': [s.material.name if s.material else None for s in mesh.material_slots],
            'indices': [p.material_index for p in mesh.data.polygons],
            'smooth_faces': [p.use_smooth for p in mesh.data.polygons],
            'actions': sorted((action.name, tuple(action.frame_range)) for action in bpy.data.actions),
            'animation_actions': [owner.animation_data.action.name for owner in
                                  (rig, mesh, mesh.data.shape_keys)],
            'pose': [(bone.name, [list(row) for row in bone.matrix_basis]) for bone in rig.pose.bones],
            'frame': bpy.context.scene.frame_current,
            'matrix': [list(row) for row in mesh.matrix_world],
            'selection': sorted(o.name for o in bpy.context.selected_objects),
            'active': bpy.context.view_layer.objects.active.name,
            'filepath': bpy.data.filepath}


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    output = ROOT / 'artifacts' / 'blender_smoke'
    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = 'Armature'
    bpy.ops.object.mode_set(mode='EDIT')
    root = rig.data.edit_bones[0]
    root.name = 'root'
    root.head = (0, 0, 0)
    root.tail = (0, 0, 1)
    child = rig.data.edit_bones.new('spine')
    child.head = (0, 0, 1)
    child.tail = (0, 0, 2)
    child.parent = root
    bpy.ops.object.mode_set(mode='OBJECT')
    data = bpy.data.meshes.new('SyntheticMesh')
    data.from_pydata([(-1, 0, 0), (0, 0, 0), (-1, 0, 1), (0, 0, 1), (1, 0, 0), (1, 0, 1)], [],
                     [(0, 1, 3, 2), (1, 4, 5, 3)])
    mesh = bpy.data.objects.new('OriginalMesh', data)
    bpy.context.scene.collection.objects.link(mesh)
    mesh.parent = rig
    modifier = mesh.modifiers.new('Armature', 'ARMATURE')
    modifier.object = rig
    group = mesh.vertex_groups.new(name='root')
    group.add(list(range(6)), 1.0, 'REPLACE')
    material = bpy.data.materials.new('SharedGameMaterial')
    data.materials.append(material)
    data.materials.append(material)
    data.polygons[0].material_index = 0
    data.polygons[1].material_index = 1
    data.polygons[0].use_smooth = False
    data.polygons[1].use_smooth = True
    uv = data.uv_layers.new(name='UV0')
    for loop in uv.data:
        loop.uv = (0.2, 0.3)
    mesh.shape_key_add(name='Basis')
    smile = mesh.shape_key_add(name='Smile')
    smile.data[2].co.z += 0.25
    smile.value = 0.3
    # A static source cannot prove that animation export is disabled. Include object,
    # pose and morph animation, then verify the FBX has no animation objects at all.
    mesh.keyframe_insert(data_path='location', frame=1)
    mesh.location.x = 0.5
    mesh.keyframe_insert(data_path='location', frame=2)
    spine = rig.pose.bones['spine']
    spine.rotation_mode = 'XYZ'
    spine.keyframe_insert(data_path='rotation_euler', frame=1)
    spine.rotation_euler.z = 0.2
    spine.keyframe_insert(data_path='rotation_euler', frame=2)
    smile.keyframe_insert(data_path='value', frame=1)
    smile.value = 0.7
    smile.keyframe_insert(data_path='value', frame=2)
    bpy.context.scene.frame_set(1)
    require(all(owner.animation_data and owner.animation_data.action for owner in
                (rig, mesh, mesh.data.shape_keys)), 'fixture must contain object, pose and morph animation')
    settings = bpy.context.scene.nte_bridge
    settings.mesh = mesh
    settings.armature = rig
    project = ROOT / 'artifacts' / 'ue_smoke' / 'NTEBridgeSmoke.uproject'
    if not project.is_file():
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text('{"FileVersion":3,"EngineAssociation":"5.6"}', encoding='utf-8')
    settings.project_file = str(project)
    settings.mesh_path = '/Game/NTEBridgeTest/SM_Bridge'
    settings.skeleton_path = '/Game/NTEBridgeTest/SK_Bridge'
    settings.create_placeholders = True
    settings.job_root = str(output / 'jobs')
    require(bpy.ops.nte_bridge.refresh_slots() == {'FINISHED'}, 'slot initialization failed')
    for part in settings.parts:
        part.material_path = '/Game/NTEBridgeTest/M_Shared'
    texture_hashes = {}
    for role in ('BASE_COLOR', 'ID_TEX', 'LIGHT_MAP', 'NORMAL'):
        image = bpy.data.images.new('Synthetic_' + role, width=4, height=4, alpha=True)
        image.pixels = [0.5, 0.5, 1.0, 1.0] * 16
        image.file_format = 'PNG'
        image.filepath_raw = str(output / (role + '.png'))
        image.save()
        texture = settings.textures.add()
        texture.texture_id = str(uuid.uuid4())
        texture.file_path = image.filepath_raw
        texture.role = role
        texture.asset_path = '/Game/NTEBridgeTest/T_' + role
        texture_hashes[role] = hashlib.sha256(Path(texture.file_path).read_bytes()).hexdigest()
        bpy.data.images.remove(image)
    ids = [part.part_id for part in settings.parts]
    require(len(set(ids)) == 2, 'same material collapsed slot identities')
    manifest = profile_manifest(settings)
    require(not manifest['features'], 'default graph unexpectedly creates runtime behavior')
    unsupported = mesh.modifiers.new('Do not silently drop mirror', 'MIRROR')
    try:
        profile_manifest(settings)
    except ValueError:
        pass
    else:
        raise AssertionError('unsupported topology modifier was silently dropped')
    mesh.modifiers.remove(unsupported)
    # Unequal state groups are exercised by the core tests; here test Blender socket serialization.
    tree = settings.graph
    cycle = tree.nodes.new('NTEBridgeCycle')
    part_nodes = [n for n in tree.nodes if n.bl_idname == 'NTEBridgePart']
    output_node = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeOutput')
    tree.links.new(part_nodes[0].outputs[0], cycle.inputs[0])
    tree.links.new(part_nodes[1].outputs[0], cycle.inputs[1])
    link = tree.links.new(cycle.outputs[0], output_node.inputs[0])
    features = compile_graph(graph_dict(tree), manifest['parts'])
    require(len(features) == 1 and len(features[0]['states']) == 2, 'cycle graph did not compile')
    tree.links.remove(link)
    source = output / 'synthetic_source.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    require(ids == [part.part_id for part in settings.parts], 'part identities changed after save/load')
    require(settings.graph.graph_id == manifest['graph_id'], 'graph identity changed after save/load')
    before = snapshot()
    path = export_job(bpy.context)
    require(snapshot() == before, 'export modified the source scene or leaked temporary datablocks')
    require(hashlib.sha256(source.read_bytes()).hexdigest() == source_hash, 'export wrote original blend')
    exported = json.loads(path.read_text(encoding='utf-8'))
    require(len(exported['textures']) == 4, 'texture roles were lost')
    for texture in exported['textures']:
        require(hashlib.sha256((path.parent / texture['source_file']).read_bytes()).hexdigest() == texture_hashes[texture['role']], 'texture staging changed bytes')
    require(exported['mesh']['expected']['shape_keys'] == ['Smile'], 'expected morph list incorrect')
    require(exported['mesh']['expected']['bones'] == [{'name':'root','parent':''}, {'name':'spine','parent':'root'}], 'bone contract incorrect')
    fbx_path = path.parent / 'meshes' / 'mesh.fbx'
    fbx, _ = parse_fbx.parse(str(fbx_path))
    fbx_objects = next(element for element in fbx.elems if element.id == b'Objects')
    animations = [element.id.decode('ascii') for element in fbx_objects.elems
                  if element.id in {b'AnimationStack', b'AnimationLayer', b'AnimationCurveNode', b'AnimationCurve'}]
    require(not animations, 'FBX unexpectedly contains animation: ' + repr(animations))
    geometry = [element for element in fbx_objects.elems
                if element.id == b'Geometry' and element.props[-1] == b'Mesh']
    require(len(geometry) == 1, 'FBX should contain one mesh geometry')
    smoothing = [element for element in geometry[0].elems if element.id == b'LayerElementSmoothing']
    require(len(smoothing) == 1, 'FBX lacks explicit face smoothing data')
    smoothing_fields = {element.id: element.props[0] for element in smoothing[0].elems}
    require(smoothing_fields[b'MappingInformationType'] == b'ByPolygon', 'FBX smoothing is not per face')
    require(smoothing_fields[b'ReferenceInformationType'] == b'Direct', 'FBX smoothing uses unexpected indexing')
    require(list(smoothing_fields[b'Smoothing']) == [0, 1], 'FBX lost flat/smooth face flags')
    existing = set(bpy.data.objects)
    actions_before_import = set(bpy.data.actions)
    # Do not hide exported animation or leaf bones with importer options.
    bpy.ops.import_scene.fbx(filepath=str(fbx_path), use_anim=True, ignore_leaf_bones=False)
    imported = [o for o in bpy.data.objects if o not in existing]
    imported_mesh = next(o for o in imported if o.type == 'MESH')
    imported_rig = next(o for o in imported if o.type == 'ARMATURE')
    require(len(imported_mesh.material_slots) == 2, 'FBX lost duplicate-material slots')
    require([slot.material.name for slot in imported_mesh.material_slots] == [p['slot_key'] for p in exported['parts']], 'FBX material keys differ')
    require('Smile' in imported_mesh.data.shape_keys.key_blocks, 'FBX lost morph')
    require(len(imported_mesh.data.uv_layers) == 1, 'FBX lost UV layer')
    require([bone.name for bone in imported_rig.data.bones] == ['root','spine'], 'FBX changed skeleton or added leaf bones')
    require(set(bpy.data.actions) == actions_before_import, 'FBX importer found unexpected animation actions')
    require(all(not owner.animation_data or not owner.animation_data.action for owner in
                (imported_mesh, imported_rig, imported_mesh.data.shape_keys)), 'FBX imported animation bindings')
    result = {'success': True, 'manifest': str(path), 'source_unchanged': True,
              'blender_version': bpy.app.version_string,
              'fbx_settings_evidence': {'source_animation_actions': len(before['animation_actions']),
                                        'animation_objects': animations,
                                        'bone_names': [bone.name for bone in imported_rig.data.bones],
                                        'smoothing_mapping': smoothing_fields[b'MappingInformationType'].decode('ascii'),
                                        'smoothing_values': list(smoothing_fields[b'Smoothing'])},
              'checks': ['register', 'duplicate-material-slots', 'stable-save-reload-ids', 'node-cycle-compile',
                         'source-scene-unchanged', 'source-file-unchanged', 'fbx-slots', 'fbx-morphs', 'fbx-bones', 'fbx-uv',
                         'four-texture-roles', 'texture-copy-hashes', 'reject-topology-modifier',
                         'fbx-no-animation', 'fbx-face-smoothing']}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_BLENDER_SMOKE=' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
