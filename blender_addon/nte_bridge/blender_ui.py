"""Small, purpose-built node editor and explicit single-character profile."""

from pathlib import Path
import json
import re
import subprocess

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from .blender_export import finish_job, graph_dict, new_id, prepare_job, profile_manifest
from .core import BridgeError, write_json
from .discovery import scan_character
from .workflow import detect_engine_dir, detect_packager_source
from .blender_cache import _directory, ensure_cache, selected_manifest
from .blender_packaging import (TYPE_ITEMS, TYPE_LABELS, TYPE_ICONS, asset_dialog_width, asset_visible, character_folder,
    clear_cook, cook_excluded_assets, default_character_folder, load_cooked_assets, resolved_project,
    selection_request, size_label, verified_cook, visible_assets, worker_python)

_ENUM_CACHE = {}


def _mesh_poll(self, obj):
    return obj.type == 'MESH'


def _rig_poll(self, obj):
    return obj.type == 'ARMATURE'


def _catalog_changed(part, context):
    settings = getattr(getattr(context, 'scene', None), 'nte_bridge', None)
    entry = settings.material_catalog.get(part.catalog_material) if settings else None
    if entry:
        part.automatic_material_path = ''
        part.material_path = entry.asset_path


def _material_path_changed(part, context):
    if part.automatic_material_path and part.material_path != part.automatic_material_path:
        part.automatic_material_path = ''


class NTEBridgePartEntry(bpy.types.PropertyGroup):
    part_id: StringProperty()
    source_slot: IntProperty(min=0)
    display_name: StringProperty(name="部件名称")
    material_path: StringProperty(name="UE 材质路径", update=_material_path_changed,
        description="完整 /Game/... 包路径；游戏原材质只占位，不打包")
    automatic_material_path: StringProperty(options={'HIDDEN'})
    source_material: PointerProperty(type=bpy.types.Material)
    catalog_material: StringProperty(name="选择角色材质", update=_catalog_changed)


class NTEBridgeSourceMeshEntry(bpy.types.PropertyGroup):
    asset_path: StringProperty()
    display_name: StringProperty()


class NTEBridgeMaterialEntry(bpy.types.PropertyGroup):
    asset_path: StringProperty()
    display_name: StringProperty()
    asset_type: StringProperty()
    parent_path: StringProperty()
    root_material_path: StringProperty()
    available: BoolProperty()
    metadata_json: StringProperty()


class NTEBridgeTextureEntry(bpy.types.PropertyGroup):
    texture_id: StringProperty()
    file_path: StringProperty(name="源贴图", subtype='FILE_PATH')
    asset_path: StringProperty(name="UE 贴图路径")
    role: EnumProperty(name="用途", items=[
        ('BASE_COLOR', '漫射 · BC7 / sRGB', ''), ('ID_TEX', 'ID · BC7 / 线性', ''),
        ('LIGHT_MAP', 'LightMap · BC7 / 线性', ''), ('NORMAL', '法线 · BC5 / 线性', '')])


class NTEBridgeCookedAssetEntry(bpy.types.PropertyGroup):
    asset_path: StringProperty()
    asset_type: StringProperty()
    size_bytes: StringProperty()
    size_text: StringProperty()
    selected: BoolProperty(name='导出此资产', default=False)
    packable: BoolProperty(default=False)
    dependency: BoolProperty(default=False)
    reason: StringProperty()


class NTEBridgeStateEntry(bpy.types.PropertyGroup):
    state_id: StringProperty()
    label: StringProperty(name="状态名")


class NTEBridgeSocket(bpy.types.NodeSocket):
    bl_idname = 'NTEBridgeSocket'
    bl_label = 'NTE 部件 / 状态'
    socket_id: StringProperty()

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.13, 0.65, 0.62, 1.0)


class NTEBridgeTree(bpy.types.NodeTree):
    bl_idname = 'NTEBridgeTree'
    bl_label = 'NTE Bridge · 角色功能图'
    bl_icon = 'NODETREE'
    graph_id: StringProperty()


def _socket(node, output, label, socket_id=None):
    socket = (node.outputs if output else node.inputs).new('NTEBridgeSocket', label)
    socket.socket_id = socket_id or new_id()
    if not output:
        socket.link_limit = 1
    return socket


class _NTEBase:
    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == 'NTEBridgeTree'

    def copy(self, source):
        self.node_id = new_id()
        if self.bl_idname == 'NTEBridgeCycle':
            old_initial = self.initial_state_id
            for state, socket in zip(self.states, self.inputs):
                old_id = state.state_id
                state.state_id = new_id()
                socket.socket_id = state.state_id
                if old_initial == old_id:
                    self.initial_state_id = state.state_id


def _part_items(node, context):
    settings = getattr(getattr(context, 'scene', None), 'nte_bridge', None)
    items = [(p.part_id, '%d · %s' % (p.source_slot, p.display_name), '', i)
             for i, p in enumerate(settings.parts)] if settings else []
    if not items:
        items = [('NONE', '请先刷新角色部件槽', '', 0)]
    _ENUM_CACHE[('part', node.as_pointer())] = items
    return items


def _part_get(node):
    settings = getattr(bpy.context.scene, 'nte_bridge', None)
    if settings:
        for i, part in enumerate(settings.parts):
            if part.part_id == node.part_id:
                return i
    return 0


def _part_set(node, value):
    settings = bpy.context.scene.nte_bridge
    if 0 <= value < len(settings.parts):
        part = settings.parts[value]
        node.part_id = part.part_id
        node.label = part.display_name


class NTEBridgePart(_NTEBase, bpy.types.Node):
    bl_idname = 'NTEBridgePart'
    bl_label = '部件'
    node_id: StringProperty()
    part_id: StringProperty()
    part_choice: EnumProperty(name="角色部件", items=_part_items, get=_part_get, set=_part_set)

    def init(self, context):
        self.node_id = new_id()
        self.width = 235
        _socket(self, True, '部件')

    def draw_buttons(self, context, layout):
        layout.prop(self, 'part_choice', text='')
        if not self.part_id:
            layout.label(text='请选择部件', icon='ERROR')


class NTEBridgeGroup(_NTEBase, bpy.types.Node):
    bl_idname = 'NTEBridgeGroup'
    bl_label = '组合 / 一个状态'
    node_id: StringProperty()

    def init(self, context):
        self.node_id = new_id()
        self.width = 200
        _socket(self, False, '部件 1')
        _socket(self, True, '组合')

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            _socket(self, False, '部件 %d' % (len(self.inputs) + 1))

    def draw_buttons(self, context, layout):
        layout.prop(self, 'label', text='名称')


def _initial_items(node, context):
    items = [(state.state_id, state.label or '状态 %d' % (i + 1), '', i)
             for i, state in enumerate(node.states)]
    if node.include_hidden:
        items.append(('$hidden', '全部隐藏', '', len(items)))
    _ENUM_CACHE[('initial', node.as_pointer())] = items
    return items


def _initial_get(node):
    for i, state in enumerate(node.states):
        if state.state_id == node.initial_state_id:
            return i
    if node.initial_state_id == '$hidden' and node.include_hidden:
        return len(node.states)
    return 0


