# NTE Bridge v0.4.0 contract

Implementation target: Blender 4.5.7 / UE 5.6.1, Windows. One skeletal mesh and its armature per import job; since v0.4.0 the mesh may be joined from several separated Blender objects bound to that armature. Original files are not saved by export. Runtime feature generation is a later milestone: features may be compiled/previewed, but asset sync must explicitly report them as not generated and legacy manifest packaging must reject unapplied features. Independent folder cooking snapshots saved UE content, including features the user has created with UE tools; it does not generate Blender nodes.

All modules live in `blender_addon/nte_bridge/`. The package imports without bpy for normal Python tests. Addon registration imports Blender code lazily.

Character discovery reads FModel-style JSON exports under a user-selected source directory. Explicit Package/ObjectPath evidence establishes canonical asset paths. Mesh candidates retain independent Skeleton/PhysicsAsset and ordered material-slot references. Materials retain instance/parent/overlay references, parameters and texture metadata; references without local JSON remain explicitly unavailable rather than reconstructed. Original source JSON is neither modified nor bundled in releases. Discovery must not automatically select all original textures for replacement or replace a slot's original MaterialInterface with its root parent. Multiple mesh candidates and ambiguous material names require selection; successful existing manual mappings and stable part IDs survive rescans.

The primary Blender send operator performs fresh FBX export followed by UE sync as a single asynchronous operation, defaulting to offline commandlet import without requiring an interactive editor. It must not send a previous manifest when the new export fails. First application of a different discovered mesh enables missing reference placeholders; reapplying the same source preserves an explicit advanced opt-out. UE creates and saves assets at exact manifest package paths; game placeholders remain excluded from export_assets. Independent export/recent-job commands remain advanced operations. Installation paths may be auto-detected only from a matching project engine association and complete existing tools; ambiguous installs require manual configuration.

Since v0.2.2, scene `cache_root` is the single configurable working directory; `job_root` is derived as `<cache_root>/Jobs`. Since v0.3.2, one owned Jobs/current directory holds the latest manifest, source copies, logs and reports. Validate the new profile first, then replace this scratch tree. Hold the Jobs OS lock through export and UE sync. Reject surviving background workers and pending live-editor imports before resetting input files. Legacy UUID directories are no longer produced; unrelated existing directories are not swept. The external packager child receives task-owned TEMP/TMP/TMPDIR; parent environment is unchanged. Automatic cache selection tries a non-C UE project's `Saved/NTEBridgeCache`, then saved Blender file's sibling cache, then unpacked character folder's parent cache; no AppData/home/current-directory fallback. Unsaved Blender files require an absolute explicit cache or a valid non-C context. Legacy default locations migrate on file load; changing the cache invalidates selected history without moving or deleting files. Resend/package reject manifests outside the active Jobs tree before launching a process. Final Mod output remains separately selected.

Since v0.2.1, the offline UE process receives `-EnablePlugins=PythonScriptPlugin,EditorScriptingUtilities,GeometryScripting,ControlRig`. This is a process-only dependency override, including for plugins explicitly disabled in the project; the project descriptor is not rewritten. Live-editor sessions still require these APIs already loaded. The receiver verifies all required scripting APIs before any asset mutation, and missing startup reports include the process exit code and log path.

FBX export always disables animation (`bake_anim=False`) and leaf bones (`add_leaf_bones=False`), and uses face smoothing (`mesh_smooth_type='FACE'`). These existing export settings are the required character-mod preset, independent of the user's manual FBX dialog settings. Shape keys and all source bones remain preserved.

Visible UE `material_slot_name` follows the assigned material asset name, while `imported_material_slot_name` retains the unique `NTE_<part UUID>` import identity. Shared material references must not merge independent slots. Optional `textures[].origin` defaults to `mod` for existing manual replacement jobs; `preview` imports a texture but strictly excludes it from `export_assets`. Automatic Blender diffuse images still use the exact original game texture paths from character material references; preview origin controls packaging only, never redirects asset placement. Optional `material_previews` entries pair `material_path` with a declared BASE_COLOR `texture_path`; each material has at most one binding. Reference materials remain excluded from packaging. Preview nodes may be added to empty Materials or updated where bridge-owned; user-authored graphs and material instances are preserved. All preview source files still participate in import source hashing, and cooked dependency files are staged only when explicitly permitted by `export_assets`.

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

