"""Portable NTE job and graph semantics. No Blender/Unreal imports."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile

SCHEMA_VERSION = 1
VERSION = "0.4.0"
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
        "MASK": {"compression": "BC7", "srgb": False},
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


_KEY_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl", "leftctrl": "ctrl",
                  "rightctrl": "ctrl", "shift": "shift", "shit": "shift", "lshift": "shift", "rshift": "shift",
                  "leftshift": "shift", "rightshift": "shift", "alt": "alt", "lalt": "alt", "ralt": "alt",
                  "leftalt": "alt", "rightalt": "alt"}
_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def key_identity(key):
    """Normalize HT key chords such as ``alt 6`` / ``Alt+6`` for conflict checks."""
    tokens = str(key).replace("＋", "+").replace("+", " ").replace("　", " ").casefold().split()
    if len(tokens) >= 2 and tokens[0] in _KEY_MODIFIERS:
        return _KEY_MODIFIERS[tokens[0]] + " " + "".join(tokens[1:])
    return "".join(tokens)


def validate_features(features, part_ids):
    """A state with no parts is an explicit all-hidden option (LoyalTools empty input)."""
    features = _records(features, "features")
    ids, keys, variables, owners = [], set(), set(), {}
    for feature in features:
        fid = _text(feature.get("id"), "功能 ID")
        ids.append(fid)
        name = feature.get("label") or fid
        _require(feature.get("type") == "visibility_cycle", f"功能 {name}: 未支持的功能类型")
        states = _records(feature.get("states"), f"功能 {name} states")
        hidden = feature.get("include_hidden_state", False)
        _require(type(hidden) is bool, f"功能 {name}: 全隐藏必须为布尔值")
        _require(len(states) + int(hidden) >= 2, f"功能 {name}: 至少需要两个选项")
        sids, members = [], []
        for state in states:
            sid = _text(state.get("id"), f"功能 {name} 状态 ID")
            _require(sid != HIDDEN_STATE, f"功能 {name}: {HIDDEN_STATE} 为保留状态 ID")
            sids.append(sid)
            parts = state.get("parts")
            _require(isinstance(parts, list), f"功能 {name} 状态 {sid}: 部件列表无效")
            _require(all(isinstance(p, str) and p in part_ids for p in parts),
                     f"功能 {name} 状态 {sid}: 引用了不存在的部件")
            members.extend(parts)
        _require(bool(members), f"功能 {name}: 所有选项都是空的，至少连接一个物体")
        _unique(sids, f"功能 {name} 状态 ID")
        _unique(members, f"功能 {name} 部件（每个部件只能属于一个选项）")
        initial = feature.get("initial_state_id")
        _require(initial in sids or (hidden and initial == HIDDEN_STATE), f"功能 {name}: 初始状态不存在")
        key = _text(feature.get("key"), f"功能 {name} 按键")
        identity = key_identity(key)
        _require(identity not in keys, f"功能 {name}: 按键 {key} 与其他切换冲突")
        keys.add(identity)
        variable = feature.get("variable", "")
        if variable:
            _require(isinstance(variable, str) and _PARAMETER.fullmatch(variable),
                     f"功能 {name}: 变量名只能使用字母、数字和下划线，且不能以数字开头")
            _require(variable.casefold() not in variables, f"功能 {name}: 变量名 {variable} 与其他切换重复")
            variables.add(variable.casefold())
        for pid in members:
            _require(pid not in owners, f"功能 {name}: 部件 {pid} 已由功能 {owners.get(pid)} 控制")
            owners[pid] = name
    _unique(ids, "功能 ID")
    return features


def ht_material_ids(feature, slot_map):
    """Return (HT Material ID(s) text, initial state index) or (None, reason).

    HT only appends hide-all at the end. A single empty option anywhere is
    representable by rotating the cycle; its initial state follows the rotation.
    """
    states = [state["parts"] for state in feature["states"]]
    ids = [state["id"] for state in feature["states"]]
    if feature.get("include_hidden_state"):
        states.append([])
        ids.append(HIDDEN_STATE)
    hidden = [index for index, parts in enumerate(states) if not parts]
    if len(hidden) > 1:
        return None, "HT 工具只支持一个空选项"
    if any(part not in slot_map for parts in states for part in parts):
        return None, "发送到 UE 后显示槽号"
    groups = ["+".join(str(slot_map[part]) for part in parts) for parts in states]
    initial = ids.index(feature["initial_state_id"]) if feature["initial_state_id"] in ids else 0
    if not hidden:
        return ";".join(groups), initial
    empty = hidden[0]
    order = list(range(empty + 1, len(states))) + list(range(empty))
    return ",".join(groups[index] for index in order), (order + [empty]).index(initial)


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
    export_targets = dict(asset_targets)
    texture_targets = {}
    for tex in _records(data.get("textures", []), "textures"):
        asset_ids.append(_text(tex.get("id"), "贴图 ID"))
        path = package_path(tex.get("asset_path"), "贴图路径")
        _require(path.casefold() not in asset_targets and path.casefold() not in reference_targets,
                 f"重复或冲突的资源路径: {path}")
        asset_targets[path.casefold()] = (path, "Texture2D")
        origin = tex.get("origin", "mod")
        _require(origin in ("mod", "preview"), f"贴图来源无效: {path}")
        if origin == "mod":
            export_targets[path.casefold()] = (path, "Texture2D")
        texture_targets[path.casefold()] = tex
        texture_settings(tex.get("role"))
        sources.append(_text(tex.get("source_file"), "贴图文件"))
    material_specs = {}
    for spec in _records(data.get("materials", []), "materials"):
        mid = _text(spec.get("id"), "材质节点 ID")
        path = package_path(spec.get("asset_path"), "材质实例路径")
        kind = spec.get("kind")
        _require(kind in ("new", "existing"), f"材质 {path}: 来源无效")
        _require(path.casefold() not in material_specs, f"材质实例路径重复: {path}")
        _require(path.casefold() in {p.casefold() for p in material_paths}, f"材质实例未被任何部件使用: {path}")
        _require(path.casefold() not in asset_targets and path.casefold() not in {
            p.casefold() for p in (skeleton, physics) if p}, f"材质实例路径与其他资源冲突: {path}")
        if kind == "new":
            parent = package_path(spec.get("parent_path"), f"材质 {path} 母材质路径")
            _require(parent.casefold() != path.casefold(), f"材质 {path}: 母材质不能是自己")
            params = spec.get("textures")
            _require(isinstance(params, dict), f"材质 {path}: 贴图参数无效")
            for param, texture in params.items():
                _require(isinstance(param, str) and _PARAMETER.fullmatch(param), f"材质 {path}: 参数名无效: {param}")
                _require(isinstance(texture, str) and texture.casefold() in texture_targets
                         and texture_targets[texture.casefold()].get("origin", "mod") == "mod",
                         f"材质 {path} 参数 {param}: 贴图未在任务中声明")
            export_targets[path.casefold()] = (path, "MaterialInstanceConstant")
        material_specs[path.casefold()] = (mid, kind)
    _unique([spec.get("id") for spec in data.get("materials", [])], "材质节点 ID")
    preview_materials = []
    for preview in _records(data.get("material_previews", []), "material_previews"):
        material = package_path(preview.get("material_path"), "预览材质路径")
        texture = package_path(preview.get("texture_path"), "预览漫射贴图路径")
        _require(material.casefold() in {p.casefold() for p in material_paths},
                 f"预览材质未被本次网格引用: {material}")
        _require(material.casefold() not in material_specs, f"材质实例不使用漫射预览: {material}")
        _require(texture.casefold() in texture_targets, f"预览贴图未在任务中声明: {texture}")
        _require(texture_targets[texture.casefold()]["role"] == "BASE_COLOR",
                 f"材质漫射预览需要 BASE_COLOR 贴图: {texture}")
        preview_materials.append(material.casefold())
    _unique(preview_materials, "预览材质")
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
        _require(path.casefold() in export_targets, f"资源未在本任务声明为网格、替换贴图或新建材质实例（预览贴图不打包）: {path}")
        _require(asset.get("asset_type") == export_targets[path.casefold()][1],
                 f"打包资源类型错误: {path}")
    _unique(export_paths, "打包资源路径")
    _require(set(export_paths) == set(export_targets), "打包清单必须包含本次网格、全部明确替换贴图和新建材质实例")
    return data


NODE_TYPES = {"OBJECT", "MATERIAL", "SWITCH", "GROUP", "OUTPUT"}
_FLOW_SOURCES = {"OBJECT", "GROUP", "SWITCH"}
_FLOW_TARGETS = {"GROUP", "SWITCH", "OUTPUT"}


def compile_blueprint(graph):
    """Resolve the LoyalTools-style blueprint into exported parts, materials and switches.

    Parts reach the generate node directly (always visible) or through exactly one
    switch option. An unlinked switch option is an explicit all-hidden option.
    Objects that do not reach the generate node are not exported.
    """
    _require(isinstance(graph, dict), "节点图必须是对象")
    _text(graph.get("id"), "节点图 ID")
    node_list = _records(graph.get("nodes"), "nodes")
    links = _records(graph.get("links"), "links")
    ids = [_text(node.get("id"), "节点 ID") for node in node_list]
    _unique(ids, "节点 ID（复制节点需要新的标识）")
    nodes = dict(zip(ids, node_list))

    def name(nid):
        node = nodes[nid]
        return node.get("label") or node.get("type", "节点")

    owner = {}
    for nid, node in nodes.items():
        _require(node.get("type") in NODE_TYPES, f"节点「{name(nid)}」: 未知类型")
        if node["type"] == "OBJECT":
            _require(node.get("object_id"), f"节点「{name(nid)}」: 请选择物体")
            parts = node.get("parts")
            _require(isinstance(parts, list) and bool(parts), f"节点「{name(nid)}」: 物体没有材质槽")
            _unique(parts, f"节点「{name(nid)}」部件")
            _require(set(node.get("split", [])) <= set(parts), f"节点「{name(nid)}」: 拆出的槽已不存在")
            for part in parts:
                _require(part not in owner, f"节点「{name(nid)}」: 与「{name(owner.get(part, nid))}」引用了同一个物体")
                owner[part] = nid
    outputs = [nid for nid, node in nodes.items() if node["type"] == "OUTPUT"]
    _require(len(outputs) == 1, "蓝图必须有且只有一个生成节点")
    mains = [nid for nid, node in nodes.items() if node["type"] == "OBJECT" and node.get("main")]
    _require(len(mains) <= 1, "蓝图只能有一个主网格物体节点")

    flow, materials, used_sockets = {nid: [] for nid in nodes}, {}, set()
    for link in links:
        src, dst = link.get("from_node"), link.get("to_node")
        _require(src in nodes and dst in nodes, "连线引用了不存在的节点")
        socket = _text(link.get("to_socket"), f"节点「{name(dst)}」输入插口")
        source_socket = link.get("from_socket") or "out"
        _require((dst, socket) not in used_sockets, f"节点「{name(dst)}」插口 {socket}: 只能连接一条线")
        used_sockets.add((dst, socket))
        src_type, dst_type = nodes[src]["type"], nodes[dst]["type"]
        if src_type == "MATERIAL":
            _require(dst_type == "OBJECT" and socket in nodes[dst]["parts"],
                     f"材质节点「{name(src)}」只能连接物体节点左侧的材质入口")
            materials[socket] = src
            continue
        _require(src_type in _FLOW_SOURCES and dst_type in _FLOW_TARGETS,
                 f"节点「{name(src)}」→「{name(dst)}」: 连线类型不兼容")
        if src_type == "OBJECT":
            _require(source_socket == "all" or source_socket in nodes[src].get("split", []),
                     f"节点「{name(src)}」: 输出插口已失效，请重新连接")
        if dst_type == "SWITCH":
            _require(socket in nodes[dst].get("options", []), f"切换「{name(dst)}」: 选项插口已失效")
        flow[dst].append((socket, src, source_socket))

    active, done = set(), set()
    def visit(nid):
        _require(nid not in active, f"节点「{name(nid)}」: 连线形成循环")
        if nid in done:
            return
        active.add(nid)
        for _, src, _ in flow[nid]:
            visit(src)
        active.remove(nid)
        done.add(nid)
    for nid in nodes:
        visit(nid)

    placement, option_parts, reached_sockets, switches = {}, {}, {}, []
    def where(context):
        if context is None:
            return "常驻"
        return "切换「%s」选项_%d" % (name(context[0]), context[1])

    def collect(nid, socket, context):
        node = nodes[nid]
        if node["type"] == "OBJECT":
            reached_sockets.setdefault(nid, set()).add(socket)
            split = node.get("split", [])
            parts = [part for part in node["parts"] if part not in split] if socket == "all" else [socket]
            for part in parts:
                _require(part not in placement, "物体「%s」的部件同时出现在%s和%s" % (
                    name(nid), where(placement.get(part)), where(context)))
                placement[part] = context
                if context is not None:
                    option_parts[context].append(part)
        elif node["type"] == "GROUP":
            for _, src, src_socket in flow[nid]:
                collect(src, src_socket, context)
        elif node["type"] == "SWITCH":
            _require(context is None, f"切换「{name(nid)}」不能放在另一个切换的选项里")
            _require(nid not in {item[0] for item in switches}, f"切换「{name(nid)}」连接到生成节点多次")
            options = node.get("options")
            _require(isinstance(options, list) and len(options) >= 2, f"切换「{name(nid)}」: 至少需要两个选项")
            _unique(options, f"切换「{name(nid)}」选项")
            switches.append((nid, options))
            by_socket = {socket_id: (src, src_socket) for socket_id, src, src_socket in flow[nid]}
            for index, socket_id in enumerate(options):
                option_parts[(nid, index)] = []
                if socket_id in by_socket:
                    collect(by_socket[socket_id][0], by_socket[socket_id][1], (nid, index))

    output = outputs[0]
    for _, src, src_socket in flow[output]:
        collect(src, src_socket, None)
    if mains:
        _require(mains[0] in reached_sockets, "主网格物体节点未连接到生成节点")
    included = {}
    for nid, sockets in reached_sockets.items():
        node = nodes[nid]
        for part in node.get("split", []):
            _require(part in placement, f"物体「{name(nid)}」拆出的槽尚未连接")
        included[nid] = [part for part in node["parts"] if part in placement]
    _require(bool(placement), "生成节点没有连接任何物体")

    features = []
    for order, (nid, options) in enumerate(switches):
        node = nodes[nid]
        states = []
        for index, socket_id in enumerate(options):
            states.append({"id": nid + "/" + socket_id, "label": "选项_%d" % index,
                           "parts": option_parts[(nid, index)]})
        initial = node.get("initial_option", 0)
        _require(type(initial) is int and 0 <= initial < len(states), f"切换「{name(nid)}」: 初始选项不存在")
        label = node.get("comment") or node.get("variable") or name(nid)
        features.append({"id": nid, "type": "visibility_cycle", "label": label,
                         "comment": node.get("comment", ""), "variable": node.get("variable", ""),
                         "key": node.get("key", ""), "states": states, "include_hidden_state": False,
                         "initial_state_id": states[initial]["id"],
                         "panel": {"enabled": True, "label": label, "order": order}})
    validate_features(features, set(placement))
    part_materials = {part: materials.get(part) for part in placement}
    used = {mid for mid in part_materials.values() if mid}
    return {"graph_id": graph["id"], "objects": included, "main": mains[0] if mains else "",
            "part_materials": part_materials,
            "materials": {mid: nodes[mid] for mid in sorted(used)}, "features": features}
