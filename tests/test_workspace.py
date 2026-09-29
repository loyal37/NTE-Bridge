from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_addon'))
from nte_bridge.core import BridgeError, write_json
from nte_bridge.workspace import MARKER, directory_lock, owned_directory, check_background_use


class WorkspaceTests(unittest.TestCase):
    def test_timed_out_remote_ue_must_finish_before_replacing_input_files(self):
        with tempfile.TemporaryDirectory() as folder:
            write_json(Path(folder) / 'ue_invocation.json', dict(stage='pending', project_file='D:/HT/HT.uproject'))
            with patch('nte_bridge.unreal_transport._project_editor_running', return_value=True):
                with self.assertRaisesRegex(BridgeError, '尚未结束'):
                    check_background_use(folder)

    def test_reset_only_matching_owned_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = owned_directory(root / 'current', 'NTEBridgeImport')
            marker = (target / MARKER).read_bytes()
            (target / 'textures').mkdir()
            (target / 'textures/old.png').write_bytes(b'old')
            outside = root / 'user.blend'
            outside.write_bytes(b'user file')
            self.assertEqual(owned_directory(target, 'NTEBridgeImport', reset=True), target)
            self.assertEqual([p.name for p in target.iterdir()], [MARKER])
            self.assertEqual((target / MARKER).read_bytes(), marker)
            self.assertEqual(outside.read_bytes(), b'user file')

    def test_unowned_or_wrong_kind_directory_is_never_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'current'
            target.mkdir()
            original = target / 'keep.txt'
            original.write_bytes(b'keep')
            with self.assertRaisesRegex(BridgeError, '不会覆盖'):
                owned_directory(target, 'NTEBridgeImport', reset=True)
            self.assertEqual(original.read_bytes(), b'keep')
            other = owned_directory(Path(folder) / 'other', 'OtherKind')
            with self.assertRaisesRegex(BridgeError, '不会覆盖'):
                owned_directory(other, 'NTEBridgeImport', reset=True)

    def test_second_operation_cannot_reset_while_first_holds_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with directory_lock(folder):
                with self.assertRaisesRegex(BridgeError, '正在使用'):
                    with directory_lock(folder):
                        self.fail('second lock unexpectedly acquired')
            with directory_lock(folder):
                pass
