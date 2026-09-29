"""Local tool discovery without starting editors or modifying their settings."""
import json
import os
from pathlib import Path


def default_job_root():
    """An absolute cache location also works before the first .blend save."""
    base = os.environ.get('LOCALAPPDATA')
    return str((Path(base) if base else Path.home() / '.cache') / 'NTEBridge' / 'Jobs')


def _registered_engines(association):
    candidates = []
    if not association:
        return candidates
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Epic Games\Unreal Engine\Builds') as key:
                candidates.append(winreg.QueryValueEx(key, association)[0])
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                'SOFTWARE\\EpicGames\\Unreal Engine\\' + association) as key:
                candidates.append(winreg.QueryValueEx(key, 'InstalledDirectory')[0])
        except OSError:
            pass
    except ImportError:
        pass
    return candidates


def _launcher_engines(association):
    if not association:
        return []
    base = os.environ.get('PROGRAMDATA', r'C:\ProgramData')
    manifest = Path(base) / 'Epic' / 'UnrealEngineLauncher' / 'LauncherInstalled.dat'
    try:
        records = json.loads(manifest.read_text(encoding='utf-8-sig'))['InstallationList']
        return [item['InstallLocation'] for item in records
                if item.get('AppName') == 'UE_' + association and item.get('InstallLocation')]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _supported_engine(path):
    root = Path(path)
    try:
        version = json.loads((root / 'Engine/Build/Build.version').read_text(encoding='utf-8-sig'))
        return (version.get('MajorVersion'), version.get('MinorVersion')) == (5, 6) and all(
            (root / name).is_file() for name in (
                'Engine/Binaries/Win64/UnrealEditor-Cmd.exe',
                'Engine/Binaries/ThirdParty/Python3/Win64/python.exe'))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def detect_engine_dir(project_file):
    """Resolve only this project's explicit association; ambiguity needs a choice."""
    try:
        project = json.loads(Path(project_file).read_text(encoding='utf-8-sig'))
        association = project.get('EngineAssociation', '')
        if not isinstance(association, str) or not association:
            return ''
    except (OSError, ValueError, TypeError, AttributeError):
        return ''
    candidates = _registered_engines(association) + _launcher_engines(association)
    found = {os.path.normcase(str(Path(path).resolve())): str(Path(path).resolve())
             for path in candidates if _supported_engine(path)}
    return next(iter(found.values())) if len(found) == 1 else ''


def detect_packager_source():
    """Recognize the user's existing loader layout; never download tools."""
    override = os.environ.get('NTE_BRIDGE_PACKAGER', '')
    candidates = [Path(override)] if override else [
        Path(drive + ':/Neverness to Everness Mod Loader/cook/packager') for drive in ('C', 'D', 'E')]
    required = ('NteMorphTargetPatch.exe', 'retoc.exe', 'oo2core_9_win64.dll')
    found = [str(path.resolve()) for path in candidates if all((path / name).is_file() for name in required)]
    return found[0] if len(found) == 1 else ''
