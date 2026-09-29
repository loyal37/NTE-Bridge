"""Real isolated UE cook, explicit export, external packager and container check."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge.core import write_json
from nte_bridge.packaging import _run, _unreal_command_line, file_sha256


def run(fixture_path, engine, packager, phase='all'):
    fixture_path = Path(fixture_path).resolve()
    assert fixture_path.is_relative_to((ROOT / 'artifacts/cooking_fixture').resolve())
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    folder = fixture_path.parent
    project = Path(fixture['project_file'])
    project_hash = file_sha256(project)
    result = {'success': False, 'fixture': str(fixture_path), 'checks': []}
    try:
        if phase in ('all', 'fixture'):
            from nte_bridge.unreal_transport import REQUIRED_COMMANDLET_PLUGINS
            argv = [Path(engine) / 'Engine/Binaries/Win64/UnrealEditor-Cmd.exe', project,
                    '-run=pythonscript', '-script=' + Path(fixture['runner']).as_posix(), '-unattended', '-nop4',
                    '-nosplash', '-UTF8Output', '-EnablePlugins=' + ','.join(REQUIRED_COMMANDLET_PLUGINS)]
            _run(_unreal_command_line(argv), folder / 'fixture.log', 600)
        evidence = json.loads((folder / 'fixture_result.json').read_text(encoding='utf-8'))
        assert evidence['success'], evidence
        result['checks'].extend(evidence['checks'])
        if phase == 'fixture':
            result['success'] = True
            return result
        from nte_bridge.cooking import cook_character, load_cook_report
        scope = fixture['character_root']
        cache = folder / 'Cache/Cooks/main'
        cache.mkdir(parents=True, exist_ok=True)
        request = cache / 'cook_request.json'
        report_path = cache / 'cook_report.json'
        if phase in ('all', 'cook'):
            write_json(request, {'schema_version': 1, 'project_file': str(project),
                                'character_folder': scope,
                                'excluded_assets': [scope + '/Materials/MI_OriginalGame']})
            reply = cook_character(request, engine, report_path=report_path)
            assert reply['success'], reply
        report = load_cook_report(report_path, verify_files=True)
        by_path = {item['asset_path']: item for item in report['assets']}
        for path, kind in fixture['expected_additional_assets'].items():
            if path.startswith(scope + '/'):
                assert by_path[path]['asset_type'] == kind and by_path[path]['packable'], by_path[path]
            else:
                assert path not in by_path, path
        for relative in ('Materials/M_Original', 'Materials/MI_OriginalGame', 'SK_BridgeCook', 'PH_BridgeCook'):
            assert not by_path[scope + '/' + relative]['packable'], relative
        assert all(not item['default_selected'] for item in report['assets'])
        result['checks'].extend(['whole_character_folder_cooked', 'custom_mi_blueprint_animbp_selectable',
                                 'original_material_skeleton_physics_blocked', 'prefix_sibling_not_cooked',
                                 'all_assets_unchecked_initially'])
        result['cook_report'] = str(report_path)
        if phase == 'cook':
            result['success'] = True
            return result
        from nte_bridge.packaging import package_selection
        selected = [scope + '/' + relative for relative in (
            'SM_BridgeCook', 'Textures/T_BASE_COLOR', 'Textures/T_NORMAL',
            'Materials/MI_Custom', 'Logic/BP_Character', 'Logic/ABP_Character')]
        export = folder / 'xg' / project.stem / 'Content/Characters'
        export.mkdir(parents=True, exist_ok=True)
        stale = export / 'OldCharacter/Unselected.uasset'
        stale.parent.mkdir(exist_ok=True)
        stale.write_bytes(b'previous unrelated export must stay on disk but never enter package')
        stale_hash = file_sha256(stale)
        selection_path = cache / 'selection.json'
        write_json(selection_path, {'schema_version': 1, 'cook_report': str(report_path),
            'cook_report_sha256': file_sha256(report_path), 'selected_assets': selected,
            'export_directory': str(export)})
        packaged = package_selection(selection_path, packager_source_dir=packager,
            output_dir=folder / 'output' / uuid.uuid4().hex[:8], mod_name='BridgeSelected_P',
            adapter_path=ROOT / 'artifacts/packager_cli/NteBridge.Packager.dll',
            report_path=cache / 'package_report.json')
        assert packaged['success'], packaged
        assert file_sha256(stale) == stale_hash
        for path in selected:
            for item in by_path[path]['files']:
                expected = project.stem + '/Content/Characters/' + scope.rsplit('/', 1)[-1] + '/' + path[len(scope) + 1:] + Path(item['path']).suffix
                copied = folder / 'xg' / expected
                assert copied.is_file() and file_sha256(copied) == item['sha256'], str(copied)
        for relative in ('Textures/T_ID_TEX', 'Textures/T_LIGHT_MAP', 'Materials/M_Original'):
            assert not (export / (scope.rsplit('/', 1)[-1] + '/' + relative + '.uasset')).exists()
        utoc = next(item['path'] for item in packaged['outputs'] if item['path'].endswith('.utoc'))
        container_folder = folder / 'container-validation'
        container_folder.mkdir(exist_ok=True)
        subprocess.run([str(Path(packager) / 'retoc.exe'), 'manifest', utoc], cwd=container_folder,
            capture_output=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=120)
        manifest = json.loads((container_folder / 'pakstore.json').read_text(encoding='utf-8'))
        write_json(folder / 'container_manifest.json', manifest)
        package_names = {item['packagestoreentry']['packagename'] for item in manifest['oplog']['entries']}
        assert package_names == set(selected), (package_names, selected)
        load_cook_report(report_path, verify_files=True)
        result['checks'].extend(['selected_sidecars_exported_at_original_paths', 'old_export_preserved_not_packaged',
                                 'container_contains_exact_selection', 'packaging_preserves_cook_snapshot'])
        result['package_report'] = str(cache / 'package_report.json')
        # Choose a different folder without a new Blender import.
        other_cache = folder / 'Cache/Cooks/other'
        other_cache.mkdir(parents=True, exist_ok=True)
        other_request = other_cache / 'cook_request.json'
        write_json(other_request, {'schema_version': 1, 'project_file': str(project),
            'character_folder': fixture['other_root'], 'excluded_assets': [scope + '/Materials/MI_OriginalGame']})
        other = cook_character(other_request, engine, report_path=other_cache / 'cook_report.json')
        assert other['success'], other
        in_scope = {item['asset_path'] for item in other['assets'] if not item['dependency']}
        expected_other = {path for path in fixture['expected_additional_assets']
                          if path.startswith(fixture['other_root'] + '/')}
        assert in_scope == expected_other, (in_scope, expected_other)
        result['checks'].append('different_character_folder_without_new_import')
        result['success'] = True
    except Exception:
        result['traceback'] = traceback.format_exc()
    finally:
        result['project_unchanged'] = file_sha256(project) == project_hash
        result['sources_unchanged'] = all(file_sha256(path) == sha for path, sha in fixture['source_hashes'].items())
        result['success'] &= result['project_unchanged'] and result['sources_unchanged']
        write_json(folder / ('integration_' + phase + '.json'), result)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--engine-dir', default='D:/ue/UE_5.6')
    parser.add_argument('--packager', default='D:/Neverness to Everness Mod Loader/cook/packager')
    parser.add_argument('--phase', choices=('all', 'fixture', 'cook', 'package'), default='all')
    args = parser.parse_args()
    raise SystemExit(0 if run(args.fixture, args.engine_dir, args.packager, args.phase)['success'] else 1)
