using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System.Diagnostics;
using System.Text.Json;

namespace FiberVoice;

internal static class Program
{
    [STAThread]
    static void Main()
    {
        ApplicationConfiguration.Initialize();
        using var instance = new Mutex(true, "Local\\FiberVoice.Desktop", out var first);
        if (!first)
        {
            MessageBox.Show("Fiber Voice is already running. Open it from the system tray.", "Fiber Voice");
            return;
        }
        Application.Run(new DialerWindow());
    }
}

internal sealed class DialerWindow : Form
{
    private readonly GatewayHost gateway = new();
    private string Origin = "";
    private readonly WebView2 web = new() { Dock = DockStyle.Fill, Visible = false };
    private readonly Label status = new() { AutoSize = true, MaximumSize = new Size(640, 0) };
    private readonly FlowLayoutPanel welcome = new()
    {
        Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
        Padding = new Padding(40), WrapContents = false, AutoScroll = true
    };
    private readonly NotifyIcon tray;
    private readonly System.Windows.Forms.Timer poll = new() { Interval = 2000 };
    private readonly HttpClient http = new(new HttpClientHandler
    {
        UseProxy = false, AllowAutoRedirect = false
    }) { Timeout = TimeSpan.FromSeconds(2) };
    private bool quitting, checking, ready;
    private string? ringingId, waitingId;

    public DialerWindow()
    {
        Text = "Fiber Voice";
        Size = new Size(1200, 820);
        MinimumSize = new Size(900, 650);
        StartPosition = FormStartPosition.CenterScreen;
        using (var iconStream = typeof(DialerWindow).Assembly.GetManifestResourceStream("FiberVoice.AppIcon.ico")!)
        using (var appIcon = new Icon(iconStream))
            Icon = (Icon)appIcon.Clone();
        welcome.Controls.Add(new Label
        {
            Text = "Your landline, on Windows", AutoSize = true,
            Font = new Font(SystemFonts.DefaultFont.FontFamily, 24, FontStyle.Bold)
        });
        welcome.Controls.Add(status);
        AddButton("Retry", async () => await ConnectAsync());
        AddButton("Show gateway log", () =>
        {
            if (File.Exists(gateway.LogPath)) Open(gateway.LogPath);
            return Task.CompletedTask;
        });
        status.Text = "Starting your landline…\nConnect this computer to your JioFiber Wi-Fi or LAN.";
        Controls.Add(web);
        Controls.Add(welcome);
        var menu = new ContextMenuStrip();
        menu.Items.Add("Open Fiber Voice", null, (_, _) => ShowDialer());
        menu.Items.Add("Quit", null, async (_, _) =>
        {
            if (checking) return;
            poll.Stop();
            ready = false;
            Hide();
            await gateway.StopAsync();
            quitting = true;
            Close();
        });
        tray = new NotifyIcon { Text = "Fiber Voice", Icon = Icon, Visible = true, ContextMenuStrip = menu };
        tray.DoubleClick += (_, _) => ShowDialer();
        tray.BalloonTipClicked += (_, _) => ShowDialer();
        FormClosing += (_, e) =>
        {
            if (!quitting && e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; Hide(); }
        };
        Shown += async (_, _) => await ConnectAsync();
        poll.Tick += async (_, _) => await CheckCallAsync();
    }

    private void AddButton(string label, Func<Task> action)
    {
        var button = new Button { Text = label, AutoSize = true, Padding = new Padding(10), Margin = new Padding(0, 12, 0, 0) };
        button.Click += async (_, _) =>
        {
            try { await action(); }
            catch (Exception error) { MessageBox.Show(this, error.Message, "Fiber Voice"); }
        };
        welcome.Controls.Add(button);
    }

    private static void Open(string target) => Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
    private void ShowDialer() { Show(); WindowState = FormWindowState.Normal; Activate(); }
    private bool IsLocal(string value) => Uri.TryCreate(value, UriKind.Absolute, out var uri)
        && Origin.Length > 0 && uri.GetLeftPart(UriPartial.Authority) + "/" == Origin
        && uri.UserInfo.Length == 0;

