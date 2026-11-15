# Fiber Voice for Mac

Make and receive calls on your **JioFiber landline number** from your Mac, using its microphone and speakers. No phone or JioJoin app is needed once setup is done.

Fiber Voice is an independent project. It is not made by, affiliated with, or endorsed by Jio.

## What you need

- A JioFiber connection with **fixed-line (landline) calling active**.
- A Mac on your **JioFiber Wi-Fi or LAN**, using the router's own DNS. Turn off VPNs and custom DNS such as 8.8.8.8 or 1.1.1.1.
- **IPv6 turned on** in the router. This is the default setting.
- Your **registered mobile number** nearby. The router texts a one-time code to it during setup.
- macOS 13 Ventura or later, on a Mac with the same chip type as the build: Apple silicon for builds made on Apple silicon.

## Install

1. Open `Fiber Voice-<version>-arm64.dmg` and drag **Fiber Voice** into **Applications**.
2. Open it. This copy is not notarized by Apple, so macOS blocks the first launch. Go to **System Settings ▸ Privacy & Security**, scroll down, click **Open Anyway** for Fiber Voice, and confirm.
3. If macOS asks whether Fiber Voice may find devices on your local network, click **Allow**. The app needs this to reach the router.

## First-time setup (about a minute)

1. **Find your router.** Click **Check router**. The app shows your fixed-line number and the router's certificate. Click **Trust this router** only when you're on your own home network.
2. **Authorize this Mac.** Click **Send code**. The router texts a code to the registered mobile number. Enter it and click **Verify**.
3. **Ready to call.** The Mac registers with your landline. Click **Start calling**.

The router treats this Mac as one of your landline's devices, so your phone's JioJoin app keeps working alongside it.

## Using it

- **Dialer (right):** type or paste a number anywhere in the window and press Return, or click the green button. Allow microphone access the first time. During a call you get Mute, a Keypad for menus like "press 1", and Hang up. Escape hangs up.
- **Recents (middle):** every call made or received on this Mac, grouped by day, with direction, result (missed, busy, no answer) and duration. Click a call to put its number in the dialer, double-click or use the call button to call back, or use the trash button to remove it. You can search by number.
- **Contact names:** calls show names instead of numbers.
  - The first time, Recents asks to **Show names from your Mac's Contacts**. Click **Allow** and confirm the macOS prompt.
  - To name a number yourself, hover a call and click the pencil. Names you save in Fiber Voice take priority over Contacts.
  - Names also appear in the dialer as you type, on the call screen, in incoming and missed call notifications, and in the menu bar. Search works by name.
  - Numbers match however they're written: `+91 98765 43210`, `09876543210` and `98765 43210` are the same contact.
- **Contacts (⌘4):** an A–Z list of everyone in your Mac's Contacts who has a phone number, plus people you add in Fiber Voice, with search by name or number.
  - Click someone to see their card: each number with its label (mobile, work, home) and a call button, your recent calls with them, and **Favourite**, **Block** and **Edit**.
  - Double-click a contact to call their main number.
  - **Favourites:** star a person, or a single number on their card, to pin them at the top of the list.
  - **Add contact (+):** saves a name and number in Fiber Voice only.
  - People from your Mac's Contacts are read-only here. **Edit in Contacts** opens them in the Contacts app, and changes show up in Fiber Voice automatically.
- **Missed:** the sidebar, the Dock icon and the menu bar icon show a count of missed calls you haven't looked at, and macOS shows a "Missed call" notification with a **Call Back** button. Click the notification to jump to **Missed** (⌘2).
- **Do Not Disturb:** silences this Mac. Callers hear that the line is unavailable, and their calls appear in **Missed** marked "Silenced". Other devices on your landline, like your phone, still ring.
  - Turn it on from the sidebar switch, the menu bar (**Do Not Disturb ▸ For 1 Hour / Until Tomorrow Morning**), or **Line ▸ Do Not Disturb** (⇧⌘D).
  - **Every night** turns it on automatically, for example 22:00–07:00. Turning it off during that window skips just that night.
  - **Allow repeated calls** lets a second call from the same number within 3 minutes ring, in case it's urgent.
- **Blocked numbers:** calls from blocked numbers are rejected and logged as "Blocked call".
  - Block from Recents (hover a call, then the ⊘ button) or add a number on the **Blocked** screen. Unblock there or with the same button.
  - Numbers match in any format. Outgoing calls to blocked numbers still work.
  - Whether the caller hears a rejection on every device depends on how Jio's network handles a decline from one device; at minimum this Mac never rings.
