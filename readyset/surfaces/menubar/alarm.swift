// RigMenuBar — the ALARM: what breaks while everything was going well.
//
// The border and the pill answer "where does the rig stand?" — a state, read when one
// thinks of it. The alarm answers another question, and that is why it exists on its
// own: "something has just BROKEN". A Mac whose power is yanked out between two songs
// does not ask to be consulted, it asks to be SEEN — without one having raised one's
// eyes, without one having opened a menu.
//
// Hence the four choices:
//
//   • It only fires on a REGRESSION. A rig already red at launch has nothing sudden
//     about it: that is the setting-up, one reads it in the list. What deserves a
//     flash is what appears AFTER the screen has become clean.
//   • It flashes for as long as what appeared is not repaired — not "for 10 s". An
//     alert that goes out by itself proves nothing: one may well have missed it.
//   • ONLY THE FRAME FLASHES. The panel, for its part, does not move by a pixel: it is
//     text to READ — what has broken, what is observed, the gesture that repairs.
//     Flashing text reads twice as slowly as fixed text, and urgency is no reason to
//     slow reading down; it is a reason to speed it up. The flashing draws the eye, the
//     panel informs it. Two roles, two surfaces.
//   • It takes NO click — except its fix button. On stage, a surface that intercepts a
//     click meant for Ableton is a worse bug than the failure it reports; but a panel
//     that says "here is the fix" without offering it forces one to go looking for it
//     in a menu, at the worst moment. The hole in the glass is exactly the size of the
//     button (see `hitTest`).

import Cocoa

// ---------------------------------------------------------------------------
// The state machine — what broke, since when, and is it repaired?
// ---------------------------------------------------------------------------
/// Tracks the problems that APPEARED since the last clean slate, and nothing else.
///
/// The subtle point is the tracking by KEY rather than by global verdict. "As long as the
/// rig is not green" would have been simpler, and wrong: a permanent warning (an open app
/// kept on purpose) would have made the screen flash all evening long, which amounts to
/// reporting nothing at all any more. Here, only the failure that has just happened
/// flashes — and it goes out as soon as IT is settled, even if the rest is not green.
struct AlarmState {
    /// The keys that appeared and are not repaired yet. Empty = nothing to report.
    private(set) var keys: Set<String> = []
    /// The corresponding problems, in the order /api/state gives them (blockers first):
    /// these are the ones the panel displays, with their observation and their fix.
    private(set) var items: [Problem] = []
    /// The most severe level among them — red if there is a blocker, orange otherwise.
    private(set) var level: RigStatus = .fail
    /// What the previous poll showed: it is the comparison with it that defines "new".
    /// With no memory of the previous round, any lasting problem would be new on every
    /// round and the alarm would never stop.
    private var known: Set<String> = []
    /// The very first state received fires NOTHING: it has no "before" to compare itself
    /// with. A rig launched already broken is not a regression, it is a setting-up.
    private var seeded = false
    /// Kills (`silence()`) the flashing of the current episode without erasing the keys:
    /// the border stays, the list stays, only the alarm calms down. A problem one cannot
    /// repair right now must not condemn the screen for the rest of the set.
    private(set) var silenced = false
    /// The timed snooze — the alarm falls silent, then COMES BACK if nothing was settled.
    ///
    /// It is a third state, and it had to be one: "stop" suits what one has seen and
    /// decided to ignore, the snooze suits what one will deal with in two songs' time and
    /// would forget without it. Confusing the two means losing one of them — either one
    /// silences for good what one only wanted to defer, or one leaves flashing what one
    /// has already judged.
    private(set) var snoozedUntil: Date?

    var firing: Bool {
        guard !keys.isEmpty, !silenced else { return false }
        if let until = snoozedUntil, Date() < until { return false }
        return true
    }
    /// How much is left to run, so it can be shown rather than left to be guessed.
    var snoozeRemaining: TimeInterval? {
        guard let until = snoozedUntil, Date() < until else { return nil }
        return until.timeIntervalSinceNow
    }

