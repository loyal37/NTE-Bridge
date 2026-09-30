import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import (BridgeError, compile_blueprint, ht_material_ids, key_identity, load_manifest,
                             resolve_source, texture_settings, validate_manifest, write_json)


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


def blueprint_fixture():
    nodes = [
        {"id": "main", "type": "OBJECT", "label": "body", "object_id": "Body", "main": True,
         "parts": ["p0", "p1", "p2"], "split": ["p1"]},
        {"id": "coat", "type": "OBJECT", "label": "coat", "object_id": "Coat", "parts": ["c0"]},
        {"id": "belt", "type": "OBJECT", "label": "belt", "object_id": "Belt", "parts": ["b0", "b1"]},
        {"id": "unused", "type": "OBJECT", "label": "ref", "object_id": "Reference", "parts": ["r0"]},
        {"id": "mat", "type": "MATERIAL", "label": "MI_coat",
         "params": [{"id": "param_base", "name": "BaseColor"}, {"id": "param_mask", "name": "SkilMask"}]},
        {"id": "tex", "type": "TEXTURE", "label": "coat_d"},
        {"id": "loose", "type": "TEXTURE", "label": "unused"},
        {"id": "grp", "type": "GROUP", "label": "长袖"},
        {"id": "sw", "type": "SWITCH", "label": "外套", "comment": "外套", "key": "alt 6", "variable": "swapkey0",
         "options": ["option_0", "option_1", "option_2"], "initial_option": 0},
        {"id": "out", "type": "OUTPUT"}]
    links = [
        {"from_node": "main", "from_socket": "all", "to_node": "out", "to_socket": "in_1"},
        {"from_node": "main", "from_socket": "p1", "to_node": "grp", "to_socket": "in_1"},
        {"from_node": "belt", "from_socket": "all", "to_node": "grp", "to_socket": "in_2"},
        {"from_node": "grp", "from_socket": "out", "to_node": "sw", "to_socket": "option_0"},
        {"from_node": "coat", "from_socket": "all", "to_node": "sw", "to_socket": "option_2"},
        {"from_node": "sw", "from_socket": "out", "to_node": "out", "to_socket": "in_2"},
        {"from_node": "mat", "from_socket": "material", "to_node": "coat", "to_socket": "c0"},
        {"from_node": "tex", "from_socket": "texture", "to_node": "mat", "to_socket": "param_base"}]
    return {"id": "graph", "nodes": nodes, "links": links}


def node(graph, node_id):
    return next(item for item in graph["nodes"] if item["id"] == node_id)


