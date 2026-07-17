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
    static let screenMode = "pref.screenMode"      // "main" (default) | "all" | "custom"
    static let screenIDs = "pref.screenIDs"        // display IDs for "custom"
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
// The floating status BAR: a rounded, status-coloured pill holding a summary label plus
// distinct clickable buttons (fix-all, fold/unfold chevron, open-web) — the delegate fills
// it via populatePill(). Height is fixed; its window width is sized to the content.
final class PillBar: NSView {
    let stack = NSStackView()
    private let grad = CAGradientLayer()
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = 17
        layer?.masksToBounds = true
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor   // subtle rim = less flat
        grad.startPoint = CGPoint(x: 0.5, y: 0); grad.endPoint = CGPoint(x: 0.5, y: 1)
        layer?.insertSublayer(grad, at: 0)
        stack.orientation = .horizontal; stack.spacing = 8; stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError("no coder") }
    override func layout() { super.layout(); grad.frame = bounds }
    // A soft vertical gradient (top lighter → bottom deeper) instead of one flat aggressive red.
    func setGradient(_ top: NSColor, _ bottom: NSColor) { grad.colors = [top.cgColor, bottom.cgColor] }
}

// The detail panel's body: a visual-effect surface that folds the panel when clicked on any
// empty area, while still letting its buttons handle their own clicks (hitTest lets a button
// or a button's subview through, and claims everything else for itself).
final class ClickableEffectView: NSVisualEffectView {
    var onClick: (() -> Void)?
    override func hitTest(_ point: NSPoint) -> NSView? {
        let hit = super.hitTest(point)
        var v = hit
        while let cur = v { if cur is NSButton { return hit }; v = cur.superview }
        return self
    }
    override func mouseUp(with event: NSEvent) { onClick?() }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .pointingHand) }
}

