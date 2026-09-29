import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.unreal_transport import matching_nodes, validate_report, commandlet_command, _transport_status
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
            self.assertNotIn("\\", script)
            self.assertTrue(Path(script).is_file())
            self.assertFalse(report.exists())
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


if __name__ == "__main__":
    unittest.main()
