"""Create the isolated UE fixture without overwriting any existing configuration."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_addon"))
from nte_bridge.core import write_json


def main():
    folder = ROOT / "artifacts/ue_smoke"
    folder.mkdir(parents=True, exist_ok=True)
    project = folder / "NTEBridgeSmoke.uproject"
    if not project.exists():
        write_json(project, {
            "FileVersion": 3, "EngineAssociation": "5.6",
            "Plugins": [{"Name": name, "Enabled": True} for name in (
                "PythonScriptPlugin", "EditorScriptingUtilities", "GeometryScripting", "ControlRig")],
        })
    config = folder / "Config/DefaultEngine.ini"
    if not config.exists():
        config.parent.mkdir(exist_ok=True)
        config.write_text(
            "[/Script/EngineSettings.GameMapsSettings]\n"
            "EditorStartupMap=/Engine/Maps/Entry\nGameDefaultMap=/Engine/Maps/Entry\n\n"
            "[/Script/UnrealEd.EditorLoadingSavingSettings]\nbAutoSaveEnable=False\n", encoding="utf-8")
    print(project)


if __name__ == "__main__":
    main()
