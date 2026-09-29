"""Local tool discovery without starting editors or modifying their settings."""
import json
import os
from pathlib import Path


def _absolute_path(value):
    value = str(value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    if not value:
        return None
    try:
        path = Path(value)
        return path.resolve() if path.is_absolute() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _non_c_path(value):
    path = _absolute_path(value)
    if path is None or path.drive.upper().removeprefix('\\\\?\\') == 'C:':
        return None
    return path


def default_cache_root(project_file='', blend_file='', source_folder=''):
    """Suggest a context-owned cache without creating folders or choosing C:."""
    for value, suffix, relative in (
            (project_file, '.uproject', 'Saved/NTEBridgeCache'),
            (blend_file, '.blend', 'NTEBridgeCache'),
            (source_folder, '', 'NTEBridgeCache')):
        path = _non_c_path(value)
        if path is None:
            continue
        try:
            if suffix:
                if path.suffix.lower() != suffix or not path.is_file():
                    continue
            elif not path.is_dir():
                continue
            candidate = _non_c_path(path.parent / relative)
            if candidate is not None and not candidate.is_file() and not candidate.parent.is_file():
                return str(candidate)
        except (OSError, ValueError):
            continue
    return ''


def default_job_root(project_file='', blend_file='', source_folder=''):
    """Compatibility wrapper; an unset context requires an explicit cache choice."""
    cache = default_cache_root(project_file, blend_file, source_folder)
    return str(Path(cache) / 'Jobs') if cache else ''


def legacy_default_job_root(value):
    """Identify old automatic locations for migration, never select them anew."""
    if str(value or '').strip().strip('"\'') == '//NTEBridgeJobs':
        return True
    path = _absolute_path(value)
    if path is None:
        return False
    base = os.environ.get('LOCALAPPDATA')
    old_root = (Path(base) if base else Path.home() / '.cache') / 'NTEBridge/Jobs'
    return path == _absolute_path(old_root)


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
