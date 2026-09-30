"""Export copies in an isolated Blender process; never save the user's blend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import bpy
from mathutils import Matrix

from .core import BridgeError, compile_blueprint, package_path, validate_manifest, write_json
from .blender_cache import ensure_cache
from .blender_nodes import (IMAGE_SUFFIXES, auto_material_path, ensure_graph, graph_dict, object_nodes,
                            texture_asset_name)
from .blender_textures import discover_material_previews, stage_preview_images
from .workspace import directory_lock, owned_directory, check_background_use


def new_id():
    return str(uuid.uuid4())


def _check_object(obj, rig, reference):
    if obj is None or obj.type != 'MESH':
        raise BridgeError("物体节点需要选择网格物体。")
    if obj.find_armature() != rig:
        raise BridgeError("物体“%s”没有绑定到主网格的骨架“%s”。" % (obj.name, rig.name))
    unsupported = [modifier.name for modifier in obj.modifiers
                   if modifier.type != 'ARMATURE' and (modifier.show_viewport or modifier.show_render)]
    if unsupported:
        raise BridgeError("物体“%s”有可能改变形态键的修改器，请先在工作副本中应用或关闭：%s" % (obj.name, '、'.join(unsupported)))
    if reference is not None:
        uvs = [layer.name for layer in obj.data.uv_layers]
        expected = [layer.name for layer in reference.data.uv_layers]
        if uvs != expected:
            raise BridgeError("物体“%s”的 UV 层 %s 与主网格 %s 不一致；合并时 Blender 按名称匹配，会产生错位的 UV。" % (obj.name, uvs, expected))
        colors = sorted(layer.name for layer in obj.data.color_attributes)
        expected_colors = sorted(layer.name for layer in reference.data.color_attributes)
        if colors != expected_colors:
            raise BridgeError("物体“%s”的颜色属性 %s 与主网格 %s 不一致；合并后缺失的颜色层会被填成默认值。" % (obj.name, colors, expected_colors))


class _Materials:
    """Resolve each part's material and collect new instances plus their textures."""

    def __init__(self, settings, plan):
        self.settings, self.plan = settings, plan
        self.nodes = {node.node_id: node for node in settings.graph.nodes if node.bl_idname == 'NTEBridgeMaterial'}
        self.specs, self.paths, self.textures, self.staging, self.targets = {}, {}, {}, [], {}

    def resolve(self, part_id, blender_material, default, label):
        mid = self.plan['part_materials'].get(part_id)
        if mid:
            return self._node(self.nodes[mid])
        if default:
            return default
        automatic = auto_material_path(self.settings, blender_material)
        if automatic:
            return automatic
        raise BridgeError("材质槽“%s”没有材质：请连接材质球节点，或在“材质与部件”中映射。" % label)

    def _node(self, node):
        path = node.resolved_path()
        label = '材质球“%s”' % node.name
        if not path:
            raise BridgeError(label + "尚未填写完整的 UE 路径或名称。")
        package_path(path, label + '路径')
        if node.source == 'ORIGINAL' or node.node_id in self.specs:
            return path
        owner = self.paths.get(path.casefold())
        if owner and owner != node.node_id:
            raise BridgeError(label + "与另一个材质球使用了同一个路径：" + path)
        self.paths[path.casefold()] = node.node_id
        if node.source == 'EXISTING':
            self.specs[node.node_id] = {"id": node.node_id, "kind": "existing", "asset_path": path}
            return path
        parent = package_path(node.parent_path.strip(), label + '母材质')
        folder = path.rsplit('/', 1)[0]
        parameters = {}
        for row in node.textures:
            param = row.param.strip()
            if not row.file_path.strip():
                continue
            if not param:
                raise BridgeError(label + "有贴图文件未填写参数名。")
            if param in parameters:
                raise BridgeError(label + "的参数重复：" + param)
            source = Path(bpy.path.abspath(row.file_path)).resolve()
            if not source.is_file():
                raise BridgeError(label + "的贴图文件不存在：" + row.file_path)
            if source.suffix.lower() not in IMAGE_SUFFIXES:
                raise BridgeError(label + "的贴图格式不受支持：" + source.name)
            key = os.path.normcase(str(source))
            record = self.textures.get(key)
            if record is None:
                asset = folder + '/' + texture_asset_name(source)
                other = self.targets.get(asset.casefold())
                if other and other != key:
                    raise BridgeError("两个不同的贴图文件会导入到同一个 UE 路径：" + asset)
                self.targets[asset.casefold()] = key
                texture_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'nte-material-texture:' + key))
                record = {"id": texture_id, "source_file": "textures/" + uuid.UUID(texture_id).hex + source.suffix.lower(),
                          "asset_path": asset, "role": row.role, "origin": "mod"}
                self.textures[key] = record
                self.staging.append((source, record["source_file"]))
            elif record["role"] != row.role:
                raise BridgeError("同一张贴图被设置成不同用途：" + source.name)
            parameters[param] = record["asset_path"]
        self.specs[node.node_id] = {"id": node.node_id, "kind": "new", "asset_path": path,
                                    "parent_path": parent, "textures": parameters}
        return path


