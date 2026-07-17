// RigMenuBar — rig-status presence on the Mac, no Stream Deck needed.
// Menu-bar-only (LSUIElement, .accessory) so it never shows in the Dock.
//
// It POLLS the dashboard's /api/state every few seconds and surfaces the aggregate
// rig status two ways, both silent and passive (no repeating sound/notification):
//
//   1. Menu-bar glyph — recoloured by status (ok → discreet template; warn → orange;
//      fail → red; dashboard unreachable → grey). Click it to open the dashboard.
//
//   2. Full-screen OVERLAY (warn/fail only) — a coloured edge border around every
//      screen + a floating pill with the counts ("⚠ 6   ❌ 5   Rig"). Click-through
//      (never blocks a click mid-gig), rides on every Space incl. full-screen apps,
//      and vanishes the instant the rig is green again. Its mere appearance IS the
//      state-change signal — persistent until fixed, no sound, impossible to miss
//      even from across the room.

import Cocoa

// ---------------------------------------------------------------------------
// Status model
// ---------------------------------------------------------------------------
enum RigStatus {
    case ok, warn, fail, unreachable

    static func parse(_ s: String) -> RigStatus {
        switch s {
        case "ok":   return .ok
        case "warn": return .warn
        case "fail": return .fail
        default:     return .unreachable
        }
    }

    /// Menu-bar glyph colour. nil = default template (adapts to light/dark).
    var glyphColor: NSColor? {
        switch self {
        case .ok:          return nil
        case .warn:        return .systemOrange
        case .fail:        return .systemRed
        case .unreachable: return .systemGray
        }
    }

    /// The big overlay only fires for real problems. "unreachable" (dashboard off, often
    /// on purpose) stays quiet — just the grey menu-bar glyph — so stopping the server
    /// doesn't paint a permanent border everywhere.
    var showsOverlay: Bool { self == .warn || self == .fail }

    var overlayColor: NSColor {
        self == .fail ? .systemRed : .systemOrange
    }
}

// ---------------------------------------------------------------------------
// Overlay: coloured screen-edge border + floating count pill (one per screen)
// ---------------------------------------------------------------------------
final class OverlayView: NSView {
    var status: RigStatus = .ok
    var warns = 0
    var fails = 0

    func update(_ s: RigStatus, warns: Int, fails: Int) {
        self.status = s; self.warns = warns; self.fails = fails
        needsDisplay = true
    }

    override func draw(_ dirty: NSRect) {
        guard status.showsOverlay else { return }
        let color = status.overlayColor

        // 1) Edge border — a thick inset stroke hugging the screen edge.
        let thickness: CGFloat = 8
        color.withAlphaComponent(0.92).setStroke()
        let frame = NSBezierPath(rect: bounds.insetBy(dx: thickness / 2, dy: thickness / 2))
        frame.lineWidth = thickness
        frame.stroke()

        // 2) Floating pill near the top centre (just under the menu bar).
        let text = pillText()
        let font = NSFont.systemFont(ofSize: 15, weight: .bold)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
        let size = (text as NSString).size(withAttributes: attrs)
        let padX: CGFloat = 16, padY: CGFloat = 8
        let pillW = size.width + padX * 2
        let pillH = size.height + padY * 2
        let pillX = (bounds.width - pillW) / 2
        let pillY = bounds.height - pillH - 34          // 34px below the top edge → clear of the menu bar
        let pillRect = NSRect(x: pillX, y: pillY, width: pillW, height: pillH)

        color.withAlphaComponent(0.95).setFill()
        NSBezierPath(roundedRect: pillRect, xRadius: pillH / 2, yRadius: pillH / 2).fill()
        (text as NSString).draw(
            at: NSPoint(x: pillX + padX, y: pillY + padY),
            withAttributes: attrs)
    }

    private func pillText() -> String {
        var parts: [String] = []
        if fails > 0 { parts.append("❌ \(fails)") }
        if warns > 0 { parts.append("⚠ \(warns)") }
        let counts = parts.isEmpty ? "" : parts.joined(separator: "   ") + "   "
        return "\(counts)Rig — à vérifier"
    }
}

