import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "blender_addon"))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.packaging import _inside, _run, _unreal_command_line, cook_assets, file_sha256, package_job, stage_assets


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job = self.root / "job"
        self.job.mkdir()
        (self.job / "meshes").mkdir()
        (self.job / "meshes/mesh.fbx").write_bytes(b"fixture-fbx")
        (self.job / "textures").mkdir()
        (self.job / "textures/a.png").write_bytes(b"fixture-png")
        self.manifest_path = self.job / "manifest.json"
        self.ue_report = self.job / "ue_report.json"
        self.cooked = self.root / "cooked"
        self.manifest = {
            "schema_version": 1, "job_id": "job", "graph_id": "graph", "character_id": "character",
            "project_file": str(self.root / "HT.uproject"), "create_placeholders": True,
            "mesh": {"id": "mesh", "source_file": "meshes/mesh.fbx", "asset_path": "/Game/Characters/SK_Test",
                     "skeleton_path": "/Game/Shared/Skeleton", "physics_asset_path": "/Game/Shared/Physics",
                     "expected": {"bones": [{"name": "root", "parent": ""}], "shape_keys": ["Smile"], "uv_layers": 1}},
            "parts": [{"id": "p0", "slot_key": "NTE_p0", "source_slot": 0, "display_name": "body", "material_path": "/Game/Shared/M_Test"}],
            "textures": [{"id": "tex", "source_file": "textures/a.png", "asset_path": "/Game/Shared/T_Test", "role": "BASE_COLOR"}],
            "features": [], "export_assets": [
                {"asset_path": "/Game/Characters/SK_Test", "asset_type": "SkeletalMesh", "origin": "mod"},
                {"asset_path": "/Game/Shared/T_Test", "asset_type": "Texture2D", "origin": "mod"}],
        }
        self.content = self.root / "Content"
        for asset in self.manifest["export_assets"]:
            source = self.content / (asset["asset_path"][len("/Game/"):] + ".uasset")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes((asset["asset_path"] + "editor-source").encode())
        self.save()
        for asset in ("Characters/SK_Test", "Shared/T_Test", "Shared/M_Test", "Shared/Skeleton", "Shared/Physics", "Characters/OldAsset"):
            for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"):
                path = self.cooked / ("HT/Content/" + asset + extension)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((asset + extension).encode())

    def tearDown(self):
        self.temporary.cleanup()

    def save(self):
        write_json(self.manifest_path, self.manifest)
        self.report = {"schema_version": 1, "job_id": "job", "success": True, "errors": [],
                       "project_file": self.manifest["project_file"], "features_applied": not self.manifest["features"],
                       "manifest_sha256": file_sha256(self.manifest_path),
                       "source_sha256": {"meshes/mesh.fbx": file_sha256(self.job / "meshes/mesh.fbx"), "textures/a.png": file_sha256(self.job / "textures/a.png")},
                       "saved_asset_sha256": {str(path.relative_to(self.content)).replace("\\", "/"): file_sha256(path) for path in self.content.rglob("*") if path.is_file()},
                       "assets": [dict(asset, saved=True) for asset in self.manifest["export_assets"]]}
        write_json(self.ue_report, self.report)

    def stage(self):
        return stage_assets(self.manifest_path, self.ue_report, self.cooked)

    def test_whitelist_cross_folder_sidecars_and_fresh_staging(self):
        first = self.stage()
        self.assertEqual(len(first["files"]), 8)
        self.assertEqual({f["path"].split("/", 2)[0] for f in first["files"]}, {"HT"})
        self.assertFalse(any("M_Test" in f["path"] or "Skeleton" in f["path"] or "Physics" in f["path"] or "OldAsset" in f["path"] for f in first["files"]))
        (Path(first["source_dir"]) / "obsolete.uasset").write_bytes(b"old")
        second = self.stage()
        self.assertNotEqual(first["source_dir"], second["source_dir"])
        self.assertFalse((Path(second["source_dir"]) / "obsolete.uasset").exists())
        for file in second["files"]:
            self.assertEqual(file["sha256"], file_sha256(Path(second["source_dir"]) / file["path"]))

    def test_required_uasset_missing_despite_sidecars(self):
        (self.cooked / "HT/Content/Characters/SK_Test.uasset").unlink()
        with self.assertRaisesRegex(BridgeError, "Required cooked"):
            self.stage()

    def test_preview_texture_dependencies_are_excluded_from_staging(self):
        source = self.job / 'textures/preview.png'
        source.write_bytes(b'preview-only')
        preview_path = '/Game/NTEBridgePreview/T_Diffuse'
        self.manifest['textures'].append(dict(id='preview', source_file='textures/preview.png',
                                             asset_path=preview_path, role='BASE_COLOR', origin='preview'))
        self.manifest['material_previews'] = [dict(material_path='/Game/Shared/M_Test', texture_path=preview_path)]
        self.save()
        self.report['source_sha256']['textures/preview.png'] = file_sha256(source)
        self.report['assets'].append(dict(asset_path=preview_path, asset_type='Texture2D', origin='preview', saved=True))
        write_json(self.ue_report, self.report)
        for extension in ('.uasset', '.uexp', '.ubulk', '.uptnl'):
            path = self.cooked / ('HT/Content/NTEBridgePreview/T_Diffuse' + extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'cooked-preview-dependency')
        staged = self.stage()
        self.assertEqual(len(staged['files']), 8)
        self.assertFalse(any('NTEBridgePreview' in entry['path'] for entry in staged['files']))

    def test_zero_length_required_asset(self):
        (self.cooked / "HT/Content/Shared/T_Test.uasset").write_bytes(b"")
        with self.assertRaisesRegex(BridgeError, "Required cooked"):
            self.stage()

    def test_stale_manifest_job_and_project_reports(self):
        for key, bad in (("job_id", "older-job"), ("manifest_sha256", "0" * 64), ("project_file", str(self.root / "Other.uproject")), ("success", False)):
            self.save()
            self.report[key] = bad
            write_json(self.ue_report, self.report)
            with self.subTest(key=key), self.assertRaises(BridgeError):
                self.stage()
        self.save()
        self.manifest["character_id"] = "changed"
        write_json(self.manifest_path, self.manifest)
        with self.assertRaisesRegex(BridgeError, "stale"):
            self.stage()

    def test_unsaved_or_wrong_class_asset_rejected(self):
        for key, bad in (("saved", False), ("asset_type", "Material"), ("origin", "game_placeholder")):
            self.save()
            self.report["assets"][0][key] = bad
            write_json(self.ue_report, self.report)
            with self.subTest(key=key), self.assertRaisesRegex(BridgeError, "verified and saved"):
                self.stage()

    def test_changed_sources_rejected_without_manifest_change(self):
        for source in ("meshes/mesh.fbx", "textures/a.png"):
            self.save()
            with (self.job / source).open("ab") as file:
                file.write(b"changed")
            with self.subTest(source=source), self.assertRaisesRegex(BridgeError, "source FBX or textures"):
                self.stage()

    def test_another_job_reimporting_same_asset_invalidates_old_report(self):
        # Manifest and FBX remain unchanged; only another import replaces the
        # project's saved target at exactly the same /Game package path.
        target = self.content / "Characters/SK_Test.uasset"
        target.write_bytes(b"job B mesh replaces job A")
        with self.assertRaisesRegex(BridgeError, "saved project assets changed"):
            self.stage()
        report = package_job(self.manifest_path, self.ue_report, packager_tools_dir=self.root,
                             run_cook=False, cooked_root=self.cooked)
        self.assertFalse(report["success"])
        self.assertEqual(report["phase"], "validation")

    def test_added_or_missing_saved_asset_sidecar_invalidates_old_report(self):
        sidecar = self.content / "Characters/SK_Test.ubulk"
        sidecar.write_bytes(b"unexpected new saved bulk")
        with self.assertRaisesRegex(BridgeError, "saved project assets changed"):
            self.stage()
        self.save()
        sidecar.unlink()
        with self.assertRaisesRegex(BridgeError, "saved project assets changed"):
            self.stage()

    def test_pending_features_rejected_even_if_claimed_applied(self):
        self.manifest["features"] = [{"id": "toggle", "type": "visibility_cycle", "key": "K", "states": [{"id": "visible", "parts": ["p0"]}],
                                      "include_hidden_state": True, "initial_state_id": "visible"}]
        self.save()
        self.report["features_applied"] = True
        write_json(self.ue_report, self.report)
        with self.assertRaisesRegex(BridgeError, "runtime features"):
            self.stage()

    def test_forbidden_exports(self):
        original = copy.deepcopy(self.manifest)
        for kind in ("Material", "Skeleton", "PhysicsAsset", "MaterialInstanceConstant"):
            self.manifest = copy.deepcopy(original)
            self.manifest["export_assets"].append({"asset_path": "/Game/Shared/Forbidden", "asset_type": kind, "origin": "mod"})
            self.save()
            with self.subTest(kind=kind), self.assertRaises(BridgeError):
                self.stage()

    def test_traversal_and_nested_staging(self):
        for relative in ("../escape", "HT/../../escape", "C:/escape", "a:stream", "/absolute", "\\\\host\\share"):
            with self.subTest(relative=relative), self.assertRaises(BridgeError):
                _inside(self.cooked, relative)
        with self.assertRaisesRegex(BridgeError, "inside the cooked"):
            stage_assets(self.manifest_path, self.ue_report, self.cooked, self.cooked / "staging")

    @unittest.skipUnless(os.name == "nt", "UE raw argument parsing is Windows-specific")
    def test_unreal_quotes_values_after_equals_for_spaces(self):
        command = _unreal_command_line(["D:/UE/UnrealEditor-Cmd.exe", "D:/My Project/HT.uproject", "-run=Cook", "-OutputDir=D:/NTE bridge/cooked", "-unattended"])
        self.assertIn('-OutputDir="D:/NTE bridge/cooked"', command)
        self.assertIn('"D:/My Project/HT.uproject"', command)
        self.assertNotIn('"-OutputDir=', command)

    def test_failure_overwrites_old_success_report(self):
        destination = self.job / "package_report.json"
        write_json(destination, {"success": True, "outputs": ["old"]})
        self.report["success"] = False
        write_json(self.ue_report, self.report)
        result = package_job(self.manifest_path, self.ue_report, run_cook=False, cooked_root=self.cooked)
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "validation")
        self.assertEqual(json.loads(destination.read_text())["outputs"], [])

    def test_staging_failure_phase(self):
        (self.cooked / "HT/Content/Shared/T_Test.uasset").unlink()
        result = package_job(self.manifest_path, self.ue_report, packager_tools_dir=self.root,
                             run_cook=False, cooked_root=self.cooked)
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "staging")

    def test_cook_refuses_concurrent_editor_for_target_project(self):
        editor = self.root / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
        editor.parent.mkdir(parents=True)
        editor.write_bytes(b"never executed")
        Path(self.manifest["project_file"]).write_text("{}")
        with mock.patch("nte_bridge.unreal_transport._project_editor_running", return_value=True), mock.patch("nte_bridge.packaging._run") as run:
            with self.assertRaisesRegex(BridgeError, "Close the target Unreal project"):
                cook_assets(self.manifest, self.root, self.root / "new-cooked")
            run.assert_not_called()

    def test_three_file_report_verification_and_stale_report_rejection(self):
        adapter = self.root / "adapter.dll"
        adapter.write_bytes(b"mock adapter; never executed")
        for scenario in ("valid", "stale", "tampered"):
            def fake_runner(command, log_path, timeout, *, env=None):
                request = json.loads(Path(command[command.index("--job") + 1]).read_text())
                reply_path = Path(command[command.index("--report") + 1])
                output_dir = Path(request["output_dir"])
                output_dir.mkdir(parents=True)
                outputs = []
                for extension in (".pak", ".utoc", ".ucas"):
                    output = output_dir / (request["mod_name"] + extension)
                    output.write_bytes(("fixture " + extension).encode())
                    outputs.append({"path": str(output), "bytes": output.stat().st_size, "sha256": file_sha256(output)})
                reply = {key: request[key] for key in ("job_id", "manifest_sha256", "run_id")}
                reply.update(success=True, outputs=outputs)
                if scenario == "stale":
                    reply["run_id"] = "previous-run"
                if scenario == "tampered":
                    Path(outputs[0]["path"]).write_bytes(b"changed after verification")
                write_json(reply_path, reply)
            with self.subTest(scenario=scenario), mock.patch("nte_bridge.packaging._run", side_effect=fake_runner):
                report = package_job(self.manifest_path, self.ue_report, packager_tools_dir=self.root,
                                     run_cook=False, cooked_root=self.cooked, adapter_path=adapter)
            self.assertEqual(report["success"], scenario == "valid")
            self.assertEqual(report["phase"], "complete" if scenario == "valid" else "packager")
            if scenario == "valid":
                self.assertEqual(len(report["outputs"]), 3)
            else:
                self.assertEqual(report["outputs"], [])

    def test_packager_child_temporary_files_stay_in_job_cache(self):
        adapter = self.root / "adapter.dll"
        adapter.write_bytes(b"mock adapter; child below exercises temporary-file routing")
        # Stand in for the adapter while retaining the actual subprocess runner
        # and its output verification. The child uses its normal temp lookup.
        script = """
import hashlib, json, os, pathlib, sys, tempfile
request = json.loads(pathlib.Path(sys.argv[2]).read_text())
temporary = pathlib.Path(tempfile.mkdtemp(prefix='external-packager-'))
(temporary / 'staging-copy.uasset').write_bytes(b'child temporary asset')
print(json.dumps({'temporary': str(temporary), 'environment': {
    key: os.environ[key] for key in ('TEMP', 'TMP', 'TMPDIR')}}), flush=True)
destination = pathlib.Path(request['output_dir'])
destination.mkdir(parents=True)
outputs = []
for extension in ('.pak', '.utoc', '.ucas'):
    output = destination / (request['mod_name'] + extension)
    output.write_bytes(('fixture ' + extension).encode())
    outputs.append({'path': str(output), 'bytes': output.stat().st_size,
                    'sha256': hashlib.sha256(output.read_bytes()).hexdigest()})
reply = {key: request[key] for key in ('job_id', 'manifest_sha256', 'run_id')}
reply.update(success=True, outputs=outputs)
pathlib.Path(sys.argv[4]).write_text(json.dumps(reply))
"""

        def adapter_child(command, log_path, timeout, *, env=None):
            self.assertIsNotNone(env)
            _run([sys.executable, "-c", script] + command[command.index("--job"):],
                 log_path, timeout, env=env)

        original_environment = dict(os.environ)
        with mock.patch("nte_bridge.packaging._run", side_effect=adapter_child):
            report = package_job(self.manifest_path, self.ue_report, packager_tools_dir=self.root,
                                 run_cook=False, cooked_root=self.cooked, adapter_path=adapter)
        self.assertEqual(dict(os.environ), original_environment)
        self.assertTrue(report["success"], report["errors"])
        run_root = Path(report["run_dir"])
        self.assertTrue(run_root.is_relative_to(self.job))
        child = json.loads((run_root / "packager.log").read_text())
        self.assertTrue(Path(child["temporary"]).is_relative_to(run_root / "temp"))
        self.assertTrue((Path(child["temporary"]) / "staging-copy.uasset").is_file())
        self.assertEqual(set(child["environment"].values()), {str(run_root / "temp")})

    def test_adapter_rejects_unlisted_files_without_executing_tools(self):
        adapter = REPO / "artifacts/packager_cli/NteBridge.Packager.dll"
        if not adapter.exists():
            self.skipTest("Build the .NET adapter to run its inventory smoke test")
        staged = self.stage()
        (Path(staged["source_dir"]) / "unexpected.uasset").write_bytes(b"unexpected")
        request = dict(staged, output_dir=str(self.root / "output"), mod_name="Test_P", tools_dir=str(self.root / "missing-tools"))
        request_path, report_path = self.root / "request.json", self.root / "adapter-report.json"
        write_json(request_path, request)
        completed = subprocess.run(["dotnet", str(adapter), "--job", str(request_path), "--report", str(report_path)], capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 1)
        report = json.loads(report_path.read_text())
        self.assertFalse(report["success"])
        self.assertEqual(report["run_id"], staged["run_id"])
        self.assertIn("inventory", report["errors"][0])
        self.assertFalse((self.root / "output").exists())


if __name__ == "__main__":
    unittest.main()