Texture roles: BASE_COLOR -> BC7/sRGB true; ID_TEX, LIGHT_MAP and MASK -> BC7/sRGB false; NORMAL -> Normalmap/BC5/sRGB false. Export list includes the mesh and explicitly replaced textures; Material/Skeleton/PhysicsAsset forbidden. Custom MaterialInstanceConstant can be added when implemented with origin=mod; game placeholders never inferred by name.

`core.py` (root agent) exposes `BridgeError(ValueError)`, `validate_manifest(dict, job_dir=None) -> dict` (returns same validated manifest), `load_manifest(path) -> dict`, `write_json(path, data)`, `texture_settings(role) -> dict` with compression='BC7'/'NORMALMAP', srgb bool; `resolve_source(job_dir, relative) -> pathlib.Path`; `compile_graph(graph, parts) -> list[feature]`.

## Blueprint (v0.4.0)

Graph format: `{id, nodes:[...], links:[{from_node, from_socket, to_node, to_socket}]}`; reroutes are resolved and frames ignored before compiling. Node types: OBJECT (`object_id`, `main`, ordered `parts`, `split` subset; outputs `all` = parts not split, plus one output per split part; one material input per part ID), MATERIAL (output `material`, links only to OBJECT material inputs), SWITCH (`comment`, `variable`, `key` in HT format, ordered `options` socket IDs, `initial_option`), GROUP and exactly one OUTPUT. `core.compile_blueprint(graph)` returns included parts per object node, the main node, `part_materials` (part -> material node or null) and features. A part reaches OUTPUT directly (always visible) or through exactly one switch option; nested switches, duplicate placement, unconnected split parts, loops, unknown sockets and an unconnected main object are rejected. Objects that do not reach OUTPUT are not exported.

Feature: `{id,type:'visibility_cycle',label,comment,variable,key,states:[{id,label,parts:[id]}],include_hidden_state,initial_state_id,panel}`. A state with empty `parts` is an explicit all-hidden option (LoyalTools empty input); a feature needs at least two states and at least one part. `include_hidden_state` remains accepted for older manifests. Keys conflict after HT normalization (`alt 6` == `Alt+6`); variables must be identifiers and unique. `core.ht_material_ids(feature, slot_map)` returns the HT `Material ID(s)` text and initial state: no empty state -> groups joined by `;`; one empty state -> the cycle is rotated so hide-all is last and groups are joined by `,` (a single group has no separator); more than one empty state is not representable.

Export: the main object's slots keep their order first; other included objects follow by node creation order. Every included object must be bound to the main armature, have no active non-armature modifiers, and match the main mesh's UV layer names and color attribute names (Blender joins these by name). The worker joins the isolated copies, restores the manifest slot order, and verifies shape-key union and UV count. Parts record `object`.

Materials: unlinked material inputs use the main part mapping or an unambiguous catalogue name match. Manifest `materials[]` lists `{id, kind:'existing', asset_path}` and `{id, kind:'new', asset_path, parent_path, textures:{parameter: texture_asset_path}}`. New-instance textures are declared manifest textures with origin `mod`, named from the sanitized file stem in the instance folder; the same file is imported once. New instances are export assets of type MaterialInstanceConstant; existing instances are only referenced. Instances never receive diffuse previews. The UE receiver requires existing instances to exist, and for new instances requires the parent in the project and every overridden parameter name on the parent's base material, all before mutation. It creates or updates only instances tagged `NTEBridge.MaterialInstanceOwner`, refuses other assets at the target path, clears and sets the declared texture overrides, reads them back and reports `material_instances`.

UE receiver: `run_job(manifest_path, report_path=None, invocation_id=None) -> dict`. Report fields `schema_version`, `job_id`, `success`, `project_file`, `assets`, `slot_map` (part ID -> int), `morph_targets`, `warnings`, `errors`, `features_applied` (false when any feature is pending). Write fresh report on every invocation incl errors. No success if slot/morph verification fails. Match full target project before writes. Do not assume arbitrary Python errors imply rollback; report partial changes.

UE helper and invocation report use fixed ue_run.py and ue_invocation.json paths. A pending report invalidates the old result before launch; invocation_id distinguishes responses even when the same manifest is resent. UI child commands also bind the expected job_id.