// ---------------------------------------------------------------------------
// App delegate
// ---------------------------------------------------------------------------
final class Delegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var timer: Timer?
    var baseSymbol: NSImage?
    var borders: [(win: NSWindow, view: BorderView)] = []
    var pills: [(win: NSPanel, bar: PillBar)] = []
    var details: [(win: NSPanel, stack: NSStackView)] = []   // one expanded panel per screen
    var panelFolded = false                 // user folded it via a pill click this session
    var current: RigStatus = .ok
    var curWarns = 0, curFails = 0
    var problems: [Problem] = []
    var lastFixAttempt: [String: Date] = [:]     // auto-fix throttle: don't re-fire a key within 60 s
    var seenProblemKeys: Set<String> = []        // to detect a NEWLY appeared problem
    var barWidths: [CGFloat] = []                 // last bar width per screen (panel matches it)

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

            let pw = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 260, height: 34),
                             styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
            configureFloatingPanel(pw)
            let bar = PillBar(frame: NSRect(x: 0, y: 0, width: 260, height: 34))
            pw.contentView = bar
            pills.append((pw, bar))

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
        let fx = ClickableEffectView(frame: pw.contentView!.bounds)
        fx.onClick = { [weak self] in self?.toggleFold() }     // click empty panel area = fold/unfold
        fx.material = .hudWindow; fx.blendingMode = .behindWindow; fx.state = .active
        fx.wantsLayer = true; fx.layer?.cornerRadius = 14; fx.layer?.masksToBounds = true
        fx.layer?.borderWidth = 1
        fx.layer?.borderColor = NSColor.white.withAlphaComponent(0.09).cgColor
        fx.autoresizingMask = [.width, .height]
        // Dark tint over the blur so the panel reads as a stable near-black surface instead of
        // picking up whatever colour the wallpaper is behind it (it was going green on a forest).
        let tint = NSView(frame: fx.bounds)
        tint.wantsLayer = true
        tint.layer?.backgroundColor = NSColor(white: 0.06, alpha: 0.55).cgColor
        tint.autoresizingMask = [.width, .height]
        fx.addSubview(tint)
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 9
        stack.translatesAutoresizingMaskIntoConstraints = false
        fx.addSubview(stack)
        // Pin top/leading/trailing only — NOT bottom. Pinning both top and bottom would
        // stretch the stack to the content view's height and make its fittingSize circular
        // (it would report the constrained height, not the natural content height).
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: fx.leadingAnchor, constant: 16),
            stack.topAnchor.constraint(equalTo: fx.topAnchor, constant: 14),
        ])
        pw.contentView = fx
        return (pw, stack)
    }

    // Build the columnar detail view: header, a grid (status | item | problem | fix),
    // then a footer with "fix all" + "config" buttons. Rebuilt on each poll.
    private func populate(_ outer: NSStackView) {
        outer.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let showWarn = Pref.on(Pref.warnings, default: true)
        let probs = showWarn ? problems : problems.filter { $0.status == "fail" }

        // No header here: the bar above already shows "Rig — N à vérifier" + the counts.
        // The panel is the header's unfolded body → straight to the problem list.
        if probs.isEmpty {
            let msg = NSTextField(labelWithString: problems.isEmpty ? "Tout est ok 🎉"
                                                   : "Aucun bloquant (warnings masqués)")
            msg.font = .systemFont(ofSize: 12); msg.textColor = .secondaryLabelColor
            outer.addArrangedSubview(msg)
        } else {
            let grid = NSGridView()
            grid.translatesAutoresizingMaskIntoConstraints = false
            grid.rowSpacing = 9; grid.columnSpacing = 12
            for p in probs {
                let item = NSTextField(labelWithString: shortItem(p))
                item.font = .systemFont(ofSize: 13, weight: .semibold); item.textColor = .white
                item.toolTip = p.label; item.lineBreakMode = .byTruncatingTail
                let prob = NSTextField(labelWithString: shortProblem(p))
                prob.font = .systemFont(ofSize: 12); prob.textColor = .secondaryLabelColor
                prob.toolTip = p.detail; prob.lineBreakMode = .byTruncatingTail
                let action: NSView
                if let rem = p.remedy {
                    let b = KeyButton(title: shorten(rem, 24), target: self, action: #selector(fixTapped(_:)))
                    b.key = p.key; b.toolTip = rem
                    colorize(b)
                    action = b
                } else {
                    action = NSView()            // no remedy → empty cell (no orphaned "—")
                }
                let row = grid.addRow(with: [statusIcon(p.status), item, prob, action])
                row.yPlacement = .center
            }
            grid.column(at: 0).xPlacement = .center
            grid.column(at: 3).xPlacement = .trailing
            outer.addArrangedSubview(grid)
        }

        // No footer: the global actions ("tout corriger", open web) live in the status bar
        // (the pill) now — the panel is just the per-problem list with each row's own fix.
    }

    // Status glyph inside a soft tinted circle — a modern "chip" look, calmer than a
    // full-bleed coloured icon.
    private func statusIcon(_ status: String) -> NSView {
        let color: NSColor = status == "fail" ? .systemRed : .systemOrange
        let name = status == "fail" ? "xmark" : "exclamationmark"
        let chip = NSView(); chip.wantsLayer = true
        chip.layer?.backgroundColor = color.withAlphaComponent(0.22).cgColor
        chip.layer?.cornerRadius = 11
        chip.translatesAutoresizingMaskIntoConstraints = false
        let iv = NSImageView()
        iv.translatesAutoresizingMaskIntoConstraints = false
        if let img = NSImage(systemSymbolName: name, accessibilityDescription: status) {
            let cfg = NSImage.SymbolConfiguration(pointSize: 11, weight: .heavy).applying(.init(hierarchicalColor: color))
            iv.image = img.withSymbolConfiguration(cfg)
        }
        chip.addSubview(iv)
        NSLayoutConstraint.activate([
            chip.widthAnchor.constraint(equalToConstant: 22),
            chip.heightAnchor.constraint(equalToConstant: 22),
            iv.centerXAnchor.constraint(equalTo: chip.centerXAnchor),
            iv.centerYAnchor.constraint(equalTo: chip.centerYAnchor),
        ])
        return chip
    }

    // Paint a button as the shared "fix action" colour with white text. bezelColor is
    // ignored under the forced-dark appearance, so we fill the layer ourselves.
    private func colorize(_ b: NSButton) {
        b.isBordered = false
        b.wantsLayer = true
        b.layer?.backgroundColor = NSColor.systemGreen.cgColor    // fix actions = green
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
        for (i, b) in borders.enumerated() {
            b.view.status = current
            if showBorder && allowedScreen(i) { b.win.orderFrontRegardless() } else { b.win.orderOut(nil) }
        }
        for (i, p) in pills.enumerated() where i < NSScreen.screens.count {
            if showPill && allowedScreen(i) {
                populatePill(p.bar)
                p.bar.layoutSubtreeIfNeeded()
                let screen = NSScreen.screens[i]
                let w = p.bar.stack.fittingSize.width + 24, h: CGFloat = 34
                while barWidths.count <= i { barWidths.append(0) }
                barWidths[i] = w                                   // panel below will match this
                let x = screen.frame.minX + (screen.frame.width - w) / 2
                let y = screen.frame.maxY - h - 34
                p.win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
                p.win.orderFrontRegardless()
            } else { p.win.orderOut(nil) }
        }
        applyDetailPanel()
    }

    // Fill the status bar: soft gradient + summary + fix-all / web, then the fold chevron
    // at the far right (disclosure convention). Keeps red, but richer than one flat aggressive tone.
    private func populatePill(_ bar: PillBar) {
        // Neutral dark bar (was full red). Severity shows only as a small coloured dot now.
        bar.setGradient(NSColor(white: 0.19, alpha: 0.96), NSColor(white: 0.11, alpha: 0.96))
        bar.stack.arrangedSubviews.forEach { $0.removeFromSuperview() }

        bar.stack.addArrangedSubview(dot(current.overlayColor))     // red/orange accent
        let n = curFails + curWarns
        let summary = NSTextField(labelWithString: "Rig — \(n) à vérifier")
        summary.font = .systemFont(ofSize: 14, weight: .bold); summary.textColor = .white
        bar.stack.addArrangedSubview(summary)

        if problems.contains(where: { $0.remedy != nil }) {
            bar.stack.addArrangedSubview(barButton("bolt.fill", "Tout corriger",
                                                   "Lancer tous les correctifs", #selector(fixAll), green: true))
        }
        bar.stack.addArrangedSubview(barButton("arrow.up.forward.square", nil,
                                               "Ouvrir le dashboard web (détail)", #selector(open)))
        // Disclosure chevron LAST (far right), per platform convention.
        let folded = panelFolded || !Pref.on(Pref.expanded, default: true)
        bar.stack.addArrangedSubview(barButton(folded ? "chevron.down" : "chevron.up", nil,
                                               folded ? "Déplier le détail" : "Replier le détail",
                                               #selector(toggleFold)))
    }

    // A small coloured status dot (red for blockers, orange for warnings).
    private func dot(_ color: NSColor) -> NSView {
        let v = NSView(); v.wantsLayer = true
        v.layer?.backgroundColor = color.cgColor; v.layer?.cornerRadius = 5
        v.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([v.widthAnchor.constraint(equalToConstant: 10),
                                     v.heightAnchor.constraint(equalToConstant: 10)])
        return v
    }

    // A pill-bar button: white SF symbol (+ optional white text). Neutral chips are
    // translucent white; the fix action ("tout corriger") is green like the panel's fixes.
    private func barButton(_ symbol: String, _ text: String?, _ tip: String, _ action: Selector,
                           green: Bool = false) -> NSButton {
        let b = NSButton(); b.target = self; b.action = action; b.toolTip = tip
        b.isBordered = false; b.wantsLayer = true
        b.layer?.backgroundColor = (green ? NSColor.systemGreen : NSColor.white.withAlphaComponent(0.26)).cgColor
        b.layer?.cornerRadius = 11
        b.imagePosition = text == nil ? .imageOnly : .imageLeading
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: tip) {
            let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .bold)
                .applying(.init(hierarchicalColor: .white))
            b.image = img.withSymbolConfiguration(cfg)
        }
        if let text = text {
            b.attributedTitle = NSAttributedString(string: text + "  ", attributes: [
                .foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 12, weight: .semibold)])
        }
        b.heightAnchor.constraint(equalToConstant: 22).isActive = true
        if text == nil { b.widthAnchor.constraint(equalToConstant: 28).isActive = true }
        return b
    }

    // The always-unfolded panel, shown under the pill on every screen.
    private func applyDetailPanel() {
        let show = Pref.on(Pref.pill, default: true) && Pref.on(Pref.expanded, default: true)
                   && current.showsOverlay && !panelFolded
        for (i, d) in details.enumerated() where i < NSScreen.screens.count {
            guard show && allowedScreen(i) else { d.win.orderOut(nil); continue }
            populate(d.stack)
            d.stack.layoutSubtreeIfNeeded()
            let sz = d.stack.fittingSize                        // content-driven size (columns)
            let barW = i < barWidths.count ? barWidths[i] : 0   // never narrower than the bar → one card
            let w = min(760, max(320, barW, sz.width + 28))
            let h = sz.height + 24
            let screen = NSScreen.screens[i]
            let pillBottom = screen.frame.maxY - 34 - 34       // matches the pill placement above
            let x = screen.frame.minX + (screen.frame.width - w) / 2
            let y = pillBottom - 4 - h                          // tight gap → connected to the bar
            d.win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
            d.win.orderFrontRegardless()
        }
    }

    // ---- Interactions ------------------------------------------------------
    // The bar's chevron: in expanded mode, fold/unfold the detail panel; otherwise pop the menu.
    @objc func toggleFold() {
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
        menu.addItem(.separator())
        menu.addItem(screensSubmenu())
        add(menu, "⏻ Quitter iRig", #selector(quit))
    }

    // "Afficher sur" submenu: main screen (default) / all / per-screen custom picks.
    private func screensSubmenu() -> NSMenuItem {
        let item = NSMenuItem(title: "🖥️ Afficher sur", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let mode = UserDefaults.standard.string(forKey: Pref.screenMode) ?? "main"
        let main = NSMenuItem(title: "Écran principal", action: #selector(setScreenMain), keyEquivalent: "")
        main.target = self; main.state = mode == "main" ? .on : .off; sub.addItem(main)
        let all = NSMenuItem(title: "Tous les écrans", action: #selector(setScreenAll), keyEquivalent: "")
        all.target = self; all.state = mode == "all" ? .on : .off; sub.addItem(all)
        if NSScreen.screens.count > 1 {
            sub.addItem(.separator())
            let ids = (UserDefaults.standard.array(forKey: Pref.screenIDs) as? [Int]) ?? []
            for (idx, s) in NSScreen.screens.enumerated() {
                let it = NSMenuItem(title: "Écran \(idx + 1) — \(Int(s.frame.width))×\(Int(s.frame.height))",
                                    action: #selector(toggleScreen(_:)), keyEquivalent: "")
                it.target = self; it.tag = displayID(s)
                it.state = (mode == "custom" && ids.contains(displayID(s))) ? .on : .off
                sub.addItem(it)
            }
        }
        item.submenu = sub
        return item
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

    // ---- Screen selection --------------------------------------------------
    private func displayID(_ s: NSScreen) -> Int {
        (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.intValue ?? -1
    }
    private func allowedScreen(_ i: Int) -> Bool {
        let screens = NSScreen.screens
        guard i < screens.count else { return false }
        switch UserDefaults.standard.string(forKey: Pref.screenMode) ?? "main" {
        case "all": return true
        case "custom":
            let ids = (UserDefaults.standard.array(forKey: Pref.screenIDs) as? [Int]) ?? []
            return ids.contains(displayID(screens[i]))
        default:                                    // "main" = the primary display (origin 0,0)
            let primary = screens.firstIndex(where: { $0.frame.origin == .zero }) ?? 0
            return i == primary
        }
    }
    @objc func setScreenMain() { UserDefaults.standard.set("main", forKey: Pref.screenMode); applyOverlay() }
    @objc func setScreenAll()  { UserDefaults.standard.set("all", forKey: Pref.screenMode); applyOverlay() }
    @objc func toggleScreen(_ sender: NSMenuItem) {
        UserDefaults.standard.set("custom", forKey: Pref.screenMode)
        var ids = (UserDefaults.standard.array(forKey: Pref.screenIDs) as? [Int]) ?? []
        if ids.contains(sender.tag) { ids.removeAll { $0 == sender.tag } } else { ids.append(sender.tag) }
        UserDefaults.standard.set(ids, forKey: Pref.screenIDs); applyOverlay()
    }

    // KeepAlive=true would respawn a plain terminate, so bootout the LaunchAgent (stays quit
    // until next login, where RunAtLoad brings it back).
    @objc func quit() {
        let p = Process(); p.launchPath = "/bin/launchctl"
        p.arguments = ["bootout", "gui/\(getuid())/com.readyset.menubar"]
        try? p.run()
    }

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
