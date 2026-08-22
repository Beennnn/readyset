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
//     Its buttons: fix-all, open the dashboard, and the details — which pop THE menu, the
//     same one the menu-bar icon carries (problem list + per-problem 🔧 fix via POST
//     /api/fix, then the actions). Un panneau translucide tenait ce rôle jusqu'au
//     2026-08-19 : illisible sur fond clair, et une seconde surface à tenir à jour en
//     parallèle du menu. Un menu natif est opaque partout et n'existe qu'en un exemplaire.
//   • One notification on change — a single silent banner the moment the status worsens
//     into a problem (never repeats; stays in Notification Center until dismissed).
//
// Only the small pill catches clicks; the border and the rest of the screen stay
// click-through, so nothing is ever blocked mid-gig.

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

struct Problem {
    let key, label, status, detail, glyph: String
    let remedy: String?
    /// Les sous-éléments d'un check composite (le soundcheck et ses gestes), tels que
    /// `/api/state` les donne. Vide pour un check ordinaire. L'icône illustre le geste
    /// à faire et se lit avant le mot ; elle peut être absente.
    let parts: [(name: String, ok: Bool, icon: String)]
    /// Le remède ne fait qu'OUVRIR la porte — le geste reste humain (l'autorisation
    /// d'accessibilité s'accorde dans les Réglages Système, et rien d'autre ne peut la
    /// donner). Un remède pareil réussit toujours et laisse le check rouge : le compter
    /// parmi « ce que le bouton sait régler » serait un mensonge de plus au moment où
    /// on cherche justement à savoir ce qui restera à faire.
    let manual: Bool
}

