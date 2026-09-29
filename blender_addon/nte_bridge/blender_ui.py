"""Small, purpose-built node editor and explicit single-character profile."""

from pathlib import Path
import json
import subprocess

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from .blender_export import finish_job, graph_dict, new_id, prepare_job, profile_manifest
from .core import BridgeError

_ENUM_CACHE = {}


def _mesh_poll(self, obj):
    return obj.type == 'MESH'


def _rig_poll(self, obj):
    return obj.type == 'ARMATURE'


class NTEBridgePartEntry(bpy.types.PropertyGroup):
    part_id: StringProperty()
    source_slot: IntProperty(min=0)
    display_name: StringProperty(name="部件名称")
    material_path: StringProperty(name="UE 材质路径", description="完整 /Game/... 包路径；游戏原材质只占位，不打包")
    source_material: PointerProperty(type=bpy.types.Material)


class NTEBridgeTextureEntry(bpy.types.PropertyGroup):
    texture_id: StringProperty()
    file_path: StringProperty(name="源贴图", subtype='FILE_PATH')
    asset_path: StringProperty(name="UE 贴图路径")
    role: EnumProperty(name="用途", items=[
        ('BASE_COLOR', '漫射 · BC7 / sRGB', ''), ('ID_TEX', 'ID · BC7 / 线性', ''),
        ('LIGHT_MAP', 'LightMap · BC7 / 线性', ''), ('NORMAL', '法线 · BC5 / 线性', '')])


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
        layout.label(text='v0.1：编译预览，暂不生成游戏切换', icon='INFO')


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
        layout.operator('nte_bridge.export', text='导出桥接任务', icon='EXPORT')


class NTEBridgeSettings(bpy.types.PropertyGroup):
    character_id: StringProperty()
    mesh_id: StringProperty()
    mesh: PointerProperty(name="角色网格", type=bpy.types.Object, poll=_mesh_poll)
    bound_mesh: PointerProperty(type=bpy.types.Object)
    armature: PointerProperty(name="角色骨架", type=bpy.types.Object, poll=_rig_poll)
    graph: PointerProperty(name="节点图", type=bpy.types.NodeTree)
    project_file: StringProperty(name="UE 工程", subtype='FILE_PATH')
    mesh_path: StringProperty(name="网格包路径", description="如 /Game/Characters/Player/Test/Test")
    skeleton_path: StringProperty(name="骨架包路径")
    physics_path: StringProperty(name="物理资产路径", description="可留空；指定时作为占位资源，不打包")
    create_placeholders: BoolProperty(name="允许创建缺失占位资源", default=False,
        description="仅供 UE 导入引用；占位材质、骨架、物理资产不会进入打包清单")
    parts: CollectionProperty(type=NTEBridgePartEntry)
    active_part: IntProperty(min=0)
    textures: CollectionProperty(type=NTEBridgeTextureEntry)
    active_texture: IntProperty(min=0)
    job_root: StringProperty(name="桥接任务目录", subtype='DIR_PATH', default='//NTEBridgeJobs')
    engine_dir: StringProperty(name="UE 安装目录", subtype='DIR_PATH')
    sync_mode: EnumProperty(name="同步方式", items=[
        ('remote', '发送到已打开的 UE', '需要目标工程启用本机 Python 远程执行'),
        ('commandlet', 'UE 关闭时后台导入', '目标工程必须关闭；后台启动命令行编辑器导入')])
    packager_source: StringProperty(name="外部打包器目录", subtype='DIR_PATH',
        description="包含 NteMorphTargetPatch.exe、retoc.exe 和 Oodle DLL 的目录")
    package_output: StringProperty(name="Mod 输出目录", subtype='DIR_PATH')
    mod_name: StringProperty(name="Mod 名称", default='NTEBridgeMod')
    last_manifest: StringProperty(name="最近任务", subtype='FILE_PATH')
    last_report: StringProperty(name="最近报告", subtype='FILE_PATH')
    status: StringProperty(default='选择网格和骨架，填写 UE 路径，再刷新部件槽。')
    busy: BoolProperty(default=False, options={'SKIP_SAVE'})


