"""Small cooked snapshot for real Blender UI/persistence tests; no UE launch."""
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge import blender_packaging
from nte_bridge.core import write_json
from nte_bridge.cooking import cook_cache_directory, snapshot_cooked, prepare_cook_request, cancel_cook_request


def create(output):
    assert bpy.app.version == (4, 5, 7)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    project = output / 'UE/HT.uproject'
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text('{}')
    settings = bpy.context.scene.nte_bridge
    settings.project_file = str(project)
    settings.mesh_path = '/Game/Characters/Player/A/SK_Body'
    settings.cache_root = str(output / 'Cache')
    settings.cook_export_directory = str(output / 'xg/HT/Content/Characters')
    report = recook(settings, 'A', 'initial')
    return settings, report


def recook(settings, role, generation):
    project = Path(settings.project_file)
    scope = '/Game/Characters/Player/' + role
    cache = cook_cache_directory(Path(settings.cache_root), project, scope)
    (project.parent / 'Content/Characters/Player' / role).mkdir(parents=True, exist_ok=True)
    request = dict(schema_version=1, project_file=str(project), character_folder=scope, excluded_assets=[])
    if not cache.exists():
        reservation = prepare_cook_request(Path(settings.cache_root), request)
        cancel_cook_request(reservation['request_path'], reservation['request_token'])
    cooked = cache / 'cooked/Windows'
    assets = []
    for name, kind in [('SK_Body', 'SkeletalMesh'), ('T_Diffuse', 'Texture2D'), ('T_Normal', 'Texture2D')]:
        asset = scope + '/' + name
        for extension in ('.uasset', '.uexp'):
            file = cooked / ('HT/Content/' + asset[len('/Game/'):] + extension)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes((asset + generation + extension).encode())
        assets.append(dict(asset_path=asset, asset_type=kind))
    report = snapshot_cooked(request, {'assets': assets}, cooked, generation, 'a' * 64)
    path = cache / 'cook_report.json'
    write_json(path, report)
    blender_packaging.load_cooked_assets(settings, path)
    return path
