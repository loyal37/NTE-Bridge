"""Verify the built installation package and register it in isolated Blender."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import VERSION, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--zip", dest="archive", default=str(ROOT / "dist" / f"NTE-Bridge-v{VERSION}-Blender.zip"))
    args = parser.parse_args()
    archive_path = Path(args.archive).resolve()
    output_root = ROOT / "artifacts/release_smoke"
    output_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="package-", dir=output_root))
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip():
            raise RuntimeError("ZIP CRC mismatch")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("Duplicate archive entries")
        for name in names:
            target = (work / name).resolve()
            if not name.startswith("nte_bridge/") or not target.is_relative_to(work):
                raise RuntimeError("Unexpected archive path: " + name)
            if Path(name).suffix.lower() in {".pdb", ".blend", ".fbx", ".uasset", ".pak", ".utoc", ".ucas", ".cs"}:
                raise RuntimeError("Non-runtime file included: " + name)
        archive.extractall(work)
    runner = work / "verify_addon.py"
    report_path = work / "result.json"
    runner.write_text(
        "import sys, json\nfrom pathlib import Path\nimport bpy, addon_utils\n"
        + "sys.path.insert(0, " + repr(str(work)) + ")\n"
        + "import nte_bridge\nfrom nte_bridge import core\n"
        + "assert Path(nte_bridge.__file__).resolve().is_relative_to(Path(" + repr(str(work)) + "))\n"
        + "addon_utils.enable('nte_bridge', default_set=False)\n"
        + "assert addon_utils.check('nte_bridge')[1], 'Addon enable failed in Blender restricted registration context'\n"
        + "nte_bridge.blender_ui._initialize_cache()\n"
        + "tree = bpy.data.node_groups.new('Installation test', 'NTEBridgeTree')\n"
        + "for kind in ('NTEBridgePart', 'NTEBridgeGroup', 'NTEBridgeCycle', 'NTEBridgeOutput'): tree.nodes.new(kind)\n"
        + "assert len(tree.nodes) == 4\n"
        + "assert core.VERSION == '.'.join(str(n) for n in nte_bridge.bl_info['version'])\n"
        + "assert (Path(nte_bridge.__file__).parent/'vendor/packager_cli/NteBridge.Packager.dll').is_file()\n"
        + "bpy.data.node_groups.remove(tree)\n"
        + "addon_utils.disable('nte_bridge', default_set=False)\n"
        + "Path(" + repr(str(report_path)) + ").write_text(json.dumps({'success':True,'version':core.VERSION}), encoding='utf-8')\n",
        encoding="utf-8")
    with (work / "blender.log").open("wb") as log:
        result = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec",
                                 "--python-exit-code", "1", "--python", str(runner)], stdout=log,
                                stderr=subprocess.STDOUT, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not report_path.is_file():
        raise RuntimeError("Packaged addon failed isolated registration; see " + str(work / "blender.log"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(archive=str(archive_path), sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                  entries=len(names), log=str(work / "blender.log"))
    write_json(output_root / "result.json", report)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
