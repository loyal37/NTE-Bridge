"""Prepare isolated cooking fixtures; preparation never starts Unreal.

Normal Python: ``python tests/unreal_cooking_fixture.py --prepare``.
Then launch the generated run_fixture.py with its generated .uproject through
the UE PythonScript commandlet. Only artifacts/cooking_fixture is writable by
the editor phase. The existing Blender smoke fixture is copied read-only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = (ROOT / "artifacts/cooking_fixture").resolve()
CHARACTER_ROOT = "/Game/Characters/Player/BridgeFixture"
OTHER_ROOT = CHARACTER_ROOT + "Other"
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import load_manifest, resolve_source, write_json


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(manifest_path=None):
    if manifest_path is None:
        smoke = json.loads((ROOT / "artifacts/blender_smoke/result.json").read_text(encoding="utf-8"))
        manifest_path = smoke["manifest"]
    source_path = Path(manifest_path).resolve()
    source = load_manifest(source_path)
    assert len(source["parts"]) == 2, "Use the synthetic Blender smoke mesh with two slots"
    assert {entry["role"] for entry in source["textures"]} == {"BASE_COLOR", "ID_TEX", "LIGHT_MAP", "NORMAL"}
    folder = FIXTURE_ROOT / ("run-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    project = folder / "CookingFixture.uproject"
    template = ROOT / "artifacts/ue_smoke/NTEBridgeSmoke.uproject"
    project_data = json.loads(template.read_text(encoding="utf-8")) if template.is_file() else {
        "FileVersion": 3, "EngineAssociation": "5.6"}
    project_data["Plugins"] = [{"Name": name, "Enabled": True} for name in (
        "PythonScriptPlugin", "EditorScriptingUtilities", "GeometryScripting", "ControlRig")]
    write_json(project, project_data)
    config = folder / "Config/DefaultEngine.ini"
    config.parent.mkdir()
    config.write_text(
        "[/Script/EngineSettings.GameMapsSettings]\nEditorStartupMap=/Engine/Maps/Entry\n"
        "GameDefaultMap=/Engine/Maps/Entry\n\n"
        "[/Script/UnrealEd.EditorLoadingSavingSettings]\nbAutoSaveEnable=False\n", encoding="utf-8")
    job = folder / "job"
    job.mkdir()
    sources = [source["mesh"]["source_file"]] + [item["source_file"] for item in source["textures"]]
    originals = {str(source_path): digest(source_path)}
    for relative in sources:
        original = resolve_source(source_path.parent, relative)
        destination = resolve_source(job, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
        originals[str(original)] = digest(original)
    manifest = copy.deepcopy(source)
    manifest.update(job_id=str(uuid.uuid4()), project_file=str(project), create_placeholders=True, features=[])
    manifest.pop("material_previews", None)
    manifest["mesh"].update(asset_path=CHARACTER_ROOT + "/SM_BridgeCook",
                            skeleton_path=CHARACTER_ROOT + "/SK_BridgeCook",
                            physics_asset_path=CHARACTER_ROOT + "/PH_BridgeCook")
    manifest["parts"][0]["material_path"] = CHARACTER_ROOT + "/Materials/M_Original"
    manifest["parts"][1]["material_path"] = CHARACTER_ROOT + "/Materials/MI_OriginalGame"
    for item in manifest["textures"]:
        item.update(asset_path=CHARACTER_ROOT + "/Textures/T_" + item["role"], origin="mod")
    manifest["export_assets"] = [{"asset_path": manifest["mesh"]["asset_path"],
                                  "asset_type": "SkeletalMesh", "origin": "mod"}] + [
        {"asset_path": item["asset_path"], "asset_type": "Texture2D", "origin": "mod"}
        for item in manifest["textures"]]
    manifest_path = job / "manifest.json"
    write_json(manifest_path, manifest)
    load_manifest(manifest_path)
    details = {"project_file": str(project), "manifest": str(manifest_path),
               "character_root": CHARACTER_ROOT, "other_root": OTHER_ROOT,
               "source_hashes": originals, "source_template": str(template),
               "expected_additional_assets": {
                   CHARACTER_ROOT + "/Materials/MI_Custom": "MaterialInstanceConstant",
                   CHARACTER_ROOT + "/Logic/BP_Character": "Blueprint",
                   CHARACTER_ROOT + "/Logic/ABP_Character": "AnimBlueprint",
                   OTHER_ROOT + "/MI_Other": "MaterialInstanceConstant",
                   OTHER_ROOT + "/BP_Other": "Blueprint"}}
    details_path = folder / "fixture.json"
    write_json(details_path, details)
    runner = folder / "run_fixture.py"
    runner.write_text(
        "import sys\nsys.path.insert(0, " + repr(str(ROOT / "tests")) + ")\n"
        "from unreal_cooking_fixture import editor_phase\n"
        "editor_phase(" + repr(str(details_path)) + ")\n", encoding="utf-8")
    details["runner"] = str(runner)
    write_json(details_path, details)
    write_json(FIXTURE_ROOT / "latest.json", {"fixture": str(details_path), **details})
    print(json.dumps({"fixture": str(details_path), "project": str(project), "runner": str(runner)}, indent=2))
    return details_path


def editor_phase(details_path):
    import unreal
    from nte_bridge.unreal_receiver import run_job

    details_path = Path(details_path).resolve()
    assert details_path.is_relative_to(FIXTURE_ROOT), "Only the isolated cooking fixture is supported"
    details = json.loads(details_path.read_text(encoding="utf-8"))
    project = Path(details["project_file"]).resolve()
    assert project.parent == details_path.parent and project.is_relative_to(FIXTURE_ROOT)
    actual_project = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
    assert actual_project == project, "Refusing to modify a different UE project"
    result_path = details_path.with_name("fixture_result.json")
    assets, tools = unreal.EditorAssetLibrary, unreal.AssetToolsHelpers.get_asset_tools()
    checks = []

    def create(path, cls, factory):
        assert not assets.does_asset_exist(path), "Fixture already exists; prepare a new isolated run: " + path
        parent, name = path.rsplit("/", 1)
        result = tools.create_asset(name, parent, cls, factory)
        assert result is not None, "Factory failed: " + path
        return result

    def save(asset):
        assert assets.save_loaded_asset(asset, only_if_is_dirty=False), "Save failed: " + asset.get_path_name()

    def make_instance(path, parent):
        instance = create(path, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        unreal.MaterialEditingLibrary.set_material_instance_parent(instance, parent)
        save(instance)
        return instance

    def make_blueprint(path, factory, cls=unreal.Blueprint):
        blueprint = create(path, cls, factory)
        unreal.BlueprintEditorLibrary.compile_blueprint(blueprint)
        assert unreal.BlueprintEditorLibrary.generated_class(blueprint), "Blueprint failed to generate its class"
        save(blueprint)
        return blueprint

    try:
        manifest_path = Path(details["manifest"])
        manifest = load_manifest(manifest_path)
        original = create(manifest["parts"][0]["material_path"], unreal.Material, unreal.MaterialFactoryNew())
        save(original)
        original_mi = make_instance(manifest["parts"][1]["material_path"], original)
        report = run_job(manifest_path)
        assert report["success"], report
        expected_original_mi = next(item for item in report["assets"]
                                    if item["asset_path"] == manifest["parts"][1]["material_path"])
        assert expected_original_mi["asset_type"] == "MaterialInstanceConstant"
        assert expected_original_mi["origin"] == "game_placeholder"
        checks += ["synthetic_mesh_four_texture_import", "original_mi_identified_as_placeholder"]

        make_instance(CHARACTER_ROOT + "/Materials/MI_Custom", original)
        bp_factory = unreal.BlueprintFactory()
        bp_factory.set_editor_property("parent_class", unreal.Actor)
        make_blueprint(CHARACTER_ROOT + "/Logic/BP_Character", bp_factory)
        anim_factory = unreal.AnimBlueprintFactory()
        anim_factory.set_editor_property("parent_class", unreal.AnimInstance)
        anim_factory.set_editor_property("target_skeleton", unreal.load_asset(manifest["mesh"]["skeleton_path"]))
        anim_factory.set_editor_property("preview_skeletal_mesh", unreal.load_asset(manifest["mesh"]["asset_path"]))
        make_blueprint(CHARACTER_ROOT + "/Logic/ABP_Character", anim_factory, unreal.AnimBlueprint)
        make_instance(OTHER_ROOT + "/MI_Other", original)
        other_bp_factory = unreal.BlueprintFactory()
        other_bp_factory.set_editor_property("parent_class", unreal.Actor)
        make_blueprint(OTHER_ROOT + "/BP_Other", other_bp_factory)
        checks.append("custom_mi_blueprint_animblueprint_and_other_folder_created")

        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        registry.scan_paths_synchronous([CHARACTER_ROOT, OTHER_ROOT], True)
        def inventory(path):
            entries = registry.get_assets_by_path(path, recursive=True, include_only_on_disk_assets=True)
            return sorted([{"asset_path": str(entry.package_name),
                            "asset_type": str(entry.asset_class_path.asset_name)} for entry in entries or []],
                          key=lambda entry: entry["asset_path"])
        character, other = inventory(CHARACTER_ROOT), inventory(OTHER_ROOT)
        by_path = {entry["asset_path"]: entry["asset_type"] for entry in character + other}
        for path, kind in details["expected_additional_assets"].items():
            assert by_path.get(path) == kind, (path, kind, by_path.get(path))
        assert not any(entry["asset_path"].startswith(OTHER_ROOT + "/") for entry in character)
        assert len(other) == 2
        checks += ["asset_registry_saved_types_verified", "character_path_boundary_excludes_prefix_sibling"]
        for path, expected in details["source_hashes"].items():
            assert digest(path) == expected, "Source smoke fixture was modified: " + path
        checks.append("original_smoke_sources_unchanged")
        result = {"success": True, "project_file": str(project), "manifest": str(manifest_path),
                  "character_root": CHARACTER_ROOT, "other_root": OTHER_ROOT,
                  "character_assets": character, "other_assets": other,
                  "checks": checks, "ue_report": str(manifest_path.with_name("ue_report.json"))}
    except Exception:
        result = {"success": False, "project_file": str(project), "checks": checks,
                  "traceback": traceback.format_exc()}
        write_json(result_path, result)
        raise
    write_json(result_path, result)
    print("NTE_COOKING_FIXTURE=" + json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", required=True,
                        help="Create an isolated fixture job and UE runner; never starts Unreal")
    parser.add_argument("--manifest", help="Synthetic Blender smoke manifest; default uses its latest result")
    arguments = parser.parse_args()
    prepare(arguments.manifest)
