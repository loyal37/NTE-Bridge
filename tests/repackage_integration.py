"""Replace a real same-name Mod twice using an existing isolated cook report."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.cooking import load_cook_report
from nte_bridge.packaging import package_selection, file_sha256


def run(report_path, packager):
    report_path = Path(report_path).resolve()
    assert report_path.is_relative_to(ROOT / 'artifacts')
    report = load_cook_report(report_path, verify_files=True)
    root = ROOT / 'artifacts/repackage032'
    root.mkdir(exist_ok=True)
    selection = root / 'selection.json'
    scope = report['character_folder']
    before = {item['path']: item['sha256'] for item in report['files']}
    checks, outputs, stages = [], [], []
    variants = [[scope + '/SM_BridgeCook'],
                [scope + '/Textures/T_BASE_COLOR', scope + '/Textures/T_NORMAL']]
    # Use actual catalog names rather than assuming a suffix spelling.
    textures = [item['asset_path'] for item in report['assets']
                if not item['dependency'] and item['asset_type'] == 'Texture2D']
    variants[1] = sorted(textures)[:2]
    assert len(variants[1]) == 2
    options = dict(packager_source_dir=packager,
        adapter_path=ROOT / 'artifacts/packager_cli/NteBridge.Packager.dll',
        output_dir=root / 'Mods', mod_name='ReplaceTest_P')
    for selected in variants:
        write_json(selection, dict(schema_version=1, cook_report=str(report_path),
            cook_report_sha256=file_sha256(report_path), selected_assets=selected,
            export_directory=str(root / 'xg' / Path(report['project_file']).stem / 'Content/Characters')))
        result = package_selection(selection, **options)
        assert result['success'], result
        stages.append(result['run_dir'])
        outputs.append({item['path']: item['sha256'] for item in result['outputs']})
        manifest_dir = root / 'container'
        manifest_dir.mkdir(exist_ok=True)
        utoc = next(item['path'] for item in result['outputs'] if item['path'].endswith('.utoc'))
        subprocess.run([str(Path(packager) / 'retoc.exe'), 'manifest', utoc], cwd=manifest_dir,
            capture_output=True, check=True, timeout=120,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        inventory = json.loads((manifest_dir / 'pakstore.json').read_text(encoding='utf-8'))
        names = {entry['packagestoreentry']['packagename'] for entry in inventory['oplog']['entries']}
        assert names == set(selected), (names, selected)
    assert outputs[0].keys() == outputs[1].keys() and outputs[0] != outputs[1]
    assert stages[0] == stages[1]
    assert len(list((root / 'packaging').iterdir())) == 1
    assert not any(path.is_dir() for path in (root / 'Mods').iterdir())
    checks += ['same-three-output-paths-replaced', 'retoc-second-inventory-exactly-matches-new-selection',
               'fixed-stage-clears-old-selected-assets', 'no-leftover-output-transactions']
    with patch('nte_bridge.packaging._run', side_effect=BridgeError('intentional adapter failure')):
        failed = package_selection(selection, **options)
    assert not failed['success'] and failed['phase'] == 'packager'
    assert outputs[1] == {path: file_sha256(path) for path in outputs[1]}
    assert before == {item['path']: item['sha256'] for item in load_cook_report(report_path, verify_files=True)['files']}
    checks += ['adapter-failure-preserves-previous-complete-mod', 'cook-snapshot-unchanged']
    result = dict(success=True, checks=checks, outputs=outputs[1], selection=str(selection),
                  report=str(report_path), stage=stages[-1])
    write_json(root / 'result.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    parser.add_argument('--packager', default='D:/Neverness to Everness Mod Loader/cook/packager')
    args = parser.parse_args()
    run(args.report, args.packager)
