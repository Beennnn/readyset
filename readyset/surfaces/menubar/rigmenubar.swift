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
//     /api/fix, then the actions). A translucent panel held that role until
//     2026-08-19: unreadable on a light background, and a second surface to keep in sync
//     alongside the menu. A native menu is opaque everywhere and exists in a single copy.
//   • Flashing alarm — a pulsing red FRAME the moment something breaks AFTER the screen
//     was clean, around a panel that does NOT blink: type icon, what broke, what to do,
//     and a ⚡ button that runs the remedy on the spot (see alarm.swift). Regression-
//     triggered, click-through except on that button, stops when that failure is fixed.
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
/// Adding a language: copy `readyset/surfaces/menubar/Resources/fr.lproj/Localizable.strings` to
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
    /// nil = monochrome template glyph (follows the system's light/dark mode).
    /// `.ok` has been GREEN since 2026-08-18: as a template, "all is well" and "the coloured
    /// glyph is disabled" looked alike stroke for stroke, so a green rig could not be told
    /// apart from a switched-off option. Green is information, not decoration — it says
    /// "checked just now, nothing to fix", which a neutral glyph does not say.
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
    /// The sub-items of a composite check (the soundcheck and its gestures), exactly as
    /// `/api/state` gives them. Empty for an ordinary check. The icon illustrates the
    /// gesture to perform and is read before the word; it may be absent.
    let parts: [(name: String, ok: Bool, icon: String)]
    /// The remedy only OPENS the door — the gesture stays human (the accessibility
    /// permission is granted in System Settings, and nothing else can grant it). Such a
    /// remedy always succeeds and leaves the check red: counting it among "what the
    /// button knows how to settle" would be one more lie at the very moment when one is
    /// trying to find out what will be left to do.
    let manual: Bool
    /// The domain the check belongs to — "System", "Audio", "Soundcheck"…
    /// Comes from the engine, and it is the SAME split as the dashboard's zones: two
    /// surfaces, a single mental map to remember.
    let group: String
    /// KNOCK-ON FAILURE — the UPSTREAM failure that explains this one, when the engine
    /// knows of one (readyset/core/cascade.py). The Stream Deck Plus powers the XL, the
    /// keyboard and the breath: when its cable drops, those three fall with it without
    /// being at fault. `nil` = standalone failure, the one that really needs its own gesture.
    let causedBy: String?
    /// The link, spelled out: "powered by the Stream Deck Plus". It is what says WHERE to
    /// look — a key number sends nobody looking anywhere.
    let causedWhy: String
    /// The failures this one explains, by their label. Non-empty = this is THE cause, and
    /// it goes ahead of everything else: repairing it clears the others in one go.
    let causes: [String]

    var isConsequence: Bool { causedBy != nil }
    var isCause: Bool { !causes.isEmpty }
}

/// The order in which failures are READ — and it is not the order in which they
/// happen.
///
/// Three rules, in this order: the cause first (it explains the others, and its gesture
/// repairs them all), then the blockers, then the alphabet so that two successive polls
/// do not make the list dance before your eyes. Each consequence is then stuck UNDER
/// its cause: between the two, the slightest foreign line breaks the very link one is
/// trying to make visible.
///
/// A consequence whose cause is not in the list (it is orange and warnings are hidden,
/// for instance) becomes an ordinary failure again — without which it would simply and
/// purely vanish from the display.
func orderedByCause(_ probs: [Problem]) -> [Problem] {
    let present = Set(probs.map(\.key))
    let rank: (Problem) -> (Int, Int, String) = {
        ($0.isCause ? 0 : 1, $0.status == "fail" ? 0 : 1, $0.label)
    }
    let heads = probs.filter { $0.causedBy == nil || !present.contains($0.causedBy!) }
                     .sorted { rank($0) < rank($1) }
    var out: [Problem] = []
    for h in heads {
        out.append(h)
        out += probs.filter { $0.causedBy == h.key }.sorted { rank($0) < rank($1) }
    }
    return out
}

