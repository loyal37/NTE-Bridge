"""Actual UE preview/slot regression in a unique isolated project (two launches)."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import load_manifest, resolve_source, write_json


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_saved_preview_job(manifest_path):
    """Read back a copied real job after a separate editor launch."""
    import unreal

    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    project = Path(manifest["project_file"]).resolve()
    assert project.is_relative_to((ROOT / "artifacts").resolve())
    assert Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve() == project
    mesh = unreal.load_asset(manifest["mesh"]["asset_path"])
    slots = mesh.get_editor_property("materials")
    identities = {str(slot.get_editor_property("imported_material_slot_name")): slot for slot in slots}
    assert len(identities) == len(manifest["parts"]) == len(slots)
    for part in manifest["parts"]:
        slot = identities[part["slot_key"]]
        material = slot.get_editor_property("material_interface")
        assert material.get_path_name().split(".")[0] == part["material_path"]
        assert str(slot.get_editor_property("material_slot_name")) == material.get_name()
    bound = []
    for entry in manifest.get("material_previews", []):
        material, texture = unreal.load_asset(entry["material_path"]), unreal.load_asset(entry["texture_path"])
        node = unreal.MaterialEditingLibrary.get_material_property_input_node(material, unreal.MaterialProperty.MP_BASE_COLOR)
        assert node and node.get_editor_property("texture") == texture
        assert unreal.EditorAssetLibrary.get_metadata_tag(node, "NTEBridge.PreviewOwner")
        assert texture.get_editor_property("compression_settings") == unreal.TextureCompressionSettings.TC_BC7
        assert texture.get_editor_property("srgb")
        bound.append({"material_path": entry["material_path"], "texture_path": entry["texture_path"]})
    morphs = sorted(str(morph.get_name()) for morph in mesh.get_editor_property("morph_targets"))
    assert morphs == sorted(manifest["mesh"]["expected"]["shape_keys"])
    previews = {entry["asset_path"] for entry in manifest["textures"] if entry.get("origin") == "preview"}
    assert previews.isdisjoint({entry["asset_path"] for entry in manifest["export_assets"]})
    return {"success": True, "slots": len(slots), "unique_imported_identities": len(identities),
            "morphs": len(morphs), "preview_textures": len(previews), "bindings": bound,
            "export_assets": len(manifest["export_assets"])}


def editor_phase(folder, phase):
    import unreal
    from nte_bridge.unreal_receiver import run_job

    folder = Path(folder).resolve()
    assert folder.is_relative_to((ROOT / "artifacts/preview_smoke").resolve())
    project = folder / "PreviewSmoke.uproject"
    assert Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve() == project
    manifest_path = folder / "job/manifest.json"
    manifest = load_manifest(manifest_path)
    library, assets = unreal.MaterialEditingLibrary, unreal.EditorAssetLibrary
    base_color = unreal.MaterialProperty.MP_BASE_COLOR
    legacy_path = "/Game/TestOriginal/M_Legacy"
    new_path = "/Game/TestOriginal/M_New"
    authored_path = "/Game/TestOriginal/M_Authored"
    instance_path = "/Game/TestOriginal/MI_Authored"
    base_texture = next(entry["asset_path"] for entry in manifest["textures"] if entry["role"] == "BASE_COLOR")
    checks = []

    def create_material(path):
        parent, name = path.rsplit("/", 1)
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, parent, unreal.Material, unreal.MaterialFactoryNew())
        assert material
        return material

    def inspect_slots(report):
        mesh = unreal.load_asset(manifest["mesh"]["asset_path"])
        slots = mesh.get_editor_property("materials")
        assert len(slots) == len(manifest["parts"])
        imported = []
        for part in manifest["parts"]:
            slot = slots[report["slot_map"][part["id"]]]
            material = slot.get_editor_property("material_interface")
            assert str(slot.get_editor_property("material_slot_name")) == material.get_name()
            assert str(slot.get_editor_property("imported_material_slot_name")) == part["slot_key"]
            imported.append(str(slot.get_editor_property("imported_material_slot_name")))
        assert len(set(imported)) == len(imported)

    def run_case(name):
        manifest["job_id"] = str(uuid.uuid4())
        write_json(manifest_path, manifest)
        report = run_job(manifest_path, folder / (name + "_report.json"))
        assert report["success"], report
        inspect_slots(report)
        return report

    if phase == "first":
        legacy = create_material(legacy_path)
        assert library.get_num_material_expressions(legacy) == 0
        assert not assets.get_metadata_tag(legacy, "NTEBridge.PreviewOwner")
        assets.save_loaded_asset(legacy, only_if_is_dirty=False)
        first = run_case("first")
        node = library.get_material_property_input_node(legacy, base_color)
        assert isinstance(node, unreal.MaterialExpressionTextureSampleParameter2D)
        assert node.get_editor_property("texture").get_path_name().split(".")[0] == base_texture
        assert library.get_num_material_expressions(legacy) == 1
        material_record = next(item for item in first["assets"] if item["asset_path"] == legacy_path)
        assert material_record["changed"] and material_record["saved"]
        assert material_record["origin"] == "game_placeholder"
        texture = unreal.load_asset(base_texture)
        assert texture.get_editor_property("compression_settings") == unreal.TextureCompressionSettings.TC_BC7
        assert texture.get_editor_property("srgb")
        assert next(item for item in first["assets"] if item["asset_path"] == base_texture)["origin"] == "preview"
        assert base_texture not in {item["asset_path"] for item in manifest["export_assets"]}
        assert legacy_path not in {item["asset_path"] for item in manifest["export_assets"]}
        write_json(folder / "first_state.json", {"node_path": node.get_path_name(), "slot_map": first["slot_map"]})
        checks += ["legacy-empty-placeholder-upgraded-and-saved", "shared-material-visible-names-with-unique-import-identities",
                   "base-color-connected", "preview-texture-bc7-srgb", "preview-and-original-material-excluded-from-package"]
    else:
        state = json.loads((folder / "first_state.json").read_text(encoding="utf-8"))
        legacy = unreal.load_asset(legacy_path)
        node = library.get_material_property_input_node(legacy, base_color)
        assert node and node.get_path_name() == state["node_path"]
        assert node.get_editor_property("texture").get_path_name().split(".")[0] == base_texture
        assert assets.get_metadata_tag(node, "NTEBridge.PreviewOwner")
        checks.append("saved-material-connection-and-metadata-survive-editor-reload")

        alternate = copy.deepcopy(next(entry for entry in manifest["textures"] if entry["role"] == "BASE_COLOR"))
        alternate.update(id=str(uuid.uuid4()), asset_path="/Game/TestOriginal/T_Alternate")
        manifest["textures"].append(alternate)
        manifest["material_previews"][0]["texture_path"] = alternate["asset_path"]
        second = run_case("second")
        assert second["slot_map"] == state["slot_map"]
        node = library.get_material_property_input_node(legacy, base_color)
        assert node.get_path_name() == state["node_path"]
        assert node.get_editor_property("texture").get_path_name().split(".")[0] == alternate["asset_path"]
        assert library.get_num_material_expressions(legacy) == 1
        checks.append("reimport-updates-existing-managed-node-without-duplicates")

        authored = create_material(authored_path)
        authored_node = library.create_material_expression(authored, unreal.MaterialExpressionConstant3Vector, -200, 0)
        authored_node.set_editor_property("constant", unreal.LinearColor(0.1, 0.2, 0.3, 1))
        assert library.connect_material_property(authored_node, "", base_color)
        library.recompile_material(authored)
        assets.save_loaded_asset(authored, only_if_is_dirty=False)
        instance = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "MI_Authored", "/Game/TestOriginal", unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        library.set_material_instance_parent(instance, authored)
        assets.save_loaded_asset(instance, only_if_is_dirty=False)
        protected_files = [folder / "Content/TestOriginal/M_Authored.uasset", folder / "Content/TestOriginal/MI_Authored.uasset"]
        protected_hashes = {str(path): digest(path) for path in protected_files}
        manifest["parts"][0]["material_path"] = authored_path
        manifest["parts"][1]["material_path"] = instance_path
        manifest["material_previews"] = [{"material_path": path, "texture_path": base_texture}
                                        for path in (authored_path, instance_path)]
        skipped = run_case("authored")
        assert len(skipped["material_previews"]) == 2 and all(not item["applied"] for item in skipped["material_previews"])
        assert len(skipped["warnings"]) == 2
        assert library.get_material_property_input_node(authored, base_color) == authored_node
        assert library.get_num_material_expressions(authored) == 1
        assert not assets.get_metadata_tag(authored, "NTEBridge.PreviewOwner")
        assert {str(path): digest(path) for path in protected_files} == protected_hashes
        checks += ["authored-material-and-instance-preserved-byte-for-byte", "skipped-previews-explain-why"]

        manifest["parts"][0]["material_path"] = new_path
        manifest["parts"][1]["material_path"] = legacy_path
        manifest["material_previews"] = [{"material_path": path, "texture_path": base_texture}
                                        for path in (new_path, legacy_path)]
        created = run_case("created")
        new_material = unreal.load_asset(new_path)
        assert library.get_material_property_input_node(new_material, base_color).get_editor_property("texture") == unreal.load_asset(base_texture)
        roughness = library.get_material_property_input_node(new_material, unreal.MaterialProperty.MP_ROUGHNESS)
        assert abs(roughness.get_editor_property("r") - 0.7) < 0.0001
        assert library.get_num_material_expressions(new_material) == 2
        assert library.get_num_material_expressions(legacy) == 1
        checks.append("new-placeholder-gets-preview-and-roughness-without-changing-legacy-roughness")

        user_node = library.create_material_expression(new_material, unreal.MaterialExpressionConstant3Vector, -200, -200)
        user_node.set_editor_property("constant", unreal.LinearColor(0.6, 0.4, 0.2, 1))
        assert library.connect_material_property(user_node, "", base_color)
        library.recompile_material(new_material)
        assets.save_loaded_asset(new_material, only_if_is_dirty=False)
        user_edited_file = folder / "Content/TestOriginal/M_New.uasset"
        user_edited_hash = digest(user_edited_file)
        preserved = run_case("rewired")
        assert next(item for item in preserved["material_previews"] if item["material_path"] == new_path)["applied"] is False
        assert library.get_material_property_input_node(new_material, base_color) == user_node
        assert digest(user_edited_file) == user_edited_hash
        checks.append("user-rewired-managed-material-preserved-byte-for-byte")
    return {"success": True, "checks": checks}


def run(source, engine):
    manifest = load_manifest(source)
    assert len(manifest["parts"]) == 2 and len(manifest["textures"]) == 4, "Use the synthetic four-texture Blender smoke fixture"
    original_files = [source] + [resolve_source(source.parent, item) for item in
        [manifest["mesh"]["source_file"]] + [entry["source_file"] for entry in manifest["textures"]]]
    before = {str(path): digest(path) for path in original_files}
    root = ROOT / "artifacts/preview_smoke"
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="run-", dir=root)).resolve()
    project = folder / "PreviewSmoke.uproject"
    write_json(project, {"FileVersion": 3, "EngineAssociation": "5.6"})
    project_bytes = project.read_bytes()
    job_dir = folder / "job"
    job_dir.mkdir()
    for path in original_files[1:]:
        target = job_dir / path.relative_to(source.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    manifest["project_file"] = str(project)
    manifest["create_placeholders"] = True
    manifest["features"] = []
    manifest["mesh"]["asset_path"] = "/Game/TestOriginal/SM_Bridge"
    manifest["mesh"]["skeleton_path"] = "/Game/TestOriginal/SK_Bridge"
    manifest["mesh"]["physics_asset_path"] = ""
    for part in manifest["parts"]:
        part["material_path"] = "/Game/TestOriginal/M_Legacy"
    for entry in manifest["textures"]:
        entry["asset_path"] = "/Game/TestOriginal/T_" + entry["role"]
        entry["origin"] = "preview" if entry["role"] == "BASE_COLOR" else "mod"
    base_texture = next(entry["asset_path"] for entry in manifest["textures"] if entry["role"] == "BASE_COLOR")
    manifest["material_previews"] = [{"material_path": "/Game/TESTORIGINAL/M_LEGACY",
                                    "texture_path": "/Game/" + base_texture.removeprefix("/Game/").lower()}]
    manifest["export_assets"] = [{"asset_path": manifest["mesh"]["asset_path"], "asset_type": "SkeletalMesh", "origin": "mod"}]
    manifest["export_assets"] += [{"asset_path": entry["asset_path"], "asset_type": "Texture2D", "origin": "mod"}
                                  for entry in manifest["textures"] if entry["origin"] == "mod"]
    write_json(job_dir / "manifest.json", manifest)
    from nte_bridge.packaging import _unreal_command_line
    from nte_bridge.unreal_transport import REQUIRED_COMMANDLET_PLUGINS
    result = {"success": False, "project": str(project), "source": str(source), "checks": []}
    print("NTE_PREVIEW_SMOKE_FOLDER=" + str(folder), flush=True)
    try:
        for phase in ("first", "reload"):
            runner = folder / (phase + ".py")
            runner.write_text("import sys\nsys.path.insert(0, " + repr(str(ROOT / "tests")) + ")\n"
                "from unreal_preview_smoke import editor_phase\n"
                "from nte_bridge.core import write_json\nimport traceback\n"
                "try:\n    result = editor_phase(" + repr(str(folder)) + ", " + repr(phase) + ")\n"
                "except Exception:\n    result = {'success': False, 'traceback': traceback.format_exc()}\n"
                "write_json(" + repr(str(folder / (phase + "_result.json"))) + ", result)\n"
                "assert result['success'], result\n", encoding="utf-8")
            command = [engine / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe", project,
                       "-run=pythonscript", "-script=" + runner.as_posix(), "-unattended", "-nop4", "-nosplash", "-UTF8Output",
                       "-EnablePlugins=" + ",".join(REQUIRED_COMMANDLET_PLUGINS)]
            with (folder / (phase + ".log")).open("wb") as log:
                completed = subprocess.run(_unreal_command_line(command), stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=600)
            outcome = json.loads((folder / (phase + "_result.json")).read_text(encoding="utf-8"))
            assert completed.returncode == 0 and outcome["success"], outcome
            result["checks"].extend(outcome["checks"])
        result["success"] = True
    except Exception:
        result["traceback"] = traceback.format_exc()
    finally:
        result["original_files_unchanged"] = before == {str(path): digest(path) for path in original_files}
        result["project_bytes_unchanged"] = project_bytes == project.read_bytes()
        result["success"] &= result["original_files_unchanged"] and result["project_bytes_unchanged"]
        write_json(folder / "result.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--engine-dir", type=Path, default=Path("D:/ue/UE_5.6"))
    args = parser.parse_args()
    raise SystemExit(0 if run(args.manifest.resolve(), args.engine_dir.resolve())["success"] else 1)
