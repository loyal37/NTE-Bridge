"""Blender 4.5.7 regression for cache routing, migration and saved profiles.

Uses the isolated synthetic fixture from blender_smoke.py, never a user scene.
Run with --background --factory-startup --disable-autoexec --python-exit-code 1.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
import nte_bridge
from nte_bridge import blender_ui
from nte_bridge.blender_cache import ensure_cache, selected_manifest
from nte_bridge.blender_export import export_job
from nte_bridge.core import BridgeError


def tree_state(root):
    """A read-only inventory proves migration/export did not touch the old cache."""
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()} if root.exists() else {}


def rejected(call, message):
    try:
        call()
    except BridgeError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError('Expected a cache path validation failure')


def main():
    assert bpy.app.version == (4, 5, 7), bpy.app.version_string
    bpy.ops.wm.read_factory_settings(use_empty=True)
    nte_bridge.register()
    output = ROOT / 'artifacts/cache_smoke' / uuid.uuid4().hex[:10]
    output.mkdir(parents=True)
    legacy_root = Path(os.environ['LOCALAPPDATA']) / 'NTEBridge/Jobs'
    legacy_before = tree_state(legacy_root)
    checks = []

    settings = bpy.context.scene.nte_bridge
    assert not bpy.data.filepath and not settings.cache_root and not settings.job_root
    rejected(lambda: ensure_cache(settings), '不会自动使用 C 盘')
    settings.cache_root = '//未保存 相对缓存'
    rejected(lambda: ensure_cache(settings), '未保存')
    assert not settings.job_root
    checks.append('unsaved-no-context-and-relative-path-rejected-without-fallback')

    source = ROOT / 'artifacts/blender_smoke/synthetic_source.blend'
    assert source.is_file(), 'Run tests/blender_smoke.py first'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    project = Path(settings.project_file)
    assert project.is_relative_to(ROOT / 'artifacts'), 'Only isolated UE test projects are allowed'

    cache = output / '自选 缓存目录'
    settings.last_manifest = 'old_manifest.json'
    settings.last_report = 'old_report.json'
    settings.cache_root = ' "' + str(cache) + '" '
    assert not settings.last_manifest and not settings.last_report
    assert Path(settings.job_root) == cache / 'Jobs'
    assert ensure_cache(settings) == cache / 'Jobs'
    assert not cache.exists(), 'Selecting a cache must not create folders'
    checks += ['cache-change-clears-old-task-and-report', 'quoted-chinese-space-cache-selection-is-read-only']

    manifest = export_job(bpy.context)
    assert manifest.is_relative_to(cache / 'Jobs')
    assert (manifest.parent / 'meshes/mesh.fbx').is_file()
    assert (manifest.parent / 'export.log').is_file()
    assert not (manifest.parent / '_export_copy.blend').exists()
    report = manifest.parent / 'sync_report.json'
    report.write_text('{"cache_smoke_fixture":true}', encoding='utf-8')
    settings.last_manifest, settings.last_report = str(manifest), str(report)
    assert selected_manifest(settings) == manifest
    checks += ['actual-fbx-export-in-selected-cache', 'task-report-and-working-files-share-cache']

    saved = output / '缓存 配置.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    assert ensure_cache(settings) == cache / 'Jobs'
    assert Path(settings.last_manifest) == manifest and Path(settings.last_report) == report
    checks.append('cache-and-current-task-persist-after-save-reopen')

    settings.cache_root = '//相对 缓存'
    assert ensure_cache(settings) == output / '相对 缓存/Jobs'
    assert not settings.last_manifest and not settings.last_report
    checks.append('saved-relative-cache-resolves-against-blend-folder')

    for action in ('sync', 'package'):
        settings.last_manifest, settings.last_report = str(manifest), str(report)
        with patch.object(blender_ui._WorkerModal, '_launch') as launch, \
                patch.object(blender_ui, '_engine_path') as engine:
            try:
                assert bpy.ops.nte_bridge.worker(action=action) == {'CANCELLED'}
            except RuntimeError as error:
                assert '当前缓存目录' in str(error), str(error)
            launch.assert_not_called()
            engine.assert_not_called()
        assert not settings.last_manifest and not settings.last_report
    checks.append('outside-cache-sync-and-package-rejected-before-engine-or-subprocess')

    # Save a legacy profile and let the actual load_post handler migrate it.
    settings.cache_root = ''
    settings.job_root = str(legacy_root)
    settings.last_manifest = str(legacy_root / 'old-task/manifest.json')
    settings.last_report = str(legacy_root / 'old-task/sync_report.json')
    migration_file = output / 'legacy_profile.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(migration_file))
    bpy.ops.wm.open_mainfile(filepath=str(migration_file), load_ui=False, use_scripts=False)
    settings = bpy.context.scene.nte_bridge
    expected = project.parent / 'Saved/NTEBridgeCache'
    assert Path(settings.cache_root) == expected, settings.cache_root
    assert Path(settings.job_root) == expected / 'Jobs'
    assert not settings.last_manifest and not settings.last_report
    assert tree_state(legacy_root) == legacy_before, 'Old C: cache was changed'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash, 'Synthetic source file changed'
    checks += ['legacy-c-cache-migrates-on-load-to-project-cache', 'old-c-cache-and-source-file-unchanged']

    result = {'success': True, 'blender': bpy.app.version_string, 'cache': str(cache),
              'manifest': str(manifest), 'migrated_cache': str(expected), 'checks': checks}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / 'artifacts/cache_smoke/latest_result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NTE_BRIDGE_CACHE_SMOKE=' + json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
