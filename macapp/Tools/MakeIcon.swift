// Renders the 1024×1024 app icon PNG: usage `make-icon <output.png>`.
import AppKit

let size = 1024.0
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// macOS icon grid: 824pt rounded square centered in 1024 with a soft shadow.
let inset = 100.0
let rect = NSRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
let shape = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
shadow.shadowBlurRadius = 28
shadow.shadowOffset = NSSize(width: 0, height: -12)
NSGraphicsContext.saveGraphicsState()
shadow.set()
NSColor(red: 0.14, green: 0.36, blue: 0.26, alpha: 1).setFill()
shape.fill()
NSGraphicsContext.restoreGraphicsState()
NSGradient(colors: [NSColor(red: 0.20, green: 0.50, blue: 0.36, alpha: 1),
                    NSColor(red: 0.09, green: 0.27, blue: 0.18, alpha: 1)])!.draw(in: shape, angle: -90)

// Subtle fiber "signal" arcs.
for (i, radius) in [175.0, 245.0, 315.0].enumerated() {
    NSColor.white.withAlphaComponent(0.55 - Double(i) * 0.15).setStroke()
    let arc = NSBezierPath()
    arc.appendArc(withCenter: NSPoint(x: 532, y: 502), radius: radius, startAngle: 22, endAngle: 68)
    arc.lineWidth = 30
    arc.lineCapStyle = .round
    arc.stroke()
}

// Handset glyph.
let config = NSImage.SymbolConfiguration(pointSize: 420, weight: .semibold)
if let symbol = NSImage(systemSymbolName: "phone.fill", accessibilityDescription: nil)?.withSymbolConfiguration(config) {
    let tinted = NSImage(size: symbol.size, flipped: false) { r in
        symbol.draw(in: r)
        NSColor.white.set()
        r.fill(using: .sourceAtop)
        return true
    }
    let s = tinted.size
    tinted.draw(in: NSRect(x: (size - s.width) / 2 + 20, y: (size - s.height) / 2 - 10, width: s.width, height: s.height))
}
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
