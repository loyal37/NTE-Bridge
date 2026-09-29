"""Reopen an unchanged .blend and retain packaging choices on Blender 4.5.7."""
import json
from pathlib import Path
import sys
import uuid
import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from blender_packaging_fixture import create, recook
from nte_bridge import blender_packaging, blender_state
from nte_bridge.core import write_json

out = ROOT / 'artifacts/state033' / uuid.uuid4().hex[:8]
settings, report = create(out)
blend = out / 'Character.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(blend))
unchanged_blend = blend.read_bytes()
settings.package_output = str(out / 'Mods')
settings.mod_name = 'RememberMe_P'
settings.cook_assets[0].selected = True
settings.cook_assets[1].selected = True
selected = [entry.asset_path for entry in settings.cook_assets if entry.selected]
assert len(selected) == 2
assert blend.read_bytes() == unchanged_blend
bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
settings = bpy.context.scene.nte_bridge
assert settings.package_output == str(out / 'Mods') and settings.mod_name == 'RememberMe_P'
assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == selected
checks = ['settings-auto-save-without-writing-model', 'reopen-restores-output-name-and-checked-assets']
old_hash = settings.cook_report_sha256
blender_packaging.clear_cook(settings)
recook(settings, 'A', 'updated')
assert settings.cook_report_sha256 != old_hash
assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == selected
bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
settings = bpy.context.scene.nte_bridge
assert settings.cook_report_sha256 != old_hash
assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == selected
checks += ['recook-retains-matching-choices', 'reopen-stale-blend-rebinds-latest-valid-report']
settings.cook_use_custom = True
settings.cook_folder = '/Game/Characters/Player/B'
recook(settings, 'B', 'other-role')
settings.mod_name = 'RoleB_P'
settings.package_output = str(out / 'OtherMods')
next(entry for entry in settings.cook_assets if entry.asset_path.endswith('/T_Normal') and not entry.dependency).selected = True
settings.cook_folder = '/Game/Characters/Player/A'
recook(settings, 'A', 'return-role')
assert settings.mod_name == 'RememberMe_P' and settings.package_output == str(out / 'Mods')
assert [entry.asset_path for entry in settings.cook_assets if entry.selected] == selected
checks += ['different-role-choices-independent', 'return-to-role-restores-own-choices']
for entry in settings.cook_assets:
    entry.selected = False
blender_packaging.clear_cook(settings)
recook(settings, 'A', 'empty-selection')
assert not any(entry.selected for entry in settings.cook_assets)
files = list((Path(settings.cache_root) / 'Settings').glob('*.json'))
assert len(files) == 2  # One unsaved-session profile plus this saved blend.
assert sum(json.loads(path.read_text(encoding='utf-8'))['identity'][1] != '<unsaved>' for path in files) == 1
assert blend.read_bytes() == unchanged_blend
checks += ['explicit-empty-selection-is-remembered', 'one-fixed-settings-file-no-model-autosave']
result = dict(success=True, blender=bpy.app.version_string, checks=checks)
write_json(out / 'result.json', result)
write_json(ROOT / 'artifacts/state033/result.json', result)
print('NTE_STATE_SMOKE=' + json.dumps(result), flush=True)
