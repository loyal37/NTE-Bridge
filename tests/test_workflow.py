import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender_addon'))
from nte_bridge.workflow import (
    default_cache_root, default_job_root, detect_engine_dir, detect_packager_source,
    legacy_default_job_root,
)


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

    def cache_context(self, root):
        project = root / 'UE Project' / 'Test.uproject'
        blend = root / 'Models' / 'Character.blend'
        source = root / 'Unpacked' / 'Character'
        for item in (project, blend):
            item.parent.mkdir(parents=True)
            item.write_text('{}', encoding='utf-8')
        source.mkdir(parents=True)
        return project, blend, source

    def test_cache_prefers_project_then_saved_blend_then_source_parent(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            root = Path(temp)
            project, blend, source = self.cache_context(root)
            self.assertEqual(default_cache_root(project, blend, source),
                             str(project.parent / 'Saved/NTEBridgeCache'))
            self.assertEqual(default_cache_root('', blend, source),
                             str(blend.parent / 'NTEBridgeCache'))
            self.assertEqual(default_cache_root('', '', source),
                             str(source.parent / 'NTEBridgeCache'))
            self.assertEqual(default_job_root(project),
                             str(project.parent / 'Saved/NTEBridgeCache/Jobs'))

    def test_unsaved_blend_can_use_project_without_creating_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            project, _, _ = self.cache_context(Path(temp))
            before = set(Path(temp).rglob('*'))
            self.assertEqual(default_cache_root(project_file=project),
                             str(project.parent / 'Saved/NTEBridgeCache'))
            self.assertEqual(set(Path(temp).rglob('*')), before)

    def test_quoted_paths_are_normalized_without_writes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            root = Path(temp)
            project, blend, source = self.cache_context(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(default_cache_root(' "' + str(project) + '" '),
                             str(project.parent / 'Saved/NTEBridgeCache'))
            self.assertEqual(default_cache_root(blend_file="'" + str(blend) + "'"),
                             str(blend.parent / 'NTEBridgeCache'))
            self.assertEqual(default_cache_root(source_folder='"' + str(source) + '"'),
                             str(source.parent / 'NTEBridgeCache'))
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(after, before)
            self.assertFalse(any(root.rglob('NTEBridgeCache')))

    @unittest.skipUnless(os.name == 'nt', 'Windows drive policy')
    def test_c_drive_contexts_are_skipped_even_when_present(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            _, blend, source = self.cache_context(Path(temp))
            with patch('nte_bridge.workflow.Path.is_file', return_value=True):
                self.assertEqual(default_cache_root(r'C:\Project\Game.uproject'), '')
                self.assertEqual(default_cache_root(blend_file=r'c:\Models\Character.blend'), '')
                self.assertEqual(default_cache_root(project_file=r'\\?\C:\Project\Game.uproject'), '')
            self.assertEqual(default_cache_root(r'C:\Project\Game.uproject', blend),
                             str(blend.parent / 'NTEBridgeCache'))
            self.assertEqual(default_cache_root(r'C:\Project\Game.uproject',
                                                r'C:\Models\Character.blend', source),
                             str(source.parent / 'NTEBridgeCache'))
            with patch('nte_bridge.workflow.Path.is_dir', return_value=True):
                self.assertEqual(default_cache_root(source_folder=r'C:\Unpacked\Character'), '')

    def test_absent_or_ambiguous_context_never_uses_home_appdata_or_cwd(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': r'D:\AnyAppData'}), \
                patch('nte_bridge.workflow.Path.home', side_effect=AssertionError('No home fallback')):
            self.assertEqual(default_cache_root(), '')
            self.assertEqual(default_job_root(), '')
            self.assertEqual(default_cache_root('Game.uproject', '//Character.blend', 'Unpacked'), '')
            self.assertEqual(default_cache_root('D:Game.uproject', 'D:Character.blend', 'D:Unpacked'), '')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            project, _, _ = self.cache_context(Path(temp))
            self.assertEqual(default_cache_root(project.parent), '')
            self.assertEqual(default_cache_root(project.parent / 'Missing.uproject'), '')
            self.assertEqual(default_cache_root(blend_file=project), '')

    def test_cache_destination_conflict_falls_back_to_valid_context(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            project, blend, _ = self.cache_context(Path(temp))
            (project.parent / 'Saved').write_text('existing file', encoding='utf-8')
            self.assertEqual(default_cache_root(project, blend),
                             str(blend.parent / 'NTEBridgeCache'))
            (blend.parent / 'NTEBridgeCache').write_text('existing file', encoding='utf-8')
            self.assertEqual(default_cache_root(project, blend), '')

    def test_legacy_cache_detection_preserves_custom_roots(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp, \
                patch.dict(os.environ, {'LOCALAPPDATA': temp}):
            old = Path(temp) / 'NTEBridge/Jobs'
            self.assertTrue(legacy_default_job_root(old))
            self.assertTrue(legacy_default_job_root(' "' + str(old) + '" '))
            self.assertTrue(legacy_default_job_root('//NTEBridgeJobs'))
            self.assertFalse(legacy_default_job_root(Path(temp) / 'My Jobs'))
            self.assertFalse(legacy_default_job_root(''))
            self.assertFalse(old.exists())

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