    /// To be called on EVERY poll, with the problems one wants watched (the blockers,
    /// plus the warnings if the option asks for them).
    mutating func update(status: RigStatus, problems: [Problem]) {
        // Engine silent: we freeze everything. No firing (a missing answer proves no
        // failure of the rig), and no clearing (it would erase an alarm in progress at the
        // worst possible moment, the one where the dashboard has just gone down too).
        guard status != .unreachable else { return }

        let now = Set(problems.map(\.key))
        defer { known = now }
        guard seeded else { seeded = true; return }

        let fresh = now.subtracting(known)
        // A "be quiet" only holds for the failures one knew about when saying it.
        // What breaks AFTERWARDS is entitled to its flashing — including while another
        // failure is snoozed. The previous version additionally required the slate to be
        // EMPTY (`keys.isEmpty`), which made the promise false in the only case where it
        // counts: one silences the Stream Deck to finish the song, the power drops in the
        // meantime, and the screen stays mute. Observed on the test bench on 29/08.
        if !fresh.isEmpty { silenced = false; snoozedUntil = nil }
        keys.formUnion(fresh)
        keys.formIntersection(now)          // repaired → leaves the alarm
        // Nothing broken any more: neither silence nor snooze has an object, and keeping
        // them would mute the next failure on the grounds of a decision made about another.
        if keys.isEmpty { silenced = false; snoozedUntil = nil }

        items = problems.filter { keys.contains($0.key) }
        level = items.contains { $0.status == "fail" } ? .fail : .warn
    }

    mutating func silence() { silenced = true; snoozedUntil = nil }
    mutating func snooze(_ seconds: TimeInterval) { snoozedUntil = Date().addingTimeInterval(seconds); silenced = false }

    /// What the fix button can run: the failures in the alarm that have a remedy.
    /// Empty = no button, and the panel SAYS so instead of offering one that would do
    /// nothing.
    var fixable: [Problem] { items.filter { $0.remedy != nil } }
}

// ---------------------------------------------------------------------------
// The frame that pulses — and nothing else pulses
// ---------------------------------------------------------------------------
final class FlashFrameView: NSView {
    var tint: NSColor = .systemRed { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    /// The flashing, applied to the LAYER and not to the window's opacity: a layer
    /// animation runs in the render process, so it costs the main thread nothing — the
    /// very thread that polls the engine while the set is running.
    func startPulsing() {
        guard layer?.animation(forKey: "pulse") == nil else { return }
        let a = CABasicAnimation(keyPath: "opacity")
        a.fromValue = 1.0
        // Not all the way to extinction. A frame that disappears half the time is seen
        // LESS well than a frame that beats: between two beats there is nothing left to
        // catch out of the corner of the eye, and the eye goes back to Ableton. The floor
        // keeps a permanent red presence; it is the beat on top that makes the alert.
        a.toValue = 0.34
        a.duration = 0.42          // snappier than a second: we want "alarm", not "breathing"
        a.autoreverses = true
        a.repeatCount = .infinity
        a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        layer?.add(a, forKey: "pulse")
    }

    func stopPulsing() { layer?.removeAnimation(forKey: "pulse") }

    /// A thick stroke, DOUBLED with a halo that fades inwards.
    ///
    /// The stroke alone did not keep its promise: on a big screen, eighteen points stuck
    /// to the edge is one more window border — the eye, busy in the centre, does not
    /// catch it. The halo changes that without stealing anything from the useful area:
    /// it fades out over about a hundred points, but it gives the beat a SURFACE, and a
    /// surface is seen in peripheral vision where a line is not.
    ///
    /// Four bands rather than a crown gradient: NSGradient only draws linear and radial,
    /// and a radial lights up the centre — the exact opposite of what we want. The four
    /// overlap in the corners, which end up brighter for it: that is an accident, and a
    /// welcome one, since the corners are what peripheral vision catches in the first
    /// place.
    override func draw(_ dirty: NSRect) {
        let thickness: CGFloat = 30
        let glow: CGFloat = 120
        if let g = NSGradient(starting: tint.withAlphaComponent(0.40),
                              ending: tint.withAlphaComponent(0.0)) {
            g.draw(in: NSRect(x: 0, y: bounds.maxY - glow, width: bounds.width, height: glow), angle: -90)
            g.draw(in: NSRect(x: 0, y: 0, width: bounds.width, height: glow), angle: 90)
            g.draw(in: NSRect(x: 0, y: 0, width: glow, height: bounds.height), angle: 0)
            g.draw(in: NSRect(x: bounds.maxX - glow, y: 0, width: glow, height: bounds.height), angle: 180)
        }
        tint.withAlphaComponent(0.98).setStroke()
        let frame = NSBezierPath(rect: bounds.insetBy(dx: thickness / 2, dy: thickness / 2))
        frame.lineWidth = thickness
        frame.stroke()
    }
}

// ---------------------------------------------------------------------------
// The buttons — big, and saying what they do
// ---------------------------------------------------------------------------
/// A panel button: a title read from a distance, and under it a line that says what
/// will happen.
///
/// The subtitle is not decoration. "Arrêter l'alarme" and "Rappel dans 5 min" look
/// alike enough for one to hesitate for a second — and a second of hesitation in front
/// of a red screen, on stage, is already too much. The line below lifts the doubt
/// without one having to remember anything.
///
/// It replaces a 12 px help line in 50 % grey laid under the buttons, which carried the
/// same information and which nobody could read.
final class BigButton: NSButton {
    // Three gestures, three colours. Two identical grey buttons forced one to READ in
    // order to choose; on stage one aims at the colour before reading. Green = it repairs,
    // amber = it comes back, muted red = it goes quiet without settling anything.
    enum Tone { case action, wait, stop, neutral, off }
    private var tone: Tone = .neutral

