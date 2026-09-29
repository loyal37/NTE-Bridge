"""Export copies in an isolated Blender process; never save the user's blend."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import uuid

import bpy
from mathutils import Matrix

from .core import BridgeError, compile_graph, validate_manifest, write_json


def new_id():
    return str(uuid.uuid4())


def graph_dict(tree):
    if tree is None:
        raise BridgeError("请先创建角色节点图。")
    nodes = []
    types = {"NTEBridgePart": "PART", "NTEBridgeGroup": "GROUP",
             "NTEBridgeCycle": "CYCLE", "NTEBridgeOutput": "OUTPUT"}
    for node in tree.nodes:
        if node.bl_idname == "NodeFrame":
            continue
        kind = types.get(node.bl_idname)
        if kind is None:
            raise BridgeError("不支持的节点: " + node.name)
        item = {"id": node.node_id, "type": kind}
        if kind == "PART":
            item["part_id"] = node.part_id
            item["label"] = node.label or node.name
        elif kind == "GROUP":
            item["label"] = node.label or node.name
        elif kind == "CYCLE":
            item.update(label=node.feature_label, key=node.key,
                        include_hidden=node.include_hidden,
                        initial_state_id=node.initial_state_id,
                        state_ids=[s.state_id for s in node.states])
        nodes.append(item)
    links = [{"from_node": link.from_node.node_id,
              "to_node": link.to_node.node_id,
              "to_socket": link.to_socket.socket_id} for link in tree.links]
    return {"id": tree.graph_id, "nodes": nodes, "links": links}


def profile_manifest(settings, job_id=None):
    mesh, rig = settings.mesh, settings.armature
    if mesh is None or mesh.type != 'MESH':
        raise BridgeError("请选择一个角色网格。")
    if rig is None or rig.type != 'ARMATURE':
        raise BridgeError("请选择角色骨架。")
    if settings.bound_mesh != mesh:
        raise BridgeError("角色网格已更换；请刷新部件槽以建立该网格的独立身份。")
    if mesh.find_armature() != rig:
        raise BridgeError("网格没有绑定到所选骨架；请先修正 Armature 修改器。")
    unsupported = [modifier.name for modifier in mesh.modifiers
                   if modifier.type != 'ARMATURE' and (modifier.show_viewport or modifier.show_render)]
    if unsupported:
        raise BridgeError("首版不应用可能改变形态键的修改器，请先在工作副本中处理：" + '、'.join(unsupported))
    if len(mesh.material_slots) != len(settings.parts) or not settings.parts:
        raise BridgeError("材质槽数量已改变；请刷新部件槽并核对映射。")
    parts = []
    for index, part in enumerate(settings.parts):
        if part.source_slot != index:
            raise BridgeError("部件槽顺序不连续；请刷新部件槽。")
        if mesh.material_slots[index].material != part.source_material:
            raise BridgeError("材质槽 %d 的源材质已改变；请刷新部件槽并核对映射。" % index)
        parts.append({"id": part.part_id, "slot_key": "NTE_" + uuid.UUID(part.part_id).hex,
                      "source_slot": index, "display_name": part.display_name,
                      "material_path": part.material_path.strip()})
    graph = graph_dict(settings.graph)
    features = compile_graph(graph, parts)
    shape_keys = mesh.data.shape_keys
    expected = {"bones": [{"name": bone.name, "parent": bone.parent.name if bone.parent else ""}
                          for bone in rig.data.bones],
                "shape_keys": [key.name for key in shape_keys.key_blocks[1:]] if shape_keys else [],
                "uv_layers": len(mesh.data.uv_layers)}
    textures = []
    for entry in settings.textures:
        path = Path(bpy.path.abspath(entry.file_path)).resolve()
        if not entry.file_path.strip() or not path.is_file():
            raise BridgeError("贴图文件不存在: " + entry.file_path)
        textures.append({"id": entry.texture_id,
                         "source_file": "textures/" + uuid.UUID(entry.texture_id).hex + path.suffix.lower(),
                         "asset_path": entry.asset_path.strip(), "role": entry.role})
    manifest = {"schema_version": 1, "job_id": job_id or new_id(), "graph_id": graph['id'],
                "character_id": settings.character_id,
                "project_file": str(Path(bpy.path.abspath(settings.project_file)).resolve()) if settings.project_file else "",
                "create_placeholders": settings.create_placeholders,
                "mesh": {"id": settings.mesh_id, "source_file": "meshes/mesh.fbx",
                         "asset_path": settings.mesh_path.strip(),
                         "skeleton_path": settings.skeleton_path.strip(),
                         "physics_asset_path": settings.physics_path.strip(), "expected": expected},
                "parts": parts, "textures": textures, "features": features,
                "export_assets": [{"asset_path": settings.mesh_path.strip(),
                                   "asset_type": "SkeletalMesh", "origin": "mod"}] + [
                    {"asset_path": t['asset_path'], "asset_type": "Texture2D", "origin": "mod"}
                    for t in textures]}
    validate_manifest(manifest)
    return manifest


def _write_export_copy(settings, blend_path):
    """Create an isolated scene datablock, then remove every temporary datablock."""
    source_mesh, source_rig = settings.mesh, settings.armature
    scene = bpy.data.scenes.new("NTEBridgeExport")
    objects, materials = [], []
    mesh_data = rig_data = None
    try:
        scene.unit_settings.system = 'METRIC'
        scene.unit_settings.scale_length = bpy.context.scene.unit_settings.scale_length
        rig = source_rig.copy()
        rig_data = source_rig.data.copy()
        rig.data = rig_data
        objects.append(rig)
        rig.animation_data_clear()
        rig.parent = None
        rig.matrix_world = source_rig.matrix_world.copy()
        scene.collection.objects.link(rig)
        for constraint in list(rig.constraints):
            rig.constraints.remove(constraint)
        for bone in rig.pose.bones:
            for constraint in list(bone.constraints):
                bone.constraints.remove(constraint)
            bone.matrix_basis = Matrix.Identity(4)
        rig.data.pose_position = 'REST'
        mesh = source_mesh.copy()
        mesh_data = source_mesh.data.copy()
        mesh.data = mesh_data
        objects.append(mesh)
        mesh.animation_data_clear()
        scene.collection.objects.link(mesh)
        for constraint in list(mesh.constraints):
            mesh.constraints.remove(constraint)
        for modifier in list(mesh.modifiers):
            mesh.modifiers.remove(modifier)
        modifier = mesh.modifiers.new("NTE Bridge Armature", 'ARMATURE')
        modifier.object = rig
        mesh.parent = rig
        mesh.matrix_world = source_mesh.matrix_world.copy()
        mesh.hide_viewport = False
        mesh.hide_render = False
        rig.hide_viewport = False
        rig.hide_render = False
        if mesh.data.shape_keys:
            mesh.data.shape_keys.animation_data_clear()
            for key in mesh.data.shape_keys.key_blocks:
                key.value = 0.0
        for index, part in enumerate(settings.parts):
            material = bpy.data.materials.new("NTE_" + uuid.UUID(part.part_id).hex)
            materials.append(material)
            mesh.material_slots[index].link = 'DATA'
            mesh.data.materials[index] = material
        # Worker identifies roles without relying on temporary object names.
        mesh["nte_bridge_export_role"] = "mesh"
        rig["nte_bridge_export_role"] = "armature"
        bpy.data.libraries.write(str(blend_path), {scene}, path_remap='ABSOLUTE', fake_user=False)
    finally:
        bpy.data.scenes.remove(scene)
        for obj in objects:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        if mesh_data and mesh_data.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh_data)
        if rig_data and rig_data.name in bpy.data.armatures:
            bpy.data.armatures.remove(rig_data)
        for material in materials:
            if material.name in bpy.data.materials:
                bpy.data.materials.remove(material)


def prepare_job(context, settings=None):
    settings = settings or context.scene.nte_bridge
    manifest = profile_manifest(settings)
    if not settings.job_root.strip():
        raise BridgeError("请选择任务输出目录。")
    job_dir = Path(bpy.path.abspath(settings.job_root)).resolve() / manifest['job_id']
    job_dir.mkdir(parents=True, exist_ok=False)
    (job_dir / 'meshes').mkdir()
    blend_path = job_dir / '_export_copy.blend'
    _write_export_copy(settings, blend_path)
    if manifest['textures']:
        (job_dir / 'textures').mkdir()
    for entry, texture in zip(settings.textures, manifest['textures']):
        shutil.copyfile(bpy.path.abspath(entry.file_path), job_dir / texture['source_file'])
    write_json(job_dir / 'graph.json', graph_dict(settings.graph))
    write_json(job_dir / 'manifest.pending.json', manifest)
    return {"job_dir": job_dir, "manifest": manifest, "blend_path": blend_path,
            "command": [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
                        '--python-exit-code', '1', '--python', str(Path(__file__).with_name('fbx_worker.py')),
                        '--', str(blend_path), str(job_dir / 'meshes' / 'mesh.fbx')]}


def finish_job(job, returncode):
    if returncode:
        raise BridgeError("FBX 导出失败，请查看任务中的 export.log。")
    manifest_path = job['job_dir'] / 'manifest.json'
    validate_manifest(job['manifest'], job['job_dir'])
    write_json(manifest_path, job['manifest'])
    # Only our own job-owned temporary source is removed; never user .blend files.
    job['blend_path'].unlink(missing_ok=True)
    (job['job_dir'] / 'manifest.pending.json').unlink(missing_ok=True)
    return manifest_path


def export_job(context, settings=None, timeout=300):
    """Synchronous entry point for tests; UI starts the same worker modally."""
    job = prepare_job(context, settings)
    with (job['job_dir'] / 'export.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(job['command'], stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return finish_job(job, result.returncode)
