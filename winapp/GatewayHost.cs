using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace FiberVoice;

internal sealed class GatewayHost : IDisposable
{
    internal static readonly string DataDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Fiber Voice");
    internal string LogPath => Path.Combine(DataDirectory, "gateway.log");
    internal static string? BrowserInstallerPath { get; private set; }
    private Process? process;
    private readonly object logLock = new();
    private string? readyFile;
    internal string? Origin { get; private set; }

    private static string ExtractPayload()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("FiberVoice.Gateway.zip")
            ?? throw new InvalidOperationException("The calling engine is missing. Rebuild using winapp/build.ps1.");
        var digest = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        var cache = Path.Combine(DataDirectory, "Engine");
        Directory.CreateDirectory(cache);
        var destination = Path.Combine(cache, digest);
        if (File.Exists(Path.Combine(destination, ".complete"))) return destination;
        var temporary = destination + "." + Guid.NewGuid().ToString("N");
        try
        {
            stream.Position = 0;
            using var zip = new ZipArchive(stream, ZipArchiveMode.Read);
            zip.ExtractToDirectory(temporary);
            File.WriteAllText(Path.Combine(temporary, ".complete"), digest);
            if (Directory.Exists(destination)) Directory.Delete(destination, true);
            Directory.Move(temporary, destination);
        }
        finally { if (Directory.Exists(temporary)) Directory.Delete(temporary, true); }
        return destination;
    }

    internal async Task<string> StartAsync()
    {
        if (process is { HasExited: false } && Origin != null) return Origin;
        await StopAsync();
        var engine = await Task.Run(ExtractPayload);
        BrowserInstallerPath = Path.Combine(engine, "MicrosoftEdgeWebview2Setup.exe");
        Directory.CreateDirectory(DataDirectory);
        var runtime = Path.Combine(DataDirectory, "Runtime");
        Directory.CreateDirectory(runtime);
        readyFile = Path.Combine(DataDirectory, "ready-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(LogPath, "Starting Fiber Voice calling engine\n");
        var start = new ProcessStartInfo(Path.Combine(engine, "python", "python.exe"))
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            WorkingDirectory = Path.Combine(engine, "gateway")
        };
        start.ArgumentList.Add("-u");
        start.ArgumentList.Add(Path.Combine(engine, "gateway", "desktop.py"));
        start.Environment.Remove("PYTHONHOME");
        start.Environment.Remove("PYTHONPATH");
        start.Environment.Remove("GATEWAY_PARENT_PID");
        start.Environment["GATEWAY_RUNTIME"] = runtime;
        start.Environment["GATEWAY_LIB_DIR"] = Path.Combine(engine, "codecs");
        start.Environment["GATEWAY_READY_FILE"] = readyFile;
        process = new Process { StartInfo = start };
        process.OutputDataReceived += (_, e) => AppendLog(e.Data);
        process.ErrorDataReceived += (_, e) => AppendLog(e.Data);
        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        for (var i = 0; i < 300; i++)
        {
            if (process.HasExited) throw new InvalidOperationException("The calling engine could not start. Choose Show gateway log for details.");
            if (File.Exists(readyFile))
            {
                using var json = JsonDocument.Parse(await File.ReadAllTextAsync(readyFile));
                var port = json.RootElement.GetProperty("port").GetInt32();
                if (port is < 1 or > 65535) throw new InvalidOperationException("Invalid gateway port.");
                Origin = $"http://127.0.0.1:{port}/";
                File.Delete(readyFile);
                return Origin;
            }
            await Task.Delay(100);
        }
        await StopAsync();
        throw new TimeoutException("The calling engine took too long to start. Choose Retry or Show gateway log.");
    }

    private void AppendLog(string? message)
    {
        if (message == null) return;
        lock (logLock)
        {
            try
            {
                if (new FileInfo(LogPath).Length > 2_000_000) File.WriteAllText(LogPath, "Log rotated\n");
                File.AppendAllText(LogPath, message + Environment.NewLine);
            }
            catch (IOException) { }
        }
    }

    internal async Task StopAsync()
    {
        Origin = null;
        if (process != null)
        {
            if (!process.HasExited)
            {
                process.StandardInput.Close();
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(25));
                try { await process.WaitForExitAsync(timeout.Token); }
                catch (OperationCanceledException) { if (!process.HasExited) process.Kill(entireProcessTree: true); }
            }
            process.Dispose();
            process = null;
        }
        if (readyFile != null && File.Exists(readyFile)) File.Delete(readyFile);
    }

    public void Dispose()
    {
        if (process is { HasExited: false })
        {
            process.StandardInput.Close();
            if (!process.WaitForExit(2000)) process.Kill(entireProcessTree: true);
        }
        process?.Dispose();
    }
}
