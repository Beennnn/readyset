// RigMenuBar — rig-status presence on the Mac, no Stream Deck needed.
// Menu-bar-only (LSUIElement, .accessory) so it never shows in the Dock.
//
// Polls the dashboard's /api/state every few seconds and surfaces a non-ok rig with
// passive, silent signals (no repeating sound/notification). Everything is toggleable
// from the menu-bar item's OPTIONS menu, and the choices persist (UserDefaults):
//
//   • Menu-bar glyph — recoloured by status (ok → discreet template; warn → orange;
//     fail → red; dashboard unreachable → grey).
//   • Edge border — a coloured frame around every screen (click-through). Warn/fail only.
//   • Floating pill — a top-centre badge with the counts ("❌ N  ⚠ M  Rig"). Warn/fail only.
//   • Expanded panel (default ON) — the problem list ALWAYS unfolded right under the pill:
//     one row per failing/warning check + a one-click 🔧 fix (POST /api/fix) when the
//     dashboard offers a remedy. Single-click the pill to fold/unfold; double-click opens
//     the web dashboard. With this mode OFF, a single click pops the same list as a menu.
//   • One notification on change — a single silent banner the moment the status worsens
//     into a problem (never repeats; stays in Notification Center until dismissed).
//
// Only the small pill + panel windows catch clicks; the border and the rest of the screen
// stay click-through, so nothing is ever blocked mid-gig.

import Cocoa
import UserNotifications

// ---------------------------------------------------------------------------
// Status model
// ---------------------------------------------------------------------------
enum RigStatus {
    case ok, warn, fail, unreachable

    static func parse(_ s: String) -> RigStatus {
        switch s {
        case "ok": return .ok; case "warn": return .warn
        case "fail": return .fail; default: return .unreachable
        }
    }
    var glyphColor: NSColor? {          // nil = default template (adapts light/dark)
        switch self {
        case .ok: return nil; case .warn: return .systemOrange
        case .fail: return .systemRed; case .unreachable: return .systemGray
        }
    }
    var showsOverlay: Bool { self == .warn || self == .fail }   // "unreachable" stays quiet
    var overlayColor: NSColor { self == .fail ? .systemRed : .systemOrange }
    var rank: Int {
        switch self { case .ok: return 0; case .unreachable: return 1
                      case .warn: return 2; case .fail: return 3 }
    }
    var label: String {
        switch self { case .ok: return "prêt"; case .warn: return "avertissement(s)"
                      case .fail: return "bloquant(s)"; case .unreachable: return "injoignable" }
    }
}

struct Problem { let key, label, status, detail, glyph: String; let remedy: String? }

// ---------------------------------------------------------------------------
// Preferences (which alert mechanisms are enabled) — persisted.
// ---------------------------------------------------------------------------
enum Pref {
    static let glyph = "pref.glyphColor", border = "pref.edgeBorder"
    static let pill = "pref.floatingPill", expanded = "pref.expandedPanel"
    static let notify = "pref.notifyOnChange"
    static let warnings = "pref.showWarnings", autofix = "pref.autoFix"
    static let popnew = "pref.popOnNewProblem"
    static func on(_ key: String, default def: Bool) -> Bool {
        let d = UserDefaults.standard
        return d.object(forKey: key) == nil ? def : d.bool(forKey: key)
    }
    static func set(_ key: String, _ v: Bool) { UserDefaults.standard.set(v, forKey: key) }
}

// A button that carries the check key it fixes.
final class KeyButton: NSButton { var key = "" }

// ---------------------------------------------------------------------------
// Edge-border view (click-through window)
// ---------------------------------------------------------------------------
final class BorderView: NSView {
    var status: RigStatus = .ok { didSet { needsDisplay = true } }
    override func draw(_ dirty: NSRect) {
        guard status.showsOverlay else { return }
        let t: CGFloat = 8
        status.overlayColor.withAlphaComponent(0.92).setStroke()
        let p = NSBezierPath(rect: bounds.insetBy(dx: t / 2, dy: t / 2)); p.lineWidth = t; p.stroke()
    }
}

// ---------------------------------------------------------------------------
// Floating pill view (clickable window). Single click → fold/menu; double → web.
// ---------------------------------------------------------------------------
final class PillView: NSView {
    var status: RigStatus = .ok
    var warns = 0, fails = 0
    var onSingle: (() -> Void)?
    var onDouble: (() -> Void)?
    private var pending: DispatchWorkItem?

