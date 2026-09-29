// Adapter for the existing BuildService. The user's installed binaries remain external.
namespace NteModPackager.Services;

public sealed record ToolPaths(string DirectoryPath, string MorphPatcher, string Retoc);

public sealed class EmbeddedToolManager
{
    public static string ToolsDirectory { get; set; } = "";

    public Task<ToolPaths> EnsureToolsAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string root = Path.GetFullPath(ToolsDirectory);
        foreach (string name in new[] { "NteMorphTargetPatch.exe", "retoc.exe", "oo2core_9_win64.dll" })
        {
            if (!File.Exists(Path.Combine(root, name)))
                throw new FileNotFoundException("Installed packager tool is missing: " + name);
        }
        return Task.FromResult(new ToolPaths(root, Path.Combine(root, "NteMorphTargetPatch.exe"), Path.Combine(root, "retoc.exe")));
    }
}
