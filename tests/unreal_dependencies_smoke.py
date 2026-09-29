"""Import a copied job into an isolated project with bridge plugins explicitly disabled.

Run with normal Python, not inside Unreal. The supplied job and its target project are
read-only inputs; every UE write is directed into a unique artifacts/dependency_smoke run.
No game fixture is stored in source control.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import load_manifest, resolve_source, texture_settings, write_json
from nte_bridge.unreal_transport import DEFAULT_ENGINE, run_commandlet_job


# Keep the expected list independent of the transport implementation under test.
REQUIRED_PLUGINS = (
    "PythonScriptPlugin", "EditorScriptingUtilities", "GeometryScripting", "ControlRig",
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(source, engine_dir, timeout):
    source = Path(source).resolve()
    manifest = load_manifest(source)
    assert manifest.get("create_placeholders"), (
        "The isolated empty project requires the source job's create_placeholders=true; "
        "this test never changes that policy or copies the user's UE assets")
    source_files = {manifest["mesh"]["source_file"]}
    source_files.update(item["source_file"] for item in manifest.get("textures", []))
    originals = {source, *(resolve_source(source.parent, name) for name in source_files)}
    original_project = Path(manifest["project_file"]).resolve()
    if original_project.is_file():
        originals.add(original_project)
    original_hashes = {str(path): sha256(path) for path in sorted(originals)}

    parent = ROOT / "artifacts/dependency_smoke"
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="run-", dir=parent)).resolve()
    assert folder.is_relative_to(parent.resolve())
    project = folder / ("NTEBridgeDependencies_" + uuid.uuid4().hex + ".uproject")
    write_json(project, {
        "FileVersion": 3, "EngineAssociation": "5.6",
        "Plugins": [{"Name": name, "Enabled": False} for name in REQUIRED_PLUGINS],
    })
    project_bytes = project.read_bytes()
    config = folder / "Config/DefaultEngine.ini"
    config.parent.mkdir()
    config.write_text(
        "[/Script/EngineSettings.GameMapsSettings]\n"
        "EditorStartupMap=/Engine/Maps/Entry\nGameDefaultMap=/Engine/Maps/Entry\n\n"
        "[/Script/UnrealEd.EditorLoadingSavingSettings]\nbAutoSaveEnable=False\n",
        encoding="utf-8")
    job_dir = folder / "job"
    job_dir.mkdir()
    for name in sorted(source_files):
        destination = resolve_source(job_dir, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolve_source(source.parent, name), destination)
        assert sha256(destination) == original_hashes[str(resolve_source(source.parent, name))]
    copied = copy.deepcopy(manifest)
    copied["job_id"] = str(uuid.uuid4())
    copied["project_file"] = str(project)
    assert {key for key in manifest if manifest[key] != copied[key]} == {"job_id", "project_file"}
    manifest_path = job_dir / "manifest.json"
    write_json(manifest_path, copied)
    result = {
        "success": False, "source_manifest": str(source), "test_manifest": str(manifest_path),
        "project_file": str(project), "original_sha256_before": original_hashes,
        "project_sha256_before": hashlib.sha256(project_bytes).hexdigest(), "checks": [],
    }
    print("NTE_DEPENDENCY_SMOKE_PROJECT=" + str(project), flush=True)
    try:
        report = run_commandlet_job(manifest_path, engine_dir, timeout=timeout)
        result["report"] = report
        log = manifest_path.with_name("ue_commandlet.log").read_text(encoding="utf-8", errors="replace")
        mounted = [name for name in REQUIRED_PLUGINS
                   if re.search(r"Mounting Engine plugin " + re.escape(name) + r"\b", log)]
        result["mounted_plugins"] = mounted
        assert set(mounted) == set(REQUIRED_PLUGINS), "Required plugin mounting is absent from the UE log"
        result["checks"].append("all-four-explicitly-disabled-plugins-loaded")
        assert report["success"], report.get("errors")
        assert not report["errors"] and not report["partial_changes"], report
        assert Path(report["project_file"]).resolve() == project
        assert report["morph_targets"] == sorted(copied["mesh"]["expected"]["shape_keys"])
        assert report["bone_count"] == len(copied["mesh"]["expected"]["bones"])
        assert report["mesh_geometry"]["uv_layers"] == copied["mesh"]["expected"]["uv_layers"]
        assert set(report["slot_map"]) == {part["id"] for part in copied["parts"]}
        assert len(set(report["slot_map"].values())) == len(copied["parts"])
        assert report["features_applied"] == (not bool(copied.get("features")))
        result["checks"].extend(("exact-project", "morphs", "bones", "actual-uv-count", "independent-slots"))
        expected_assets = {copied["mesh"]["asset_path"], copied["mesh"]["skeleton_path"]}
        if copied["mesh"].get("physics_asset_path"):
            expected_assets.add(copied["mesh"]["physics_asset_path"])
        expected_assets.update(part["material_path"] for part in copied["parts"])
        expected_assets.update(item["asset_path"] for item in copied.get("textures", []))
        assert {item["asset_path"] for item in report["assets"]} == expected_assets
        for asset in report["assets"]:
            assert asset["saved"], asset
            path = folder / "Content" / (asset["asset_path"].removeprefix("/Game/") + ".uasset")
            assert path.is_file() and path.stat().st_size > 0, path
        result["checks"].append("exact-asset-paths-saved")
        for texture in copied.get("textures", []):
            settings = dict(texture_settings(texture["role"]), role=texture["role"])
            assert report["texture_settings"][texture["asset_path"]] == settings
        result["texture_count"] = len(copied.get("textures", []))
        if result["texture_count"]:
            result["checks"].append("texture-settings-readback")
        result["success"] = True
    except Exception:
        result["traceback"] = traceback.format_exc()
    finally:
        after = {name: sha256(name) for name in original_hashes}
        result["original_sha256_after"] = after
        result["original_sources_unchanged"] = after == original_hashes
        result["project_sha256_after"] = sha256(project)
        result["project_bytes_unchanged"] = project.read_bytes() == project_bytes
        result["copied_sources_unchanged"] = all(
            sha256(resolve_source(job_dir, name)) == original_hashes[str(resolve_source(source.parent, name))]
            for name in source_files)
        unchanged = all(result[name] for name in (
            "original_sources_unchanged", "project_bytes_unchanged", "copied_sources_unchanged"))
        if unchanged:
            result["checks"].append("project-and-original-source-bytes-unchanged")
        result["success"] = result["success"] and unchanged
        write_json(folder / "result.json", result)
        print("NTE_DEPENDENCY_SMOKE_RESULT=" + str(folder / "result.json"), flush=True)
        print(json.dumps({key: result[key] for key in ("success", "checks")}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--engine-dir", default=DEFAULT_ENGINE)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    result = run(args.manifest, args.engine_dir, args.timeout)
    if not result["success"]:
        print(result.get("traceback", "Input or project bytes changed"), file=sys.stderr)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
