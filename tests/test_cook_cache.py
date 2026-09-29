"""Bounded role-cache publication, crash recovery and cross-process exclusion."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge import cooking
from nte_bridge.core import BridgeError, write_json
from nte_bridge.packaging import file_sha256


class CookCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'UE/HT.uproject'
        self.project.parent.mkdir()
        self.project.write_text('{"EngineAssociation":"5.6"}', encoding='utf-8')
        self.content = self.project.parent / 'Content/Characters/Player/Nitsa'
        self.content.mkdir(parents=True)
        self.source = self.content / 'Keep.uasset'
        self.source.write_bytes(b'original UE source is untouched')
        self.cache = self.root / 'Cache'
        self.request = dict(schema_version=1, project_file=str(self.project),
                            character_folder='/Game/Characters/Player/Nitsa', excluded_assets=[])
        self.names = ['Keep', 'Gone']
        self.fail_cook = False
        self.commandlets = []

    def prepare(self):
        return cooking.prepare_cook_request(self.cache, self.request)

    def fake_run(self, command, log_path, timeout, env=None):
        self.commandlets.append(command)
        command = command if isinstance(command, str) else ' '.join(map(str, command))
        command = command.replace('"', '')
        pending = Path(log_path).parent
        self.assertEqual(pending.name, 'pending')
        self.assertEqual(Path(env['TEMP']), pending / 'temp')
        Path(log_path).write_text('commandlet diagnostic', encoding='utf-8')
        if '-run=pythonscript' in command:
            self.assertIn('-script=' + (pending / 'catalog_runner.py').as_posix(), command)
            write_json(pending / 'catalog.json', dict(success=True, errors=[],
                request_sha256=file_sha256(pending.parent / 'cook_request.json'),
                project_file=str(self.project), assets=[
                    dict(asset_path=self.request['character_folder'] + '/' + name,
                         asset_type='SkeletalMesh' if name == 'Keep' else 'Texture2D') for name in self.names]))
        else:
            output = pending / 'cooked/Windows/HT/Content/Characters/Player/Nitsa'
            output.mkdir(parents=True)
            for name in self.names:
                for suffix in ('.uasset', '.uexp'):
                    (output / (name + suffix)).write_bytes((name + suffix).encode())
            if self.fail_cook:
                raise BridgeError('deliberate commandlet failure')

    def cook(self, submission=None, report_path=None):
        submission = submission or self.prepare()
        with patch('nte_bridge.cooking._run', side_effect=self.fake_run), \
                patch('nte_bridge.cooking._editor', return_value=self.root / 'UnrealEditor-Cmd.exe'), \
                patch('nte_bridge.unreal_transport._project_editor_running', return_value=False):
            result = cooking.cook_character(submission['request_path'], self.root / 'Engine',
                report_path=report_path or submission['report_path'], request_token=submission['request_token'])
        return submission, result

    def worker(self, root, abrupt=False):
        script = ('import os,sys\nsys.path.insert(0,sys.argv[1])\n'
                  'from nte_bridge.cooking import _workspace_lock\n'
                  'from nte_bridge.core import BridgeError\n'
                  'try:\n'
                  ' with _workspace_lock(sys.argv[2]):\n'
                  '  os._exit(0) if sys.argv[3] == "die" else None\n'
                  'except BridgeError:\n sys.exit(23)\n')
        return subprocess.run([sys.executable, '-c', script, str(ROOT / 'blender_addon'), str(root),
                               'die' if abrupt else 'normal'], capture_output=True, timeout=20)

    def test_stable_compact_identity_is_read_only_and_separates_project_and_role(self):
        first = cooking.cook_cache_directory(self.cache, self.project, self.request['character_folder'])
        self.assertEqual(first, cooking.cook_cache_directory(self.cache, self.project, self.request['character_folder']))
        self.assertLessEqual(len(first.name), 29)
        self.assertNotEqual(first, cooking.cook_cache_directory(self.cache, self.root / 'Other/HT.uproject', self.request['character_folder']))
        self.assertNotEqual(first, cooking.cook_cache_directory(self.cache, self.project, '/Game/Other/Nitsa'))
        self.assertFalse(self.cache.exists())

    def test_reservation_prevents_request_replacement_and_cancel_is_idempotent(self):
        first = self.prepare()
        path = Path(first['request_path'])
        before = path.read_bytes()
        self.request['excluded_assets'] = [self.request['character_folder'] + '/Keep']
        with self.assertRaisesRegex(BridgeError, '等待启动'):
            self.prepare()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(cooking.cancel_cook_request(path, first['request_token']))
        self.assertFalse(cooking.cancel_cook_request(path, first['request_token']))
        second = self.prepare()
        self.assertEqual(first['request_path'], second['request_path'])
        self.assertNotEqual(first['request_token'], second['request_token'])
        self.assertNotEqual(path.read_bytes(), before)

    def test_expired_reservation_can_be_replaced_and_old_worker_cannot_claim_new_one(self):
        first = self.prepare()
        marker = Path(first['cache_directory']) / cooking._RESERVATION
        lease = json.loads(marker.read_text(encoding='utf-8'))
        lease['expires_at'] = 0
        write_json(marker, lease)
        second = self.prepare()
        report = Path(second['report_path'])
        report.write_bytes(b'active report must remain unchanged')
        with self.assertRaisesRegex(BridgeError, '预约已失效'):
            self.cook(first)
        self.assertEqual(report.read_bytes(), b'active report must remain unchanged')
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['token'], second['request_token'])

    def test_os_lock_reports_busy_across_processes_and_releases_after_process_death(self):
        root = Path(self.prepare()['cache_directory'])
        with cooking._workspace_lock(root):
            child = self.worker(root)
            self.assertEqual(child.returncode, 23, child.stderr.decode(errors='replace'))
        dead = self.worker(root, abrupt=True)
        self.assertEqual(dead.returncode, 0, dead.stderr.decode(errors='replace'))
        with cooking._workspace_lock(root):
            pass

    def test_recook_replaces_current_removes_stale_assets_and_old_selected_staging(self):
        submission, first = self.cook()
        self.assertTrue(first['success'], first)
        old_hash = file_sha256(submission['report_path'])
        cache = Path(submission['cache_directory'])
        staged = cache / 'current/selections/old/packaging/data.bin'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'obsolete selected package staging')
        self.names = ['Keep']
        second_submission, second = self.cook()
        self.assertTrue(second['success'], second)
        self.assertEqual(submission['request_path'], second_submission['request_path'])
        self.assertEqual(submission['report_path'], second_submission['report_path'])
        self.assertEqual(first['cooked_root'], second['cooked_root'])
        self.assertNotEqual(first['cook_id'], second['cook_id'])
        self.assertFalse(staged.exists())
        self.assertFalse(list(Path(second['cooked_root']).rglob('Gone.*')))
        self.assertEqual([path.name for path in cache.iterdir() if path.is_dir()], ['current'])
        self.assertEqual(len(list((self.cache / 'Cooks').iterdir())), 1)
        self.assertEqual(self.source.read_bytes(), b'original UE source is untouched')
        cooking.load_cook_report(second_submission['report_path'], verify_files=True)
        with self.assertRaisesRegex(BridgeError, '报告已改变'):
            cooking.load_cook_report(submission['report_path'], old_hash)

    def test_old_copied_report_expires_even_when_cooked_file_bytes_are_identical(self):
        submission, first = self.cook()
        archived = self.root / 'archived.json'
        write_json(archived, first)
        _, second = self.cook()
        self.assertEqual(first['files'], second['files'])
        with self.assertRaisesRegex(BridgeError, '已过期'):
            cooking.load_cook_report(archived, verify_files=True)

    def test_failed_recook_preserves_successful_current_but_current_report_is_failure(self):
        submission, first = self.cook()
        archive = self.root / 'last_success.json'
        write_json(archive, first)
        self.fail_cook = True
        self.names = ['Broken']
        _, failed = self.cook()
        self.assertFalse(failed['success'])
        cache = Path(submission['cache_directory'])
        self.assertFalse((cache / 'pending').exists())
        self.assertFalse((cache / 'previous').exists())
        self.assertEqual(cooking.load_cook_report(archive, verify_files=True)['cook_id'], first['cook_id'])
        self.assertIn('commandlet diagnostic', Path(failed['diagnostic_log']).read_text(encoding='utf-8'))
        with self.assertRaisesRegex(BridgeError, '成功'):
            cooking.load_cook_report(submission['report_path'])

    def test_report_publication_failure_rolls_back_previous_current(self):
        submission, first = self.cook()
        archive = self.root / 'last_success.json'
        write_json(archive, first)
        original_write = cooking.write_json
        rejected = []
        def fail_once(path, data):
            if Path(path) == Path(submission['report_path']) and data.get('success') and not rejected:
                rejected.append(True)
                raise OSError('report publish blocked')
            return original_write(path, data)
        self.names = ['Different']
        with patch('nte_bridge.cooking.write_json', side_effect=fail_once):
            _, failure = self.cook()
        self.assertFalse(failure['success'])
        self.assertEqual(cooking.load_cook_report(archive, verify_files=True)['files'], first['files'])
        cache = Path(submission['cache_directory'])
        self.assertFalse((cache / 'previous').exists())
        self.assertFalse((cache / 'pending').exists())

    def test_interrupted_uncommitted_publication_recovers_previous_before_next_attempt(self):
        submission, first = self.cook()
        root = Path(submission['cache_directory'])
        with cooking._workspace_lock(root):
            metadata = cooking._cache_metadata(root)
            cooking._move_work(root, 'current', 'previous', metadata)
            for name in ('current', 'pending'):
                work = root / name
                work.mkdir()
                write_json(work / cooking._WORK_MARKER, dict(kind='NTEBridgeCookWork',
                    cache_id=metadata['cache_id'], cook_id='interrupted', committed=False))
                (work / 'partial').write_bytes(b'failed generation')
            cooking._recover_work(root, metadata)
        self.assertFalse((root / 'pending').exists())
        self.assertFalse((root / 'previous').exists())
        self.assertEqual(cooking.load_cook_report(submission['report_path'], verify_files=True)['cook_id'], first['cook_id'])

    def test_unknown_work_directory_is_never_removed(self):
        submission = self.prepare()
        unknown = Path(submission['cache_directory']) / 'pending'
        unknown.mkdir()
        sentinel = unknown / 'user-file'
        sentinel.write_bytes(b'do not delete')
        _, result = self.cook(submission)
        self.assertFalse(result['success'])
        self.assertIn('所有权', '\n'.join(result['errors']))
        self.assertEqual(sentinel.read_bytes(), b'do not delete')

    def test_work_directory_link_cannot_escape_into_ue_content(self):
        submission = self.prepare()
        root = Path(submission['cache_directory'])
        link = root / 'pending'
        try:
            link.symlink_to(self.content, target_is_directory=True)
        except OSError as error:
            if os.name != 'nt':
                raise
            # Junctions need no Windows symbolic-link privilege and exercise
            # the same resolved-boundary guard used for real cache folders.
            process = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                'New-Item -ItemType Junction -Path $env:NTE_TEST_LINK -Target $env:NTE_TEST_TARGET | Out-Null'],
                env=dict(os.environ, NTE_TEST_LINK=str(link), NTE_TEST_TARGET=str(self.content)),
                capture_output=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors='replace'))
        try:
            _, failure = self.cook(submission)
            self.assertFalse(failure['success'])
            self.assertEqual(self.source.read_bytes(), b'original UE source is untouched')
            self.assertEqual(link.resolve(), self.content.resolve())
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                # Remove this verified junction itself, never its target tree.
                self.assertEqual(link.resolve(), self.content.resolve())
                link.rmdir()

    def test_persistent_delete_failure_keeps_marker_and_retry_can_finish(self):
        submission, _ = self.cook()
        root = Path(submission['cache_directory'])
        marker = root / 'current' / cooking._WORK_MARKER
        metadata = cooking._cache_metadata(root)
        original_unlink = Path.unlink
        def deny_log(path, *args, **kwargs):
            if path.name == 'catalog.log':
                raise PermissionError('sharing violation')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', deny_log), patch('nte_bridge.cooking.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                cooking._remove_work(root, 'current', metadata)
        self.assertEqual(sleep.call_count, 6)
        self.assertTrue(marker.is_file())
        cooking._remove_work(root, 'current', metadata)
        self.assertFalse((root / 'current').exists())

    def test_copied_or_external_report_locks_actual_cache_and_prevents_recook(self):
        report_path = self.root / 'external/report.json'
        submission, result = self.cook(report_path=report_path)
        self.assertTrue(result['success'], result)
        archived = self.root / 'copied_report.json'
        shutil.copy2(report_path, archived)
        with cooking.lock_cook_report(archived):
            self.assertEqual(self.worker(submission['cache_directory']).returncode, 23)
            with self.assertRaisesRegex(BridgeError, '正在烘焙或打包'):
                self.prepare()
        self.prepare()

    def test_busy_cook_preserves_existing_report(self):
        submission, first = self.cook()
        report = Path(submission['report_path'])
        before = report.read_bytes()
        with cooking.lock_cook_report(report):
            with self.assertRaisesRegex(BridgeError, '正在烘焙或打包'):
                cooking.cook_character(submission['request_path'], self.root / 'Engine')
        self.assertEqual(report.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
