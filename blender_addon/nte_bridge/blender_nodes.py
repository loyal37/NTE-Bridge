"""LoyalTools-style character blueprint: object, material, switch, group and generate nodes."""

from pathlib import Path
import json
import os
import re
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from .core import BridgeError, ht_material_ids

TREE = 'NTEBridgeTree'
OBJECT_SOCKET = 'NTEBridgeObjectSocket'
MATERIAL_SOCKET = 'NTEBridgeMaterialSocket'
LEGACY_SOCKET = 'NTEBridgeSocket'
FLOW_NODES = {'NTEBridgeObject', 'NTEBridgeGroup', 'NTEBridgeSwitch', 'NTEBridgeOutput'}
PER_PART_PARAMETERS = ('BaseColor', 'ID_Tex', 'LightMap', 'NomralMap', 'NormalMap', 'NomrMap', 'SkilMask')
DEFAULT_PARAMETERS = ('BaseColor', 'ID_Tex', 'LightMap', 'NomralMap')
IMAGE_SUFFIXES = {'.png', '.tga', '.jpg', '.jpeg', '.bmp', '.psd', '.exr', '.dds', '.tif', '.tiff'}
ROLE_ITEMS = [('BASE_COLOR', '漫射 · BC7 / sRGB', ''), ('ID_TEX', 'ID · BC7 / 线性', ''),
              ('LIGHT_MAP', 'LightMap · BC7 / 线性', ''), ('NORMAL', '法线 · BC5 / 线性', ''),
              ('MASK', '遮罩 · BC7 / 线性', '')]
ROLE_SUFFIXES = {'BASE_COLOR': ('d', 'diffuse', 'basecolor', 'albedo'), 'ID_TEX': ('id',),
                 'LIGHT_MAP': ('m', 'lightmap', 'lm'), 'NORMAL': ('n', 'normal', 'nrm'), 'MASK': ('mask',)}
_REPORT_CACHE = {}
_SYNCING = False


def new_id():
    return str(uuid.uuid4())


def _settings(context=None):
    scene = getattr(context or bpy.context, 'scene', None)
    return getattr(scene, 'nte_bridge', None)


def _text_width(texts, minimum=200):
    width = minimum
    for text in texts:
        count = sum(2 if ord(char) > 0x2E7F else 1 for char in str(text or ''))
        width = max(width, count * 8 + 40)
    return width


def role_for_parameter(name):
    key = re.sub('[^a-z]', '', str(name).casefold())
    if key in {'basecolor', 'diffuse', 'diffusecolormap', 'albedo'}:
        return 'BASE_COLOR'
    if key in {'idtex', 'id'}:
        return 'ID_TEX'
    if key == 'lightmap':
        return 'LIGHT_MAP'
    if 'normal' in key or 'nomr' in key:
        return 'NORMAL'
    return 'MASK'


def texture_asset_name(path):
    return re.sub(r'[^A-Za-z0-9_]', '_', Path(path).stem)


def _normalized_stem(path):
    return re.sub(r'[\s\-_]+', '_', Path(path).stem.casefold()).strip('_')


def auto_material_path(settings, material):
    """Unambiguous name match against the role catalogue; never guess by slot order."""
    if settings is None or material is None:
        return ''
    names = {material.name, re.sub(r'\.\d{3,}$', '', material.name)}
    paths = {entry.asset_path for entry in settings.material_catalog if entry.display_name in names}
    return next(iter(paths)) if len(paths) == 1 else ''


def _ht_slot_map(settings):
    path = getattr(settings, 'last_report', '') if settings else ''
    if not path:
        return {}
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    cached = _REPORT_CACHE.get(path)
    if not cached or cached[0] != stamp:
        try:
            report = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            slot_map = report.get('slot_map', {}) if report.get('success') else {}
        except (OSError, ValueError):
            slot_map = {}
        cached = _REPORT_CACHE[path] = (stamp, slot_map)
    return cached[1]


# --------------------------------------------------------------------- sockets

class NTEBridgeObjectSocket(bpy.types.NodeSocket):
    bl_idname = OBJECT_SOCKET
    bl_label = '物体'

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.0, 0.8, 0.8, 1.0)


class NTEBridgeMaterialSocket(bpy.types.NodeSocket):
    bl_idname = MATERIAL_SOCKET
    bl_label = '材质'
    auto_material: StringProperty()

    def draw(self, context, layout, node, text):
        if self.is_output or self.is_linked:
            layout.label(text=text)
            return
        row = layout.row(align=True)
        row.label(text=text)
        if self.auto_material:
            row.label(text='自动：' + self.auto_material.rsplit('/', 1)[-1])
        else:
            row.label(text='未匹配', icon='ERROR')

    def draw_color(self, context, node):
        return (0.39, 0.78, 0.39, 1.0)


class NTEBridgeSocket(bpy.types.NodeSocket):
    """v0.3 socket, registered only so older files can be upgraded."""
    bl_idname = LEGACY_SOCKET
    bl_label = '旧版插口'
    socket_id: StringProperty()

    def draw(self, context, layout, node, text):
        layout.label(text=text)

    def draw_color(self, context, node):
        return (0.5, 0.5, 0.5, 1.0)


def _sockets_compatible(source, target):
    if source.node.bl_idname == 'NodeReroute' or target.node.bl_idname == 'NodeReroute':
        return True
    if MATERIAL_SOCKET in (source.bl_idname, target.bl_idname):
        return source.bl_idname == target.bl_idname
    return source.bl_idname == target.bl_idname == OBJECT_SOCKET or LEGACY_SOCKET in (
        source.bl_idname, target.bl_idname)


def _link_valid(link):
    return _sockets_compatible(link.from_socket, link.to_socket)


class NTEBridgeTree(bpy.types.NodeTree):
    bl_idname = TREE
    bl_label = 'NTE Bridge · 角色蓝图'
    bl_icon = 'NODETREE'
    graph_id: StringProperty()
    next_order: IntProperty(default=1)

    def update(self):
        for link in self.links:
            try:
                link.is_valid = _link_valid(link)
            except (AttributeError, TypeError):
                pass


class _NTENode:
    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == TREE


