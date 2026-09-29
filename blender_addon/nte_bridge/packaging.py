"""Cook and package a verified bridge job, without importing Blender.

The packager consumes a fresh allow-listed tree. Cooking may produce dependencies;
those are deliberately never copied unless they appear in export_assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid

from .core import BridgeError, load_manifest, resolve_source, write_json

SIDECARS = (".uasset", ".uexp", ".ubulk", ".uptnl")
PACKABLE_TYPES = {"SkeletalMesh", "Texture2D"}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root, relative):
    """Resolve a strict relative path, including symlinks/junctions."""
    root = Path(root).resolve()
    value = str(relative).replace("\\", "/")
    parts = PurePosixPath(value).parts
    if not parts or value.startswith("/") or ":" in value or any(p in (".", "..") for p in parts):
        raise BridgeError("Unsafe relative path: " + value)
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise BridgeError("Path escapes its root: " + value)
    return target


def _validated_job(manifest_path, ue_report_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    report = json.loads(Path(ue_report_path).read_text(encoding="utf-8-sig"))
    manifest_hash = file_sha256(manifest_path)
    if report.get("schema_version") != 1:
        raise BridgeError("Unsupported UE report schema.")
    if report.get("success") is not True or report.get("errors"):
        raise BridgeError("A successful UE import report is required before packaging.")
    if report.get("job_id") != manifest["job_id"] or report.get("manifest_sha256") != manifest_hash:
        raise BridgeError("UE report is stale: job ID or manifest SHA256 does not match.")
    sources = [manifest["mesh"]["source_file"]] + [texture["source_file"] for texture in manifest.get("textures", [])]
    source_hashes = {source: file_sha256(resolve_source(manifest_path.parent, source)) for source in sources}
    if report.get("source_sha256") != source_hashes:
        raise BridgeError("UE report is stale: source FBX or textures changed after import.")
    if os.path.normcase(str(Path(report.get("project_file", "")).resolve())) != os.path.normcase(str(Path(manifest["project_file"]).resolve())):
        raise BridgeError("UE report belongs to a different project.")
    if manifest.get("features"):
        raise BridgeError("This bridge version cannot package pending runtime features; remove them or wait for generation support.")
    assets = manifest.get("export_assets", [])
    if not assets:
        raise BridgeError("The export allow-list is empty.")
    reported = {item.get("asset_path"): item for item in report.get("assets", []) if isinstance(item, dict)}
    seen = set()
    for asset in assets:
        path = asset["asset_path"]
        if path.casefold() in seen:
            raise BridgeError("Duplicate export asset: " + path)
        seen.add(path.casefold())
        if asset.get("asset_type") not in PACKABLE_TYPES or asset.get("origin") != "mod":
            raise BridgeError("Export asset type/origin is not supported: " + path)
        actual = reported.get(path, {})
        if actual.get("asset_type") != asset["asset_type"] or actual.get("saved") is not True or actual.get("origin") != "mod":
            raise BridgeError("Export asset was not verified and saved by UE: " + path)
    # Job A's unchanged FBX/report must not cook Job B's later import into the
    # same /Game package. Bind the report to the exact saved editor asset bytes.
    content = Path(manifest["project_file"]).resolve().parent / "Content"
    saved_hashes = {}
    for asset in assets:
        relative_base = asset["asset_path"][len("/Game/"):]
        required = _inside(content, relative_base + ".uasset")
        if not required.is_file() or required.stat().st_size == 0:
            raise BridgeError("Saved UE asset is missing/empty: " + asset["asset_path"])
        for extension in SIDECARS:
            relative = relative_base + extension
            file = _inside(content, relative)
            if file.is_file():
                saved_hashes[relative] = file_sha256(file)
    if report.get("saved_asset_sha256") != saved_hashes:
        raise BridgeError("UE report is stale: saved project assets changed since this job imported them. Sync this job again before cooking.")
    return manifest, manifest_hash


def stage_assets(manifest_path, ue_report_path, cooked_root, staging_parent=None):
    """Copy exactly the allowed packages and available sidecars to a new tree.

    cooked_root must contain <ProjectName>/Content, e.g. Saved/Cooked/Windows.
    No existing staging directory is reused or cleaned recursively.
    """
    manifest, manifest_hash = _validated_job(manifest_path, ue_report_path)
    cooked_root = Path(cooked_root).resolve()
    project_name = Path(manifest["project_file"]).stem
    files = []
    missing = []
    for asset in manifest["export_assets"]:
        path = asset["asset_path"]
        if not path.startswith("/Game/"):
            raise BridgeError("Only canonical /Game packages are supported: " + path)
        relative_base = project_name + "/Content/" + path[len("/Game/"):]
        required = _inside(cooked_root, relative_base + ".uasset")
        if not required.is_file() or required.stat().st_size == 0:
            missing.append(path)
            continue
        for extension in SIDECARS:
            relative = relative_base + extension
            source = _inside(cooked_root, relative)
            if source.is_file():
                files.append((relative, source))
    if missing:
        raise BridgeError("Required cooked packages are missing/empty: " + ", ".join(missing))
    parent = Path(staging_parent or Path(manifest_path).resolve().parent / "packaging").resolve()
    if parent.is_relative_to(cooked_root):
        raise BridgeError("Staging directory cannot be inside the cooked source.")
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="package-", dir=parent))
    source_dir = run_dir / "staging"
    source_dir.mkdir()
    inventory = []
    for relative, source in files:
        destination = _inside(source_dir, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        inventory.append({"path": relative, "bytes": destination.stat().st_size, "sha256": file_sha256(destination)})
    report = {"schema_version": 1, "job_id": manifest["job_id"], "manifest_sha256": manifest_hash,
              "run_id": str(uuid.uuid4()), "source_dir": str(source_dir), "run_dir": str(run_dir),
              "project_file": manifest["project_file"], "files": inventory}
    write_json(run_dir / "staging_report.json", report)
    return report


def _run(command, log_path, timeout, *, env=None):
    """File-backed logs avoid pipe deadlocks; timeout terminates the process tree."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with log_path.open("wb") as log:
        arguments = command if isinstance(command, str) else [str(x) for x in command]
        process = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT, env=env, **options)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise BridgeError("Process cancelled or timed out. Log: " + str(log_path))
    if code:
        raise BridgeError("Process failed (exit %s). Log: %s" % (code, log_path))