    init(tone: Tone, title: String, subtitle: String, target: AnyObject?, action: Selector) {
        super.init(frame: .zero)
        self.tone = tone
        self.target = target
        self.action = action
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 10
        setButtonType(.momentaryChange)
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(greaterThanOrEqualToConstant: 62).isActive = true
        set(title: title, subtitle: subtitle)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    func set(tone: Tone) { self.tone = tone; isEnabled = tone != .off }

    func set(title: String, subtitle: String) {
        // A switched-off button stays PERFECTLY readable: that is even its only use.
        // Greying a button until it is illegible in order to say "unavailable" removes
        // the information one wanted to give — here, that nothing can be repaired alone.
        // The contrast of a switched-off button is tuned UPWARDS, not downwards. "No
        // automatic fix" is an answer, not an absence: it is even the most useful line in
        // the panel when it shows, since it says one has to get up.
        let fg: NSColor = tone == .off ? NSColor.white.withAlphaComponent(0.80) : .white
        let sub: NSColor = tone == .off ? NSColor.white.withAlphaComponent(0.58)
                                        : NSColor.white.withAlphaComponent(0.74)
        layer?.backgroundColor = {
            switch tone {
            case .action:  return NSColor(calibratedRed: 0.13, green: 0.47, blue: 0.26, alpha: 1).cgColor
            case .wait:    return NSColor(calibratedRed: 0.52, green: 0.36, blue: 0.06, alpha: 1).cgColor
            case .stop:    return NSColor(calibratedRed: 0.44, green: 0.15, blue: 0.15, alpha: 1).cgColor
            case .neutral: return NSColor(calibratedWhite: 0.24, alpha: 1).cgColor
            case .off:     return NSColor(calibratedWhite: 0.20, alpha: 1).cgColor
            }
        }()
        layer?.borderWidth = tone == .off ? 1 : 0
        layer?.borderColor = NSColor.white.withAlphaComponent(0.22).cgColor

        let para = NSMutableParagraphStyle()
        para.alignment = .center
        para.lineSpacing = 2
        let s = NSMutableAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: 17, weight: .semibold),
            .foregroundColor: fg, .paragraphStyle: para])
        if !subtitle.isEmpty {
            s.append(NSAttributedString(string: "\n" + subtitle, attributes: [
                .font: NSFont.systemFont(ofSize: 12.5, weight: .regular),
                .foregroundColor: sub, .paragraphStyle: para]))
        }
        attributedTitle = s
        (cell as? NSButtonCell)?.usesSingleLineMode = false
        (cell as? NSButtonCell)?.lineBreakMode = .byWordWrapping
        needsDisplay = true
    }
}

