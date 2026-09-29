"""Build an installable addon ZIP from an explicit runtime file allowlist."""
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import VERSION, write_json


def main():
    source = ROOT / "blender_addon" / "nte_bridge"
    destination = ROOT / "dist" / f"NTE-Bridge-v{VERSION}-Blender.zip"
    destination.parent.mkdir(exist_ok=True)
    adapter_dir = ROOT / "artifacts" / "packager_cli"
    adapter_files = [adapter_dir / ("NteBridge.Packager" + suffix)
                     for suffix in (".dll", ".deps.json", ".runtimeconfig.json")]
    if not all(path.is_file() for path in adapter_files):
        raise RuntimeError("Build the packager adapter first; see tools/packager_cli/README.md")
    files = sorted(p for p in source.rglob("*") if p.is_file() and
                   "__pycache__" not in p.parts and p.suffix.lower() in {".py", ".json", ".dll", ".exe", ".md"})
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, Path("nte_bridge") / path.relative_to(source))
        archive.write(ROOT / "README.md", "nte_bridge/README.md")
        archive.write(ROOT / "docs/VALIDATION.md", "nte_bridge/docs/VALIDATION.md")
        archive.write(ROOT / "docs/CONTRACT.md", "nte_bridge/docs/CONTRACT.md")
        archive.write(ROOT / "tools/packager_cli/README.md", "nte_bridge/tools/packager_cli/README.md")
        for path in adapter_files:
            archive.write(path, "nte_bridge/vendor/packager_cli/" + path.name)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC verification failed")
        entries = archive.namelist()
    report = {"version": VERSION, "file": destination.name,
              "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
              "size": destination.stat().st_size, "entries": entries}
    write_json(ROOT / "dist" / "release-verification.json", report)
    print(destination)
    print(report["sha256"])


if __name__ == "__main__":
    main()
