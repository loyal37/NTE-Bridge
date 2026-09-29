"""Synthetic FModel exports; never commit extracted game data as test fixtures."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import BridgeError
from nte_bridge.discovery import normalize_reference, scan_character


def ref(kind, path, suffix="0"):
    return {"ObjectName": f"{kind}'{path.rsplit('/', 1)[-1]}'", "ObjectPath": path + "." + suffix}


def mesh(name="SK_Character", folder="/Game/Characters/Test", material="/Game/Shared/MI_Same"):
    return {"Type": "SkeletalMesh", "Name": name, "Package": folder + "/" + name,
            "Properties": {"Skeleton": ref("Skeleton", folder + "/SK_Skeleton", "1"),
                           "PhysicsAsset": ref("PhysicsAsset", folder + "/PH_Test")},
            "SkeletalMaterials": [{"MaterialSlotName": "body", "Material": ref("MaterialInstanceConstant", material)},
                                  {"MaterialSlotName": "sleeve", "Material": ref("MaterialInstanceConstant", material)}]}


def material(name="MI_Same", parent="/Game/Shared/M_Root", kind="MaterialInstanceConstant", folder="/Game/Shared"):
    props = {"Parent": ref("Material", parent)} if parent else {}
    return {"Type": kind, "Name": name, "Package": folder + "/" + name, "Properties": props}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "local_name_is_not_ue_path"
        self.root.mkdir()

    def write(self, relative, data, root=None):
        path = (root or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
        return path

    def test_package_paths_slots_and_shared_material_identity(self):
        self.write("renamed_on_disk.json", [mesh()])
        self.write("ter/Ml_any_name.json", [material()])
        result = scan_character(self.root)
        found = result["meshes"][0]
        self.assertEqual(found["asset_path"], "/Game/Characters/Test/SK_Character")
        self.assertEqual(found["skeleton_path"], "/Game/Characters/Test/SK_Skeleton")
        self.assertEqual(found["physics_path"], "/Game/Characters/Test/PH_Test")
        self.assertEqual([slot["name"] for slot in found["slots"]], ["body", "sleeve"])
        self.assertEqual([slot["index"] for slot in found["slots"]], [0, 1])
        self.assertEqual(len({slot["material_path"] for slot in found["slots"]}), 1)
        self.assertEqual(sum(x["available"] for x in result["materials"]), 1)
        json.dumps(result, allow_nan=False)

    def test_numeric_and_ue_object_suffixes_only(self):
        for value in ("/Game/A/Test.84", "/Game/A/Test.Test", "Material'/Game/A/Test.Test'", "/Game/A/Test"):
            self.assertEqual(normalize_reference(value), "/Game/A/Test")
        for value in ("../Test", "C:/Game/Test", "/Game/A/../Test.0", "/Game/A/Test.Other", "/Game/A/Test:Sub", "Test", "/Game/A/Test.0.foo"):
            self.assertEqual(normalize_reference(value), "", value)

    def test_outer_reference_identifies_mesh_without_package(self):
        body = mesh()
        del body["Package"]
        self.write("mesh.json", [body, {"Type": "MorphTarget", "Name": "Smile",
                                      "Outer": ref("SkeletalMesh", "/Game/Characters/Test/SK_Character", "84")}])
        result = scan_character(self.root)
        self.assertEqual(result["meshes"][0]["asset_path"], "/Game/Characters/Test/SK_Character")

    def test_cross_document_reference_can_resolve_unique_sibling(self):
        body = mesh()
        del body["Package"]
        self.write("mesh.json", [body])
        self.write("physics.json", [{"Type": "PhysicsAsset", "Name": "PH_Test",
                                    "Package": "/Game/Characters/Test/PH_Test",
                                    "Properties": {"PreviewSkeletalMesh": ref("SkeletalMesh", "/Game/Characters/Test/SK_Character")}}])
        self.assertEqual(scan_character(self.root)["meshes"][0]["asset_path"], "/Game/Characters/Test/SK_Character")

    def test_no_path_is_guessed_and_ambiguous_names_are_not_selected(self):
        body = mesh()
        del body["Package"]
        self.write("SK_Character.json", [body])
        first = scan_character(self.root)
        self.assertEqual(first["meshes"], [])
        self.assertTrue(any("未根据磁盘目录猜测" in warning for warning in first["warnings"]))
        self.write("refs.json", [{"Type": "DataAsset", "Name": "Refs", "Properties": {
            "First": ref("SkeletalMesh", "/Game/First/SK_Character"),
            "Second": ref("SkeletalMesh", "/Game/Second/SK_Character")}}])
        second = scan_character(self.root)
        self.assertEqual(second["meshes"], [])
        self.assertTrue(any("歧义" in warning for warning in second["warnings"]))

    def test_multiple_meshes_are_retained_as_explicit_choices(self):
        self.write("character.json", [mesh()])
        secondary = mesh("SK_Fire")
        secondary["Properties"].pop("PhysicsAsset")
        self.write("fire/mesh.json", {"Exports": [secondary]})
        found = scan_character(self.root)["meshes"]
        self.assertEqual([x["name"] for x in found], ["SK_Character", "SK_Fire"])
        self.assertEqual(found[1]["physics_path"], "")

    def test_instance_chain_preserves_known_root_and_missing_metadata(self):
        child = material()
        child["Properties"]["Parent"] = ref("MaterialInstanceConstant", "/Game/Shared/MI_Parent")
        self.write("material.json", [child, material("MI_Parent")])
        catalog = {m["asset_path"]: m for m in scan_character(self.root)["materials"]}
        found = catalog["/Game/Shared/MI_Same"]
        self.assertEqual(found["parent_chain"], ["/Game/Shared/MI_Parent", "/Game/Shared/M_Root"])
        self.assertEqual(found["root_material_path"], "/Game/Shared/M_Root")
        self.assertTrue(found["parent_chain_complete"])
        self.assertFalse(catalog["/Game/Shared/M_Root"]["available"])
        self.assertEqual(catalog["/Game/Shared/M_Root"]["parameters"], {})

    def test_unavailable_instance_does_not_become_a_fabricated_root(self):
        child = material()
        child["Properties"]["Parent"] = ref("MaterialInstanceConstant", "/Game/Shared/MI_Unknown")
        self.write("material.json", [child])
        catalog = {m["name"]: m for m in scan_character(self.root)["materials"]}
        self.assertEqual(catalog["MI_Same"]["root_material_path"], "")
        self.assertFalse(catalog["MI_Same"]["parent_chain_complete"])
        self.assertEqual(catalog["MI_Same"]["parent_chain"], ["/Game/Shared/MI_Unknown"])
        self.assertFalse(catalog["MI_Unknown"]["available"])

    def test_parent_cycles_terminate_with_warning(self):
        a, b = material("MI_A"), material("MI_B")
        a["Properties"]["Parent"] = ref("MaterialInstanceConstant", "/Game/Shared/MI_B")
        b["Properties"]["Parent"] = ref("MaterialInstanceConstant", "/Game/Shared/MI_A")
        self.write("material.json", [a, b])
        result = scan_character(self.root)
        self.assertTrue(any("循环" in warning for warning in result["warnings"]))
        self.assertTrue(all(not m["root_material_path"] for m in result["materials"]))

    def test_overlay_dependencies_and_raw_parameter_information(self):
        child = material()
        child["Properties"].update({
            "OutlineOverlayMaterial": ref("MaterialInstanceConstant", "/Game/Shared/MI_Outline"),
            "HairShadowOverlayMaterial": "Material'/Game/Shared/M_Shadow.M_Shadow'",
            "TextureParameterValues": [
                {"ParameterInfo": {"Name": "NomralMap", "Association": "LayerParameter", "Index": 2},
                 "ParameterValue": ref("Texture2D", "/Game/Tex/T_N")},
                {"ParameterInfo": {"Name": "Unused"}, "ParameterValue": None}],
            "ScalarParameterValues": [{"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.15}],
            "BasePropertyOverrides": {"bOverride_TwoSided": True}})
        self.write("material.json", [child])
        catalog = {m["name"]: m for m in scan_character(self.root)["materials"]}
        found = catalog["MI_Same"]
        self.assertEqual(found["textures"][0], {"name": "NomralMap", "asset_path": "/Game/Tex/T_N",
                                               "type": "Texture2D", "association": "LayerParameter", "index": 2})
        self.assertEqual(found["textures"][1]["asset_path"], "")
        self.assertEqual(found["parameters"]["ScalarParameterValues"][0]["ParameterValue"], 0.15)
        self.assertIn("MI_Outline", catalog)
        self.assertIn("M_Shadow", catalog)
        self.assertFalse(catalog["MI_Outline"]["available"])
        self.assertEqual(catalog["MI_Outline"]["referenced_by"], ["/Game/Shared/MI_Same"])

    def test_bad_json_is_reported_while_valid_candidates_survive(self):
        self.write("mesh.json", [mesh()])
        (self.root / "broken.json").write_text("{", encoding="utf-8")
        self.write("not_an_export.json", 123)
        result = scan_character(self.root)
        self.assertEqual(len(result["meshes"]), 1)
        self.assertTrue(any("broken.json" in x for x in result["warnings"]))
        self.assertTrue(any("not_an_export.json" in x for x in result["warnings"]))

    def test_malformed_export_identifiers_do_not_abort_other_records(self):
        self.write("mixed.json", [mesh(), {"Type": ["SkeletalMesh"], "Name": "Invalid"},
                                  {"Type": "SkeletalMesh", "Name": ["Invalid"]},
                                  {"Type": "Material", "Name": {"Invalid": True}}])
        result = scan_character(self.root)
        self.assertEqual([m["name"] for m in result["meshes"]], ["SK_Character"])
        self.assertTrue(any("Type / Name" in warning for warning in result["warnings"]))

    def test_duplicate_exports_are_deduplicated_but_conflicts_are_not_chosen(self):
        self.write("first.json", [mesh()])
        self.write("copy.json", [mesh()])
        self.assertEqual(len(scan_character(self.root)["meshes"]), 1)
        changed = mesh()
        changed["Properties"]["Skeleton"] = ref("Skeleton", "/Game/Other/Skeleton")
        self.write("copy.json", [changed])
        result = scan_character(self.root)
        self.assertEqual(result["meshes"], [])
        self.assertTrue(any("不同导出数据" in x for x in result["warnings"]))

    def test_optional_dependency_root_requires_its_own_exact_path_evidence(self):
        self.write("material.json", [material()])
        shared = Path(self.temp.name) / "shared"
        shared.mkdir()
        # A matching filename/name does not establish the desired UE path.
        wrong = material("M_Root", None, "Material", "/Game/Unrelated")
        self.write("wrong/M_Root.json", [wrong], root=shared)
        unknown = material("M_Root", None, "Material")
        unknown.pop("Package")
        self.write("unknown/M_Root.json", [unknown], root=shared)
        result = scan_character(self.root, dependency_root=shared)
        catalog = {m["name"]: m for m in result["materials"]}
        self.assertFalse(catalog["M_Root"]["available"])
        self.write("right/M_Root.json", [material("M_Root", None, "Material")], root=shared)
        catalog = {m["name"]: m for m in scan_character(self.root, dependency_root=shared)["materials"]}
        self.assertTrue(catalog["M_Root"]["available"])
        # Default scan never follows sibling directories.
        catalog = {m["name"]: m for m in scan_character(self.root)["materials"]}
        self.assertFalse(catalog["M_Root"]["available"])

    def test_empty_and_missing_directory_explain_json_requirement(self):
        with self.assertRaisesRegex(BridgeError, "填写"):
            scan_character("")
        with self.assertRaisesRegex(BridgeError, "JSON"):
            scan_character(self.root)
        with self.assertRaisesRegex(BridgeError, "不存在"):
            scan_character(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