def _unreal_command_line(command):
    """UE parses the raw Windows command line, requiring -Key=\"value spaces\".

    This goes straight to CreateProcess, never through cmd.exe/PowerShell.
    Normal list2cmdline quotes the whole -Key=value token, which FParse::Value
    would truncate at its first space.
    """
    if os.name != "nt":
        return command
    arguments = []
    for argument in map(str, command):
        if '"' in argument or "\n" in argument or "\r" in argument:
            raise BridgeError("Quotes and newlines are unsupported in UE process arguments.")
        if argument.startswith("-") and "=" in argument:
            key, value = argument.split("=", 1)
            arguments.append(key + '=\"' + value + '\"')
        elif argument.startswith("-") and not any(c.isspace() for c in argument):
            arguments.append(argument)
        else:
            arguments.append('\"' + argument + '\"')
    return " ".join(arguments)


def build_adapter(packager_source_dir, output_dir=None, timeout=300):
    """Compile linked packager services; do not copy their repository or tools."""
    repo = Path(__file__).resolve().parents[2]
    project = repo / "tools/packager_cli/NteBridge.Packager.csproj"
    if not project.is_file():
        raise BridgeError("Packager adapter is not bundled; configure adapter_path or build it from the bridge repository.")
    output = Path(output_dir or repo / "artifacts/packager_cli").resolve()
    source = Path(packager_source_dir).resolve()
    if not (source / "src/NteModPackager/Services/BuildService.cs").is_file():
        raise BridgeError("Packager source directory must contain src/NteModPackager/Services/BuildService.cs.")
    _run(["dotnet", "publish", project, "-c", "Release", "-o", output, "-p:PackagerSourceDir=" + str(source)], output / "build.log", timeout)
    return output / "NteBridge.Packager.dll"


