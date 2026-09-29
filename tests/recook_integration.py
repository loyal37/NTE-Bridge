"""Verify actual recooking replaces one stable role cache and cleans old selections."""
import argparse
import json
from pathlib import Path
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge.core import write_json
from nte_bridge.cooking import prepare_cook_request, cook_character, load_cook_report
from nte_bridge.packaging import file_sha256, package_selection


def run(fixture_path, engine, packager):
    fixture_path = Path(fixture_path).resolve()
    assert fixture_path.is_relative_to((ROOT / 'artifacts/cooking_fixture').resolve())
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    base = ROOT / 'artifacts/recook_smoke' / uuid.uuid4().hex[:10]
    base.mkdir(parents=True)
    project = Path(fixture['project_file'])
    project_hash = file_sha256(project)
    request = dict(schema_version=1, project_file=str(project), character_folder=fixture['character_root'],
                   excluded_assets=[fixture['character_root'] + '/Materials/MI_OriginalGame'])
    result = dict(success=False, cache=str(base / 'Cache'), checks=[])
    print('RECOOK_SMOKE=' + str(base), flush=True)
    try:
        first = prepare_cook_request(base / 'Cache', request)
        reply = cook_character(first['request_path'], engine, report_path=first['report_path'],
                               request_token=first['request_token'])
        assert reply['success'], reply
        first_hash = file_sha256(first['report_path'])
        first_id, root = reply['cook_id'], Path(reply['cooked_root'])
        role_cache = Path(first['cache_directory'])
        assert root.is_relative_to(role_cache / 'current')
        load_cook_report(first['report_path'], first_hash, verify_files=True)
        result['checks'].append('first-real-cook-publishes-current')

        # Perform real packaging while the stable role's cache lock is held.
        selection_dir = role_cache / 'current/selections/first-package'
        selection_dir.mkdir(parents=True)
        selection = selection_dir / 'selection.json'
        write_json(selection, dict(schema_version=1, cook_report=first['report_path'],
            cook_report_sha256=first_hash, selected_assets=[fixture['character_root'] + '/SM_BridgeCook'],
            export_directory=str(base / 'xg' / project.stem / 'Content/Characters')))
        packaged = package_selection(selection, packager_source_dir=packager,
            adapter_path=ROOT / 'artifacts/packager_cli/NteBridge.Packager.dll',
            output_dir=base / 'output', mod_name='StableCook_P')
        assert packaged['success'], packaged
        assert Path(packaged['run_dir']).is_relative_to(role_cache / 'current')
        result['checks'].append('actual-selected-export-package-under-current-lock')

        obsolete = root / project.stem / 'Content/Characters/RemovedAsset.uasset'
        obsolete.write_bytes(b'old cooked file must not survive recook')
        second = prepare_cook_request(base / 'Cache', request)
        assert second['cache_directory'] == first['cache_directory']
        assert second['request_path'] == first['request_path'] and second['report_path'] == first['report_path']
        reply = cook_character(second['request_path'], engine, report_path=second['report_path'],
                               request_token=second['request_token'])
        assert reply['success'], reply
        assert Path(reply['cooked_root']) == root and reply['cook_id'] != first_id
        assert file_sha256(second['report_path']) != first_hash
        assert not obsolete.exists() and not selection_dir.exists()
        assert not (role_cache / 'pending').exists() and not (role_cache / 'previous').exists()
        load_cook_report(second['report_path'], verify_files=True)
        result['checks'] += ['same-role-reuses-request-report-and-cooked-path', 'new-generation-invalidates-old-selection',
                             'stale-cooked-assets-and-old-packaging-staging-removed', 'pending-previous-workspaces-cleaned']

        preserved = base / 'last_success_report.json'
        write_json(preserved, reply)
        before = {entry['path']: entry['sha256'] for entry in reply['files']}
        failed = prepare_cook_request(base / 'Cache', request)
        failure = cook_character(failed['request_path'], base / 'MissingEngine', report_path=failed['report_path'],
                                  request_token=failed['request_token'])
        assert not failure['success']
        valid = load_cook_report(preserved, verify_files=True)
        assert before == {entry['path']: entry['sha256'] for entry in valid['files']}
        assert not (role_cache / 'pending').exists() and not (role_cache / 'previous').exists()
        result['checks'].append('failed-recook-reports-failure-and-retains-last-successful-files')
        result.update(success=True, role_cache=str(role_cache), cooked_root=str(root), last_success_report=str(preserved))
    except Exception:
        result['traceback'] = traceback.format_exc()
    finally:
        result['project_unchanged'] = file_sha256(project) == project_hash
        result['sources_unchanged'] = all(file_sha256(path) == sha for path, sha in fixture['source_hashes'].items())
        result['success'] &= result['project_unchanged'] and result['sources_unchanged']
        write_json(base / 'result.json', result)
        write_json(ROOT / 'artifacts/recook_smoke/latest_result.json', result)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--engine-dir', default='D:/ue/UE_5.6')
    parser.add_argument('--packager', default='D:/Neverness to Everness Mod Loader/cook/packager')
    args = parser.parse_args()
    raise SystemExit(0 if run(args.fixture, args.engine_dir, args.packager)['success'] else 1)
