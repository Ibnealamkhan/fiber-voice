# Fiber Voice

Make and receive calls on your **JioFiber landline number** from a computer on your JioFiber network, using its microphone and speakers. Use it in a web browser, or install the Mac or Windows desktop app.

**[Download for Mac and setup guide →](https://ibnealamkhan.github.io/fiber-voice/)** (Apple silicon, macOS 13 or later)

**[Download for Windows →](https://github.com/Ibnealamkhan/fiber-voice/releases/latest/download/FiberVoice-Setup.exe)** (Windows 11 x64, preview)

> Fiber Voice is an independent, unofficial project. It is not made by, affiliated with, or endorsed by Jio. It only works on your own JioFiber connection, with your own landline number.

## Features

- **Calling:** outgoing and incoming calls in HD voice (AMR-WB), a keypad for phone menus, mute, and a call timer.
- **Call waiting and hold:** hold and resume a call; take a second call with Hold & Answer, then swap between the two; or End & Answer, or decline.
- **Conference calls:** add people to a call (up to 6), phone-style: the app calls each person, then Jio's network merges them in, with live status for each person.
- **Video calls (beta):** H.264 video at up to 640×480 with phones that support video calling, with camera on/off and answer-with-video.
- **Recents:** a call log with missed, blocked and unreachable calls, plus search.
- **Contacts:** names you save in the app, favourites, and per-contact call history. The Mac app can also show names from your Mac's Contacts.
- **Block list and Do Not Disturb:** manual, for 1 hour, until morning, or every night on a schedule, with an option to let repeated callers through.
- **Simple setup:** on-screen first-time setup that trusts your router and verifies an OTP sent to your registered mobile.
- **Private:** everything runs locally. The dialer is only reachable from the same computer, at `127.0.0.1`.
- **Mac app extras:** menu bar icon, open at login, incoming-call notifications with Answer and Decline, click-to-call `tel:` links, and Dock badges.

## How it works

```
Browser / Mac app ──WebRTC──▶ Fiber Voice gateway ──SIP/TLS + RTP (IPv6)──▶ JioFiber router ──▶ phone network
   (your mic)                 (127.0.0.1, this computer)                      (your landline)
```

A small local gateway registers with your JioFiber router as one more device on your landline, just like the JioJoin app does. It converts browser audio to the AMR / AMR-WB format the phone network uses. Your phone's JioJoin app keeps working alongside it.

## What you need

- A JioFiber connection with **fixed-line (landline) calling active**.
- The computer connected to your **JioFiber Wi-Fi or LAN**, using the router's own DNS (no VPN or custom DNS), with **IPv6 on**. IPv6 is on by default.
- Your **registered mobile number** nearby, to receive the one-time setup code.

---

## Option 1: Use it in your web browser

Works on **macOS** and **Linux**. On Windows, use the [Windows app](#option-3-windows-app).

**1. Install the requirements**

macOS (with [Homebrew](https://brew.sh)):

```sh
brew install python opencore-amr vo-amrwbenc
```

Debian / Ubuntu:

```sh
sudo apt install -y git python3 python3-venv libopencore-amrnb0 libopencore-amrwb0 libvo-amrwbenc0
```

**2. Download and start**

```sh
git clone https://github.com/Ibnealamkhan/fiber-voice.git
cd fiber-voice/gateway
./run.sh
```

The first run creates a Python environment and installs the dependencies (`aiohttp`, `aiortc`). After that, it starts in a second or two.

**3. Open the dialer**

Go to **<http://127.0.0.1:8765/>** in Chrome, Edge, Safari or Firefox on the same computer. Complete the [first-time setup](#first-time-setup), then type a number and press Return. Allow microphone access when the browser asks.

**Good to know**

- Keep the terminal open. Calls, and incoming ringing, work only while `run.sh` is running and the page is open. Press **Ctrl+C** to stop.
- Your setup is saved in `gateway/.runtime/`, so restarting doesn't need a new code.
- The page only works on the same computer; other devices on your network can't open it. That's deliberate, for security.
- Contacts in the browser version are the ones you add in the app. The Mac app can also read your Mac's Contacts.

---

## Option 2: Build the Mac app

> Just want to use it? Download the ready-made app from the [latest release](https://github.com/Ibnealamkhan/fiber-voice/releases/latest) and follow the [install guide](https://ibnealamkhan.github.io/fiber-voice/#install). Build it yourself for an Intel Mac, or to make changes.

This produces **Fiber Voice.app** and a drag-to-install **DMG**. The app has Python, the gateway and the audio codecs built in, so the people you share it with don't need to install anything.

**1. Install the build tools** (on the Mac you build on)

```sh
xcode-select --install                         # Swift compiler (Command Line Tools)
brew install python opencore-amr vo-amrwbenc   # Python 3.11+ and the AMR codecs
```

**2. Build**

```sh
git clone https://github.com/Ibnealamkhan/fiber-voice.git
cd fiber-voice
./macapp/build.sh
```

The build:
1. Runs the gateway tests.
2. Bundles the gateway and codecs into a self-contained app.
3. Compiles the Swift app, draws the icon, signs it, and makes the DMG.

It takes a few minutes the first time. You get:

```
macapp/dist/Fiber Voice.app
macapp/dist/Fiber Voice-1.0.0-arm64.dmg
```

**3. Install**

1. Open the DMG and drag **Fiber Voice** into **Applications**.
2. Open it from Applications.

The first time, macOS may block it because it isn't notarized by Apple. Go to **System Settings ▸ Privacy & Security**, click **Open Anyway**, and confirm.

**Notes**

- **Chip type:** a build runs on Macs with the same chip type as the build Mac. Build on Apple silicon for Apple-silicon Macs, and on an Intel Mac for Intel Macs. macOS 13 or later is required.
- **Version number:** set it with `VERSION=1.1.0 ./macapp/build.sh`.
- **Sharing without the security warning:** sign and notarize with an Apple Developer ID:
  ```sh
  SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" NOTARY_PROFILE=notary ./macapp/build.sh
  ```

Full Mac app guide (menu bar, notifications, contacts, Do Not Disturb, troubleshooting): [macapp/README.md](macapp/README.md).

---

## Option 3: Windows app

> **Preview:** the Windows app hasn't been tested on a live JioFiber line yet.

**[Download FiberVoice-Setup.exe](https://github.com/Ibnealamkhan/fiber-voice/releases/latest/download/FiberVoice-Setup.exe)** from the
[latest release](https://github.com/Ibnealamkhan/fiber-voice/releases/latest) and
run it to install **Fiber Voice**, with its app icon and Start menu and desktop
shortcuts. The installer isn't signed, so Windows may show "Windows protected
your PC": choose **More info ▸ Run anyway**. The app starts its bundled calling
engine automatically: no Python, terminal or other software is needed. Complete
router trust and OTP setup in the app, just like on Mac.

To build it yourself, run `./winapp/build.ps1` in PowerShell; the installer is
written to `winapp/dist/FiberVoice-Setup.exe`. See the [Windows app guide](winapp/README.md).
Windows 11 x64 is the target. The app installs Microsoft Edge WebView2 if missing
(internet required for that first-time step).

---

## First-time setup

The browser and the Mac app use the same three steps:

1. **Find your router.** Click **Check router**. It shows your fixed-line number and the router's security certificate. Click **Trust this router** only when you're on your own home network.
2. **Authorize this computer.** Click **Send code**. The router texts a code to your registered mobile; enter it and click **Verify**.
3. **Ready to call.** It registers with your landline. Click **Start calling**.

To start over, for example after changing routers, use **Log out** in the sidebar. That also clears the call log, contacts saved in the app, favourites, blocked numbers and Do Not Disturb settings.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Cannot find the JioFiber router" | Connect directly to the JioFiber network. Turn off VPNs and custom DNS. A mesh or second router with its own DHCP can hide the JioFiber router. |
| IPv6 "Not available" | Turn on IPv6 in the router's admin page, then reconnect. |
| The code never arrives | Wait a minute, then click **Send a new code**. It goes to the mobile number registered with your JioFiber account. |
| Setup refused / device limit | Remove an old device from the JioJoin app's device list and retry. |
| "Encrypted configuration" | Your router firmware isn't supported yet. |
| A call shows "Unreachable" | Jio reported the other phone as switched off or out of coverage. Hover over the call in Recents for the exact reason. |
| No sound | Allow microphone access for the browser or the app in **System Settings ▸ Privacy & Security ▸ Microphone**. Use the audio test on the **Connection** page. Headphones avoid echo. |

Diagnostics:
- **Mac app:** **Help ▸ Show Gateway Log**.
- **Windows app:** `%LOCALAPPDATA%\Fiber Voice\gateway.log`.
- **Browser version:** the terminal running `run.sh`.

Logs mask phone numbers to their last 4 digits.

## Privacy and security

- Everything runs on your computer. The dialer is served only on `127.0.0.1` and rejects requests from other websites and devices.
- Your SIP credentials are stored locally with owner-only permissions, and are sent only to a router whose certificate matches the one you trusted.
- No calls are recorded and no audio is stored. The call log keeps numbers, times and durations, on your computer only.
- The Mac app reads your Contacts only if you allow it. They're never saved by Fiber Voice or sent anywhere.

## Project layout

```
gateway/            Python SIP/WebRTC gateway and the web dialer (static/)
  run.sh            start it for browser use
  tests/            unit and end-to-end audio tests
winapp/             Windows desktop app (C#) and its installer (FiberVoice-Setup.exe)
macapp/             native macOS app (Swift) and build script
  Sources/          app shell: window, menu bar, notifications, contacts, tel: links
  build.sh          builds Fiber Voice.app and the DMG
```

Developer notes (modules, API, protocol details): [gateway/README.md](gateway/README.md).

## Development

```sh
cd gateway
./run.sh                                         # once, to create .venv
.venv/bin/python -m unittest discover -s tests   # run the test suite
```

## License

[MIT](LICENSE). Bundled third-party codecs keep their own licenses: OpenCORE AMR (Apache 2.0) and VisualOn AMR-WB encoder (Apache 2.0).