def _sync_sockets(collection, specs, socket_type):
    """Keep identifiers stable so links follow their slot after reordering."""
    wanted = [identifier for identifier, _ in specs]
    for socket in list(collection):
        if socket.identifier not in wanted or socket.bl_idname != socket_type:
            collection.remove(socket)
    existing = {socket.identifier for socket in collection}
    for identifier, name in specs:
        if identifier not in existing:
            collection.new(socket_type, name, identifier=identifier)
    for index, (identifier, name) in enumerate(specs):
        current = [socket.identifier for socket in collection].index(identifier)
        if current != index:
            collection.move(current, index)
        collection[index].name = name


def _dynamic_inputs(node, prefix='输入'):
    """LoyalTools behaviour: a linked last socket adds one; extra trailing empties are removed."""
    if not node.inputs or node.inputs[-1].is_linked:
        used = {socket.identifier for socket in node.inputs}
        number = 1
        while 'in_%d' % number in used:
            number += 1
        node.inputs.new(OBJECT_SOCKET, prefix, identifier='in_%d' % number)
    while len(node.inputs) > 1 and not node.inputs[-1].is_linked and not node.inputs[-2].is_linked:
        node.inputs.remove(node.inputs[-1])
    for index, socket in enumerate(node.inputs):
        socket.name = '%s %d' % (prefix, index + 1)


# ------------------------------------------------------------------ object node

class NTEBridgeObjectSlot(bpy.types.PropertyGroup):
    part_id: StringProperty()
    material: PointerProperty(type=bpy.types.Material)
    slot_index: IntProperty()
    label: StringProperty()
    split: BoolProperty(name='单独输出', default=False, update=lambda slot, context: _slot_split_changed(slot))


def _slot_split_changed(slot):
    pointer = slot.as_pointer()
    node = next((node for node in slot.id_data.nodes if node.bl_idname == 'NTEBridgeObject'
                 and any(item.as_pointer() == pointer for item in node.slots)), None)
    if node is not None:
        sync_object_node(node, _settings())


def _mesh_poll(node, obj):
    return obj.type == 'MESH'


def _target_changed(node, context):
    if not node.is_main:
        node.slots.clear()
    sync_object_node(node, _settings(context))


class NTEBridgeObject(_NTENode, bpy.types.Node):
    '''引用一个 Blender 网格物体；左侧为每个材质槽的材质入口，右侧输出部件'''
    bl_idname = 'NTEBridgeObject'
    bl_label = '物体'
    bl_icon = 'OBJECT_DATAMODE'
    bl_width_min = 260

    node_id: StringProperty()
    target: PointerProperty(name='物体', type=bpy.types.Object, poll=_mesh_poll, update=_target_changed)
    is_main: BoolProperty(options={'HIDDEN'})
    order_key: IntProperty(options={'HIDDEN'})
    slots: CollectionProperty(type=NTEBridgeObjectSlot)
    hide_auto: BoolProperty(name='隐藏已自动匹配的材质入口', default=True,
                            update=lambda node, context: sync_object_node(node, _settings(context)))
    show_slots: BoolProperty(name='材质槽', default=False)

    def init(self, context):
        self.node_id = new_id()
        self.width = 300
        tree = self.id_data
        self.order_key = tree.next_order
        tree.next_order += 1
        self.outputs.new(OBJECT_SOCKET, '输出', identifier='all')

    def copy(self, node):
        self.node_id = new_id()
        self.is_main = False
        self.order_key = self.id_data.next_order
        self.id_data.next_order += 1
        self.target = None
        self.slots.clear()

    def draw_label(self):
        return ('主网格 · ' if self.is_main else '') + (self.target.name if self.target else '物体')

    def draw_buttons(self, context, layout):
        settings = _settings(context)
        row = layout.row(align=True)
        field = row.row(align=True)
        field.enabled = not self.is_main
        field.prop(self, 'target', text='')
        if self.target:
            op = row.operator('nte_bridge.select_node_object', text='', icon='RESTRICT_SELECT_OFF')
            op.object_name = self.target.name
        if self.target is None:
            layout.label(text='请选择网格物体', icon='ERROR')
            return
        rig = self.target.find_armature()
        expected = settings.mesh.find_armature() if settings and settings.mesh else None
        if rig is None:
            layout.label(text='尚未绑定骨架', icon='ERROR')
        elif expected is not None and rig != expected:
            layout.label(text='绑定的骨架与主网格不同：' + rig.name, icon='ERROR')
        keys = self.target.data.shape_keys
        layout.label(text='%d 个材质槽 · %d 个形态键' % (len(self.target.material_slots),
                     max(0, len(keys.key_blocks) - 1) if keys else 0))
        if [slot.material for slot in self.target.material_slots] != [slot.material for slot in self.slots]:
            layout.operator('nte_bridge.refresh_slots' if self.is_main else 'nte_bridge.sync_blueprint',
                            text='材质槽已变化，点击刷新', icon='FILE_REFRESH')
        row = layout.row()
        row.prop(self, 'show_slots', icon='TRIA_DOWN' if self.show_slots else 'TRIA_RIGHT', emboss=False)
        if self.show_slots:
            box = layout.box()
            box.prop(self, 'hide_auto')
            box.label(text='勾选的槽单独输出，可接入切换：')
            for slot in self.slots:
                box.prop(slot, 'split', text='%d · %s' % (slot.slot_index, slot.label))


def _slot_label(material, index):
    return material.name if material else '槽 %d' % index


def _match_slot_ids(old, current):
    """old: [(index, material, part_id, split)], current: [material]. Mirrors part reconciliation."""
    used, result = set(), []
    for index, material in enumerate(current):
        candidates = [i for i, item in enumerate(old) if i not in used and item[1] == material]
        match = next((i for i in candidates if old[i][0] == index), candidates[0] if candidates else None)
        if match is None:
            match = next((i for i, item in enumerate(old) if i not in used and item[0] == index
                          and not any(item[1] == other for other in current)), None)
        if match is None:
            result.append((new_id(), False))
        else:
            used.add(match)
            result.append((old[match][2], old[match][3]))
    return result