// ---------------------------------------------------------------------------
// App delegate: menu-bar item + overlay windows + poll loop
// ---------------------------------------------------------------------------
final class Delegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var timer: Timer?
    var baseSymbol: NSImage?                 // SF Symbol glyph, recoloured per status
    var overlays: [(win: NSWindow, view: OverlayView)] = []
    var current: RigStatus = .ok
    var curWarns = 0, curFails = 0

    let url = "http://127.0.0.1:8765"
    let stateURL = "http://127.0.0.1:8765/api/state"

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.toolTip = "Rig — ouvrir le dashboard d'état"
            b.action = #selector(open)
            b.target = self
        }
        // A colourable SF Symbol (macOS 13+). We rebuild it with a palette colour rather
        // than tint a template — the menu bar ignores contentTintColor on template glyphs.
        baseSymbol = NSImage(systemSymbolName: "pianokeys", accessibilityDescription: "Rig")

        rebuildOverlays()
        // Recreate overlays when the display layout changes (unplug a screen, resolution…).
        NotificationCenter.default.addObserver(
            self, selector: #selector(rebuildOverlays),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)

        refresh()
        let t = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    // One click-through, all-Spaces overlay window per screen.
    @objc func rebuildOverlays() {
        overlays.forEach { $0.win.orderOut(nil) }
        overlays.removeAll()
        for screen in NSScreen.screens {
            let win = NSWindow(contentRect: screen.frame, styleMask: .borderless,
                               backing: .buffered, defer: false)
            win.isOpaque = false
            win.backgroundColor = .clear
            win.hasShadow = false
            win.ignoresMouseEvents = true                     // never eats a click
            win.level = .screenSaver                          // above normal + full-screen apps
            win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            let view = OverlayView(frame: NSRect(origin: .zero, size: screen.frame.size))
            win.contentView = view
            overlays.append((win, view))
        }
        applyOverlay()                                        // reflect current status on the fresh windows
    }

    func refresh() {
        guard let u = URL(string: stateURL) else { return }
        var req = URLRequest(url: u)
        req.timeoutInterval = 4
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { [weak self] data, _, err in
            var status = RigStatus.unreachable
            var warns = 0, fails = 0
            if err == nil, let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let s = obj["status"] as? String { status = RigStatus.parse(s) }
                warns = (obj["warns"] as? NSNumber)?.intValue ?? 0
                fails = (obj["fails"] as? NSNumber)?.intValue ?? 0
            }
            DispatchQueue.main.async { self?.apply(status, warns: warns, fails: fails) }
        }.resume()
    }

    func apply(_ status: RigStatus, warns: Int, fails: Int) {
        current = status; curWarns = warns; curFails = fails
        applyGlyph()
        applyOverlay()
    }

    private func applyGlyph() {
        guard let b = item.button else { return }
        let tip: String
        switch current {
        case .fail:        tip = "Rig : \(curFails) bloquant(s) — ouvrir le dashboard"
        case .warn:        tip = "Rig : \(curWarns) avertissement(s) — ouvrir le dashboard"
        case .ok:          tip = "Rig prêt — ouvrir le dashboard"
        case .unreachable: tip = "Rig : état indisponible (dashboard éteint ?)"
        }
        b.toolTip = tip

        guard let base = baseSymbol else {                    // no SF Symbol → coloured emoji dot
            let dot: String
            switch current { case .fail: dot = "🔴"; case .warn: dot = "🟠"
                             case .unreachable: dot = "⚪"; case .ok: dot = "" }
            b.title = "🎹" + dot
            return
        }
        if let c = current.glyphColor {                       // recolour the whole glyph
            let cfg = NSImage.SymbolConfiguration(paletteColors: [c])
            let img = base.withSymbolConfiguration(cfg) ?? base
            img.isTemplate = false
            b.image = img
        } else {                                              // healthy: discreet template glyph
            let img = base.copy() as! NSImage
            img.isTemplate = true
            b.image = img
            b.contentTintColor = nil
        }
    }

    private func applyOverlay() {
        for o in overlays {
            o.view.update(current, warns: curWarns, fails: curFails)
            if current.showsOverlay { o.win.orderFrontRegardless() } else { o.win.orderOut(nil) }
        }
    }

    @objc func open() {
        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
let delegate = Delegate()
app.delegate = delegate
app.run()