// ---------------------------------------------------------------------------
// Preferences (which alert mechanisms are enabled) — persisted.
// ---------------------------------------------------------------------------
enum Pref {
    static let glyph = "pref.glyphColor", border = "pref.edgeBorder"
    static let pill = "pref.floatingPill"
    static let notify = "pref.notifyOnChange"
    static let warnings = "pref.showWarnings", autofix = "pref.autoFix"
    /// Coupure d'alimentation du Stream Deck — OFF par défaut, et c'est délibéré : le
    /// port se désigne par un identifiant (« 32-2 2 ») dérivé de l'énumération USB, qui
    /// change dès qu'on rebranche ailleurs. Constaté le 2026-08-19 : le hub est passé du
    /// dock à un port direct du Mac, et la config figée une heure plus tôt ne désignait
    /// plus rien. Une entrée qui coupe une alimentation ne doit pas s'offrir tant que sa
    /// cible n'a pas été confirmée.
    static let streamDeck = "pref.streamDeckPower"
    static let screenMode = "pref.screenMode"      // "main" (default) | "all" | "custom"
    static let screenIDs = "pref.screenIDs"        // display IDs for "custom"
    static func on(_ key: String, default def: Bool) -> Bool {
        let d = UserDefaults.standard
        return d.object(forKey: key) == nil ? def : d.bool(forKey: key)
    }
    static func set(_ key: String, _ v: Bool) { UserDefaults.standard.set(v, forKey: key) }
}

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
// Floating pill view (clickable window): summary + fix-all, dashboard, details buttons.
// ---------------------------------------------------------------------------
// The floating status BAR: a rounded, status-coloured pill holding a summary label plus
// distinct clickable buttons (fix-all, open-web, details → the menu) — the delegate fills
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
    var current: RigStatus = .ok
    var curWarns = 0, curFails = 0
    /// Le mode DEMANDÉ (auto | live | studio) — c'est bien le demandé et non le résolu :
    /// « Auto » doit rester coché quand il choisit studio tout seul, sinon le menu laisse
    /// croire qu'on a figé le mode à la main.
    var requestedMode = "auto"
    /// Mode EFFECTIF rendu par /api/state, à ne pas confondre avec `requestedMode` : en
    /// « auto » le rig résout lui-même vers live ou studio, donc le mode DEMANDÉ ne dit
    /// pas où on se trouve. `nil` = moteur injoignable, donc mode non prouvé.
    var effectiveMode: String?
    /// Alimentation du port qui porte le Stream Deck. `nil` = pas d'état prouvé : soit
    /// sd-power est absent, soit ses ports ne sont pas figés, soit l'un d'eux ne répond
    /// plus. Dans les trois cas on n'offre aucune action plutôt que d'en offrir une qui
    /// échouerait en silence.
    var streamDeckPowered: Bool?
    /// Le bouton des Réglages, gardé pour pouvoir réécrire son libellé à chaque sondage.
    private var sdButton: NSButton?
    var problems: [Problem] = []
    var lastFixAttempt: [String: Date] = [:]     // auto-fix throttle: don't re-fire a key within 60 s
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
        let t = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
            // Sondé au même rythme plutôt qu'à l'ouverture du menu : `fill(_:)` reconstruit
            // tout d'un bloc et de façon synchrone, il ne peut donc pas attendre un appel
            // externe. Le coût est un uhubctl toutes les 5 s, mesuré à 0,1 s.
            self?.refreshStreamDeck()
        }
        RunLoop.main.add(t, forMode: .common); timer = t

        // Test/debug hook: RIG_SETTINGS=1 opens the Settings window at launch (for screenshots).
        if ProcessInfo.processInfo.environment["RIG_SETTINGS"] != nil { showSettings() }
    }

    // ---- Overlay windows (border + pill per screen) ------------------------
    @objc func rebuildOverlays() {
        borders.forEach { $0.win.orderOut(nil) }; borders.removeAll()
        pills.forEach { $0.win.orderOut(nil) }; pills.removeAll()
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
        }
        applyOverlay()
    }

    private func configureFloatingPanel(_ pw: NSPanel) {
        pw.isOpaque = false; pw.backgroundColor = .clear; pw.hasShadow = true
        pw.level = .screenSaver; pw.isFloatingPanel = true; pw.becomesKeyOnlyIfNeeded = true
        pw.hidesOnDeactivate = false
        pw.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    }

    private func shorten(_ s: String, _ n: Int) -> String {
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.count <= n ? t : String(t.prefix(n - 1)) + "…"
    }

    /// Largeur visée pour une ligne de la section « problèmes », en caractères. Au-delà,
    /// macOS élargit le menu jusqu'à sa limite puis tronque — et c'est la fin de la ligne,
    /// donc l'explication, qui saute. Ce qui ne rentre pas passe en info-bulle.
    private let menuTitleBudget = 56
    /// Range des libellés courts sur le moins de lignes possible sans dépasser `width`
    /// caractères. Sert aux gestes du soundcheck : les huit tiennent en deux lignes au
    /// lieu de huit, et le menu ne se déroule plus sur tout l'écran pour deux mots par
    /// ligne. La largeur est calée sur la ligne du check juste au-dessus (~75 caractères,
    /// titre + décompte + icônes) : au-delà, ce sont ces lignes-ci qui décideraient de la
    /// largeur du menu. Le compte de caractères vaut ce qu'il vaut avec une police
    /// proportionnelle — on ne cherche pas l'alignement, juste à ne pas déborder.
    private func packed(_ items: [String], width: Int) -> [String] {
        var rows: [String] = []
        var cur = ""
        for it in items {
            if cur.isEmpty { cur = it }
            else if cur.count + 3 + it.count <= width { cur += "   " + it }
            else { rows.append(cur); cur = it }
        }
        if !cur.isEmpty { rows.append(cur) }
        return rows
    }

    // ---- Poll --------------------------------------------------------------
    func refresh() {
        guard let u = URL(string: stateURL) else { return }
        var req = URLRequest(url: u); req.timeoutInterval = 4; req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { [weak self] data, _, err in
            var status = RigStatus.unreachable; var warns = 0, fails = 0; var probs: [Problem] = []
            var wantedMode = "auto"
            var liveMode: String?
            if err == nil, let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let s = obj["status"] as? String { status = RigStatus.parse(s) }
                wantedMode = (obj["requested"] as? String) ?? "auto"
                liveMode = obj["mode"] as? String
                warns = (obj["warns"] as? NSNumber)?.intValue ?? 0
                fails = (obj["fails"] as? NSNumber)?.intValue ?? 0
                if let items = obj["items"] as? [[String: Any]] {
                    for it in items {
                        let st = (it["status"] as? String) ?? "ok"
                        guard st == "fail" || st == "warn" else { continue }
                        let parts = (it["parts"] as? [[String: Any]] ?? []).map {
                            (name: ($0["name"] as? String) ?? "?",
                             ok: ($0["ok"] as? Bool) ?? false,
                             icon: ($0["icon"] as? String) ?? "")
                        }
                        probs.append(Problem(
                            key: (it["key"] as? String) ?? "", label: (it["label"] as? String) ?? "?",
                            status: st, detail: (it["detail"] as? String) ?? "",
                            glyph: (it["glyph"] as? String) ?? "•", remedy: it["remedy"] as? String,
                            parts: parts, manual: (it["manual"] as? Bool) ?? false))
                    }
                    probs.sort { (($0.status == "fail" ? 0 : 1), $0.label) < (($1.status == "fail" ? 0 : 1), $1.label) }
                }
            }
            DispatchQueue.main.async {
                self?.requestedMode = wantedMode
                // Repasse à nil quand le moteur n'a pas répondu : sans ça un dernier mode
                // connu périmé autoriserait une coupure sur une information morte.
                self?.effectiveMode = liveMode
                self?.apply(status, warns: warns, fails: fails, problems: probs)
            }
        }.resume()
    }

    func apply(_ status: RigStatus, warns: Int, fails: Int, problems: [Problem]) {
        let old = current
        current = status; curWarns = warns; curFails = fails; self.problems = problems

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
                let x = screen.frame.minX + (screen.frame.width - w) / 2
                let y = screen.frame.maxY - h - 34
                p.win.setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
                p.win.orderFrontRegardless()
            } else { p.win.orderOut(nil) }
        }
    }

    // Fill the status bar: soft gradient + summary + fix-all / web, then the details button
    // at the far right (it pops the menu). Keeps red, but richer than one flat aggressive tone.
    private func populatePill(_ bar: PillBar) {
        // Neutral dark bar (was full red). Severity shows only as a small coloured dot now.
        bar.setGradient(NSColor(white: 0.19, alpha: 0.96), NSColor(white: 0.11, alpha: 0.96))
        bar.stack.arrangedSubviews.forEach { $0.removeFromSuperview() }

        bar.stack.addArrangedSubview(dot(current.overlayColor))     // red/orange accent
        let n = curFails + curWarns
        let summary = NSTextField(labelWithString: String(format: T("pill.summary", "Rig — %d to check"), n))
        summary.font = .systemFont(ofSize: 14, weight: .bold); summary.textColor = .white
        bar.stack.addArrangedSubview(summary)

        // Le MÊME geste unique que le menu (2026-08-22) : deux surfaces qui proposaient
        // deux verbes et deux portées, c'était une hésitation de plus au moment où il en
        // faut zéro. La barre ne s'affiche que quand ça cloche — la condition est acquise.
        bar.stack.addArrangedSubview(barButton("wand.and.stars", "Tout préparer",
                                               "Lancer les apps, appliquer tous les correctifs, re-vérifier",
                                               #selector(prepareAll), green: true))
        bar.stack.addArrangedSubview(barButton("arrow.up.forward.square", nil,
                                               T("pill.openWeb", "Open the web dashboard (details)"), #selector(open)))
        bar.stack.addArrangedSubview(barButton("list.bullet", nil,
                                               T("pill.details", "Show the details"),
                                               #selector(showDetails(_:))))
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

    // ---- Interactions ------------------------------------------------------
    /// Le détail, au clic sur la barre flottante : le MÊME menu que l'icône de la barre
    /// de menus. Avant, c'était un panneau translucide posé sous la barre — illisible sur
    /// un fond clair, et une surface de plus à maintenir en parallèle du menu. Un menu
    /// natif est opaque, lisible partout, et porte déjà chaque correctif ; le dashboard
    /// reste à un cran, en tête de liste.
    @objc func showDetails(_ sender: NSButton) {
        let menu = NSMenu()
        fill(menu)
        // Point en coordonnées ÉCRAN (`in: nil`) : passer la vue laisse macOS caler la
        // liste sur le point et déborder l'en-tête au-dessus du bord haut — la barre est
        // collée en haut de l'écran — d'où une flèche de défilement et un titre invisible.
        guard let win = sender.window else { return }
        let r = win.convertToScreen(sender.convert(sender.bounds, to: nil))
        menu.popUp(positioning: nil, at: NSPoint(x: r.minX, y: r.minY - 6), in: nil)
    }

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

    private func runFix(_ key: String) {
        guard !key.isEmpty, let u = URL(string: fixURL) else { return }
        var req = URLRequest(url: u); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["key": key])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refresh() }
        }.resume()
    }

    // ---- The menu — rebuilt each open, shared by both surfaces --------------
    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === item.menu else { return }
        fill(menu)
    }

    /// Le contenu du menu, construit une seule fois pour l'icône de la barre de menus ET
    /// pour la barre flottante : deux listes séparées finiraient par diverger, et il
    /// faudrait se rappeler laquelle des deux sait faire quoi — exactement ce qu'on veut
    /// éviter un soir de concert.
    private func fill(_ menu: NSMenu) {
        menu.removeAllItems()
        // Validation explicite : ouvert depuis la barre flottante (une fenêtre qui ne
        // devient jamais active), l'auto-activation de macOS grise des entrées pourtant
        // valides — le sous-menu Mode, entre autres. Nos cibles sont toutes posées à la
        // main, donc on décide nous-mêmes : seuls l'en-tête et le « tout est ok » sont
        // désactivés, plus bas.
        menu.autoenablesItems = false
        let head = NSMenuItem(title: "🎹 Rig — \(current.label)", action: nil, keyEquivalent: "")
        head.isEnabled = false; menu.addItem(head)
        menu.addItem(.separator())

        // Le dashboard en premier (2026-08-20) : c'est la sortie vers TOUT le reste — les
        // détails qu'une ligne de menu tronque, le journal, les courbes. Une seule entrée
        // pour la fenêtre web, d'ailleurs : le journal des actions vit dans la même page,
        // donc deux lignes ouvraient exactement la même URL.
        add(menu, T("menu.dashboard", "🌐 Open the dashboard"), #selector(open))
        menu.addItem(.separator())

        // Les actions d'abord, le détail des problèmes en bas (2026-08-20) : sa longueur
        // varie — huit gestes de soundcheck manquants, et il poussait les actions hors de
        // portée. Sous elles, chaque action garde la même place d'un soir à l'autre.
        // Réglages et Quitter restent tout en dernier, à la place que macOS leur donne
        // partout ailleurs : le détail se glisse AU-DESSUS d'eux, pas après.
        // Le menu porte les MÊMES actions que la barre du dashboard, en sections : d'abord
        // le mode, puis LA seule action à connaître, puis ce qui cloche, puis les gestes
        // ponctuels qui y répondent. Ce découpage est le même dans les deux surfaces ;
        // c'est ce qui permet de ne pas avoir à se rappeler où est quoi.
        let mode = NSMenuItem(title: T("menu.mode", "Mode"), action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for (title, sel, key) in [(T("mode.auto", "🅰 Auto"), #selector(setModeAuto), "auto"),
                                  (T("mode.live", "🎤 Live"), #selector(setModeLive), "live"),
                                  (T("mode.studio", "🎧 Studio"), #selector(setModeStudio), "studio")] {
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            mi.target = self; mi.state = (requestedMode == key) ? .on : .off
            sub.addItem(mi)
            // « Auto » coché ne dit pas OÙ il a atterri, et c'est pourtant la seule chose
            // qui compte avant de brancher : le rig résout tout seul vers live ou studio.
            // La ligne se colore aux mêmes teintes que le dashboard (bleu = studio, ambre
            // = live) pour que la couleur veuille dire la même chose sur les deux surfaces.
            if key == "auto" && requestedMode == "auto" {
                sub.addItem(resolvedModeItem())
            }
        }
        // Activation explicite : validation manuelle oblige, un parent de sous-menu sans
        // action resterait grisé — et macOS masque alors sa flèche.
        mode.submenu = sub; mode.isEnabled = true; menu.addItem(mode)

        // CE QUI RESTE À PRÉPARER D'ABORD, le geste qui le fera ensuite (2026-08-22) :
        // une ligne par problème, son correctif dans le titre — cliquer la ligne le lance.
        // Le bouton était au-dessus ; on le lisait donc avant de savoir s'il y avait lieu
        // de le cliquer, et il restait proposé sur un rig déjà prêt.
        menu.addItem(.separator())
        let probs = Pref.on(Pref.warnings, default: true) ? problems : problems.filter { $0.status == "fail" }
        if probs.isEmpty {
            let none = NSMenuItem(title: problems.isEmpty ? "Tout est ok 🎉"
                                        : T("panel.noBlockers", "No blockers (warnings hidden)"),
                                  action: nil, keyEquivalent: "")
            none.isEnabled = false; menu.addItem(none)
        }
        // Les checks composites (le soundcheck) descendent en bas de la liste : ils
        // ouvrent une section à eux, et une section au milieu couperait les autres en deux.
        for p in probs.filter({ $0.parts.isEmpty }) + probs.filter({ !$0.parts.isEmpty }) {
            let missing = p.parts.filter { !$0.ok }
            // La ligne ne porte plus QUE le nom du check (2026-08-20) : le détail la
            // faisait déborder, et macOS tronquait — en coupant justement la fin, donc
            // l'explication. Il vit désormais dans l'info-bulle SEULE, où rien ne le
            // tronque. Les icônes des manquants sont parties avec : 24 caractères pour
            // répéter la liste packée qui les donne AVEC leur nom, deux lignes plus bas.
            var title = "\(p.status == "fail" ? "🔴" : "🟠") \(shorten(p.label, menuTitleBudget - 4))"
            // 🔧 = le bouton sait le faire ; ✋ = la ligne ouvre la porte, le geste reste
            // à toi. Deux symboles pour deux natures d'action, dans la liste comme dans
            // le récapitulatif juste en dessous — ils se comptent l'un l'autre.
            if let rem = p.remedy { title += "   \(p.manual ? "✋" : "🔧") \(shorten(rem, 24))" }
            // Sans correctif, la ligne ouvre le dashboard plutôt que d'être inerte : une
            // ligne sans action, macOS la grise — or c'est la lisibilité qu'on vient chercher.
            if !missing.isEmpty { menu.addItem(.separator()) }
            let mi = NSMenuItem(title: title,
                                action: p.remedy == nil ? #selector(open) : #selector(applyFix(_:)),
                                keyEquivalent: "")
            mi.target = self; mi.representedObject = p.key
            // L'info-bulle ne tronque rien et va à la ligne : c'est là que vit le texte
            // complet du moteur, « → les jouer une fois… » compris.
            mi.toolTip = p.detail.isEmpty ? p.label : "\(p.label)\n\(p.detail)"
            menu.addItem(mi)
            // Un geste manquant n'est pas une panne : c'est une preuve qui manque, et rien
            // à cliquer — d'où des lignes indentées sous leur check, sans action. Plusieurs
            // par ligne : à huit gestes, une ligne chacun faisait à lui seul la moitié du
            // menu, pour deux mots par ligne.
            for row in packed(missing.map { "\($0.icon.isEmpty ? "◦" : $0.icon) \($0.name)" }, width: menuTitleBudget) {
                let sub = NSMenuItem(title: row, action: nil, keyEquivalent: "")
                sub.indentationLevel = 1; sub.isEnabled = false
                sub.toolTip = T("menu.gestureHint", "Play it once — the check is passive, nothing to tick")
                menu.addItem(sub)
            }
        }
        // UN SEUL geste, et seulement s'il y a lieu (2026-08-22, demande de Benoît).
        // « Tout préparer » lance les apps PUIS applique tous les correctifs : « Lancer
        // tous les correctifs » était donc un sous-ensemble du bouton d'à côté — deux
        // noms, deux portées, une seule décision. Fusionnés. Et sur un rig déjà vert, la
        // ligne disparaît : proposer de préparer ce qui est prêt, c'est inviter à un
        // geste qui ne peut que déranger (il relance des apps et range des fenêtres).
        if !problems.isEmpty {
            let auto = problems.filter { $0.remedy != nil && !$0.manual }.count
            let hand = problems.count - auto
            // Ce que le bouton peut VRAIMENT faire, écrit avant qu'on le clique : une
            // partie des lignes rouges ne se règle pas depuis ici (l'autorisation macOS
            // se donne dans les Réglages Système, les gestes du soundcheck se jouent, une
            // lampe éteinte s'allume). Annoncer « tout » sans le dire, c'est la promesse
            // verte du 22/08 qui recouvrait un silence total.
            var recap: [String] = []
            if auto > 0 { recap.append(String(format: T("menu.recap.auto", "🔧 %d fixable here"), auto)) }
            if hand > 0 { recap.append(String(format: T("menu.recap.hand", "✋ %d up to you"), hand)) }
            let line = NSMenuItem(title: recap.joined(separator: "   ·   "), action: nil, keyEquivalent: "")
            line.isEnabled = false; menu.addItem(line)
            add(menu, T("menu.prepare", "✨ Prepare everything"), #selector(prepareAll),
                tip: T("menu.prepare.tip", """
                       Launches the rig apps, opens the set, tidies the windows, then applies \
                       every fix it can — and re-checks everything. The ✋ lines stay yours to do.
                       """))
        }
        menu.addItem(.separator())

        // Les deux gestes ponctuels APRÈS la liste (2026-08-20) : ce sont des réponses à
        // ce qu'on vient d'y lire — « iPhone pas en charge », « une app de trop est
        // ouverte ». Au-dessus, on les lisait avant de savoir s'il y avait lieu de les
        // faire ; en dessous, la main descend de la ligne rouge vers son geste.
        add(menu, T("menu.charge", "🔋 Confirm the iPhone is charging"), #selector(confirmCharge))
        add(menu, T("menu.quitOthers", "🧹 Quit the other apps…"), #selector(quitOthers))
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
        ]))

        tabs.addTabViewItem(settingsTab(T("tab.alerts", "Alerts"), "bell", [
            Row("exclamationmark.triangle", T("alerts.warnings.title", "Show warnings, not just errors"),
                T("alerts.warnings.hint",
                  "Otherwise only blocking errors (red) are listed; orange warnings stay hidden."),
                Pref.warnings, true, { self.applyOverlay() }),
            Row("bell", T("alerts.notify.title", "One notification when the state changes"),
                T("alerts.notify.hint",
                  "A macOS notification when severity gets worse (green → orange → red), not on every check."),
                Pref.notify, false, {}),
            Row("wrench.and.screwdriver", T("alerts.autofix.title", "Fix automatically"),
                T("alerts.autofix.hint",
                  "iRig relaunches apps and ports on its own, without asking — including mid-song. "
                  + "Leave this off on stage."),
                Pref.autofix, false, warning: true, { self.maybeAutoFix() }),
            Row("powerplug", T("alerts.streamDeck.title", "Stream Deck power"),
                T("alerts.streamDeck.hint",
                  "Cuts the USB port carrying the Stream Deck — and everything plugged into "
                  + "it. The port is designated by a number that changes when you move the "
                  + "cable, so re-run `sd-power detect` after replugging. Cutting stays "
                  + "studio-only; the switch enables the polling the button needs."),
                Pref.streamDeck, false, warning: true,
                extra: { self.makeStreamDeckButton() }, { self.refreshStreamDeck() }),
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
        /// Un contrôle POSÉ À GAUCHE de la bascule, pour un réglage qui commande aussi une
        /// action immédiate — l'alimentation du Stream Deck, seul cas à ce jour. Deux
        /// lignes séparées diraient moins bien qu'il s'agit d'une seule chose : la bascule
        /// autorise le pilotage, le bouton l'exerce.
        let extra: (() -> NSView)?
        init(_ icon: String, _ label: String, _ hint: String, _ key: String, _ def: Bool,
             warning: Bool = false, extra: (() -> NSView)? = nil, _ onChange: @escaping () -> Void) {
            self.icon = icon; self.label = label; self.hint = hint; self.key = key
            self.def = def; self.warning = warning; self.extra = extra; self.onChange = onChange
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
            let control: NSView
            if let extra = row.extra {
                let pair = NSStackView(views: [extra(), sw])
                pair.orientation = .horizontal; pair.alignment = .centerY; pair.spacing = 10
                control = pair
            } else {
                control = sw
            }
            grid.addRow(with: [badge, text, control]).yPlacement = .center
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

    // ---- Alimentation du Stream Deck ---------------------------------------
    //
    // Les hubs internes du dock annoncent « ppps » (per-port power switching — couper le
    // +5 V d'un port précis par une commande USB standard). `~/.local/bin/sd-power` envoie
    // cette commande via uhubctl, sur le port qui porte le hub du Stream Deck : tout ce qui
    // est branché derrière tombe avec lui.
    //
    // Exécuté LOCALEMENT, et non par un POST vers /api comme le reste de ce menu. C'est
    // délibéré : le cas où l'on veut le plus sûrement rétablir un Stream Deck coupé est
    // celui où le moteur est à terre — le faire transiter par l'API le rendrait
    // indisponible exactement quand il sert.

    private var sdPowerPath: String { NSHomeDirectory() + "/.local/bin/sd-power" }

    private func sdPower(_ arg: String, done: @escaping (String) -> Void) {
        let exe = sdPowerPath
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            return DispatchQueue.main.async { done("") }
        }
        DispatchQueue.global(qos: .utility).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = [arg]
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
            var out = ""
            do {
                try p.run()
                // Lire AVANT waitUntilExit : attendre la fin du process d'abord
                // interbloquerait les deux dès que la sortie remplit le tampon du tube.
                let d = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                out = String(data: d, encoding: .utf8) ?? ""
            } catch { out = "" }
            DispatchQueue.main.async { done(out) }
        }
    }

    func refreshStreamDeck() {
        // Option éteinte = aucun appel à uhubctl. Le sondage n'est pas gratuit : il touche
        // le bus USB toutes les 5 s, ce qu'on ne fait pas pour une entrée qui ne s'affiche
        // même pas.
        guard Pref.on(Pref.streamDeck, default: false) else {
            streamDeckPowered = nil
            updateStreamDeckButton()
            return
        }
        sdPower("status") { [weak self] out in
            // Sortie vide et « inconnu » mènent au même endroit : aucun état prouvé, donc
            // aucune action offerte. sd-power rend « inconnu » quand une cible de sa config
            // ne répond plus — un port renuméroté, typiquement.
            if out.contains("État : on")       { self?.streamDeckPowered = true }
            else if out.contains("État : off") { self?.streamDeckPowered = false }
            else                               { self?.streamDeckPowered = nil }
            self?.updateStreamDeckButton()
        }
    }

    /// Le bouton PORTE l'état et la permission : pas d'action séparée couper/rallumer,
    /// pas de boîte de dialogue. Il vit dans les Réglages et nulle part ailleurs
    /// (2026-08-20) — le menu se lit en montant sur scène, et couper l'alimentation d'un
    /// port USB n'est pas un geste de scène. La bascule d'à côté commande le sondage ;
    /// sans elle, aucun état n'est connu et le bouton n'a rien à proposer.
    private func makeStreamDeckButton() -> NSView {
        let b = NSButton(title: "", target: self, action: #selector(toggleStreamDeck))
        b.bezelStyle = .rounded
        b.controlSize = .regular
        sdButton = b
        updateStreamDeckButton()
        return b
    }

    /// Rejoué à chaque sondage : la fenêtre Réglages n'est construite qu'une fois, donc
    /// sans ça le bouton garderait le libellé qu'il avait à son ouverture.
    private func updateStreamDeckButton() {
        guard let b = sdButton else { return }
        guard Pref.on(Pref.streamDeck, default: false) else {
            b.title = T("sd.off", "Disabled"); b.isEnabled = false; return
        }
        switch streamDeckPowered {
        case .some(false):
            // Rallumer n'est JAMAIS bloqué : ni hors studio, ni moteur injoignable. Le
            // garde-fou protège le geste risqué, pas le retour à l'état sûr — refuser un
            // rallumage laisserait le Stream Deck mort sans issue.
            b.title = T("sd.restore", "Restore"); b.isEnabled = true
        case .some(true) where effectiveMode == "studio":
            b.title = T("sd.cut", "Cut"); b.isEnabled = true
        case .some(true):
            // Couper en live, c'est perdre le pilotage du set au pire moment. Un mode
            // inconnu compte comme un refus : on ne peut alors PAS prouver qu'on n'est
            // pas en live, et le doute doit pencher du côté qui ne casse pas le concert.
            let m = effectiveMode ?? T("sd.modeUnknown", "mode unknown")
            b.title = T("sd.studioOnly", "Studio only") + " (\(m))"; b.isEnabled = false
        case .none:
            b.title = T("sd.unset", "Run `sd-power detect`"); b.isEnabled = false
        }
    }

    @objc func toggleStreamDeck() {
        // Revalidation au clic : le menu a pu être construit plusieurs secondes plus tôt et
        // le mode bascule tout seul entre-temps. On ne coupe que sur un studio confirmé
        // à cet instant ; sinon on se contente de rafraîchir, et le libellé dira pourquoi.
        guard streamDeckPowered == false || effectiveMode == "studio" else {
            refreshStreamDeck(); return
        }
        // « on »/« off » explicites plutôt que « toggle » : le script relirait l'état de
        // son côté, ce qui rouvrirait la course que la revalidation ci-dessus vient de
        // fermer.
        sdPower(streamDeckPowered == false ? "on" : "off") { [weak self] _ in
            self?.refreshStreamDeck()
        }
    }

    /// La ligne « voici où Auto a atterri ». Désactivée — il n'y a rien à cliquer, le mode
    /// se choisit par les trois entrées au-dessus. La couleur passe par un attributedTitle :
    /// un item désactivé est gris par défaut, et gris se lirait « indisponible » alors que
    /// c'est l'information la plus utile du sous-menu.
    private func resolvedModeItem() -> NSMenuItem {
        // Mêmes teintes que le dashboard (--accent / --warn) : la couleur doit vouloir dire
        // la même chose sur les deux surfaces, sinon elle n'apprend rien.
        let studio = NSColor(red: 0.29, green: 0.62, blue: 1.00, alpha: 1)   // #4a9eff
        let live   = NSColor(red: 0.96, green: 0.73, blue: 0.26, alpha: 1)   // #f4b942
        let text: String, colour: NSColor
        switch effectiveMode {
        case "studio": text = T("mode.resolved", "→ currently") + "  " + T("mode.studio", "🎧 Studio"); colour = studio
        case "live":   text = T("mode.resolved", "→ currently") + "  " + T("mode.live", "🎤 Live");     colour = live
        // Moteur injoignable : le mode n'est pas prouvé. Gris, et c'est juste — là,
        // l'information EST « on ne sait pas ».
        default:       text = T("mode.resolvedUnknown", "→ currently unknown"); colour = .secondaryLabelColor
        }
        let mi = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        mi.attributedTitle = NSAttributedString(string: text, attributes: [
            .foregroundColor: colour,
            .font: NSFont.menuFont(ofSize: NSFont.systemFontSize(for: .small)),
        ])
        mi.indentationLevel = 1
        mi.isEnabled = false
        mi.toolTip = T("mode.resolvedHint", "Auto picked this one — the three entries above force it instead")
        return mi
    }

    // ---- Menu helper -------------------------------------------------------
    /// `tip` : ce que le geste fait vraiment, là où rien ne le tronque — un titre de menu,
    /// macOS le coupe, et c'est toujours la fin qui saute, donc la nuance.
    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector, tip: String? = nil) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: ""); mi.target = self
        mi.toolTip = tip
        menu.addItem(mi)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
let delegate = Delegate()
app.delegate = delegate
app.run()