def sync_object_node(node, settings):
    """Mirror the object's material slots into stable part IDs and sockets."""
    global _SYNCING
    if _SYNCING:
        return
    _SYNCING = True
    try:
        if node.is_main and settings is not None and settings.mesh is not None:
            if node.target != settings.mesh:
                node.target = settings.mesh
            split = {slot.part_id for slot in node.slots if slot.split}
            node.slots.clear()
            for part in settings.parts:
                slot = node.slots.add()
                slot.part_id, slot.material = part.part_id, part.source_material
                slot.slot_index, slot.label = part.source_slot, part.display_name or _slot_label(part.source_material, part.source_slot)
                slot.split = part.part_id in split
        elif node.target is not None:
            old = [(slot.slot_index, slot.material, slot.part_id, slot.split) for slot in node.slots]
            materials = [slot.material for slot in node.target.material_slots]
            identities = _match_slot_ids(old, materials)
            node.slots.clear()
            for index, (material, (part_id, split)) in enumerate(zip(materials, identities)):
                slot = node.slots.add()
                slot.part_id, slot.material, slot.slot_index = part_id, material, index
                slot.label, slot.split = _slot_label(material, index), split
        else:
            node.slots.clear()
        splits = [slot for slot in node.slots if slot.split]
        _sync_sockets(node.outputs, [('all', '其余槽位' if splits else '输出')] +
                      [(slot.part_id, slot.label) for slot in splits], OBJECT_SOCKET)
        _sync_sockets(node.inputs, [(slot.part_id, slot.label) for slot in node.slots], MATERIAL_SOCKET)
        _refresh_auto(node, settings)
        node.width = max(node.width, _text_width([node.target.name if node.target else ''], 260))
    finally:
        _SYNCING = False


def _refresh_auto(node, settings):
    parts = {part.part_id: part for part in settings.parts} if settings is not None else {}
    for slot, socket in zip(node.slots, node.inputs):
        if node.is_main:
            part = parts.get(slot.part_id)
            automatic = part.material_path.strip() if part else ''
        else:
            automatic = auto_material_path(settings, slot.material)
        if socket.auto_material != automatic:
            socket.auto_material = automatic
        hide = bool(node.hide_auto and automatic and not socket.is_linked)
        if socket.hide != hide:
            socket.hide = hide


def refresh_auto_labels(settings):
    """Update automatic material labels after mapping or catalogue changes."""
    tree = getattr(settings, 'graph', None)
    if tree is None or _SYNCING:
        return
    for node in object_nodes(tree):
        if len(node.slots) == len(node.inputs):
            _refresh_auto(node, settings)


# ---------------------------------------------------------------- material node

def _catalog_picked(node, context):
    settings = _settings(context)
    entry = settings.material_catalog.get(node.catalog_choice) if settings else None
    if entry:
        node.material_path = entry.asset_path


def _parent_picked(node, context):
    settings = _settings(context)
    entry = settings.material_catalog.get(node.parent_choice) if settings else None
    if entry:
        node.parent_path = entry.asset_path


class NTEBridgeMaterialTexture(bpy.types.PropertyGroup):
    param: StringProperty(name='参数', update=lambda row, context: setattr(row, 'role', role_for_parameter(row.param)))
    file_path: StringProperty(name='贴图文件', subtype='FILE_PATH')
    role: EnumProperty(name='用途', items=ROLE_ITEMS, default='MASK')


class NTEBridgeMaterial(_NTENode, bpy.types.Node):
    '''选择调用的原游戏材质、工程已有材质实例，或新建材质实例'''
    bl_idname = 'NTEBridgeMaterial'
    bl_label = '材质球'
    bl_icon = 'MATERIAL'
    bl_width_min = 280

    node_id: StringProperty()
    source: EnumProperty(name='来源', default='NEW', items=[
        ('ORIGINAL', '原游戏材质', '游戏原路径材质，只作占位引用，不打包'),
        ('EXISTING', '工程已有材质实例', 'UE 工程中已有的材质实例；桥接不修改，可作为 Mod 资产打包'),
        ('NEW', '新建材质实例', '按母材质新建材质实例并导入覆盖贴图，作为 Mod 资产打包')])
    catalog_choice: StringProperty(name='原游戏材质', update=_catalog_picked)
    material_path: StringProperty(name='UE 路径', description='完整 /Game/... 路径')
    mi_name: StringProperty(name='名称', description='新材质实例名称，例如 MI_zankou_cloth')
    mi_folder: StringProperty(name='保存目录', description='UE 目录，例如 /Game/Characters/Player/036_zankou_1/ter/cloth_ter')
    parent_choice: StringProperty(name='母材质', update=_parent_picked)
    parent_path: StringProperty(name='母材质路径', description='工程中已存在的母材质，例如 /Game/Characters/Player/019_mint/ter_new_2/cloth_ter/MI_player_019_mint_2')
    textures: CollectionProperty(type=NTEBridgeMaterialTexture)

    def init(self, context):
        self.node_id = new_id()
        self.width = 340
        settings = _settings(context)
        if settings and settings.mesh_path.strip():
            self.mi_folder = settings.mesh_path.strip().rsplit('/', 1)[0] + '/ter/cloth_ter'
        for name in DEFAULT_PARAMETERS:
            row = self.textures.add()
            row.param = name
        self.outputs.new(MATERIAL_SOCKET, '材质', identifier='material')

    def copy(self, node):
        self.node_id = new_id()
        if self.mi_name:
            self.mi_name += '_copy'

    def resolved_path(self):
        if self.source == 'NEW':
            folder, name = self.mi_folder.strip().rstrip('/'), self.mi_name.strip()
            return folder + '/' + name if folder and name else ''
        return self.material_path.strip()

    def draw_label(self):
        path = self.resolved_path()
        return '材质球 · ' + path.rsplit('/', 1)[-1] if path else '材质球'

    def draw_buttons(self, context, layout):
        settings = _settings(context)
        layout.prop(self, 'source', text='')
        tree, node = self.id_data.name, self.name
        if self.source == 'ORIGINAL':
            if settings and settings.material_catalog:
                layout.prop_search(self, 'catalog_choice', settings, 'material_catalog', text='')
            layout.prop(self, 'material_path', text='')
        elif self.source == 'EXISTING':
            row = layout.row(align=True)
            row.prop(self, 'material_path', text='')
            op = row.operator('nte_bridge.pick_project_asset', text='', icon='FILEBROWSER')
            op.tree_name, op.node_name, op.target = tree, node, 'material_path'
        else:
            layout.prop(self, 'mi_name')
            layout.prop(self, 'mi_folder', text='目录')
            layout.label(text='母材质')
            if settings and settings.material_catalog:
                layout.prop_search(self, 'parent_choice', settings, 'material_catalog', text='')
            row = layout.row(align=True)
            row.prop(self, 'parent_path', text='')
            op = row.operator('nte_bridge.pick_project_asset', text='', icon='FILEBROWSER')
            op.tree_name, op.node_name, op.target = tree, node, 'parent_path'
            box = layout.box()
            for index, row_data in enumerate(self.textures):
                row = box.row(align=True)
                row.prop(row_data, 'param', text='')
                row.prop(row_data, 'file_path', text='')
                op = row.operator('nte_bridge.material_rows', text='', icon='X')
                op.tree_name, op.node_name, op.action, op.index = tree, node, 'REMOVE', index
            row = box.row(align=True)
            for action, text, icon in [('ADD', '添加', 'ADD'), ('PARENT', '按母材质', 'FILE_REFRESH'),
                                       ('SUGGEST', '按后缀补全', 'VIEWZOOM')]:
                op = row.operator('nte_bridge.material_rows', text=text, icon=icon)
                op.tree_name, op.node_name, op.action = tree, node, action
        path = self.resolved_path()
        if path:
            layout.label(text=path.rsplit('/', 1)[-1] + ('（新建，打包）' if self.source == 'NEW' else
                         '（已有，可打包）' if self.source == 'EXISTING' else '（占位，不打包）'))