// ---------------------------------------------------------------------------
// Preferences (which alert mechanisms are enabled) — persisted.
// ---------------------------------------------------------------------------
enum Pref {
    static let glyph = "pref.glyphColor", border = "pref.edgeBorder"
    static let pill = "pref.floatingPill"
    static let notify = "pref.notifyOnChange"
    static let warnings = "pref.showWarnings", autofix = "pref.autoFix"
    /// The flashing when something BREAKS while the screen was clean (alarm.swift).
    static let alarm = "pref.alarmFlash"
    /// Stream Deck power cut — OFF by default, and that is deliberate: the port is
    /// designated by an id ("32-2 2") derived from the USB enumeration, which changes as
    /// soon as you replug elsewhere. Observed on 2026-08-19: the hub moved from the dock
    /// to a port directly on the Mac, and the config frozen an hour earlier designated
    /// nothing any more. An entry that cuts a power supply must not offer itself as long
    /// as its target has not been confirmed.
    static let streamDeck = "pref.streamDeckPower"
    /// Pause: the app stays alive but watches nothing and displays nothing any more.
    /// Persisted on purpose — the app cannot be quit, so launchd relaunches it; a pause
    /// held in memory alone would be cancelled by the first restart.
    static let paused = "pref.paused"
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

/// NSTabViewController resets the window title to the current tab's label on every
/// switch — hence an "Untitled" window on first display, then a title that changes as
/// you navigate. We set it back afterwards to keep a stable title.
final class SettingsTabController: NSTabViewController {
    // The first tab is selected during construction, while `view.window` is still nil:
    // the resize below would therefore never apply on opening, and the window would keep
    // its initial size. We replay it when the view appears.
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
        win.title = T("settings.title", "readyset Settings")
        // The window would otherwise keep the height of the tallest tab, leaving a big gap
        // under the shorter tabs. We resize it to the real content, keeping the TOP edge
        // fixed: `setFrame` anchors at the bottom, so without compensating the origin the
        // window would seem to jump upwards on every tab change.
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
    /// Forgiving index: `selectedTabViewItemIndex` is -1 as long as no tab is selected,
    /// which would crash a direct access.
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
    var alarms: [(win: NSPanel, view: AlarmView)] = []
    /// What has broken since the last clean slate — see alarm.swift.
    var alarm = AlarmState()
    var pills: [(win: NSPanel, bar: PillBar)] = []
    var current: RigStatus = .ok
    var curWarns = 0, curFails = 0
    /// The REQUESTED mode (auto | live | studio) — the requested one, not the resolved one:
    /// "Auto" must stay ticked when it picks studio by itself, otherwise the menu suggests
    /// the mode has been pinned by hand.
    var requestedMode = "auto"
    /// EFFECTIVE mode returned by /api/state, not to be confused with `requestedMode`: in
    /// "auto" the rig resolves to live or studio by itself, so the REQUESTED mode does not
    /// say where we are. `nil` = engine unreachable, hence mode not proven.
    var effectiveMode: String?
    /// Power on the port carrying the Stream Deck. `nil` = no proven state: either
    /// sd-power is missing, or its ports are not pinned down, or one of them no longer
    /// answers. In all three cases we offer no action rather than offering one that would
    /// fail in silence.
    var streamDeckPowered: Bool?
    /// The Settings button, kept so its label can be rewritten on every poll.
    private var sdButton: NSButton?
    var problems: [Problem] = []
    var lastFixAttempt: [String: Date] = [:]     // auto-fix throttle: don't re-fire a key within 60 s
    var settingsWin: NSWindow?                    // the classic Settings window

    /// The engine's address. Overridable by RIG_URL — that is what makes an ALERT
    /// testable: point it at a fake engine that goes from green to red on demand, instead
    /// of having to break the real rig to see whether the signal fires.
    let url = ProcessInfo.processInfo.environment["RIG_URL"] ?? "http://127.0.0.1:8765"
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
            // Polled at the same rate rather than on menu opening: `fill(_:)` rebuilds
            // everything in one synchronous block, so it cannot wait for an external call.
            // The cost is one uhubctl every 5 s, measured at 0.1 s.
            self?.refreshStreamDeck()
        }
        RunLoop.main.add(t, forMode: .common); timer = t

