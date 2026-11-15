// Names for phone numbers from the Mac's Contacts (read on device, never stored or sent anywhere).
import AppKit
import Contacts
import WebKit

final class ContactBook {
    private let store = CNContactStore()
    private(set) var names: [String: String] = [:]
    /// People with phone numbers: [{id, name, numbers: [{label, number}]}], sorted by name.
    private(set) var people: [[String: Any]] = []
    var onChange: (() -> Void)?
    private var observer: NSObjectProtocol?

    /// Same rule as the gateway and page: the last ten digits identify an Indian number
    /// however it is written (+91 98765 43210, 09876543210, 98765 43210).
    static func key(for number: String) -> String {
        let digits = number.filter { $0.isASCII && $0.isNumber }
        return digits.count >= 10 ? String(digits.suffix(10)) : digits
    }

    var status: String {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return "notDetermined"
        case .denied, .restricted: return "denied"
        case .authorized: return "authorized"
        @unknown default: return "authorized"  // e.g. limited access: read what is shared
        }
    }

    func start() {
        observer = NotificationCenter.default.addObserver(forName: .CNContactStoreDidChange, object: nil, queue: .main) { [weak self] _ in
            self?.reload()
        }
        if status == "authorized" { reload() }
    }

    func requestAccess(completion: @escaping () -> Void) {
        store.requestAccess(for: .contacts) { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.reload()
                completion()
            }
        }
    }

    func reload() {
        guard status == "authorized" else {
            names = [:]
            people = []
            onChange?()
            return
        }
        let store = self.store
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var result: [String: String] = [:]
            var people: [[String: Any]] = []
            let keys: [CNKeyDescriptor] = [CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
                                           CNContactPhoneNumbersKey as CNKeyDescriptor,
                                           CNContactOrganizationNameKey as CNKeyDescriptor]
            try? store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { contact, _ in
                var name = CNContactFormatter.string(from: contact, style: .fullName) ?? ""
                if name.isEmpty { name = contact.organizationName }
                guard !name.isEmpty else { return }
                var numbers: [[String: String]] = []
                for phone in contact.phoneNumbers {
                    let key = ContactBook.key(for: phone.value.stringValue)
                    guard key.count >= 3 else { continue }
                    if result[key] == nil { result[key] = name }
                    let label = phone.label.map { CNLabeledValue<NSString>.localizedString(forLabel: $0) } ?? "phone"
                    numbers.append(["label": label.lowercased(), "number": phone.value.stringValue])
                }
                if !numbers.isEmpty { people.append(["id": contact.identifier, "name": name, "numbers": numbers]) }
            }
            people.sort { ($0["name"] as? String ?? "").localizedCaseInsensitiveCompare($1["name"] as? String ?? "") == .orderedAscending }
            DispatchQueue.main.async {
                self?.people = people
                self?.names = result
                self?.onChange?()
            }
        }
    }

    func name(for number: String) -> String? { names[ContactBook.key(for: number)] }
}

/// Avoids a retain cycle between WKUserContentController and the app delegate.
final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(controller, didReceive: message)
    }
}

extension AppDelegate: WKScriptMessageHandler {
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard isGatewayOrigin(message.frameInfo.securityOrigin),
              let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "requestContacts":
            contacts.requestAccess { [weak self] in self?.pushContacts() }
        case "openContactsSettings":
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts") {
                NSWorkspace.shared.open(url)
            }
        case "openContact":
            // Edit people in the Contacts app; Fiber Voice only reads them.
            if let id = body["id"] as? String,
               let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed),
               let url = URL(string: "addressbook://\(encoded)") {
                NSWorkspace.shared.open(url)
            }
        default:
            break
        }
    }

    /// Saved names in the app win over names from the Mac's Contacts.
    func displayName(for number: String) -> String? {
        let key = ContactBook.key(for: number)
        return savedNames[key] ?? contacts.names[key]
    }

    func pushContacts() {
        guard pageReady else { return }
        webView.callAsyncJavaScript("return window.fiberApp ? window.fiberApp.setSystemContacts(names, status, people) : false",
                                    arguments: ["names": contacts.names, "status": contacts.status, "people": contacts.people],
                                    in: nil, in: .page) { _ in }
    }
}
