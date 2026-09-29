"""Portable NTE job and graph semantics. No Blender/Unreal imports."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile

SCHEMA_VERSION = 1
VERSION = "0.2.0"
HIDDEN_STATE = "$hidden"


class BridgeError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise BridgeError(message)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label}: 不能为空")
    return value


def package_path(value, label="资源路径", optional=False):
    if optional and value == "":
        return value
    _text(value, label)
    _require(re.fullmatch(r"/Game/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+", value),
             f"{label}: 使用完整 /Game/目录/资源 路径，不含 .资源 后缀或空格: {value}")
    return value


def resolve_source(job_dir, relative):
    _text(relative, "source_file")
    rel = PurePosixPath(relative.replace("\\", "/"))
    _require(not rel.is_absolute() and not PureWindowsPath(relative).drive
             and ".." not in rel.parts and ":" not in relative,
             f"文件必须位于任务目录内: {relative}")
    root = Path(job_dir).resolve()
    target = root.joinpath(*rel.parts).resolve()
    _require(target.is_relative_to(root) and target != root, f"文件越过任务目录: {relative}")
    return target


def texture_settings(role):
    recipes = {
        "BASE_COLOR": {"compression": "BC7", "srgb": True},
        "ID_TEX": {"compression": "BC7", "srgb": False},
        "LIGHT_MAP": {"compression": "BC7", "srgb": False},
        "NORMAL": {"compression": "NORMALMAP", "srgb": False},
    }
    _require(role in recipes, f"未知贴图用途: {role}")
    return dict(recipes[role])


def write_json(path, data):
    """Replace one complete document atomically; never leave half-written jobs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def manifest_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise BridgeError(f"无法读取清单 {path}: {exc}") from exc
    return validate_manifest(data, path.parent)


def _records(value, label):
    _require(isinstance(value, list), f"{label}: 必须是列表")
    _require(all(isinstance(item, dict) for item in value), f"{label}: 条目必须是对象")
    return value


def _unique(values, label):
    _require(len(values) == len(set(values)), f"{label}: 存在重复值")


def validate_features(features, part_ids):
    features = _records(features, "features")
    ids, keys, owners = [], set(), {}
    for feature in features:
        fid = _text(feature.get("id"), "功能 ID")
        ids.append(fid)
        _require(feature.get("type") == "visibility_cycle", f"功能 {fid}: 未支持的功能类型")
        states = _records(feature.get("states"), f"功能 {fid} states")
        hidden = feature.get("include_hidden_state", False)
        _require(type(hidden) is bool, f"功能 {fid}: 全隐藏必须为布尔值")
        _require(len(states) + int(hidden) >= 2, f"功能 {fid}: 至少两个状态，或一个状态加全隐藏")
        sids, members = [], []
        for state in states:
            sid = _text(state.get("id"), f"功能 {fid} 状态 ID")
            _require(sid != HIDDEN_STATE, f"功能 {fid}: {HIDDEN_STATE} 为保留状态 ID")
            sids.append(sid)
            parts = state.get("parts")
            _require(isinstance(parts, list) and bool(parts), f"功能 {fid} 状态 {sid}: 没有部件")
            _require(all(isinstance(p, str) and p in part_ids for p in parts),
                     f"功能 {fid} 状态 {sid}: 引用了不存在的部件")
            members.extend(parts)
        _unique(sids, f"功能 {fid} 状态 ID")
        _unique(members, f"功能 {fid} 部件（当前版本每个部件只能属于一个状态）")
        initial = feature.get("initial_state_id")
        _require(initial in sids or (hidden and initial == HIDDEN_STATE),
                 f"功能 {fid}: 初始状态不存在")
        key = _text(feature.get("key"), f"功能 {fid} 按键")
        key_identity = re.sub(r"\s+", "", key).casefold()
        _require(key_identity not in keys, f"功能 {fid}: 按键 {key} 与其他功能冲突")
        keys.add(key_identity)
        for pid in members:
            _require(pid not in owners, f"功能 {fid}: 部件 {pid} 已由功能 {owners.get(pid)} 控制")
            owners[pid] = fid
    _unique(ids, "功能 ID")
    return features