def cook_assets(manifest, engine_dir, output_dir, timeout=1800):
    """Cook only the allowed asset directories into an isolated loose-file tree."""
    engine = Path(engine_dir).resolve()
    if engine.name.lower() != "engine":
        engine = engine / "Engine"
    editor = engine / "Binaries/Win64/UnrealEditor-Cmd.exe"
    if not editor.is_file():
        raise BridgeError("UnrealEditor-Cmd.exe was not found in the configured engine.")
    project = Path(manifest["project_file"]).resolve()
    if not project.is_file():
        raise BridgeError("UE project file does not exist.")
    from .unreal_transport import _project_editor_running
    if _project_editor_running(project):
        raise BridgeError("Close the target Unreal project before offline cooking; an editor/import process still has it open.")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    directories = sorted({str(_inside(project.parent / "Content", str(PurePosixPath(a["asset_path"][len("/Game/"):]).parent)))
                          for a in manifest["export_assets"]})
    if any(Path(directory) == project.parent / "Content" for directory in directories):
        raise BridgeError("Assets directly under /Game would require cooking the entire Content folder; move them into a character folder.")
    command = [editor, project, "-run=Cook", "-TargetPlatform=Windows", "-SkipZenStore", "-unattended", "-nop4",
               "-UTF8Output", "-OutputDir=" + str(output / "[Platform]"), "-abslog=" + str(output.parent / "cook-engine.log")]
    command.extend("-CookDir=" + directory for directory in directories)
    _run(_unreal_command_line(command), output.parent / "cook.log", timeout)
    # Keep UE's [Platform] token: its asynchronous delete workspace uses _Del
    # in place of the platform and must not share the actual cooked root.
    for candidate in (output, output / "Windows"):
        if (candidate / project.stem / "Content").is_dir():
            return candidate
    raise BridgeError("Cook finished without the expected <Project>/Content loose-file directory.")