// ---------------------------------------------------------------------------
// The panel — fixed, readable, and actionable
// ---------------------------------------------------------------------------
/// Built out of real subviews (and not drawn by hand) because it carries a BUTTON: a
/// painted rectangle receives no click, and the repair gesture must be there, before
/// one's eyes, instead of having to be looked for in a menu at the worst moment.
final class AlarmCard: NSView {
    private let stack = NSStackView()
    private let title = NSTextField(labelWithString: "")
    /// Three possible gestures in front of a failure, and one button for each — because
    /// the three are different decisions: repair it, file it, or come back to it. The 🎹
    /// menu offered only one, and one had to open it to find it.
    private var fixButton: BigButton!
    private var stopButton: BigButton!
    private var snoozeButton: BigButton!
    private let buttons = NSStackView()
    private var buttonsWidth: NSLayoutConstraint?
    /// What the buttons trigger. Set by the delegate: the card does not know how to talk
    /// to the engine, and has no business knowing.
    var onFix: (() -> Void)?
    var onStop: (() -> Void)?
    var onSnooze: (() -> Void)?
    /// Confirm by hand that the iPhone is charging. The check CANNOT see it on its own,
    /// so its "fix" is a human word — it used to belong only in the menu, that is to say
    /// two clicks away from a panel which, for its part, is already before one's eyes.
    var onConfirmCharge: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = 20
        layer?.backgroundColor = NSColor.black.withAlphaComponent(0.90).cgColor
        layer?.borderWidth = 4

        title.font = .systemFont(ofSize: 27, weight: .heavy)
        title.lineBreakMode = .byTruncatingTail

