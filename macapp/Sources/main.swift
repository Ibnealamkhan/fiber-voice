// Fiber Voice: a native Mac shell around the local JioFiber SIP/WebRTC gateway.
//
// The app launches the bundled gateway on 127.0.0.1, shows its dialer in a
// WKWebView (with microphone access), keeps running in the background so
// incoming calls ring, and stops the gateway (deregistering) when it quits.
import AppKit
import Darwin
import UserNotifications
import WebKit

let appName = "Fiber Voice"

final class Gateway {
    private(set) var process: Process?
    private(set) var port: Int = 8765
    var onExit: ((Int32) -> Void)?
    let supportDir: URL
    let logURL: URL

    init() {
        let fm = FileManager.default
        supportDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(appName, isDirectory: true)
        let logs = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/\(appName)", isDirectory: true)
        try? fm.createDirectory(at: supportDir, withIntermediateDirectories: true,
                                attributes: [.posixPermissions: 0o700])
        try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: supportDir.path)
        try? fm.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("gateway.log")
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    static func portIsFree(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    static func ephemeralPort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { ptr -> Int32 in
                _ = bind(fd, ptr, len)
                return getsockname(fd, ptr, &len)
            }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    func start() throws {
        guard let resources = Bundle.main.resourceURL else { throw GatewayError.missing("resources") }
        let executable = resources.appendingPathComponent("gateway/fiber-gateway")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw GatewayError.missing(executable.path)
        }
        // Prefer a stable port (handy for opening the dialer in a browser too).
        port = Gateway.portIsFree(8765) ? 8765 : Gateway.ephemeralPort()

        let process = Process()
        process.executableURL = executable
        process.currentDirectoryURL = supportDir
        var env = ProcessInfo.processInfo.environment.filter { ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG"].contains($0.key) }
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PORT"] = String(port)
        env["GATEWAY_RUNTIME"] = supportDir.path
        env["GATEWAY_LIB_DIR"] = resources.appendingPathComponent("codecs").path
        env["GATEWAY_PARENT_PID"] = String(getpid())
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            // Keep the log bounded: start fresh when it grows past 2 MB.
            if (try? log.seekToEnd()) ?? 0 > 2_000_000 { try? log.truncate(atOffset: 0) }
            log.write("\n--- \(Date()) starting gateway on 127.0.0.1:\(port)\n".data(using: .utf8)!)
            process.standardOutput = log
            process.standardError = log
        }
        process.terminationHandler = { [weak self] p in
            DispatchQueue.main.async { self?.onExit?(p.terminationStatus) }
        }
        try process.run()
        self.process = process
    }