All bridge-launched UE processes use -NODEFAULTLOG with -stdout/-FullStdOutLogOutput; the bridge captures and overwrites fixed cache logs. This prevents UE's default dated log backups and avoids adding bridge logs to the project's Saved/Logs.

UE transport: `send_job(manifest_path, engine_dir, timeout=...) -> dict`; use verified project, no first-node selection. May offer commandlet helper when editor closed. Do not mutate project config silently or expose remote execution beyond localhost.

UE report additionally includes `manifest_sha256` (exact JSON bytes) and `source_sha256` (relative source_file -> SHA256 of every FBX/texture). Packaging checks both before cooking. Actual UV verification uses the built-in GeometryScripting plugin; hierarchy verification uses ControlRig. Both are required alongside PythonScriptPlugin and EditorScriptingUtilities. Reports include imported UV count, bounds and compression/sRGB readback.

`saved_asset_sha256` maps paths relative to the project's `Content/` directory to the exact saved `.uasset` and existing `.uexp/.ubulk/.uptnl` hashes for every export asset. Cook and staging recheck the complete set so an older job cannot package assets overwritten by a newer job. A different source manifest alone does not prove which version is currently stored in UE.

Legacy packaging module/API: validate manifest and successful same-job UE report; refuse nonempty unapplied features; stage exactly export_assets from cooked `<Project>/Content` with sidecars into a new job directory; invoke existing packager service via external-source .NET CLI adapter. This API remains compatible for existing scripts. Blender's primary packaging workflow uses the separate folder snapshot and explicit selection below.

## Independent character cooking and asset selection (v0.3.0)

Blender exposes two operations: cook a UE character folder, then open an asset selection dialog to export selected cooked assets and automatically package them. Default folder follows the current discovered character, including its mesh subfolders. The custom folder browser starts inside the selected project's Content tree, verifies the chosen disk directory is strictly below Content, and converts it to /Game automatically. Cooking requires saved UE assets and an existing directory; no recent bridge import is required. It refuses an open target project and leaves the project descriptor unchanged.

Since v0.3.2, Cooks/<short-project-name>_<full-project-path-hash> owns one shared cooked/Windows tree for all character scopes. Native UE -iterate with -SkipZenStore updates the loose-file sandbox in place; character scope only sets CookDir and the default asset-list filter. Unchanged previously cooked characters and dependencies remain. Native UE global cook-settings invalidation may still rebuild the sandbox. The bridge does not keep per-role trees or full pending/current/previous snapshots.

`cooking.prepare_cook_request(cache_root, request)` reserves the stable project directory before writing `cook_request.json`. It returns fixed request/report paths plus a one-use `request_token`. The worker passes that token to `cooking.cook_character(request_path, engine_dir, report_path=None, timeout=1800, request_token=None)`. A failed worker launch cancels its reservation; abandoned reservations expire. Existing direct CLI requests remain supported when no reservation is active. The UTF-8 request is:

```json
{
  "schema_version": 1,
  "project_file": "D:/ueproject/HT/HT.uproject",
  "character_folder": "/Game/Characters/Player/078_Nitsa",
  "excluded_assets": ["/Game/Characters/Player/078_Nitsa/Ml_player_078_Nitsa_01"]
}
```

The UE AssetRegistry supplies true asset classes. A fresh cook processes the entire directory recursively; imported manifests do not limit the inventory. The report binds the request fingerprint, project, folder and cook ID to an asset inventory with package paths, classes, selectable status/reason, byte totals, file counts and exact sidecar SHA256/size records. All assets initially have `default_selected=false`. Materials, Skeletons, PhysicsAssets, unsupported/editor-only classes and explicitly identified original reference materials cannot be selected. Existing custom MaterialInstanceConstant, Blueprint and AnimBlueprint assets can be selected. Automatic diffuse textures are ordinary Texture2D assets in this inventory: they remain unchecked until the user chooses them.

`packaging.package_selection(selection_path, ...)` receives:

```json
{
  "schema_version": 1,
  "cook_report": "D:/NTEBridgeCache/Cooks/example/cook_report.json",
  "cook_report_sha256": "<SHA256 of the exact report bytes>",
  "selected_assets": ["/Game/Characters/Player/078_Nitsa/SM_Nitsa"],
  "export_directory": "D:/Neverness to Everness Mod Loader/cook/packager/xg/HT/Content/Characters"
}
```