        fixButton = BigButton(tone: .action, title: "", subtitle: "",
                              target: self, action: #selector(fixTapped))
        stopButton = BigButton(tone: .stop, title: T("alarm.stop", "✕ Stop the alarm"),
                               subtitle: T("alarm.stopSub", "The error stays, the screen calms down"),
                               target: self, action: #selector(stopTapped))
        snoozeButton = BigButton(tone: .wait, title: T("alarm.snooze", "⏰ Remind me in 5 min"),
                                 subtitle: T("alarm.snoozeSub", "Comes back if it is still broken"),
                                 target: self, action: #selector(snoozeTapped))
        buttons.orientation = .horizontal
        buttons.distribution = .fillEqually
        buttons.alignment = .top
        buttons.spacing = 10
        [fixButton, snoozeButton, stopButton].forEach { buttons.addArrangedSubview($0!) }
        buttons.translatesAutoresizingMaskIntoConstraints = false

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 18, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    /// The left button carries two gestures depending on what there is to do: repair when
    /// a repair exists, confirm the charging when that is the only possible gesture.
    private var fixIsConfirm = false
    @objc private func fixTapped() { fixIsConfirm ? onConfirmCharge?() : onFix?() }
    @objc private func stopTapped() { onStop?() }
    @objc private func snoozeTapped() { onSnooze?() }

    /// The button is the ONLY place that takes a click. Everywhere else `hitTest` returns
    /// `nil`, so the window lets the event through to whatever is underneath — an alert
    /// panel has no business coming between the finger and Ableton.
    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        // A SWITCHED-OFF button stays in the list: it takes up its place, and a click on
        // it must stop there rather than go through the glass and land in Ableton.
        for b in [fixButton, stopButton, snoozeButton] where b != nil && !b!.isHidden && b!.superview != nil {
            if b!.convert(b!.bounds, to: self).contains(local) { return b!.isEnabled ? b! : self }
        }
        return nil
    }

    /// Fills the panel — and stops when the page is full, not at a fixed count.
    ///
    /// The previous rule capped at three failures, on reasoning that held up: a fourth
    /// line does not change the gesture, since the button repairs them all. It was just
    /// missing the other half — knowing WHAT went down is not only of use for pressing a
    /// button. On a 27-inch, ten lines fit effortlessly and say the extent of the damage
    /// in one go; capping them at three means throwing away free information. The real
    /// cap is therefore not a number, it is the available HEIGHT, and that changes from
    /// one screen to the next.
    ///
    /// The filling order follows the order of usefulness, and that is where something is
    /// at stake: first all the STANDALONE failures — each asks for its own gesture —
    /// then only, if room is left, the KNOCK-ON failures, slipped under the one that
    /// explains them. A consequence teaches nothing one does not already know from
    /// reading its cause; it must not take the line of a problem nobody has yet seen.
    func show(_ items: [Problem], fixable: [Problem], tint: NSColor, width: CGFloat,
              maxHeight: CGFloat) {
        layer?.borderColor = tint.cgColor
        title.stringValue = T("alarm.title", "SOMETHING JUST BROKE")
        title.textColor = tint

        // `removeFromSuperview` and not `stack.removeView(_:)`: the latter RAISES an
        // exception when the view is not (or no longer) in the list — and it did so on
        // the very first display, when the title was not yet in it. Removing from the
        // hierarchy removes from the list too, assuming nothing about the previous state.
        stack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        stack.addArrangedSubview(title)

        // The footer is prepared BEFORE the rows, so that we know the room it will take.
        // Without this reservation, the last failure added would push the repair button
        // off the screen — the panel would be complete and unusable.
        prepareFooter(items: items, fixable: fixable)
        let footer = 62 + stack.spacing * 2   // minimum height of a row of buttons
        let budget = maxHeight - footer

        let rowWidth = width - 48
        let present = Set(items.map(\.key))
        // A consequence whose cause is NOT displayed becomes a failure like the others
        // again: otherwise it would be relegated to the second pass in the name of a link
        // that nothing on screen shows.
        func follows(_ p: Problem) -> Bool { p.causedBy.map(present.contains) ?? false }

        var shown: [String] = []            // displayed keys, in the stack's order

        func fits() -> Bool {
            stack.layoutSubtreeIfNeeded()
            return stack.fittingSize.height <= budget
        }
        /// Adds a row at the requested index and REMOVES it if it overflows — measuring
        /// afterwards is the only honest way: the height of wrapping text is not guessed,
        /// it is observed.
        func place(_ p: Problem, follower: Bool, at index: Int) -> Bool {
            let row = makeRow(p, width: rowWidth, follower: follower)
            stack.insertArrangedSubview(row, at: index)
            if fits() { shown.insert(p.key, at: index - 1); return true }
            row.removeFromSuperview()
            return false
        }
        func drop(_ key: String) {
            guard let i = shown.firstIndex(of: key) else { return }
            stack.arrangedSubviews[i + 1].removeFromSuperview()
            shown.remove(at: i)
        }

        let heads = items.filter { !follows($0) }
        for p in heads where !place(p, follower: false, at: shown.count + 1) { break }

        // The knock-ons, group by group and ALL OR NOTHING. A single consequence out of
        // three, picked by whatever room was left, would say "the keyboard went down" and
        // keep silent about the XL — leading one to believe it is fine. Yet the "⛓ brings
        // down" line already names all three of them: better to let it do the job alone
        // than to half contradict it.
        for cause in heads where !cause.causes.isEmpty {
            let group = items.filter { $0.causedBy == cause.key }
            // +1 for the title, +1 to be placed AFTER the cause: sticking the consequence
            // to its cause is the whole point — one foreign line between the two and the
            // link stops being visible.
            guard !group.isEmpty, let at = shown.firstIndex(of: cause.key) else { continue }
            var placed: [String] = []
            var complete = true
            for p in group {
                if !place(p, follower: true, at: at + 2 + placed.count) { complete = false; break }
                placed.append(p.key)
            }
            guard complete else { placed.forEach(drop); continue }
            // The three lines are there, under their cause: "brings down: Stream Deck
            // XL, Keyboard, Breath controller" would name a second time what one reads
            // just below. The summary exists ONLY for the screen where they do not fit;
            // here it steps aside, and the room it frees goes back to the panel.
            stack.arrangedSubviews[at + 1].removeFromSuperview()
            stack.insertArrangedSubview(makeRow(cause, width: rowWidth, follower: false,
                                                withCauses: false), at: at + 1)
        }

        // "+N" counts ONLY the standalone failures left outside. An unexpanded knock-on
        // is not a hidden failure: its name is on its cause's line, two lines higher up.
        // Counting it here would make one fear damage one already has before one's eyes.
        let skipped = heads.count - shown.filter { key in heads.contains { $0.key == key } }.count

        if skipped > 0 {
            let more = NSTextField(labelWithString: String(format: T("alarm.more", "+%d more"), skipped))
            more.font = .systemFont(ofSize: 13)
            more.textColor = NSColor.white.withAlphaComponent(0.6)
            stack.addArrangedSubview(more)
        }
        stack.addArrangedSubview(buttons)
        if buttonsWidth == nil {
            buttonsWidth = buttons.widthAnchor.constraint(equalToConstant: width - 48)
            buttonsWidth?.isActive = true
        } else {
            buttonsWidth?.constant = width - 48
        }

        frame.size = NSSize(width: width, height: stack.fittingSize.height)
        layoutSubtreeIfNeeded()
        needsDisplay = true
    }

    /// The state of the three buttons. Only one really changes: the repair one.
    ///
    /// It names THE gesture when there is only one — "Démarrer session Amphetamine" is
    /// understood without opening anything — and counts them when there are several. When
    /// there is nothing to repair, it does not disappear: it SAYS so. That was the
    /// request, and it is right — an absent button leaves the question open ("is there a
    /// fix, or am I just not seeing it?"), whereas a switched-off button saying "no
    /// automatic fix" answers it.
    private func prepareFooter(items: [Problem], fixable: [Problem]) {
        fixIsConfirm = false
        if let only = fixable.first, fixable.count == 1 {
            fixButton.set(tone: .action)
            fixButton.set(title: T("alarm.fix", "⚡ Fix it now"),
                          subtitle: only.remedy ?? "")
        } else if fixable.count > 1 {
            fixButton.set(tone: .action)
            fixButton.set(title: T("alarm.fix", "⚡ Fix it now"),
                          subtitle: String(format: T("alarm.fixCount", "%d fixes at once"), fixable.count))
        } else if items.contains(where: { $0.key == "sys:iphonecharge" }) {
            // Nothing to repair automatically, but ONE gesture exists and fits in a word.
            fixIsConfirm = true
            fixButton.set(tone: .action)
            fixButton.set(title: T("alarm.confirmCharge", "🔋 Confirm it is charging"),
                          subtitle: T("alarm.confirmChargeSub", "Only you can see the cable"))
        } else {
            fixButton.set(tone: .off)
            fixButton.set(title: T("alarm.fixNone", "No automatic fix"),
                          subtitle: T("alarm.fixNoneSub", "This one needs your hands"))
        }
    }

    /// One failure: its TYPE icon on the left (the one the engine gives — 🔌 for power,
    /// 🎛 for a MIDI port…), and on the right what is seen then what is to be done.
    ///
    /// A KNOCK-ON failure reads differently, and is drawn differently: no observation, no
    /// advice, one notch more grey. It is not there to be dealt with — it is there so one
    /// understands why the keyboard fell silent without having to wonder whether it is a
    /// second problem. The visual distinction IS the information.
    private func makeRow(_ p: Problem, width: CGFloat, follower: Bool,
                         withCauses: Bool = true) -> NSView {
        let icon = NSTextField(labelWithString: follower ? "↳" : (p.glyph.isEmpty ? "•" : p.glyph))
        icon.font = .systemFont(ofSize: follower ? 20 : 30)
        icon.alignment = .center
        icon.textColor = follower ? NSColor.white.withAlphaComponent(0.45) : .labelColor
        icon.widthAnchor.constraint(equalToConstant: 42).isActive = true

        let name = NSTextField(labelWithString: p.label)
        name.font = .systemFont(ofSize: follower ? 16 : 20, weight: follower ? .semibold : .bold)
        name.textColor = follower
            ? NSColor.white.withAlphaComponent(0.62)
            : (p.status == "fail" ? .systemRed : .systemOrange)

        let texts = NSStackView(views: [name])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 3

        if follower {
            // The link spelled out, because it is what says where to go and look:
            // "powered by the Stream Deck Plus" sends you to a cable, "consequence" on
            // its own sends you nowhere.
            let why = p.causedWhy.isEmpty
                ? T("alarm.knockOnBare", "knock-on failure")
                : String(format: T("alarm.knockOn", "knock-on — %@"), p.causedWhy)
            texts.addArrangedSubview(wrapped(why, size: 13,
                                             color: NSColor.white.withAlphaComponent(0.45),
                                             width: width - 54))
        } else {
            // The observation and the advice arrive glued by a line break (checks._hint):
            // "what I see" then "→ what you can do". We separate them to give the second one
            // the tone of an instruction, without which the two read as a single sentence.
            let parts = p.detail.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            let observed = parts.first.map(String.init) ?? ""
            let advice = parts.count > 1 ? String(parts[1]) : ""
            if !observed.isEmpty {
                texts.addArrangedSubview(wrapped(observed, size: 15, color: .white, width: width - 54))
            }
            if !advice.isEmpty {
                texts.addArrangedSubview(wrapped(advice, size: 14,
                                                 color: NSColor.white.withAlphaComponent(0.72),
                                                 width: width - 54))
            }
            // What this failure brings down — on the CAUSE's line, and from the panel's
            // very first line. That is what lets a narrow screen, where the knock-on lines
            // did not fit, still say the extent of the failure: "the Plus has gone, and
            // with it the XL, the keyboard and the breath".
            if !p.causes.isEmpty && withCauses {
                let list = String(format: T("alarm.causes", "⛓  brings down: %@"),
                                  p.causes.joined(separator: ", "))
                texts.addArrangedSubview(wrapped(list, size: 14, color: .systemOrange,
                                                 width: width - 54))
            }
        }

        let row = NSStackView(views: [icon, texts])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 12
        return row
    }

    private func wrapped(_ s: String, size: CGFloat, color: NSColor, width: CGFloat) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: s)
        f.font = .systemFont(ofSize: size)
        f.textColor = color
        f.preferredMaxLayoutWidth = width
        f.widthAnchor.constraint(equalToConstant: width).isActive = true
        return f
    }
}

