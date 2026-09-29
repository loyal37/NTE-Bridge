import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_addon'))
from nte_bridge.workflow import default_job_root, detect_engine_dir, detect_packager_source


class WorkflowTests(unittest.TestCase):
    def engine(self, folder, minor=6):
        for name in ('Engine/Build/Build.version', 'Engine/Binaries/Win64/UnrealEditor-Cmd.exe',
                     'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'):
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({'MajorVersion': 5, 'MinorVersion': minor}), encoding='utf-8')
        return str(folder)

    def test_ambiguous_or_wrong_version_does_not_choose_first_editor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'Test.uproject'
            project.write_text('{"EngineAssociation":"5.6"}', encoding='utf-8')
            one, two, wrong = [self.engine(root / name, minor) for name, minor in [('one', 6), ('two', 6), ('wrong', 7)]]
            with patch('nte_bridge.workflow._registered_engines', return_value=[one]), \
                    patch('nte_bridge.workflow._launcher_engines', return_value=[one, wrong]):
                self.assertEqual(detect_engine_dir(project), one)
            with patch('nte_bridge.workflow._registered_engines', return_value=[one, two]), \
                    patch('nte_bridge.workflow._launcher_engines', return_value=[]):
                self.assertEqual(detect_engine_dir(project), '')
            project.write_text('{}', encoding='utf-8')
            self.assertEqual(detect_engine_dir(project), '')

    def test_unsaved_blend_jobs_do_not_depend_on_current_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'LOCALAPPDATA': temp}):
            path = Path(default_job_root())
            self.assertTrue(path.is_absolute())
            self.assertEqual(path, Path(temp) / 'NTEBridge/Jobs')
            self.assertFalse(path.exists())

    def test_explicit_packager_override_requires_complete_existing_tools(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'NTE_BRIDGE_PACKAGER': temp}):
            root = Path(temp)
            for name in ('NteMorphTargetPatch.exe', 'retoc.exe'):
                (root / name).touch()
            self.assertEqual(detect_packager_source(), '')
            (root / 'oo2core_9_win64.dll').touch()
            self.assertEqual(detect_packager_source(), str(root.resolve()))


if __name__ == '__main__':
    unittest.main()
