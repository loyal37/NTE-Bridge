"""UE 5.6 Python receiver. Imports assets only; never cooks or generates runtime logic."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import traceback

from .core import BridgeError, load_manifest, resolve_source, texture_settings, write_json


def _canonical_file(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path)))).replace("\\", "/")


def _package(asset):
    return asset.get_path_name().split(".", 1)[0]


def _asset(unreal, path, expected_type):
    if not path:
        return None
    found = unreal.load_asset(path)
    if found is not None and not isinstance(found, expected_type):
        raise BridgeError("Asset type conflict at " + path + ": " + found.get_class().get_name())
    return found


def _skeleton_snapshot(skeleton):
    import unreal
    pose = skeleton.get_reference_pose()
    names = [str(name) for name in pose.get_bone_names()]
    # AnimPose parent arrays are protected. Control Rig's public reader imports the
    # Skeleton into a transient hierarchy and never edits the source Skeleton.
    hierarchy = unreal.RigHierarchy()
    keys = hierarchy.get_controller().import_bones_from_asset(skeleton.get_path_name())
    bones = []
    for key in keys:
        parent_name = str(hierarchy.get_first_parent(key).name)
        bones.append({"name": str(key.name), "parent": "" if parent_name == "None" else parent_name})
    if len(keys) != len(names):
        raise BridgeError("Skeleton contains unsupported virtual bones or an incomplete reference hierarchy")
    transforms = []
    for name in names:
        transform = pose.get_ref_bone_pose(name)
        transforms.append(tuple(round(float(getattr(value, axis)), 7)
                                for value, axes in ((transform.translation, "xyz"),
                                                    (transform.rotation, "xyzw"),
                                                    (transform.scale3d, "xyz"))
                                for axis in axes))
    return {"bones": bones, "transforms": transforms}


def _check_bones(actual, expected):
    actual_map = {bone["name"]: bone["parent"] for bone in actual}
    expected_map = {bone["name"]: bone["parent"] for bone in expected}
    if actual_map != expected_map:
        added = sorted(set(expected_map) - set(actual_map))
        missing = sorted(set(actual_map) - set(expected_map))
        different = sorted(name for name in set(actual_map) & set(expected_map)
                           if actual_map[name] != expected_map[name])
        raise BridgeError("Skeleton hierarchy mismatch; new bones=%s, absent bones=%s, changed parents=%s"
                          % (added, missing, different))


def _source_hashes(manifest, job_dir):
    files = [manifest["mesh"]["source_file"]] + [entry["source_file"] for entry in manifest.get("textures", [])]
    return {source: hashlib.sha256(resolve_source(job_dir, source).read_bytes()).hexdigest() for source in files}


def _saved_asset_hashes(manifest):
    content = Path(manifest["project_file"]).resolve().parent / "Content"
    hashes = {}
    for entry in manifest["export_assets"]:
        relative = entry["asset_path"][len("/Game/"):]
        if not (content / (relative + ".uasset")).is_file():
            raise BridgeError("Saved asset file is missing: " + relative + ".uasset")
        for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"):
            filename = relative + extension
            path = content / filename
            if path.is_file():
                hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _mesh_geometry(unreal, mesh, expected_uvs):
    dynamic_mesh = unreal.DynamicMesh()
    result = unreal.GeometryScript_AssetUtils.copy_mesh_from_skeletal_mesh(
        mesh, dynamic_mesh, unreal.GeometryScriptCopyMeshFromAssetOptions(),
        unreal.GeometryScriptMeshReadLOD())
    if isinstance(result, tuple) and result[-1] != unreal.GeometryScriptOutcomePins.SUCCESS:
        raise BridgeError("Could not inspect imported skeletal mesh geometry")
    uv_count = unreal.GeometryScript_MeshQueries.get_num_uv_sets(dynamic_mesh)
    if uv_count != expected_uvs:
        raise BridgeError("Imported UV layer count differs: expected %d, got %d" % (expected_uvs, uv_count))
    bounds = mesh.get_imported_bounds()
    return {"uv_layers": uv_count,
            "dimensions_cm": [float(getattr(bounds.box_extent, axis)) * 2 for axis in "xyz"],
            "bounds_origin_cm": [float(getattr(bounds.origin, axis)) for axis in "xyz"]}


def _task(unreal, source, destination, factory, options=None):
    task = unreal.AssetImportTask()
    values = {"filename": str(source), "destination_path": destination.rsplit("/", 1)[0],
              "destination_name": destination.rsplit("/", 1)[1], "replace_existing": True,
              "replace_existing_settings": True, "automated": True, "save": False,
              "async_": False, "factory": factory}
    for name, value in values.items():
        # Python exposes the C++ Async property as async_.
        if name == "async_":
            task.set_editor_property("async_", value)
        else:
            task.set_editor_property(name, value)
    if options is not None:
        task.set_editor_property("options", options)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    result = list(task.get_objects())
    if not result:
        raise BridgeError("Import returned no assets: " + str(source))
    return result


def _rename_generated(unreal, asset, destination):
    if _package(asset) != destination:
        if unreal.EditorAssetLibrary.does_asset_exist(destination):
            raise BridgeError("Generated placeholder destination is occupied: " + destination)
        if not unreal.EditorAssetLibrary.rename_asset(_package(asset), destination):
            raise BridgeError("Cannot move generated placeholder to " + destination)
    return asset


def _check_editor_dependencies(unreal):
    requirements = (
        ("Editor Scripting Utilities（EditorScriptingUtilities）", ("EditorAssetLibrary",)),
        ("Geometry Script（GeometryScripting）", (
            "GeometryScript_AssetUtils", "GeometryScript_MeshQueries", "DynamicMesh")),
        ("Control Rig（ControlRig）", ("RigHierarchy",)),
    )
    missing = [label for label, names in requirements if any(not hasattr(unreal, name) for name in names)]
    if missing:
        raise BridgeError("UE 未加载桥接所需插件：" + "、".join(missing)
                          + "。请关闭目标工程后用新版桥接的“后台导入”重试；"
                            "若使用已打开的 UE，请在插件设置中启用这些插件并重启编辑器。当前尚未修改资产。")


_PREVIEW_OWNER_TAG = "NTEBridge.PreviewOwner"
_PREVIEW_NODE_TAG = "NTEBridge.PreviewBaseColorNode"
_PREVIEW_OWNER = "NTEBridge.BaseColorPreview.v1"


def _apply_material_preview(unreal, material, texture, newly_created, report):
    """Maintain only our direct BaseColor preview; keep authored shaders intact."""
    path = _package(material)
    result = {"material_path": path, "texture_path": _package(texture), "applied": False}
    report.setdefault("material_previews", []).append(result)
    if not isinstance(material, unreal.Material):
        result["reason"] = "材质实例保留原有父材质与参数"
    else:
        library = unreal.MaterialEditingLibrary
        assets = unreal.EditorAssetLibrary
        count = library.get_num_material_expressions(material)
        owner = assets.get_metadata_tag(material, _PREVIEW_OWNER_TAG)
        node = None
        if owner == _PREVIEW_OWNER:
            node_path = assets.get_metadata_tag(material, _PREVIEW_NODE_TAG)
            if node_path:
                node = unreal.find_object(None, node_path)
            if (node is None or not isinstance(node, unreal.MaterialExpressionTextureSampleParameter2D)
                    or assets.get_metadata_tag(node, _PREVIEW_OWNER_TAG) != _PREVIEW_OWNER
                    or library.get_material_property_input_node(material, unreal.MaterialProperty.MP_BASE_COLOR) != node):
                result["reason"] = "桥接预览连接已被修改，保留当前材质图"
        elif count:
            result["reason"] = "已有材质图，保留用户着色器"
        if "reason" not in result:
            if node is None:
                # v0.2.1 created empty placeholders without metadata. Their empty
                # expression graph is the only legacy asset we may adopt.
                node = library.create_material_expression(
                    material, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
                if node is None:
                    raise BridgeError("无法创建材质预览节点：" + path)
                node.set_editor_property("parameter_name", "NTEBridge_PreviewBaseColor")
                node.set_editor_property("desc", "NTE Bridge · BaseColor preview only")
                assets.set_metadata_tag(node, _PREVIEW_OWNER_TAG, _PREVIEW_OWNER)
                assets.set_metadata_tag(material, _PREVIEW_OWNER_TAG, _PREVIEW_OWNER)
                assets.set_metadata_tag(material, _PREVIEW_NODE_TAG, node.get_path_name())
            node.set_editor_property("texture", texture)
            if not library.connect_material_property(node, "RGB", unreal.MaterialProperty.MP_BASE_COLOR):
                raise BridgeError("无法连接材质预览 Base Color：" + path)
            if newly_created:
                roughness = library.create_material_expression(material, unreal.MaterialExpressionConstant, -200, 240)
                roughness.set_editor_property("r", 0.7)
                roughness.set_editor_property("desc", "NTE Bridge · preview roughness")
                assets.set_metadata_tag(roughness, _PREVIEW_OWNER_TAG, _PREVIEW_OWNER)
                if not library.connect_material_property(roughness, "", unreal.MaterialProperty.MP_ROUGHNESS):
                    raise BridgeError("无法连接材质预览 Roughness：" + path)
            library.recompile_material(material)
            if (library.get_material_property_input_node(material, unreal.MaterialProperty.MP_BASE_COLOR) != node
                    or node.get_editor_property("texture") != texture):
                raise BridgeError("材质预览读取校验失败：" + path)
            result.update(applied=True, node_path=node.get_path_name())
            return True
    report["warnings"].append("跳过预览 " + path + "：" + result["reason"] + "。")
    return False


def _run(unreal, manifest, job_dir, report):
    from .progress import report_progress
    report_progress('UE 已启动，检查角色资产与材质引用')
    current_project = unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())
    report["project_file"] = _canonical_file(current_project)
    if _canonical_file(current_project) != _canonical_file(manifest["project_file"]):
        raise BridgeError("Wrong Unreal project. Requested %s, running %s" %
                          (manifest["project_file"], current_project))
    version = unreal.SystemLibrary.get_engine_version()
    report["engine_version"] = version
    if not version.startswith("5.6."):
        raise BridgeError("NTE Bridge 需要 Unreal Engine 5.6，当前为 " + version)
    _check_editor_dependencies(unreal)
    mesh_spec = manifest["mesh"]
    create = manifest.get("create_placeholders", False)
    skeleton = _asset(unreal, mesh_spec["skeleton_path"], unreal.Skeleton)
    physics_path = mesh_spec.get("physics_asset_path", "")
    physics = _asset(unreal, physics_path, unreal.PhysicsAsset)
    _asset(unreal, mesh_spec["asset_path"], unreal.SkeletalMesh)
    materials = {}
    missing = []
    if skeleton is None:
        missing.append(mesh_spec["skeleton_path"])
    if physics_path and physics is None:
        missing.append(physics_path)
    for part in manifest["parts"]:
        material_path = part["material_path"]
        if material_path not in materials:
            materials[material_path] = _asset(unreal, material_path, unreal.MaterialInterface)
            if materials[material_path] is None:
                missing.append(material_path)
    for texture in manifest.get("textures", []):
        _asset(unreal, texture["asset_path"], unreal.Texture2D)
    if missing and not create:
        raise BridgeError("Missing reference assets. Create placeholders is disabled: " + ", ".join(missing))
    before_skeleton = _skeleton_snapshot(skeleton) if skeleton else None
    if before_skeleton:
        _check_bones(before_skeleton["bones"], mesh_spec["expected"]["bones"])

    touched = {}
    def remember(asset, origin, save=True):
        path = _package(asset)
        save = save or touched.get(path, (None, False))[1]
        touched[path] = (asset, save)
        record = next((item for item in report["assets"] if item["asset_path"] == path), None)
        if record is None:
            record = {"asset_path": path, "asset_type": asset.get_class().get_name(),
                      "origin": origin, "saved": False, "changed": save}
            report["assets"].append(record)
        else:
            record["changed"] = record["changed"] or save
        return asset

    # All validation above precedes mutations. A failed import can still change in-memory assets;
    # the report explicitly lists partial changes instead of claiming transaction rollback.
    created_materials = set()
    for path, material in list(materials.items()):
        if material is None:
            report["mutation_started"] = True
            folder, name = path.rsplit("/", 1)
            material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                name, folder, unreal.Material, unreal.MaterialFactoryNew())
            if material is None:
                raise BridgeError("Could not create material placeholder " + path)
            materials[path] = remember(material, "game_placeholder")
            created_materials.add(path)
        else:
            remember(material, "game_placeholder", save=False)

    options = unreal.FbxImportUI()
    for name, value in {"automated_import_should_detect_type": False,
                        "mesh_type_to_import": unreal.FBXImportType.FBXIT_SKELETAL_MESH,
                        "import_as_skeletal": True, "import_mesh": True,
                        "import_animations": False, "import_materials": False,
                        "import_textures": False, "create_physics_asset": bool(physics_path and physics is None),
                        "skeleton": skeleton, "physics_asset": physics,
                        "override_full_name": True, "reset_to_fbx_on_material_conflict": True}.items():
        options.set_editor_property(name, value)
    skeletal_options = options.get_editor_property("skeletal_mesh_import_data")
    for name, value in {"import_morph_targets": True, "update_skeleton_reference_pose": False,
                        "use_t0_as_ref_pose": False, "import_mesh_lo_ds": False,
                        # FBX's KeepSectionsSeparate replaces original slot names with
                        # MeshName_N. Unique per-part FBX materials already prevent merging.
                        "import_meshes_in_bone_hierarchy": True, "keep_sections_separate": False,
                        "reorder_material_to_fbx_order": True,
                        "import_translation": unreal.Vector(0, 0, 0),
                        "import_rotation": unreal.Rotator(0, 0, 0), "import_uniform_scale": 1.0,
                        "convert_scene": True, "force_front_x_axis": False, "convert_scene_unit": False,
                        "transform_vertex_to_absolute": True, "bake_pivot_in_vertex": False,
                        "vertex_color_import_option": unreal.VertexColorImportOption.REPLACE,
                        "normal_import_method": unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS_AND_TANGENTS}.items():
        skeletal_options.set_editor_property(name, value)
    cvar = "Interchange.FeatureFlags.Import.FBX"
    original = unreal.SystemLibrary.get_console_variable_int_value(cvar)
    try:
        unreal.SystemLibrary.execute_console_command(None, cvar + " 0")
        if unreal.SystemLibrary.get_console_variable_int_value(cvar) != 0:
            raise BridgeError("Could not select the legacy FBX importer")
        report["mutation_started"] = True
        report_progress('UE 正在导入 FBX 网格与形态键')
        imported = _task(unreal, resolve_source(job_dir, mesh_spec["source_file"]),
                         mesh_spec["asset_path"], unreal.FbxFactory(), options)
    finally:
        unreal.SystemLibrary.execute_console_command(None, cvar + " " + str(original))
        if unreal.SystemLibrary.get_console_variable_int_value(cvar) != original:
            raise BridgeError("Failed to restore the previous FBX importer setting")
    mesh = _asset(unreal, mesh_spec["asset_path"], unreal.SkeletalMesh)
    if mesh is None or not any(isinstance(obj, unreal.SkeletalMesh) for obj in imported):
        raise BridgeError("FBX did not produce the requested SkeletalMesh")
    remember(mesh, "mod")
    mesh_skeleton = mesh.get_editor_property("skeleton")
    if skeleton is None:
        skeleton = _rename_generated(unreal, mesh_skeleton, mesh_spec["skeleton_path"])
        remember(skeleton, "game_placeholder")
    elif mesh_skeleton != skeleton:
        raise BridgeError("Importer changed the Skeleton reference")
    after_skeleton = _skeleton_snapshot(skeleton)
    _check_bones(after_skeleton["bones"], mesh_spec["expected"]["bones"])
    if before_skeleton and before_skeleton != after_skeleton:
        raise BridgeError("Importer changed the existing Skeleton reference pose or hierarchy; assets were not saved")
    if before_skeleton:
        remember(skeleton, "game_placeholder", save=False)
    if physics_path and physics is None:
        physics = mesh.get_editor_property("physics_asset")
        if physics is None:
            raise BridgeError("Importer did not generate the requested PhysicsAsset placeholder")
        physics = _rename_generated(unreal, physics, physics_path)
        remember(physics, "game_placeholder")
    elif physics:
        remember(physics, "game_placeholder", save=False)
    mesh.set_editor_property("physics_asset", physics)

    slots = list(mesh.get_editor_property("materials"))
    slot_indices = {}
    for index, slot in enumerate(slots):
        key = str(slot.get_editor_property("imported_material_slot_name"))
        if key in slot_indices:
            raise BridgeError("FBX imported duplicate slot identity: " + key)
        slot_indices[key] = index
    expected_keys = {part["slot_key"] for part in manifest["parts"]}
    if set(slot_indices) != expected_keys:
        raise BridgeError("Imported material slots differ from manifest. Expected %s, got %s" %
                          (sorted(expected_keys), sorted(slot_indices)))
    for part in manifest["parts"]:
        index = slot_indices[part["slot_key"]]
        slots[index].set_editor_property("material_interface", materials[part["material_path"]])
        slots[index].set_editor_property("material_slot_name", materials[part["material_path"]].get_name())
        # Imported identities remain unique even when visible names/materials match.
        report["slot_map"][part["id"]] = index
    mesh.set_editor_property("materials", slots)
    morphs = sorted(str(morph.get_name()) for morph in mesh.get_editor_property("morph_targets"))
    report["morph_targets"] = morphs
    expected_morphs = sorted(mesh_spec["expected"].get("shape_keys", []))
    if morphs != expected_morphs:
        raise BridgeError("Morph targets differ from Blender export. Expected %s, got %s" % (expected_morphs, morphs))
    report["bone_count"] = len(after_skeleton["bones"])
    report["mesh_geometry"] = _mesh_geometry(unreal, mesh, mesh_spec["expected"]["uv_layers"])
    report["slot_signature"] = hashlib.sha256(
        repr(sorted(slot_indices.items())).encode("utf-8")).hexdigest()

    report_progress('检查网格材质槽、骨架、UV 与形态键')
    textures = {}
    entries = manifest.get("textures", [])
    for index, entry in enumerate(entries):
        report_progress('UE 导入贴图：' + entry['asset_path'].rsplit('/', 1)[-1], index, len(entries))
        _task(unreal, resolve_source(job_dir, entry["source_file"]), entry["asset_path"], unreal.TextureFactory())
        texture = _asset(unreal, entry["asset_path"], unreal.Texture2D)
        if texture is None:
            raise BridgeError("Texture was not imported at " + entry["asset_path"])
        settings = texture_settings(entry["role"])
        compression = (unreal.TextureCompressionSettings.TC_BC7 if settings["compression"] == "BC7"
                       else unreal.TextureCompressionSettings.TC_NORMALMAP)
        texture.set_editor_property("compression_settings", compression)
        texture.set_editor_property("srgb", settings["srgb"])
        if (texture.get_editor_property("compression_settings") != compression
                or bool(texture.get_editor_property("srgb")) != settings["srgb"]):
            raise BridgeError("Texture setting readback failed for " + entry["asset_path"])
        remember(texture, entry.get("origin", "mod"))
        textures[entry["asset_path"].casefold()] = texture
        report.setdefault("texture_settings", {})[entry["asset_path"]] = {
            "compression": settings["compression"], "srgb": bool(texture.get_editor_property("srgb")),
            "role": entry["role"]}

    material_paths = {path.casefold(): path for path in materials}
    for preview in manifest.get("material_previews", []):
        path = material_paths[preview["material_path"].casefold()]
        material = materials[path]
        if _apply_material_preview(unreal, material, textures[preview["texture_path"].casefold()],
                                   path in created_materials, report):
            remember(material, "game_placeholder")

    if _source_hashes(manifest, job_dir) != report["source_sha256"]:
        raise BridgeError("Source files changed during import; save and packaging stopped")
    for index, record in enumerate(report["assets"]):
        report_progress('UE 编译并保存资产', index, len(report['assets']))
        asset, needs_save = touched[record["asset_path"]]
        if needs_save:
            if not unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False):
                raise BridgeError("Could not save " + record["asset_path"])
        record["saved"] = True
    report["saved_asset_sha256"] = _saved_asset_hashes(manifest)
    if manifest.get("features"):
        report["warnings"].append("Runtime nodes are pending: asset sync has NOT generated UE toggle or panel logic. Packaging is disabled.")
    report["features_applied"] = not bool(manifest.get("features"))
    report["success"] = True


def run_job(manifest_path, report_path=None, invocation_id=None):
    """Execute in UE's Python interpreter; always replace this invocation's report."""
    source = Path(manifest_path).resolve()
    destination = Path(report_path) if report_path else source.with_name("ue_report.json")
    report = {"schema_version": 1, "job_id": "", "success": False, "project_file": "",
              "manifest_sha256": "", "source_sha256": {}, "assets": [], "slot_map": {}, "morph_targets": [],
              "warnings": [], "errors": [], "features_applied": False, "partial_changes": False,
              "mutation_started": False}
    if invocation_id:
        report['invocation_id'] = invocation_id
    try:
        report["manifest_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = load_manifest(source)
        report["job_id"] = manifest["job_id"]
        report["project_file"] = manifest["project_file"]
        report["source_sha256"] = _source_hashes(manifest, source.parent)
        import unreal
        _run(unreal, manifest, source.parent, report)
    except Exception as exc:
        report["errors"].append(str(exc))
        report["traceback"] = traceback.format_exc()
        report["partial_changes"] = report["mutation_started"]
        if report["partial_changes"]:
            report["warnings"].append("This failed job may have changed assets in memory or saved a subset. No rollback is claimed.")
    write_json(destination, report)
    return report
