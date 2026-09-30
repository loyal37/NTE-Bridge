"""Run by Blender with --disable-autoexec, against a bridge-owned copy only."""

import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nte_bridge.progress import report_progress


def _join(mesh, parts):
    """Blender merges shape keys by name only when the active mesh already has keys."""
    if any(o.data.shape_keys for o in parts) and not mesh.data.shape_keys:
        mesh.shape_key_add(name='Basis', from_mix=False)
    objects = [mesh] + parts
    for obj in bpy.context.scene.objects:
        obj.select_set(obj in objects)
    bpy.context.view_layer.objects.active = mesh
    with bpy.context.temp_override(active_object=mesh, object=mesh, selected_objects=objects,
                                   selected_editable_objects=objects):
        result = bpy.ops.object.join()
    remaining = [o for o in bpy.context.scene.objects if o.get('nte_bridge_export_role') == 'part']
    if result != {'FINISHED'} or remaining:
        raise RuntimeError("Could not join separated blueprint objects")


def _order_materials(mesh):
    """Restore the recorded UE slot order; join order is not a contract."""
    order = json.loads(mesh['nte_bridge_material_order'])
    current = [slot.material.name if slot.material else '' for slot in mesh.material_slots]
    if sorted(current) != sorted(order) or len(set(order)) != len(order):
        raise RuntimeError("Joined material slots differ from the manifest: %r" % current)
    if current != order:
        remap = [order.index(name) for name in current]
        indices = [0] * len(mesh.data.polygons)
        mesh.data.polygons.foreach_get('material_index', indices)
        mesh.data.polygons.foreach_set('material_index', [remap[index] for index in indices])
        materials = {material.name: material for material in mesh.data.materials}
        for index, name in enumerate(order):
            mesh.data.materials[index] = materials[name]
        mesh.data.update()
    if [slot.material.name for slot in mesh.material_slots] != order:
        raise RuntimeError("Material slot order verification failed")
    expected_keys = json.loads(mesh['nte_bridge_shape_keys'])
    keys = mesh.data.shape_keys
    actual = [block.name for block in keys.key_blocks[1:]] if keys else []
    if sorted(actual) != sorted(expected_keys):
        raise RuntimeError("Joined shape keys differ: expected %r, got %r" % (expected_keys, actual))
    if len(mesh.data.uv_layers) != mesh['nte_bridge_uv_layers']:
        raise RuntimeError("Joined UV layer count changed")


def main():
    source, target = sys.argv[sys.argv.index('--') + 1:]
    report_progress('读取模型导出副本')
    bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
    scenes = [s for s in bpy.data.scenes if s.name.startswith('NTEBridgeExport')]
    if len(scenes) != 1:
        raise RuntimeError("Expected exactly one bridge export scene")
    scene = scenes[0]
    bpy.context.window.scene = scene
    meshes = [o for o in scene.objects if o.get('nte_bridge_export_role') == 'mesh']
    rigs = [o for o in scene.objects if o.get('nte_bridge_export_role') == 'armature']
    if len(meshes) != 1 or len(rigs) != 1:
        raise RuntimeError("Expected one mesh and armature")
    mesh, rig = meshes[0], rigs[0]
    parts = sorted((o for o in scene.objects if o.get('nte_bridge_export_role') == 'part'),
                   key=lambda o: o['nte_bridge_export_order'])
    if parts:
        report_progress('合并 %d 个分离物体' % (len(parts) + 1))
        _join(mesh, parts)
    _order_materials(mesh)
    # Blender's standard UE FBX import behavior recognizes the Armature root.
    rig.name = 'Armature'
    mesh.name = 'NTEBridgeMesh'
    for index, slot in enumerate(mesh.material_slots):
        desired = slot.material.name.split('.')[0]
        slot.material.name = desired
    for obj in scene.objects:
        obj.select_set(False)
    mesh.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    report_progress('导出 FBX 网格、骨架与形态键')
    result = bpy.ops.export_scene.fbx(
        filepath=target, use_selection=True, object_types={'MESH', 'ARMATURE'},
        use_mesh_modifiers=False, add_leaf_bones=False, bake_anim=False,
        use_armature_deform_only=False, armature_nodetype='NULL',
        use_custom_props=False, use_triangles=False, mesh_smooth_type='FACE',
        apply_unit_scale=True, apply_scale_options='FBX_SCALE_NONE',
        axis_forward='-Z', axis_up='Y', path_mode='STRIP', embed_textures=False,
    )
    if result != {'FINISHED'} or not Path(target).is_file():
        raise RuntimeError("FBX export did not produce a file")
    print('NTE_BRIDGE_FBX_EXPORT_OK', flush=True)
    report_progress('FBX 导出完成，准备发送到 UE')


if __name__ == '__main__':
    main()
