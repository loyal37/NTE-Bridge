import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import BridgeError, compile_graph, load_manifest, resolve_source, texture_settings, validate_manifest, write_json


def fixture():
    return {
        "schema_version": 1, "job_id": "job", "graph_id": "graph", "character_id": "character",
        "project_file": "D:/Fixture/Fixture.uproject", "create_placeholders": True,
        "mesh": {"id": "mesh", "asset_path": "/Game/Test/SK_Test", "source_file": "meshes/mesh.fbx",
                 "skeleton_path": "/Game/Test/Skeleton", "physics_asset_path": "",
                 "expected": {"bones": [{"name": "root", "parent": ""}, {"name": "child", "parent": "root"}],
                              "shape_keys": ["Smile"], "uv_layers": 4}},
        "parts": [{"id": f"p{i}", "slot_key": f"NTE_p{i}", "source_slot": i,
                   "display_name": f"part{i}", "material_path": "/Game/Shared/M_Same"} for i in range(5)],
        "textures": [], "features": [],
        "export_assets": [{"asset_path": "/Game/Test/SK_Test", "asset_type": "SkeletalMesh", "origin": "mod"}],
    }


def graph_fixture():
    nodes = [{"id": f"part{i}", "type": "PART", "part_id": f"p{i}"} for i in range(5)]
    nodes += [{"id": "a", "type": "GROUP", "label": "长袖"}, {"id": "b", "type": "GROUP", "label": "短袖"},
              {"id": "cycle", "type": "CYCLE", "state_ids": ["state-a", "state-b"], "key": "K", "initial_state_id": "state-b"},
              {"id": "output", "type": "OUTPUT"}]
    links = [{"from_node": f"part{i}", "to_node": "a" if i < 2 else "b", "to_socket": str(i)} for i in range(5)]
    links += [{"from_node": "a", "to_node": "cycle", "to_socket": "state-a"},
              {"from_node": "b", "to_node": "cycle", "to_socket": "state-b"},
              {"from_node": "cycle", "to_node": "output", "to_socket": "feature"}]
    return {"id": "graph", "nodes": nodes, "links": links}