class NTEBRIDGE_OT_refresh_slots(bpy.types.Operator):
    bl_idname = 'nte_bridge.refresh_slots'
    bl_label = '刷新部件槽 / 初始化节点图'
    bl_description = '按槽索引保留已有映射；修改槽顺序后必须重新核对。不会合并相同材质槽'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.nte_bridge
        if not settings.mesh:
            self.report({'ERROR'}, '请先选择角色网格')
            return {'CANCELLED'}
        if not settings.armature:
            settings.armature = settings.mesh.find_armature()
        same_mesh = settings.bound_mesh == settings.mesh
        if not same_mesh:
            settings.character_id = new_id()
            settings.mesh_id = new_id()
            settings.graph = None
            settings.last_manifest = ''
            settings.last_report = ''
            if settings.mesh.find_armature():
                settings.armature = settings.mesh.find_armature()
        settings.character_id = settings.character_id or new_id()
        settings.mesh_id = settings.mesh_id or new_id()
        old = {p.source_slot: (p.part_id, p.display_name, p.material_path) for p in settings.parts} if same_mesh else {}
        settings.parts.clear()
        for index, slot in enumerate(settings.mesh.material_slots):
            part = settings.parts.add()
            part.part_id, part.display_name, part.material_path = old.get(index, (new_id(), slot.name or '部件 %d' % index, ''))
            part.name = part.part_id
            part.source_slot = index
            part.source_material = slot.material
        settings.bound_mesh = settings.mesh
        if not settings.graph:
            settings.graph = bpy.data.node_groups.new('NTE · ' + settings.mesh.name, 'NTEBridgeTree')
            settings.graph.graph_id = new_id()
            settings.graph.use_fake_user = True
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
        settings.status = '已识别 %d 个独立槽；请逐槽核对 UE 材质路径。' % len(settings.parts)
        self.report({'INFO'}, settings.status)
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


