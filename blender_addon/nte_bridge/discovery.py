"""Read FModel JSON metadata without guessing UE paths from disk layout.

The returned catalog is reference information only; discovery never changes a
Blender scene, imports assets, or adds original textures/materials to a package.
"""
import json
import os
from pathlib import Path
import re

from .core import BridgeError


_MATERIAL_TYPES = {"Material", "MaterialInstance", "MaterialInstanceConstant", "MaterialInstanceDynamic", "MaterialInterface"}
_RESOURCE_TYPES = _MATERIAL_TYPES | {"SkeletalMesh", "Skeleton", "PhysicsAsset"}
_PACKAGE = re.compile(r"/(?:Game|Engine)/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+$")
_QUALIFIED = re.compile(r"^(\w+)'([^']+)'$")
_MAX_FILES = 10000
_MAX_FILE_BYTES = 64 * 1024 * 1024


def normalize_reference(value):
    """Return a package path from a FModel reference or UE object path.

    FModel's numeric export suffix (/Game/X.84) is not an asset name. A normal
    UE object suffix (/Game/X.X) is accepted too; subobjects and traversal are
    rejected. No local path, short name, or inferred directory is accepted.
    """
    if isinstance(value, dict):
        for key in ("ObjectPath", "AssetPathName", "Package", "Path"):
            if key in value:
                result = normalize_reference(value[key])
                if result:
                    return result
        return ""
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    match = _QUALIFIED.fullmatch(raw)
    if match:
        raw = match.group(2)
    if "." in raw:
        package, suffix = raw.rsplit(".", 1)
        if not suffix.isdecimal() and suffix != package.rsplit("/", 1)[-1]:
            return ""
        raw = package
    return raw if _PACKAGE.fullmatch(raw) else ""


def _type_name(value):
    if not isinstance(value, str):
        return "", ""
    match = _QUALIFIED.fullmatch(value)
    if not match:
        return "", ""
    return match.group(1), match.group(2).rsplit("/", 1)[-1].split(".")[-1]


def _references(value):
    """Yield typed references at every nesting level, including overlays."""
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            path = normalize_reference(node)
            if path:
                kind, name = _type_name(node.get("ObjectName", ""))
                yield {"asset_path": path, "type": kind,
                       "name": name or path.rsplit("/", 1)[-1]}
            pending.extend(x for x in node.values() if isinstance(x, (dict, list))
                           or isinstance(x, str) and _QUALIFIED.fullmatch(x))
        elif isinstance(node, list):
            pending.extend(x for x in node if isinstance(x, (dict, list)))
        elif isinstance(node, str):
            path = normalize_reference(node)
            if path:
                kind, name = _type_name(node)
                yield {"asset_path": path, "type": kind, "name": name}


def _json_files(root, warnings):
    files = []
    def error(exc):
        warnings.append(f"无法读取目录: {exc}")
    for directory, folders, names in os.walk(root, followlinks=False, onerror=error):
        folders[:] = sorted(name for name in folders
                            if not Path(directory, name).is_symlink()
                            and Path(directory, name).resolve().is_relative_to(root))
        for name in sorted(names):
            path = Path(directory, name)
            if path.suffix.lower() == ".json" and not path.is_symlink() and path.resolve().is_relative_to(root):
                files.append(path)
                if len(files) > _MAX_FILES:
                    raise BridgeError(f"JSON 文件超过 {_MAX_FILES} 个，请选择更具体的角色目录")
    return files


def _read_document(path, warnings):
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            warnings.append(f"JSON 过大，已跳过: {path}")
            return None
        def reject_constant(value):
            raise ValueError(f"非有限数值: {value}")
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
        if isinstance(data, dict) and isinstance(data.get("Exports"), list):
            data = data["Exports"]
        elif isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            warnings.append(f"JSON 不是资源导出列表，已跳过: {path}")
            return None
        exports = []
        for item in data:
            if not isinstance(item, dict) or not item.get("Type"):
                continue
            if not isinstance(item["Type"], str) or not isinstance(item.get("Name"), str):
                warnings.append(f"JSON 资源的 Type / Name 不是字符串，已跳过该记录: {path}")
                continue
            exports.append(item)
        references = list(_references(exports))
        return {"source_file": str(path), "exports": exports, "references": references}
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        warnings.append(f"JSON 无法读取，已跳过: {path} ({exc})")
        return None


def _reference_index(references):
    result = {}
    for ref in references:
        if ref["type"] and ref["name"] == ref["asset_path"].rsplit("/", 1)[-1]:
            result.setdefault((ref["type"], ref["name"]), set()).add(ref["asset_path"])
    return result


