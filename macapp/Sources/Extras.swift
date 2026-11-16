// Click-to-call (tel: links), notification actions, menu bar icon and launch at login.
import AppKit
import ServiceManagement
import UserNotifications
import WebKit

func formatPhone(_ raw: String) -> String {
    let digits = raw.filter { $0.isASCII && $0.isNumber }
    var mobile: String?
    if digits.count == 12, digits.hasPrefix("91") { mobile = String(digits.dropFirst(2)) }
    else if digits.count == 11, digits.hasPrefix("0") { mobile = String(digits.dropFirst()) }
    else if digits.count == 10 { mobile = digits }
    if let m = mobile, let first = m.first, "6789".contains(first) {
        return "+91 \(m.prefix(5)) \(m.suffix(5))"
    }
    return raw.isEmpty ? "Unknown" : raw
}

extension AppDelegate: NSMenuDelegate {

    // MARK: Click-to-call

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.first(where: { ["tel", "callto"].contains($0.scheme?.lowercased() ?? "") }) else { return }
        guard let number = AppDelegate.phoneNumber(from: url) else {
            NSSound.beep()
            return
        }
        showWindow()
        dial(number)
    }

    /// `tel:+91-98765%2043210;phone-context=…` → `+919876543210`
    static func phoneNumber(from url: URL) -> String? {
        var raw = url.absoluteString
        if let colon = raw.firstIndex(of: ":") { raw = String(raw[raw.index(after: colon)...]) }
        raw = raw.removingPercentEncoding ?? raw
        if let cut = raw.firstIndex(where: { $0 == ";" || $0 == "?" }) { raw = String(raw[..<cut]) }
        raw = raw.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        let digits = raw.filter { $0.isASCII && $0.isNumber }
        guard (3...15).contains(digits.count) else { return nil }
        return (raw.hasPrefix("+") ? "+" : "") + digits
    }

    func dial(_ number: String) {
        guard pageReady else { pendingDial = number; return }
        webView.callAsyncJavaScript("return window.fiberApp ? window.fiberApp.dial(n) : false",
                                    arguments: ["n": number], in: nil, in: .page) { _ in }
    }

    func flushPendingDial() {
        guard let number = pendingDial else { return }
        pendingDial = nil
        dial(number)
    }

    func callPage(_ function: String) {
        guard pageReady else { return }
        webView.callAsyncJavaScript("return window.fiberApp ? await window.fiberApp.\(function)() : false",
                                    arguments: [:], in: nil, in: .page) { _ in }
    }

    var isDefaultPhoneApp: Bool {
        guard let handler = NSWorkspace.shared.urlForApplication(toOpen: URL(string: "tel:100")!) else { return false }
        return handler.standardizedFileURL.path == Bundle.main.bundleURL.standardizedFileURL.path
    }

    @objc func makeDefaultPhoneApp() {
        // macOS asks the user to confirm the change.
        NSWorkspace.shared.setDefaultApplication(at: Bundle.main.bundleURL, toOpenURLsWithScheme: "tel") { error in
            guard let error else { return }
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = "Couldn’t make \(appName) the app for phone links."
                alert.informativeText = error.localizedDescription
                alert.runModal()
            }
        }
    }

    // MARK: Notification actions

    func registerNotificationActions() {
        let answer = UNNotificationAction(identifier: "ANSWER", title: "Answer", options: [.foreground])
        let decline = UNNotificationAction(identifier: "DECLINE", title: "Decline", options: [.destructive])
        let callBack = UNNotificationAction(identifier: "CALL_BACK", title: "Call Back", options: [.foreground])
        let answerVideo = UNNotificationAction(identifier: "ANSWER_VIDEO", title: "Answer with Video", options: [.foreground])
        let answerVoice = UNNotificationAction(identifier: "ANSWER", title: "Answer Voice Only", options: [.foreground])
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: "INCOMING_CALL", actions: [answer, decline], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "INCOMING_VIDEO_CALL", actions: [answerVideo, answerVoice, decline], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "MISSED_CALL", actions: [callBack], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "CALL_WAITING", actions: [
                UNNotificationAction(identifier: "ANSWER_WAITING", title: "Hold & Answer", options: [.foreground]),
                UNNotificationAction(identifier: "END_ANSWER_WAITING", title: "End & Answer", options: [.foreground]),
                UNNotificationAction(identifier: "DECLINE_WAITING", title: "Decline", options: [.destructive]),
            ], intentIdentifiers: [], options: []),
        ])
    }

    func handleNotification(action: String, userInfo: [AnyHashable: Any]) {
        switch action {
        case "ANSWER":
            showWindow()
            callPage("answer")
        case "ANSWER_VIDEO":
            showWindow()
            callPage("answerVideo")
        case "DECLINE":
            // Decline directly, so it works even while the window is hidden.
            if let id = userInfo["callId"] as? String { declineCall(id: id) }
            stopRinging()
        case "ANSWER_WAITING":
            showWindow()
            callPage("answerWaiting")
        case "END_ANSWER_WAITING":
            showWindow()
            callPage("endAndAnswer")
        case "DECLINE_WAITING":
            if let id = userInfo["callId"] as? String { declineCall(id: id) }
        case "CALL_BACK":
            showWindow()
            if let number = userInfo["number"] as? String { dial(number) }
        default:
            showWindow()
            if userInfo["view"] as? String == "missed" { showMissed() }
        }
    }

    func declineCall(id: String) {
        guard let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else { return }
        var request = URLRequest(url: gateway.baseURL.appendingPathComponent("api/calls/\(encoded)"), timeoutInterval: 5)
        request.httpMethod = "DELETE"
        URLSession.shared.dataTask(with: request).resume()
    }

    // MARK: Menu bar

    func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        item.button?.imagePosition = .imageLeading
        statusItem = item
        updateStatusIcon()
    }

    var lineStatusText: String {
        if callActive { return ringingCallId != nil ? "Incoming call" : "On a call" }
        switch lineState {
        case "registered": return dndActive ? "Do Not Disturb" : "Ready for calls"
        case "needs_authorization": return "Not set up"
        case "offline": return "Gateway not running"
        default: return "Connecting…"
        }
    }

    func updateStatusIcon() {
        guard let button = statusItem?.button else { return }
        let symbol = callActive ? "phone.connection.fill" : lineState != "registered" ? "phone.down.fill" : dndActive ? "moon.fill" : "phone.fill"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: appName)
            ?? NSImage(systemSymbolName: "phone.fill", accessibilityDescription: appName)
        image?.isTemplate = true
        button.image = image
        button.title = missedUnseen > 0 ? " \(missedUnseen)" : ""
        button.toolTip = "\(appName) — \(lineStatusText)"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        func add(_ title: String, _ action: Selector?, key: String = "", state: Bool? = nil) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = self
            if let state { item.state = state ? .on : .off }
            if action == nil { item.isEnabled = false }
            menu.addItem(item)
            return item
        }
        let header = add(lineNumber.map(formatPhone) ?? appName, nil)
        header.attributedTitle = NSAttributedString(string: header.title, attributes: [.font: NSFont.boldSystemFont(ofSize: 13)])
        _ = add(lineStatusText, nil)
        menu.addItem(.separator())
        _ = add("Show \(appName)", #selector(showWindow))
        _ = add("New Call…", #selector(newCall))
        if missedUnseen > 0 { _ = add("Missed Calls (\(missedUnseen))", #selector(showMissed)) }
        let dndItem = add(dndActive ? "Do Not Disturb: \(dndText)" : "Do Not Disturb", #selector(noop))
        let dndMenu = NSMenu()
        for (title, action, on) in [(dndActive ? "Turn Off" : "Turn On", #selector(toggleDnd), false),
                                    ("For 1 Hour", #selector(dndOneHour), false),
                                    ("Until Tomorrow Morning", #selector(dndUntilMorning), false)] as [(String, Selector, Bool)] {
            let sub = NSMenuItem(title: title, action: action, keyEquivalent: "")
            sub.target = self
            sub.state = on ? .on : .off
            dndMenu.addItem(sub)
        }
        dndMenu.addItem(.separator())
        let settings = NSMenuItem(title: "Do Not Disturb Settings…", action: #selector(showDnd), keyEquivalent: "")
        settings.target = self
        dndMenu.addItem(settings)
        dndItem.submenu = dndMenu
        _ = add("Blocked Numbers…", #selector(showBlocked))
        if !recent.isEmpty {
            menu.addItem(.separator())
            _ = add("Recent Calls", nil)
            let time = DateFormatter()
            time.timeStyle = .short
            time.dateStyle = .none
            time.doesRelativeDateFormatting = true
            for entry in recent.prefix(5) {
                guard let number = entry["number"] as? String else { continue }
                let result = entry["result"] as? String ?? ""
                let incoming = entry["direction"] as? String == "incoming"
                let arrow = result == "missed" ? "✕" : incoming ? "↙" : "↗"
                var when = ""
                if let started = entry["startedAt"] as? Double {
                    let date = Date(timeIntervalSince1970: started)
                    time.dateStyle = Calendar.current.isDateInToday(date) ? .none : .short
                    when = time.string(from: date)
                }
                let item = add("\(arrow)  \(displayName(for: number) ?? formatPhone(number))    \(when)", #selector(dialRecent(_:)))
                item.representedObject = number
                item.toolTip = "Call \(formatPhone(number))"
            }
        }
        menu.addItem(.separator())
        _ = add("Open at Login", #selector(toggleOpenAtLogin), state: opensAtLogin)
        _ = add("Use for Phone Links", #selector(makeDefaultPhoneApp), state: isDefaultPhoneApp)
        menu.addItem(.separator())
        _ = add("Quit \(appName)", #selector(NSApplication.terminate(_:)), key: "q").target = NSApp
    }

    // MARK: Do Not Disturb & block list

    var dndText: String {
        if dnd["activeReason"] as? String == "manual" { return "On" }
        guard let until = dnd["activeUntil"] as? Double else { return "On" }
        let date = Date(timeIntervalSince1970: until)
        let time = DateFormatter()
        time.timeStyle = .short
        time.dateStyle = .none
        return "until " + (Calendar.current.isDateInToday(date) ? "" : "tomorrow ") + time.string(from: date)
    }

    @objc func noop() {}

    func setDnd(_ changes: [String: Any]) {
        gateway.send("PUT", "api/dnd", json: changes) { [weak self] result in
            guard let self else { return }
            if let result { self.dnd = result }
            self.updateStatusIcon()
            self.callPage("refreshScreening")
        }
    }

    @objc func toggleDnd() { setDnd(["enabled": !dndActive]) }
    @objc func dndOneHour() { setDnd(["minutes": 60]) }

    @objc func dndUntilMorning() {
        let end = (dnd["end"] as? String ?? "07:00").split(separator: ":").compactMap { Int($0) }
        let calendar = Calendar.current
        var morning = calendar.date(bySettingHour: end.first ?? 7, minute: end.count > 1 ? end[1] : 0, second: 0, of: Date()) ?? Date()
        if morning <= Date() { morning = calendar.date(byAdding: .day, value: 1, to: morning) ?? morning }
        setDnd(["until": morning.timeIntervalSince1970])
    }

    @objc func showContacts() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showContacts()") }
    @objc func showDnd() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showDnd()") }
    @objc func showBlocked() { showWindow(); webView.evaluateJavaScript("window.fiberApp && window.fiberApp.started && window.fiberApp.showBlocked()") }

    @objc func newCall() {
        showWindow()
        callPage("focusDialer")
    }

    @objc func dialRecent(_ sender: NSMenuItem) {
        guard let number = sender.representedObject as? String else { return }
        showWindow()
        dial(number)
    }

    /// Cached line state, recent calls and missed count for the badge and menu bar.
    func refreshSummary() {
        gateway.getJSON("api/history?limit=6") { [weak self] result in
            guard let self else { return }
            self.recent = result?["entries"] as? [[String: Any]] ?? []
            self.missedUnseen = result?["missedUnseen"] as? Int ?? 0
            NSApp.dockTile.badgeLabel = self.missedUnseen > 0 ? String(self.missedUnseen) : nil
            self.updateStatusIcon()
        }
        gateway.getJSON("api/screening") { [weak self] result in
            if let dnd = result?["dnd"] as? [String: Any] { self?.dnd = dnd; self?.updateStatusIcon() }
        }
        gateway.getJSON("api/contacts") { [weak self] result in
            if let names = result?["contacts"] as? [String: String] { self?.savedNames = names }
        }
        gateway.getJSON("api/registration/status") { [weak self] result in
            guard let self else { return }
            if let result {
                self.lineNumber = result["number"] as? String ?? self.lineNumber
                self.lineState = result["registered"] as? Bool == true ? "registered" : result["state"] as? String ?? "connecting"
            } else {
                self.lineState = "offline"
            }
            self.updateStatusIcon()
        }
    }

    // MARK: Launch at login

    var opensAtLogin: Bool { SMAppService.mainApp.status == .enabled }

    var isInstalledCopy: Bool {
        let path = Bundle.main.bundlePath
        return path.hasPrefix("/Applications/") || path.hasPrefix(NSHomeDirectory() + "/Applications/")
    }

    @objc func toggleOpenAtLogin() {
        do {
            if opensAtLogin { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Couldn’t change Open at Login."
            alert.informativeText = isInstalledCopy ? error.localizedDescription
                : "Move \(appName) into your Applications folder, open it from there, and try again."
            alert.runModal()
        }
        if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
    }

    /// Ask once, from an installed copy, whether to start at login so calls can ring.
    func offerOpenAtLogin() {
        let key = "offeredOpenAtLogin"
        guard isInstalledCopy, !opensAtLogin, !UserDefaults.standard.bool(forKey: key) else { return }
        UserDefaults.standard.set(true, forKey: key)
        let alert = NSAlert()
        alert.messageText = "Open \(appName) when you log in?"
        alert.informativeText = "Your landline can only ring on this Mac while \(appName) is running. It starts quietly in the menu bar. You can change this anytime from the phone icon in the menu bar."
        alert.addButton(withTitle: "Open at Login")
        alert.addButton(withTitle: "Not Now")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.toggleOpenAtLogin() }
        }
    }

    /// True when macOS started the app as a login item (start hidden in that case).
    static var launchedAtLogin: Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              event.eventID == kAEOpenApplication else { return false }
        return event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }
}