def _initial_set(node, value):
    if value < len(node.states):
        node.initial_state_id = node.states[value].state_id
    elif node.include_hidden and value == len(node.states):
        node.initial_state_id = '$hidden'


def _hidden_changed(node, context):
    if not node.include_hidden and node.initial_state_id == '$hidden' and node.states:
        node.initial_state_id = node.states[0].state_id


class NTEBridgeCycle(_NTEBase, bpy.types.Node):
    bl_idname = 'NTEBridgeCycle'
    bl_label = '材质组循环'
    node_id: StringProperty()
    states: CollectionProperty(type=NTEBridgeStateEntry)
    feature_label: StringProperty(name="功能名称", default='服装切换')
    key: StringProperty(name="按键", default='K')
    include_hidden: BoolProperty(name="追加全部隐藏", default=False, update=_hidden_changed)
    initial_state_id: StringProperty()
    initial_choice: EnumProperty(name="初始状态", items=_initial_items, get=_initial_get, set=_initial_set)

    def add_state(self):
        state = self.states.add()
        state.state_id = new_id()
        state.label = '状态 %d' % len(self.states)
        _socket(self, False, state.label, state.state_id)
        if not self.initial_state_id:
            self.initial_state_id = state.state_id

    def init(self, context):
        self.node_id = new_id()
        self.width = 255
        self.add_state()
        self.add_state()
        _socket(self, True, '功能')

    def draw_buttons(self, context, layout):
        layout.prop(self, 'feature_label')
        layout.prop(self, 'key')
        layout.prop(self, 'include_hidden')
        layout.prop(self, 'initial_choice')
        row = layout.row(align=True)
        for action, text in [('ADD', '增加状态'), ('REMOVE', '删除末状态')]:
            op = row.operator('nte_bridge.edit_state', text=text)
            op.tree_name = self.id_data.name
            op.node_name = self.name
            op.action = action
        layout.label(text='配置预览，暂不生成游戏切换', icon='INFO')


class NTEBridgeOutput(_NTEBase, bpy.types.Node):
    bl_idname = 'NTEBridgeOutput'
    bl_label = '角色输出'
    node_id: StringProperty()

    def init(self, context):
        self.node_id = new_id()
        self.width = 260
        _socket(self, False, '功能 1')

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            _socket(self, False, '功能 %d' % (len(self.inputs) + 1))

    def draw_buttons(self, context, layout):
        layout.label(text='网格与贴图由角色配置导出')
        layout.label(text='没有切换功能时可保持不连接')
        layout.operator('nte_bridge.validate', text='校验并预览配置', icon='CHECKMARK')
        layout.operator('nte_bridge.send', text='发送到 UE', icon='EXPORT')


def _source_data(settings):
    try:
        return json.loads(settings.source_data) if settings.source_data else {}
    except (ValueError, TypeError):
        return {}


def _selected_source(settings):
    return next((mesh for mesh in _source_data(settings).get('meshes', [])
                 if mesh['asset_path'] == settings.applied_source_mesh), None)


def _resolved_folder(path):
    value = path.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return str(Path(bpy.path.abspath(value)).resolve()) if value else ''


def _ensure_source_current(settings):
    if not settings.source_folder.strip():
        return  # Existing profiles and manual advanced configuration remain supported.
    data = _source_data(settings)
    if _resolved_folder(settings.source_folder).casefold() != str(data.get('folder', '')).casefold():
        raise BridgeError('角色文件夹已改变或尚未读取，请先读取角色。')
    candidate = settings.source_meshes.get(settings.source_mesh_choice)
    if not candidate or candidate.asset_path != settings.applied_source_mesh:
        raise BridgeError('请选择并使用一个原始骨骼网格，再发送到 UE。')


def _fill_material_mappings(settings):
    """Only fill empty mappings with an unambiguous name match, never slot order."""
    source = _selected_source(settings)
    if not source:
        return 0
    aliases = {}
    for slot in source.get('slots', []):
        path = slot.get('material_path', '')
        if not path:
            continue
        for name in (slot.get('name', ''), path.rsplit('/', 1)[-1]):
            if name:
                aliases.setdefault(name, set()).add(path)
    filled = 0
    for part in settings.parts:
        if part.material_path:
            continue
        name = part.source_material.name if part.source_material else part.display_name
        paths = aliases.get(name, set())
        if not paths:
            # Blender's duplicate suffix is safe only when the full name is not
            # itself a source name and the remaining name has one target path.
            base = re.sub(r'\.\d{3,}$', '', name)
            paths = aliases.get(base, set()) if base != name else set()
        if len(paths) == 1:
            part.material_path = next(iter(paths))
            entry = next((item for item in settings.material_catalog
                          if item.asset_path == part.material_path), None)
            if entry:
                part.catalog_material = entry.name
            part.automatic_material_path = part.material_path
            filled += 1
    return filled


def _refresh_parts(settings):
    if not settings.mesh:
        raise BridgeError('请先选择 Blender 角色网格。')
    same_mesh = settings.bound_mesh == settings.mesh
    rig = settings.mesh.find_armature()
    if not same_mesh:
        settings.character_id = new_id()
        settings.mesh_id = new_id()
        settings.graph = None
        settings.last_manifest = ''
        settings.last_report = ''
        settings.armature = rig
    settings.armature = rig
    settings.character_id = settings.character_id or new_id()
    settings.mesh_id = settings.mesh_id or new_id()
    old = [(p.source_slot, p.source_material, p.part_id, p.display_name,
            p.material_path, p.catalog_material, p.automatic_material_path) for p in settings.parts] if same_mesh else []
    retained, used = [], set()
    for index, slot in enumerate(settings.mesh.material_slots):
        candidates = [i for i, item in enumerate(old) if i not in used and item[1] == slot.material]
        at_index = next((i for i in candidates if old[i][0] == index), None)
        if at_index is not None:
            match = at_index
        elif len(candidates) == 1:
            match = candidates[0]
        elif candidates:
            raise BridgeError('重复材质槽的顺序已改变，无法确定部件身份；请恢复槽顺序后再刷新。')
        else:
            match = None
        if match is not None:
            used.add(match)
            retained.append(old[match][2:])
        else:
            # A replaced material can retain its slot identity, but no old
            # mapping. Never take an identity whose material moved elsewhere.
            replaced = next((i for i, item in enumerate(old)
                             if i not in used and item[0] == index
                             and not any(item[1] == current.material for current in settings.mesh.material_slots)), None)
            if replaced is not None:
                used.add(replaced)
                item = old[replaced]
                label = item[3]
                if item[1] and label == item[1].name:
                    label = slot.name or '部件 %d' % index
                retained.append((item[2], label, '', '', ''))
            else:
                retained.append((new_id(), slot.name or '部件 %d' % index, '', '', ''))
    settings.parts.clear()
    for index, slot in enumerate(settings.mesh.material_slots):
        part = settings.parts.add()
        part.part_id, part.display_name, path, choice, automatic = retained[index]
        part.name = part.part_id
        part.source_slot = index
        part.source_material = slot.material
        part.catalog_material = choice
        part.material_path = path  # Retain an explicit manual override over the catalog selection.
        part.automatic_material_path = automatic
    settings.active_part = min(settings.active_part, max(0, len(settings.parts) - 1))
    settings.bound_mesh = settings.mesh
    if not settings.graph:
        settings.graph = bpy.data.node_groups.new('NTE · ' + settings.mesh.name, 'NTEBridgeTree')
        settings.graph.graph_id = new_id()
        settings.graph.use_fake_user = True
    valid_ids = {part.part_id for part in settings.parts}
    for node in list(settings.graph.nodes):
        if (node.bl_idname == 'NTEBridgePart' and node.part_id not in valid_ids
                and not any(socket.is_linked for socket in node.outputs)):
            settings.graph.nodes.remove(node)
    existing = {n.part_id for n in settings.graph.nodes if n.bl_idname == 'NTEBridgePart'}
    for index, part in enumerate(settings.parts):
        if part.part_id not in existing:
            node = settings.graph.nodes.new('NTEBridgePart')
            node.part_id = part.part_id
            node.label = part.display_name
            node.location = (0, -index * 95)
    if not any(n.bl_idname == 'NTEBridgeOutput' for n in settings.graph.nodes):
        node = settings.graph.nodes.new('NTEBridgeOutput')
        node.location = (720, 0)
    _fill_material_mappings(settings)