def package_job(manifest_path, ue_report_path, engine_dir=None, packager_source_dir=None,
                packager_tools_dir=None, output_dir=None, mod_name=None, cooked_root=None,
                run_cook=True, timeout=1800, adapter_path=None, report_path=None):
    """Return and persist a phase-specific report; errors never reuse old success."""
    manifest_path = Path(manifest_path).resolve()
    report_path = Path(report_path or manifest_path.parent / "package_report.json")
    if report_path.resolve() in (manifest_path, Path(ue_report_path).resolve()):
        raise BridgeError("Package report must not overwrite the source manifest or UE report.")
    result = {"schema_version": 1, "success": False, "phase": "validation", "errors": [], "outputs": [],
              "manifest_sha256": file_sha256(manifest_path) if manifest_path.is_file() else ""}
    try:
        manifest, manifest_hash = _validated_job(manifest_path, ue_report_path)
        result.update(job_id=manifest["job_id"], project_file=manifest["project_file"])
        name = mod_name or "NTE_" + str(manifest["character_id"]).replace("-", "")[:12] + "_P"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", name):
            raise BridgeError("Mod name must contain only ASCII letters, numbers, underscore or hyphen.")
        if not packager_tools_dir:
            packager_tools_dir = packager_source_dir
        if not packager_tools_dir:
            raise BridgeError("Configure the installed packager tools directory.")
        parent = manifest_path.parent / "packaging"
        parent.mkdir(exist_ok=True)
        run_root = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        result["run_dir"] = str(run_root)
        if run_cook:
            result["phase"] = "cook"
            if not engine_dir:
                raise BridgeError("Configure the UE engine directory before cooking.")
            cooked_root = cook_assets(manifest, engine_dir, run_root / "cooked", timeout)
        elif not cooked_root:
            raise BridgeError("cooked_root is required when run_cook is false.")
        result["phase"] = "staging"
        staging = stage_assets(manifest_path, ue_report_path, cooked_root, run_root)
        result["staging_report"] = str(Path(staging["run_dir"]) / "staging_report.json")
        result["phase"] = "adapter"
        bundled = Path(__file__).parent / "vendor/packager_cli/NteBridge.Packager.dll"
        adapter = Path(adapter_path) if adapter_path else bundled
        if not adapter.is_file():
            if not packager_source_dir:
                raise BridgeError("A compiled adapter or the external packager source directory is required.")
            adapter = build_adapter(packager_source_dir)
        destination = Path(output_dir).resolve() if output_dir else run_root / "output"
        if destination.is_relative_to(Path(staging["source_dir"])):
            raise BridgeError("Package output cannot be inside staging.")
        if any((destination / (name + ext)).exists() for ext in (".pak", ".utoc", ".ucas")):
            raise BridgeError("Output already exists; choose an empty output directory or a new mod name.")
        request = dict(staging, output_dir=str(destination), mod_name=name, tools_dir=str(Path(packager_tools_dir).resolve()))
        request_path = run_root / "packager_request.json"
        adapter_report_path = run_root / "packager_report.json"
        write_json(request_path, request)
        result["phase"] = "packager"
        command = ["dotnet", adapter] if adapter.suffix.lower() == ".dll" else [adapter]
        # The external BuildService uses Path.GetTempPath() for another full
        # staging copy. Keep that child process's workspace beside this job.
        temporary_root = run_root / "temp"
        temporary_root.mkdir()
        adapter_environment = dict(os.environ)
        adapter_environment.update({key: str(temporary_root) for key in ("TEMP", "TMP", "TMPDIR")})
        _run(command + ["--job", request_path, "--report", adapter_report_path],
             run_root / "packager.log", timeout, env=adapter_environment)
        reply = json.loads(adapter_report_path.read_text(encoding="utf-8-sig"))
        if reply.get("success") is not True or any(reply.get(key) != request[key] for key in ("job_id", "manifest_sha256", "run_id")):
            raise BridgeError("Packager returned a failed or stale report.")
        outputs = reply.get("outputs", [])
        expected = {str((destination / (name + ext)).resolve()) for ext in (".pak", ".utoc", ".ucas")}
        if len(outputs) != 3 or {str(Path(item["path"]).resolve()) for item in outputs} != expected:
            raise BridgeError("Packager did not report exactly the expected three output files.")
        for item in outputs:
            output = Path(item["path"])
            if not output.is_file() or output.stat().st_size <= 0 or output.stat().st_size != item["bytes"] or file_sha256(output) != item["sha256"].lower():
                raise BridgeError("Packager output verification failed: " + str(output))
        result.update(success=True, phase="complete", outputs=outputs, packager_report=str(adapter_report_path))
    except Exception as error:
        result["errors"].append(str(error))
    write_json(report_path, result)
    return result