def profile_manifest(settings, job_id=None, preview_staging=None, export_plan=None):
    # Also guard direct/scripted exports, not only operators in the sidebar.
    from .blender_ui import _ensure_source_current
    _ensure_source_current(settings)
    mesh = settings.mesh
    if mesh is None or mesh.type != 'MESH':
        raise BridgeError("请选择一个角色网格。")
    rig = mesh.find_armature()
    settings.armature = rig
    if rig is None or rig.type != 'ARMATURE':
        raise BridgeError("角色网格尚未绑定骨架；请先设置网格的 Armature 绑定。")
    if settings.bound_mesh != mesh:
        raise BridgeError("角色网格已更换；请刷新部件槽以建立该网格的独立身份。")
    _check_object(mesh, rig, None)
    if len(mesh.material_slots) != len(settings.parts) or not settings.parts:
        raise BridgeError("材质槽数量已改变；请刷新部件槽并核对映射。")
    for index, part in enumerate(settings.parts):
        if part.source_slot != index:
            raise BridgeError("部件槽顺序不连续；请刷新部件槽。")
        if mesh.material_slots[index].material != part.source_material:
            raise BridgeError("材质槽 %d 的源材质已改变；请刷新部件槽并核对映射。" % index)
    tree = ensure_graph(settings)
    graph = graph_dict(tree)
    plan = compile_blueprint(graph)
    nodes = {node.node_id: node for node in object_nodes(tree)}
    main = nodes.get(plan['main'])
    if main is None or main.target != mesh:
        raise BridgeError("主网格物体节点与侧栏选择的网格不一致；请刷新角色蓝图。")
    ordered = [main] + sorted((nodes[nid] for nid in plan['objects'] if nid != plan['main']),
                              key=lambda node: node.order_key)
    seen = set()
    for node in ordered:
        if node.target in seen:
            raise BridgeError("物体“%s”被多个物体节点引用。" % node.target.name)
        seen.add(node.target)
        if not node.is_main:
            _check_object(node.target, rig, mesh)
            if [slot.material for slot in node.target.material_slots] != [slot.material for slot in node.slots]:
                raise BridgeError("物体“%s”的材质槽已变化；请刷新角色蓝图。" % node.target.name)
    materials = _Materials(settings, plan)
    parts, objects, previews = [], [], []
    for node in ordered:
        obj = node.target
        if node.is_main:
            entries = [(part.source_slot, part.part_id, part.display_name, part.source_material,
                        part.material_path.strip()) for part in settings.parts]
        else:
            entries = [(slot.slot_index, slot.part_id, obj.name + ' · ' + slot.label, slot.material, '')
                       for slot in node.slots]
        slot_keys = []
        for slot_index, part_id, display, blender_material, default in entries:
            path = materials.resolve(part_id, blender_material, default, display).strip()
            key = "NTE_" + uuid.UUID(part_id).hex
            parts.append({"id": part_id, "slot_key": key, "source_slot": len(parts), "display_name": display,
                          "material_path": path, "object": obj.name})
            slot_keys.append(key)
            if path.casefold() not in materials.paths:
                previews.append(SimpleNamespace(source_material=blender_material, material_path=path,
                                                optional=not node.is_main))
        objects.append((obj, slot_keys))
    features = plan['features']
    shape_names = []
    for obj in [node.target for node in ordered]:
        keys = obj.data.shape_keys
        for block in (keys.key_blocks[1:] if keys else []):
            if block.name not in shape_names:
                shape_names.append(block.name)
    expected = {"bones": [{"name": bone.name, "parent": bone.parent.name if bone.parent else ""}
                          for bone in rig.data.bones],
                "shape_keys": shape_names,
                "uv_layers": len(mesh.data.uv_layers)}
    textures = []
    for entry in settings.textures:
        path = Path(bpy.path.abspath(entry.file_path)).resolve()
        if not entry.file_path.strip() or not path.is_file():
            raise BridgeError("贴图文件不存在: " + entry.file_path)
        textures.append({"id": entry.texture_id,
                         "source_file": "textures/" + uuid.UUID(entry.texture_id).hex + path.suffix.lower(),
                         "asset_path": entry.asset_path.strip(), "role": entry.role, "origin": "mod"})
    manual_count = len(textures)
    textures.extend(materials.textures.values())
    preview_textures, material_previews, staging = discover_material_previews(settings, textures[:manual_count], previews)
    textures.extend(preview_textures)
    if preview_staging is not None:
        preview_staging.extend(staging)
    if export_plan is not None:
        export_plan.update(objects=objects, texture_staging=list(materials.staging),
                           shape_keys=shape_names, uv_layers=expected["uv_layers"])
    new_instances = [spec for spec in materials.specs.values() if spec["kind"] == "new"]
    manifest = {"schema_version": 1, "job_id": job_id or new_id(), "graph_id": graph['id'],
                "character_id": settings.character_id,
                "project_file": str(Path(bpy.path.abspath(settings.project_file)).resolve()) if settings.project_file else "",
                "create_placeholders": settings.create_placeholders,
                "mesh": {"id": settings.mesh_id, "source_file": "meshes/mesh.fbx",
                         "asset_path": settings.mesh_path.strip(),
                         "skeleton_path": settings.skeleton_path.strip(),
                         "physics_asset_path": settings.physics_path.strip(), "expected": expected},
                "parts": parts, "textures": textures, "material_previews": material_previews,
                "materials": list(materials.specs.values()), "features": features,
                "export_assets": [{"asset_path": settings.mesh_path.strip(),
                                   "asset_type": "SkeletalMesh", "origin": "mod"}] + [
                    {"asset_path": t['asset_path'], "asset_type": "Texture2D", "origin": "mod"}
                    for t in textures if t.get('origin', 'mod') == 'mod'] + [
                    {"asset_path": spec['asset_path'], "asset_type": "MaterialInstanceConstant", "origin": "mod"}
                    for spec in new_instances]}
    validate_manifest(manifest)
    return manifest