def _mesh_changed(settings, context):
    if settings.mesh and not settings.busy:
        try:
            _refresh_parts(settings)
            settings.status = '已读取 Blender 网格；展开材质与部件核对映射。'
        except Exception as error:
            settings.status = str(error)


def _cache_changed(settings, context):
    clear_cook(settings)
    settings.last_manifest = ''
    settings.last_report = ''
    settings.job_root = ''
    if settings.cache_root.strip():
        try:
            settings.job_root = str(_directory(settings.cache_root) / 'Jobs')
        except BridgeError:
            pass


def _cook_context_changed(settings, context):
    clear_cook(settings)


def _cook_role_changed(settings, context):
    # Changing characters must never silently retain another role's override.
    settings.cook_use_custom = False
    clear_cook(settings)


@persistent
def _restore_cache(_unused=None):
    for scene in bpy.data.scenes:
        settings = scene.nte_bridge
        try:
            ensure_cache(settings)
        except (BridgeError, OSError):
            settings.job_root = ''
            settings.last_manifest = ''
            settings.last_report = ''
            clear_cook(settings)
        if settings.cook_report:
            try:
                verified_cook(settings)
            except (BridgeError, OSError, ValueError, KeyError):
                clear_cook(settings)


def _initialize_cache():
    # Blender restricts bpy.data while enabling an addon. Migrate loaded scenes
    # after registration has left that context; load_post covers opened files.
    _restore_cache()
    return None


class NTEBridgeSettings(bpy.types.PropertyGroup):
    character_id: StringProperty()
    mesh_id: StringProperty()
    mesh: PointerProperty(name="Blender 网格", type=bpy.types.Object, poll=_mesh_poll, update=_mesh_changed)
    bound_mesh: PointerProperty(type=bpy.types.Object)
    armature: PointerProperty(name="角色骨架", type=bpy.types.Object, poll=_rig_poll)
    graph: PointerProperty(name="节点图", type=bpy.types.NodeTree)
    source_folder: StringProperty(name="解包的角色文件夹", subtype='DIR_PATH', update=_cook_role_changed,
        description="例如 E:\\NTE mods\\078_Nitsa；读取其中导出的 JSON 资源信息")
    source_data: StringProperty(options={'HIDDEN'})
    source_meshes: CollectionProperty(type=NTEBridgeSourceMeshEntry)
    source_mesh_choice: StringProperty(name="原始骨骼网格")
    applied_source_mesh: StringProperty()
    material_catalog: CollectionProperty(type=NTEBridgeMaterialEntry)
    catalog_choice: StringProperty(name="角色材质与母材质")
    discovery_status: StringProperty()
    project_file: StringProperty(name="UE 工程", subtype='FILE_PATH', update=_cook_context_changed)
    mesh_path: StringProperty(name="网格包路径", description="如 /Game/Characters/Player/Test/Test", update=_cook_role_changed)
    skeleton_path: StringProperty(name="骨架包路径")
    physics_path: StringProperty(name="物理资产路径", description="可留空；指定时作为占位资源，不打包")
    create_placeholders: BoolProperty(name="自动创建缺失的原资源占位", default=False,
        description="读取并使用新角色时自动开启：在原路径补齐材质、骨架和物理资产引用；占位资源不会打包")
    parts: CollectionProperty(type=NTEBridgePartEntry)
    active_part: IntProperty(min=0)
    textures: CollectionProperty(type=NTEBridgeTextureEntry)
    active_texture: IntProperty(min=0)
    cache_root: StringProperty(name="缓存目录", subtype='DIR_PATH', update=_cache_changed,
        options={'PATH_SUPPORTS_BLEND_RELATIVE'},
        description="统一存放桥接任务、FBX、临时文件与报告；更改后需重新导出，随 Blender 工程保存")
    job_root: StringProperty(name="桥接任务目录", subtype='DIR_PATH', options={'HIDDEN'})
    engine_dir: StringProperty(name="UE 安装目录", subtype='DIR_PATH')
    sync_mode: EnumProperty(name="同步方式", default='commandlet',
        description="后台导入要求目标工程关闭；自动启动 UE 命令行编辑器，完成后退出",
        items=[('commandlet', '后台导入（无需打开 UE）', '目标工程必须关闭；自动启动后台 UE 并在导入完成后退出', 1),
               ('remote', '发送到已打开的 UE', '可选：需要目标工程启用本机 Python 远程执行', 0)])
    packager_source: StringProperty(name="外部打包器目录", subtype='DIR_PATH',
        description="包含 NteMorphTargetPatch.exe、retoc.exe 和 Oodle DLL 的目录")
    package_output: StringProperty(name="Mod 输出目录", subtype='DIR_PATH')
    mod_name: StringProperty(name="Mod 名称", default='NTEBridgeMod')
    cook_use_custom: BoolProperty(name='自定义 UE 烘焙目录', default=False, update=_cook_context_changed,
        description='默认烘焙当前角色的完整目录；开启后可选择工程中的其他目录')
    cook_folder: StringProperty(name='UE 烘焙目录', update=_cook_context_changed,
        description='完整 UE 资源目录，例如 /Game/Characters/Player/078_Nitsa')
    cook_report: StringProperty(name='最近烘焙清单', subtype='FILE_PATH')
    cook_report_sha256: StringProperty(options={'HIDDEN'})
    cook_assets: CollectionProperty(type=NTEBridgeCookedAssetEntry)
    cook_active_asset: IntProperty(min=0)
    cook_search: StringProperty(name='搜索资产', description='按名称、类型或资源路径搜索')
    cook_type: EnumProperty(name='资产类型', items=TYPE_ITEMS, default='ALL')
    cook_show_dependencies: BoolProperty(name='显示目录外依赖', default=False,
        description='显示烘焙时生成的共享资产及引擎依赖；不可打包的引用仍禁止勾选')
    cook_export_directory: StringProperty(name='烘焙资产导出目录', subtype='DIR_PATH',
        default='D:/Neverness to Everness Mod Loader/cook/packager/xg/HT/Content/Characters',
        description='角色文件夹直接导出到此目录，如 Characters/078_Nitsa；保留角色内部子目录，随后自动打包')
    last_manifest: StringProperty(name="最近任务", subtype='FILE_PATH')
    last_report: StringProperty(name="最近报告", subtype='FILE_PATH')
    status: StringProperty(default='先读取解包的角色文件夹，再选择 Blender 网格。')
    busy: BoolProperty(default=False, options={'SKIP_SAVE'})