class ContractTests(unittest.TestCase):
    def test_shared_material_slots_survive(self):
        manifest = fixture()
        self.assertIs(validate_manifest(manifest), manifest)
        self.assertEqual(len(manifest["parts"]), 5)

    def test_blueprint_static_switch_empty_option_and_material(self):
        plan = compile_blueprint(blueprint_fixture())
        self.assertEqual(plan["main"], "main")
        self.assertEqual(plan["objects"], {"main": ["p0", "p1", "p2"], "belt": ["b0", "b1"], "coat": ["c0"]})
        self.assertEqual(plan["part_materials"]["c0"], "mat")
        self.assertIsNone(plan["part_materials"]["p0"])
        self.assertEqual(list(plan["materials"]), ["mat"])
        self.assertEqual(plan["material_textures"], {"mat": {"param_base": "tex"}})
        feature = plan["features"][0]
        self.assertEqual([state["parts"] for state in feature["states"]], [["p1", "b0", "b1"], [], ["c0"]])
        self.assertEqual(feature["initial_state_id"], "sw/option_0")
        self.assertEqual((feature["key"], feature["variable"], feature["label"]), ("alt 6", "swapkey0", "外套"))

    def test_ht_material_ids_rotate_single_empty_option(self):
        feature = compile_blueprint(blueprint_fixture())["features"][0]
        slots = {"p0": 0, "p1": 1, "p2": 2, "c0": 3, "b0": 4, "b1": 5}
        self.assertEqual(ht_material_ids(feature, slots), ("3,1+4+5", 1))
        self.assertEqual(ht_material_ids(feature, {"p1": 1})[0], None)
        both = dict(feature, states=[feature["states"][0], feature["states"][2]], initial_state_id="sw/option_2")
        self.assertEqual(ht_material_ids(both, slots), ("1+4+5;3", 1))
        single = dict(feature, states=[feature["states"][1], feature["states"][2]], initial_state_id="sw/option_1")
        self.assertEqual(ht_material_ids(single, slots), ("3", 1))
        double = dict(feature, states=feature["states"] + [{"id": "extra", "parts": []}])
        self.assertEqual(ht_material_ids(double, slots), (None, "HT 工具只支持一个空选项"))
        legacy = dict(both, include_hidden_state=True, initial_state_id="$hidden")
        self.assertEqual(ht_material_ids(legacy, slots), ("1+4+5,3", 2))

    def test_duplicate_placement_and_nested_switch_rejected(self):
        graph = blueprint_fixture()
        graph["links"].append({"from_node": "coat", "from_socket": "all", "to_node": "grp", "to_socket": "in_3"})
        with self.assertRaisesRegex(BridgeError, "同时出现"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["nodes"].append({"id": "inner", "type": "SWITCH", "key": "K", "options": ["option_0", "option_1"]})
        graph["links"] = [link for link in graph["links"] if link["from_node"] != "coat"]
        graph["links"] += [{"from_node": "coat", "from_socket": "all", "to_node": "inner", "to_socket": "option_0"},
                           {"from_node": "inner", "from_socket": "out", "to_node": "sw", "to_socket": "option_2"}]
        with self.assertRaisesRegex(BridgeError, "不能放在另一个切换"):
            compile_blueprint(graph)

    def test_split_main_and_link_types(self):
        graph = blueprint_fixture()
        graph["links"] = [link for link in graph["links"] if link["from_socket"] != "p1"]
        with self.assertRaisesRegex(BridgeError, "拆出的槽尚未连接"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["links"].append({"from_node": "mat", "from_socket": "material", "to_node": "grp", "to_socket": "in_3"})
        with self.assertRaisesRegex(BridgeError, "材质入口"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["links"] = [link for link in graph["links"] if link["from_node"] != "main" or link["to_node"] != "out"]
        graph["links"] = [link for link in graph["links"] if link["from_socket"] != "p1"]
        node(graph, "main")["split"] = []
        with self.assertRaisesRegex(BridgeError, "主网格"):
            compile_blueprint(graph)

    def test_texture_links_only_feed_material_parameters(self):
        graph = blueprint_fixture()
        graph["links"].append({"from_node": "loose", "from_socket": "texture", "to_node": "belt", "to_socket": "b0"})
        with self.assertRaisesRegex(BridgeError, "只能连接材质实例的参数入口"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["links"].append({"from_node": "loose", "from_socket": "texture", "to_node": "mat", "to_socket": "param_gone"})
        with self.assertRaisesRegex(BridgeError, "只能连接材质实例的参数入口"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["links"].append({"from_node": "belt", "from_socket": "all", "to_node": "mat", "to_socket": "param_mask"})
        with self.assertRaisesRegex(BridgeError, "只能连接贴图节点"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        node(graph, "mat")["params"] = []
        with self.assertRaisesRegex(BridgeError, "参数入口"):
            compile_blueprint(graph)

    def test_switch_requires_content_and_unique_controls(self):
        graph = blueprint_fixture()
        graph["links"] = [link for link in graph["links"] if link["to_node"] != "sw"]
        graph["links"].append({"from_node": "belt", "from_socket": "all", "to_node": "out", "to_socket": "in_3"})
        graph["links"] = [link for link in graph["links"] if link["from_socket"] != "p1"]
        node(graph, "main")["split"] = []
        with self.assertRaisesRegex(BridgeError, "所有选项都是空的"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["nodes"].append({"id": "sw2", "type": "SWITCH", "key": "Alt+6", "variable": "swapkey1",
                               "options": ["option_0", "option_1"]})
        graph["nodes"].append({"id": "extra", "type": "OBJECT", "object_id": "Extra", "parts": ["e0"]})
        graph["links"] += [{"from_node": "extra", "from_socket": "all", "to_node": "sw2", "to_socket": "option_0"},
                           {"from_node": "sw2", "from_socket": "out", "to_node": "out", "to_socket": "in_3"}]
        with self.assertRaisesRegex(BridgeError, "按键"):
            compile_blueprint(graph)
        node(graph, "sw2")["key"] = "K"
        node(graph, "sw2")["variable"] = "SwapKey0"
        with self.assertRaisesRegex(BridgeError, "变量名"):
            compile_blueprint(graph)
        self.assertEqual(key_identity("Alt + 6"), key_identity("alt 6"))

    def test_loops_and_unknown_sockets_rejected(self):
        graph = blueprint_fixture()
        graph["nodes"] += [{"id": "x", "type": "GROUP"}, {"id": "y", "type": "GROUP"}]
        graph["links"] += [{"from_node": "x", "from_socket": "out", "to_node": "y", "to_socket": "in_1"},
                           {"from_node": "y", "from_socket": "out", "to_node": "x", "to_socket": "in_1"}]
        with self.assertRaisesRegex(BridgeError, "循环"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        graph["links"][4]["to_socket"] = "option_9"
        with self.assertRaisesRegex(BridgeError, "选项插口已失效"):
            compile_blueprint(graph)
        graph = blueprint_fixture()
        node(graph, "belt")["parts"] = ["c0"]
        with self.assertRaisesRegex(BridgeError, "同一个物体"):
            compile_blueprint(graph)

    def test_new_and_existing_material_instances_in_manifest(self):
        manifest = fixture()
        manifest["parts"][0]["material_path"] = "/Game/Test/Mod/MI_New"
        manifest["parts"][1]["material_path"] = "/Game/Test/MI_Existing"
        manifest["textures"] = [{"id": "d", "source_file": "textures/d.png", "asset_path": "/Game/Test/Mod/cloth_d",
                                 "role": "BASE_COLOR", "origin": "mod"},
                                {"id": "m", "source_file": "textures/m.png", "asset_path": "/Game/Test/Mod/cloth_mask",
                                 "role": "MASK", "origin": "mod"}]
        manifest["materials"] = [{"id": "n", "kind": "new", "asset_path": "/Game/Test/Mod/MI_New",
                                  "parent_path": "/Game/Other/MI_Mother",
                                  "textures": {"BaseColor": "/Game/Test/Mod/cloth_d", "SkilMask": "/Game/Test/Mod/cloth_mask"}},
                                 {"id": "e", "kind": "existing", "asset_path": "/Game/Test/MI_Existing"}]
        manifest["export_assets"] += [{"asset_path": t["asset_path"], "asset_type": "Texture2D", "origin": "mod"}
                                      for t in manifest["textures"]]
        with self.assertRaisesRegex(BridgeError, "新建材质实例"):
            validate_manifest(manifest)
        manifest["export_assets"].append({"asset_path": "/Game/Test/Mod/MI_New",
                                          "asset_type": "MaterialInstanceConstant", "origin": "mod"})
        self.assertIs(validate_manifest(manifest), manifest)
        for mutate, message in [
                (lambda m: m["materials"][0]["textures"].update(NomralMap="/Game/Test/Mod/missing"), "贴图未在任务中声明"),
                (lambda m: m["materials"][0].update(parent_path="/Game/Test/Mod/MI_New"), "母材质不能是自己"),
                (lambda m: m["materials"][1].update(asset_path="/Game/Test/MI_Unused"), "未被任何部件使用"),
                (lambda m: m["materials"][0]["textures"].update({"bad name": "/Game/Test/Mod/cloth_d"}), "参数名无效"),
                (lambda m: m["export_assets"].append({"asset_path": "/Game/Test/MI_Existing",
                                                       "asset_type": "MaterialInstanceConstant", "origin": "mod"}), "打包")]:
            broken = copy.deepcopy(manifest)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(BridgeError, message):
                validate_manifest(broken)
        broken = copy.deepcopy(manifest)
        broken["textures"].append({"id": "p", "source_file": "textures/p.png", "asset_path": "/Game/Preview/T_P",
                                   "role": "BASE_COLOR", "origin": "preview"})
        broken["material_previews"] = [{"material_path": "/Game/Test/Mod/MI_New", "texture_path": "/Game/Preview/T_P"}]
        with self.assertRaisesRegex(BridgeError, "不使用漫射预览"):
            validate_manifest(broken)

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
        self.assertEqual(texture_settings("MASK"), {"compression": "BC7", "srgb": False})

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