# ------------------------------------------------------------------ switch node

def _next_variable(tree, exclude=None):
    used = {node.custom_var_name for node in tree.nodes
            if node.bl_idname == 'NTEBridgeSwitch' and node != exclude}
    number = 0
    while 'swapkey%d' % number in used:
        number += 1
    return 'swapkey%d' % number


def _switch_option_labels(node):
    labels = []
    for socket in node.inputs:
        if not socket.is_linked:
            labels.append('空（全部隐藏）')
            continue
        source = _through_reroute(socket.links[0])
        if source is None:
            labels.append('空（全部隐藏）')
        elif source.from_node.bl_idname in {'NTEBridgeObject', 'NTEBridgeSwitch'}:
            labels.append(source.from_node.draw_label())
        else:
            labels.append(source.from_node.label or source.from_node.name)
    return labels


class NTEBridgeSwitch(_NTENode, bpy.types.Node):
    '''物体切换：每个入口是一个选项，空入口代表全部隐藏'''
    bl_idname = 'NTEBridgeSwitch'
    bl_label = '物体切换'
    bl_icon = 'SHADERFX'

    node_id: StringProperty()
    comment: StringProperty(name='备注', description='切换名称，也用于面板显示')
    custom_var_name: StringProperty(name='变量名', description='UE 动画蓝图中保存切换状态的整数变量名')
    hotkey: StringProperty(name='按键', description='HT 按键格式，例如 K、9、alt 6、ctrl 6、shift 6')
    input_slot_count: IntProperty(name='选项数量', default=2, min=2, max=64,
                                  update=lambda node, context: node._update_input_sockets())
    initial_option: IntProperty(name='初始选项', default=0, min=0, max=63)
    description_expanded: BoolProperty(name='节点说明', default=False)

    def init(self, context):
        self.node_id = new_id()
        self.width = 320
        self.custom_var_name = _next_variable(self.id_data, self)
        self.outputs.new(OBJECT_SOCKET, '输出', identifier='out')
        self._update_input_sockets()

    def copy(self, node):
        self.node_id = new_id()
        self.custom_var_name = _next_variable(self.id_data, self)

    def _update_input_sockets(self):
        while len(self.inputs) > self.input_slot_count:
            self.inputs.remove(self.inputs[-1])
        while len(self.inputs) < self.input_slot_count:
            self.inputs.new(OBJECT_SOCKET, '选项_%d' % len(self.inputs), identifier='option_%d' % len(self.inputs))
        for index, socket in enumerate(self.inputs):
            socket.name = '选项_%d' % index

    def update(self):
        self._update_input_sockets()

    def draw_label(self):
        return '物体切换 · ' + self.comment if self.comment else '物体切换'

    def draw_buttons(self, context, layout):
        layout.prop(self, 'comment', text='备注')
        layout.prop(self, 'custom_var_name', text='变量名')
        layout.prop(self, 'hotkey', text='按键')
        if not self.hotkey.strip():
            layout.label(text='请填写按键，例如 alt 6', icon='ERROR')
        layout.separator()
        layout.label(text='选项数量:')
        row = layout.row(align=True)
        row.prop(self, 'input_slot_count', text='')
        op = row.operator('nte_bridge.switch_option', text='', icon='ADD')
        op.tree_name, op.node_name, op.delta = self.id_data.name, self.name, 1
        op = row.operator('nte_bridge.switch_option', text='', icon='REMOVE')
        op.tree_name, op.node_name, op.delta = self.id_data.name, self.name, -1
        layout.prop(self, 'initial_option')
        text = _switch_ht_text(self, _settings(context))
        if text:
            layout.label(text='HT：' + text, icon='COPYDOWN')
        layout.separator()
        layout.prop(self, 'description_expanded', text='节点说明',
                    icon='TRIA_DOWN' if self.description_expanded else 'TRIA_RIGHT')
        if self.description_expanded:
            column = layout.column()
            column.scale_y = 0.8
            for index, label in enumerate(_switch_option_labels(self)):
                column.label(text='选项_%d：%s' % (index, label))
            column.label(text='空入口 = 全部隐藏；按键依次切换各选项。')
            column.label(text='HT 编号在发送到 UE 后显示，可复制到 HT 工具。')


def _switch_ht_text(node, settings):
    slot_map = _ht_slot_map(settings)
    if not slot_map:
        return ''
    states = []
    for index, socket in enumerate(node.inputs):
        parts = []
        if socket.is_linked:
            source = _through_reroute(socket.links[0])
            if source:
                parts = _collect_parts(source.from_node, source.from_socket.identifier)
        states.append({'id': str(index), 'parts': parts})
    feature = {'states': states, 'initial_state_id': str(min(node.initial_option, len(states) - 1))}
    text, detail = ht_material_ids(feature, slot_map)
    return '%s（初始 %d）' % (text, detail) if text else ''


