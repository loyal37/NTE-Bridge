import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_addon'))
from nte_bridge.core import BridgeError
from nte_bridge.packaging import _publish_outputs, _output_lock, file_sha256


class OutputReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / 'Mods'
        self.destination.mkdir()
        self.name = 'Example_P'
        self.targets = [self.destination / (self.name + ext) for ext in ('.pak', '.utoc', '.ucas')]
        for target in self.targets:
            target.write_bytes(('old ' + target.name).encode())
        self.old = {target: target.read_bytes() for target in self.targets}
        self.unrelated = self.destination / 'OtherMod.pak'
        self.unrelated.write_bytes(b'leave other mods intact')

    def incoming(self, generation):
        build = self.root / ('build' + str(generation))
        build.mkdir()
        result = []
        for target in self.targets:
            path = build / target.name
            path.write_bytes(('new %s %s' % (generation, target.name)).encode())
            result.append(dict(path=str(path), bytes=path.stat().st_size, sha256=file_sha256(path)))
        return result

    def assert_clean(self):
        self.assertFalse(any(p.is_dir() for p in self.destination.iterdir()))
        self.assertEqual(self.unrelated.read_bytes(), b'leave other mods intact')

    def test_same_name_replaces_all_three_on_every_success_without_extra_copies(self):
        for generation in (1, 2):
            incoming = self.incoming(generation)
            result = _publish_outputs(list(reversed(incoming)), self.destination, self.name)
            self.assertEqual({Path(item['path']) for item in result}, set(self.targets))
            for target in self.targets:
                self.assertEqual(target.read_bytes(), ('new %s %s' % (generation, target.name)).encode())
            self.assert_clean()

    def test_bad_new_fingerprint_preserves_complete_old_set(self):
        incoming = self.incoming(1)
        incoming[-1]['sha256'] = '0' * 64
        with self.assertRaisesRegex(BridgeError, '校验失败'):
            _publish_outputs(incoming, self.destination, self.name)
        self.assertEqual({p: p.read_bytes() for p in self.targets}, self.old)
        self.assert_clean()

    def test_failure_during_install_rolls_back_old_set(self):
        replace = os.replace

        def fail_second_new(source, target):
            if str(source).endswith('.utoc.new'):
                raise PermissionError('simulated locked destination')
            return replace(source, target)

        with patch('nte_bridge.packaging.os.replace', side_effect=fail_second_new):
            with self.assertRaises(PermissionError):
                _publish_outputs(self.incoming(1), self.destination, self.name)
        self.assertEqual({p: p.read_bytes() for p in self.targets}, self.old)
        self.assert_clean()

    def test_locked_old_file_preserves_old_set(self):
        replace = os.replace

        def fail_backup(source, target):
            if Path(source) == self.targets[1]:
                raise PermissionError('old utoc is locked')
            return replace(source, target)

        with patch('nte_bridge.packaging.os.replace', side_effect=fail_backup):
            with self.assertRaises(PermissionError):
                _publish_outputs(self.incoming(1), self.destination, self.name)
        self.assertEqual({p: p.read_bytes() for p in self.targets}, self.old)
        self.assert_clean()

    def test_partial_old_set_is_completed_and_directory_target_rejected(self):
        self.targets[1].unlink()
        self.targets[2].unlink()
        self.targets[2].mkdir()
        with self.assertRaisesRegex(BridgeError, '普通文件'):
            _publish_outputs(self.incoming(1), self.destination, self.name)
        self.assertEqual(self.targets[0].read_bytes(), self.old[self.targets[0]])
        self.targets[2].rmdir()
        _publish_outputs(self.incoming(2), self.destination, self.name)
        self.assertTrue(all(p.is_file() for p in self.targets))
        self.assert_clean()

    def test_same_mod_output_lock_rejects_other_publisher(self):
        with _output_lock(self.destination, self.name):
            with self.assertRaisesRegex(BridgeError, '另一打包任务'):
                _publish_outputs(self.incoming(1), self.destination, self.name)
        self.assertEqual({p: p.read_bytes() for p in self.targets}, self.old)
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