class NTEBRIDGE_OT_scan_character(bpy.types.Operator):
    bl_idname = 'nte_bridge.scan_character'
    bl_label = '读取角色'
    bl_description = '从解包 JSON 读取网格、骨架、物理资产以及材质引用和参数'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            if not settings.source_folder.strip():
                raise BridgeError('请选择解包的角色文件夹。')
            result = scan_character(_resolved_folder(settings.source_folder))
            if not result.get('meshes'):
                raise BridgeError('文件夹中未找到可识别的骨骼网格 JSON，请检查解包导出内容。')
            settings.source_data = json.dumps(result, ensure_ascii=False)
            old_choice = settings.source_mesh_choice
            settings.source_meshes.clear()
            for mesh in result['meshes']:
                entry = settings.source_meshes.add()
                entry.asset_path = mesh['asset_path']
                entry.display_name = mesh['name']
                entry.name = mesh['name'] + '  |  ' + mesh['asset_path']
            settings.material_catalog.clear()
            for material in sorted(result.get('materials', []), key=lambda item: (not item.get('available', False), item['asset_path'])):
                entry = settings.material_catalog.add()
                entry.asset_path = material['asset_path']
                entry.display_name = material['name']
                entry.name = material['name'] + '  |  ' + material['asset_path']
                entry.asset_type = material.get('type', '')
                entry.parent_path = material.get('parent_path', '')
                entry.root_material_path = material.get('root_material_path', '')
                entry.available = material.get('available', False)
                entry.metadata_json = json.dumps(material, ensure_ascii=False)
            if len(settings.source_meshes) == 1:
                settings.source_mesh_choice = settings.source_meshes[0].name
                _apply_source(settings)
            elif settings.source_meshes.get(old_choice):
                settings.source_mesh_choice = old_choice
                if settings.source_meshes[old_choice].asset_path == settings.applied_source_mesh:
                    _apply_source(settings)
            else:
                settings.source_mesh_choice = ''
            loaded = sum(item.available for item in settings.material_catalog)
            settings.discovery_status = '%d 个网格 · %d 份材质信息' % (len(settings.source_meshes), loaded)
            settings.status = ('已读取角色；请选择原始骨骼网格并点击使用。'
                               if not settings.source_mesh_choice else '角色信息已更新；已有部件和手动映射已保留。')
            self.report({'INFO'}, settings.discovery_status)
            return {'FINISHED'}
        except Exception as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def _apply_source(settings):
    entry = settings.source_meshes.get(settings.source_mesh_choice)
    source = next((item for item in _source_data(settings).get('meshes', [])
                   if entry and item['asset_path'] == entry.asset_path), None)
    if not source:
        raise BridgeError('请先选择读取到的原始骨骼网格。')
    if not source.get('skeleton_path'):
        raise BridgeError('该网格 JSON 缺少骨架引用，请补充解包信息或使用高级手动配置。')
    for part in settings.parts:
        if part.automatic_material_path and part.material_path == part.automatic_material_path:
            part.material_path = ''
            part.catalog_material = ''
            part.automatic_material_path = ''
    same_source = settings.applied_source_mesh == source['asset_path']
    if not same_source:
        settings.create_placeholders = True
    for prop, key in [('mesh_path', 'asset_path'), ('skeleton_path', 'skeleton_path'),
                      ('physics_path', 'physics_path')]:
        if not same_source or not getattr(settings, prop):
            setattr(settings, prop, source.get(key, ''))
    settings.applied_source_mesh = source['asset_path']
    if not same_source:
        settings.last_manifest = ''
        settings.last_report = ''
    filled = _fill_material_mappings(settings)
    settings.status = '已使用 %s；自动匹配 %d 个未映射材质槽。' % (source['name'], filled)


class NTEBRIDGE_OT_apply_source(bpy.types.Operator):
    bl_idname = 'nte_bridge.apply_source'
    bl_label = '使用此网格'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            if _resolved_folder(settings.source_folder).casefold() != str(_source_data(settings).get('folder', '')).casefold():
                raise BridgeError('目录已改变，请先重新读取角色。')
            _apply_source(settings)
            self.report({'INFO'}, settings.status)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class NTEBRIDGE_OT_refresh_slots(bpy.types.Operator):
    bl_idname = 'nte_bridge.refresh_slots'
    bl_label = '刷新部件槽'
    bl_description = '保留独立槽身份及已有映射，只为名称唯一匹配的空槽补全材质；槽顺序变化后请核对'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            _refresh_parts(settings)
            mapped = sum(bool(part.material_path) for part in settings.parts)
            settings.status = '已识别 %d 个独立槽，已映射 %d 个。' % (len(settings.parts), mapped)
            self.report({'INFO'}, settings.status)
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class NTEBRIDGE_OT_source_report(bpy.types.Operator):
    bl_idname = 'nte_bridge.source_report'
    bl_label = '查看完整材质信息'
    bl_description = '查看材质参数、贴图引用、母材质链和未找到的依赖'
    material_name: StringProperty()

    def execute(self, context):
        settings = context.scene.nte_bridge
        entry = settings.material_catalog.get(self.material_name)
        data = json.loads(entry.metadata_json) if entry else _source_data(settings)
        report = bpy.data.texts.get('NTE Bridge 角色资源.json') or bpy.data.texts.new('NTE Bridge 角色资源.json')
        report.clear()
        report.write(json.dumps(data, ensure_ascii=False, indent=2))
        if context.area:
            context.area.type = 'TEXT_EDITOR'
            context.area.spaces.active.text = report
        return {'FINISHED'}


class NTEBRIDGE_OT_edit_state(bpy.types.Operator):
    bl_idname = 'nte_bridge.edit_state'
    bl_label = '修改循环状态'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    action: StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'NTEBridgeCycle':
            return {'CANCELLED'}
        if self.action == 'ADD':
            node.add_state()
        elif len(node.states) > 2 or (node.include_hidden and len(node.states) > 1):
            removed = node.states[-1].state_id
            node.inputs.remove(node.inputs[-1])
            node.states.remove(len(node.states) - 1)
            if node.initial_state_id == removed:
                node.initial_state_id = node.states[0].state_id
        return {'FINISHED'}


class NTEBRIDGE_OT_edit_texture(bpy.types.Operator):
    bl_idname = 'nte_bridge.edit_texture'
    bl_label = '修改贴图清单'
    bl_options = {'REGISTER', 'UNDO'}
    action: StringProperty()

    def execute(self, context):
        settings = context.scene.nte_bridge
        if self.action == 'ADD':
            entry = settings.textures.add()
            entry.texture_id = new_id()
            settings.active_texture = len(settings.textures) - 1
        elif settings.textures:
            settings.textures.remove(min(settings.active_texture, len(settings.textures) - 1))
            settings.active_texture = max(0, min(settings.active_texture, len(settings.textures) - 1))
        return {'FINISHED'}