def validate_manifest(data, job_dir=None):
    _require(isinstance(data, dict), "清单必须是对象")
    _require(type(data.get("schema_version")) is int and data["schema_version"] == SCHEMA_VERSION,
             "清单版本不受支持，需要 schema_version=1")
    for field in ("job_id", "graph_id", "character_id", "project_file"):
        _text(data.get(field), field)
    project = data["project_file"]
    _require(project.lower().endswith(".uproject") and
             (Path(project).is_absolute() or PureWindowsPath(project).is_absolute()),
             "project_file 必须是目标 .uproject 的绝对路径")
    _require(type(data.get("create_placeholders", False)) is bool, "create_placeholders 必须为布尔值")
    mesh = data.get("mesh")
    _require(isinstance(mesh, dict), "缺少 mesh 配置")
    mesh_id = _text(mesh.get("id"), "网格 ID")
    target = package_path(mesh.get("asset_path"), "网格路径")
    skeleton = package_path(mesh.get("skeleton_path"), "骨架路径")
    physics = package_path(mesh.get("physics_asset_path", ""), "物理资产路径", optional=True)
    _require(len({p.casefold() for p in (target, skeleton, physics) if p}) == (3 if physics else 2),
             "网格、骨架、物理资产路径不能相同")
    source = _text(mesh.get("source_file"), "FBX 文件")
    _require(PurePosixPath(source).suffix.lower() == ".fbx", "网格源文件必须为 FBX")
    expected = mesh.get("expected")
    _require(isinstance(expected, dict), "缺少网格校验信息 expected")
    bones = _records(expected.get("bones"), "bones")
    _require(bool(bones), "骨架没有骨骼")
    bone_names = [_text(b.get("name"), "骨名") for b in bones]
    _unique(bone_names, "骨名")
    bone_parents = {}
    for bone in bones:
        parent = bone.get("parent")
        _require(isinstance(parent, str) and (parent == "" or parent in bone_names)
                 and parent != bone["name"], f"骨骼 {bone['name']}: 父级无效")
        bone_parents[bone["name"]] = parent
    for name in bone_names:
        seen = set()
        while name:
            _require(name not in seen, "骨骼层级存在环")
            seen.add(name)
            name = bone_parents[name]
    _require(sum(not b["parent"] for b in bones) == 1, "导出骨架必须只有一个根骨")
    shapes = expected.get("shape_keys")
    _require(isinstance(shapes, list) and all(isinstance(s, str) and s for s in shapes), "形态键清单无效")
    _unique(shapes, "形态键名称")
    uvs = expected.get("uv_layers")
    _require(type(uvs) is int and 1 <= uvs <= 8, "UV 层数必须为 1–8")
    parts = _records(data.get("parts"), "parts")
    _require(bool(parts), "网格没有材质槽映射")
    part_ids, slot_keys, slot_indices, material_paths = [], [], [], []
    for part in parts:
        part_ids.append(_text(part.get("id"), "部件 ID"))
        key = _text(part.get("slot_key"), "导出槽名")
        _require(re.fullmatch(r"NTE_[A-Za-z0-9_]+", key), f"导出槽名无效: {key}")
        slot_keys.append(key.casefold())
        index = part.get("source_slot")
        _require(type(index) is int and index >= 0, "源材质槽编号必须为非负整数")
        slot_indices.append(index)
        material_paths.append(package_path(part.get("material_path"), "材质路径"))
    _unique(part_ids, "部件 ID")
    _unique(slot_keys, "导出槽名")
    _unique(slot_indices, "源槽编号")
    _require(sorted(slot_indices) == list(range(len(parts))), "需要完整、连续的材质槽映射（包括不参与切换的槽）")
    asset_targets = {target.casefold(): (target, "SkeletalMesh")}
    reference_targets = {p.casefold() for p in [skeleton, physics] + material_paths if p}
    _require(target.casefold() not in reference_targets, "网格路径与引用资源冲突")
    _require(skeleton.casefold() not in {p.casefold() for p in material_paths}, "骨架路径与材质路径冲突")
    _require(not physics or physics.casefold() not in {p.casefold() for p in material_paths}, "物理路径与材质路径冲突")
    sources, asset_ids = [source], [mesh_id]
    for tex in _records(data.get("textures", []), "textures"):
        asset_ids.append(_text(tex.get("id"), "贴图 ID"))
        path = package_path(tex.get("asset_path"), "贴图路径")
        _require(path.casefold() not in asset_targets and path.casefold() not in reference_targets,
                 f"重复或冲突的资源路径: {path}")
        asset_targets[path.casefold()] = (path, "Texture2D")
        texture_settings(tex.get("role"))
        sources.append(_text(tex.get("source_file"), "贴图文件"))
    _unique(asset_ids, "资源 ID")
    for relative in sources:
        resolved = resolve_source(job_dir or Path.cwd(), relative)
        if job_dir is not None:
            _require(resolved.is_file(), f"源文件不存在: {resolved}")
    validate_features(data.get("features", []), set(part_ids))
    exports = _records(data.get("export_assets"), "export_assets")
    export_paths = []
    for asset in exports:
        path = package_path(asset.get("asset_path"), "打包资源路径")
        export_paths.append(path.casefold())
        _require(asset.get("origin") == "mod", f"禁止打包原游戏占位资源: {path}")
        _require(path.casefold() in asset_targets, f"资源未在本任务声明为网格或替换贴图: {path}")
        _require(asset.get("asset_type") == asset_targets[path.casefold()][1],
                 f"打包资源类型错误: {path}")
    _unique(export_paths, "打包资源路径")
    _require(set(export_paths) == set(asset_targets), "打包清单必须包含本次网格和全部明确替换贴图")
    return data