def _identity(export, document, global_index, warnings):
    name, kind = export.get("Name", ""), export.get("Type", "")
    explicit = {normalize_reference(export[key]) for key in ("Package", "ObjectPath", "PathName")
                if key in export}
    explicit.discard("")
    local = _reference_index(document["references"]).get((kind, name), set())
    candidates = explicit or local or global_index.get((kind, name), set())
    if len(candidates) == 1:
        path = next(iter(candidates))
        if path.rsplit("/", 1)[-1] == name:
            return path
    reason = "路径存在歧义" if len(candidates) > 1 else "缺少可验证的完整 UE 路径"
    warnings.append(f"{kind} {name}: {reason}，未根据磁盘目录猜测 ({document['source_file']})")
    return ""


def _properties(export):
    return export.get("Properties", {}) if isinstance(export.get("Properties"), dict) else {}


def _material_stub(path, kind="MaterialInterface"):
    return {"name": path.rsplit("/", 1)[-1], "asset_path": path,
            "type": kind or "MaterialInterface", "parent_path": "",
            "root_material_path": "", "parent_chain": [], "parent_chain_complete": False,
            "source_file": "", "available": False, "parameters": {},
            "textures": [], "dependencies": [], "overrides": {}, "referenced_by": []}


def _material(export, path, source):
    result = _material_stub(path, export["Type"])
    props = _properties(export)
    result.update(available=True, source_file=source,
                  parent_path=normalize_reference(props.get("Parent")))
    # Preserve association/index, null values, game-specific spellings and all
    # parameter types. These are original metadata, not effective merged values.
    result["parameters"] = {key: value for key, value in props.items()
                            if "Parameter" in key}
    result["overrides"] = {key: value for key, value in props.items()
                           if "Override" in key and "Material" not in key}
    refs = list(_references(props))
    result["dependencies"] = list({(ref["asset_path"], ref["type"]): ref for ref in refs}.values())
    texture_parameters = props.get("TextureParameterValues", [])
    for parameter in texture_parameters if isinstance(texture_parameters, list) else []:
        if not isinstance(parameter, dict):
            continue
        info = parameter.get("ParameterInfo", {})
        info = info if isinstance(info, dict) else {}
        value = parameter.get("ParameterValue")
        kind, _ = _type_name(value.get("ObjectName", "")) if isinstance(value, dict) else ("", "")
        result["textures"].append({"name": info.get("Name", parameter.get("ParameterName", "")),
                                   "asset_path": normalize_reference(value), "type": kind,
                                   "association": info.get("Association", ""),
                                   "index": info.get("Index", -1)})
    return result


def _mesh(export, path, source, warnings):
    props = _properties(export)
    result = {"name": export["Name"], "asset_path": path, "source_file": source,
              "skeleton_path": normalize_reference(props.get("Skeleton", export.get("Skeleton"))),
              "physics_path": normalize_reference(props.get("PhysicsAsset", export.get("PhysicsAsset"))),
              "slots": []}
    slots = export.get("SkeletalMaterials", props.get("Materials", export.get("Materials", [])))
    if not isinstance(slots, list):
        warnings.append(f"材质槽数据无效: {path}")
        slots = []
    for index, item in enumerate(slots):
        if not isinstance(item, dict):
            item = {}
        material_path = normalize_reference(item.get("Material", item.get("MaterialInterface")))
        result["slots"].append({"index": index, "name": item.get("MaterialSlotName") or str(index),
                                "imported_name": item.get("ImportedMaterialSlotName") or "",
                                "material_path": material_path})
        if not material_path:
            warnings.append(f"材质槽 {index} 缺少完整材质路径: {path}")
    if not result["skeleton_path"]:
        warnings.append(f"网格缺少完整骨架引用: {path}")
    return result


def _finish_chains(materials, warnings):
    for material in materials.values():
        current, visited = material, set()
        while current:
            path = current["asset_path"]
            if path in visited:
                warnings.append(f"材质父级形成循环: {material['asset_path']}")
                break
            visited.add(path)
            if current["type"] == "Material":
                material["root_material_path"] = path
                material["parent_chain_complete"] = True
                break
            parent = current["parent_path"]
            if not parent:
                break
            material["parent_chain"].append(parent)
            current = materials.get(parent)
        if not material["available"]:
            warnings.append(f"仅有材质引用，未找到对应 JSON，参数未知: {material['asset_path']}")


