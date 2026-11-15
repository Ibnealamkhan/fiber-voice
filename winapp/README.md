# Fiber Voice for Windows

Open **FiberVoice.exe** to make and receive JioFiber landline calls. This is a
single, portable Windows x64 app. No terminal, WSL, Docker, Python installation,
or .NET installation is needed on the computer running it.

## Use

1. Run `FiberVoice-Setup.exe` to install **Fiber Voice**, with its app icon and
   named Start menu shortcut. The desktop shortcut is selected by default and
   can be turned off during setup. Open **Fiber Voice** after installation.
2. Stay connected to your JioFiber Wi-Fi or LAN, with IPv6 enabled and the
   router’s DNS in use.
3. Complete the built-in router trust and OTP setup. Allow microphone access;
   allow camera access if you want video calling.
4. Open **Connection** and run the audio test, then verify a real call in both
   directions.

The C# Windows app bundles the existing Python calling engine and its runtime,
audio codecs, web dialer, and .NET runtime inside the EXE. It extracts the engine
on first launch and starts it automatically on a free loopback port. The engine
stops when you quit the app, and also shuts down if the app crashes. The first
launch may take longer while files are unpacked.

If Edge WebView2 is missing, the app runs Microsoft’s bundled runtime installer.
That first-time step needs internet access. The app does not install or configure
WSL or Docker. Windows 11 x64 is the target; native Windows ARM64 builds are not
provided. The EXE is unsigned.

Closing the window keeps the app running in the system tray so calls can ring.
Right-click its tray icon and choose **Quit** to stop it. Incoming calls open the
window and show a notification. Windows sleep interrupts calls.

## Local data and troubleshooting

Settings, credentials, and call history: `%LOCALAPPDATA%\Fiber Voice\Runtime`.
Browser profile: `%LOCALAPPDATA%\Fiber Voice\WebView2`.
Extracted calling engine and licenses: `%LOCALAPPDATA%\Fiber Voice\Engine`.
Diagnostics: `%LOCALAPPDATA%\Fiber Voice\gateway.log`, available through the
**Show gateway log** button if startup fails.

Windows Contacts, start at login, and phone-link registration are not included.
Windows firewall or microphone privacy settings may need adjustment if calls
connect without audio. The app never disables the firewall.

## Build from source

On a build computer, install Python 3.14+ with pip, the .NET 10 SDK, and NSIS, then run:

```powershell
./winapp/build.ps1
```

Outputs: `winapp/dist/FiberVoice-Setup.exe` (installer) and
`winapp/dist/FiberVoice.exe` (portable). The installer registers **Fiber Voice** in
Windows Installed apps and provides an uninstaller. Uninstalling keeps your
settings and call history. Python 3.14 is only a build requirement;
the included Windows engine uses CPython 3.13.16. Downloads and Windows wheels
are hash-locked. If Microsoft updates its WebView2 bootstrapper, review the new
binary and update the pinned hash in `prepare_bundle.py` deliberately.

Cross-build on macOS/Linux:

```sh
python3 winapp/prepare_bundle.py
dotnet publish winapp/FiberVoice.csproj -c Release -r win-x64 --self-contained true -o winapp/dist/standalone-win-x64
cp winapp/dist/standalone-win-x64/FiberVoice.exe winapp/dist/FiberVoice.exe
cd winapp && makensis installer.nsi
```

The **Windows app** GitHub Actions workflow builds the EXE and checks native
codec loading and gateway startup/shutdown on Windows. It does not publish a
release. Actual router registration, media, WebView2 permissions and tray behavior
still need verification on a Windows PC connected to JioFiber.
