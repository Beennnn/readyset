// RigMenuBar — l'ALARME : ce qui casse alors que tout allait bien.
//
// Le liseré et la pastille répondent à « où en est le rig ? » — un état, qu'on lit quand
// on y pense. L'alarme répond à une autre question, et c'est pour ça qu'elle existe à
// part : « quelque chose vient de LÂCHER ». Un Mac dont on arrache l'alimentation entre
// deux morceaux ne demande pas d'être consulté, il demande à être VU — sans qu'on ait
// levé les yeux, sans qu'on ait ouvert un menu.
//
// D'où les quatre partis pris :
//
//   • Elle ne se déclenche que sur une RÉGRESSION. Un rig déjà rouge au lancement n'a
//     rien de soudain : c'est la mise en place, on la lit dans la liste. Ce qui mérite un
//     clignotement, c'est ce qui apparaît APRÈS que l'écran soit devenu propre.
//   • Elle clignote tant que ce qui est apparu n'est pas réparé — pas « pendant 10 s ».
//     Une alerte qui s'éteint toute seule ne prouve rien : on l'a peut-être ratée.
//   • SEUL LE CADRE CLIGNOTE. Le panneau, lui, ne bouge pas d'un pixel : c'est du texte à
//     LIRE — ce qui a lâché, ce qu'on observe, le geste qui répare. Un texte qui clignote
//     se lit deux fois moins vite qu'un texte fixe, et l'urgence n'est pas une raison de
//     ralentir la lecture ; c'en est une de l'accélérer. Le clignotement attire l'œil, le
//     panneau l'informe. Deux rôles, deux surfaces.
//   • Elle ne prend AUCUN clic — sauf son bouton de correction. Sur scène, une surface qui
//     intercepte un clic destiné à Ableton est un bug plus grave que la panne qu'elle
//     signale ; mais un panneau qui dit « voilà le correctif » sans l'offrir oblige à
//     aller le chercher dans un menu, au pire moment. Le trou dans la vitre fait
//     exactement la taille du bouton (voir `hitTest`).

import Cocoa

// ---------------------------------------------------------------------------
// La machine à états — qui a lâché, depuis quand, et est-ce réparé ?
// ---------------------------------------------------------------------------
/// Suit les problèmes APPARUS depuis la dernière ardoise propre, et rien d'autre.
///
/// Le point subtil est le suivi par CLÉ plutôt que par verdict global. « Tant que le rig
/// n'est pas vert » aurait été plus simple, et faux : un avertissement permanent (une app
/// ouverte qu'on garde exprès) aurait fait clignoter l'écran toute la soirée, ce qui
/// revient à ne plus rien signaler du tout. Ici, seule la panne qui vient d'arriver
/// clignote — et elle s'éteint dès qu'ELLE est réglée, même si le reste n'est pas vert.
struct AlarmState {
    /// Les clés apparues et pas encore réparées. Vide = rien à signaler.
    private(set) var keys: Set<String> = []
    /// Les problèmes correspondants, dans l'ordre où /api/state les donne (bloquants
    /// d'abord) : c'est eux que le panneau affiche, avec leur constat et leur correctif.
    private(set) var items: [Problem] = []
    /// Le niveau le plus grave parmi eux — rouge s'il y a un bloquant, sinon orange.
    private(set) var level: RigStatus = .fail
    /// Ce que le sondage précédent montrait : c'est la comparaison avec lui qui définit
    /// « nouveau ». Sans mémoire du coup d'avant, tout problème durable serait neuf à
    /// chaque tour et l'alarme ne s'arrêterait jamais.
    private var known: Set<String> = []
    /// Le tout premier état reçu ne déclenche RIEN : il n'a pas de « avant » auquel se
    /// comparer. Un rig lancé déjà cassé n'est pas une régression, c'est une mise en place.
    private var seeded = false
    /// Tue (`silence()`) le clignotement de l'épisode en cours sans effacer les clés : le
    /// liseré reste, la liste reste, seule l'alarme se calme. Un problème qu'on ne peut pas
    /// réparer maintenant ne doit pas condamner l'écran pour le reste du set.
    private(set) var silenced = false

    var firing: Bool { !keys.isEmpty && !silenced }

    /// À appeler à CHAQUE sondage, avec les problèmes qu'on veut surveiller (les bloquants,
    /// plus les avertissements si l'option le demande).
    mutating func update(status: RigStatus, problems: [Problem]) {
        // Moteur muet : on gèle tout. Ni déclenchement (une réponse absente ne prouve
        // aucune panne du rig), ni extinction (elle effacerait une alarme en cours au
        // moment le plus mal choisi, celui où le dashboard vient lui aussi de tomber).
        guard status != .unreachable else { return }

        let now = Set(problems.map(\.key))
        defer { known = now }
        guard seeded else { seeded = true; return }

        let fresh = now.subtracting(known)
        // Nouvel épisode : un « tais-toi » ne vaut que pour les pannes qu'on connaissait
        // en le prononçant. Ce qui casse ENSUITE a droit à son clignotement.
        if !fresh.isEmpty, keys.isEmpty { silenced = false }
        keys.formUnion(fresh)
        keys.formIntersection(now)          // réparé → sort de l'alarme
        if keys.isEmpty { silenced = false }

        items = problems.filter { keys.contains($0.key) }
        level = items.contains { $0.status == "fail" } ? .fail : .warn
    }