Selection is resolved against that completed cook snapshot, not mutable live UE content or arbitrary loose files. Empty selections, stale reports, changed selected cooked files, missing required `.uasset`, unsafe paths and forbidden assets fail before invoking the external packager. Each selected package carries its verified `.uasset/.uexp/.ubulk/.uptnl` files; unchecked dependencies never join the package automatically. Changing Blender's cache/project/folder invalidates the displayed report. Remembered package names are restored only against a valid matching report and intersected with its packable assets.

Stable caches carry schema-2 project ownership markers. Only owned selections and temp scratch trees may be cleared; the bridge never recursively deletes its cooked sandbox. Fixed cook_request.json, catalog files, logs and cook_report.json replace previous metadata. OS locks serialize all roles' cooking and selected packaging for the project. A separate cook state commits each successful cook_id; report SHA plus that identity reject old generations even when bytes are unchanged. In-place cook failure invalidates old packaging reports because some files may already be updated. Preflight failure before the sandbox is touched preserves its previous successful identity. Legacy schema-1 reports remain readable but new cooks use the project layout.

Selections use one selections/selection.json and one packaging/stage tree. Repeated selection replaces both, including obsolete package sidecars. The UI passes selection_sha256 to the worker to reject a selection overwritten during process startup. Staging has an ownership marker and is reset before copying only checked assets.

Since v0.3.1 the persistent export directory holds the selected character directly: scope `/Game/Characters/Player/A` exports `SM_A` as `<export_directory>/A/SM_A.uasset`. Internal character subdirectories survive. Paths outside the selected character scope keep their original Content-relative location. The complete destination map is checked for collisions before overwrites, and the same mapping handles obsolete selected sidecars. Records retain canonical source `path` plus actual `export_relative_path`/`exported_path`. Since v0.3.3, after validating all sources/destinations, the current role export folder is deleted and rebuilt from the current selection. Other roles and unchecked shared dependencies remain; selected shared packages only replace their own files and obsolete sidecars. Reject redirected export roots/role paths, source Content overlap, broad Characters/Player scopes and final Mod outputs inside the role that will be deleted. The xg reset is direct; an export/copy failure may leave a partial xg role for the next retry. This is separate from transactional final Mod publication. Packaging independently consumes verified selected files at their original UE package paths, so the user-facing folder arrangement does not change game references or admit old persistent files. Final output is separately configured. The adapter builds into reset staging/new-output, verifies all three files, then Python replaces existing same-name .pak/.utoc/.ucas under an output lock. A bounded same-volume transaction restores the old set on I/O failure and removes temporary files after success. Adapter failure never touches the previous final set. The optional Python API output default is <project>/Saved/NTEBridgeMods, outside reusable scratch; Blender requires an explicit output directory. Child TEMP/TMP/TMPDIR stay inside the active cache. Reusing a cook for another selection does not invoke UE again.


## v0.3.3 progress, selection state and performance

Normal selected packaging validates report structure, SHA/generation identity, selected sidecar completeness and copied selected-file hashes. It does not rehash every other cooked package. Full verification remains available through load_cook_report(verify_files=True). Incremental cooking keeps one cook_hashes.json beside the sandbox: size, nanosecond modification/creation times and file identity guard reuse; changed/new files are hashed and removed files pruned. Every selected payload is hashed again before packaging, even if the cook inventory reused its checksum.

Small Settings/<profile-id>.json records are independent of reset Jobs/Cooks workspaces. Identity includes the full UE project and saved .blend path (or the unsaved session); role records preserve output directory, Mod name, export directory, tool path, filters and checked package names. Property edits save them atomically without saving the .blend. Save As writes the current choices under the new profile. Reopening restores the latest valid cook when available; stale/failed cook reports never become eligible just because choices were remembered.

The native asset dialog lives in a bridge-owned auxiliary Blender window. Its export button starts a worker without closing the dialog; failure keeps it available, and success closes only the owned window. Workers attach to the originating Blender window so manual closing of the asset window does not interrupt writes. Worker progress.json is replaced in place with an invocation token; UI polls only its own timer, displays phase/elapsed time, real file counts, UE cook counts and external packager percent. Unknown-duration phases remain indeterminate. Sending still incurs UE startup and FBX/texture import; cooking still incurs native shader/asset work.
