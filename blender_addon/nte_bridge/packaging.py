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
