import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'blender_addon'))
from nte_bridge.progress import read_progress, report_progress


class ProgressTests(unittest.TestCase):
    def test_progress_reports_real_counts_and_rejects_stale_worker(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'artifacts') as temporary:
            root = Path(temporary)
            path = root / 'progress.json'
            with patch.dict(os.environ, NTE_BRIDGE_PROGRESS=str(path), NTE_BRIDGE_PROGRESS_TOKEN='current'):
                report_progress('导出资产', 2, 4)
                self.assertEqual(read_progress(path, 'current'), ('导出资产 · 2 / 4', .5))
                self.assertIsNone(read_progress(path, 'stale'))
                log = root / 'cook.log'
                log.write_text('LogCook: Display: Cooked packages 15 Packages Remain 5 Total 20\n')
                report_progress('烘焙', log=log)
                self.assertEqual(read_progress(path, 'current'), ('UE 正在烘焙资产 · 15 / 20', .75))
                report_progress('正在启动 UE')
                self.assertEqual(read_progress(path, 'current'), ('正在启动 UE', -1.0))
                log.write_text(json.dumps(dict(stage='Verifying', percent=88)))
                report_progress('外部打包器', log=log)
                self.assertEqual(read_progress(path, 'current'), ('校验 Mod · 88 / 100', .88))
            self.assertFalse(path.with_suffix('.tmp').exists())
            path.write_text('{unfinished')
            self.assertIsNone(read_progress(path, 'current'))