    mutating func silence() { silenced = true }

    /// Ce que le bouton de correction peut lancer : les pannes de l'alarme qui ont un
    /// remède. Vide = pas de bouton, et le panneau le DIT au lieu d'en offrir un qui ne
    /// ferait rien.
    var fixable: [Problem] { items.filter { $0.remedy != nil } }
}

// ---------------------------------------------------------------------------
// Le cadre qui pulse — et rien d'autre ne pulse
// ---------------------------------------------------------------------------
final class FlashFrameView: NSView {
    var tint: NSColor = .systemRed { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    /// Le clignotement, posé sur le CALQUE et non sur l'opacité de la fenêtre : une
    /// animation de calque tourne dans le processus de rendu, elle ne coûte donc rien au
    /// fil principal — celui-là même qui sonde le moteur pendant que le set tourne.
    func startPulsing() {
        guard layer?.animation(forKey: "pulse") == nil else { return }
        let a = CABasicAnimation(keyPath: "opacity")
        a.fromValue = 1.0
        a.toValue = 0.10
        a.duration = 0.55
        a.autoreverses = true
        a.repeatCount = .infinity
        a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        layer?.add(a, forKey: "pulse")
    }

    func stopPulsing() { layer?.removeAnimation(forKey: "pulse") }

    override func draw(_ dirty: NSRect) {
        let thickness: CGFloat = 18
        tint.withAlphaComponent(0.95).setStroke()
        let frame = NSBezierPath(rect: bounds.insetBy(dx: thickness / 2, dy: thickness / 2))
        frame.lineWidth = thickness
        frame.stroke()
    }
}

// ---------------------------------------------------------------------------
// Le panneau — fixe, lisible, et actionnable
// ---------------------------------------------------------------------------
/// Construit en vraies sous-vues (et non dessiné à la main) parce qu'il porte un BOUTON :
/// un rectangle peint ne reçoit pas de clic, et le geste de réparation doit être là, sous
/// les yeux, au lieu d'être à chercher dans un menu au pire moment.
final class AlarmCard: NSView {
    private let stack = NSStackView()
    private let title = NSTextField(labelWithString: "")
    private let fixButton = NSButton(title: "", target: nil, action: nil)
    private let hint = NSTextField(labelWithString: "")
    private var rows: [NSView] = []
    /// Ce que le bouton déclenche. Posé par le délégué : la carte ne sait pas parler au
    /// moteur, et n'a pas à le savoir.
    var onFix: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = 20
        layer?.backgroundColor = NSColor.black.withAlphaComponent(0.90).cgColor
        layer?.borderWidth = 4

        title.font = .systemFont(ofSize: 27, weight: .heavy)
        title.lineBreakMode = .byTruncatingTail

        fixButton.bezelStyle = .regularSquare
        fixButton.controlSize = .large
        fixButton.target = self
        fixButton.action = #selector(fixTapped)
        fixButton.isBordered = true
        fixButton.setButtonType(.momentaryPushIn)

        hint.font = .systemFont(ofSize: 12)
        hint.textColor = NSColor.white.withAlphaComponent(0.5)

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

    @objc private func fixTapped() { onFix?() }

    /// Le bouton est le SEUL endroit qui prend un clic. Partout ailleurs `hitTest` rend
    /// `nil`, donc la fenêtre laisse passer l'événement vers ce qu'il y a dessous — un
    /// panneau d'alerte n'a pas à s'interposer entre le doigt et Ableton.
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard !fixButton.isHidden else { return nil }
        let inButton = fixButton.convert(fixButton.bounds, to: self).contains(convert(point, from: superview))
        return inButton ? fixButton : nil
    }