class NTEBRIDGE_OT_open_graph(bpy.types.Operator):
    bl_idname = 'nte_bridge.open_graph'
    bl_label = '打开角色节点图'

    def execute(self, context):
        tree = context.scene.nte_bridge.graph
        if not tree:
            self.report({'ERROR'}, '请先初始化节点图')
            return {'CANCELLED'}
        context.area.type = 'NODE_EDITOR'
        context.area.ui_type = 'NTEBridgeTree'
        context.area.spaces.active.node_tree = tree
        context.area.spaces.active.pin = True
        return {'FINISHED'}


class NTEBRIDGE_OT_validate(bpy.types.Operator):
    bl_idname = 'nte_bridge.validate'
    bl_label = '校验并预览配置'

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            manifest = profile_manifest(settings)
            text = bpy.data.texts.get('NTE Bridge 配置预览.json') or bpy.data.texts.new('NTE Bridge 配置预览.json')
            text.clear()
            text.write(json.dumps(manifest, ensure_ascii=False, indent=2))
            settings.status = '校验通过：%d 部件，%d 贴图，%d 待生成运行时功能。' % (
                len(manifest['parts']), len(manifest['textures']), len(manifest['features']))
            self.report({'INFO'}, settings.status)
            return {'FINISHED'}
        except (BridgeError, ValueError, OSError) as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


def _engine_path(settings):
    engine_dir = settings.engine_dir.strip() or detect_engine_dir(bpy.path.abspath(settings.project_file))
    if not engine_dir:
        raise BridgeError('未找到工程对应的 UE，请在高级设置指定 UE 安装目录。')
    engine = Path(bpy.path.abspath(engine_dir)).resolve()
    python = engine / 'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'
    if not python.is_file():
        raise BridgeError('UE 安装目录中找不到 Python；请在高级设置选择包含 Engine 的目录。')
    return engine, python


def _job_defaults(settings):
    ensure_cache(settings)


def _task_command(manifest, engine, python, action, report, mode='remote'):
    command = [str(python), str(Path(__file__).with_name('cli.py')), action,
               '--manifest', str(manifest), '--engine-dir', str(engine), '--report', str(report)]
    if action == 'sync':
        command += ['--mode', mode]
    return command


def _completed_report(settings, report_path, code):
    if not report_path.is_file():
        raise BridgeError('没有收到任务报告；请在高级设置查看任务日志。')
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    if code or not report.get('success'):
        raise BridgeError('任务失败：' + '; '.join(str(e) for e in report.get('errors', ['详见报告'])))
    if report.get('features_applied') is False:
        settings.status = '资产同步成功；Blender 节点切换尚未生成，请在 UE 完成所需切换后再烘焙。'
    else:
        settings.status = '任务完成；结果与警告可在高级设置的报告中查看。'