    func text() -> String {
        var parts: [String] = []
        if fails > 0 { parts.append("❌ \(fails)") }
        if warns > 0 { parts.append("⚠ \(warns)") }
        let c = parts.isEmpty ? "" : parts.joined(separator: "   ") + "   "
        return "\(c)Rig — à vérifier"
    }
    static let font = NSFont.systemFont(ofSize: 15, weight: .bold)

    override func draw(_ dirty: NSRect) {
        guard status.showsOverlay else { return }
        let attrs: [NSAttributedString.Key: Any] = [.font: PillView.font, .foregroundColor: NSColor.white]
        status.overlayColor.withAlphaComponent(0.95).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: bounds.height / 2, yRadius: bounds.height / 2).fill()
        let s = (text() as NSString).size(withAttributes: attrs)
        (text() as NSString).draw(at: NSPoint(x: (bounds.width - s.width) / 2,
                                              y: (bounds.height - s.height) / 2), withAttributes: attrs)
    }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .pointingHand) }

    override func mouseUp(with e: NSEvent) {
        if e.clickCount >= 2 { pending?.cancel(); pending = nil; onDouble?() }
        else if e.clickCount == 1 {
            let w = DispatchWorkItem { [weak self] in self?.onSingle?(); self?.pending = nil }
            pending = w
            DispatchQueue.main.asyncAfter(deadline: .now() + NSEvent.doubleClickInterval, execute: w)
        }
    }
}