def scan_character(folder, *, dependency_root=None):
    """Return JSON-serializable mesh choices and original material metadata.

    Every mesh remains a separate choice. ``dependency_root`` is optional and
    explicit: only same-name JSON candidates for referenced missing materials
    are read there, and metadata is accepted only on an exact UE path match.
    The default never traverses siblings outside the selected character folder.
    """
    if not str(folder).strip():
        raise BridgeError("请填写角色解包目录")
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise BridgeError(f"角色解包目录不存在: {folder}")
    warnings, documents = [], []
    for path in _json_files(root, warnings):
        document = _read_document(path, warnings)
        if document:
            documents.append(document)
    if not documents:
        raise BridgeError("角色目录内没有可读取的 FModel JSON；请同时导出资源的 JSON 元数据")
    refs = [ref for doc in documents for ref in doc["references"]]
    global_index = _reference_index(refs)
    meshes, materials, signatures, blocked = {}, {}, {}, set()

    def merge(export, path, source):
        signature = json.dumps(export, sort_keys=True, ensure_ascii=False)
        if path in blocked:
            return
        if path in signatures and signatures[path] != signature:
            blocked.add(path)
            meshes.pop(path, None)
            materials.pop(path, None)
            warnings.append(f"同一 UE 路径有不同导出数据，未自动选择: {path}")
            return
        if path in signatures:
            return
        signatures[path] = signature
        if export["Type"] == "SkeletalMesh":
            meshes[path] = _mesh(export, path, source, warnings)
        elif export["Type"] in _MATERIAL_TYPES:
            materials[path] = _material(export, path, source)

    for doc in documents:
        for export in doc["exports"]:
            if export.get("Type") in _RESOURCE_TYPES:
                path = _identity(export, doc, global_index, warnings)
                if path:
                    merge(export, path, doc["source_file"])

    def add_references(references):
        for ref in references:
            if ref["type"] in _MATERIAL_TYPES and ref["asset_path"] not in materials:
                materials[ref["asset_path"]] = _material_stub(ref["asset_path"], ref["type"])
    add_references(refs)
    # A path-only slot/parent remains visible even if its JSON omitted class.
    for mesh in meshes.values():
        for slot in mesh["slots"]:
            if slot["material_path"]:
                materials.setdefault(slot["material_path"], _material_stub(slot["material_path"]))
    for material in list(materials.values()):
        if material["parent_path"]:
            materials.setdefault(material["parent_path"], _material_stub(material["parent_path"]))

    if dependency_root is not None:
        dependency_root = Path(dependency_root).expanduser().resolve()
        if not dependency_root.is_dir():
            raise BridgeError(f"共享材质资料目录不存在: {dependency_root}")
        filenames = {}
        for path in _json_files(dependency_root, warnings):
            filenames.setdefault(path.stem, []).append(path)
        attempted = set()
        while True:
            pending = [m for p, m in materials.items() if not m["available"] and p not in attempted and p not in blocked]
            if not pending:
                break
            for missing in pending:
                target = missing["asset_path"]
                attempted.add(target)
                for file in filenames.get(missing["name"], []):
                    doc = _read_document(file, warnings)
                    if not doc:
                        continue
                    for export in doc["exports"]:
                        if export.get("Type") not in _MATERIAL_TYPES or export.get("Name") != missing["name"]:
                            continue
                        # Do not use the original target reference to "prove"
                        # the identity of an otherwise unidentified JSON file.
                        path = _identity(export, doc, {}, warnings)
                        if path != target:
                            continue
                        merge(export, path, doc["source_file"])
                        add_references(doc["references"])
                        for m in list(materials.values()):
                            if m["parent_path"]:
                                materials.setdefault(m["parent_path"], _material_stub(m["parent_path"]))

    for material in list(materials.values()):
        for ref in material["dependencies"]:
            dependency = materials.get(ref["asset_path"])
            if dependency and dependency is not material:
                dependency["referenced_by"].append(material["asset_path"])
    for mesh in meshes.values():
        for slot in mesh["slots"]:
            if slot["material_path"] in materials:
                materials[slot["material_path"]]["referenced_by"].append(mesh["asset_path"])
    for path in blocked:
        if path in materials:
            materials[path]["ambiguous"] = True
    for material in materials.values():
        material["referenced_by"] = sorted(set(material["referenced_by"]))
    _finish_chains(materials, warnings)
    if not meshes:
        warnings.append("没有识别到具有完整 UE 路径的骨骼网格体，请检查 SkeletalMesh JSON")
    return {"folder": str(root), "meshes": sorted(meshes.values(), key=lambda x: x["asset_path"]),
            "materials": sorted(materials.values(), key=lambda x: x["asset_path"]),
            "warnings": list(dict.fromkeys(warnings))}
