using System.Security.Cryptography;
using System.Text.Json;
using NteModPackager.Models;
using NteModPackager.Services;

if (args.Length != 4 || args[0] != "--job" || args[2] != "--report")
{
    Console.Error.WriteLine("Usage: NteBridge.Packager --job request.json --report result.json");
    return 2;
}

string reportPath = Path.GetFullPath(args[3]);
Dictionary<string, object?> report = new() { ["schema_version"] = 1, ["success"] = false };
using CancellationTokenSource cancellation = new();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cancellation.Cancel(); };
int exitCode = 1;
try
{
    using JsonDocument document = JsonDocument.Parse(await File.ReadAllTextAsync(args[1]));
    JsonElement job = document.RootElement;
    foreach (string key in new[] { "job_id", "manifest_sha256", "run_id" })
        report[key] = job.GetProperty(key).GetString();
    string source = Path.GetFullPath(Required(job, "source_dir"));
    string output = Path.GetFullPath(Required(job, "output_dir"));
    string name = Required(job, "mod_name");
    if (job.GetProperty("schema_version").GetInt32() != 1)
        throw new InvalidDataException("Unsupported request schema.");
    if (source.Equals(output, StringComparison.OrdinalIgnoreCase) || output.StartsWith(source + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        throw new InvalidDataException("Output directory must be outside staging.");
    HashSet<string> expected = new(StringComparer.OrdinalIgnoreCase);
    foreach (JsonElement file in job.GetProperty("files").EnumerateArray())
    {
        string relative = Required(file, "path").Replace('/', Path.DirectorySeparatorChar);
        if (Path.IsPathRooted(relative) || relative.Split(Path.DirectorySeparatorChar).Any(p => p is ".." or "."))
            throw new InvalidDataException("Unsafe staged path.");
        string path = Path.GetFullPath(Path.Combine(source, relative));
        if (!path.StartsWith(source + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) || !expected.Add(path))
            throw new InvalidDataException("Duplicate or escaping staged path.");
        if (!new[] { ".uasset", ".uexp", ".ubulk", ".uptnl" }.Contains(Path.GetExtension(path), StringComparer.OrdinalIgnoreCase))
            throw new InvalidDataException("Unsupported staged file type.");
        RejectLinks(source, path);
        if (!File.Exists(path) || new FileInfo(path).Length != file.GetProperty("bytes").GetInt64() ||
            !string.Equals(await Hash(path), Required(file, "sha256"), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Staged file changed or is missing: " + relative);
    }
    // Do not let an unlisted file/junction join the build after preview.
    HashSet<string> actual = new(StringComparer.OrdinalIgnoreCase);
    foreach (string entry in Directory.EnumerateFileSystemEntries(source, "*", SearchOption.AllDirectories))
    {
        if ((File.GetAttributes(entry) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Links are not allowed in staging.");
        if (File.Exists(entry)) actual.Add(Path.GetFullPath(entry));
    }
    if (expected.Count == 0 || !expected.SetEquals(actual))
        throw new InvalidDataException("Staged inventory does not match the request.");
    EmbeddedToolManager.ToolsDirectory = Required(job, "tools_dir");
    BuildOptions options = new(source, output, name);
    if (BuildService.ExistingOutputFiles(options).Count != 0)
        throw new InvalidDataException("Output files already exist; use a new directory or name.");
    BuildResult result = await new BuildService().BuildAsync(options, new ImmediateProgress(), cancellation.Token);
    List<object> outputs = [];
    foreach (string path in new[] { result.PakPath, result.UtocPath, result.UcasPath })
    {
        if (!File.Exists(path) || new FileInfo(path).Length == 0)
            throw new InvalidDataException("Expected output is missing or empty: " + path);
        outputs.Add(new { path = Path.GetFullPath(path), bytes = new FileInfo(path).Length, sha256 = await Hash(path) });
    }
    report["outputs"] = outputs;
    report["success"] = true;
    report["errors"] = Array.Empty<string>();
    exitCode = 0;
}
catch (Exception error)
{
    report["errors"] = new[] { error.Message };
    Console.Error.WriteLine(error);
}
Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);
string temporary = reportPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
await File.WriteAllTextAsync(temporary, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
File.Move(temporary, reportPath, overwrite: true);
return exitCode;

static string Required(JsonElement value, string key) => value.GetProperty(key).GetString() is string text && !string.IsNullOrWhiteSpace(text)
    ? text : throw new InvalidDataException("Missing field: " + key);

static async Task<string> Hash(string path)
{
    await using FileStream stream = File.OpenRead(path);
    return Convert.ToHexString(await SHA256.HashDataAsync(stream)).ToLowerInvariant();
}

static void RejectLinks(string root, string path)
{
    for (string? current = path; current != null; current = Path.GetDirectoryName(current))
    {
        if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Links are not allowed in staging.");
        if (current.Equals(root, StringComparison.OrdinalIgnoreCase)) return;
    }
}

sealed class ImmediateProgress : IProgress<BuildProgress>
{
    public void Report(BuildProgress value) => Console.WriteLine(JsonSerializer.Serialize(new
    { stage = value.Stage.ToString(), percent = value.Percent, status = value.Status, message = value.LogLine }));
}