        // Test/debug hook: RIG_SETTINGS=1 opens the Settings window at launch (for screenshots).
        if ProcessInfo.processInfo.environment["RIG_SETTINGS"] != nil { showSettings() }
    }

    // ---- Overlay windows (border + pill per screen) ------------------------
    @objc func rebuildOverlays() {
        borders.forEach { $0.win.orderOut(nil) }; borders.removeAll()
        alarms.forEach { $0.win.orderOut(nil) }; alarms.removeAll()
        pills.forEach { $0.win.orderOut(nil) }; pills.removeAll()
        for screen in NSScreen.screens {
            let bw = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
            bw.isOpaque = false; bw.backgroundColor = .clear; bw.hasShadow = false
            bw.ignoresMouseEvents = true; bw.level = .screenSaver
            bw.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            let bv = BorderView(frame: NSRect(origin: .zero, size: screen.frame.size))
            bw.contentView = bv; borders.append((bw, bv))

            // The alarm gets its OWN window rather than enriching the border: the frame
            // flashes there, the panel stays fixed there, and it carries a button — three
            // things the border does not do.
            //
            // A NON-ACTIVATING PANEL, like the pill, and for the same reason: clicking
            // "Fix" must NOT push Ableton to the background. An ordinary window would have
            // activated the app on every click — and on stage, losing Ableton's foreground
            // costs more than the failure being repaired.
            //
            // `ignoresMouseEvents` stays FALSE (the button must receive its click): it is
            // the view's `hitTest` that returns `nil` everywhere else, so anything that is
            // not the button passes through the glass as if there were nothing there.
            let aw = NSPanel(contentRect: screen.frame, styleMask: [.nonactivatingPanel, .borderless],
                             backing: .buffered, defer: false)
            configureFloatingPanel(aw)
            aw.hasShadow = false
            let av = AlarmView(frame: NSRect(origin: .zero, size: screen.frame.size))
            av.card.onFix = { [weak self] in self?.fixAlarm() }
            av.card.onStop = { [weak self] in self?.silenceAlarm() }
            // Five minutes: enough to finish the current song and the next one, too little
            // for a failure to be forgotten until the end of the set.
            av.card.onSnooze = { [weak self] in self?.snoozeAlarm(300) }
            av.card.onConfirmCharge = { [weak self] in self?.confirmCharge() }
            aw.contentView = av; alarms.append((aw, av))

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

    /// Target width for a line of the "problems" section, in characters. Beyond it, macOS
    /// widens the menu up to its limit then truncates — and it is the end of the line, so
    /// the explanation, that is dropped. What does not fit moves into the tooltip.
    private let menuTitleBudget = 56
    /// Packs short labels onto as few lines as possible without exceeding `width`
    /// characters. Used for the soundcheck gestures: the eight of them fit on two lines
    /// instead of eight, and the menu no longer unrolls down the whole screen for two
    /// words per line. The width is set on the check line just above (~75 characters,
    /// title + count + icons): beyond that, these very lines would be the ones deciding
    /// the menu's width. A character count is worth what it is worth with a proportional
    /// font — we are not after alignment, only after not overflowing.
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
    var paused: Bool { Pref.on(Pref.paused, default: false) }

    /// Enter or leave pause. On entering we TEAR DOWN what is on screen instead of
    /// waiting for the next round: without that the border and the pill would stay
    /// displayed, and the pause would not keep its promise — to show nothing any more.
    @objc func togglePause() {
        Pref.set(Pref.paused, !paused)
        if paused { alarm.silence() }
        refresh()
    }

    func refresh() {
        if paused {
            current = .unreachable
            applyGlyph()
            applyOverlay()
            return
        }
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
                        let causes = (it["causes"] as? [[String: Any]] ?? [])
                            .compactMap { $0["label"] as? String }
                        probs.append(Problem(
                            key: (it["key"] as? String) ?? "", label: (it["label"] as? String) ?? "?",
                            status: st, detail: (it["detail"] as? String) ?? "",
                            glyph: (it["glyph"] as? String) ?? "•", remedy: it["remedy"] as? String,
                            parts: parts, manual: (it["manual"] as? Bool) ?? false,
                            group: (it["group"] as? String) ?? "",
                            causedBy: it["caused_by"] as? String,
                            causedWhy: (it["caused_why"] as? String) ?? "",
                            causes: causes))
                    }
                    probs = orderedByCause(probs)
                }
            }
            DispatchQueue.main.async {
                self?.requestedMode = wantedMode
                // Back to nil when the engine did not answer: without that a stale
                // last-known mode would authorise a power cut on dead information.
                self?.effectiveMode = liveMode
                self?.apply(status, warns: warns, fails: fails, problems: probs)
            }
        }.resume()
    }

    func apply(_ status: RigStatus, warns: Int, fails: Int, problems: [Problem]) {
        let old = current
        current = status; curWarns = warns; curFails = fails; self.problems = problems

        // What the alarm watches: blockers always, warnings only if they were asked for.
        // The same setting therefore drives both surfaces — hiding the oranges in the list
        // and then taking them full screen would be a contradiction, and it is the most
        // conspicuous surface that would lose the trust.
        let watched = problems.filter { $0.status == "fail" || Pref.on(Pref.warnings, default: true) }
        alarm.update(status: status, problems: watched)

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
        if paused {
            b.toolTip = T("menu.pausedTip", "Rig paused — click to resume")
            if let base = baseSymbol {
                let img = base.withSymbolConfiguration(
                    .init(paletteColors: [NSColor.tertiaryLabelColor])) ?? base
                img.isTemplate = false; b.image = img; b.title = ""
            } else { b.title = "🎹⏸" }
            return
        }
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
        // While paused, none of the three displays comes out — that is what pause means.
        let showBorder = !paused && Pref.on(Pref.border, default: true) && current.showsOverlay
        let showPill = !paused && Pref.on(Pref.pill, default: true) && current.showsOverlay
        let showAlarm = !paused && Pref.on(Pref.alarm, default: true) && alarm.firing
        for (i, b) in borders.enumerated() {
            b.view.status = current
            if showBorder && allowedScreen(i) { b.win.orderFrontRegardless() } else { b.win.orderOut(nil) }
        }
        for (i, a) in alarms.enumerated() {
            guard showAlarm && allowedScreen(i) else {
                a.view.stopPulsing(); a.win.orderOut(nil); continue
            }
            a.view.show(alarm.items, fixable: alarm.fixable, level: alarm.level)
            a.win.orderFrontRegardless()
            a.view.startPulsing()
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

        // The SAME single gesture as the menu (2026-08-22): two surfaces offering two
        // verbs and two scopes was one more hesitation at the moment when zero is needed.
        // The bar only shows when something is wrong — the condition is already met.
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
    /// The details, on a click on the floating bar: the SAME menu as the menu-bar icon.
    /// Before, it was a translucent panel laid under the bar — unreadable on a light
    /// background, and one more surface to maintain alongside the menu. A native menu is
    /// opaque, readable everywhere, and already carries every fix; the dashboard stays
    /// one notch away, at the top of the list.
    @objc func showDetails(_ sender: NSButton) {
        let menu = NSMenu()
        fill(menu)
        // Point in SCREEN coordinates (`in: nil`): passing the view lets macOS pin the
        // list on the point and push the header above the top edge — the bar is stuck to
        // the top of the screen — hence a scroll arrow and an invisible title.
        guard let win = sender.window else { return }
        let r = win.convertToScreen(sender.convert(sender.bounds, to: nil))
        menu.popUp(positioning: nil, at: NSPoint(x: r.minX, y: r.minY - 6), in: nil)
    }

    @objc func applyFix(_ sender: NSMenuItem) { if let k = sender.representedObject as? String { runFix(k) } }
    /// POSTs an engine action, then refreshes.
    ///
    /// The menu and the dashboard buttons hit EXACTLY the same endpoints: that is the
    /// only way to have the same actions on both sides without one list drifting from
    /// the other. On stage you do not open a web page to tidy windows, and having to
    /// remember which of the two surfaces knows how to do what is exactly what we want
    /// to avoid.
    private func act(_ path: String, _ body: [String: Any] = [:]) {
        guard let u = URL(string: url + path) else { return }
        var rq = URLRequest(url: u); rq.httpMethod = "POST"; rq.timeoutInterval = 120
        rq.setValue("application/json", forHTTPHeaderField: "Content-Type")
        rq.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: rq) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refresh() }
        }.resume()
    }

    /// The single action: launch everything, repair, tidy, re-check. See /api/preflight.
    @objc func prepareAll() { act("/api/preflight", ["dry": false]) }
    @objc func setModeAuto() { act("/api/mode", ["mode": "auto"]) }
    @objc func setModeLive() { act("/api/mode", ["mode": "live"]) }
    @objc func setModeStudio() { act("/api/mode", ["mode": "studio"]) }
    /// The only check the Mac cannot measure: we declare it.
    @objc func confirmCharge() { act("/api/manual", ["key": "iphone_charge", "value": true]) }

    /// Quits the apps the rig does not need — AFTER a confirmation naming each one.
    /// An app may be holding an unsaved document; this is the only action in the menu
    /// that can make work be lost, hence the only one that asks a question.
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

    /// The menu's content, built once for the menu-bar icon AND for the floating bar:
    /// two separate lists would end up diverging, and one would have to remember which
    /// of the two knows how to do what — exactly what we want to avoid on a gig night.
    private func fill(_ menu: NSMenu) {
        menu.removeAllItems()
        // Explicit validation: opened from the floating bar (a window that never becomes
        // active), macOS auto-enabling greys out entries that are nevertheless valid — the
        // Mode submenu, among others. Our targets are all set by hand, so we decide for
        // ourselves: only the header and the "everything is ok" line are disabled, further
        // down.
        menu.autoenablesItems = false
        let head = NSMenuItem(title: "🎹 Rig — \(current.label)", action: nil, keyEquivalent: "")
        head.isEnabled = false; menu.addItem(head)
        menu.addItem(.separator())

        // The dashboard first (2026-08-20): it is the way out to EVERYTHING else — the
        // details a menu line truncates, the log, the curves. A single entry for the web
        // window, by the way: the action log lives in the same page, so two lines opened
        // exactly the same URL.
        // Silencing the alarm goes to the TOP, above even the dashboard: it is the only
        // entry one gropes for while the screen is flashing, and it only exists in that
        // case.
        if alarm.firing {
            add(menu, T("menu.silence", "🔕 Silence the alarm"), #selector(silenceAlarm),
                tip: T("menu.silence.tip",
                       "Stops the flashing for what is already broken; the border and the "
                       + "list stay. Anything that breaks LATER flashes again."))
            menu.addItem(.separator())
        }

        add(menu, T("menu.dashboard", "🌐 Open the dashboard"), #selector(open))
        menu.addItem(.separator())

        // Actions first, the detail of the problems at the bottom (2026-08-20): its length
        // varies — eight missing soundcheck gestures, and it pushed the actions out of
        // reach. Below them, each action keeps the same place from one night to the next.
        // Settings and Quit stay right at the end, in the place macOS gives them
        // everywhere else: the detail slips in ABOVE them, not after.
        // The menu carries the SAME actions as the dashboard's bar, in sections: first the
        // mode, then THE one action to know about, then what is wrong, then the one-off
        // gestures that answer it. This split is the same on both surfaces; that is what
        // makes it unnecessary to remember where anything is.
        let mode = NSMenuItem(title: T("menu.mode", "Mode"), action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for (title, sel, key) in [(T("mode.auto", "🅰 Auto"), #selector(setModeAuto), "auto"),
                                  (T("mode.live", "🎤 Live"), #selector(setModeLive), "live"),
                                  (T("mode.studio", "🎧 Studio"), #selector(setModeStudio), "studio")] {
            let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
            mi.target = self; mi.state = (requestedMode == key) ? .on : .off
            sub.addItem(mi)
            // "Auto" ticked does not say WHERE it landed, and yet that is the only thing
            // that counts before plugging in: the rig resolves to live or studio by itself.
            // The line is coloured in the same hues as the dashboard (blue = studio, amber
            // = live) so that the colour means the same thing on both surfaces.
            if key == "auto" && requestedMode == "auto" {
                sub.addItem(resolvedModeItem())
            }
        }
        // Explicit enabling: manual validation obliges, a submenu parent with no action
        // would stay greyed out — and macOS then hides its arrow.
        mode.submenu = sub; mode.isEnabled = true; menu.addItem(mode)

        // WHAT IS LEFT TO PREPARE FIRST, the gesture that will do it next (2026-08-22):
        // one line per problem, its fix in the title — clicking the line runs it.
        // The button used to be above; it was therefore read before knowing whether there
        // was any reason to click it, and it stayed on offer on an already-ready rig.
        menu.addItem(.separator())
        let probs = Pref.on(Pref.warnings, default: true) ? problems : problems.filter { $0.status == "fail" }
        if probs.isEmpty {
            let none = NSMenuItem(title: problems.isEmpty ? "Tout est ok 🎉"
                                        : T("panel.noBlockers", "No blockers (warnings hidden)"),
                                  action: nil, keyEquivalent: "")
            none.isEnabled = false; menu.addItem(none)
        }
        // TWO SECTIONS, by NATURE OF THE ACTION (requested on 2026-08-22) — and no
        // longer by domain. Grouping by domain said what it was about; it did not say
        // who has to move, which is the only question one asks when opening this
        // menu. Each of the two sections answers one gesture: click the button, or
        // get up. The domain is not lost for all that — it becomes a grey prefix on
        // the line, where it costs zero menu lines.
        //
        // The button lives INSIDE the section it repairs: that is what makes it readable
        // at a glance ("here is what it will do, and here it is"). If there is nothing
        // automatic to do, the section does not exist, so neither does the button — a
        // "Tout préparer" under a list of human gestures would promise nothing.
        let ordered = probs.filter { $0.parts.isEmpty } + probs.filter { !$0.parts.isEmpty }
        let fixable = ordered.filter { $0.remedy != nil && !$0.manual }
        let byHand  = ordered.filter { $0.remedy == nil || $0.manual }

        if fixable.isEmpty {
            // "Nothing to repair" is SAID, otherwise the section disappears without one
            // knowing whether it is empty or whether the menu forgot something — and it is
            // the good news of the night: all that could settle itself already has.
            let none = NSMenuItem(title: T("menu.autoOk", "✅ Automatic settings are fine"),
                                  action: nil, keyEquivalent: "")
            none.isEnabled = false; menu.addItem(none)
        } else {
            menu.addItem(sectionHead(T("menu.sec.fixable", "Fixable automatically")))
            for p in fixable { addProblem(menu, p) }
            // The button CARRIES ITS COUNT (requested on 2026-08-22): "Tout
            // préparer" looked like a gesture to perform even when it had nothing
            // to do. "Fix 2 points" says what is going to happen, and the fact that
            // it no longer appears at all when that count drops to zero is then no
            // longer a mysterious disappearance but the logical follow-on.
            let label = String(format: fixable.count == 1 ? T("menu.fixOne", "✨ Fix %d point")
                                                          : T("menu.fixN", "✨ Fix %d points"),
                               fixable.count)
            let go = NSMenuItem(title: label, action: #selector(prepareAll), keyEquivalent: "")
            go.target = self
            // Indented with the lines it repairs, and in bold: a gesture, under what it
            // does. Placed outside the section, it read as one more observation (22/08).
            go.indentationLevel = 1
            go.attributedTitle = NSAttributedString(string: label, attributes: [
                .font: NSFont.boldSystemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize)])
            go.toolTip = T("menu.prepare.tip", """
                           Launches the rig apps, opens the set, tidies the windows, then applies \
                           every fix it can — and re-checks everything. The ✋ lines stay yours to do.
                           """)
            menu.addItem(go)
        }

        if !byHand.isEmpty {
            menu.addItem(.separator())
            menu.addItem(sectionHead(String(format: T("menu.sec.byHand", "Up to you (%d)"),
                                            byHand.count)))
            for p in byHand { addProblem(menu, p) }
        }
        menu.addItem(.separator())

        // The two one-off gestures AFTER the list (2026-08-20): they are answers to what
        // one has just read in it — "iPhone not charging", "one app too many is open".
        // Above, they were read before knowing whether there was any reason to perform
        // them; below, the hand comes down from the red line towards its gesture.
        add(menu, T("menu.charge", "🔋 Confirm the iPhone is charging"), #selector(confirmCharge))
        add(menu, T("menu.quitOthers", "🧹 Quit the other apps…"), #selector(quitOthers))
        menu.addItem(.separator())

        let settings = NSMenuItem(title: T("menu.settings", "⚙︎ Settings…"),
                                  action: #selector(showSettings), keyEquivalent: ",")
        settings.target = self; menu.addItem(settings)
        // No more "Quit": the app is a safety net, and a net one can close by mistake no
        // longer protects. What one really wants when clicking Quit is for it to stop
        // making itself heard — which is exactly what the pause is.
        add(menu, paused ? T("menu.resume", "▶︎ Resume monitoring")
                         : T("menu.pause", "⏸ Pause — stop reacting to anything"),
            #selector(togglePause))
    }

    // ---- Settings window (classic macOS look) ------------------------------
    @objc func showSettings() {
        if settingsWin == nil { settingsWin = makeSettingsWindow() }
        settingsWin?.center()
        settingsWin?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Settings window in TABS (NSTabViewController, .toolbar style) — the native
    /// template of macOS Settings. Before: the three sections stacked in a single
    /// column, i.e. 732 px tall once the explanations had been added. Tabs bring each
    /// page back to its own height and the window resizes all by itself when changing
    /// tab.
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
            Row("light.beacon.max", T("alerts.alarm.title", "Flash the screen on a new failure"),
                T("alerts.alarm.hint",
                  "When something breaks AFTER the screen was clean — the Mac losing power, "
                  + "a port dropping — a red banner flashes until that failure is fixed. It "
                  + "never catches a click, and the 🎹 menu silences it."),
                Pref.alarm, true, { self.applyOverlay() }),
            Row("bell", T("alerts.notify.title", "One notification when the state changes"),
                T("alerts.notify.hint",
                  "A macOS notification when severity gets worse (green → orange → red), not on every check."),
                Pref.notify, false, {}),
            Row("wrench.and.screwdriver", T("alerts.autofix.title", "Fix automatically"),
                T("alerts.autofix.hint",
                  "readyset relaunches apps and ports on its own, without asking — including mid-song. "
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
        w.title = T("settings.title", "readyset Settings")
        w.isReleasedWhenClosed = false
        // .resizable: the width came from `fittingSize`, which underestimates an NSGridView
        // nested in an NSStackView — hence text clipped on the right as soon as a label
        // gets longer. Staying resizable keeps the next slightly long label from
        // reproducing the bug.
        w.styleMask.insert(.resizable)
        w.styleMask.remove(.miniaturizable)   // a settings window does not minimise
        return w
    }

    /// One tab page: the views stacked, with the margins of macOS Settings.
    private func settingsPage(_ views: [NSView]) -> NSView {
        // Flexible spacer last: the window takes the height of the TALLEST tab, and
        // without this spacer the NSGridView expands to fill the surplus — hence gaps
        // between the rows of the shortest one. The spacer absorbs all the excess, the
        // settings stay stuck to the top, whatever the tab.
        let filler = NSView()
        filler.setContentHuggingPriority(.init(1), for: .vertical)
        let stack = NSStackView(views: views + [filler])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 20
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 20, right: 24)
        for v in views { v.setContentHuggingPriority(.required, for: .vertical) }
        // Width floor: the wrapping width of the explanations (330) + the icon column
        // + the switch column + the margins.
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
        /// A control PLACED TO THE LEFT of the toggle, for a setting that also drives an
        /// immediate action — the Stream Deck's power, the only case so far. Two separate
        /// rows would say less well that this is a single thing: the toggle authorises the
        /// control, the button exercises it.
        let extra: (() -> NSView)?
        init(_ icon: String, _ label: String, _ hint: String, _ key: String, _ def: Bool,
             warning: Bool = false, extra: (() -> NSView)? = nil, _ onChange: @escaping () -> Void) {
            self.icon = icon; self.label = label; self.hint = hint; self.key = key
            self.def = def; self.warning = warning; self.extra = extra; self.onChange = onChange
        }
    }

    /// No more section title: the tab already carries it. Repeating it above the grid
    /// would duplicate the label of the selected tab.
    private func settingsSection(_ rows: [Row]) -> NSView {
        let box = NSStackView(); box.orientation = .vertical; box.alignment = .leading; box.spacing = 10
        let grid = NSGridView(); grid.translatesAutoresizingMaskIntoConstraints = false
        grid.rowSpacing = 16; grid.columnSpacing = 14
        for row in rows {
            // Icon column: one symbol per setting, in a tinted badge in the System Settings
            // manner. It is not decorative — it is the landmark one finds again at a glance
            // when coming back to change ONE precise setting, without re-reading the labels.
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

            // A ⚠️ in the label would duplicate the orange badge: the colour of the title
            // and that of the icon already say "careful".
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
            // yPlacement is carried by the ROW, not by the column: without that the switch
            // settles at the top of the title+hint block instead of being centred opposite.
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
    /// Quit, for good — but only this once.
    ///
    /// It used to be a `launchctl bootout`: the only way not to be relaunched, as long as
    /// the agent carried `KeepAlive: true`. The price was hidden and severe — unloading
    /// the agent leaves it unloaded, so readyset no longer came back AT THE NEXT LOGIN
    /// either, and a manual `bootstrap` was needed to notice it.
    ///
    /// The agent moved to `KeepAlive: { SuccessfulExit: false }`, which only relaunches on
    /// a non-zero exit code. A clean exit is therefore enough, and it leaves the service
    /// in place for the next login session. What falls over on its own is still caught;
    /// what one closes on purpose stays closed.
    @objc func quit() { NSApp.terminate(nil) }

    @objc func silenceAlarm() { alarm.silence(); applyOverlay() }
    /// The snooze comes back on its own: the poll runs every 5 s and calls `applyOverlay`
    /// again, which re-asks `firing` — which looks at the clock. No timer to arm, hence
    /// nothing that could be lost if the app is relaunched in the meantime.
    func snoozeAlarm(_ seconds: TimeInterval) { alarm.snooze(seconds); applyOverlay() }

    /// The panel's button: runs the remedy of EVERY failure in the alarm, and of those
    /// alone. Not `/api/preflight` ("Tout préparer") — that one relaunches apps and
    /// reopens the set, which is not what we want from a button pressed mid-song because
    /// the power dropped. We repair what has just broken, nothing more.
    @objc func fixAlarm() {
        for p in alarm.fixable { runFix(p.key) }
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

    // ---- Stream Deck power -------------------------------------------------
    //
    // The dock's internal hubs advertise "ppps" (per-port power switching — cutting the
    // +5 V of one precise port with a standard USB command). `~/.local/bin/sd-power` sends
    // that command via uhubctl, on the port carrying the Stream Deck's hub: everything
    // plugged in behind it falls with it.
    //
    // Run LOCALLY, and not through a POST to /api like the rest of this menu. That is
    // deliberate: the case where one most surely wants to restore a cut Stream Deck is
    // the one where the engine is down — routing it through the API would make it
    // unavailable exactly when it is of use.

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
                // Read BEFORE waitUntilExit: waiting for the process to finish first
                // would deadlock both as soon as the output fills the pipe's buffer.
                let d = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                out = String(data: d, encoding: .utf8) ?? ""
            } catch { out = "" }
            DispatchQueue.main.async { done(out) }
        }
    }

    func refreshStreamDeck() {
        // Option switched off = no call to uhubctl. The polling is not free: it touches
        // the USB bus every 5 s, which we do not do for an entry that is not even being
        // displayed.
        guard Pref.on(Pref.streamDeck, default: false) else {
            streamDeckPowered = nil
            updateStreamDeckButton()
            return
        }
        sdPower("status") { [weak self] out in
            // Empty output and "inconnu" lead to the same place: no proven state, hence
            // no action offered. sd-power returns "inconnu" when a target in its config no
            // longer answers — a renumbered port, typically.
            if out.contains("État : on")       { self?.streamDeckPowered = true }
            else if out.contains("État : off") { self?.streamDeckPowered = false }
            else                               { self?.streamDeckPowered = nil }
            self?.updateStreamDeckButton()
        }
    }

    /// The button CARRIES the state and the permission: no separate cut/restore action,
    /// no dialog box. It lives in the Settings and nowhere else
    /// (2026-08-20) — the menu is read while walking on stage, and cutting the power of a
    /// USB port is not a stage gesture. The toggle next to it drives the polling;
    /// without it, no state is known and the button has nothing to offer.
    private func makeStreamDeckButton() -> NSView {
        let b = NSButton(title: "", target: self, action: #selector(toggleStreamDeck))
        b.bezelStyle = .rounded
        b.controlSize = .regular
        sdButton = b
        updateStreamDeckButton()
        return b
    }

    /// Replayed on every poll: the Settings window is built only once, so without this
    /// the button would keep the label it had when it was opened.
    private func updateStreamDeckButton() {
        guard let b = sdButton else { return }
        guard Pref.on(Pref.streamDeck, default: false) else {
            b.title = T("sd.off", "Disabled"); b.isEnabled = false; return
        }
        switch streamDeckPowered {
        case .some(false):
            // Restoring is NEVER blocked: not outside studio, not on an unreachable
            // engine. The guardrail protects the risky gesture, not the return to the safe
            // state — refusing a restore would leave the Stream Deck dead with no way out.
            b.title = T("sd.restore", "Restore"); b.isEnabled = true
        case .some(true) where effectiveMode == "studio":
            b.title = T("sd.cut", "Cut"); b.isEnabled = true
        case .some(true):
            // Cutting in live means losing control of the set at the worst moment. An
            // unknown mode counts as a refusal: one then CANNOT prove one is not in
            // live, and the doubt must lean to the side that does not break the gig.
            let m = effectiveMode ?? T("sd.modeUnknown", "mode unknown")
            b.title = T("sd.studioOnly", "Studio only") + " (\(m))"; b.isEnabled = false
        case .none:
            b.title = T("sd.unset", "Run `sd-power detect`"); b.isEnabled = false
        }
    }

    @objc func toggleStreamDeck() {
        // Revalidation on click: the menu may have been built several seconds earlier and
        // the mode may flip by itself in the meantime. We only cut on a studio confirmed
        // at that instant; otherwise we merely refresh, and the label will say why.
        guard streamDeckPowered == false || effectiveMode == "studio" else {
            refreshStreamDeck(); return
        }
        // Explicit "on"/"off" rather than "toggle": the script would re-read the state
        // on its own side, which would reopen the very race the revalidation above has
        // just closed.
        sdPower(streamDeckPowered == false ? "on" : "off") { [weak self] _ in
            self?.refreshStreamDeck()
        }
    }

    /// The "here is where Auto landed" line. Disabled — there is nothing to click, the
    /// mode is chosen through the three entries above. The colour goes through an
    /// attributedTitle: a disabled item is grey by default, and grey would read as
    /// "unavailable" whereas this is the most useful information in the submenu.
    private func resolvedModeItem() -> NSMenuItem {
        // Same hues as the dashboard (--accent / --warn): the colour must mean the same
        // thing on both surfaces, otherwise it teaches nothing.
        let studio = NSColor(red: 0.29, green: 0.62, blue: 1.00, alpha: 1)   // #4a9eff
        let live   = NSColor(red: 0.96, green: 0.73, blue: 0.26, alpha: 1)   // #f4b942
        let text: String, colour: NSColor
        switch effectiveMode {
        case "studio": text = T("mode.resolved", "→ currently") + "  " + T("mode.studio", "🎧 Studio"); colour = studio
        case "live":   text = T("mode.resolved", "→ currently") + "  " + T("mode.live", "🎤 Live");     colour = live
        // Engine unreachable: the mode is not proven. Grey, and rightly so — here, the
        // information IS "we do not know".
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
    /// One problem line, as it appears in either of the two sections.
    /// Extracted on 2026-08-22 when the list split in two: the same rendering on both
    /// sides is the only thing that makes it possible to compare the two columns at a
    /// glance.
    private func addProblem(_ menu: NSMenu, _ p: Problem) {
        // The domain as a GREY prefix rather than a subheading: it says what this is
        // about without consuming a menu line, and without competing with the question
        // that now structures the menu — who has to move.
        let dot = p.status == "fail" ? "🔴" : "🟠"
        let lead = p.group.isEmpty ? "" : "\(p.group) · "
        // The title now carries only the check's name: the detail overflowed and macOS
        // truncated it from the end, hence from the explanation. It lives in the tooltip,
        // where nothing cuts it.
        var title = "\(dot) \(lead)\(shorten(p.label, menuTitleBudget - lead.count - 8))"
        // 🔧 = the button can do it; ✋ = the line opens the door, the gesture stays yours.
        if let rem = p.remedy { title += "   \(p.manual ? "✋" : "🔧") \(shorten(rem, 22))" }
        // With no fix, the line opens the dashboard rather than being inert: macOS greys
        // out a line with no action, and readability is precisely what we are after here.
        let mi = NSMenuItem(title: title,
                            action: p.remedy == nil ? #selector(open) : #selector(applyFix(_:)),
                            keyEquivalent: "")
        mi.target = self; mi.representedObject = p.key
        mi.indentationLevel = 1
        if !lead.isEmpty {
            let a = NSMutableAttributedString(string: title)
            a.addAttribute(.foregroundColor, value: NSColor.secondaryLabelColor,
                           range: NSRange(location: dot.count + 1, length: lead.count))
            mi.attributedTitle = a
        }
        mi.toolTip = p.detail.isEmpty ? p.label : "\(p.label)\n\(p.detail)"
        menu.addItem(mi)
        // A missing gesture is not a failure: it is a missing proof, and nothing to click
        // — hence lines indented under their check, with no action. Several per line: at
        // eight gestures, one line each made up half the menu for two words.
        for row in packed(p.parts.filter { !$0.ok }.map { "\($0.icon.isEmpty ? "◦" : $0.icon) \($0.name)" },
                          width: menuTitleBudget - 4) {
            let sub = NSMenuItem(title: row, action: nil, keyEquivalent: "")
            sub.indentationLevel = 2; sub.isEnabled = false
            sub.toolTip = T("menu.gestureHint", "Play it once — the check is passive, nothing to tick")
            menu.addItem(sub)
        }
    }

    /// A domain's subheading: small, grey, not clickable — it structures without
    /// claiming to be an action. The groups come from the engine, so they already carry
    /// the dashboard's words.
    private func sectionHead(_ title: String) -> NSMenuItem {
        let mi = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        mi.isEnabled = false
        mi.attributedTitle = NSAttributedString(string: title.uppercased(), attributes: [
            .font: NSFont.systemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize - 2, weight: .semibold),
            .foregroundColor: NSColor.secondaryLabelColor])
        return mi
    }

    /// `tip`: what the gesture really does, where nothing truncates it — a menu title
    /// gets cut by macOS, and it is always the end that is dropped, hence the nuance.
    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector, tip: String? = nil) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: ""); mi.target = self
        mi.toolTip = tip
        menu.addItem(mi)
    }
}

// Entry point. `@main` rather than top-level statements: as soon as more than one
// file is compiled, Swift accepts them ONLY in a file named `main.swift` — and renaming
// this one would have made every path citing it stale (build.sh, the two CLAUDE.md
// files, the README). An @main type says exactly the same thing under any file name
// whatsoever.
@main
enum RigMenuBarApp {
    /// Held here, and not in a local variable: `NSApplication.delegate` does not own its
    /// delegate. As a local it survived only by accident — `run()` never gives control
    /// back, so the stack never unwinds. A static property guarantees it instead of hoping.
    private static let delegate = Delegate()

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
        app.delegate = delegate
        app.run()
    }
}