def _write_export_copy(settings, blend_path, export_plan):
    """Create an isolated scene datablock, then remove every temporary datablock."""
    source_rig = settings.armature
    scene = bpy.data.scenes.new("NTEBridgeExport")
    objects, datas, materials = [], [], []
    rig_data = None
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
        rig.hide_viewport = False
        rig.hide_render = False
        order = []
        for position, (source, slot_keys) in enumerate(export_plan['objects']):
            obj = source.copy()
            data = source.data.copy()
            obj.data = data
            objects.append(obj)
            datas.append(data)
            obj.animation_data_clear()
            scene.collection.objects.link(obj)
            for constraint in list(obj.constraints):
                obj.constraints.remove(constraint)
            for modifier in list(obj.modifiers):
                obj.modifiers.remove(modifier)
            modifier = obj.modifiers.new("NTE Bridge Armature", 'ARMATURE')
            modifier.object = rig
            obj.parent = rig
            obj.matrix_world = source.matrix_world.copy()
            obj.hide_viewport = False
            obj.hide_render = False
            if obj.data.shape_keys:
                obj.data.shape_keys.animation_data_clear()
                for key in obj.data.shape_keys.key_blocks:
                    key.value = 0.0
            for index, key in enumerate(slot_keys):
                material = bpy.data.materials.new(key)
                materials.append(material)
                obj.material_slots[index].link = 'DATA'
                obj.data.materials[index] = material
            order.extend(slot_keys)
            # Worker identifies roles without relying on temporary object names.
            obj["nte_bridge_export_role"] = "mesh" if position == 0 else "part"
            obj["nte_bridge_export_order"] = position
        main = objects[1]
        main["nte_bridge_material_order"] = json.dumps(order)
        main["nte_bridge_shape_keys"] = json.dumps(export_plan['shape_keys'], ensure_ascii=False)
        main["nte_bridge_uv_layers"] = export_plan['uv_layers']
        rig["nte_bridge_export_role"] = "armature"
        bpy.data.libraries.write(str(blend_path), {scene}, path_remap='ABSOLUTE', fake_user=False)
    finally:
        bpy.data.scenes.remove(scene)
        for obj in objects:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        for data in datas:
            if data.name in bpy.data.meshes:
                bpy.data.meshes.remove(data)
        if rig_data and rig_data.name in bpy.data.armatures:
            bpy.data.armatures.remove(rig_data)
        for material in materials:
            if material.name in bpy.data.materials:
                bpy.data.materials.remove(material)


