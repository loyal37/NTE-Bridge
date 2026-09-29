"""Run with UE PythonScript commandlet; NTE_BRIDGE_SMOKE_MANIFEST selects synthetic data.

Only run against artifacts/ue_smoke/NTEBridgeSmoke.uproject. This script refuses any other
project so it cannot accidentally import its test fixtures into the user's working project.
"""
import copy
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import load_manifest, write_json
from nte_bridge.unreal_receiver import run_job

import unreal


def main():
    project = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
    expected_project = (ROOT / "artifacts/ue_smoke/NTEBridgeSmoke.uproject").resolve()
    assert project == expected_project, "Smoke test only supports the isolated NTEBridgeSmoke project"
    source = Path(os.environ["NTE_BRIDGE_SMOKE_MANIFEST"]).resolve()
    manifest = load_manifest(source)
    assert Path(manifest["project_file"]).resolve() == expected_project
    wrong = copy.deepcopy(manifest)
    wrong["project_file"] = str(project.with_name("WrongProject.uproject"))
    wrong_path = source.with_name("wrong_project.json")
    write_json(wrong_path, wrong)
    rejected = run_job(wrong_path, source.with_name("wrong_project_report.json"))
    assert not rejected["success"] and not rejected["assets"] and not rejected["partial_changes"], rejected
    assert "Wrong Unreal project" in rejected["errors"][0], rejected
    before = unreal.SystemLibrary.get_console_variable_int_value("Interchange.FeatureFlags.Import.FBX")
    first = run_job(source, source.with_name("first_import_report.json"))
    assert first["success"], first
    assert first["features_applied"] == (not bool(manifest.get("features")))
    assert len(set(first["slot_map"].values())) == len(manifest["parts"]), first
    assert first["morph_targets"] == sorted(manifest["mesh"]["expected"]["shape_keys"])
    assert before == unreal.SystemLibrary.get_console_variable_int_value("Interchange.FeatureFlags.Import.FBX")
    hierarchy_mismatch = copy.deepcopy(manifest)
    leaf = next(bone for bone in hierarchy_mismatch["mesh"]["expected"]["bones"]
                if bone["parent"] and not any(other["parent"] == bone["name"]
                                             for other in hierarchy_mismatch["mesh"]["expected"]["bones"]))
    leaf["name"] += "_Unexpected"
    hierarchy_path = source.with_name("wrong_skeleton.json")
    write_json(hierarchy_path, hierarchy_mismatch)
    hierarchy_report = run_job(hierarchy_path, source.with_name("wrong_skeleton_report.json"))
    assert not hierarchy_report["success"] and not hierarchy_report["mutation_started"], hierarchy_report
    assert "Skeleton hierarchy mismatch" in hierarchy_report["errors"][0]
    missing = copy.deepcopy(manifest)
    missing["create_placeholders"] = False
    missing["parts"][0]["material_path"] = "/Game/NTEBridgeTest/M_MustNotCreate"
    missing_path = source.with_name("missing_placeholder.json")
    write_json(missing_path, missing)
    missing_report = run_job(missing_path, source.with_name("missing_placeholder_report.json"))
    assert not missing_report["success"] and not missing_report["mutation_started"], missing_report
    assert not unreal.EditorAssetLibrary.does_asset_exist("/Game/NTEBridgeTest/M_MustNotCreate")
    second = run_job(source)
    assert second["success"], second
    assert first["slot_map"] == second["slot_map"], second
    assert first["slot_signature"] == second["slot_signature"], second
    assert before == unreal.SystemLibrary.get_console_variable_int_value("Interchange.FeatureFlags.Import.FBX")
    mesh = unreal.load_asset(manifest["mesh"]["asset_path"])
    materials = mesh.get_editor_property("materials")
    for part in manifest["parts"]:
        slot = materials[second["slot_map"][part["id"]]]
        assert slot.get_editor_property("material_interface").get_path_name().split(".")[0] == part["material_path"]
    return {"success": True, "checks": ["wrong-project-before-mutation", "initial-import", "morphs",
            "independent-shared-material-slots", "same-job-reimport", "cvar-restored",
            "skeleton-change-before-mutation", "missing-placeholder-before-mutation", "actual-uv-count",
            "texture-compression-readback"], "report": second}


try:
    result = main()
except Exception:
    result = {"success": False, "traceback": traceback.format_exc()}
write_json(ROOT / "artifacts/ue_smoke/smoke_result.json", result)
print("NTE_BRIDGE_SMOKE=" + json.dumps(result, ensure_ascii=False))
if not result["success"]:
    raise RuntimeError(result["traceback"])