def stage_selection(selection_path, staging_parent=None):
    """Validate one cooked snapshot and copy only explicitly checked packages."""
    from .cooking import load_cook_report, _packable
    from .core import package_path
    selection_path = Path(selection_path).resolve()
    selection = json.loads(selection_path.read_text(encoding='utf-8-sig'))
    if selection.get('schema_version') != 1:
        raise BridgeError('不支持的资产选择格式。')
    report_path = Path(selection.get('cook_report', ''))
    if not report_path.is_absolute() or not re.fullmatch('[0-9a-fA-F]{64}', selection.get('cook_report_sha256', '')):
        raise BridgeError('资产选择必须指定烘焙报告绝对路径及其 SHA256。')
    report = load_cook_report(report_path, selection['cook_report_sha256'], verify_files=True)
    selected = selection.get('selected_assets')
    if not isinstance(selected, list) or not selected:
        raise BridgeError('请先勾选需要导出的烘焙资产。')
    catalog = {asset['asset_path'].casefold(): asset for asset in report['assets']}
    excluded = {path.casefold() for path in report.get('excluded_assets', [])}
    assets, seen, files = [], set(), []
    for path in selected:
        package_path(path, '勾选资源路径')
        key = path.casefold()
        if key in seen or key not in catalog:
            raise BridgeError('勾选清单包含重复或未烘焙的资产：' + path)
        seen.add(key)
        asset = catalog[key]
        allowed, reason = _packable(asset['asset_path'], asset['asset_type'], excluded)
        if not allowed or asset.get('packable') is not True:
            raise BridgeError('此资产禁止打包：' + path + '；' + (reason or asset.get('reason', '')))
        main = [entry for entry in asset['files'] if entry['path'].lower().endswith('.uasset')]
        if len(main) != 1 or main[0]['bytes'] <= 0:
            raise BridgeError('勾选资产缺少有效主文件：' + path)
        if any(PurePosixPath(entry['path']).suffix.lower() not in SIDECARS for entry in asset['files']):
            raise BridgeError('勾选资产含不支持的旁文件：' + path)
        assets.append(asset['asset_path'])
        files.extend(asset['files'])
    cooked = Path(report['cooked_root']).resolve()
    parent = Path(staging_parent or selection_path.parent / 'packaging').resolve()
    if parent.is_relative_to(cooked):
        raise BridgeError('打包暂存目录不能放在烘焙快照中。')
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='selection-', dir=parent))
    source_dir = run_dir / 'staging'
    source_dir.mkdir()
    for item in files:
        source = _inside(cooked, item['path'])
        destination = _inside(source_dir, item['path'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if destination.stat().st_size != item['bytes'] or file_sha256(destination) != item['sha256']:
            raise BridgeError('复制过程中烘焙文件发生改变：' + item['path'])
    result = {'schema_version': 1, 'job_id': report['cook_id'], 'cook_id': report['cook_id'],
              'manifest_sha256': report['manifest_sha256'], 'run_id': str(uuid.uuid4()),
              'project_file': report['project_file'], 'character_folder': report['character_folder'],
              'cook_report': str(report_path.resolve()), 'cook_report_sha256': selection['cook_report_sha256'],
              'cooked_root': str(cooked), 'selected_assets': assets,
              'source_dir': str(source_dir), 'run_dir': str(run_dir), 'files': files}
    write_json(run_dir / 'staging_report.json', result)
    return result


def export_selection(staging, export_directory):
    """Mirror selected packages to the explicit Characters mount, never sweep xg."""
    value = Path(export_directory)
    project = Path(staging['project_file']).stem
    if not value.is_absolute() or value.name.casefold() != 'characters' \
            or value.parent.name.casefold() != 'content' or value.parent.parent.name.casefold() != project.casefold():
        raise BridgeError('导出目录应为 <打包器>/xg/%s/Content/Characters；保持工程挂载名和 Content 结构。' % project)
    value = value.resolve()
    if value.name.casefold() != 'characters' or value.parent.name.casefold() != 'content' \
            or value.parent.parent.name.casefold() != project.casefold():
        raise BridgeError('导出目录的链接目标改变了工程挂载名或 Content/Characters 结构。')
    root = value.parents[2]
    cooked = Path(staging['cooked_root']).resolve()
    stage = Path(staging['source_dir']).resolve()
    if root.is_relative_to(cooked) or cooked.is_relative_to(root) or root.is_relative_to(stage) or stage.is_relative_to(root):
        raise BridgeError('导出目录不能与烘焙快照或打包暂存目录重叠。')
    targets, stale = [], []
    listed = {item['path'].casefold() for item in staging['files']}
    # Complete preflight before overwriting any previous exported file.
    for item in staging['files']:
        source = _inside(stage, item['path'])
        destination = _inside(root, item['path'])
        if not source.is_file() or source.stat().st_size != item['bytes'] or file_sha256(source) != item['sha256']:
            raise BridgeError('导出前暂存文件发生改变：' + item['path'])
        if destination.exists() and not destination.is_file():
            raise BridgeError('导出文件位置已被文件夹占用：' + str(destination))
        ancestor = destination.parent
        while ancestor != root.parent:
            if ancestor.exists() and not ancestor.is_dir():
                raise BridgeError('导出目录被文件占用：' + str(ancestor))
            ancestor = ancestor.parent
        targets.append((item, source, destination))
    for asset in staging['selected_assets']:
        base = project + '/Content/' + asset[len('/Game/'):]
        for extension in SIDECARS:
            relative = base + extension
            if relative.casefold() not in listed:
                previous = _inside(root, relative)
                if previous.exists():
                    if not previous.is_file():
                        raise BridgeError('旧旁文件路径被目录占用：' + str(previous))
                    stale.append(previous)
    exported = []
    for item, source, destination in targets:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        exported.append(dict(item, exported_path=str(destination)))
    # Only obsolete sidecars belonging to the selected package are removed.
    # Unselected packages stay in xg, and adapter input remains fresh staging.
    for previous in stale:
        previous.unlink()
    return {'export_directory': str(value.resolve()), 'exported_files': exported,
            'removed_stale_sidecars': [str(path) for path in stale]}


def _selection_package_preflight(staging, packager_source_dir, packager_tools_dir, output_dir, mod_name, adapter_path):
    name = mod_name or 'NTEBridgeMod'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}', name):
        raise BridgeError('Mod 名称只能包含英文字母、数字、下划线或连字符。')
    tools_dir = Path(packager_tools_dir or packager_source_dir or '')
    required = ('NteMorphTargetPatch.exe', 'retoc.exe', 'oo2core_9_win64.dll')
    if not (packager_tools_dir or packager_source_dir) or not all((tools_dir / name).is_file() for name in required):
        raise BridgeError('外部打包器目录缺少形态键修复、retoc 或 Oodle 工具。')
    adapter = Path(adapter_path) if adapter_path else Path(__file__).parent / 'vendor/packager_cli/NteBridge.Packager.dll'
    if not adapter.is_file() and (adapter_path or not packager_source_dir or not
            (Path(packager_source_dir) / 'src/NteModPackager/Services/BuildService.cs').is_file()):
        raise BridgeError('缺少可用的外部打包器适配器或适配器构建源码。')
    destination = Path(output_dir).resolve() if output_dir else Path(staging['run_dir']) / 'output'
    if destination.is_relative_to(Path(staging['source_dir'])) or destination.is_relative_to(Path(staging['cooked_root'])):
        raise BridgeError('Mod 输出目录不能放在烘焙快照或打包暂存文件夹内。')
    if destination.exists() and not destination.is_dir():
        raise BridgeError('Mod 输出目录指向了文件。')
    if any((destination / (name + extension)).exists() for extension in ('.pak', '.utoc', '.ucas')):
        raise BridgeError('同名 Mod 输出已存在，请换一个名称或输出目录。')


def _package_staging(staging, packager_source_dir, packager_tools_dir, output_dir,
                     mod_name, adapter_path, timeout):
    run_root = Path(staging['run_dir'])
    name = mod_name or 'NTEBridgeMod'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}', name):
        raise BridgeError('Mod 名称只能包含英文字母、数字、下划线或连字符。')
    tools_dir = packager_tools_dir or packager_source_dir
    if not tools_dir:
        raise BridgeError('请选择外部打包器工具目录。')
    bundled = Path(__file__).parent / 'vendor/packager_cli/NteBridge.Packager.dll'
    adapter = Path(adapter_path) if adapter_path else bundled
    if not adapter.is_file():
        if not packager_source_dir:
            raise BridgeError('缺少外部打包器适配器。')
        adapter = build_adapter(packager_source_dir)
    destination = Path(output_dir).resolve() if output_dir else run_root / 'output'
    if destination.is_relative_to(Path(staging['source_dir'])):
        raise BridgeError('Mod 输出目录不能在打包暂存文件夹内。')
    if any((destination / (name + extension)).exists() for extension in ('.pak', '.utoc', '.ucas')):
        raise BridgeError('同名 Mod 输出已存在，请换一个名称或输出目录。')
    request = dict(staging, output_dir=str(destination), mod_name=name, tools_dir=str(Path(tools_dir).resolve()))
    request_path, reply_path = run_root / 'packager_request.json', run_root / 'packager_report.json'
    write_json(request_path, request)
    temporary_root = run_root / 'temp'
    temporary_root.mkdir()
    environment = dict(os.environ)
    environment.update({key: str(temporary_root) for key in ('TEMP', 'TMP', 'TMPDIR')})
    command = ['dotnet', adapter] if adapter.suffix.lower() == '.dll' else [adapter]
    _run(command + ['--job', request_path, '--report', reply_path], run_root / 'packager.log', timeout, env=environment)
    reply = json.loads(reply_path.read_text(encoding='utf-8-sig'))
    if reply.get('success') is not True or any(reply.get(key) != request[key]
                                             for key in ('job_id', 'manifest_sha256', 'run_id')):
        raise BridgeError('外部打包器返回了失败或过期的报告。')
    outputs = reply.get('outputs', [])
    expected = {str((destination / (name + extension)).resolve()) for extension in ('.pak', '.utoc', '.ucas')}
    if len(outputs) != 3 or {str(Path(item['path']).resolve()) for item in outputs} != expected:
        raise BridgeError('外部打包器没有生成预期的三个 Mod 文件。')
    for item in outputs:
        path = Path(item['path'])
        if not path.is_file() or item['bytes'] <= 0 or path.stat().st_size != item['bytes'] or file_sha256(path) != item['sha256'].lower():
            raise BridgeError('打包输出文件指纹不匹配：' + str(path))
    return outputs, str(reply_path)