def prepare_job(context, settings=None):
    settings = settings or context.scene.nte_bridge
    preview_staging, export_plan = [], {}
    manifest = profile_manifest(settings, preview_staging=preview_staging, export_plan=export_plan)
    jobs = ensure_cache(settings).resolve()
    lock = directory_lock(jobs, '导出或发送任务正在使用当前缓存，请等待完成。')
    lock.__enter__()
    try:
        job_dir = jobs / 'current'
        check_background_use(job_dir)
        job_dir = owned_directory(job_dir, 'NTEBridgeImport', reset=True)
        return _prepare_job_files(settings, manifest, preview_staging, job_dir, lock, export_plan)
    except Exception:
        lock.__exit__(None, None, None)
        raise


def _prepare_job_files(settings, manifest, preview_staging, job_dir, lock, export_plan):
    (job_dir / 'meshes').mkdir()
    blend_path = job_dir / '_export_copy.blend'
    _write_export_copy(settings, blend_path, export_plan)
    if manifest['textures']:
        (job_dir / 'textures').mkdir()
    for entry, texture in zip(settings.textures, manifest['textures']):
        shutil.copyfile(bpy.path.abspath(entry.file_path), job_dir / texture['source_file'])
    for source, relative in export_plan['texture_staging']:
        shutil.copyfile(source, job_dir / relative)
    stage_preview_images(preview_staging, job_dir)
    write_json(job_dir / 'graph.json', graph_dict(settings.graph))
    write_json(job_dir / 'manifest.pending.json', manifest)
    return {"job_dir": job_dir, "manifest": manifest, "blend_path": blend_path, '_lock': lock,
            "command": [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
                        '--python-exit-code', '1', '--python', str(Path(__file__).with_name('fbx_worker.py')),
                        '--', str(blend_path), str(job_dir / 'meshes' / 'mesh.fbx')]}


def release_job(job):
    lock = job.pop('_lock', None) if job else None
    if lock:
        lock.__exit__(None, None, None)


def finish_job(job, returncode, *, release=True):
    try:
        return _finish_job(job, returncode)
    finally:
        if release or returncode:
            release_job(job)


def _finish_job(job, returncode):
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
    try:
        with (job['job_dir'] / 'export.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(job['command'], stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return finish_job(job, result.returncode)
    finally:
        release_job(job)