    /// Ask the gateway to deregister and exit; force it after a timeout.
    func stop(timeout: TimeInterval = 6, completion: @escaping () -> Void) {
        guard let process, process.isRunning else { completion(); return }
        onExit = nil
        process.terminate()  // SIGTERM → graceful aiohttp shutdown + SIP deregistration
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(timeout)
            while process.isRunning && Date() < deadline { usleep(100_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            DispatchQueue.main.async(execute: completion)
        }
    }

    func send(_ method: String, _ path: String, json: [String: Any], completion: @escaping ([String: Any]?) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent(path), timeoutInterval: 5)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            var result: [String: Any]?
            if let data, (response as? HTTPURLResponse)?.statusCode == 200 {
                result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    func getJSON(_ path: String, timeout: TimeInterval = 3, completion: @escaping ([String: Any]?) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent(path), timeoutInterval: timeout)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { data, response, _ in
            var result: [String: Any]?
            if let data, (response as? HTTPURLResponse)?.statusCode == 200 {
                result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }
}

enum GatewayError: LocalizedError {
    case missing(String)
    var errorDescription: String? {
        switch self { case .missing(let what): return "The app bundle is incomplete (missing \(what)). Reinstall \(appName)." }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate {
    let gateway = Gateway()
    var window: NSWindow!
    var webView: WKWebView!
    var statusLabel: NSTextField!
    var spinner: NSProgressIndicator!
    var placeholder: NSView!
    var monitor: Timer?
    var ringTimer: Timer?
    var ringingCallId: String?
    var waitingCallId: String?
    var callActive = false
    var quitting = false
    var activity: NSObjectProtocol?
    var pollCount = 0
    var ringingNumber = ""
    var statusItem: NSStatusItem?
    var pendingDial: String?
    var pageReady = false
    var offeredLoginItem = false
    var lineNumber: String?
    var lineState = "connecting"
    var recent: [[String: Any]] = []
    var missedUnseen = 0
    let contacts = ContactBook()
    var savedNames: [String: String] = [:]
    var dnd: [String: Any] = [:]
    var dndActive: Bool { dnd["active"] as? Bool == true }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let quietStart = AppDelegate.launchedAtLogin
        buildMenu()
        buildWindow()
        buildStatusItem()
        registerNotificationActions()
        contacts.onChange = { [weak self] in self?.pushContacts() }
        contacts.start()
        if quietStart { window.orderOut(nil) }  // started at login: wait in the menu bar
        // Keep timers and the gateway responsive in the background so calls ring.
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiatedAllowingIdleSystemSleep], reason: "Receiving landline calls")
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        startGateway()
    }

    // MARK: Window and web view

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = appName
        window.minSize = NSSize(width: 420, height: 620)
        window.delegate = self
        window.setFrameAutosaveName("MainWindow")
        if !window.setFrameUsingName("MainWindow") { window.center() }
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(red: 0.957, green: 0.965, blue: 0.949, alpha: 1)

        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(WeakScriptHandler(self), name: "fiber")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        placeholder = NSView()
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.startAnimation(nil)
        statusLabel = NSTextField(wrappingLabelWithString: "Starting the voice gateway…")
        statusLabel.alignment = .center
        statusLabel.textColor = NSColor(red: 0.13, green: 0.22, blue: 0.18, alpha: 1)
        statusLabel.font = .systemFont(ofSize: 14)
        let stack = NSStackView(views: [spinner, statusLabel])
        stack.orientation = .vertical
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        placeholder.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: placeholder.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: placeholder.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 460),
            statusLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 460),
        ])
        window.contentView = placeholder
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showStatus(_ text: String, busy: Bool) {
        statusLabel.stringValue = text
        busy ? spinner.startAnimation(nil) : spinner.stopAnimation(nil)
        spinner.isHidden = !busy
        window.contentView = placeholder
    }

    func startGateway() {
        showStatus("Starting the voice gateway…", busy: true)
        gateway.onExit = { [weak self] status in self?.gatewayExited(status) }
        do {
            try gateway.start()
        } catch {
            showStatus(error.localizedDescription, busy: false)
            return
        }
        waitForGateway(attempt: 0)
    }

    func waitForGateway(attempt: Int) {
        gateway.getJSON("api/setup", timeout: 1) { [weak self] result in
            guard let self else { return }
            if result != nil {
                self.window.contentView = self.webView
                self.pageReady = false
                self.webView.load(URLRequest(url: self.gateway.baseURL))
                self.startMonitor()
            } else if attempt < 120, self.gateway.process?.isRunning == true {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForGateway(attempt: attempt + 1) }
            } else if self.gateway.process?.isRunning == true {
                self.showStatus("The voice gateway did not start. See Help ▸ Show Gateway Log.", busy: false)
            }
        }
    }

    func gatewayExited(_ status: Int32) {
        guard !quitting else { return }
        pageReady = false
        lineState = "offline"
        updateStatusIcon()
        monitor?.invalidate()
        stopRinging()
        showStatus("The voice gateway stopped unexpectedly (status \(status)). Restarting…", busy: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.startGateway() }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Stay running so the landline can still ring; quit from the menu.
        window.orderOut(nil)
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: Incoming calls

    func startMonitor() {
        monitor?.invalidate()
        monitor = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.poll() }
    }

    func poll() {
        gateway.getJSON("api/calls/current") { [weak self] result in
            guard let self else { return }
            let call = result?["call"] as? [String: Any]
            let state = call?["state"] as? String ?? ""
            let wasActive = self.callActive
            self.callActive = ["dialing", "ringing", "early_media", "incoming", "answering", "active", "ending"].contains(state)
            if wasActive != self.callActive { self.updateStatusIcon() }
            if state == "incoming", let id = call?["id"] as? String {
                if self.ringingCallId != id {
                    self.ringingCallId = id
                    self.ringingNumber = call?["number"] as? String ?? "Unknown number"
                    self.incomingCall(id: id, number: self.ringingNumber, video: call?["offeredVideo"] as? Bool == true)
                }
            } else if let id = self.ringingCallId {
                self.ringingCallId = nil
                self.stopRinging()
                self.checkMissed(callId: id, number: self.ringingNumber)
            }
            // Call waiting: a second call rings while you're on a call.
            if let waiting = result?["waiting"] as? [String: Any], let id = waiting["id"] as? String {
                if self.waitingCallId != id {
                    self.waitingCallId = id
                    self.callWaiting(id: id, number: waiting["number"] as? String ?? "Unknown number")
                }
            } else if self.waitingCallId != nil {
                self.waitingCallId = nil
                UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["call-waiting"])
            }
        }
        pollCount += 1
        if pollCount % 4 == 1 { refreshSummary() }
    }

    func checkMissed(callId: String, number: String) {
        gateway.getJSON("api/history?limit=5") { [weak self] result in
            let entries = result?["entries"] as? [[String: Any]] ?? []
            guard let entry = entries.first(where: { $0["id"] as? String == callId }),
                  entry["result"] as? String == "missed" else { return }
            let content = UNMutableNotificationContent()
            content.title = "Missed call"
            content.body = self?.displayName(for: number) ?? formatPhone(number)
            if self?.displayName(for: number) != nil { content.subtitle = formatPhone(number) }
            content.categoryIdentifier = "MISSED_CALL"
            content.userInfo = ["view": "missed", "number": number]
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: "missed-\(callId)", content: content, trigger: nil))
            self?.refreshSummary()
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let action = response.actionIdentifier
        let userInfo = response.notification.request.content.userInfo
        DispatchQueue.main.async {
            self.handleNotification(action: action, userInfo: userInfo)
            completionHandler()
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }

    func incomingCall(id: String, number: String, video: Bool = false) {
        showWindow()
        NSApp.requestUserAttention(.criticalRequest)
        let content = UNMutableNotificationContent()
        content.title = video ? "Incoming video call" : "Incoming call"
        content.body = displayName(for: number) ?? formatPhone(number)
        if displayName(for: number) != nil { content.subtitle = formatPhone(number) }
        content.categoryIdentifier = video ? "INCOMING_VIDEO_CALL" : "INCOMING_CALL"
        content.userInfo = ["callId": id, "number": number]
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "incoming-call", content: content, trigger: nil))
        ringTimer?.invalidate()
        ring()
        ringTimer = Timer.scheduledTimer(withTimeInterval: 2.2, repeats: true) { [weak self] _ in self?.ring() }
    }

    func callWaiting(id: String, number: String) {
        let content = UNMutableNotificationContent()
        content.title = "Call waiting"
        content.body = displayName(for: number) ?? formatPhone(number)
        if displayName(for: number) != nil { content.subtitle = formatPhone(number) }
        content.categoryIdentifier = "CALL_WAITING"
        content.userInfo = ["callId": id, "number": number]
        if !dndActive { content.sound = .default }
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "call-waiting", content: content, trigger: nil))
    }

    func ring() { NSSound(named: "Submarine")?.play() }

    func stopRinging() {
        ringTimer?.invalidate()
        ringTimer = nil
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["incoming-call"])
    }

    // MARK: WKUIDelegate / navigation

    func isGatewayOrigin(_ origin: WKSecurityOrigin) -> Bool {
        origin.protocol == "http" && origin.host == "127.0.0.1" && origin.port == gateway.port
    }

    @available(macOS 12.0, *)
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        // The macOS microphone/camera prompts (TCC) still ask the user the first time.
        decisionHandler(isGatewayOrigin(origin) ? .grant : .deny)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.host == "127.0.0.1" && url.port == gateway.port { decisionHandler(.allow); return }
        if ["http", "https", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { pageReady = false; webView.reload() }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
        pushContacts()
        flushPendingDial()
        refreshSummary()
        if !offeredLoginItem {
            offeredLoginItem = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let self, self.window.isVisible else { return }
                self.offerOpenAtLogin()
            }
        }
    }

    // MARK: Menu actions

    @objc func reloadPage() { pageReady = false; webView.load(URLRequest(url: gateway.baseURL)) }

    @objc func showRecents() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showRecents()") }
    @objc func showMissed() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showMissed()") }
    @objc func showConnection() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showConnection()") }

    @objc func setUpAgain() {
        showWindow()
        let alert = NSAlert()
        alert.messageText = "Log out of your landline?"
        alert.informativeText = "This Mac stops receiving calls, forgets the trusted router and clears its call log, saved names and blocked numbers. Logging back in needs a new code sent to your registered mobile number."
        alert.addButton(withTitle: "Log Out")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            self?.webView.evaluateJavaScript("window.fiberVoice && window.fiberVoice.resetSetup()")
            NSApp.dockTile.badgeLabel = nil
        }
    }

    @objc func openInBrowser() { NSWorkspace.shared.open(gateway.baseURL) }
    @objc func showLog() { NSWorkspace.shared.activateFileViewerSelecting([gateway.logURL]) }
    @objc func showDataFolder() { NSWorkspace.shared.open(gateway.supportDir) }

    @objc func showAbout() {
        let credits = NSAttributedString(
            string: "Calls from your JioFiber fixed-line number using this Mac. Works only on your JioFiber network.\nIndependent project; not affiliated with or endorsed by Jio.",
            attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        NSApp.orderFrontStandardAboutPanel(options: [.credits: credits])
    }

    func buildMenu() {
        let main = NSMenu()
        func item(_ title: String, _ action: Selector?, _ key: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        submenu(appName, [
            item("About \(appName)", #selector(showAbout)), .separator(),
            item("Hide \(appName)", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))), .separator(),
            item("Quit \(appName)", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "z", [.command, .shift]), .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("Line", [
            item("Recents", #selector(showRecents), "1"),
            item("Missed Calls", #selector(showMissed), "2"),
            item("Contacts", #selector(showContacts), "4"),
            item("Connection", #selector(showConnection), "3"), .separator(),
            item("Do Not Disturb", #selector(toggleDnd), "d", [.command, .shift]),
            item("Blocked Numbers", #selector(showBlocked)), .separator(),
            item("Reload", #selector(reloadPage), "r"), .separator(),
            item("Log Out…", #selector(setUpAgain)),
            item("Open Dialer in Browser", #selector(openInBrowser)),
        ])
        let windowItems = [item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
                           item("Close", #selector(NSWindow.performClose(_:)), "w")]
        submenu("Window", windowItems)
        submenu("Help", [item("Show Gateway Log", #selector(showLog)), item("Show Data Folder", #selector(showDataFolder))])
        NSApp.mainMenu = main
        NSApp.windowsMenu = main.items[3].submenu
    }

    // MARK: Quit

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if callActive && !quitting {
            let alert = NSAlert()
            alert.messageText = "A call is in progress."
            alert.informativeText = "Quitting ends the call."
            alert.addButton(withTitle: "Quit and Hang Up")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        quitting = true
        monitor?.invalidate()
        stopRinging()
        gateway.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