def package_selection(selection_path, packager_source_dir=None, packager_tools_dir=None,
                      output_dir=None, mod_name=None, timeout=1800, adapter_path=None, report_path=None):
    """Export checked snapshot assets and package their fresh tree; never recook."""
    selection_path = Path(selection_path).resolve()
    report_path = Path(report_path or selection_path.with_name('selection_package_report.json')).resolve()
    selection = json.loads(selection_path.read_text(encoding='utf-8-sig'))
    if report_path in (selection_path, Path(selection.get('cook_report', '')).resolve()):
        raise BridgeError('打包报告不能覆盖资产选择或烘焙报告。')
    cook_report = Path(selection.get('cook_report', ''))
    if cook_report.is_file():
        snapshot = json.loads(cook_report.read_text(encoding='utf-8-sig'))
        cooked = Path(snapshot.get('cooked_root', ''))
        if report_path == Path(snapshot.get('project_file', '')).resolve() \
                or (cooked.is_absolute() and report_path.is_relative_to(cooked.resolve())):
            raise BridgeError('打包报告不能覆盖 UE 工程或烘焙快照文件。')
    result = {'schema_version': 1, 'success': False, 'phase': 'validation', 'errors': [], 'outputs': []}
    write_json(report_path, result)
    try:
        staging = stage_selection(selection_path)
        result.update(job_id=staging['job_id'], cook_id=staging['cook_id'],
                      project_file=staging['project_file'], run_dir=staging['run_dir'],
                      selected_assets=staging['selected_assets'], manifest_sha256=staging['manifest_sha256'],
                      staging_report=str(Path(staging['run_dir']) / 'staging_report.json'))
        _selection_package_preflight(staging, packager_source_dir, packager_tools_dir,
                                     output_dir, mod_name, adapter_path)
        result['phase'] = 'export'
        if not selection.get('export_directory'):
            raise BridgeError('请选择烘焙资产导出目录（xg/工程名/Content/Characters）。')
        result.update(export_selection(staging, selection['export_directory']))
        result['phase'] = 'packager'
        outputs, adapter_report = _package_staging(staging, packager_source_dir, packager_tools_dir,
                                                   output_dir, mod_name, adapter_path, timeout)
        result.update(success=True, phase='complete', outputs=outputs, packager_report=adapter_report)
    except Exception as error:
        result['errors'].append(str(error))
    write_json(report_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--ue-report", required=True)
    parser.add_argument("--engine-dir")
    parser.add_argument("--packager-source-dir")
    parser.add_argument("--packager-tools-dir")
    parser.add_argument("--adapter-path")
    parser.add_argument("--output-dir")
    parser.add_argument("--mod-name")
    parser.add_argument("--cooked-root", help="Use existing cooked files instead of running cook")
    parser.add_argument("--report-path")
    arguments = vars(parser.parse_args(argv))
    arguments["manifest_path"] = arguments.pop("manifest")
    arguments["ue_report_path"] = arguments.pop("ue_report")
    arguments["run_cook"] = not bool(arguments.get("cooked_root"))
    report = package_job(**arguments)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