class _WorkerModal:
    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def _spawn(self, command, log_path):
        self._log = Path(log_path).open('w', encoding='utf-8')
        try:
            self._process = subprocess.Popen(command, stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            self._log.close()
            raise

    def _launch(self, context, command, log_path):
        self._settings = context.scene.nte_bridge
        self._spawn(command, log_path)
        self._settings.busy = True
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self.report({'WARNING'}, '任务进行中；取消请使用任务进程，不在写入中强制关闭。')
        if event.type != 'TIMER' or self._process.poll() is None:
            return {'PASS_THROUGH'}
        self._log.close()
        try:
            continuation = self._complete(self._process.returncode)
            if continuation:
                self._spawn(*continuation)
                if context.area:
                    context.area.tag_redraw()
                return {'PASS_THROUGH'}
            result, report_type = {'FINISHED'}, {'INFO'}
        except Exception as error:
            self._settings.status = str(error)
            result, report_type = {'CANCELLED'}, {'ERROR'}
        context.window_manager.event_timer_remove(self._timer)
        self._settings.busy = False
        if context.area:
            context.area.tag_redraw()
        self.report(report_type, self._settings.status)
        return result


class NTEBRIDGE_OT_export(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.export'
    bl_label = '仅导出桥接任务'
    bl_description = '高级操作：只导出临时副本网格与贴图，不发送到 UE'

    def execute(self, context):
        settings = context.scene.nte_bridge
        settings.last_manifest = ''
        settings.last_report = ''
        try:
            _ensure_source_current(settings)
            _job_defaults(settings)
            self._job = prepare_job(context)
            settings.status = '正在后台导出 FBX 临时副本…'
            return self._launch(context, self._job['command'], self._job['job_dir'] / 'export.log')
        except Exception as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        path = finish_job(self._job, code)
        self._settings.last_manifest = str(path)
        pending = bool(self._job['manifest']['features'])
        self._settings.status = ('导出完成；Blender 节点切换尚未生成，请在 UE 完成所需切换后再烘焙。' if pending
                                 else '导出完成；可在高级设置发送该任务。')


class NTEBRIDGE_OT_send(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.send'
    bl_label = '发送到 UE'
    bl_description = '导出当前 Blender 配置和模型，随后自动同步到指定 UE 工程'

    def execute(self, context):
        settings = context.scene.nte_bridge
        settings.last_manifest = ''
        settings.last_report = ''
        try:
            _ensure_source_current(settings)
            self._engine, self._python = _engine_path(settings)
            self._sync_mode = settings.sync_mode
            _job_defaults(settings)
            self._job = prepare_job(context)
            self._phase = 'export'
            settings.status = '1/2 正在导出当前模型…'
            return self._launch(context, self._job['command'], self._job['job_dir'] / 'export.log')
        except Exception as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        if self._phase == 'export':
            manifest = finish_job(self._job, code)
            self._settings.last_manifest = str(manifest)
            self._report = manifest.parent / 'sync_report.json'
            self._settings.last_report = str(self._report)
            self._phase = 'sync'
            self._settings.status = '2/2 正在同步到 UE…'
            command = _task_command(manifest, self._engine, self._python, 'sync', self._report, self._sync_mode)
            return command, manifest.parent / 'sync.log'
        _completed_report(self._settings, self._report, code)


class NTEBRIDGE_OT_worker(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.worker'
    bl_label = '运行桥接任务'
    action: EnumProperty(items=[('sync', '发送已有任务到 UE', ''), ('package', '烘焙并打包', '')])

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            manifest = selected_manifest(settings)
            engine, python = _engine_path(settings)
            self._report = manifest.parent / (self.action + '_report.json')
            command = _task_command(manifest, engine, python, self.action, self._report, settings.sync_mode)
            if self.action == 'package':
                packager = settings.packager_source.strip() or detect_packager_source()
                if not packager or not settings.package_output:
                    raise BridgeError('请选择 Mod 输出目录；无法自动找到打包器时请在高级设置指定。')
                command += ['--packager-source', bpy.path.abspath(packager),
                            '--output-dir', bpy.path.abspath(settings.package_output), '--mod-name', settings.mod_name]
            settings.status = '正在后台' + ('同步 UE 资产…' if self.action == 'sync' else '烘焙并打包…')
            settings.last_report = str(self._report)
            return self._launch(context, command, manifest.parent / (self.action + '.log'))
        except Exception as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        _completed_report(self._settings, self._report, code)


class NTEBRIDGE_OT_cook(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.cook'
    bl_label = '烘焙角色目录'
    bl_description = '独立烘焙所选 UE 目录；无需重新导出或发送 Blender 网格'

    def execute(self, context):
        settings = context.scene.nte_bridge
        clear_cook(settings)
        reservation = None
        try:
            from .cooking import prepare_cook_request
            project = resolved_project(settings)
            folder = character_folder(settings)
            engine_dir = settings.engine_dir.strip() or detect_engine_dir(str(project))
            if not engine_dir:
                raise BridgeError('未找到工程对应的 UE，请在高级设置指定 UE 安装目录。')
            engine = Path(bpy.path.abspath(engine_dir)).resolve()
            reservation = prepare_cook_request(ensure_cache(settings).parent,
                {'schema_version': 1, 'project_file': str(project), 'character_folder': folder,
                 'excluded_assets': cook_excluded_assets(settings)})
            request = Path(reservation['request_path'])
            root = Path(reservation['cache_directory'])
            self._report = Path(reservation['report_path'])
            command = [str(worker_python()), str(Path(__file__).with_name('cli.py')), 'cook',
                       '--request', str(request), '--request-token', reservation['request_token'],
                       '--engine-dir', str(engine), '--report', str(self._report)]
            settings.last_report = str(self._report)
            settings.status = '正在后台烘焙 ' + folder + '…'
            return self._launch(context, command, root / 'cook.log')
        except Exception as error:
            if reservation and (not hasattr(self, '_process') or self._process.poll() is not None):
                from .cooking import cancel_cook_request
                cancel_cook_request(reservation['request_path'], reservation['request_token'])
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        _completed_report(self._settings, self._report, code)
        report = load_cooked_assets(self._settings, self._report)
        self._settings.status = '烘焙完成：%d 个资产；打开资产窗口，勾选后导出并打包。' % len(report['assets'])


class NTEBRIDGE_OT_select_filtered_cooked(bpy.types.Operator):
    bl_idname = 'nte_bridge.select_filtered_cooked'
    bl_label = '选择当前列表'
    action: EnumProperty(items=[('ALL', '全选当前筛选', ''), ('NONE', '全不选当前筛选', '')])

    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def execute(self, context):
        settings = context.scene.nte_bridge
        for entry in visible_assets(settings):
            if entry.packable:
                entry.selected = self.action == 'ALL'
        return {'FINISHED'}


class NTEBRIDGE_UL_cooked_assets(bpy.types.UIList):
    def filter_items(self, context, data, propname):
        return ([self.bitflag_filter_item if asset_visible(data, entry) else 0
                 for entry in getattr(data, propname)], [])

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        checkbox = row.row(align=True)
        checkbox.enabled = item.packable
        checkbox.prop(item, 'selected', text='')
        name = row.split(factor=0.33)
        name.label(text=item.asset_path.rsplit('/', 1)[-1], icon=TYPE_ICONS.get(item.asset_type, 'FILE'))
        kind = name.split(factor=0.20)
        kind.label(text=TYPE_LABELS.get(item.asset_type, item.asset_type))
        size = kind.split(factor=0.17)
        size.label(text=item.size_text)
        size.label(text=item.asset_path, icon='NONE' if item.packable else 'LOCKED')


class NTEBRIDGE_OT_select_cooked_assets(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.select_cooked_assets'
    bl_label = '导出所选资产并打包'
    bl_description = '选择本次烘焙资产，导出到指定目录后自动调用外部打包器'

    def invoke(self, context, event):
        try:
            verified_cook(context.scene.nte_bridge)
            return context.window_manager.invoke_props_dialog(self, width=asset_dialog_width(context),
                confirm_text='导出所选资产并打包', title='烘焙资产 · 选择导出')
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def draw(self, context):
        settings = context.scene.nte_bridge
        layout = self.layout
        layout.label(text=character_folder(settings), icon='FILE_FOLDER')
        filters = layout.row(align=True)
        filters.prop(settings, 'cook_search', text='', icon='VIEWZOOM')
        filters.prop(settings, 'cook_type', text='')
        selection = layout.row(align=True)
        selection.prop(settings, 'cook_show_dependencies')
        selection.operator('nte_bridge.select_filtered_cooked', text='全选当前筛选').action = 'ALL'
        selection.operator('nte_bridge.select_filtered_cooked', text='全不选当前筛选').action = 'NONE'
        header = layout.row().split(factor=0.35)
        header.label(text='选择 / 资产名称')
        kind = header.split(factor=0.20)
        kind.label(text='类型')
        size = kind.split(factor=0.17)
        size.label(text='大小')
        size.label(text='UE 资源路径')
        layout.template_list('NTEBRIDGE_UL_cooked_assets', '', settings, 'cook_assets', settings,
                             'cook_active_asset', rows=12, maxrows=16)
        selected = [entry for entry in settings.cook_assets if entry.selected and entry.packable]
        layout.label(text='当前显示 %d 项 · 已选 %d 项 · 总可选 %d 项 · %s' % (
            len(visible_assets(settings)), len(selected), sum(entry.packable for entry in settings.cook_assets),
            size_label(sum(int(entry.size_bytes) for entry in selected))))
        layout.label(text='原母材质、骨架和物理占位不可选；隐藏的已选资产仍包含在本次导出中', icon='INFO')
        if settings.cook_assets and settings.cook_active_asset < len(settings.cook_assets):
            active = settings.cook_assets[settings.cook_active_asset]
            if asset_visible(settings, active):
                layout.label(text=active.asset_path, icon='FILE_FOLDER')
                if active.reason:
                    layout.label(text=active.reason, icon='INFO' if active.packable else 'LOCKED')
        layout.separator()
        _wide_prop(layout, settings, 'cook_export_directory')
        _wide_prop(layout, settings, 'package_output', 'Mod 成品输出目录')
        layout.prop(settings, 'mod_name')

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            selection = selection_request(settings)
            packager = settings.packager_source.strip() or detect_packager_source()
            if not packager or not settings.package_output.strip():
                raise BridgeError('请选择 Mod 成品输出目录；无法自动找到打包器时请在高级设置指定。')
            from .cooking import lock_cook_report
            # Another Blender may be replacing this role's current directory.
            # Never recreate it between the backend's rename operations.
            with lock_cook_report(selection['cook_report']):
                selection = selection_request(settings)
                root = Path(settings.cook_report).parent / 'current' / 'selections' / new_id()
                root.mkdir(parents=True, exist_ok=False)
                path = root / 'selection.json'
                self._report = root / 'package_report.json'
                write_json(path, selection)
            command = [str(worker_python()), str(Path(__file__).with_name('cli.py')), 'package-selection',
                       '--selection', str(path), '--packager-source', bpy.path.abspath(packager),
                       '--output-dir', bpy.path.abspath(settings.package_output), '--mod-name', settings.mod_name,
                       '--report', str(self._report)]
            settings.last_report = str(self._report)
            settings.status = '正在导出 %d 个所选资产并打包…' % len(selection['selected_assets'])
            return self._launch(context, command, root / 'package.log')
        except Exception as error:
            settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        _completed_report(self._settings, self._report, code)
        self._settings.status = '所选资产已导出并打包完成。'


class NTEBRIDGE_UL_parts(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text='%d · %s' % (item.source_slot, item.display_name), icon='MATERIAL')
        layout.label(text='已映射' if item.material_path else '待映射', icon='CHECKMARK' if item.material_path else 'ERROR')


class NTEBRIDGE_UL_textures(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=Path(item.file_path).name or '新贴图', icon='IMAGE_DATA')
        layout.label(text=item.role)


def _wide_prop(layout, data, prop, label=None):
    layout.label(text=label or data.bl_rna.properties[prop].name)
    layout.prop(data, prop, text='')


def _wrapped(layout, text, context, icon='NONE'):
    width = max(22, int((getattr(context.region, 'width', 300) - 36) / 7))
    lines, line, count = [], '', 0
    for char in str(text):
        step = 2 if ord(char) > 255 else 1
        if count + step > width:
            lines.append(line)
            line, count = '', 0
        line += char
        count += step
    if line:
        lines.append(line)
    for index, line in enumerate(lines):
        layout.label(text=line, icon=icon if index == 0 else 'NONE')


def _panel_column(panel, context):
    column = panel.layout.column()
    column.enabled = not context.scene.nte_bridge.busy
    return column


def _material_details(layout, entry, context):
    if not entry:
        return
    if not entry.available:
        _wrapped(layout, '已有游戏引用；本文件夹没有该材质的参数资料。', context)
    else:
        data = json.loads(entry.metadata_json)
        parameters = data.get('parameters', {})
        count = sum(len(value) if isinstance(value, (dict, list)) else 1 for value in parameters.values())
        layout.label(text='%d 项参数 · %d 项贴图引用' % (count, len(data.get('textures', []))))
    layout.operator('nte_bridge.source_report', text='查看参数与贴图信息', icon='TEXT').material_name = entry.name


class NTEBRIDGE_PT_main(bpy.types.Panel):
    bl_label = 'NTE Bridge · 角色'
    bl_idname = 'NTEBRIDGE_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'NTE Bridge'

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        _wide_prop(column, settings, 'source_folder')
        column.operator('nte_bridge.scan_character', icon='FILE_REFRESH')
        if settings.source_meshes:
            if len(settings.source_meshes) > 1:
                column.label(text='原始骨骼网格（%d 个）' % len(settings.source_meshes))
                column.prop_search(settings, 'source_mesh_choice', settings, 'source_meshes', text='')
                selected = settings.source_meshes.get(settings.source_mesh_choice)
                if not selected or selected.asset_path != settings.applied_source_mesh:
                    row = column.row()
                    row.enabled = bool(selected)
                    row.operator('nte_bridge.apply_source', text='使用此网格', icon='CHECKMARK')
            source = _selected_source(settings)
            if source:
                if len(settings.source_meshes) == 1:
                    _wrapped(column, source['name'], context, 'OUTLINER_DATA_MESH')
                refs = ['骨架已读取' if settings.skeleton_path else '缺少骨架引用']
                if settings.physics_path:
                    refs.append('物理资产已读取')
                column.label(text=' · '.join(refs), icon='CHECKMARK')
            else:
                column.label(text='请选择要替换的原始网格', icon='INFO')
        column.separator(factor=0.5)
        _wide_prop(column, settings, 'mesh')
        if settings.mesh:
            rig = settings.mesh.find_armature()
            if rig:
                column.label(text='已识别绑定骨架：' + rig.name, icon='ARMATURE_DATA')
            else:
                column.label(text='网格尚未绑定骨架', icon='ERROR')
            mapped = sum(bool(part.material_path) for part in settings.parts)
            column.label(text='材质槽 %d / %d 已映射' % (mapped, len(settings.parts)),
                         icon='CHECKMARK' if mapped and mapped == len(settings.parts) else 'INFO')
        if settings.source_folder and _resolved_folder(settings.source_folder).casefold() != str(_source_data(settings).get('folder', '')).casefold():
            column.label(text='目录已改变，请重新读取角色', icon='INFO')


class _NTEChildPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'NTE Bridge'
    bl_parent_id = 'NTEBRIDGE_PT_main'
    bl_options = {'DEFAULT_CLOSED'}


class NTEBRIDGE_PT_materials(_NTEChildPanel, bpy.types.Panel):
    bl_label = '材质与部件'
    bl_idname = 'NTEBRIDGE_PT_materials'
    bl_order = 0

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        column.operator('nte_bridge.refresh_slots', icon='FILE_REFRESH')
        if not settings.parts:
            column.label(text='选择 Blender 网格后自动读取材质槽')
            return
        column.template_list('NTEBRIDGE_UL_parts', '', settings, 'parts', settings, 'active_part',
                             rows=min(5, max(2, len(settings.parts))))
        if settings.active_part >= len(settings.parts):
            return
        part = settings.parts[settings.active_part]
        column.prop(part, 'display_name')
        _wrapped(column, 'Blender 材质：' + (part.source_material.name if part.source_material else '空槽'), context)
        if settings.material_catalog:
            column.label(text='选择原游戏材质（母材质）')
            column.prop_search(part, 'catalog_material', settings, 'material_catalog', text='')
        _wide_prop(column, part, 'material_path', '当前 UE 材质路径')
        entry = next((item for item in settings.material_catalog if item.asset_path == part.material_path), None)
        _material_details(column, entry, context)


class NTEBRIDGE_PT_catalog(_NTEChildPanel, bpy.types.Panel):
    bl_label = '原游戏材质（母材质）'
    bl_idname = 'NTEBRIDGE_PT_catalog'
    bl_order = 1

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        loaded = sum(entry.available for entry in settings.material_catalog)
        if not settings.material_catalog:
            column.label(text='读取角色后查看材质参数与贴图引用')
            return
        column.label(text='%d 份材质资料 · %d 项外部引用' % (loaded, len(settings.material_catalog) - loaded))
        column.prop_search(settings, 'catalog_choice', settings, 'material_catalog', text='')
        entry = settings.material_catalog.get(settings.catalog_choice)
        if entry:
            _wrapped(column, entry.asset_path, context)
            _material_details(column, entry, context)
        column.operator('nte_bridge.source_report', text='查看角色资源清单', icon='TEXT')


class NTEBRIDGE_PT_textures(_NTEChildPanel, bpy.types.Panel):
    bl_label = '替换贴图'
    bl_idname = 'NTEBRIDGE_PT_textures'
    bl_order = 2

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        column.label(text='已连接的漫射自动用于 UE 预览')
        column.label(text='其他需要导入 UE 的贴图在这里添加')
        column.label(text='是否打包在烘焙资产窗口选择')
        if settings.textures:
            column.template_list('NTEBRIDGE_UL_textures', '', settings, 'textures', settings, 'active_texture',
                                 rows=min(4, max(2, len(settings.textures))))
        else:
            column.label(text='沿用原贴图时无需添加')
        row = column.row(align=True)
        row.operator('nte_bridge.edit_texture', text='添加贴图', icon='ADD').action = 'ADD'
        if settings.textures:
            row.operator('nte_bridge.edit_texture', text='移除', icon='REMOVE').action = 'REMOVE'
        if settings.textures and settings.active_texture < len(settings.textures):
            entry = settings.textures[settings.active_texture]
            _wide_prop(column, entry, 'file_path')
            _wide_prop(column, entry, 'asset_path')
            column.prop(entry, 'role', text='')


class NTEBRIDGE_PT_nodes(_NTEChildPanel, bpy.types.Panel):
    bl_label = '功能节点'
    bl_idname = 'NTEBRIDGE_PT_nodes'
    bl_order = 3

    def draw(self, context):
        column = _panel_column(self, context)
        _wrapped(column, '节点目前用于配置预览，接入运行时生成后才能打包切换功能。', context, 'INFO')
        column.operator('nte_bridge.open_graph', icon='NODETREE')
        column.operator('nte_bridge.validate', icon='CHECKMARK')


class NTEBRIDGE_PT_send(_NTEChildPanel, bpy.types.Panel):
    bl_label = '发送到 UE'
    bl_idname = 'NTEBRIDGE_PT_send'
    bl_order = 4
    bl_options = set()

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        _wide_prop(column, settings, 'project_file')
        column.prop(settings, 'sync_mode', text='')
        row = column.row()
        row.scale_y = 1.3
        row.operator('nte_bridge.send', text='发送到 UE', icon='EXPORT')
        _wrapped(self.layout, settings.status, context, 'TIME' if settings.busy else 'INFO')


class NTEBRIDGE_PT_package(_NTEChildPanel, bpy.types.Panel):
    bl_label = '烘焙与打包'
    bl_idname = 'NTEBRIDGE_PT_package'
    bl_order = 5

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        column.prop(settings, 'cook_use_custom')
        if settings.cook_use_custom:
            _wide_prop(column, settings, 'cook_folder')
        else:
            _wrapped(column, default_character_folder(settings) or '读取角色后自动确定 UE 目录', context, 'FILE_FOLDER')
        column.operator('nte_bridge.cook', text='烘焙角色目录', icon='RENDER_STILL')
        column.separator()
        row = column.row()
        row.enabled = bool(settings.cook_report)
        row.operator('nte_bridge.select_cooked_assets', text='选择烘焙资产…', icon='ASSET_MANAGER')
        if settings.cook_report:
            local = sum(not entry.dependency for entry in settings.cook_assets)
            column.label(text='当前目录 %d 项 · 目录外依赖 %d 项' % (local, len(settings.cook_assets) - local))


class NTEBRIDGE_PT_advanced(_NTEChildPanel, bpy.types.Panel):
    bl_label = '高级设置'
    bl_idname = 'NTEBRIDGE_PT_advanced'
    bl_order = 6

    def draw(self, context):
        settings = context.scene.nte_bridge
        column = _panel_column(self, context)
        _wide_prop(column, settings, 'cache_root')
        column.label(text='任务、报告和临时文件统一存放')
        column.label(text='留空时自动选择非 C 盘位置')
        paths = column.column()
        paths.enabled = False
        _wide_prop(paths, settings, 'job_root')
        column.separator()
        column.label(text='资源路径（从角色资料自动读取）')
        for prop in ('mesh_path', 'skeleton_path', 'physics_path'):
            _wide_prop(column, settings, prop)
        column.prop(settings, 'create_placeholders')
        column.separator()
        column.label(text='工具路径（留空时自动查找）')
        for prop in ('engine_dir', 'packager_source'):
            _wide_prop(column, settings, prop)
        column.separator()
        column.label(text='独立步骤与诊断')
        column.operator('nte_bridge.export', text='仅导出当前配置', icon='EXPORT')
        paths = column.column()
        paths.enabled = False
        _wide_prop(paths, settings, 'last_manifest')
        row = column.row()
        row.enabled = bool(settings.last_manifest)
        row.operator('nte_bridge.worker', text='发送最近任务到 UE', icon='IMPORT').action = 'sync'
        paths = column.column()
        paths.enabled = False
        _wide_prop(paths, settings, 'last_report')
        _wide_prop(paths, settings, 'cook_report')


def _add_menu(self, context):
    space = context.space_data
    if space and space.type == 'NODE_EDITOR' and space.tree_type == 'NTEBridgeTree':
        self.layout.separator()
        for node_type, label in [('NTEBridgePart', 'NTE · 部件'), ('NTEBridgeGroup', 'NTE · 组合'),
                                 ('NTEBridgeCycle', 'NTE · 材质组循环'), ('NTEBridgeOutput', 'NTE · 角色输出')]:
            op = self.layout.operator('node.add_node', text=label)
            op.type = node_type
            op.use_transform = True


CLASSES = (NTEBridgePartEntry, NTEBridgeSourceMeshEntry, NTEBridgeMaterialEntry,
           NTEBridgeTextureEntry, NTEBridgeCookedAssetEntry, NTEBridgeStateEntry, NTEBridgeSocket,
           NTEBridgeTree, NTEBridgePart, NTEBridgeGroup, NTEBridgeCycle, NTEBridgeOutput,
           NTEBridgeSettings, NTEBRIDGE_OT_scan_character, NTEBRIDGE_OT_apply_source,
           NTEBRIDGE_OT_refresh_slots, NTEBRIDGE_OT_source_report, NTEBRIDGE_OT_edit_state,
           NTEBRIDGE_OT_edit_texture, NTEBRIDGE_OT_open_graph, NTEBRIDGE_OT_validate,
           NTEBRIDGE_OT_export, NTEBRIDGE_OT_send, NTEBRIDGE_OT_worker, NTEBRIDGE_UL_parts,
           NTEBRIDGE_OT_cook, NTEBRIDGE_OT_select_filtered_cooked, NTEBRIDGE_UL_cooked_assets, NTEBRIDGE_OT_select_cooked_assets,
           NTEBRIDGE_UL_textures, NTEBRIDGE_PT_main, NTEBRIDGE_PT_materials,
           NTEBRIDGE_PT_catalog, NTEBRIDGE_PT_textures, NTEBRIDGE_PT_nodes,
           NTEBRIDGE_PT_send, NTEBRIDGE_PT_package, NTEBRIDGE_PT_advanced)

def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.nte_bridge = PointerProperty(type=NTEBridgeSettings)
    bpy.types.NODE_MT_add.append(_add_menu)
    bpy.app.handlers.load_post.append(_restore_cache)
    if hasattr(bpy.data, 'scenes'):
        _restore_cache()
    else:
        bpy.app.timers.register(_initialize_cache, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(_initialize_cache):
        bpy.app.timers.unregister(_initialize_cache)
    if _restore_cache in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_restore_cache)
    bpy.types.NODE_MT_add.remove(_add_menu)
    if hasattr(bpy.types.Scene, 'nte_bridge'):
        del bpy.types.Scene.nte_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _ENUM_CACHE.clear()