class ContractTests(unittest.TestCase):
    def test_shared_material_slots_survive(self):
        manifest = fixture()
        self.assertIs(validate_manifest(manifest), manifest)
        self.assertEqual(len(manifest["parts"]), 5)

    def test_unequal_group_cycle_and_stable_order(self):
        graph = graph_fixture()
        features = compile_graph(graph, fixture()["parts"])
        self.assertEqual([s["parts"] for s in features[0]["states"]], [["p0", "p1"], ["p2", "p3", "p4"]])
        self.assertFalse(features[0]["include_hidden_state"])
        graph["nodes"][7]["state_ids"].reverse()
        changed = compile_graph(graph, fixture()["parts"])[0]
        self.assertEqual(changed["initial_state_id"], "state-b")
        self.assertEqual(changed["states"][0]["parts"], ["p2", "p3", "p4"])

    def test_explicit_hidden_default(self):
        graph = graph_fixture()
        graph["nodes"][7].update(include_hidden=True, initial_state_id="$hidden")
        self.assertEqual(compile_graph(graph, fixture()["parts"])[0]["initial_state_id"], "$hidden")
        graph["nodes"][7]["include_hidden"] = False
        with self.assertRaisesRegex(BridgeError, "初始状态"):
            compile_graph(graph, fixture()["parts"])

    def test_missing_input_and_duplicate_members_fail(self):
        graph = graph_fixture()
        graph["links"] = [link for link in graph["links"] if link["to_socket"] != "state-b"]
        with self.assertRaisesRegex(BridgeError, "每个状态"):
            compile_graph(graph, fixture()["parts"])
        graph = graph_fixture()
        graph["links"].append({"from_node": "part0", "to_node": "b", "to_socket": "extra"})
        with self.assertRaisesRegex(BridgeError, "重复"):
            compile_graph(graph, fixture()["parts"])

    def test_disconnected_dependency_cycle_rejected(self):
        graph = graph_fixture()
        graph["nodes"] += [{"id": "x", "type": "GROUP"}, {"id": "y", "type": "GROUP"}]
        graph["links"] += [{"from_node": "x", "to_node": "y", "to_socket": "0"}, {"from_node": "y", "to_node": "x", "to_socket": "0"}]
        with self.assertRaisesRegex(BridgeError, "循环依赖"):
            compile_graph(graph, fixture()["parts"])

    def test_source_path_escape_rejected(self):
        for path in ("../bad.fbx", "a/../../bad.fbx", "C:/bad.fbx", "C:bad.fbx", "//host/share/file", "a:stream", "\\\\host\\share\\file"):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                resolve_source(tempfile.gettempdir(), path)

    def test_forbidden_exports_and_colliding_paths(self):
        for kind in ("Material", "Skeleton", "PhysicsAsset"):
            manifest = fixture()
            manifest["export_assets"].append({"asset_path": "/Game/Shared/M_Same", "asset_type": kind, "origin": "mod"})
            with self.subTest(kind=kind), self.assertRaises(BridgeError):
                validate_manifest(manifest)
        manifest = fixture()
        manifest["textures"] = [{"id": "tex", "source_file": "textures/a.png", "asset_path": "/Game/Shared/M_Same", "role": "BASE_COLOR"}]
        with self.assertRaisesRegex(BridgeError, "冲突"):
            validate_manifest(manifest)

    def test_texture_recipes(self):
        self.assertEqual(texture_settings("BASE_COLOR"), {"compression": "BC7", "srgb": True})
        self.assertEqual(texture_settings("ID_TEX"), {"compression": "BC7", "srgb": False})
        self.assertEqual(texture_settings("LIGHT_MAP"), {"compression": "BC7", "srgb": False})
        self.assertEqual(texture_settings("NORMAL"), {"compression": "NORMALMAP", "srgb": False})

    def test_preview_texture_imported_but_never_exported(self):
        manifest = fixture()
        texture = {"id": "preview", "source_file": "textures/preview.png",
                   "asset_path": "/Game/NTEBridgePreview/T_Diffuse", "role": "BASE_COLOR", "origin": "preview"}
        manifest["textures"] = [texture]
        manifest["material_previews"] = [{"material_path": "/Game/Shared/M_Same",
                                           "texture_path": texture["asset_path"]}]
        self.assertIs(validate_manifest(manifest), manifest)
        manifest["export_assets"].append({"asset_path": texture["asset_path"], "asset_type": "Texture2D", "origin": "mod"})
        with self.assertRaisesRegex(BridgeError, "预览贴图不打包"):
            validate_manifest(manifest)
        texture["origin"] = "mod"
        self.assertIs(validate_manifest(manifest), manifest)
        del texture["origin"]  # Older explicit texture manifests remain compatible.
        self.assertIs(validate_manifest(manifest), manifest)

    def test_preview_bindings_require_declared_diffuse_and_unique_referenced_material(self):
        base = fixture()
        base["textures"] = [{"id": "preview", "source_file": "textures/p.png",
                             "asset_path": "/Game/Preview/T_P", "role": "BASE_COLOR", "origin": "preview"}]
        binding = {"material_path": "/Game/Shared/M_Same", "texture_path": "/Game/Preview/T_P"}
        base["material_previews"] = [binding]
        for field, value in (("material_path", "/Game/Unrelated/M_Other"),
                             ("texture_path", "/Game/Preview/Missing")):
            manifest = copy.deepcopy(base)
            manifest["material_previews"][0][field] = value
            with self.subTest(field=field), self.assertRaises(BridgeError):
                validate_manifest(manifest)
        manifest = copy.deepcopy(base)
        manifest["material_previews"].append(dict(binding))
        with self.assertRaisesRegex(BridgeError, "重复"):
            validate_manifest(manifest)
        for field, value in (("origin", "unknown"), ("role", "NORMAL")):
            manifest = copy.deepcopy(base)
            manifest["textures"][0][field] = value
            with self.subTest(field=field), self.assertRaises(BridgeError):
                validate_manifest(manifest)

    def test_round_trip_checks_real_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = fixture()
            write_json(root / "manifest.json", job)
            with self.assertRaisesRegex(BridgeError, "不存在"):
                load_manifest(root / "manifest.json")
            (root / "meshes").mkdir()
            (root / "meshes/mesh.fbx").write_bytes(b"fixture")
            self.assertEqual(load_manifest(root / "manifest.json"), job)

    def test_bone_cycle_and_discontinuous_slots(self):
        job = fixture()
        job["mesh"]["expected"]["bones"][0]["parent"] = "child"
        with self.assertRaisesRegex(BridgeError, "层级存在环"):
            validate_manifest(job)
        job = fixture()
        job["parts"][3]["source_slot"] = 8
        with self.assertRaisesRegex(BridgeError, "连续"):
            validate_manifest(job)


if __name__ == "__main__":
    unittest.main()
