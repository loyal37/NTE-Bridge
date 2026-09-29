# External packager adapter

Builds the existing packager's `BuildService`, `ProcessRunner` and models as linked
source. `PackagerSourceDir` must point to the installed/source checkout; no game
assets, packager binaries, or external repository are copied into this project.

```powershell
dotnet publish tools/packager_cli/NteBridge.Packager.csproj -c Release -o artifacts/packager_cli '-p:PackagerSourceDir=D:/Neverness to Everness Mod Loader/cook/packager'
dotnet artifacts/packager_cli/NteBridge.Packager.dll --job request.json --report result.json
```

The Python packaging module produces the request from a fresh allow-listed staging
tree. The adapter checks the full file inventory and hashes before calling the
existing service. Installed `NteMorphTargetPatch.exe`, `retoc.exe`, and
`oo2core_9_win64.dll` remain in the explicitly configured `tools_dir`. Output must
not already exist. Service cancellation kills child processes; service verification
and rollback behavior are retained. Final reports bind job ID, manifest hash and
unique packaging run ID, and include the three output file hashes.

The service packages already-cooked textures. BC7/BC5 and sRGB settings are owned
by the UE import/cook step; this adapter does not transcode image compression.

The adapter is framework-dependent and requires .NET 8 Runtime. For the Blender
ZIP, bundle only `NteBridge.Packager.dll`, `.deps.json` and `.runtimeconfig.json`
under `nte_bridge/vendor/packager_cli/`; installed packager tools stay external.

Initial build provenance (2026-09-29): external NTE Mod Packager v2.5.0, repository
HEAD `09613bb`, source SHA256:

| Linked source | SHA256 |
| --- | --- |
| `Services/BuildService.cs` | `FCCA9F96AE326F4329ECB0100DB368CDEB3D9069A657172731BCAD4F6620497C` |
| `Services/ProcessRunner.cs` | `B55853BE957D44EF54646F4A54D017CB264F415E998C8517A0AD7B8B4CACBC30` |
| `Models/BuildModels.cs` | `77F44CAD256DC7277B6EE2563140B1B4192C6455DB3C18C5024AFC3F431CB9AE` |

The existing packager README and its current sample tree use `HT/Content/...`.
The bridge preserves the selected UE project basename (`HT.uproject` -> `HT`) as
the cooked mount prefix; it never invents another game project name. Import paths
remain canonical `/Game/...`. A differently named UE project therefore needs
explicit game compatibility validation before using its output in game.