- **Connection (⌘3):** router and registration status, plus a microphone and speaker test.
- **Log out** (sidebar, or **Line ▸ Log Out…**): stops this Mac from receiving calls, forgets the router and its authorization, and clears the call log, contacts added in Fiber Voice, favourites, blocked numbers and Do Not Disturb settings. Your Mac's Contacts are never changed. Logging back in needs a new code.

### Incoming calls

- When a call comes in, the window opens, the Dock icon bounces, a ringtone plays, and a notification appears with **Answer** and **Decline** buttons. Hover over the banner, or click **Options**, to see them.
- For the buttons to stay on screen until you respond, set **System Settings ▸ Notifications ▸ Fiber Voice** to **Alerts** (or **Persistent** on newer macOS).

### Menu bar and Open at Login

- A phone icon in the menu bar shows your line's status: a solid phone means ready, a crossed-out phone means not connected, and a different icon appears during a call. Its menu has your number, **New Call…**, missed calls, your five most recent calls (click one to call back), **Open at Login**, **Use for Phone Links** and **Quit**.
- **Open at Login** starts Fiber Voice quietly in the menu bar when you log in, so your landline can always ring. The app offers to turn this on the first time you open it from Applications.
- **Closing the window does not quit the app.** It keeps running so incoming calls can ring. To reopen the dialer, click the Dock or menu bar icon. To stop receiving calls, quit with ⌘Q.

### Click-to-call

- Fiber Voice handles `tel:` phone links. To make it the default, choose **Use for Phone Links** in the menu bar menu and confirm the macOS prompt. Clicking a phone number in Safari, Mail, Contacts, Calendar or Messages then opens Fiber Voice with the number ready; press Return to call.
- Wear headphones for the best audio quality.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Cannot find the JioFiber router" | Join the JioFiber network directly, not a separate mesh or second router running its own DHCP. Turn off VPNs and custom DNS. |
| IPv6 "Not available" | Turn on IPv6 in the router's admin page (`http://192.168.29.1` or similar), then reconnect Wi-Fi. |
| The code never arrives | Wait a minute, then click **Send a new code**. The code goes to the mobile number registered with your JioFiber account. |
| "Device limit" / setup refused | Remove an old device from the JioJoin app's device list, then retry. |
| "Encrypted configuration" | Your router firmware hands out configuration in a format this app can't read yet. |
| Registered, but calls fail | Use **Line ▸ Reload**, then **Retry registration**. Check that landline calling works from your phone. |
| You changed routers | Use **Log out**, then set up again |

**Help ▸ Show Gateway Log** opens the diagnostic log. Settings, credentials and the call log live in `~/Library/Application Support/Fiber Voice/`, readable only by your user account. To fully remove the app, delete it from Applications along with that folder.

## Privacy and security

- Everything runs on your Mac. The dialer is served only on `127.0.0.1` and rejects requests from other sites and devices.
- Credentials are sent only to a router whose certificate matches the one you trusted.
- The app records no calls and stores no audio. The call log keeps only numbers, times and durations, on this Mac.
- Contacts are read on this Mac only when you allow it. They're used for display, never saved by Fiber Voice and never sent anywhere.

---

## Building from source

On a Mac with Xcode Command Line Tools, Python 3.11+ and Homebrew:

```sh
brew install opencore-amr vo-amrwbenc
./macapp/build.sh
```

The output is `macapp/dist/Fiber Voice.app` and `macapp/dist/Fiber Voice-<version>-<arch>.dmg`. The build runs the gateway tests, freezes the Python gateway with PyInstaller, bundles the AMR codec libraries, compiles the Swift shell, and signs ad hoc.

To distribute without Gatekeeper warnings, sign and notarize with an Apple Developer ID:

```sh
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" NOTARY_PROFILE=notary ./macapp/build.sh
```

Each build targets the build Mac's CPU. Build on an Intel Mac for Intel users.

### How it fits together

```
Fiber Voice.app
├── Contents/MacOS/FiberVoice          Swift/AppKit shell (Sources/): window, WKWebView, mic permission,
│                                      ringing and notification actions, menu bar icon,
│                                      tel: links, open at login, gateway lifecycle
└── Contents/Resources/
    ├── gateway/fiber-gateway          the Python SIP/WebRTC gateway (../gateway), frozen
    └── codecs/                        OpenCORE AMR + VisualOn AMR-WB encoder dylibs
```

The shell starts the gateway on `127.0.0.1:8765`, or on a free port if 8765 is busy. It passes `GATEWAY_RUNTIME` (the Application Support folder) and `GATEWAY_LIB_DIR` (the bundled codecs), then loads the gateway's dialer page. Quitting sends SIGTERM, so the gateway deregisters only its own SIP contact. If the shell crashes, the gateway notices its parent is gone and exits.