def _collect_parts(node, socket_identifier, depth=0):
    if depth > 64:
        return []
    if node.bl_idname == 'NTEBridgeObject':
        split = {slot.part_id for slot in node.slots if slot.split}
        if socket_identifier == 'all':
            return [slot.part_id for slot in node.slots if slot.part_id not in split]
        return [socket_identifier]
    parts = []
    if node.bl_idname in {'NTEBridgeGroup', 'NTEBridgeSwitch'}:
        for socket in node.inputs:
            if socket.is_linked:
                source = _through_reroute(socket.links[0])
                if source:
                    parts += _collect_parts(source.from_node, source.from_socket.identifier, depth + 1)
    return parts


# ------------------------------------------------------------ group / generate

class NTEBridgeGroup(_NTENode, bpy.types.Node):
    '''群组：把多个物体或切换放在一起，可接入另一个群组、切换或生成节点'''
    bl_idname = 'NTEBridgeGroup'
    bl_label = '群组'
    bl_icon = 'GROUP'
    node_id: StringProperty()

    def init(self, context):
        self.node_id = new_id()
        self.width = 200
        self.inputs.new(OBJECT_SOCKET, '输入 1', identifier='in_1')
        self.outputs.new(OBJECT_SOCKET, '输出', identifier='out')

    def copy(self, node):
        self.node_id = new_id()

    def update(self):
        if all(socket.bl_idname == OBJECT_SOCKET for socket in self.inputs):
            _dynamic_inputs(self)

    def draw_buttons(self, context, layout):
        layout.operator('nte_bridge.view_group_objects', text='查看递归解析预览', icon='HIDE_OFF').node_name = self.name


class NTEBridgeOutput(_NTENode, bpy.types.Node):
    '''生成：连接到这里的物体会合并成角色骨骼网格并发送到 UE'''
    bl_idname = 'NTEBridgeOutput'
    bl_label = '生成'
    bl_icon = 'EXPORT'
    node_id: StringProperty()

    def init(self, context):
        self.node_id = new_id()
        self.width = 320
        self.inputs.new(OBJECT_SOCKET, '输入 1', identifier='in_1')

    def copy(self, node):
        self.node_id = new_id()

    def update(self):
        if all(socket.bl_idname == OBJECT_SOCKET for socket in self.inputs):
            _dynamic_inputs(self)

    def draw_buttons(self, context, layout):
        settings = _settings(context)
        row = layout.row()
        row.scale_y = 1.4
        row.operator('nte_bridge.send', text='发送到 UE', icon='EXPORT')
        row = layout.row(align=True)
        row.operator('nte_bridge.validate', text='校验', icon='CHECKMARK')
        row.operator('nte_bridge.slot_table', text='槽位表', icon='TEXT')
        if settings and settings.blueprint_summary:
            layout.label(text=settings.blueprint_summary, icon='INFO')


# --------------------------------------------------------------- legacy nodes

class NTEBridgeStateEntry(bpy.types.PropertyGroup):
    state_id: StringProperty()
    label: StringProperty(name="状态名")


class NTEBridgePart(_NTENode, bpy.types.Node):
    bl_idname = 'NTEBridgePart'
    bl_label = '旧版部件'
    node_id: StringProperty()
    part_id: StringProperty()

    def draw_buttons(self, context, layout):
        layout.operator('nte_bridge.sync_blueprint', text='升级为新版蓝图', icon='FILE_REFRESH')


class NTEBridgeCycle(_NTENode, bpy.types.Node):
    bl_idname = 'NTEBridgeCycle'
    bl_label = '旧版材质组循环'
    node_id: StringProperty()
    states: CollectionProperty(type=NTEBridgeStateEntry)
    feature_label: StringProperty(default='服装切换')
    key: StringProperty(default='K')
    include_hidden: BoolProperty(default=False)
    initial_state_id: StringProperty()

    def draw_buttons(self, context, layout):
        layout.operator('nte_bridge.sync_blueprint', text='升级为新版蓝图', icon='FILE_REFRESH')


# ------------------------------------------------------ graph maintenance

def _through_reroute(link):
    """Follow reroute nodes back to the real source; None when a reroute is empty."""
    seen = 0
    while link is not None and link.from_node.bl_idname == 'NodeReroute' and seen < 64:
        inputs = link.from_node.inputs
        link = inputs[0].links[0] if inputs and inputs[0].is_linked else None
        seen += 1
    return link


def _relink(tree, old_socket, new_socket):
    for link in list(old_socket.links):
        if old_socket.is_output:
            tree.links.new(new_socket, link.to_socket)
        else:
            tree.links.new(link.from_socket, new_socket)


def _replace_legacy_sockets(tree, node):
    """Old group/output nodes keep their idname; rebuild their inputs as object sockets."""
    if all(socket.bl_idname != LEGACY_SOCKET for socket in list(node.inputs) + list(node.outputs)):
        return
    sources = [link.from_socket for socket in node.inputs for link in socket.links]
    targets = [link.to_socket for socket in node.outputs for link in socket.links]
    for socket in list(node.inputs):
        node.inputs.remove(socket)
    for socket in list(node.outputs):
        node.outputs.remove(socket)
    node.inputs.new(OBJECT_SOCKET, '输入 1', identifier='in_1')
    if node.bl_idname == 'NTEBridgeGroup':
        output = node.outputs.new(OBJECT_SOCKET, '输出', identifier='out')
        for target in targets:
            tree.links.new(output, target)
    for source in sources:
        tree.links.new(source, node.inputs[-1])
        _dynamic_inputs(node)
    if not node.node_id:
        node.node_id = new_id()