    func show(_ items: [Problem], fixable: [Problem], tint: NSColor, width: CGFloat) {
        layer?.borderColor = tint.cgColor
        title.stringValue = T("alarm.title", "SOMETHING JUST BROKE")
        title.textColor = tint

        // `removeFromSuperview` et non `stack.removeView(_:)` : ce dernier LÈVE une
        // exception quand la vue n'est pas (ou plus) dans la liste — et il l'a fait au
        // tout premier affichage, quand le titre n'y était pas encore. Retirer de la
        // hiérarchie retire aussi de la liste, sans rien supposer de l'état d'avant.
        stack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        rows.removeAll()

        stack.addArrangedSubview(title)
        // Trois pannes au plus : au-delà, le panneau devient un mur de texte qu'on ne lit
        // pas — et une quatrième ligne ne change pas le geste (le bouton les répare toutes).
        for p in items.prefix(3) {
            let row = makeRow(p, width: width - 48)
            rows.append(row)
            stack.addArrangedSubview(row)
        }
        if items.count > 3 {
            let more = NSTextField(labelWithString: String(format: T("alarm.more", "+%d more"), items.count - 3))
            more.font = .systemFont(ofSize: 13)
            more.textColor = NSColor.white.withAlphaComponent(0.6)
            rows.append(more)
            stack.addArrangedSubview(more)
        }

        // Le bouton nomme LE geste quand il n'y en a qu'un (« Démarrer session
        // Amphetamine » se comprend sans rien ouvrir), et les compte quand il y en a
        // plusieurs. Sans remède, pas de bouton du tout : en offrir un qui ne ferait rien
        // est pire que de dire honnêtement que la main doit s'en mêler.
        if let only = fixable.first, fixable.count == 1 {
            fixButton.isHidden = false
            fixButton.title = "⚡  " + (only.remedy ?? T("alarm.fix", "Fix it now"))
        } else if fixable.count > 1 {
            fixButton.isHidden = false
            fixButton.title = String(format: "⚡  " + T("alarm.fixAll", "Fix these %d now"), fixable.count)
        } else {
            fixButton.isHidden = true
        }
        if !fixButton.isHidden {
            fixButton.font = .systemFont(ofSize: 17, weight: .semibold)
            fixButton.contentTintColor = .white
            stack.addArrangedSubview(fixButton)
        }

        hint.stringValue = fixable.isEmpty
            ? T("alarm.manualOnly", "No automatic fix — this one needs your hands.  ·  menu 🎹 → Silence the alarm")
            : T("alarm.hint", "menu 🎹 → Silence the alarm")
        stack.addArrangedSubview(hint)

        frame.size = NSSize(width: width, height: stack.fittingSize.height)
        layoutSubtreeIfNeeded()
        needsDisplay = true
    }

    /// Une panne : son icône de TYPE à gauche (celle que le moteur donne — 🔌 pour
    /// l'alimentation, 🎛 pour un port MIDI…), et à droite ce qu'on voit puis ce qu'on fait.
    private func makeRow(_ p: Problem, width: CGFloat) -> NSView {
        let icon = NSTextField(labelWithString: p.glyph.isEmpty ? "•" : p.glyph)
        icon.font = .systemFont(ofSize: 30)
        icon.alignment = .center
        icon.widthAnchor.constraint(equalToConstant: 42).isActive = true

        let name = NSTextField(labelWithString: p.label)
        name.font = .systemFont(ofSize: 20, weight: .bold)
        name.textColor = p.status == "fail" ? .systemRed : .systemOrange

        // Le constat et le conseil arrivent collés par un saut de ligne (checks._hint) :
        // « ce que je vois » puis « → ce que tu peux faire ». On les sépare pour donner au
        // second le ton d'une consigne, sans quoi les deux se lisent comme une seule phrase.
        let parts = p.detail.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
        let observed = parts.first.map(String.init) ?? ""
        let advice = parts.count > 1 ? String(parts[1]) : ""

        let texts = NSStackView(views: [name])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 3
        if !observed.isEmpty {
            texts.addArrangedSubview(wrapped(observed, size: 15, color: .white, width: width - 54))
        }
        if !advice.isEmpty {
            texts.addArrangedSubview(wrapped(advice, size: 14,
                                             color: NSColor.white.withAlphaComponent(0.72),
                                             width: width - 54))
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
// L'assemblage : le cadre derrière, le panneau devant
// ---------------------------------------------------------------------------
final class AlarmView: NSView {
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

    /// La vitre ne prend un clic QUE sur le bouton du panneau ; ailleurs elle n'existe pas
    /// pour la souris.
    override func hitTest(_ point: NSPoint) -> NSView? { card.hitTest(point) }

    /// Ce qui est DÉJÀ affiché. Le sondage revient toutes les 5 s avec, presque toujours,
    /// exactement les mêmes pannes : reconstruire le panneau à chaque fois ferait sauter le
    /// texte sous les yeux de quelqu'un en train de le lire. On ne rebâtit que si le
    /// contenu a réellement changé.
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
        let sig = items.map { "\($0.key)|\($0.status)|\($0.detail)|\($0.remedy ?? "")" }
            .joined(separator: "¦") + "@\(Int(w))"
        if sig != signature {
            signature = sig
            card.show(items, fixable: fixable, tint: tint, width: w)
        }
        // Au tiers SUPÉRIEUR, jamais au centre : le centre de l'écran, c'est là que se
        // lisent les pistes d'Ableton pendant le morceau. Le panneau ne prend pas les
        // clics, mais il prend la place — et la place utile ne se reprend pas.
        card.setFrameOrigin(NSPoint(x: bounds.midX - card.frame.width / 2,
                                    y: bounds.height * 0.70 - card.frame.height))
    }
}
