"""Blender 4.5.7 integration checks for character discovery and material mapping.

Run in factory-startup background mode. NTE_BRIDGE_DISCOVERY_SOURCE optionally
points to a local copy of the unpacked 078_Nitsa directory. No game fixtures are
stored in this repository; only generated test scenes are saved in artifacts.
"""

import hashlib
import json
import os
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
import nte_bridge
from nte_bridge import blender_ui
from nte_bridge.blender_export import profile_manifest
from nte_bridge.core import BridgeError
from nte_bridge.discovery import scan_character

OUT = ROOT / "artifacts" / "blender_discovery_smoke"
CHECKS = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check(name):
    CHECKS.append(name)
    print("PASS:", name)


def must_reject(callback, message):
    try:
        result = callback()
    except (BridgeError, ValueError, RuntimeError, OSError):
        return
    require(result == {"CANCELLED"}, message)


def source_hashes(folder):
    paths = list(folder.glob("*.json")) + list((folder / "ter").rglob("*.json"))
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def create_mesh(names, materials=None):
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = "DiscoveryArmature"
    rig.data.bones[0].name = "root"
    data = bpy.data.meshes.new("DiscoveryMeshData")
    vertices, faces = [], []
    for index in range(len(names)):
        start = len(vertices)
        vertices += [(index, 0, 0), (index + 0.8, 0, 0), (index, 0, 1)]
        faces.append((start, start + 1, start + 2))
    data.from_pydata(vertices, [], faces)
    mesh = bpy.data.objects.new("DiscoveryMesh", data)
    bpy.context.scene.collection.objects.link(mesh)
    mesh.parent = rig
    mesh.modifiers.new("Armature", "ARMATURE").object = rig
    mesh.vertex_groups.new(name="root").add(list(range(len(vertices))), 1.0, "REPLACE")
    data.uv_layers.new(name="UV0")
    for index, name in enumerate(names):
        material = materials[index] if materials else bpy.data.materials.new(name)
        data.materials.append(material)
        data.polygons[index].material_index = index
    return mesh, rig


def choose_source(settings, path):
    entry = next(item for item in settings.source_meshes if item.asset_path == path)
    settings.source_mesh_choice = entry.name
    require(bpy.ops.nte_bridge.apply_source() == {"FINISHED"}, "source application failed")


def snapshot(settings):
    return {
        "source_folder": settings.source_folder,
        "source_data": settings.source_data,
        "applied_source_mesh": settings.applied_source_mesh,
        "paths": [settings.mesh_path, settings.skeleton_path, settings.physics_path],
        "parts": [(p.part_id, p.source_slot, p.material_path, p.source_material.name)
                  for p in settings.parts],
        "catalog": [(m.asset_path, m.parent_path, m.available, m.metadata_json)
                    for m in settings.material_catalog],
    }


def generated_mesh_record(name, slots):
    return [{"Type": "SkeletalMesh", "Name": name,
             "Package": "/Game/DiscoveryTest/" + name,
             "Properties": {
                 "Skeleton": {"ObjectName": "Skeleton'TestSkeleton'",
                              "ObjectPath": "/Game/DiscoveryTest/TestSkeleton.0"}},
             "SkeletalMaterials": [
                 {"MaterialSlotName": alias,
                  "Material": {"ObjectName": "Material'" + path.rsplit("/", 1)[-1] + "'",
                               "ObjectPath": path + ".0"}}
                 for alias, path in slots]}]


def write_fixture(folder, name, slots):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + ".json")).write_text(
        json.dumps(generated_mesh_record(name, slots)), encoding="utf-8")


