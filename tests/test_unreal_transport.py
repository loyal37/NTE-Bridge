import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.unreal_transport import (matching_nodes, validate_report, commandlet_command,
                                         run_commandlet_job, _transport_status, _finish)
from nte_bridge.unreal_receiver import _check_bones, run_job, _saved_asset_hashes
from test_core import fixture


class TransportTests(unittest.TestCase):
    def test_project_matching_does_not_choose_first_or_same_short_name(self):
        target = "D:/Project/Test.uproject"
        nodes = [{"node_id": "other", "project_root": "D:/Wrong", "project_name": "Test"},
                 {"node_id": "right", "project_root": "D:/Project", "project_name": "Test"},
                 {"node_id": "unknown", "project_name": "Test"}]
        self.assertEqual([n["node_id"] for n in matching_nodes(nodes, target)], ["right"])

    def test_report_rejects_other_job_project_and_changed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = fixture()
            path = Path(directory) / "manifest.json"
            write_json(path, manifest)
            report = {"schema_version": 1, "job_id": manifest["job_id"], "project_file": manifest["project_file"],
                      "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "errors": [], "assets": []}
            self.assertIs(validate_report(report, path, manifest), report)
            for key, value in (("job_id", "other"), ("project_file", "D:/Other/Test.uproject"), ("manifest_sha256", "stale")):
                changed = dict(report, **{key: value})
                with self.subTest(key=key), self.assertRaises(BridgeError):
                    validate_report(changed, path, manifest)

    def test_hierarchy_rejects_new_missing_and_reparented_bones(self):
        skeleton = [{"name": "root", "parent": ""}, {"name": "child", "parent": "root"}]
        _check_bones(skeleton, list(reversed(skeleton)))
        for altered in (skeleton[:1], skeleton + [{"name": "new", "parent": "root"}],
                        [{"name": "root", "parent": "child"}, {"name": "child", "parent": ""}]):
            with self.subTest(altered=altered), self.assertRaises(BridgeError):
                _check_bones(skeleton, altered)

    def test_failed_invocation_overwrites_stale_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manifest.json"
            report = source.with_name("ue_report.json")
            source.write_text("{}", encoding="utf-8")
            write_json(report, {"success": True, "job_id": "stale"})
            result = run_job(source)
            self.assertFalse(result["success"])
            self.assertTrue(result["errors"])
            self.assertFalse(result["partial_changes"])
            self.assertNotIn("stale", report.read_text(encoding="utf-8"))

    def test_new_transport_attempt_invalidates_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manifest.json"
            manifest = fixture()
            write_json(source, manifest)
            write_json(source.with_name("ue_report.json"), {"success": True})
            _transport_status(source, manifest, "Connecting")
            import json
            result = json.loads(source.with_name("ue_report.json").read_text(encoding="utf-8"))
            self.assertFalse(result["success"])
            self.assertEqual(result["job_id"], manifest["job_id"])
            self.assertEqual(result["stage"], "transport")

    def test_commandlet_helper_preserves_space_paths_without_backslash_escapes(self):
        with tempfile.TemporaryDirectory(prefix="NTE space ") as directory:
            root = Path(directory)
            source = root / "manifest.json"
            manifest = fixture()
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"test")
            executable = root / "UE/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"test")
            write_json(source, manifest)
            argv, report = commandlet_command(source, root / "UE")
            script = next(arg for arg in argv if arg.startswith("-script="))[8:]
            self.assertIn("NTE space ", script)
            self.assertIn('-NODEFAULTLOG', argv)
            self.assertNotIn("\\", script)
            self.assertTrue(Path(script).is_file())
            first = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(first['stage'], 'pending')
            second_argv, second_report = commandlet_command(source, root / 'UE')
            self.assertEqual(argv, second_argv)
            self.assertEqual(report, second_report)
            second = json.loads(report.read_text(encoding='utf-8'))
            self.assertNotEqual(first['invocation_id'], second['invocation_id'])
            with self.assertRaisesRegex(BridgeError, '过期'):
                _finish(source, report, manifest, first['invocation_id'])
            self.assertEqual(len(list(root.glob('ue_run*.py'))), 1)
            self.assertEqual(len(list(root.glob('ue_invocation*.json'))), 1)
            self.assertIn("del sys.modules[_nte_name]", Path(script).read_text(encoding="utf-8"))

    def test_saved_asset_identity_changes_when_another_job_overwrites_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = fixture()
            manifest["project_file"] = str(root / "Test.uproject")
            asset = root / "Content/Test/SK_Test.uasset"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"job-a")
            before = _saved_asset_hashes(manifest)
            asset.write_bytes(b"job-b")
            self.assertNotEqual(before, _saved_asset_hashes(manifest))
            asset.with_suffix(".ubulk").write_bytes(b"bulk")
            self.assertEqual(set(_saved_asset_hashes(manifest)), {"Test/SK_Test.uasset", "Test/SK_Test.ubulk"})

    def test_commandlet_enables_dependencies_without_changing_project(self):
        required = {"PythonScriptPlugin", "EditorScriptingUtilities", "GeometryScripting", "ControlRig"}
        with tempfile.TemporaryDirectory(prefix="NTE dependencies ") as directory:
            root = Path(directory)
            source = root / "manifest.json"
            project = root / "Test.uproject"
            manifest = fixture()
            manifest["project_file"] = str(project)
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"test")
            executable = root / "UE/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"test")
            write_json(source, manifest)
            for mode in ("omitted", "disabled"):
                with self.subTest(mode=mode):
                    descriptor = {"FileVersion": 3, "EngineAssociation": "5.6",
                                  "Plugins": [{"Name": "UserPlugin", "Enabled": True}]}
                    if mode == "disabled":
                        descriptor["Plugins"] += [{"Name": name, "Enabled": False}
                                                  for name in sorted(required)]
                    before = json.dumps(descriptor, ensure_ascii=False, indent=3).encode("utf-8")
                    project.write_bytes(before)
                    argv, _ = commandlet_command(source, root / "UE")
                    flags = [arg for arg in argv if arg.startswith("-EnablePlugins=")]
                    self.assertEqual(len(flags), 1)
                    self.assertEqual(set(flags[0].split("=", 1)[1].split(",")), required)
                    self.assertEqual(project.read_bytes(), before)

    def test_missing_editor_apis_fail_before_asset_access_with_restart_guidance(self):
        dependencies = {"EditorAssetLibrary": "EditorScriptingUtilities",
                        "GeometryScript_AssetUtils": "GeometryScripting",
                        "GeometryScript_MeshQueries": "GeometryScripting",
                        "DynamicMesh": "GeometryScripting",
                        "RigHierarchy": "ControlRig"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            manifest = fixture()
            manifest["project_file"] = str(root / "Test.uproject")
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"test")
            write_json(source, manifest)
            for missing in [(name,) for name in dependencies] + [tuple(dependencies)]:
                with self.subTest(missing=missing):
                    unreal = SimpleNamespace(
                        Paths=SimpleNamespace(convert_relative_path_to_full=lambda path: path,
                                              get_project_file_path=lambda: manifest["project_file"]),
                        SystemLibrary=SimpleNamespace(get_engine_version=lambda: "5.6.1-test"),
                        **{name: object() for name in dependencies if name not in missing})
                    with mock.patch.dict(sys.modules, {"unreal": unreal}), \
                            mock.patch("nte_bridge.unreal_receiver._asset") as asset, \
                            mock.patch("nte_bridge.unreal_receiver._task") as task:
                        report = run_job(source)
                    asset.assert_not_called()
                    task.assert_not_called()
                    self.assertFalse(report["success"])
                    self.assertFalse(report["mutation_started"])
                    self.assertFalse(report["partial_changes"])
                    self.assertEqual(report["assets"], [])
                    self.assertEqual(len(report["errors"]), 1)
                    error = report["errors"][0]
                    self.assertIn("后台导入", error)
                    for name in missing:
                        self.assertIn(dependencies[name], error)

    def test_offline_sync_refuses_open_project_before_process_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            manifest = fixture()
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"test")
            write_json(source, manifest)
            write_json(source.with_name("ue_report.json"), {"success": True})
            with mock.patch("nte_bridge.unreal_transport._project_editor_running", return_value=True), \
                    mock.patch("nte_bridge.unreal_transport.commandlet_command") as command, \
                    mock.patch("nte_bridge.unreal_transport.subprocess.run") as launch:
                with self.assertRaisesRegex(BridgeError, "关闭"):
                    run_commandlet_job(source)
            command.assert_not_called()
            launch.assert_not_called()
            report = json.loads(source.with_name("ue_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["success"])
            self.assertEqual(report["stage"], "transport")

    def test_commandlet_without_report_replaces_stale_success_with_failure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            manifest = fixture()
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"test")
            write_json(source, manifest)
            write_json(source.with_name("ue_report.json"), {"success": True, "job_id": "old"})
            command = (["UnrealEditor-Cmd.exe", "-run=pythonscript"], root / "missing-report.json")
            with mock.patch("nte_bridge.unreal_transport._project_editor_running", return_value=False), \
                    mock.patch("nte_bridge.unreal_transport.commandlet_command", return_value=command), \
                    mock.patch("nte_bridge.unreal_transport.subprocess.run",
                               return_value=SimpleNamespace(returncode=1)) as launch:
                with self.assertRaisesRegex(BridgeError, "退出码 1"):
                    run_commandlet_job(source)
            launch.assert_called_once()
            report = json.loads(source.with_name("ue_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["success"])
            self.assertEqual(report["job_id"], manifest["job_id"])
            self.assertEqual(report["stage"], "transport")
            self.assertIn("退出码 1", report["errors"][0])
            self.assertIn(str(root / "ue_commandlet.log"), report["errors"][0])


if __name__ == "__main__":
    unittest.main()