def _upgrade_legacy(tree, main, settings):
    changed = False
    for node in list(tree.nodes):
        if node.bl_idname in {'NTEBridgeGroup', 'NTEBridgeOutput'}:
            if any(socket.bl_idname == LEGACY_SOCKET for socket in list(node.inputs) + list(node.outputs)):
                _replace_legacy_sockets(tree, node)
                changed = True
    for node in [n for n in tree.nodes if n.bl_idname == 'NTEBridgePart']:
        targets = [link.to_socket for socket in node.outputs for link in socket.links]
        if targets and main is not None:
            slot = next((slot for slot in main.slots if slot.part_id == node.part_id), None)
            if slot is not None:
                slot.split = True
                sync_object_node(main, settings)
                output = next(socket for socket in main.outputs if socket.identifier == node.part_id)
                for target in targets:
                    tree.links.new(output, target)
        tree.nodes.remove(node)
        changed = True
    for node in [n for n in tree.nodes if n.bl_idname == 'NTEBridgeCycle']:
        switch = tree.nodes.new('NTEBridgeSwitch')
        switch.location = node.location
        switch.comment, switch.hotkey = node.feature_label, node.key
        states = list(node.states)
        switch.input_slot_count = max(2, len(states) + int(node.include_hidden))
        for index, (state, socket) in enumerate(zip(states, node.inputs)):
            for link in list(socket.links):
                tree.links.new(link.from_socket, switch.inputs[index])
            if state.state_id == node.initial_state_id:
                switch.initial_option = index
        if node.include_hidden and node.initial_state_id == '$hidden':
            switch.initial_option = len(states)
        for socket in node.outputs:
            for link in list(socket.links):
                tree.links.new(switch.outputs[0], link.to_socket)
        tree.nodes.remove(node)
        changed = True
    return changed


def ensure_graph(settings):
    """Create or upgrade the character blueprint and sync every object node."""
    if settings.mesh is None:
        return None
    tree = settings.graph
    if tree is None:
        tree = bpy.data.node_groups.new('NTE · ' + settings.mesh.name, TREE)
        tree.use_fake_user = True
        settings.graph = tree
    if not tree.graph_id:
        tree.graph_id = new_id()
    main = next((node for node in tree.nodes if node.bl_idname == 'NTEBridgeObject' and node.is_main), None)
    created = main is None
    if created:
        main = tree.nodes.new('NTEBridgeObject')
        main.is_main = True
        main.location = (0, 0)
    sync_object_node(main, settings)
    _upgrade_legacy(tree, main, settings)
    output = next((node for node in tree.nodes if node.bl_idname == 'NTEBridgeOutput'), None)
    if output is None:
        output = tree.nodes.new('NTEBridgeOutput')
        output.location = (main.location.x + 700, main.location.y)
    if created and not main.outputs[0].is_linked:
        tree.links.new(main.outputs[0], output.inputs[-1])
        output.update()
    for node in tree.nodes:
        if node.bl_idname == 'NTEBridgeObject' and node != main:
            sync_object_node(node, settings)
    return tree


def object_nodes(tree):
    return [node for node in tree.nodes if node.bl_idname == 'NTEBridgeObject']


def graph_dict(tree):
    """Serialize nodes and reroute-resolved links for core.compile_blueprint."""
    if tree is None:
        raise BridgeError('请先选择 Blender 网格以创建角色蓝图。')
    nodes = []
    for node in tree.nodes:
        kind = node.bl_idname
        if kind in {'NodeFrame', 'NodeReroute'}:
            continue
        if kind in {'NTEBridgePart', 'NTEBridgeCycle'}:
            raise BridgeError('蓝图中还有旧版节点，请点击节点上的“升级为新版蓝图”。')
        if kind == 'NTEBridgeObject':
            nodes.append({'id': node.node_id, 'type': 'OBJECT', 'label': node.draw_label(),
                          'object_id': node.target.name if node.target else '', 'main': node.is_main,
                          'parts': [slot.part_id for slot in node.slots],
                          'split': [slot.part_id for slot in node.slots if slot.split]})
        elif kind == 'NTEBridgeMaterial':
            nodes.append({'id': node.node_id, 'type': 'MATERIAL', 'label': node.draw_label()})
        elif kind == 'NTEBridgeSwitch':
            nodes.append({'id': node.node_id, 'type': 'SWITCH', 'label': node.draw_label(),
                          'comment': node.comment.strip(), 'variable': node.custom_var_name.strip(),
                          'key': node.hotkey.strip(), 'options': [s.identifier for s in node.inputs],
                          'initial_option': node.initial_option})
        elif kind == 'NTEBridgeGroup':
            nodes.append({'id': node.node_id, 'type': 'GROUP', 'label': node.label or node.name})
        elif kind == 'NTEBridgeOutput':
            nodes.append({'id': node.node_id, 'type': 'OUTPUT', 'label': '生成'})
        else:
            raise BridgeError('蓝图中有不支持的节点：' + node.name)
    links = []
    for link in tree.links:
        if link.to_node.bl_idname == 'NodeReroute' or link.is_muted:
            continue
        source = _through_reroute(link)
        if source is None:
            continue
        if not _sockets_compatible(source.from_socket, link.to_socket):
            raise BridgeError('节点「%s」→「%s」连线类型不兼容' % (source.from_node.name, link.to_node.name))
        links.append({'from_node': source.from_node.node_id, 'from_socket': source.from_socket.identifier,
                      'to_node': link.to_node.node_id, 'to_socket': link.to_socket.identifier})
    return {'id': tree.graph_id, 'nodes': nodes, 'links': links}


@persistent
def _object_slots_changed(scene, depsgraph=None):
    """Keep material inputs in step with slot edits; cheap signature check only."""
    if _SYNCING:
        return
    settings = getattr(scene, 'nte_bridge', None)
    tree = getattr(settings, 'graph', None) if settings else None
    if tree is None:
        return
    for node in object_nodes(tree):
        if node.target is None:
            continue
        current = [slot.material for slot in node.target.material_slots]
        if node.is_main:
            current_parts = [part.part_id for part in settings.parts]
            if current_parts != [slot.part_id for slot in node.slots]:
                sync_object_node(node, settings)
        elif current != [slot.material for slot in node.slots]:
            sync_object_node(node, settings)


# ------------------------------------------------------------------ operators

def _tree_node(tree_name, node_name):
    tree = bpy.data.node_groups.get(tree_name)
    return tree.nodes.get(node_name) if tree else None


class NTEBRIDGE_OT_sync_blueprint(bpy.types.Operator):
    bl_idname = 'nte_bridge.sync_blueprint'
    bl_label = '刷新角色蓝图'
    bl_description = '升级旧版节点，并按物体当前材质槽刷新材质入口'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            if ensure_graph(settings) is None:
                raise BridgeError('请先选择 Blender 网格。')
            return {'FINISHED'}
        except BridgeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class NTEBRIDGE_OT_select_node_object(bpy.types.Operator):
    bl_idname = 'nte_bridge.select_node_object'
    bl_label = '选中物体'
    object_name: StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or context.view_layer.objects.get(obj.name) is None:
            return {'CANCELLED'}
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


