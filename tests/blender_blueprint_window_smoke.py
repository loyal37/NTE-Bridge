"""Visible Blender 4.5.7: the character blueprint opens in its own window (LoyalTools style).

Run without --background: blender --factory-startup --python tests/blender_blueprint_window_smoke.py
"""
import json
from pathlib import Path
import shutil
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
sys.path.insert(0, str(ROOT / 'tests'))
import nte_bridge
from nte_bridge import blender_nodes
from blender_blueprint_smoke import image_file, quad_mesh

OUT = ROOT / 'artifacts' / 'blender_blueprint_window'
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
bpy.context.preferences.view.show_splash = False
checks = []
state = {}


def fail():
    (OUT / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    bpy.ops.wm.quit_blender()


def later(callback, delay=0.8):
    def guarded():
        try:
            return callback()
        except Exception:
            fail()
    bpy.app.timers.register(guarded, first_interval=delay)


def blueprint_windows(tree):
    return [window for window in bpy.context.window_manager.windows
            if any(space.type == 'NODE_EDITOR' and space.node_tree == tree
                   for area in window.screen.areas for space in area.spaces)]


def build():
    nte_bridge.register()
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.armature_add()
    rig = bpy.context.object
    rig.name = 'Armature'
    bpy.ops.object.mode_set(mode='EDIT')
    rig.data.edit_bones[0].name = 'root'
    bpy.ops.object.mode_set(mode='OBJECT')
    diffuse = image_file(OUT / 'coat_d.png', 0.8)
    for name in ('coat_m', 'coat_n', 'coat_ mask'):
        bpy.data.images.remove(image_file(OUT / (name + '.png'), 0.4))
    coat_material = bpy.data.materials.new('CoatMaterial')
    coat_material.use_nodes = True
    texture = coat_material.node_tree.nodes.new('ShaderNodeTexImage')
    texture.image = diffuse
    coat_material.node_tree.links.new(texture.outputs['Color'],
                                      coat_material.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
    body = quad_mesh('Body', rig, [bpy.data.materials.new('MainA'), bpy.data.materials.new('MainB')], (0, 0, 0), ['Smile'])
    coat = quad_mesh('Coat_1', rig, [coat_material], (0, 0.5, 0))
    belt = quad_mesh('Belt_2', rig, [bpy.data.materials.new('BeltMaterial')] * 2, (0, -0.5, 0.2))
    settings = bpy.context.scene.nte_bridge
    settings.mesh_path = '/Game/Characters/Player/036_zankou_1/player_036_zankou_new_skin'
    settings.mesh = body
    for part in settings.parts:
        part.material_path = '/Game/Characters/Player/036_zankou_1/MI_player_036_zankou_new_1'
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    coat.select_set(True)
    belt.select_set(True)
    view = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
    with bpy.context.temp_override(area=view):
        assert bpy.ops.nte_bridge.blueprint_add_objects(mode='SWITCH') == {'FINISHED'}
    tree = settings.graph
    switch = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeSwitch')
    switch.comment, switch.hotkey, switch.input_slot_count = '外套', 'alt 6', 3
    nodes = {n.target.name: n for n in blender_nodes.object_nodes(tree)}
    material = tree.nodes.new('NTEBridgeMaterial')
    material.mi_name, material.parent_path = 'MI_zankou_cloth', '/Game/Characters/Player/019_mint/ter_new_2/cloth_ter/MI_player_019_mint_2'
    tree.links.new(material.outputs[0], nodes['Coat_1'].inputs[0])
    for action in ('PARENT', 'SUGGEST'):
        assert bpy.ops.nte_bridge.material_rows(tree_name=tree.name, node_name=material.name, action=action) == {'FINISHED'}
    original = tree.nodes.new('NTEBridgeMaterial')
    original.source, original.material_path = 'ORIGINAL', '/Game/Characters/Player/036_zankou_1/ter/cloth_ter/MI_kuhara_Zankou_transparent'
    for socket in nodes['Belt_2'].inputs:
        tree.links.new(original.outputs[0], socket)
    main = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeObject' and n.is_main)
    output = next(n for n in tree.nodes if n.bl_idname == 'NTEBridgeOutput')
    main.location, output.location, switch.location = (0, 420), (900, 260), (450, 0)
    nodes['Coat_1'].location, nodes['Belt_2'].location = (0, 40), (0, -260)
    material.location, original.location = (-420, 60), (-420, -300)
    for index, node in enumerate(n for n in tree.nodes if n.bl_idname == 'NTEBridgeTexture'):
        node.location = (-800, 300 - index * 190)
    state['tree'] = tree
    later(open_window, 0.5)


def open_window():
    tree = state['tree']
    state['before'] = len(bpy.context.window_manager.windows)
    view = next(area for area in bpy.context.window.screen.areas if area.type == 'VIEW_3D')
    with bpy.context.temp_override(area=view):
        assert bpy.ops.nte_bridge.open_graph() == {'FINISHED'}
    later(check_window, 1.5)


def check_window():
    tree = state['tree']
    windows = blueprint_windows(tree)
    assert len(bpy.context.window_manager.windows) == state['before'] + 1, 'no new window was created'
    assert len(windows) == 1, 'blueprint should be shown in exactly one window'
    window = windows[0]
    assert window != bpy.context.window_manager.windows[0], 'blueprint replaced the main window'
    assert len(window.screen.areas) == 1, 'blueprint window should contain only the node editor'
    area = window.screen.areas[0]
    space = area.spaces.active
    assert area.type == 'NODE_EDITOR' and area.ui_type == 'NTEBridgeTree' and space.pin and space.node_tree == tree
    main_view = next(area for area in bpy.context.window_manager.windows[0].screen.areas if area.type == 'VIEW_3D')
    assert main_view is not None, 'main 3D view was changed'
    checks.append('opens-standalone-window-with-only-pinned-blueprint')
    region = next(item for item in area.regions if item.type == 'WINDOW')
    origin = region.view2d.view_to_region(0, 0, clip=False)
    assert abs(origin[0] - region.width / 2) > 5, 'Frame All was not applied to the new window'
    scale = bpy.context.preferences.system.ui_scale
    for node in (n for n in tree.nodes if n.bl_idname.startswith('NTEBridge')):
        x, y = region.view2d.view_to_region(node.location.x * scale, node.location.y * scale, clip=False)
        assert 0 <= x <= region.width and 0 <= y <= region.height, 'node outside the framed view: ' + node.name
    checks.append('frames-all-nodes-after-opening')
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        bpy.ops.screen.screenshot(filepath=str(OUT / 'blueprint_window.png'))
    space.pin = False  # A replacement window is pinned again by the operator.
    view = next(area for area in bpy.context.window_manager.windows[0].screen.areas if area.type == 'VIEW_3D')
    with bpy.context.temp_override(window=bpy.context.window_manager.windows[0], area=view):
        assert bpy.ops.nte_bridge.open_graph() == {'FINISHED'}
    later(check_reopen, 1.5)


def check_reopen():
    tree = state['tree']
    windows = blueprint_windows(tree)
    assert len(windows) == 1, 'reopen left more than one blueprint window'
    area = max(windows[0].screen.areas, key=lambda item: item.width * item.height)
    assert area.spaces.active.pin and len(windows[0].screen.areas) == 1, 'reopen did not replace the old window'
    assert len(bpy.context.window_manager.windows) == state['before'] + 1, 'reopen accumulated windows'
    checks.append('reopen-replaces-previous-blueprint-window')
    (OUT / 'result.json').write_text(json.dumps({'success': True, 'blender': bpy.app.version_string, 'checks': checks},
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    bpy.ops.wm.quit_blender()


later(build, 1.5)
