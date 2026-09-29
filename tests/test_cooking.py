import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.cooking import cook_character, load_cook_report, snapshot_cooked, validate_cook_request
from nte_bridge.packaging import export_selection, file_sha256, package_selection, stage_selection


class CookingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'UE Project/HT.uproject'
        self.project.parent.mkdir()
        self.project.write_text('{"EngineAssociation":"5.6"}', encoding='utf-8')
        (self.project.parent / 'Content/Characters/A').mkdir(parents=True)
        self.request = {'schema_version': 1, 'project_file': str(self.project),
                        'character_folder': '/Game/Characters/A',
                        'excluded_assets': ['/Game/Characters/A/MI_Original']}
        self.request_path = self.root / 'request.json'
        write_json(self.request_path, self.request)
        self.cooked = self.root / 'cooked/Windows'
        self.types = {
            '/Game/Characters/A/SK_Body': 'SkeletalMesh',
            '/Game/Characters/A/T_Preview': 'Texture2D',
            '/Game/Common/MI_Custom': 'MaterialInstanceConstant',
            '/Game/Characters/A/BP_Menu': 'WidgetBlueprint',
            '/Game/Characters/A/ABP_Main': 'AnimBlueprint',
            '/Game/Characters/A/MI_Original': 'MaterialInstanceConstant',
            '/Game/Characters/A/M_Original': 'Material',
            '/Game/Characters/A/Skeleton': 'Skeleton',
            '/Game/Characters/A/Physics': 'PhysicsAsset',
            '/Game/Characters/A/EditorOnly': 'EditorUtilityBlueprint',
        }
        for asset in self.types:
            for extension in ('.uasset', '.uexp'):
                path = self.cooked / ('HT/Content/' + asset[len('/Game/'):] + extension)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((asset + extension).encode())
        (self.cooked / 'HT/AssetRegistry.bin').write_bytes(b'auxiliary registry')
        self.catalog = {'success': True, 'request_sha256': file_sha256(self.request_path),
                        'project_file': str(self.project),
                        'assets': [{'asset_path': path, 'asset_type': kind} for path, kind in self.types.items()]}
        self.report = snapshot_cooked(self.request, self.catalog, self.cooked, 'cook-id', file_sha256(self.request_path))
        self.report_path = self.root / 'cook_report.json'
        write_json(self.report_path, self.report)
        self.selection_path = self.root / 'selection.json'
        self.export = self.root / 'packager/xg/HT/Content/Characters'
        self.selection = {'schema_version': 1, 'cook_report': str(self.report_path),
                          'cook_report_sha256': file_sha256(self.report_path),
                          'selected_assets': ['/Game/Characters/A/SK_Body', '/Game/Common/MI_Custom'],
                          'export_directory': str(self.export)}
        self.save_selection()

    def save_selection(self):
        write_json(self.selection_path, self.selection)

    def save_report(self):
        write_json(self.report_path, self.report)
        self.selection['cook_report_sha256'] = file_sha256(self.report_path)
        self.save_selection()

    def test_snapshot_preserves_actual_classes_and_lists_every_file_unselected(self):
        self.assertEqual(len(self.report['files']), 21)
        self.assertEqual(len(self.report['unassigned_files']), 1)
        catalog = {row['asset_path']: row for row in self.report['assets']}
        for path in self.types:
            self.assertEqual(catalog[path]['asset_type'], self.types[path])
            self.assertFalse(catalog[path]['default_selected'])
        for path in ('/Game/Common/MI_Custom', '/Game/Characters/A/BP_Menu',
                     '/Game/Characters/A/ABP_Main', '/Game/Characters/A/T_Preview'):
            self.assertTrue(catalog[path]['packable'])
        for path in ('/Game/Characters/A/MI_Original', '/Game/Characters/A/M_Original',
                     '/Game/Characters/A/Skeleton', '/Game/Characters/A/Physics', '/Game/Characters/A/EditorOnly'):
            self.assertFalse(catalog[path]['packable'])
        self.assertTrue(catalog['/Game/Common/MI_Custom']['dependency'])
        self.assertEqual(load_cook_report(self.report_path, file_sha256(self.report_path), True), self.report)

    def test_selection_only_stages_checked_packages_and_does_not_need_import_state(self):
        # Existing UE assets can change after this cook; packaging intentionally
        # uses its validated cooked snapshot, not an earlier import/source hash.
        (self.project.parent / 'Content/Characters/A/Modified.uasset').write_bytes(b'UE manual edits')
        first = stage_selection(self.selection_path)
        self.assertEqual(len(first['files']), 4)
        self.assertFalse(any('T_Preview' in item['path'] or 'BP_Menu' in item['path'] for item in first['files']))
        second = stage_selection(self.selection_path)
        self.assertEqual(first['run_dir'], second['run_dir'])
        self.assertNotEqual(first['run_id'], second['run_id'])
        for item in second['files']:
            self.assertEqual(file_sha256(Path(second['source_dir']) / item['path']), item['sha256'])

    def test_report_or_cooked_bytes_or_unlisted_sidecars_invalidate_selection(self):
        self.report_path.write_text(self.report_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaisesRegex(BridgeError, '报告已改变'):
            stage_selection(self.selection_path)
        self.save_report()
        target = self.cooked / 'HT/Content/Characters/A/SK_Body.uexp'
        original = target.read_bytes()
        target.write_bytes(b'changed cooked data')
        with self.assertRaisesRegex(BridgeError, '增加、删除或修改'):
            stage_selection(self.selection_path)
        target.write_bytes(original)
        target.with_suffix('.ubulk').write_bytes(b'late sidecar')
        with self.assertRaisesRegex(BridgeError, '增加、删除或修改'):
            stage_selection(self.selection_path)

    def test_forbidden_assets_cannot_be_enabled_by_forging_checkbox_flag(self):
        for name in ('M_Original', 'MI_Original', 'Skeleton', 'Physics', 'EditorOnly'):
            asset = '/Game/Characters/A/' + name
            next(row for row in self.report['assets'] if row['asset_path'] == asset)['packable'] = True
            self.selection['selected_assets'] = [asset]
            self.save_report()
            with self.subTest(asset=asset), self.assertRaisesRegex(BridgeError, '禁止打包'):
                stage_selection(self.selection_path)

    def test_empty_duplicate_unknown_and_traversal_selections_are_rejected(self):
        for selected in ([], ['/Game/Characters/A/Missing'], ['/Game/Characters/A/../Bad'],
                         ['/Game/Characters/A/SK_Body', '/game/characters/a/sk_body']):
            self.selection['selected_assets'] = selected
            self.save_selection()
            with self.subTest(selected=selected), self.assertRaises(BridgeError):
                stage_selection(self.selection_path)

    def test_asset_snapshot_cannot_omit_or_substitute_sidecars(self):
        asset = self.report['assets'][0]
        asset['files'].pop()
        asset['file_count'] = len(asset['files'])
        asset['bytes'] = sum(item['bytes'] for item in asset['files'])
        self.save_report()
        with self.assertRaisesRegex(BridgeError, '主文件或旁文件'):
            stage_selection(self.selection_path)

    def test_explicit_preview_texture_can_be_selected_after_cook(self):
        self.selection['selected_assets'] = ['/Game/Characters/A/T_Preview']
        self.save_selection()
        self.assertEqual(len(stage_selection(self.selection_path)['files']), 2)

    def test_export_preserves_game_paths_keeps_unselected_and_removes_only_selected_stale_sidecars(self):
        stage = stage_selection(self.selection_path)
        existing = self.export / 'A/Old_Unselected.uasset'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'keep old unselected')
        stale = self.export.parent / 'Common/MI_Custom.ubulk'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'old now absent sidecar')
        result = export_selection(stage, self.export)
        self.assertEqual(len(result['exported_files']), 4)
        self.assertEqual(existing.read_bytes(), b'keep old unselected')
        self.assertFalse(stale.exists())
        self.assertTrue((self.export / 'A/SK_Body.uasset').is_file())
        self.assertTrue((self.export.parent / 'Common/MI_Custom.uasset').is_file())
        self.assertFalse(any('Old_Unselected' in item['path'] for item in stage['files']))
        for row in result['exported_files']:
            self.assertEqual(file_sha256(row['exported_path']), row['sha256'])

    def test_export_preflights_all_targets_before_overwriting(self):
        stage = stage_selection(self.selection_path)
        old = self.export / 'A/SK_Body.uasset'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'prior export')
        (self.export.parent / 'Common/MI_Custom.uexp').mkdir(parents=True)
        with self.assertRaisesRegex(BridgeError, '文件夹占用'):
            export_selection(stage, self.export)
        self.assertEqual(old.read_bytes(), b'prior export')
        with self.assertRaisesRegex(BridgeError, '保持工程挂载名'):
            export_selection(stage, self.root / 'xg/Wrong/Content/Characters')

    def player_selection(self, include_colliding_asset=False):
        source = '/Game/Characters/Player/A/Sub/SK_Body'
        for extension in ('.uasset', '.uexp'):
            path = self.cooked / ('HT/Content/' + source[len('/Game/'):] + extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('nested-character' + extension).encode())
        self.request['character_folder'] = '/Game/Characters/Player/A'
        self.catalog['assets'].append({'asset_path': source, 'asset_type': 'SkeletalMesh'})
        self.selection['selected_assets'] = [source]
        if include_colliding_asset:
            other = '/Game/Characters/A/Sub/SK_Body'
            for extension in ('.uasset', '.uexp'):
                path = self.cooked / ('HT/Content/' + other[len('/Game/'):] + extension)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'different shared asset')
            self.catalog['assets'].append({'asset_path': other, 'asset_type': 'SkeletalMesh'})
            self.selection['selected_assets'].append(other)
        self.report = snapshot_cooked(self.request, self.catalog, self.cooked, 'cook-id', file_sha256(self.request_path))
        self.save_report()
        return stage_selection(self.selection_path)

    def test_character_exports_directly_without_player_and_staging_keeps_game_mount(self):
        stage = self.player_selection()
        stale = self.export / 'A/Sub/SK_Body.ubulk'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'old selected bulk')
        result = export_selection(stage, self.export)
        self.assertFalse((self.export / 'Player').exists())
        self.assertFalse(stale.exists())
        self.assertTrue((self.export / 'A/Sub/SK_Body.uasset').is_file())
        self.assertEqual(stage['selected_assets'], ['/Game/Characters/Player/A/Sub/SK_Body'])
        for item in result['exported_files']:
            self.assertIn('/Characters/Player/A/', item['path'])
            self.assertIn('/Characters/A/Sub/', item['export_relative_path'])
            self.assertEqual(file_sha256(Path(stage['source_dir']) / item['path']), item['sha256'])
            self.assertEqual(file_sha256(item['exported_path']), item['sha256'])

    def test_character_rebase_collision_fails_before_touching_exported_files(self):
        stage = self.player_selection(include_colliding_asset=True)
        previous = self.export / 'A/Sub/SK_Body.uasset'
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b'previous export')
        with self.assertRaisesRegex(BridgeError, '同一个角色导出文件'):
            export_selection(stage, self.export)
        self.assertEqual(previous.read_bytes(), b'previous export')
        self.assertFalse((previous.parent / 'SK_Body.uexp').exists())

    def test_character_export_mapping_has_strict_scope_and_root_boundary(self):
        from nte_bridge.packaging import exported_relative_path
        stage = self.player_selection()
        sibling = 'HT/Content/Characters/Player/AOther/Sub/SK_Body.uasset'
        self.assertEqual(exported_relative_path(stage, sibling), sibling)
        stage['character_folder'] = '/Game/Characters'
        original = 'HT/Content/Characters/A/Sub/SK_Body.uasset'
        self.assertEqual(exported_relative_path(stage, original), original)

    def test_package_exports_then_adapter_only_consumes_fresh_staging_without_cook(self):
        adapter = self.root / 'adapter.dll'
        adapter.write_bytes(b'test adapter')
        for tool in ('NteMorphTargetPatch.exe', 'retoc.exe', 'oo2core_9_win64.dll'):
            (self.root / tool).write_bytes(b'test tool')
        destination = self.root / 'Mods'
        captured = []

        def fake_adapter(command, log_path, timeout, env=None):
            request_path = Path(command[command.index('--job') + 1])
            reply_path = Path(command[command.index('--report') + 1])
            request = json.loads(request_path.read_text(encoding='utf-8'))
            captured.append(request)
            self.assertEqual(len(request['files']), 4)
            self.assertEqual(Path(env['TEMP']), Path(request['run_dir']) / 'temp')
            self.assertEqual(env['TEMP'], env['TMP'])
            build_output = Path(request['output_dir'])
            build_output.mkdir()
            outputs = []
            for suffix in ('.pak', '.utoc', '.ucas'):
                target = build_output / ('SelectedMod' + suffix)
                target.write_bytes(b'packed selected assets')
                outputs.append({'path': str(target), 'bytes': target.stat().st_size, 'sha256': file_sha256(target)})
            write_json(reply_path, dict(success=True, outputs=outputs, **{
                key: request[key] for key in ('job_id', 'manifest_sha256', 'run_id')}))

        with patch('nte_bridge.packaging._run', side_effect=fake_adapter), \
                patch('nte_bridge.packaging.cook_assets', side_effect=AssertionError('Must not cook')):
            result = package_selection(self.selection_path, str(self.root), output_dir=destination,
                                       mod_name='SelectedMod', adapter_path=adapter)
        self.assertTrue(result['success'], result)
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(result['exported_files']), 4)
        self.assertEqual(len(result['outputs']), 3)
        self.assertTrue(all(Path(item['path']).parent == destination for item in result['outputs']))

    def test_report_cannot_overwrite_project_or_cooked_snapshot(self):
        for target in (self.project, self.cooked / 'HT/AssetRegistry.bin', self.report_path, self.selection_path):
            original = target.read_bytes()
            with self.subTest(target=target), self.assertRaisesRegex(BridgeError, '不能覆盖'):
                package_selection(self.selection_path, report_path=target)
            self.assertEqual(target.read_bytes(), original)

    def test_bad_packager_inputs_fail_before_export_overwrites(self):
        adapter = self.root / 'adapter.dll'
        adapter.write_bytes(b'test adapter')
        for tool in ('NteMorphTargetPatch.exe', 'retoc.exe', 'oo2core_9_win64.dll'):
            (self.root / tool).write_bytes(b'test tool')
        destination = self.root / 'ExistingMods'
        destination.mkdir()
        (destination / 'AlreadyExists.pak').mkdir()
        parameters = [dict(mod_name='bad name'), dict(packager_tools_dir=self.root / 'MissingTools'),
                      dict(mod_name='AlreadyExists')]
        for change in parameters:
            options = dict(packager_source_dir=self.root, output_dir=destination,
                           mod_name='NewMod', adapter_path=adapter)
            options.update(change)
            with self.subTest(change=change), patch('nte_bridge.packaging.export_selection') as export:
                result = package_selection(self.selection_path, **options)
            self.assertFalse(result['success'])
            self.assertEqual(result['phase'], 'validation')
            export.assert_not_called()

    def test_open_project_is_rejected_before_catalog_or_cook_launch(self):
        request = self.root / 'shared-cook/request.json'
        write_json(request, self.request)
        with patch('nte_bridge.unreal_transport._project_editor_running', return_value=True), \
                patch('nte_bridge.cooking._editor', return_value=self.root / 'editor.exe'), \
                patch('nte_bridge.cooking._run') as launch:
            result = cook_character(request, self.root / 'engine')
        self.assertFalse(result['success'])
        self.assertIn('仍在打开', result['errors'][0])
        launch.assert_not_called()

    def test_cook_scans_then_cooks_full_folder_and_uses_forward_slash_script_arg(self):
        engine = self.root / 'UE/Engine'
        (engine / 'Build').mkdir(parents=True)
        (engine / 'Build/Build.version').write_text('{"MajorVersion":5,"MinorVersion":6}', encoding='utf-8')
        editor = engine / 'Binaries/Win64/UnrealEditor-Cmd.exe'
        editor.parent.mkdir(parents=True)
        editor.touch()
        captured = []
        request = self.root / 'shared-cook/request.json'
        write_json(request, self.request)

        def fake_unreal(command, log_path, timeout, env=None):
            text = command if isinstance(command, str) else ' '.join(map(str, command))
            captured.append(text)
            run_root = Path(log_path).parent
            self.assertEqual(Path(env['TEMP']), run_root / 'temp')
            if '-run=pythonscript' in text.replace('"', ''):
                self.assertIn((run_root / 'catalog_runner.py').as_posix(), text)
                self.assertNotIn('-script="' + str(run_root).replace('/', '\\'), text)
                write_json(run_root / 'catalog.json', self.catalog)
            elif '-run=Cook' in text.replace('"', ''):
                self.assertIn(str(self.project.parent / 'Content/Characters/A'), text)
                self.assertIn('[Platform]', text)
                self.assertIn('-iterate', text)
                self.assertIn('-NODEFAULTLOG', text)
                self.assertIn('-FullStdOutLogOutput', text)
                shutil.copytree(self.cooked, run_root / 'cooked/Windows')
            else:
                self.fail(text)

        with patch('nte_bridge.unreal_transport._project_editor_running', return_value=False), \
                patch('nte_bridge.cooking._run', side_effect=fake_unreal):
            result = cook_character(request, engine)
        self.assertTrue(result['success'], result)
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(result['assets']), len(self.types))
        self.assertTrue(all(not item['default_selected'] for item in result['assets']))

    def test_request_rejects_root_missing_folder_and_relative_project(self):
        for field, value in (('character_folder', '/Game'), ('character_folder', '/Game/Missing'),
                             ('project_file', 'HT.uproject'), ('excluded_assets', ['/Game/../Bad'])):
            with self.subTest(field=field, value=value), self.assertRaises(BridgeError):
                validate_cook_request(dict(self.request, **{field: value}))


if __name__ == '__main__':
    unittest.main()
