"""Run by Blender with --disable-autoexec, against a bridge-owned copy only."""

import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nte_bridge.progress import report_progress


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