class NTEBRIDGE_OT_switch_option(bpy.types.Operator):
    bl_idname = 'nte_bridge.switch_option'
    bl_label = '增减选项'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    delta: IntProperty()

    def execute(self, context):
        node = _tree_node(self.tree_name, self.node_name)
        if node is None or node.bl_idname != 'NTEBridgeSwitch':
            return {'CANCELLED'}
        node.input_slot_count = max(2, min(64, node.input_slot_count + self.delta))
        node.initial_option = min(node.initial_option, node.input_slot_count - 1)
        return {'FINISHED'}


def _connected_diffuse(node):
    """Diffuse image files of Blender materials in slots this material node feeds."""
    from .blender_textures import material_diffuse_image
    files = set()
    for link in node.outputs[0].links:
        target = link.to_node
        if target.bl_idname != 'NTEBridgeObject':
            continue
        slot = next((slot for slot in target.slots if slot.part_id == link.to_socket.identifier), None)
        try:
            image = material_diffuse_image(slot.material) if slot else None
        except BridgeError:
            image = None
        if image is not None and image.source == 'FILE' and not image.packed_file and image.filepath:
            files.add(str(Path(bpy.path.abspath(image.filepath)).resolve()))
    return files


def suggest_texture_files(rows):
    """Fill empty rows from same-folder files named <base>_<suffix>; returns filled params."""
    base_row = next((row for row in rows if row.file_path.strip() and row.role == 'BASE_COLOR'), None) or \
        next((row for row in rows if row.file_path.strip()), None)
    if base_row is None:
        return []
    source = Path(bpy.path.abspath(base_row.file_path)).resolve()
    stem = _normalized_stem(source)
    suffixes = '|'.join(ROLE_SUFFIXES[base_row.role])
    base = re.sub(r'_(%s)\d*$' % suffixes, '', stem)
    if base == stem or not source.parent.is_dir():
        return []
    files = [path for path in source.parent.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file()]
    filled = []
    for row in rows:
        if row.file_path.strip():
            continue
        wanted = {base + '_' + suffix for suffix in ROLE_SUFFIXES[row.role]}
        matches = [path for path in files if _normalized_stem(path) in wanted]
        if len(matches) == 1:
            row.file_path = str(matches[0])
            filled.append(row.param)
    return filled


class NTEBRIDGE_OT_material_rows(bpy.types.Operator):
    bl_idname = 'nte_bridge.material_rows'
    bl_label = '编辑贴图参数'
    bl_description = '添加/删除贴图行；按母材质生成参数行；按同目录文件后缀补全空行'
    bl_options = {'REGISTER', 'UNDO'}
    tree_name: StringProperty()
    node_name: StringProperty()
    action: StringProperty()
    index: IntProperty()

    def execute(self, context):
        node = _tree_node(self.tree_name, self.node_name)
        if node is None or node.bl_idname != 'NTEBridgeMaterial':
            return {'CANCELLED'}
        if self.action == 'ADD':
            node.textures.add()
        elif self.action == 'REMOVE' and 0 <= self.index < len(node.textures):
            node.textures.remove(self.index)
        elif self.action == 'PARENT':
            settings = context.scene.nte_bridge
            entry = next((item for item in settings.material_catalog
                          if item.asset_path.casefold() == node.parent_path.strip().casefold()), None)
            names = []
            if entry and entry.available:
                declared = {reference.get('name', '') for reference in json.loads(entry.metadata_json).get('textures', [])}
                names = [name for name in PER_PART_PARAMETERS if name in declared]
            names = names or list(DEFAULT_PARAMETERS)
            files = {row.param: row.file_path for row in node.textures}
            node.textures.clear()
            for name in names:
                row = node.textures.add()
                row.param = name
                row.file_path = files.get(name, '')
            base = next((row for row in node.textures if row.role == 'BASE_COLOR'), None)
            diffuse = _connected_diffuse(node)
            if base is not None and not base.file_path and len(diffuse) == 1:
                base.file_path = next(iter(diffuse))
            self.report({'INFO'}, '已按%s生成 %d 个贴图参数' % ('母材质资料' if entry and entry.available else '常用参数', len(names)))
        elif self.action == 'SUGGEST':
            filled = suggest_texture_files(node.textures)
            self.report({'INFO'}, ('已补全：' + '、'.join(filled)) if filled else '没有找到唯一匹配的同名后缀贴图')
        return {'FINISHED'}


class NTEBRIDGE_OT_pick_project_asset(bpy.types.Operator):
    bl_idname = 'nte_bridge.pick_project_asset'
    bl_label = '选择工程资产'
    bl_description = '在 UE 工程 Content 中选择 .uasset，自动转换为 /Game 路径'
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.uasset', options={'HIDDEN'})
    tree_name: StringProperty(options={'HIDDEN'})
    node_name: StringProperty(options={'HIDDEN'})
    target: StringProperty(options={'HIDDEN'})

    def _content(self, context):
        project = context.scene.nte_bridge.project_file.strip()
        if not project:
            raise BridgeError('请先在“发送到 UE”中选择 UE 工程。')
        return Path(bpy.path.abspath(project)).resolve().parent / 'Content'

    def invoke(self, context, event):
        try:
            content = self._content(context)
        except BridgeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.filepath = str(content) + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        node = _tree_node(self.tree_name, self.node_name)
        try:
            content = self._content(context)
            chosen = Path(self.filepath).resolve()
            if chosen.suffix.lower() != '.uasset' or not chosen.is_relative_to(content):
                raise BridgeError('请选择当前 UE 工程 Content 目录中的 .uasset。')
            path = '/Game/' + chosen.relative_to(content).with_suffix('').as_posix()
            if node is None or self.target not in {'material_path', 'parent_path'}:
                return {'CANCELLED'}
            setattr(node, self.target, path)
            return {'FINISHED'}
        except BridgeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class NTEBRIDGE_OT_view_group_objects(bpy.types.Operator):
    '''递归解析当前群组下的全部物体并在 3D 视图中局部显示；再次点击退出局部视图'''
    bl_idname = 'nte_bridge.view_group_objects'
    bl_label = '查看递归解析预览'
    node_name: StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, 'edit_tree', None) or context.scene.nte_bridge.graph
        node = tree.nodes.get(self.node_name) if tree else None
        area = next((a for window in context.window_manager.windows for a in window.screen.areas
                     if a.type == 'VIEW_3D'), None)
        window = next((w for w in context.window_manager.windows if area in w.screen.areas[:]), None)
        if node is None or area is None:
            self.report({'WARNING'}, '没有可用的 3D 视图')
            return {'CANCELLED'}
        space = area.spaces.active
        if space.local_view:
            with context.temp_override(window=window, area=area):
                bpy.ops.view3d.localview()
            return {'FINISHED'}
        objects, seen = set(), set()

        def collect(current):
            if current in seen:
                return
            seen.add(current)
            if current.bl_idname == 'NTEBridgeObject' and current.target:
                objects.add(current.target)
            for socket in current.inputs:
                if socket.bl_idname == OBJECT_SOCKET:
                    for link in socket.links:
                        source = _through_reroute(link)
                        if source:
                            collect(source.from_node)
        collect(node)
        objects = [obj for obj in objects if context.view_layer.objects.get(obj.name)]
        if not objects:
            self.report({'WARNING'}, '该群组下没有物体')
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in objects:
            obj.hide_set(False)
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        with context.temp_override(window=window, area=area):
            bpy.ops.view3d.localview()
        return {'FINISHED'}