// ---------------------------------------------------------------------------
// The assembly: the frame behind, the panel in front
// ---------------------------------------------------------------------------
final class AlarmView: NSView {
    /// What is left to breathe above and below the panel. Enough for the frame's halo to
    /// stay visible behind it — without which the app's most conspicuous alert would be
    /// hidden by its own text.
    static let margin: CGFloat = 100
    private let flash = FlashFrameView(frame: .zero)
    let card = AlarmCard(frame: .zero)
    private var items: [Problem] = []
    private var fixable: [Problem] = []
    private var tint: NSColor = .systemRed

    override init(frame: NSRect) {
        super.init(frame: frame)
        addSubview(flash)
        addSubview(card)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    /// The glass takes a click ONLY on the panel's button; elsewhere it does not exist
    /// as far as the mouse is concerned.
    override func hitTest(_ point: NSPoint) -> NSView? { card.hitTest(point) }

    /// What is ALREADY displayed. The poll comes back every 5 s with, almost always,
    /// exactly the same failures: rebuilding the panel each time would make the text jump
    /// before the eyes of someone in the middle of reading it. We only rebuild if the
    /// content has actually changed.
    private var signature = ""

    func show(_ items: [Problem], fixable: [Problem], level: RigStatus) {
        self.items = items
        self.fixable = fixable
        tint = level == .fail ? .systemRed : .systemOrange
        flash.tint = tint
        rebuild()
    }

    func startPulsing() { flash.startPulsing() }
    func stopPulsing() { flash.stopPulsing() }

    override func layout() {
        super.layout()
        flash.frame = bounds
        rebuild()
    }

    private func rebuild() {
        let w = min(bounds.width - 140, 880)
        // The room the panel is allowed to take. Its TOP is fixed (see further down):
        // what grows goes downwards, so the budget is the distance from that top to the
        // bottom of the screen, minus a margin so as not to die on the edge. On a laptop
        // that makes four or five failures, on a desktop screen a good ten — and that is
        // just as it should be: the panel shows as much as the screen can carry.
        // Centred: the panel then grows on BOTH sides, so the available room is the
        // screen height minus a top margin and a bottom margin.
        let h = max(bounds.height - 2 * Self.margin, 220)
        let sig = items.map { "\($0.key)|\($0.status)|\($0.detail)|\($0.remedy ?? "")|\($0.causedBy ?? "")" }
            .joined(separator: "¦") + "@\(Int(w))x\(Int(h))"
        if sig != signature {
            signature = sig
            card.show(items, fixable: fixable, tint: tint, width: w, maxHeight: h)
        }
        // IN THE CENTRE. The panel used to be in the upper third to spare Ableton's
        // tracks — a good reflex, except that it protected the wrong thing: when the
        // screen announces that something has just broken, the question of the moment is
        // no longer "where is the song at". What one then reads, one must read where the
        // eye falls, without looking for it. And the panel still takes no click at all:
        // what is underneath stays usable by going through it.
        card.setFrameOrigin(NSPoint(x: bounds.midX - card.frame.width / 2,
                                    y: bounds.midY - card.frame.height / 2))
    }
}