// ---------------------------------------------------------------------------
// App delegate
// ---------------------------------------------------------------------------
final class Delegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var timer: Timer?
    var baseSymbol: NSImage?
    var borders: [(win: NSWindow, view: BorderView)] = []
    var pills: [(win: NSPanel, view: PillView)] = []
    var details: [(win: NSPanel, stack: NSStackView)] = []   // one expanded panel per screen
    var panelFolded = false                 // user folded it via a pill click this session
    var current: RigStatus = .ok
    var curWarns = 0, curFails = 0
    var problems: [Problem] = []
    var lastFixAttempt: [String: Date] = [:]     // auto-fix throttle: don't re-fire a key within 60 s
    var seenProblemKeys: Set<String> = []        // to detect a NEWLY appeared problem

    let url = "http://127.0.0.1:8765"
    var stateURL: String { url + "/api/state" }
    var fixURL: String { url + "/api/fix" }

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        baseSymbol = NSImage(systemSymbolName: "pianokeys", accessibilityDescription: "Rig")
        let m = NSMenu(); m.delegate = self
        item.menu = m
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }

        rebuildOverlays()
        NotificationCenter.default.addObserver(self, selector: #selector(rebuildOverlays),
            name: NSApplication.didChangeScreenParametersNotification, object: nil)

        refresh()
        let t = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(t, forMode: .common); timer = t
    }

    // ---- Overlay windows (border + pill per screen) ------------------------
    @objc func rebuildOverlays() {
        borders.forEach { $0.win.orderOut(nil) }; borders.removeAll()
        pills.forEach { $0.win.orderOut(nil) }; pills.removeAll()
        details.forEach { $0.win.orderOut(nil) }; details.removeAll()
        for screen in NSScreen.screens {
            let bw = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
            bw.isOpaque = false; bw.backgroundColor = .clear; bw.hasShadow = false
            bw.ignoresMouseEvents = true; bw.level = .screenSaver
            bw.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            let bv = BorderView(frame: NSRect(origin: .zero, size: screen.frame.size))
            bw.contentView = bv; borders.append((bw, bv))

            let pw = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 200, height: 34),
                             styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
            configureFloatingPanel(pw)
            let pv = PillView(frame: NSRect(x: 0, y: 0, width: 200, height: 34))
            pv.onSingle = { [weak self] in self?.pillClicked() }
            pv.onDouble = { [weak self] in self?.open() }
            pw.contentView = pv
            pills.append((pw, pv))

            details.append(makeDetailPanel())          // matching expanded panel for this screen
        }
        applyOverlay()
    }

    private func configureFloatingPanel(_ pw: NSPanel) {
        pw.isOpaque = false; pw.backgroundColor = .clear; pw.hasShadow = true
        pw.level = .screenSaver; pw.isFloatingPanel = true; pw.becomesKeyOnlyIfNeeded = true
        pw.hidesOnDeactivate = false
        pw.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    }

    // ---- Expanded detail panel (one per screen; repopulated on each poll) ---
    private func makeDetailPanel() -> (win: NSPanel, stack: NSStackView) {
        let pw = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 560, height: 80),
                         styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        configureFloatingPanel(pw)
        // Force dark appearance: in Light mode the .hudWindow material renders light-grey,
        // which makes the white text unreadable. Dark appearance = dark material + our white
        // text keeps high contrast on any wallpaper / system appearance.
        pw.appearance = NSAppearance(named: .darkAqua)
        let fx = NSVisualEffectView(frame: pw.contentView!.bounds)
        fx.material = .hudWindow; fx.blendingMode = .behindWindow; fx.state = .active
        fx.wantsLayer = true; fx.layer?.cornerRadius = 12; fx.layer?.masksToBounds = true
        fx.autoresizingMask = [.width, .height]
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        fx.addSubview(stack)
        // Pin top/leading/trailing only — NOT bottom. Pinning both top and bottom would
        // stretch the stack to the content view's height and make its fittingSize circular
        // (it would report the constrained height, not the natural content height).
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: fx.leadingAnchor, constant: 14),
            stack.topAnchor.constraint(equalTo: fx.topAnchor, constant: 12),
        ])
        pw.contentView = fx
        return (pw, stack)
    }

    private func label(_ s: String, bold: Bool) -> NSTextField {
        let t = NSTextField(labelWithString: s)
        t.textColor = .white
        t.font = bold ? .systemFont(ofSize: 13, weight: .bold) : .systemFont(ofSize: 13)
        t.lineBreakMode = .byTruncatingTail
        t.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return t
    }

    // Build the columnar detail view: header, a grid (status | item | problem | fix),
    // then a footer with "fix all" + "config" buttons. Rebuilt on each poll.
    private func populate(_ outer: NSStackView) {
        outer.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let showWarn = Pref.on(Pref.warnings, default: true)
        let probs = showWarn ? problems : problems.filter { $0.status == "fail" }

        outer.addArrangedSubview(label("🎹 Rig — \(curFails) bloquant(s), \(curWarns) avertissement(s)", bold: true))

        if probs.isEmpty {
            outer.addArrangedSubview(label(problems.isEmpty ? "Tout est ok 🎉"
                                           : "Aucun bloquant (warnings masqués)", bold: false))
        } else {
            let grid = NSGridView()
            grid.translatesAutoresizingMaskIntoConstraints = false
            grid.rowSpacing = 6; grid.columnSpacing = 12
            for p in probs {
                let icon = label(p.status == "fail" ? "❌" : "⚠️", bold: false)
                let item = label(shortItem(p), bold: true); item.toolTip = p.label
                let prob = label(shortProblem(p), bold: false); prob.toolTip = p.detail
                let action: NSView
                if let rem = p.remedy {
                    let b = KeyButton(title: shorten(rem, 24), target: self, action: #selector(fixTapped(_:)))
                    b.key = p.key; b.bezelStyle = .rounded; b.controlSize = .small; b.toolTip = rem
                    colorize(b)                          // fix actions share one accent colour
                    action = b
                } else { action = label("—", bold: false) }
                grid.addRow(with: [icon, item, prob, action])
            }
            grid.column(at: 0).xPlacement = .center
            grid.column(at: 3).xPlacement = .trailing
            outer.addArrangedSubview(grid)
        }

        let footer = NSStackView(); footer.orientation = .horizontal; footer.spacing = 10
        let fixAll = NSButton(title: "⚡ Lancer tous les correctifs", target: self, action: #selector(fixAll))
        fixAll.bezelStyle = .rounded; fixAll.controlSize = .small
        fixAll.isEnabled = probs.contains { $0.remedy != nil }
        if fixAll.isEnabled { colorize(fixAll) }         // same accent colour as the per-fix buttons
        let cfgBtn = NSButton(title: "⚙️ Config", target: self, action: #selector(open))
        cfgBtn.bezelStyle = .rounded; cfgBtn.controlSize = .small   // stays default grey
        footer.addArrangedSubview(fixAll); footer.addArrangedSubview(cfgBtn)
        outer.addArrangedSubview(footer)
    }

    // Paint a button as the shared "fix action" colour with white text. bezelColor is
    // ignored under the forced-dark appearance, so we fill the layer ourselves.
    private func colorize(_ b: NSButton) {
        b.isBordered = false
        b.wantsLayer = true
        b.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        b.layer?.cornerRadius = 6
        b.attributedTitle = NSAttributedString(string: "  " + b.title + "  ", attributes: [
            .foregroundColor: NSColor.white,
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize, weight: .semibold)])
        b.heightAnchor.constraint(equalToConstant: 22).isActive = true
    }

    private func shorten(_ s: String, _ n: Int) -> String {
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.count <= n ? t : String(t.prefix(n - 1)) + "…"
    }
    private func shortItem(_ p: Problem) -> String {   // strip parentheticals/quotes, cap length
        var s = p.label
        for sep in [" (", " «", " —", " :"] { if let r = s.range(of: sep) { s = String(s[..<r.lowerBound]) } }
        return shorten(s, 26)
    }
    private func shortProblem(_ p: Problem) -> String { shorten(p.detail.isEmpty ? "—" : p.detail, 34) }

    // ---- Poll --------------------------------------------------------------
    func refresh() {
        guard let u = URL(string: stateURL) else { return }
        var req = URLRequest(url: u); req.timeoutInterval = 4; req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { [weak self] data, _, err in
            var status = RigStatus.unreachable; var warns = 0, fails = 0; var probs: [Problem] = []
            if err == nil, let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let s = obj["status"] as? String { status = RigStatus.parse(s) }
                warns = (obj["warns"] as? NSNumber)?.intValue ?? 0
                fails = (obj["fails"] as? NSNumber)?.intValue ?? 0
                if let items = obj["items"] as? [[String: Any]] {
                    for it in items {
                        let st = (it["status"] as? String) ?? "ok"
                        guard st == "fail" || st == "warn" else { continue }
                        probs.append(Problem(
                            key: (it["key"] as? String) ?? "", label: (it["label"] as? String) ?? "?",
                            status: st, detail: (it["detail"] as? String) ?? "",
                            glyph: (it["glyph"] as? String) ?? "•", remedy: it["remedy"] as? String))
                    }
                    probs.sort { (($0.status == "fail" ? 0 : 1), $0.label) < (($1.status == "fail" ? 0 : 1), $1.label) }
                }
            }
            DispatchQueue.main.async { self?.apply(status, warns: warns, fails: fails, problems: probs) }
        }.resume()
    }

    func apply(_ status: RigStatus, warns: Int, fails: Int, problems: [Problem]) {
        let old = current
        current = status; curWarns = warns; curFails = fails; self.problems = problems

        // Pop on new problem: if a check that wasn't a problem before just became one, unfold
        // the panel so it can't be missed — unless the user turned that behaviour off.
        let curKeys = Set(problems.map { $0.key })
        let newlyAppeared = curKeys.subtracting(seenProblemKeys)
        seenProblemKeys = curKeys
        if !newlyAppeared.isEmpty, Pref.on(Pref.popnew, default: true) { panelFolded = false }

        if !status.showsOverlay { panelFolded = false }        // reset fold when we return to normal
        applyGlyph(); applyOverlay()
        if status.rank > old.rank, status.showsOverlay { maybeNotify() }
        maybeAutoFix()
    }

    // Auto-fix mode (off by default): fire each problem's remedy as it appears, throttled
    // to once per key per 60 s so a fix that doesn't clear the problem doesn't spam.
    private func maybeAutoFix() {
        guard Pref.on(Pref.autofix, default: false) else { return }
        let now = Date()
        for p in problems where p.remedy != nil {
            if let last = lastFixAttempt[p.key], now.timeIntervalSince(last) < 60 { continue }
            lastFixAttempt[p.key] = now
            runFix(p.key)
        }
    }

    @objc func fixAll() { for p in problems where p.remedy != nil { runFix(p.key) } }

    // ---- Menu-bar glyph ----------------------------------------------------
    private func applyGlyph() {
        guard let b = item.button else { return }
        b.toolTip = "Rig : \(current.label) — clic pour les options"
        let colored = Pref.on(Pref.glyph, default: true)
        guard let base = baseSymbol else {
            let dot = !colored ? "" : (current == .fail ? "🔴" : current == .warn ? "🟠"
                        : current == .unreachable ? "⚪" : "")
            b.title = "🎹" + dot; return
        }
        if colored, let c = current.glyphColor {
            let img = base.withSymbolConfiguration(.init(paletteColors: [c])) ?? base
            img.isTemplate = false; b.image = img
        } else {
            let img = base.copy() as! NSImage; img.isTemplate = true
            b.image = img; b.contentTintColor = nil
        }
    }

    // ---- Overlays ----------------------------------------------------------
    private func applyOverlay() {
        let showBorder = Pref.on(Pref.border, default: true) && current.showsOverlay
        let showPill = Pref.on(Pref.pill, default: true) && current.showsOverlay
        for b in borders {
            b.view.status = current
            if showBorder { b.win.orderFrontRegardless() } else { b.win.orderOut(nil) }
        }
        for (i, p) in pills.enumerated() where i < NSScreen.screens.count {
            p.view.status = current; p.view.warns = curWarns; p.view.fails = curFails; p.view.needsDisplay = true
            if showPill {
                let screen = NSScreen.screens[i]
                let s = (p.view.text() as NSString).size(withAttributes: [.font: PillView.font])
                let w = s.width + 32, h: CGFloat = 34
                let x = screen.frame.minX + (screen.frame.width - w) / 2
                let y = screen.frame.maxY - h - 34
                p.win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
                p.view.frame = NSRect(x: 0, y: 0, width: w, height: h)
                p.view.window?.invalidateCursorRects(for: p.view)
                p.win.orderFrontRegardless()
            } else { p.win.orderOut(nil) }
        }
        applyDetailPanel()
    }

    // The always-unfolded panel, shown under the pill on every screen.
    private func applyDetailPanel() {
        let show = Pref.on(Pref.pill, default: true) && Pref.on(Pref.expanded, default: true)
                   && current.showsOverlay && !panelFolded
        for (i, d) in details.enumerated() where i < NSScreen.screens.count {
            guard show else { d.win.orderOut(nil); continue }
            populate(d.stack)
            d.stack.layoutSubtreeIfNeeded()
            let sz = d.stack.fittingSize                        // content-driven size (columns)
            let w = min(760, max(320, sz.width + 28))
            let h = sz.height + 24
            let screen = NSScreen.screens[i]
            let pillBottom = screen.frame.maxY - 34 - 34       // matches the pill placement above
            let x = screen.frame.minX + (screen.frame.width - w) / 2
            let y = pillBottom - 8 - h
            d.win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
            d.win.orderFrontRegardless()
        }
    }

    // ---- Interactions ------------------------------------------------------
    // Single click on the pill: in expanded mode, fold/unfold the panel; otherwise pop the menu.
    private func pillClicked() {
        if Pref.on(Pref.expanded, default: true) { panelFolded.toggle(); applyOverlay() }
        else { showDetailMenu() }
    }

    @objc func fixTapped(_ sender: KeyButton) { runFix(sender.key) }
    @objc func applyFix(_ sender: NSMenuItem) { if let k = sender.representedObject as? String { runFix(k) } }
    private func runFix(_ key: String) {
        guard !key.isEmpty, let u = URL(string: fixURL) else { return }
        var req = URLRequest(url: u); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["key": key])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refresh() }
        }.resume()
    }

    // Fallback pop-up menu (used when the expanded-panel mode is OFF).
    @objc func showDetailMenu() {
        let menu = NSMenu()
        let head = NSMenuItem(title: "🎹 Rig — \(curFails) bloquant(s), \(curWarns) avertissement(s)",
                              action: nil, keyEquivalent: ""); head.isEnabled = false
        menu.addItem(head); menu.addItem(.separator())
        if problems.isEmpty {
            let ok = NSMenuItem(title: "Tout est ok 🎉", action: nil, keyEquivalent: ""); ok.isEnabled = false
            menu.addItem(ok)
        }
        for p in problems {
            let mi = NSMenuItem(title: "\(p.glyph) \(p.label) — \(p.detail)", action: nil, keyEquivalent: "")
            if let rem = p.remedy {
                let sub = NSMenu()
                let fix = NSMenuItem(title: "🔧 \(rem)", action: #selector(applyFix(_:)), keyEquivalent: "")
                fix.target = self; fix.representedObject = p.key; sub.addItem(fix)
                mi.submenu = sub
            } else { mi.isEnabled = false; mi.toolTip = "Pas de résolution automatique — à corriger à la main." }
            menu.addItem(mi)
        }
        menu.addItem(.separator())
        add(menu, "🌐 Ouvrir le dashboard", #selector(open))
        add(menu, "↻ Rafraîchir", #selector(refreshNow))
        menu.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
    }

    // ---- Options menu (menu-bar item) — rebuilt each open ------------------
    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === item.menu else { return }
        menu.removeAllItems()
        let head = NSMenuItem(title: "🎹 Rig — \(current.label)", action: nil, keyEquivalent: "")
        head.isEnabled = false; menu.addItem(head)
        menu.addItem(.separator())
        let sec = NSMenuItem(title: "Mécanismes d'alerte", action: nil, keyEquivalent: ""); sec.isEnabled = false
        menu.addItem(sec)
        toggle(menu, "Glyphe menubar coloré", Pref.glyph, true, #selector(toggleGlyph))
        toggle(menu, "Liseré au bord de l'écran", Pref.border, true, #selector(toggleBorder))
        toggle(menu, "Pastille flottante", Pref.pill, true, #selector(togglePill))
        toggle(menu, "Menu déplié sous la pastille", Pref.expanded, true, #selector(toggleExpanded))
        toggle(menu, "Déplier sur un nouveau problème", Pref.popnew, true, #selector(togglePopnew))
        toggle(menu, "Afficher les warnings dans la popup", Pref.warnings, true, #selector(toggleWarnings))
        toggle(menu, "1 notification au changement d'état", Pref.notify, false, #selector(toggleNotify))
        toggle(menu, "⚠️ Corriger automatiquement (peut perturber)", Pref.autofix, false, #selector(toggleAutofix))
        menu.addItem(.separator())
        add(menu, "⚡ Lancer tous les correctifs", #selector(fixAll))
        add(menu, "🔍 Voir le détail des problèmes…", #selector(showDetailMenu))
        add(menu, "🌐 Ouvrir le dashboard", #selector(open))
        add(menu, "↻ Rafraîchir maintenant", #selector(refreshNow))
    }

    // ---- Toggle actions ----------------------------------------------------
    @objc func toggleGlyph()    { flip(Pref.glyph, true);  applyGlyph() }
    @objc func toggleBorder()   { flip(Pref.border, true); applyOverlay() }
    @objc func togglePill()     { flip(Pref.pill, true);   applyOverlay() }
    @objc func toggleExpanded() { flip(Pref.expanded, true); panelFolded = false; applyOverlay() }
    @objc func togglePopnew()   { flip(Pref.popnew, true) }
    @objc func toggleWarnings() { flip(Pref.warnings, true); applyOverlay() }
    @objc func toggleNotify()   { flip(Pref.notify, false) }
    @objc func toggleAutofix()  { flip(Pref.autofix, false); maybeAutoFix() }
    private func flip(_ key: String, _ def: Bool) { Pref.set(key, !Pref.on(key, default: def)) }

    @objc func refreshNow() { refresh() }
    @objc func open() { if let u = URL(string: url) { NSWorkspace.shared.open(u) } }

    private func maybeNotify() {
        guard Pref.on(Pref.notify, default: false) else { return }
        let c = UNMutableNotificationContent()
        c.title = current == .fail ? "Rig : bloquant" : "Rig : avertissement"
        c.body = "\(curFails) bloquant(s), \(curWarns) avertissement(s) — voir le dashboard."
        c.sound = nil
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "rig-change-\(current.rank)", content: c, trigger: nil))
    }

    // ---- Menu helpers ------------------------------------------------------
    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: ""); mi.target = self; menu.addItem(mi)
    }
    private func toggle(_ menu: NSMenu, _ title: String, _ key: String, _ def: Bool, _ sel: Selector) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: ""); mi.target = self
        mi.state = Pref.on(key, default: def) ? .on : .off; menu.addItem(mi)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
let delegate = Delegate()
app.delegate = delegate
app.run()
