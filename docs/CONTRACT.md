# NTE Bridge v0.2 contract

Implementation target: Blender 4.5.7 / UE 5.6.1, Windows. One skeletal mesh and its armature per job. Original files are not saved by export. Runtime feature generation is a later milestone: features may be compiled/previewed, but asset sync must explicitly report them as not generated and packaging must reject unapplied features.

All modules live in `blender_addon/nte_bridge/`. The package imports without bpy for normal Python tests. Addon registration imports Blender code lazily.

Character discovery reads FModel-style JSON exports under a user-selected source directory. Explicit Package/ObjectPath evidence establishes canonical asset paths. Mesh candidates retain independent Skeleton/PhysicsAsset and ordered material-slot references. Materials retain instance/parent/overlay references, parameters and texture metadata; references without local JSON remain explicitly unavailable rather than reconstructed. Original source JSON is neither modified nor bundled in releases. Discovery must not automatically select all original textures for replacement or replace a slot's original MaterialInterface with its root parent. Multiple mesh candidates and ambiguous material names require selection; successful existing manual mappings and stable part IDs survive rescans.

The primary Blender send operator performs fresh FBX export followed by UE sync as a single asynchronous operation, defaulting to offline commandlet import without requiring an interactive editor. It must not send a previous manifest when the new export fails. First application of a different discovered mesh enables missing reference placeholders; reapplying the same source preserves an explicit advanced opt-out. UE creates and saves assets at exact manifest package paths; game placeholders remain excluded from export_assets. Independent export/existing-job commands remain advanced operations. Installation paths may be auto-detected only from a matching project engine association and complete existing tools; ambiguous installs require manual configuration. Unsaved Blender files use an absolute per-user cache directory for jobs.

Since v0.2.1, the offline UE process receives `-EnablePlugins=PythonScriptPlugin,EditorScriptingUtilities,GeometryScripting,ControlRig`. This is a process-only dependency override, including for plugins explicitly disabled in the project; the project descriptor is not rewritten. Live-editor sessions still require these APIs already loaded. The receiver verifies all required scripting APIs before any asset mutation, and missing startup reports include the process exit code and log path.

FBX export always disables animation (`bake_anim=False`) and leaf bones (`add_leaf_bones=False`), and uses face smoothing (`mesh_smooth_type='FACE'`). These existing export settings are the required character-mod preset, independent of the user's manual FBX dialog settings. Shape keys and all source bones remain preserved.

Manifest is UTF-8 JSON, schema_version=1. Object references use canonical `/Game/Folder/Asset` package paths (not `.Asset` suffix). A job directory contains `manifest.json`, `meshes/mesh.fbx`, optional `textures/*`, and generated reports. All source_file values are relative, resolved inside the job directory; reject escapes. Never ship source paths or user content in the addon distribution.

```json
{
  "schema_version": 1,
  "job_id": "uuid",
  "graph_id": "uuid",
  "character_id": "uuid",
  "project_file": "D:/.../HT.uproject",
  "create_placeholders": false,
  "mesh": {
    "id": "uuid",
    "source_file": "meshes/mesh.fbx",
    "asset_path": "/Game/Characters/Test/Test",
    "skeleton_path": "/Game/Characters/Test/Test_Skeleton",
    "physics_asset_path": "",
    "expected": {"bones": [{"name": "root", "parent": ""}], "shape_keys": ["Smile"], "uv_layers": 1}
  },
  "parts": [{"id": "uuid", "slot_key": "NTE_<uuidhex>", "source_slot": 0, "display_name": "Coat", "material_path": "/Game/Characters/Test/M_Test"}],
  "textures": [{"id": "uuid", "source_file": "textures/tex.png", "asset_path": "/Game/Characters/Test/T_Test", "role": "BASE_COLOR"}],
  "features": [],
  "export_assets": [{"asset_path": "/Game/Characters/Test/Test", "asset_type": "SkeletalMesh", "origin": "mod"}]
}
```

Texture roles: BASE_COLOR -> BC7/sRGB true; ID_TEX and LIGHT_MAP -> BC7/sRGB false; NORMAL -> Normalmap/BC5/sRGB false. Export list includes the mesh and explicitly replaced textures; Material/Skeleton/PhysicsAsset forbidden. Custom MaterialInstanceConstant can be added when implemented with origin=mod; game placeholders never inferred by name.

`core.py` (root agent) exposes `BridgeError(ValueError)`, `validate_manifest(dict, job_dir=None) -> dict` (returns same validated manifest), `load_manifest(path) -> dict`, `write_json(path, data)`, `texture_settings(role) -> dict` with compression='BC7'/'NORMALMAP', srgb bool; `resolve_source(job_dir, relative) -> pathlib.Path`; `compile_graph(graph, parts) -> list[feature]`.

Graph format: `{id, nodes:[{id,type, ...}], links:[{from_node,to_node,to_socket}]}`. Types PART (`part_id`), GROUP (`label`), CYCLE (`label`,`key`,`include_hidden`,`initial_state_id`,`state_ids`), OUTPUT. GROUP input socket identifies member input; CYCLE to_socket is state ID. CYCLE.state_ids is ordered stable state IDs. GROUP may collect PART nodes; CYCLE states collect GROUP or PART; OUTPUT collects CYCLE. Only output-reachable features compile. Cycle dependencies and duplicate IDs/repeated or missing parts rejected. Feature: `{id,type:'visibility_cycle',label,key,states:[{id,label,parts:[id]}],include_hidden_state,initial_state_id,panel:{enabled,label,order}}`. Empty features valid for original-model workflow.

UE receiver: `run_job(manifest_path, report_path=None) -> dict`. Report fields `schema_version`, `job_id`, `success`, `project_file`, `assets`, `slot_map` (part ID -> int), `morph_targets`, `warnings`, `errors`, `features_applied` (false when any feature is pending). Write fresh report on every invocation incl errors. No success if slot/morph verification fails. Match full target project before writes. Do not assume arbitrary Python errors imply rollback; report partial changes.

UE transport: `send_job(manifest_path, engine_dir, timeout=...) -> dict`; use verified project, no first-node selection. May offer commandlet helper when editor closed. Do not mutate project config silently or expose remote execution beyond localhost.

UE report additionally includes `manifest_sha256` (exact JSON bytes) and `source_sha256` (relative source_file -> SHA256 of every FBX/texture). Packaging checks both before cooking. Actual UV verification uses the built-in GeometryScripting plugin; hierarchy verification uses ControlRig. Both are required alongside PythonScriptPlugin and EditorScriptingUtilities. Reports include imported UV count, bounds and compression/sRGB readback.

`saved_asset_sha256` maps paths relative to the project's `Content/` directory to the exact saved `.uasset` and existing `.uexp/.ubulk/.uptnl` hashes for every export asset. Cook and staging recheck the complete set so an older job cannot package assets overwritten by a newer job. A different source manifest alone does not prove which version is currently stored in UE.

Packaging module: validate manifest and successful same-job UE report; refuse nonempty unapplied features; stage exactly export_assets from cooked `<Project>/Content` with sidecars into a new job directory; invoke existing packager service via external-source .NET CLI adapter. Packaging UI may run worker process; do not block Blender drawing with long cook. Existing editor needs saved imported assets before cook. Reports must distinguish cook, staging and packager failures.