    private async Task ConnectAsync()
    {
        if (checking) return;
        checking = true;
        try
        {
            status.Text = "Starting your landline… The first launch may take a moment.";
            Origin = await gateway.StartAsync();
            using var response = await http.GetAsync(Origin + "api/registration/status");
            response.EnsureSuccessStatusCode();
            using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            if (json.RootElement.ValueKind != JsonValueKind.Object) throw new InvalidOperationException("Unexpected gateway response.");
            if (web.CoreWebView2 == null)
            {
                var profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Fiber Voice", "WebView2");
                var env = await CreateBrowserAsync(profile);
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2!.Settings.AreHostObjectsAllowed = false;
                web.CoreWebView2.Settings.IsWebMessageEnabled = false;
                web.CoreWebView2.NavigationStarting += (_, e) => { e.Cancel = !IsLocal(e.Uri); };
                web.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; };
                web.CoreWebView2.DownloadStarting += (_, e) => { e.Cancel = true; };
                web.CoreWebView2.PermissionRequested += (_, e) =>
                {
                    e.State = CoreWebView2PermissionState.Deny;
                    if (!IsLocal(e.Uri) || e.PermissionKind is not (CoreWebView2PermissionKind.Microphone or CoreWebView2PermissionKind.Camera)) return;
                    e.State = MessageBox.Show(this, $"Allow Fiber Voice to use your {e.PermissionKind.ToString().ToLowerInvariant()} for calls?",
                        "Call permission", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes
                        ? CoreWebView2PermissionState.Allow : CoreWebView2PermissionState.Deny;
                };
                web.CoreWebView2.NavigationCompleted += (_, e) =>
                {
                    if (e.IsSuccess) return;
                    ready = false;
                    welcome.Visible = true;
                    web.Visible = false;
                    status.Text = "The dialer could not load. Check the gateway and retry.";
                };
            }
            ready = true;
            welcome.Visible = false;
            web.Visible = true;
            web.CoreWebView2.Navigate(Origin);
            poll.Start();
        }
        catch (Exception error)
        {
            ready = false;
            status.Text = "Cannot open the dialer.\n\n" + error.Message;
            welcome.Visible = true;
            web.Visible = false;
        }
        finally { checking = false; }
    }

    private async Task<CoreWebView2Environment> CreateBrowserAsync(string profile)
    {
        try { return await CoreWebView2Environment.CreateAsync(userDataFolder: profile); }
        catch (WebView2RuntimeNotFoundException)
        {
            status.Text = "Installing Microsoft Edge WebView2 for the calling window…\nInternet access is required for this first-time step.";
            var installer = GatewayHost.BrowserInstallerPath
                ?? throw new InvalidOperationException("Microsoft Edge WebView2 is missing from this build.");
            using var setup = Process.Start(new ProcessStartInfo(installer, "/silent /install")
            {
                UseShellExecute = false, CreateNoWindow = true
            }) ?? throw new InvalidOperationException("Could not start Microsoft WebView2 setup.");
            await setup.WaitForExitAsync();
            if (setup.ExitCode != 0) throw new InvalidOperationException("WebView2 installation failed. Check internet access and choose Retry.");
            return await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
        }
    }

    private bool polling;
    private async Task CheckCallAsync()
    {
        if (!ready || polling) return;
        polling = true;
        try
        {
            using var response = await http.GetAsync(Origin + "api/calls/current");
            response.EnsureSuccessStatusCode();
            using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Text = "Fiber Voice";
            // Call waiting: a second call rings during a call.
            if (json.RootElement.TryGetProperty("waiting", out var waiting) && waiting.ValueKind == JsonValueKind.Object
                && waiting.TryGetProperty("id", out var waitingProperty))
            {
                var waitingCall = waitingProperty.GetString();
                if (waitingId != waitingCall)
                {
                    waitingId = waitingCall;
                    tray.ShowBalloonTip(10000, "Call waiting", "Open Fiber Voice to answer or decline it.", ToolTipIcon.Info);
                    ShowDialer();
                }
            }
            else waitingId = null;
            var root = json.RootElement.GetProperty("call");
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("state", out var state) || state.GetString() != "incoming") { ringingId = null; return; }
            var id = root.GetProperty("id").GetString();
            if (ringingId == id) return;
            ringingId = id;
            tray.ShowBalloonTip(15000, "Incoming landline call", "Open Fiber Voice to answer or decline.", ToolTipIcon.Info);
            ShowDialer();
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException or JsonException or KeyNotFoundException)
        {
            Text = "Fiber Voice — gateway unavailable";
        }
        finally { polling = false; }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { poll.Stop(); poll.Dispose(); tray.Visible = false; tray.Dispose(); gateway.Dispose(); http.Dispose(); }
        base.Dispose(disposing);
    }
}