def _trailing_number(obj):
    match = re.search(r'_([0-9]+)$', obj.name)
    return int(match.group(1)) if match else 0


class NTEBRIDGE_OT_blueprint_add_objects(bpy.types.Operator):
    bl_idname = 'nte_bridge.blueprint_add_objects'
    bl_label = '加入角色蓝图'
    bl_description = '为选中的网格物体创建物体节点'
    bl_options = {'REGISTER', 'UNDO'}
    mode: EnumProperty(items=[('STATIC', '加入蓝图（常驻）', ''), ('SWITCH', '创建物体切换', '')])

    def execute(self, context):
        settings = context.scene.nte_bridge
        tree = ensure_graph(settings)
        if tree is None:
            self.report({'ERROR'}, '请先在 NTE Bridge 侧栏选择 Blender 网格。')
            return {'CANCELLED'}
        selected = sorted([obj for obj in context.selected_objects if obj.type == 'MESH' and obj != settings.mesh],
                          key=_trailing_number)
        if not selected:
            self.report({'WARNING'}, '请选择主网格以外的网格物体')
            return {'CANCELLED'}
        output = next(node for node in tree.nodes if node.bl_idname == 'NTEBridgeOutput')
        existing = {node.target: node for node in object_nodes(tree) if node.target}
        base_x = max((node.location.x for node in tree.nodes), default=0) - 900
        base_y = min((node.location.y for node in tree.nodes), default=0) - 260
        created = []
        for index, obj in enumerate(selected):
            node = existing.get(obj)
            if node is None:
                node = tree.nodes.new('NTEBridgeObject')
                node.location = (base_x, base_y - index * 220)
                node.target = obj
            created.append(node)
        if self.mode == 'STATIC':
            for node in created:
                if not node.outputs[0].is_linked:
                    tree.links.new(node.outputs[0], output.inputs[-1])
                    output.update()
        else:
            switch = tree.nodes.new('NTEBridgeSwitch')
            switch.location = (base_x + 450, base_y)
            switch.input_slot_count = max(2, len(created))
            for node, socket in zip(created, switch.inputs):
                if not node.outputs[0].is_linked:
                    tree.links.new(node.outputs[0], socket)
            tree.links.new(switch.outputs[0], output.inputs[-1])
            output.update()
        self.report({'INFO'}, '已加入 %d 个物体' % len(created))
        return {'FINISHED'}


def _node_add_menu(self, context):
    space = context.space_data
    if not space or space.type != 'NODE_EDITOR' or space.tree_type != TREE:
        return
    self.layout.separator()
    for node_type, label, icon in [('NTEBridgeObject', '物体', 'OBJECT_DATAMODE'),
                                   ('NTEBridgeMaterial', '材质球', 'MATERIAL'),
                                   ('NTEBridgeSwitch', '物体切换', 'SHADERFX'),
                                   ('NTEBridgeGroup', '群组', 'GROUP'),
                                   ('NTEBridgeOutput', '生成', 'EXPORT')]:
        op = self.layout.operator('node.add_node', text=label, icon=icon)
        op.type = node_type
        op.use_transform = True


def _object_context_menu(self, context):
    if getattr(context.scene, 'nte_bridge', None) is None:
        return
    self.layout.separator()
    self.layout.operator('nte_bridge.blueprint_add_objects', text='NTE：加入蓝图（常驻）',
                         icon='NODETREE').mode = 'STATIC'
    self.layout.operator('nte_bridge.blueprint_add_objects', text='NTE：创建物体切换',
                         icon='SHADERFX').mode = 'SWITCH'


CLASSES = (NTEBridgeObjectSocket, NTEBridgeMaterialSocket, NTEBridgeSocket, NTEBridgeTree,
           NTEBridgeObjectSlot, NTEBridgeObject, NTEBridgeMaterialTexture, NTEBridgeMaterial,
           NTEBridgeSwitch, NTEBridgeGroup, NTEBridgeOutput, NTEBridgeStateEntry, NTEBridgePart,
           NTEBridgeCycle, NTEBRIDGE_OT_sync_blueprint, NTEBRIDGE_OT_select_node_object,
           NTEBRIDGE_OT_switch_option, NTEBRIDGE_OT_material_rows, NTEBRIDGE_OT_pick_project_asset,
           NTEBRIDGE_OT_view_group_objects, NTEBRIDGE_OT_blueprint_add_objects)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.NODE_MT_add.append(_node_add_menu)
    bpy.types.VIEW3D_MT_object_context_menu.append(_object_context_menu)
    bpy.app.handlers.depsgraph_update_post.append(_object_slots_changed)


def unregister():
    if _object_slots_changed in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_object_slots_changed)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_object_context_menu)
    bpy.types.NODE_MT_add.remove(_node_add_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _REPORT_CACHE.clear()
