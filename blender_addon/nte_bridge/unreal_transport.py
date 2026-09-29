"""Project-qualified local UE transport; callable from a Blender worker process."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from .core import BridgeError, load_manifest, write_json

DEFAULT_ENGINE = "D:/ue/UE_5.6"
REQUIRED_COMMANDLET_PLUGINS = (
    "PythonScriptPlugin", "EditorScriptingUtilities", "GeometryScripting", "ControlRig",
)


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path)))).replace("\\", "/")


def _engine_root(engine_dir):
    root = Path(engine_dir).resolve()
    return root if root.name.lower() == "engine" else root / "Engine"


def _remote_module(engine_dir):
    source = (_engine_root(engine_dir) / "Plugins/Experimental/PythonScriptPlugin/Content/Python/remote_execution.py")
    if not source.is_file():
        raise BridgeError("Unreal Python remote execution module was not found: " + str(source))
    spec = importlib.util.spec_from_file_location("_nte_unreal_remote_execution", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def matching_nodes(nodes, project_file):
    """Discovery is only a filter; the live project path is also probed before execution."""
    project = Path(project_file).resolve()
    return [node for node in nodes
            if node.get("project_root") and node.get("project_name")
            and _canonical(node["project_root"]) == _canonical(project.parent)
            and node["project_name"].casefold() == project.stem.casefold()]


def validate_report(report, manifest_path, manifest=None):
    source = Path(manifest_path)
    manifest = manifest or load_manifest(source)
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise BridgeError("Unreal report has an unsupported format")
    if report.get("job_id") != manifest["job_id"]:
        raise BridgeError("Unreal report belongs to another job")
    if report.get("manifest_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
        raise BridgeError("Unreal report does not match the current manifest bytes")
    if _canonical(report.get("project_file", "")) != _canonical(manifest["project_file"]):
        raise BridgeError("Unreal report belongs to another project")
    if not isinstance(report.get("errors"), list) or not isinstance(report.get("assets"), list):
        raise BridgeError("Unreal report is incomplete")
    return report


def _invocation_files(manifest_path):
    source = Path(manifest_path).resolve()
    invocation = uuid.uuid4().hex
    report = source.with_name("ue_invocation.json")
    runner = source.with_name("ue_run.py")
    # One invocation result is sufficient; its token, not a filename history,
    # distinguishes a fresh response from a delayed or stale one.
    write_json(report, {'success': False, 'stage': 'pending', 'invocation_id': invocation,
                       'project_file': load_manifest(source)['project_file'], 'errors': ['UE task has not completed']})
    addon_root = Path(__file__).resolve().parent.parent
    runner.write_text(
        "import sys\n"
        + "for _nte_name in list(sys.modules):\n"
        + "    if _nte_name == 'nte_bridge' or _nte_name.startswith('nte_bridge.'):\n"
        + "        del sys.modules[_nte_name]\n"
        + "sys.path.insert(0, " + repr(str(addon_root)) + ")\n"
        + "from nte_bridge.unreal_receiver import run_job\n"
        + "result = run_job(" + repr(str(source)) + ", " + repr(str(report)) + ", invocation_id=" + repr(invocation) + ")\n"
        + "print('NTE_BRIDGE_RESULT=' + str(result['success']))\n",
        encoding="utf-8")
    return source, report, runner


def _finish(source, report_file, manifest, invocation_id=None):
    if not report_file.is_file():
        raise BridgeError("Unreal did not write this invocation's report; inspect its log")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    if invocation_id and report.get('invocation_id') != invocation_id:
        raise BridgeError('UE 返回了过期的同步报告，请重新发送。')
    report = validate_report(report, source, manifest)
    write_json(source.with_name("ue_report.json"), report)
    return report


def _transport_status(source, manifest, message):
    """Invalidate an earlier success before a new attempt, including connection failures."""
    write_json(source.with_name("ue_report.json"), {
        "schema_version": 1, "job_id": manifest["job_id"], "project_file": manifest["project_file"],
        "manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "success": False,
        "assets": [], "slot_map": {}, "morph_targets": [], "features_applied": False,
        "warnings": [], "errors": [message], "stage": "transport"})


def send_job(manifest_path, engine_dir=DEFAULT_ENGINE, timeout=180):
    """Send to the single matching local editor. Does not enable remote execution settings.

    If no editor is discoverable, raise with guidance rather than silently launching a second
    process against a potentially open project. Explicit run_commandlet_job is the offline route.
    """
    source = Path(manifest_path).resolve()
    manifest = load_manifest(source)
    _transport_status(source, manifest, "Waiting for the target editor; asset sync has not completed")
    remote_module = _remote_module(engine_dir)
    config = remote_module.RemoteExecutionConfig()
    config.multicast_bind_address = "127.0.0.1"
    config.multicast_ttl = 0
    # A distinct local ephemeral port permits independent jobs without taking another tool's port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    config.command_endpoint = ("127.0.0.1", port)
    remote = remote_module.RemoteExecution(config)
    deadline = time.monotonic() + timeout
    try:
        remote.start()
        discovery_deadline = min(deadline, time.monotonic() + 6)
        while time.monotonic() < discovery_deadline:
            time.sleep(0.15)
        candidates = matching_nodes(remote.remote_nodes, manifest["project_file"])
        if len(candidates) != 1:
            if candidates:
                raise BridgeError("Multiple editors have the target project open; close the duplicate before sending")
            raise BridgeError("Target editor was not discovered. Open the exact project and enable Python Remote Execution on localhost, or explicitly use the offline commandlet after closing the editor")
        remote.open_command_connection(candidates[0]["node_id"])
        # UE's bundled module has no public timeout argument; constrain its connected socket.
        channel = remote._command_connection._command_channel_socket
        channel.settimeout(max(1, deadline - time.monotonic()))
        probe = remote.run_command(
            "__import__('unreal').Paths.convert_relative_path_to_full(__import__('unreal').Paths.get_project_file_path())",
            exec_mode=remote_module.MODE_EVAL_STATEMENT)
        if not probe.get("success"):
            raise BridgeError("Could not verify the connected Unreal project")
        value = probe.get("result", "")
        try:
            project = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            project = value
        if not isinstance(project, str) or _canonical(project) != _canonical(manifest["project_file"]):
            raise BridgeError("Connected editor failed exact project verification")
        source, report_file, runner = _invocation_files(source)
        invocation_id = json.loads(report_file.read_text(encoding='utf-8'))['invocation_id']
        channel.settimeout(max(1, deadline - time.monotonic()))
        script = "exec(compile(open(%r, encoding='utf-8').read(), %r, 'exec'))" % (str(runner), str(runner))
        response = remote.run_command(script, exec_mode=remote_module.MODE_EXEC_STATEMENT)
        if not response.get("success") and (not report_file.is_file()
                or json.loads(report_file.read_text(encoding='utf-8')).get('stage') == 'pending'):
            write_json(report_file, {'success': False, 'stage': 'transport-failed',
                                    'invocation_id': invocation_id, 'errors': [str(response.get('result', ''))]})
            raise BridgeError("Unreal receiver failed before writing a report: " + str(response.get("result", "")))
        return _finish(source, report_file, manifest, invocation_id)
    except (TimeoutError, socket.timeout) as exc:
        message = "Unreal timed out. The import may still be running; inspect the job report before retrying"
        _transport_status(source, manifest, message)
        raise BridgeError(message) from exc
    except Exception as exc:
        _transport_status(source, manifest, str(exc))
        raise
    finally:
        remote.stop()


def commandlet_command(manifest_path, engine_dir=DEFAULT_ENGINE):
    """Return (argv, invocation report path). Writing a job helper never launches UE."""
    source = Path(manifest_path).resolve()
    manifest = load_manifest(source)
    editor = _engine_root(engine_dir) / "Binaries/Win64/UnrealEditor-Cmd.exe"
    if not editor.is_file():
        raise BridgeError("UnrealEditor-Cmd.exe was not found: " + str(editor))
    source, report_file, runner = _invocation_files(source)
    argv = [editor.as_posix(), Path(manifest["project_file"]).resolve().as_posix(), "-run=pythonscript",
            # These editor APIs are needed only by the bridge process. UE's
            # command-line override also works when disabled in the .uproject,
            # without rewriting the user's persistent plugin configuration.
            "-EnablePlugins=" + ",".join(REQUIRED_COMMANDLET_PLUGINS),
            "-script=" + runner.as_posix(), "-unattended", "-nop4", "-nosplash", "-nullrhi",
            "-NODEFAULTLOG", "-stdout", "-FullStdOutLogOutput"]
    return argv, report_file


def _project_editor_running(project_file):
    """Fail closed on Windows if process inspection itself fails."""
    if os.name != "nt":
        return False
    command = ("Get-CimInstance Win32_Process -Filter \"Name = 'UnrealEditor.exe' OR Name = 'UnrealEditor-Cmd.exe'\" "
               "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise BridgeError("Could not check whether Unreal already has the project open")
    processes = json.loads(result.stdout or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    target = _canonical(project_file).casefold()
    target_name = Path(project_file).name.casefold()
    for process in processes:
        line = process.get("CommandLine")
        if line is None:
            raise BridgeError("An Unreal process cannot be inspected; close it before offline sync")
        if target in line.replace("\\", "/").casefold():
            return True
        # Relative launch paths cannot be resolved against another process's CWD.
        # A matching project filename is conservatively treated as already open.
        if target_name in line.casefold():
            return True
    return False


def run_commandlet_job(manifest_path, engine_dir=DEFAULT_ENGINE, timeout=300):
    source = Path(manifest_path).resolve()
    manifest = load_manifest(source)
    _transport_status(source, manifest, "Starting offline Unreal sync; asset sync has not completed")
    if _project_editor_running(manifest["project_file"]):
        raise BridgeError("目标 UE 工程仍在编辑器中打开；请先关闭该工程再后台导入，或选择发送到已打开的 UE。")
    argv, report_file = commandlet_command(source, engine_dir)
    invocation_id = (json.loads(report_file.read_text(encoding='utf-8')).get('invocation_id')
                     if report_file.is_file() else None)
    log_path = source.with_name("ue_commandlet.log")
    from .progress import report_progress
    report_progress('启动 UE 后台并加载工程', log=log_path)
    with log_path.open("w", encoding="utf-8") as log:
        try:
            # UE FParse needs -script="a path" rather than "-script=a path".
            command = " ".join((arg.partition("=")[0] + "=" + subprocess.list2cmdline([arg.partition("=")[2]]))
                               if arg.startswith("-") and "=" in arg else subprocess.list2cmdline([arg])
                               for arg in argv) if os.name == "nt" else argv
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("Unreal commandlet timed out; inspect " + str(log_path)) from exc
    if not report_file.is_file() or json.loads(report_file.read_text(encoding='utf-8')).get('stage') == 'pending':
        message = ("UE 后台导入未生成报告（退出码 %d）。请检查引擎插件是否完整；日志：%s"
                   % (process.returncode, log_path))
        _transport_status(source, manifest, message)
        raise BridgeError(message)
    report = _finish(source, report_file, manifest, invocation_id)
    if process.returncode:
        report["success"] = False
        report["errors"].append("Unreal commandlet exited with code %d; see %s" % (process.returncode, log_path))
        write_json(source.with_name("ue_report.json"), report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        function = run_commandlet_job if args.offline else send_job
        report = function(args.manifest, args.engine, args.timeout)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["success"] else 1
    except Exception as exc:
        print(json.dumps({"success": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
