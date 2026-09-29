"""Read-only UE 5.6 loose cooked texture PixelFormat FString diagnostics.

This checks the actual serialized platform-data field, not asset compression
settings. It is a narrow inspection utility rather than a general asset parser.
UE source: Engine/Private/TextureDerivedData.cpp SerializePlatformData, bulk-data
layout: int32 SizeX, int32 SizeY, uint32 PackedData, FString PixelFormatString.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon"))
from nte_bridge.core import load_manifest, write_json


def inspect(manifest_path, cooked_root):
    manifest = load_manifest(manifest_path)
    prefix = Path(cooked_root) / Path(manifest["project_file"]).stem / "Content"
    textures = []
    for texture in manifest["textures"]:
        base = prefix / texture["asset_path"][len("/Game/"):]
        records = []
        for extension in (".uasset", ".uexp"):
            file = base.with_suffix(extension)
            if not file.exists():
                continue
            data = file.read_bytes()
            for match in re.finditer(rb"PF_[A-Za-z0-9_]+\x00", data):
                offset = match.start()
                if offset < 16:
                    continue
                length = struct.unpack_from("<i", data, offset - 4)[0]
                width, height, packed = struct.unpack_from("<iiI", data, offset - 16)
                if length != len(match.group()) or not (1 <= width <= 65536 and 1 <= height <= 65536):
                    continue
                records.append({"file": str(file), "offset": offset, "pixel_format": match.group()[:-1].decode("ascii"),
                                "width": width, "height": height, "packed_data": packed})
        expected = "PF_BC5" if texture["role"] == "NORMAL" else "PF_BC7"
        textures.append({"asset_path": texture["asset_path"], "role": texture["role"], "expected": expected,
                         "success": bool(records) and {record["pixel_format"] for record in records} == {expected}, "serialized_fields": records})
    return {"schema_version": 1, "job_id": manifest["job_id"], "success": all(t["success"] for t in textures), "textures": textures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("cooked_root")
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args()
    report = inspect(arguments.manifest, arguments.cooked_root)
    write_json(arguments.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["success"] else 1)