class _WorkerModal:
    @classmethod
    def poll(cls, context):
        return not context.scene.nte_bridge.busy

    def _launch(self, context, command, log_path):
        self._log = Path(log_path).open('w', encoding='utf-8')
        try:
            self._process = subprocess.Popen(command, stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            self._log.close()
            raise
        self._settings = context.scene.nte_bridge
        self._settings.busy = True
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self.report({'WARNING'}, '任务进行中；取消请使用任务进程，不在写入中强制关闭。')
        if event.type != 'TIMER' or self._process.poll() is None:
            return {'PASS_THROUGH'}
        context.window_manager.event_timer_remove(self._timer)
        self._log.close()
        self._settings.busy = False
        try:
            self._complete(self._process.returncode)
            self.report({'INFO'}, self._settings.status)
            return {'FINISHED'}
        except Exception as error:
            self._settings.status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class NTEBRIDGE_OT_export(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.export'
    bl_label = '导出桥接任务'
    bl_description = '导出临时副本网格与纹理；不会保存或改写原始 .blend'

    def execute(self, context):
        try:
            self._job = prepare_job(context)
            context.scene.nte_bridge.status = '正在后台导出 FBX 临时副本…'
            return self._launch(context, self._job['command'], self._job['job_dir'] / 'export.log')
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        path = finish_job(self._job, code)
        self._settings.last_manifest = str(path)
        pending = bool(self._job['manifest']['features'])
        self._settings.status = ('导出完成；切换功能仅编译预览，尚未生成，不能打包。' if pending
                                 else '导出完成，可发送到 UE。')


class NTEBRIDGE_OT_worker(_WorkerModal, bpy.types.Operator):
    bl_idname = 'nte_bridge.worker'
    bl_label = '运行桥接任务'
    action: EnumProperty(items=[('sync', '发送到 UE', ''), ('package', '烘焙并打包', '')])

    def execute(self, context):
        settings = context.scene.nte_bridge
        try:
            manifest = Path(bpy.path.abspath(settings.last_manifest)).resolve()
            if not settings.last_manifest or not manifest.is_file():
                raise BridgeError('请先成功导出桥接任务。')
            engine = Path(bpy.path.abspath(settings.engine_dir)).resolve()
            python = engine / 'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'
            if not python.is_file():
                raise BridgeError('UE 安装目录中找不到 Python；请选择包含 Engine 的引擎目录。')
            self._report = manifest.parent / (self.action + '_report.json')
            command = [str(python), str(Path(__file__).with_name('cli.py')), self.action,
                       '--manifest', str(manifest), '--engine-dir', str(engine), '--report', str(self._report)]
            if self.action == 'sync':
                command += ['--mode', settings.sync_mode]
            if self.action == 'package':
                if not settings.packager_source or not settings.package_output:
                    raise BridgeError('请填写外部打包器目录和 Mod 输出目录。')
                command += ['--packager-source', bpy.path.abspath(settings.packager_source),
                            '--output-dir', bpy.path.abspath(settings.package_output), '--mod-name', settings.mod_name]
            settings.status = '正在后台' + ('同步 UE 资产…' if self.action == 'sync' else '烘焙并打包…')
            settings.last_report = str(self._report)
            return self._launch(context, command, manifest.parent / (self.action + '.log'))
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

    def _complete(self, code):
        if not self._report.is_file():
            raise BridgeError('没有收到任务报告；请查看任务日志。')
        report = json.loads(self._report.read_text(encoding='utf-8-sig'))
        if code or not report.get('success'):
            raise BridgeError('任务失败：' + '; '.join(str(e) for e in report.get('errors', ['详见报告'])))
        if report.get('features_applied') is False:
            self._settings.status = '资产同步成功；运行时功能未生成，该任务不能打包。'
        else:
            self._settings.status = '任务完成；结果与警告详见报告。'


class NTEBRIDGE_UL_parts(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text='%d · %s' % (item.source_slot, item.display_name), icon='MATERIAL')
        layout.label(text='已映射' if item.material_path else '待映射', icon='CHECKMARK' if item.material_path else 'ERROR')


class NTEBRIDGE_UL_textures(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=Path(item.file_path).name or '新贴图', icon='IMAGE_DATA')
        layout.label(text=item.role)


class NTEBRIDGE_PT_main(bpy.types.Panel):
    bl_label = 'NTE Bridge · 异环桥接'
    bl_idname = 'NTEBRIDGE_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'NTE Bridge'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.nte_bridge
        layout.label(text='v0.1 · 资源桥接；功能节点仅编译预览', icon='INFO')
        column = layout.column()
        column.enabled = not settings.busy
        for prop in ('mesh', 'armature', 'project_file', 'mesh_path', 'skeleton_path', 'physics_path'):
            column.prop(settings, prop)
        column.prop(settings, 'create_placeholders')
        column.operator('nte_bridge.refresh_slots', icon='FILE_REFRESH')
        column.template_list('NTEBRIDGE_UL_parts', '', settings, 'parts', settings, 'active_part', rows=4)
        if settings.parts and settings.active_part < len(settings.parts):
            part = settings.parts[settings.active_part]
            column.prop(part, 'display_name')
            row = column.row()
            row.enabled = False
            row.prop(part, 'source_material', text='源材质')
            column.prop(part, 'material_path')
        column.separator()
        column.label(text='只加入需要替换的贴图')
        column.template_list('NTEBRIDGE_UL_textures', '', settings, 'textures', settings, 'active_texture', rows=3)
        row = column.row(align=True)
        for action, text in [('ADD', '添加贴图'), ('REMOVE', '删除贴图')]:
            row.operator('nte_bridge.edit_texture', text=text).action = action
        if settings.textures and settings.active_texture < len(settings.textures):
            entry = settings.textures[settings.active_texture]
            for prop in ('file_path', 'asset_path', 'role'):
                column.prop(entry, prop)
        column.separator()
        column.operator('nte_bridge.open_graph', icon='NODETREE')
        column.operator('nte_bridge.validate', icon='CHECKMARK')
        column.prop(settings, 'job_root')
        column.operator('nte_bridge.export', icon='EXPORT')
        column.prop(settings, 'last_manifest')
        column.prop(settings, 'engine_dir')
        column.prop(settings, 'sync_mode')
        column.operator('nte_bridge.worker', text='发送最近任务到 UE', icon='IMPORT').action = 'sync'
        column.separator()
        for prop in ('packager_source', 'package_output', 'mod_name'):
            column.prop(settings, prop)
        column.operator('nte_bridge.worker', text='烘焙并打包最近任务', icon='PACKAGE').action = 'package'
        layout.label(text=settings.status, icon='TIME' if settings.busy else 'INFO')
        layout.prop(settings, 'last_report')


def _add_menu(self, context):
    space = context.space_data
    if space and space.type == 'NODE_EDITOR' and space.tree_type == 'NTEBridgeTree':
        self.layout.separator()
        for node_type, label in [('NTEBridgePart', 'NTE · 部件'), ('NTEBridgeGroup', 'NTE · 组合'),
                                 ('NTEBridgeCycle', 'NTE · 材质组循环'), ('NTEBridgeOutput', 'NTE · 角色输出')]:
            op = self.layout.operator('node.add_node', text=label)
            op.type = node_type
            op.use_transform = True


CLASSES = (NTEBridgePartEntry, NTEBridgeTextureEntry, NTEBridgeStateEntry, NTEBridgeSocket,
           NTEBridgeTree, NTEBridgePart, NTEBridgeGroup, NTEBridgeCycle, NTEBridgeOutput,
           NTEBridgeSettings, NTEBRIDGE_OT_refresh_slots, NTEBRIDGE_OT_edit_state,
           NTEBRIDGE_OT_edit_texture, NTEBRIDGE_OT_open_graph, NTEBRIDGE_OT_validate,
           NTEBRIDGE_OT_export, NTEBRIDGE_OT_worker, NTEBRIDGE_UL_parts,
           NTEBRIDGE_UL_textures, NTEBRIDGE_PT_main)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.nte_bridge = PointerProperty(type=NTEBridgeSettings)
    bpy.types.NODE_MT_add.append(_add_menu)


def unregister():
    bpy.types.NODE_MT_add.remove(_add_menu)
    if hasattr(bpy.types.Scene, 'nte_bridge'):
        del bpy.types.Scene.nte_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _ENUM_CACHE.clear()
