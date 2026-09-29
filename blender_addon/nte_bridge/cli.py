"""Worker entry point; callable with Unreal's bundled Python interpreter."""
import argparse
import json
from pathlib import Path
import sys
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nte_bridge.core import BridgeError, load_manifest, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="NTE Bridge task worker")
    sub = parser.add_subparsers(dest="command", required=True)
    validation = sub.add_parser("validate")
    validation.add_argument("--manifest", required=True)
    validation.add_argument("--report")
    sync = sub.add_parser("sync")
    sync.add_argument("--manifest", required=True)
    sync.add_argument("--engine-dir", required=True)
    sync.add_argument("--report")
    sync.add_argument("--mode", choices=("remote", "commandlet"), default="remote")
    sync.add_argument("--timeout", type=int, default=300)
    package = sub.add_parser("package")
    package.add_argument("--manifest", required=True)
    package.add_argument("--engine-dir", required=True)
    package.add_argument("--packager-source", required=True)
    package.add_argument("--packager-tools")
    package.add_argument("--output-dir", required=True)
    package.add_argument("--mod-name", required=True)
    package.add_argument("--ue-report")
    package.add_argument("--report")
    package.add_argument("--cooked-root")
    package.add_argument("--skip-cook", action="store_true")
    package.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    report_path = Path(args.report) if args.report else None
    result = {"success": False, "errors": [], "warnings": []}
    try:
        if report_path and report_path.resolve() == Path(args.manifest).resolve():
            report_path = None
            raise BridgeError("报告路径不能覆盖输入清单")
        manifest = load_manifest(args.manifest)
        if report_path:
            from nte_bridge.core import resolve_source
            input_paths = {Path(args.manifest).resolve(), Path(manifest["project_file"]).resolve()}
            job_dir = Path(args.manifest).resolve().parent
            input_paths.add(resolve_source(job_dir, manifest["mesh"]["source_file"]))
            input_paths.update(resolve_source(job_dir, t["source_file"]) for t in manifest.get("textures", []))
            if report_path.resolve() in input_paths:
                report_path = None
                raise BridgeError("报告路径不能覆盖清单、UE 工程或任务源文件")
        result["job_id"] = manifest["job_id"]
        if args.command == "validate":
            result.update(success=True, message="清单校验通过", features=len(manifest.get("features", [])))
        elif args.command == "sync":
            from nte_bridge import unreal_transport
            if args.mode == "commandlet":
                result = unreal_transport.run_commandlet_job(args.manifest, args.engine_dir, timeout=args.timeout)
            else:
                result = unreal_transport.send_job(args.manifest, args.engine_dir, timeout=args.timeout)
        elif args.command == "package":
            from nte_bridge.packaging import package_job
            result = package_job(
                args.manifest,
                args.ue_report or str(Path(args.manifest).parent / "ue_report.json"),
                args.engine_dir, args.packager_source,
                packager_tools_dir=args.packager_tools, output_dir=args.output_dir,
                mod_name=args.mod_name, cooked_root=args.cooked_root,
                run_cook=not args.skip_cook, timeout=args.timeout,
            )
    except Exception as exc:
        result.update(success=False, errors=[str(exc)], error_type=type(exc).__name__)
        if not isinstance(exc, BridgeError):
            result["traceback"] = traceback.format_exc()
    if report_path:
        write_json(report_path, result)
    # Escaped console JSON also works on older Windows terminal encodings.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