def main():
    require(bpy.app.version == (4, 5, 7), "Run this integration test with Blender 4.5.7")
    OUT.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    source = Path(os.environ.get("NTE_BRIDGE_DISCOVERY_SOURCE", r"E:\NTE mods\078_Nitsa"))
    if not source.is_dir():
        result = {"success": True, "skipped": "Local unpacked Nitsa source unavailable",
                  "blender_version": bpy.app.version_string, "checks": []}
        (OUT / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(result["skipped"])
        return
    hashes = source_hashes(source)
    inventory = scan_character(source)
    body_path = "/Game/Characters/Player/078_Nitsa/player_078_Nitsa_skin_morpher"
    body = next(mesh for mesh in inventory["meshes"] if mesh["asset_path"] == body_path)
    require(len(body["slots"]) == 11, "Nitsa main body should have 11 source slots")
    require(len(inventory["meshes"]) >= 2, "Nitsa fire mesh should remain a separate candidate")
    check("real_source_mesh_candidates_and_slot_count")

    # Deliberately reverse source order. Include hair slot aliases (Ml_) whose
    # referenced material basename uses MI_, plus a same-material duplicate.
    expected = list(reversed(body["slots"]))
    names = [slot["name"] if "hair" in slot["name"] else slot["material_path"].rsplit("/", 1)[-1]
             for slot in expected]
    # Every slot owns a face: slots without faces are ignored by design.
    mesh, rig = create_mesh(names + ["_shared", "_suffix", "_custom"])
    shared_slot = next(index for index, slot in enumerate(expected)
                       if slot["material_path"].endswith("/eye_bantou"))
    mesh.data.materials[len(names)] = mesh.material_slots[shared_slot].material
    suffix_material = bpy.data.materials.new("eye_bantou.001")
    mesh.data.materials[len(names) + 1] = suffix_material
    custom_material = bpy.data.materials.new("CustomUnmappedPart")
    mesh.data.materials[len(names) + 2] = custom_material
    settings = bpy.context.scene.nte_bridge
    settings.mesh = mesh
    require(settings.armature == rig, "Selecting a bound mesh did not find its armature")
    settings.source_folder = '"' + str(source) + '"'
    require(blender_ui._resolved_folder(settings.source_folder) ==
            blender_ui._resolved_folder(str(source)), "Pasted quoted folder was not normalized")
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Nitsa scan operator failed")
    check("quoted_source_folder_scans_successfully")
    require(not settings.applied_source_mesh, "Multiple meshes must require explicit selection")
    check("multiple_mesh_scan_requires_explicit_selection")
    choose_source(settings, body_path)
    require(settings.create_placeholders, "Discovered role must enable missing placeholders on first use")
    settings.create_placeholders = False
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Repeat scan failed")
    require(not settings.create_placeholders, "Same-role scan lost explicit placeholder opt-out")
    settings.create_placeholders = True
    check("source_placeholder_defaults_and_same_source_opt_out")
    require((settings.mesh_path, settings.skeleton_path, settings.physics_path) ==
            (body_path, body_path + "_Skeleton", body_path + "_PhysicsAsset"),
            "Three package paths were not populated from source metadata")
    check("all_three_paths_read_from_real_metadata")
    require([part.material_path for part in settings.parts[:11]] ==
            [slot["material_path"] for slot in expected],
            "Reordered source slots or Ml/MI aliases mapped to the wrong material")
    require(settings.parts[11].material_path == settings.parts[12].material_path ==
            "/Game/Characters/Player/Common_ter/eye_bantou", "Shared duplicate material path lost")
    require(not settings.parts[13].material_path, "Unknown custom slot must not be matched by ordinal")
    require(len({part.part_id for part in settings.parts}) == len(settings.parts),
            "Separate slots sharing a material collapsed identities")
    check("reordered_hair_alias_shared_duplicate_suffix_and_custom_mapping")

    available = [item for item in settings.material_catalog if item.available]
    require(len(available) == 15, "Expected all 15 locally exported material instances")
    shared = next(item for item in settings.material_catalog if item.asset_path.endswith("/eye_bantou"))
    require(not shared.available, "Missing shared material JSON must be marked unavailable")
    face = next(item for item in available if item.asset_path.endswith("/MI_player_078_Nitsa_face"))
    require(face.parent_path.endswith("/MI_CharacterToonFace_XL"), "Parent material reference lost")
    face_metadata = json.loads(face.metadata_json)
    require(not face_metadata["parent_chain_complete"] and not face.root_material_path,
            "Unresolved external parent incorrectly presented as complete mother material")
    require(not settings.textures, "Discovering source textures must not opt them into packaging")
    check("material_inventory_parent_availability_and_texture_opt_in")
    require(bpy.ops.nte_bridge.source_report(material_name=face.name) == {"FINISHED"},
            "Material information report failed")
    report_text = bpy.data.texts.get("NTE Bridge 角色资源.json")
    require(report_text and json.loads(report_text.as_string()) == face_metadata,
            "Material information report omitted discovered metadata")
    check("complete_material_metadata_report")

    settings.parts[0].material_path = "/Game/DiscoveryTest/ManualOverride"
    settings.parts[13].material_path = "/Game/DiscoveryTest/CustomMaterial"
    original_ids = [part.part_id for part in settings.parts]
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Repeat scan failed")
    if settings.applied_source_mesh != body_path:
        choose_source(settings, body_path)
    require([part.part_id for part in settings.parts] == original_ids, "Repeat scan changed stable slot UUIDs")
    require(settings.parts[0].material_path == "/Game/DiscoveryTest/ManualOverride",
            "Repeat scan replaced a manual material mapping")
    require(settings.parts[13].material_path == "/Game/DiscoveryTest/CustomMaterial",
            "Repeat scan replaced custom material mapping")
    check("repeat_scan_preserves_ids_and_manual_mappings")

    project = OUT / "DiscoveryValidation.uproject"
    project.write_text('{"FileVersion":3,"EngineAssociation":"5.6"}', encoding="utf-8")
    settings.project_file = str(project)
    manifest = profile_manifest(settings)
    require(len(manifest["parts"]) == 14, "A valid discovered profile failed manifest validation")
    require(not manifest["textures"], "Original texture inventory was exported without opt-in")
    check("discovered_profile_builds_valid_manifest")

    first, second = settings.mesh.material_slots[0].material, settings.mesh.material_slots[1].material
    first_id, second_id = settings.parts[0].part_id, settings.parts[1].part_id
    second_path = settings.parts[1].material_path
    settings.mesh.data.materials[0], settings.mesh.data.materials[1] = second, first
    must_reject(lambda: profile_manifest(settings), "Reordered slots exported without reconciliation")
    require(bpy.ops.nte_bridge.refresh_slots() == {"FINISHED"}, "Unambiguous slot reorder could not be refreshed")
    require(settings.parts[0].part_id == second_id and settings.parts[1].part_id == first_id,
            "Slot reorder left identities attached to the wrong source materials")
    require(settings.parts[0].material_path == second_path and
            settings.parts[1].material_path == "/Game/DiscoveryTest/ManualOverride",
            "Slot reorder left mappings attached to the wrong source materials")
    check("post_binding_slot_reorder_preserves_material_identity")

    empty = OUT / "empty_source"
    empty.mkdir(exist_ok=True)
    settings.source_folder = str(empty)
    must_reject(lambda: blender_ui._ensure_source_current(settings), "Changed source folder accepted stale paths")
    must_reject(lambda: profile_manifest(settings), "Direct export accepted stale source folder")
    must_reject(lambda: bpy.ops.nte_bridge.scan_character(), "Empty source directory should be rejected")
    settings.source_folder = str(source)
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Restore scan failed")
    if settings.applied_source_mesh != body_path:
        choose_source(settings, body_path)
    check("changed_or_empty_source_cannot_export_stale_paths")

    before_save = snapshot(settings)
    scene_file = OUT / "discovery_catalog.blend"
    require(bpy.ops.wm.save_as_mainfile(filepath=str(scene_file)) == {"FINISHED"}, "Test scene save failed")
    bpy.ops.wm.open_mainfile(filepath=str(scene_file))
    settings = bpy.context.scene.nte_bridge
    require(snapshot(settings) == before_save, "Source inventory/mappings changed across Blender save/reload")
    require(len(profile_manifest(settings)["parts"]) == 14, "Saved source profile no longer validates")
    check("catalogue_and_mapping_save_reload")

    old_rig = settings.armature
    rebound_rig = old_rig.copy()
    rebound_rig.data = old_rig.data.copy()
    rebound_rig.data.bones[0].name = "rebound_root"
    bpy.context.scene.collection.objects.link(rebound_rig)
    settings.mesh.parent = rebound_rig
    next(modifier for modifier in settings.mesh.modifiers if modifier.type == 'ARMATURE').object = rebound_rig
    require(settings.armature == old_rig, "Test did not retain a stale rig selection")
    rebound_manifest = profile_manifest(settings)
    require(settings.armature == rebound_rig, "Export did not refresh the actually bound armature")
    require(rebound_manifest["mesh"]["expected"]["bones"][0]["name"] == "rebound_root",
            "Export used stale armature bones after a mesh binding change")
    check("bound_armature_is_derived_again_at_export")

    # Replacing a source material must retain a linked part's identity while
    # clearing its obsolete material mapping until the user reconciles it.
    changed_id = settings.parts[0].part_id
    main_node = next(node for node in settings.graph.nodes
                     if node.bl_idname == 'NTEBridgeObject' and node.is_main)
    main_node.slots[0].split = True
    linked_output = next(socket for socket in main_node.outputs if socket.identifier == changed_id)
    linked_group = settings.graph.nodes.new('NTEBridgeGroup')
    settings.graph.links.new(linked_output, linked_group.inputs[0])
    output_node = next(node for node in settings.graph.nodes if node.bl_idname == 'NTEBridgeOutput')
    settings.graph.links.new(linked_group.outputs[0], output_node.inputs[-1])
    settings.mesh.data.materials[0] = bpy.data.materials.new("AChangedSourceMaterial")
    must_reject(lambda: profile_manifest(settings), "Changed slot material exported stale mapping")
    check("changed_source_slot_requires_reconciliation")
    require(bpy.ops.nte_bridge.refresh_slots() == {"FINISHED"}, "Replaced material could not be reconciled")
    linked_output = next((socket for socket in main_node.outputs if socket.identifier == changed_id), None)
    require(settings.parts[0].part_id == changed_id and linked_output is not None,
            "Source material replacement invalidated its linked part identity")
    require(linked_output.is_linked and linked_group.inputs[0].is_linked,
            "Source material replacement removed a user's graph link")
    require(not settings.parts[0].material_path and not settings.parts[0].catalog_material
            and not settings.parts[0].automatic_material_path,
            "Source material replacement retained an obsolete mapping or provenance")
    settings.parts[0].material_path = "/Game/DiscoveryTest/ReconciledMaterial"
    require(len(profile_manifest(settings)["parts"]) == 14,
            "Reconciled replacement still invalidates the graph or manifest")
    check("material_replacement_keeps_linked_identity_and_clears_mapping")

    # A genuinely ambiguous alias is built from generated metadata rather than
    # copying any game asset into the repository.
    collision = OUT / "collision_source"
    write_fixture(collision, "AliasCollision", [("AmbiguousMaterial", "/Game/A/MaterialA"),
                                               ("AmbiguousMaterial", "/Game/B/MaterialB")])
    collision_mesh, collision_rig = create_mesh(["AmbiguousMaterial"])
    settings.mesh = collision_mesh
    require(settings.armature == collision_rig, "Changing mesh did not derive the new bound armature")
    settings.source_folder = str(collision)
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Synthetic collision scan failed")
    require(settings.applied_source_mesh == "/Game/DiscoveryTest/AliasCollision", "Single mesh was not auto-selected")
    require(not settings.parts[0].material_path, "Ambiguous name alias was silently assigned")
    check("ambiguous_name_alias_remains_unmapped")

    switch_a, switch_b = OUT / "switch_source_a", OUT / "switch_source_b"
    write_fixture(switch_a, "SwitchA", [("SwitchPart", "/Game/A/MaterialA")])
    write_fixture(switch_b, "SwitchB", [("SwitchPart", "/Game/B/MaterialB")])
    switch_mesh, _ = create_mesh(["SwitchPart", "KeepExplicitMapping"])
    settings.mesh = switch_mesh
    settings.source_folder = str(switch_a)
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "First switch-source scan failed")
    require(settings.parts[0].material_path == "/Game/A/MaterialA", "First automatic mapping failed")
    settings.parts[1].material_path = "/Game/Custom/ExplicitMaterial"
    switch_ids = [part.part_id for part in settings.parts]
    settings.source_folder = str(switch_b)
    require(bpy.ops.nte_bridge.scan_character() == {"FINISHED"}, "Second switch-source scan failed")
    require(settings.parts[0].material_path == "/Game/B/MaterialB",
            "Changing source retained stale automatic material mapping")
    require(settings.parts[1].material_path == "/Game/Custom/ExplicitMaterial",
            "Changing source erased explicit custom material mapping")
    require([part.part_id for part in settings.parts] == switch_ids,
            "Changing source changed Blender slot identities")
    check("source_switch_replaces_only_automatic_mappings")

    moved_mesh, _ = create_mesh(["MovedMaterialA", "RemovedMaterialB"])
    settings.mesh = moved_mesh
    moved_a = moved_mesh.material_slots[0].material
    moved_a_id, removed_b_id = [part.part_id for part in settings.parts]
    settings.parts[0].material_path = "/Game/Custom/MovedA"
    settings.parts[1].material_path = "/Game/Custom/RemovedB"
    moved_mesh.data.materials[0] = bpy.data.materials.new("ReplacementMaterialC")
    moved_mesh.data.materials[1] = moved_a
    require(bpy.ops.nte_bridge.refresh_slots() == {"FINISHED"}, "Mixed move/replacement reconciliation failed")
    require(settings.parts[1].part_id == moved_a_id and
            settings.parts[1].material_path == "/Game/Custom/MovedA",
            "Moving A in [A,B] to [C,A] lost its identity or mapping")
    require(settings.parts[0].part_id not in {moved_a_id, removed_b_id} and
            not settings.parts[0].material_path,
            "Replacement C stole a moved material identity or stale mapping")
    valid_part_ids = {part.part_id for part in settings.parts}
    moved_main = next(node for node in settings.graph.nodes if node.bl_idname == 'NTEBridgeObject' and node.is_main)
    require({slot.part_id for slot in moved_main.slots} == valid_part_ids and
            {socket.identifier for socket in moved_main.inputs} == valid_part_ids,
            "Main object node kept a removed part identity")
    settings.parts[0].material_path = "/Game/Custom/ReplacementC"
    require(len(profile_manifest(settings)["parts"]) == 2,
            "Mixed move/replacement left an invalid graph after mapping")
    check("mixed_move_and_replacement_does_not_steal_identity")

    require(source_hashes(source) == hashes, "Discovery modified an original unpacked asset")
    check("original_source_metadata_unchanged")
    result = {"success": True, "blender_version": bpy.app.version_string,
              "addon_module": nte_bridge.__file__, "source": str(source.resolve()),
              "source_mesh": body_path, "source_mesh_candidates": len(inventory["meshes"]),
              "source_slots": len(body["slots"]), "synthetic_slots": 14,
              "available_materials": len(available), "source_files_verified": len(hashes),
              "checks": CHECKS, "saved_scene": str(scene_file)}
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
