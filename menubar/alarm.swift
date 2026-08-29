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
    /// Le report à échéance — l'alarme se tait, puis REVIENT si rien n'a été réglé.
    ///
    /// C'est un troisième état, et il fallait qu'il en soit un : « arrêter » convient à ce
    /// qu'on a vu et décidé d'ignorer, le report à ce qu'on traitera dans deux morceaux et
    /// qu'on oublierait sans lui. Les confondre, c'est perdre l'un des deux — soit on tait
    /// pour de bon ce qu'on voulait juste différer, soit on laisse clignoter ce qu'on a
    /// déjà jugé.
    private(set) var snoozedUntil: Date?

    var firing: Bool {
        guard !keys.isEmpty, !silenced else { return false }
        if let until = snoozedUntil, Date() < until { return false }
        return true
    }
    /// Ce qu'il reste à courir, pour l'afficher plutôt que de laisser deviner.
    var snoozeRemaining: TimeInterval? {
        guard let until = snoozedUntil, Date() < until else { return nil }
        return until.timeIntervalSinceNow
    }

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
        if !fresh.isEmpty, keys.isEmpty { silenced = false; snoozedUntil = nil }
        keys.formUnion(fresh)
        keys.formIntersection(now)          // réparé → sort de l'alarme
        if keys.isEmpty { silenced = false; snoozedUntil = nil }
        // Une panne réglée pendant le report ferme le report avec elle : ce qui revient
        // ne doit être que ce qui est encore cassé.
        if snoozedUntil != nil, keys.isEmpty { snoozedUntil = nil }

        items = problems.filter { keys.contains($0.key) }
        level = items.contains { $0.status == "fail" } ? .fail : .warn
    }

    mutating func silence() { silenced = true; snoozedUntil = nil }
    mutating func snooze(_ seconds: TimeInterval) { snoozedUntil = Date().addingTimeInterval(seconds); silenced = false }

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
        // Pas jusqu'à l'extinction. Un cadre qui disparaît la moitié du temps se voit
        // MOINS bien qu'un cadre qui bat : entre deux battements, il n'y a plus rien à
        // accrocher du coin de l'œil, et l'œil retourne à Ableton. Le plancher garde une
        // présence rouge permanente ; c'est le battement par-dessus qui fait l'alerte.
        a.toValue = 0.34
        a.duration = 0.42          // plus nerveux que la seconde : on veut « alarme », pas « respiration »
        a.autoreverses = true
        a.repeatCount = .infinity
        a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        layer?.add(a, forKey: "pulse")
    }

    func stopPulsing() { layer?.removeAnimation(forKey: "pulse") }

    /// Un trait épais, DOUBLÉ d'un halo qui se fond vers l'intérieur.
    ///
    /// Le trait seul ne tenait pas sa promesse : sur un grand écran, dix-huit points
    /// collés au bord, c'est une bordure de fenêtre de plus — l'œil, occupé au centre, ne
    /// l'attrape pas. Le halo change ça sans rien voler à la surface utile : il s'éteint
    /// en une centaine de points, mais il donne au battement une SURFACE, et une surface
    /// se voit en vision périphérique là où une ligne ne se voit pas.
    ///
    /// Quatre bandes plutôt qu'un dégradé de couronne : NSGradient ne dessine que du
    /// linéaire et du radial, et un radial éclaire le centre — l'exact contraire de ce
    /// qu'on veut. Les quatre se recouvrent dans les coins, qui s'en trouvent plus
    /// lumineux : c'est un accident, et il tombe bien, les coins sont ce que la vision
    /// périphérique attrape en premier.
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
// Les boutons — gros, et qui disent ce qu'ils font
// ---------------------------------------------------------------------------
/// Un bouton du panneau : un titre qu'on lit de loin, et sous lui une ligne qui dit ce
/// qui se passera.
///
/// Le sous-titre n'est pas de la décoration. « Arrêter l'alarme » et « Rappel dans 5 min »
/// se ressemblent assez pour qu'on hésite une seconde — et une seconde d'hésitation
/// devant un écran rouge, sur scène, c'est déjà trop. La ligne du dessous lève le doute
/// sans qu'on ait à se souvenir de rien.
///
/// Il remplace une ligne d'aide de 12 px en gris à 50 % posée sous les boutons, qui
/// portait la même information et que personne ne pouvait lire.
final class BigButton: NSButton {
    enum Tone { case action, neutral, off }
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
        // Un bouton éteint reste PARFAITEMENT lisible : c'est même sa seule utilité.
        // Griser un bouton jusqu'à l'illisible pour dire « indisponible » supprime
        // l'information qu'on voulait donner — ici, que rien ne peut être réparé tout seul.
        let fg: NSColor = tone == .off ? NSColor.white.withAlphaComponent(0.55) : .white
        let sub: NSColor = tone == .off ? NSColor.white.withAlphaComponent(0.38)
                                        : NSColor.white.withAlphaComponent(0.72)
        layer?.backgroundColor = {
            switch tone {
            case .action:  return NSColor(calibratedRed: 0.13, green: 0.47, blue: 0.26, alpha: 1).cgColor
            case .neutral: return NSColor(calibratedWhite: 0.24, alpha: 1).cgColor
            case .off:     return NSColor(calibratedWhite: 0.14, alpha: 1).cgColor
            }
        }()
        layer?.borderWidth = tone == .off ? 1 : 0
        layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor

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
// Le panneau — fixe, lisible, et actionnable
// ---------------------------------------------------------------------------
/// Construit en vraies sous-vues (et non dessiné à la main) parce qu'il porte un BOUTON :
/// un rectangle peint ne reçoit pas de clic, et le geste de réparation doit être là, sous
/// les yeux, au lieu d'être à chercher dans un menu au pire moment.
final class AlarmCard: NSView {
    private let stack = NSStackView()
    private let title = NSTextField(labelWithString: "")
    /// Trois gestes possibles devant une panne, et un bouton pour chacun — parce que les
    /// trois sont des décisions différentes : la réparer, la classer, ou y revenir. Le
    /// menu 🎹 en offrait un seul, et il fallait l'ouvrir pour le trouver.
    private var fixButton: BigButton!
    private var stopButton: BigButton!
    private var snoozeButton: BigButton!
    private let buttons = NSStackView()
    private var buttonsWidth: NSLayoutConstraint?
    /// Ce que les boutons déclenchent. Posés par le délégué : la carte ne sait pas parler
    /// au moteur, et n'a pas à le savoir.
    var onFix: (() -> Void)?
    var onStop: (() -> Void)?
    var onSnooze: (() -> Void)?

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
        stopButton = BigButton(tone: .neutral, title: T("alarm.stop", "Stop the alarm"),
                               subtitle: T("alarm.stopSub", "The error stays, the screen calms down"),
                               target: self, action: #selector(stopTapped))
        snoozeButton = BigButton(tone: .neutral, title: T("alarm.snooze", "Remind me in 5 min"),
                                 subtitle: T("alarm.snoozeSub", "Comes back if it is still broken"),
                                 target: self, action: #selector(snoozeTapped))
        buttons.orientation = .horizontal
        buttons.distribution = .fillEqually
        buttons.alignment = .top
        buttons.spacing = 10
        [fixButton, stopButton, snoozeButton].forEach { buttons.addArrangedSubview($0!) }
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

    @objc private func fixTapped() { onFix?() }
    @objc private func stopTapped() { onStop?() }
    @objc private func snoozeTapped() { onSnooze?() }

    /// Le bouton est le SEUL endroit qui prend un clic. Partout ailleurs `hitTest` rend
    /// `nil`, donc la fenêtre laisse passer l'événement vers ce qu'il y a dessous — un
    /// panneau d'alerte n'a pas à s'interposer entre le doigt et Ableton.
    override func hitTest(_ point: NSPoint) -> NSView? {
        let local = convert(point, from: superview)
        // Un bouton ÉTEINT reste dans la liste : il occupe sa place, et un clic dessus doit
        // s'arrêter là plutôt que de traverser la vitre et d'atterrir dans Ableton.
        for b in [fixButton, stopButton, snoozeButton] where b != nil && !b!.isHidden && b!.superview != nil {
            if b!.convert(b!.bounds, to: self).contains(local) { return b!.isEnabled ? b! : self }
        }
        return nil
    }

    /// Remplit le panneau — et s'arrête quand la page est pleine, pas à un compte fixe.
    ///
    /// La règle d'avant plafonnait à trois pannes, sur un raisonnement qui tenait : une
    /// quatrième ligne ne change pas le geste, puisque le bouton les répare toutes. Il
    /// manquait juste l'autre moitié — savoir CE QUI est tombé n'a pas pour seul usage
    /// d'appuyer sur un bouton. Sur un 27 pouces, dix lignes tiennent sans effort et
    /// disent d'un coup l'étendue des dégâts ; les plafonner à trois, c'est jeter une
    /// information gratuite. Le vrai plafond n'est donc pas un nombre, c'est la HAUTEUR
    /// disponible, et elle change d'un écran à l'autre.
    ///
    /// L'ordre de remplissage suit l'ordre d'utilité, et c'est là qu'il se joue quelque
    /// chose : d'abord toutes les pannes AUTONOMES — chacune demande son propre geste —
    /// puis seulement, s'il reste de la place, les pannes LIÉES, glissées sous celle qui
    /// les explique. Une conséquence n'apprend rien qu'on ne sache déjà en lisant sa
    /// cause ; elle ne doit pas prendre la ligne d'un problème que personne n'a encore vu.
    func show(_ items: [Problem], fixable: [Problem], tint: NSColor, width: CGFloat,
              maxHeight: CGFloat) {
        layer?.borderColor = tint.cgColor
        title.stringValue = T("alarm.title", "SOMETHING JUST BROKE")
        title.textColor = tint

        // `removeFromSuperview` et non `stack.removeView(_:)` : ce dernier LÈVE une
        // exception quand la vue n'est pas (ou plus) dans la liste — et il l'a fait au
        // tout premier affichage, quand le titre n'y était pas encore. Retirer de la
        // hiérarchie retire aussi de la liste, sans rien supposer de l'état d'avant.
        stack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        stack.addArrangedSubview(title)

        // Le pied de page se prépare AVANT les lignes, pour qu'on sache la place qu'il
        // prendra. Sans cette réservation, la dernière panne ajoutée pousserait le bouton
        // de réparation hors de l'écran — le panneau serait complet et inutilisable.
        prepareFooter(fixable: fixable)
        let footer = 62 + stack.spacing * 2   // hauteur mini d'une rangée de boutons
        let budget = maxHeight - footer

        let rowWidth = width - 48
        let present = Set(items.map(\.key))
        // Une conséquence dont la cause n'est PAS affichée redevient une panne comme les
        // autres : sinon elle serait reléguée au second tour au nom d'un lien que rien à
        // l'écran ne montre.
        func follows(_ p: Problem) -> Bool { p.causedBy.map(present.contains) ?? false }

        var shown: [String] = []            // clés affichées, dans l'ordre du stack

        func fits() -> Bool {
            stack.layoutSubtreeIfNeeded()
            return stack.fittingSize.height <= budget
        }
        /// Ajoute une ligne à l'index demandé et la RETIRE si elle déborde — mesurer
        /// après coup est la seule façon honnête : la hauteur d'un texte qui s'enroule ne
        /// se devine pas, elle se constate.
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

        // Les liées, groupe par groupe et TOUT OU RIEN. Une seule conséquence sur trois,
        // choisie par ce qui restait de place, dirait « le clavier est tombé » et tairait
        // le XL — donnant à croire qu'il va bien. Or la ligne « ⛓ entraîne aussi » les
        // nomme déjà toutes les trois : mieux vaut la laisser faire seule que la
        // contredire à moitié.
        for cause in heads where !cause.causes.isEmpty {
            let group = items.filter { $0.causedBy == cause.key }
            // +1 pour le titre, +1 pour se placer APRÈS la cause : coller la conséquence
            // à sa cause est tout l'intérêt — une ligne étrangère entre les deux et le
            // lien cesse de se voir.
            guard !group.isEmpty, let at = shown.firstIndex(of: cause.key) else { continue }
            var placed: [String] = []
            var complete = true
            for p in group {
                if !place(p, follower: true, at: at + 2 + placed.count) { complete = false; break }
                placed.append(p.key)
            }
            guard complete else { placed.forEach(drop); continue }
            // Les trois lignes sont là, sous leur cause : « entraîne aussi : Stream Deck
            // XL, Clavier, Breath controller » nommerait une deuxième fois ce qu'on lit
            // juste en dessous. Le résumé n'existe QUE pour l'écran où elles ne tiennent
            // pas ; ici il s'efface, et la place qu'il libère revient au panneau.
            stack.arrangedSubviews[at + 1].removeFromSuperview()
            stack.insertArrangedSubview(makeRow(cause, width: rowWidth, follower: false,
                                                withCauses: false), at: at + 1)
        }

        // « +N » ne compte QUE les pannes autonomes restées dehors. Une liée non dépliée
        // n'est pas une panne cachée : son nom est sur la ligne de sa cause, deux lignes
        // plus haut. La compter ici ferait craindre des dégâts qu'on a déjà sous les yeux.
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

    /// L'état des trois boutons. Un seul change vraiment : celui de la réparation.
    ///
    /// Il nomme LE geste quand il n'y en a qu'un — « Démarrer session Amphetamine » se
    /// comprend sans rien ouvrir — et les compte quand il y en a plusieurs. Quand il n'y a
    /// rien à réparer, il ne disparaît pas : il le DIT. C'était la demande, et elle est
    /// juste — un bouton absent laisse la question ouverte (« est-ce qu'il y a un
    /// correctif, ou est-ce que je ne le vois pas ? »), un bouton éteint qui dit « aucune
    /// correction automatique » y répond.
    private func prepareFooter(fixable: [Problem]) {
        if let only = fixable.first, fixable.count == 1 {
            fixButton.set(tone: .action)
            fixButton.set(title: T("alarm.fix", "Fix it now"),
                          subtitle: only.remedy ?? "")
        } else if fixable.count > 1 {
            fixButton.set(tone: .action)
            fixButton.set(title: T("alarm.fix", "Fix it now"),
                          subtitle: String(format: T("alarm.fixCount", "%d fixes at once"), fixable.count))
        } else {
            fixButton.set(tone: .off)
            fixButton.set(title: T("alarm.fixNone", "No automatic fix"),
                          subtitle: T("alarm.fixNoneSub", "This one needs your hands"))
        }
    }

    /// Une panne : son icône de TYPE à gauche (celle que le moteur donne — 🔌 pour
    /// l'alimentation, 🎛 pour un port MIDI…), et à droite ce qu'on voit puis ce qu'on fait.
    ///
    /// Une panne LIÉE se lit autrement, et se dessine autrement : pas de constat, pas de
    /// conseil, un cran de gris en plus. Elle n'est pas là pour être traitée — elle est là
    /// pour qu'on comprenne pourquoi le clavier s'est tu sans avoir à se demander si c'est
    /// un deuxième problème. La distinction visuelle EST l'information.
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
            // Le lien en toutes lettres, parce que c'est lui qui dit où aller regarder :
            // « alimenté par le Stream Deck Plus » envoie vers un câble, « conséquence »
            // tout seul n'envoie nulle part.
            let why = p.causedWhy.isEmpty
                ? T("alarm.knockOnBare", "knock-on failure")
                : String(format: T("alarm.knockOn", "knock-on — %@"), p.causedWhy)
            texts.addArrangedSubview(wrapped(why, size: 13,
                                             color: NSColor.white.withAlphaComponent(0.45),
                                             width: width - 54))
        } else {
            // Le constat et le conseil arrivent collés par un saut de ligne (checks._hint) :
            // « ce que je vois » puis « → ce que tu peux faire ». On les sépare pour donner au
            // second le ton d'une consigne, sans quoi les deux se lisent comme une seule phrase.
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
            // Ce que cette panne entraîne — sur la ligne de la CAUSE, et dès la première
            // ligne du panneau. C'est ce qui permet à un écran étroit, où les lignes liées
            // n'ont pas tenu, de dire quand même l'étendue de la panne : « le Plus a lâché,
            // et avec lui le XL, le clavier et le breath ».
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
// L'assemblage : le cadre derrière, le panneau devant
// ---------------------------------------------------------------------------
final class AlarmView: NSView {
    /// Ce qu'on laisse respirer en haut et en bas du panneau. Assez pour que le halo du
    /// cadre reste visible derrière lui — sans quoi l'alerte la plus voyante de l'app
    /// serait cachée par son propre texte.
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
        // La place que le panneau a le droit de prendre. Son HAUT est fixe (voir plus bas) :
        // ce qui grandit descend, donc le budget est la distance de ce haut au bas de
        // l'écran, moins une marge pour ne pas venir mourir sur le bord. Sur un portable
        // cela fait quatre ou cinq pannes, sur un écran de bureau une bonne dizaine — et
        // c'est très bien ainsi : le panneau montre autant que l'écran peut en porter.
        // Centrée : le panneau grandit alors des DEUX côtés, donc la place disponible
        // est la hauteur d'écran moins une marge haute et une marge basse.
        let h = max(bounds.height - 2 * Self.margin, 220)
        let sig = items.map { "\($0.key)|\($0.status)|\($0.detail)|\($0.remedy ?? "")|\($0.causedBy ?? "")" }
            .joined(separator: "¦") + "@\(Int(w))x\(Int(h))"
        if sig != signature {
            signature = sig
            card.show(items, fixable: fixable, tint: tint, width: w, maxHeight: h)
        }
        // AU CENTRE. Le panneau était au tiers supérieur pour épargner les pistes
        // d'Ableton — un bon réflexe, sauf qu'il protégeait la mauvaise chose : quand
        // l'écran annonce que quelque chose vient de lâcher, la question du moment n'est
        // plus « où en est le morceau ». Ce qu'on lit alors, on doit le lire là où l'œil
        // tombe, sans le chercher. Et le panneau ne prend toujours aucun clic : ce qui est
        // dessous reste utilisable en le traversant.
        card.setFrameOrigin(NSPoint(x: bounds.midX - card.frame.width / 2,
                                    y: bounds.midY - card.frame.height / 2))
    }
}