def compile_graph(graph, parts):
    """Resolve output-reachable declarative nodes into explicit visibility states."""
    _require(isinstance(graph, dict), "节点图必须是对象")
    _text(graph.get("id"), "节点图 ID")
    nodes_list = _records(graph.get("nodes"), "nodes")
    links = _records(graph.get("links"), "links")
    ids = [_text(n.get("id"), "节点 ID") for n in nodes_list]
    _unique(ids, "节点 ID（复制节点需要新的标识）")
    nodes = dict(zip(ids, nodes_list))
    valid_parts = {p["id"] for p in parts}
    for nid, node in nodes.items():
        _require(node.get("type") in {"PART", "GROUP", "CYCLE", "OUTPUT"}, f"节点 {nid}: 未知类型")
        if node["type"] == "PART":
            _require(node.get("part_id") in valid_parts, f"节点 {nid}: 部件已丢失")
    outputs = [n for n in nodes_list if n["type"] == "OUTPUT"]
    _require(len(outputs) == 1, "节点图必须有且只有一个角色输出节点")
    incoming = {nid: [] for nid in nodes}
    sockets = set()
    allowed = {"PART": set(), "GROUP": {"PART", "GROUP"}, "CYCLE": {"PART", "GROUP"}, "OUTPUT": {"CYCLE"}}
    for link in links:
        src, dst = link.get("from_node"), link.get("to_node")
        _require(src in nodes and dst in nodes, "连线引用了不存在的节点")
        socket = _text(link.get("to_socket"), f"节点 {dst} 输入插口")
        _require((dst, socket) not in sockets, f"节点 {dst} 插口 {socket}: 只能连接一个输入")
        sockets.add((dst, socket))
        incoming[dst].append((socket, src))
    # Validate the entire dependency graph before type resolution, including disconnected cycles.
    active, done = set(), set()
    def visit(nid):
        _require(nid not in active, f"节点 {nid}: 连线形成循环依赖")
        if nid in done:
            return
        active.add(nid)
        for _, src in incoming[nid]:
            visit(src)
        active.remove(nid)
        done.add(nid)
    for nid in nodes:
        visit(nid)
    for dst, entries in incoming.items():
        for _, src in entries:
            _require(nodes[src]["type"] in allowed[nodes[dst]["type"]], f"节点 {dst}: 输入类型不兼容")
    def collect(nid):
        node = nodes[nid]
        if node["type"] == "PART":
            return [node["part_id"]]
        result = [pid for _, src in incoming[nid] for pid in collect(src)]
        _require(bool(result), f"节点 {nid}: 组合没有部件")
        _unique(result, f"节点 {nid} 部件")
        return result
    features = []
    used = set()
    for _, nid in incoming[outputs[0]["id"]]:
        _require(nid not in used, f"节点 {nid}: 同一切换重复连接到输出")
        used.add(nid)
        node = nodes[nid]
        state_ids = node.get("state_ids")
        _require(isinstance(state_ids, list) and all(isinstance(s, str) and s for s in state_ids),
                 f"节点 {nid}: 状态 ID 列表无效")
        _unique(state_ids, f"节点 {nid} 状态 ID")
        by_socket = dict(incoming[nid])
        _require(set(by_socket) == set(state_ids), f"节点 {nid}: 每个状态都需要连接，且不能有未知状态插口")
        states = [{"id": sid, "label": nodes[by_socket[sid]].get("label", sid),
                   "parts": collect(by_socket[sid])} for sid in state_ids]
        features.append({
            "id": nid, "type": "visibility_cycle", "label": node.get("label", "切换"),
            "key": node.get("key", "K"), "states": states,
            "include_hidden_state": node.get("include_hidden", False),
            "initial_state_id": node.get("initial_state_id") or (state_ids[0] if state_ids else ""),
            "panel": node.get("panel", {"enabled": True, "label": node.get("label", "切换"), "order": len(features)}),
        })
    return validate_features(features, valid_parts)
