"""Visible native dialog: real async worker, progress, failure/retry, success close."""
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from blender_packaging_fixture import create
from nte_bridge import blender_assets, blender_ui, blender_packaging
from nte_bridge.core import write_json

OUT = ROOT / 'artifacts/dialog033'
OUT.mkdir(parents=True, exist_ok=True)
for name in ('error.txt', 'result.json'):
    (OUT / name).unlink(missing_ok=True)
settings, report = create(OUT / 'fixture')
settings.package_output = str(OUT / 'Mods')
settings.mod_name = 'DialogTest_P'
settings.cook_assets[0].selected = True
bpy.context.preferences.view.show_splash = False
bpy.context.preferences.view.language = 'zh_HANS'
checks, mode = [], 'failure'
original_spawn = blender_ui._WorkerModal._spawn
started = time.monotonic()


def spawn(operator, command, log_path):
    code = """import json,os,sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nte_bridge.progress import report_progress
for i in range(8):
    report_progress('正在导出并打包所选资产', i, 8)
    time.sleep(.4)
ok=sys.argv[3]=='success'
Path(sys.argv[2]).write_text(json.dumps(dict(success=ok,errors=[] if ok else ['Test failure; retry available'])),encoding='utf-8')
sys.exit(0 if ok else 1)
"""
    original_spawn(operator, [str(blender_packaging.worker_python()), '-c', code,
                             str(ROOT / 'blender_addon'), str(operator._report), mode], log_path)


patcher = patch.object(blender_ui._WorkerModal, '_spawn', spawn)
patcher.start()


def fail():
    (OUT / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    patcher.stop()
    bpy.ops.wm.quit_blender()


def later(callback, delay=.5):
    def guarded():
        try:
            return callback()
        except Exception:
            fail()
    bpy.app.timers.register(guarded, first_interval=delay)


def asset_window():
    return next((window for window in bpy.context.window_manager.windows if blender_assets.is_asset_window(window)), None)


def capture(name):
    window = asset_window()
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        bpy.ops.screen.screenshot(filepath=str(OUT / (name + '.png')))


def open_dialog():
    global parent
    parent = bpy.context.window.as_pointer()
    area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
    with bpy.context.temp_override(area=area):
        assert bpy.ops.nte_bridge.select_cooked_assets('INVOKE_DEFAULT') == {'FINISHED'}
    later(click_export, 1.5)


def click_export():
    window = asset_window()
    assert window
    capture('before')
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.nte_bridge.select_cooked_assets() == {'RUNNING_MODAL'}
    later(during, 1.5)


def during():
    assert settings.busy, 'Export did not start the worker'
    assert asset_window() and blender_assets._popups, 'Dialog disappeared during task'
    assert settings.elapsed > .25 and settings.progress_factor >= 0
    capture('working')
    checks.append('export-keeps-native-dialog-visible-with-progress-and-elapsed-time')
    later(after_failure, 3.0)


def after_failure():
    global mode
    assert not settings.busy and asset_window()
    assert 'Test failure' in settings.status
    capture('failed')
    checks.append('failed-task-keeps-dialog-and-choices-for-retry')
    mode = 'success'
    window = asset_window()
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.nte_bridge.select_cooked_assets() == {'RUNNING_MODAL'}
    later(after_success, 4.8)


def after_success():
    assert not settings.busy and not asset_window()
    assert any(window.as_pointer() == parent for window in bpy.context.window_manager.windows)
    assert '打包完成' in settings.status
    checks.append('success-closes-only-owned-asset-window-after-worker-completes')
    write_json(OUT / 'result.json', dict(success=True, blender=bpy.app.version_string, checks=checks))
    patcher.stop()
    bpy.ops.wm.quit_blender()


later(open_dialog, 2.0)
