// RigMenuBar — rig-status presence on the Mac, no Stream Deck needed.
// Menu-bar-only (LSUIElement, .accessory) so it never shows in the Dock.
//
// Polls the dashboard's /api/state every few seconds and surfaces a non-ok rig with
// passive, silent signals (no repeating sound/notification). Everything is toggleable
// from the menu-bar item's OPTIONS menu, and the choices persist (UserDefaults):
//
//   • Menu-bar glyph — recoloured by status (ok → green; warn → orange; fail → red;
//     dashboard unreachable → grey).
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
// Localisation
// ---------------------------------------------------------------------------
/// Every user-visible string goes through `T`. The English text lives in the call site
/// as the DEFAULT value, so the app is fully usable — and generic — with no .strings
/// file at all; a translation only overrides what it actually covers. That is what makes
/// English the base language of the project while a French build is just `fr.lproj`
/// dropped into Resources, never a fork of the source.
///
/// Adding a language: copy `menubar/Resources/fr.lproj/Localizable.strings` to
/// `<code>.lproj/`, translate the right-hand side, and add the code to
/// CFBundleLocalizations in build.sh. No Swift change.
func T(_ key: String, _ english: String) -> String {
    NSLocalizedString(key, value: english, comment: "")
}

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
    /// nil = glyphe template monochrome (suit le clair/sombre du système).
    /// `.ok` est VERT depuis le 2026-08-18 : en template, « tout va bien » et « le glyphe
    /// coloré est désactivé » se ressemblaient trait pour trait, donc un rig vert ne se
    /// distinguait pas d'une option éteinte. Le vert est une information, pas du décor —
    /// il dit « vérifié à l'instant, rien à corriger », ce qu'un glyphe neutre ne dit pas.
    var glyphColor: NSColor? {
        switch self {
        case .ok: return .systemGreen; case .warn: return .systemOrange
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
        switch self { case .ok: return T("status.ok", "ready"); case .warn: return T("status.warn", "warning(s)")
                      case .fail: return T("status.fail", "blocker(s)")
                      case .unreachable: return T("status.unreachable", "unreachable") }
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

/// NSTabViewController repose le titre de la fenêtre sur le libellé de l'onglet courant
/// à chaque bascule — d'où une fenêtre « Sans titre » au premier affichage, puis un titre
/// qui change en naviguant. On le refixe après coup pour garder un titre stable.
final class SettingsTabController: NSTabViewController {
    // Le premier onglet est sélectionné pendant la construction, quand `view.window` est
    // encore nil : le redimensionnement ci-dessous ne s'appliquerait donc jamais à
    // l'ouverture, et la fenêtre garderait sa taille initiale. On le rejoue à l'affichage.
    override func viewDidAppear() {
        super.viewDidAppear()
        fit(to: tabViewItems[safe: selectedTabViewItemIndex])
    }

    override func tabView(_ tabView: NSTabView, didSelect item: NSTabViewItem?) {
        super.tabView(tabView, didSelect: item)
        fit(to: item)
    }

    private func fit(to item: NSTabViewItem?) {
        guard let win = view.window else { return }
        win.title = T("settings.title", "iRig Settings")
        // La fenêtre garderait sinon la hauteur de l'onglet le plus haut, laissant un grand
        // vide sous les onglets plus courts. On la retaille sur le contenu réel, en gardant
        // le bord HAUT fixe : `setFrame` ancre en bas, donc sans compenser l'origine la
        // fenêtre semblerait sauter vers le haut à chaque changement d'onglet.
        guard let page = item?.viewController?.view else { return }
        let size = NSSize(width: max(page.fittingSize.width, win.contentLayoutRect.width),
                          height: page.fittingSize.height)
        let top = win.frame.maxY
        win.setContentSize(size)
        var f = win.frame; f.origin.y = top - f.height
        win.setFrame(f, display: true, animate: false)
    }
}

extension Array {
    /// Indice tolérant : `selectedTabViewItemIndex` vaut -1 tant qu'aucun onglet n'est
    /// sélectionné, ce qui ferait planter un accès direct.
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

// A macOS toggle switch bound to a preference key (used by the Settings window).
final class PrefSwitch: NSSwitch {
	var key = ""
	var onChange: (() -> Void)?
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
    /// Le mode DEMANDÉ (auto | live | studio) — c'est bien le demandé et non le résolu :
    /// « Auto » doit rester coché quand il choisit studio tout seul, sinon le menu laisse
    /// croire qu'on a figé le mode à la main.
    var requestedMode = "auto"
    var problems: [Problem] = []
    var lastFixAttempt: [String: Date] = [:]     // auto-fix throttle: don't re-fire a key within 60 s
    var seenProblemKeys: Set<String> = []        // to detect a NEWLY appeared problem
    var barWidths: [CGFloat] = []                 // last bar width per screen (panel matches it)
    var settingsWin: NSWindow?                    // the classic Settings window

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

        // Test/debug hook: RIG_SETTINGS=1 opens the Settings window at launch (for screenshots).
        if ProcessInfo.processInfo.environment["RIG_SETTINGS"] != nil { showSettings() }
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
                                                   : T("panel.noBlockers", "No blockers (warnings hidden)"))
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
            var wantedMode = "auto"
            if err == nil, let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let s = obj["status"] as? String { status = RigStatus.parse(s) }
                wantedMode = (obj["requested"] as? String) ?? "auto"
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
            DispatchQueue.main.async {
                self?.requestedMode = wantedMode
                self?.apply(status, warns: warns, fails: fails, problems: probs)
            }
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
                        : current == .unreachable ? "⚪" : "🟢")
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
        let summary = NSTextField(labelWithString: String(format: T("pill.summary", "Rig — %d to check"), n))
        summary.font = .systemFont(ofSize: 14, weight: .bold); summary.textColor = .white
        bar.stack.addArrangedSubview(summary)

        if problems.contains(where: { $0.remedy != nil }) {
            bar.stack.addArrangedSubview(barButton("bolt.fill", "Tout corriger",
                                                   "Lancer tous les correctifs", #selector(fixAll), green: true))
        }
        bar.stack.addArrangedSubview(barButton("arrow.up.forward.square", nil,
                                               T("pill.openWeb", "Open the web dashboard (details)"), #selector(open)))
        // Disclosure chevron LAST (far right), per platform convention.
        let folded = panelFolded || !Pref.on(Pref.expanded, default: true)
        bar.stack.addArrangedSubview(barButton(folded ? "chevron.down" : "chevron.up", nil,
                                               folded ? T("pill.unfold", "Unfold the details") : T("pill.fold", "Fold the details"),
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
        else { open() }   // panneau déplié désactivé → le dashboard, pas un pop-up moche
    }

    @objc func fixTapped(_ sender: KeyButton) { runFix(sender.key) }
    @objc func applyFix(_ sender: NSMenuItem) { if let k = sender.representedObject as? String { runFix(k) } }
    /// POSTe une action du moteur, puis rafraîchit.
    ///
    /// Le menu et les boutons du dashboard tapent EXACTEMENT les mêmes endpoints : c'est
    /// la seule façon d'avoir les mêmes actions des deux côtés sans qu'une liste dérive
    /// de l'autre. Sur scène on n'ouvre pas une page web pour ranger des fenêtres, et
    /// devoir se rappeler laquelle des deux surfaces sait faire quoi est exactement ce
    /// qu'on veut éviter.
    private func act(_ path: String, _ body: [String: Any] = [:]) {
        guard let u = URL(string: url + path) else { return }
        var rq = URLRequest(url: u); rq.httpMethod = "POST"; rq.timeoutInterval = 120
        rq.setValue("application/json", forHTTPHeaderField: "Content-Type")
        rq.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: rq) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refresh() }
        }.resume()
    }

    /// L'action unique : lance tout, répare, range, re-vérifie. Voir /api/preflight.
    @objc func prepareAll() { act("/api/preflight", ["dry": false]) }
    @objc func setModeAuto() { act("/api/mode", ["mode": "auto"]) }
    @objc func setModeLive() { act("/api/mode", ["mode": "live"]) }
    @objc func setModeStudio() { act("/api/mode", ["mode": "studio"]) }
    /// Le seul check que le Mac ne peut pas mesurer : on le déclare.
    @objc func confirmCharge() { act("/api/manual", ["key": "iphone_charge", "value": true]) }

    /// Ferme les applis dont le rig n'a pas besoin — APRÈS confirmation nommant chacune.
    /// Une app peut tenir un document non enregistré ; c'est la seule action du menu qui
    /// puisse faire perdre du travail, donc la seule qui pose une question.
    @objc func quitOthers() {
        guard let u = URL(string: stateURL) else { return }
        URLSession.shared.dataTask(with: u) { [weak self] data, _, _ in
            guard let d = data,
                  let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
                  let extra = o["unexpected"] as? [[String: Any]] else { return }
            let names = extra.compactMap { $0["name"] as? String }
            let paths = extra.compactMap { $0["path"] as? String }
            DispatchQueue.main.async {
                guard !paths.isEmpty else { return }
                let a = NSAlert()
                a.messageText = T("quit.title", "Quit these apps?")
                a.informativeText = names.joined(separator: ", ")
                    + "\n\n" + T("quit.warn", "An app holding an unsaved document will ask for itself.")
                a.addButton(withTitle: T("quit.ok", "Quit them"))
                a.addButton(withTitle: T("quit.cancel", "Cancel"))
                NSApp.activate(ignoringOtherApps: true)
                if a.runModal() == .alertFirstButtonReturn {
                    self?.act("/api/quit-apps", ["paths": paths, "dry": false])
                }
            }
        }.resume()
    }

    @objc func openJournal() { if let u = URL(string: url) { NSWorkspace.shared.open(u) } }

    private func runFix(_ key: String) {
        guard !key.isEmpty, let u = URL(string: fixURL) else { return }
        var req = URLRequest(url: u); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["key": key])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refresh() }
        }.resume()
    }

    // ---- Options menu (menu-bar item) — rebuilt each open ------------------
    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === item.menu else { return }
        menu.removeAllItems()
        let head = NSMenuItem(title: "🎹 Rig — \(current.label)", action: nil, keyEquivalent: "")
        head.isEnabled = false; menu.addItem(head)
        menu.addItem(.separator())
        // L'ouverture du dashboard passe en tête : c'est l'action de loin la plus
        // fréquente. Les réglages descendent en bas, avec Quitter — on y touche une
        // fois puis plus jamais, ils n'ont rien à faire au-dessus des actions du soir.
        // Le menu porte les MÊMES actions que la barre du dashboard, en sections :
        // d'abord le mode, puis LA seule action à connaître, puis les gestes ponctuels,
        // enfin ce qui ouvre une fenêtre. Ce découpage est le même dans les deux
        // surfaces ; c'est ce qui permet de ne pas avoir à se rappeler où est quoi.
        let mode = NSMenuItem(title: T("menu.mode", "Mode"), action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for (title, sel, key) in [(T("mode.auto", "🅰 Auto"), #selector(setModeAuto), "auto"),
                                  (T("mode.live", "🎤 Live"), #selector(setModeLive), "live"),
                                  (T("mode.studio", "🎧 Studio"), #selector(setModeStudio), "studio")] {
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            mi.target = self; mi.state = (requestedMode == key) ? .on : .off
            sub.addItem(mi)
        }
        mode.submenu = sub; menu.addItem(mode)
        menu.addItem(.separator())

        add(menu, T("menu.prepare", "✨ Prepare everything"), #selector(prepareAll))
        menu.addItem(.separator())

        add(menu, T("menu.charge", "🔋 Confirm the iPhone is charging"), #selector(confirmCharge))
        add(menu, T("menu.quitOthers", "🧹 Quit the other apps…"), #selector(quitOthers))
        menu.addItem(.separator())

        add(menu, T("menu.dashboard", "🌐 Open the dashboard"), #selector(open))
        add(menu, T("menu.journal", "📜 Action journal"), #selector(openJournal))
        menu.addItem(.separator())
        let settings = NSMenuItem(title: T("menu.settings", "⚙︎ Settings…"),
                                  action: #selector(showSettings), keyEquivalent: ",")
        settings.target = self; menu.addItem(settings)
        add(menu, T("menu.quit", "⏻ Quit iRig"), #selector(quit))
    }

    // ---- Settings window (classic macOS look) ------------------------------
    @objc func showSettings() {
        if settingsWin == nil { settingsWin = makeSettingsWindow() }
        settingsWin?.center()
        settingsWin?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Fenêtre Réglages en ONGLETS (NSTabViewController style .toolbar) — le gabarit
    /// natif des Réglages macOS. Avant : les trois sections empilées dans une seule
    /// colonne, soit 732 px de haut une fois les explications ajoutées. Les onglets
    /// ramènent chaque page à sa propre hauteur et la fenêtre se redimensionne toute
    /// seule en changeant d'onglet.
    private func makeSettingsWindow() -> NSWindow {
        let tabs = SettingsTabController()
        tabs.tabStyle = .toolbar

        tabs.addTabViewItem(settingsTab(T("tab.display", "Display"), "eye", [
            Row("paintpalette", T("display.glyph.title", "Colour the menu-bar glyph"),
                T("display.glyph.hint",
                  "The menu-bar piano takes the colour of the worst check: green, orange, red."),
                Pref.glyph, true, { self.applyGlyph() }),
            Row("square.dashed", T("display.border.title", "Border around the screen"),
                T("display.border.hint",
                  "Frames the screen in colour for as long as a problem lasts. Readable from across a stage."),
                Pref.border, true, { self.applyOverlay() }),
            Row("capsule", T("display.pill.title", "Floating pill"),
                T("display.pill.hint",
                  "A small bar on screen summarising the state, on top of the menu-bar icon."),
                Pref.pill, true, { self.applyOverlay() }),
            Row("list.bullet.rectangle", T("display.panel.title", "Details in a panel under the pill"),
                T("display.panel.hint",
                  "Lists the problems under the pill, each with its own fix button. Turned off, the "
                  + "details come up as a plain menu when you click the pill."),
                Pref.expanded, true, { self.panelFolded = false; self.applyOverlay() }),
        ]))

        tabs.addTabViewItem(settingsTab(T("tab.alerts", "Alerts"), "bell", [
            Row("exclamationmark.triangle", T("alerts.warnings.title", "Show warnings, not just errors"),
                T("alerts.warnings.hint",
                  "Otherwise only blocking errors (red) are listed; orange warnings stay hidden."),
                Pref.warnings, true, { self.applyOverlay() }),
            Row("rectangle.expand.vertical", T("alerts.popnew.title", "Open the panel on every new problem"),
                T("alerts.popnew.hint",
                  "The moment a check turns to a warning OR an error, the panel unfolds by itself so it "
                  + "can't go unnoticed."),
                Pref.popnew, true, {}),
            Row("bell", T("alerts.notify.title", "One notification when the state changes"),
                T("alerts.notify.hint",
                  "A macOS notification when severity gets worse (green → orange → red), not on every check."),
                Pref.notify, false, {}),
            Row("wrench.and.screwdriver", T("alerts.autofix.title", "Fix automatically"),
                T("alerts.autofix.hint",
                  "iRig relaunches apps and ports on its own, without asking — including mid-song. "
                  + "Leave this off on stage."),
                Pref.autofix, false, warning: true, { self.maybeAutoFix() }),
        ]))

        let screens = NSViewController()
        screens.view = settingsPage([settingsScreenSection()])
        let screensTab = NSTabViewItem(viewController: screens)
        screensTab.label = T("tab.screens", "Screens")
        screensTab.image = NSImage(systemSymbolName: "display.2", accessibilityDescription: nil)
        tabs.addTabViewItem(screensTab)

        let w = NSWindow(contentViewController: tabs)
        w.title = T("settings.title", "iRig Settings")
        w.isReleasedWhenClosed = false
        // .resizable : la largeur venait de `fittingSize`, qui sous-estime un NSGridView
        // imbriqué dans un NSStackView — d'où du texte rogné à droite dès qu'un libellé
        // s'allonge. Rester redimensionnable évite que le prochain libellé un peu long
        // re-produise le bug.
        w.styleMask.insert(.resizable)
        w.styleMask.remove(.miniaturizable)   // une fenêtre de réglages ne se réduit pas
        return w
    }

    /// Une page d'onglet : les vues empilées, avec les marges des Réglages macOS.
    private func settingsPage(_ views: [NSView]) -> NSView {
        // Cale flexible en dernier : la fenêtre prend la hauteur de l'onglet le PLUS haut,
        // et sans cette cale le NSGridView se dilate pour occuper le surplus — d'où des
        // trous entre les lignes du plus court. La cale absorbe tout l'excédent, les
        // réglages restent collés en haut, quel que soit l'onglet.
        let filler = NSView()
        filler.setContentHuggingPriority(.init(1), for: .vertical)
        let stack = NSStackView(views: views + [filler])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 20
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 20, right: 24)
        for v in views { v.setContentHuggingPriority(.required, for: .vertical) }
        // Plancher de largeur : la largeur d'enroulement des explications (330) + la colonne
        // des icônes + celle de l'interrupteur + les marges.
        stack.widthAnchor.constraint(greaterThanOrEqualToConstant: 470).isActive = true
        return stack
    }

    private func settingsTab(_ title: String, _ symbol: String, _ rows: [Row]) -> NSTabViewItem {
        let vc = NSViewController()
        vc.view = settingsPage([settingsSection(rows)])
        let item = NSTabViewItem(viewController: vc)
        item.label = title
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        return item
    }

    /// One settings row. `hint` is the grey second line that says what the switch actually
    /// DOES — a toggle whose label only names a UI element ("Panneau déplié sous la
    /// pastille") tells you where it applies but not what changes, so nobody can predict
    /// the effect without flipping it. `warning: true` paints the row orange with a ⚠️:
    /// reserved for switches that can act on the rig on their own.
    private struct Row {
        let icon: String, label: String, hint: String, key: String, def: Bool
        let warning: Bool, onChange: () -> Void
        init(_ icon: String, _ label: String, _ hint: String, _ key: String, _ def: Bool,
             warning: Bool = false, _ onChange: @escaping () -> Void) {
            self.icon = icon; self.label = label; self.hint = hint; self.key = key
            self.def = def; self.warning = warning; self.onChange = onChange
        }
    }

    /// Plus de titre de section : l'onglet le porte déjà. Le répéter au-dessus de la
    /// grille ferait doublon avec le libellé de l'onglet sélectionné.
    private func settingsSection(_ rows: [Row]) -> NSView {
        let box = NSStackView(); box.orientation = .vertical; box.alignment = .leading; box.spacing = 10
        let grid = NSGridView(); grid.translatesAutoresizingMaskIntoConstraints = false
        grid.rowSpacing = 16; grid.columnSpacing = 14
        for row in rows {
            // Colonne d'icônes : un symbole par réglage, en pastille teintée façon Réglages
            // système. Ce n'est pas décoratif — c'est le repère qu'on retrouve d'un coup
            // d'œil quand on revient changer UN réglage précis, sans relire les libellés.
            let tint: NSColor = row.warning ? .systemOrange : .controlAccentColor
            let badge = NSView()
            badge.wantsLayer = true
            badge.layer?.cornerRadius = 7
            badge.layer?.backgroundColor = tint.withAlphaComponent(0.16).cgColor
            badge.translatesAutoresizingMaskIntoConstraints = false
            let glyph = NSImageView()
            let cfg = NSImage.SymbolConfiguration(pointSize: 15, weight: .medium)
                .applying(.init(paletteColors: [tint]))
            glyph.image = NSImage(systemSymbolName: row.icon, accessibilityDescription: row.label)?
                .withSymbolConfiguration(cfg)
            glyph.translatesAutoresizingMaskIntoConstraints = false
            badge.addSubview(glyph)
            NSLayoutConstraint.activate([
                badge.widthAnchor.constraint(equalToConstant: 30),
                badge.heightAnchor.constraint(equalToConstant: 30),
                glyph.centerXAnchor.constraint(equalTo: badge.centerXAnchor),
                glyph.centerYAnchor.constraint(equalTo: badge.centerYAnchor),
            ])

            // Le ⚠️ dans le libellé ferait doublon avec la pastille orange : la couleur du
            // titre et celle de l'icône disent déjà « attention ».
            let title = NSTextField(labelWithString: row.label)
            if row.warning { title.textColor = .systemOrange }
            let hint = NSTextField(labelWithString: row.hint)
            hint.font = .systemFont(ofSize: 11); hint.textColor = .secondaryLabelColor
            // The hint wraps rather than stretching the window: these sentences are longer
            // than the labels, and an un-wrapped one is what pushes the content off the
            // right edge.
            hint.lineBreakMode = .byWordWrapping
            hint.preferredMaxLayoutWidth = 330
            let text = NSStackView(views: [title, hint])
            text.orientation = .vertical; text.alignment = .leading; text.spacing = 2

            let sw = PrefSwitch(); sw.key = row.key; sw.onChange = row.onChange
            sw.state = Pref.on(row.key, default: row.def) ? .on : .off
            sw.target = self; sw.action = #selector(switchToggled(_:))
            // yPlacement est porté par la LIGNE, pas par la colonne : sans ça l'interrupteur
            // se cale en haut du bloc titre+hint au lieu d'être centré en face.
            grid.addRow(with: [badge, text, sw]).yPlacement = .center
        }
        grid.column(at: 0).xPlacement = .center
        grid.column(at: 2).xPlacement = .trailing
        box.addArrangedSubview(grid)
        return box
    }

    private func settingsScreenSection() -> NSView {
        let box = NSStackView(); box.orientation = .vertical; box.alignment = .leading; box.spacing = 10
        let grid = NSGridView(); grid.rowSpacing = 12; grid.columnSpacing = 24
        let l = NSTextField(labelWithString: T("screens.showOn", "Show on"))
        let pop = NSPopUpButton()
        pop.addItems(withTitles: [T("screens.main", "Main screen"), T("screens.all", "All screens")])
        pop.selectItem(at: (UserDefaults.standard.string(forKey: Pref.screenMode) ?? "main") == "all" ? 1 : 0)
        pop.target = self; pop.action = #selector(screenPopup(_:))
        grid.addRow(with: [l, pop])
        box.addArrangedSubview(grid)
        return box
    }

    @objc func switchToggled(_ sw: PrefSwitch) {
        Pref.set(sw.key, sw.state == .on)
        sw.onChange?()
    }
    @objc func screenPopup(_ p: NSPopUpButton) {
        UserDefaults.standard.set(p.indexOfSelectedItem == 1 ? "all" : "main", forKey: Pref.screenMode)
        applyOverlay()
    }

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

    // ---- Menu helper -------------------------------------------------------
    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: ""); mi.target = self; menu.addItem(mi)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
let delegate = Delegate()
app.delegate = delegate
app.run()
