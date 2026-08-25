// RigMenuBar — l'ALARME : ce qui casse alors que tout allait bien.
//
// Le liseré et la pastille répondent à « où en est le rig ? » — un état, qu'on lit quand
// on y pense. L'alarme répond à une autre question, et c'est pour ça qu'elle existe à
// part : « quelque chose vient de LÂCHER ». Un Mac dont on arrache l'alimentation entre
// deux morceaux ne demande pas d'être consulté, il demande à être VU — sans qu'on ait
// levé les yeux, sans qu'on ait ouvert un menu.
//
// D'où les trois partis pris :
//
//   • Elle ne se déclenche que sur une RÉGRESSION. Un rig déjà rouge au lancement n'a
//     rien de soudain : c'est la mise en place, on la lit dans la liste. Ce qui mérite un
//     clignotement, c'est ce qui apparaît APRÈS que l'écran soit devenu propre.
//   • Elle clignote tant que ce qui est apparu n'est pas réparé — pas « pendant 10 s ».
//     Une alerte qui s'éteint toute seule ne prouve rien : on l'a peut-être ratée.
//   • Elle ne prend AUCUN clic (fenêtre transparente aux événements). Sur scène, une
//     surface qui intercepte un clic destiné à Ableton est un bug plus grave que la panne
//     qu'elle signale. Pour la taire, on passe par le menu 🎹.

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
    /// Leurs libellés, dans l'ordre où /api/state les donne (bloquants d'abord).
    private(set) var labels: [String] = []
    /// Le niveau le plus grave parmi elles — rouge s'il y a un bloquant, sinon orange.
    private(set) var level: RigStatus = .fail
    /// Ce que le sondage précédent montrait : c'est la comparaison avec lui qui définit
    /// « nouveau ». Sans mémoire du coup d'avant, tout problème durable serait neuf à
    /// chaque tour et l'alarme ne s'arrêterait jamais.
    private var known: Set<String> = []
    /// Le tout premier état reçu ne déclenche RIEN : il n'a pas de « avant » auquel se
    /// comparer. Un rig lancé déjà cassé n'est pas une régression, c'est une mise en place.
    private var seeded = false
    /// Tue (`silence()`) le clignotement de l'épisode en cours, sans effacer les clés :
    /// le liseré rouge reste, la liste reste, seule l'alarme se calme. Un problème qu'on
    /// ne peut pas réparer maintenant ne doit pas condamner l'écran pour le reste du set.
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

        let mine = problems.filter { keys.contains($0.key) }
        labels = mine.map(\.label)
        level = mine.contains { $0.status == "fail" } ? .fail : .warn
    }

    mutating func silence() { silenced = true }
}

// ---------------------------------------------------------------------------
// La vue — un bandeau qui pulse, plus un cadre épais
// ---------------------------------------------------------------------------
/// Dessinée à la main plutôt qu'assemblée en sous-vues : tout le contenu vit dans un seul
/// calque, donc une seule animation d'opacité les fait pulser ENSEMBLE. Avec des NSTextField
/// empilés, chacun aurait eu la sienne et le décalage se serait vu.
final class AlarmView: NSView {
    var labels: [String] = [] { didSet { needsDisplay = true } }
    var level: RigStatus = .fail { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) non utilisé") }

    /// Le clignotement. Posé sur le calque et non sur `alphaValue` de la fenêtre : une
    /// animation de calque tourne dans le processus de rendu, elle ne coûte donc rien au
    /// fil principal — celui-là même qui sonde le moteur pendant que le set tourne.
    func startPulsing() {
        guard layer?.animation(forKey: "pulse") == nil else { return }
        let a = CABasicAnimation(keyPath: "opacity")
        a.fromValue = 1.0
        a.toValue = 0.12
        a.duration = 0.55
        a.autoreverses = true
        a.repeatCount = .infinity
        a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        layer?.add(a, forKey: "pulse")
    }

    func stopPulsing() { layer?.removeAnimation(forKey: "pulse") }

    override func draw(_ dirty: NSRect) {
        guard !labels.isEmpty else { return }
        let tint = level == .fail ? NSColor.systemRed : NSColor.systemOrange

        // 1. Le cadre — le même que le liseré d'état, mais plus épais : il se superpose au
        //    sien et l'épaissit visuellement, au lieu de le contredire avec une autre forme.
        let thickness: CGFloat = 18
        tint.withAlphaComponent(0.95).setStroke()
        let frame = NSBezierPath(rect: bounds.insetBy(dx: thickness / 2, dy: thickness / 2))
        frame.lineWidth = thickness
        frame.stroke()

        // 2. Le bandeau. Placé au tiers SUPÉRIEUR et non au centre : le centre de l'écran,
        //    c'est là que se lisent les pistes d'Ableton pendant le morceau. Le bandeau est
        //    transparent aux clics, mais pas au regard — il ne doit rien recouvrir d'utile.
        let w = min(bounds.width - 120, 900)
        let h: CGFloat = 132
        let box = NSRect(x: bounds.midX - w / 2, y: bounds.height * 0.62, width: w, height: h)

        NSColor.black.withAlphaComponent(0.88).setFill()
        let card = NSBezierPath(roundedRect: box, xRadius: 20, yRadius: 20)
        card.fill()
        tint.setStroke()
        card.lineWidth = 4
        card.stroke()

        // Titre : ce qui vient de casser, pas « erreur ». Le mot doit se comprendre de
        // trois mètres, de biais, pendant qu'on joue.
        draw(T("alarm.title", "⚠️  SOMETHING JUST BROKE"),
             in: box.insetBy(dx: 24, dy: 0), top: 18, size: 30, weight: .heavy, color: tint)

        // Les libellés en toutes lettres : « Alimentation Mac » se répare, « 1 bloquant »
        // s'interprète. Deux au plus — au-delà, le compte, sinon le bandeau devient un mur
        // de texte qu'on ne lit pas.
        let shown = labels.prefix(2).joined(separator: "   ·   ")
        let rest = labels.count > 2 ? "   +\(labels.count - 2)" : ""
        draw(shown + rest, in: box.insetBy(dx: 24, dy: 0), top: 62, size: 19,
             weight: .semibold, color: .white)

        draw(T("alarm.hint", "menu 🎹 → Silence the alarm"),
             in: box.insetBy(dx: 24, dy: 0), top: 98, size: 12,
             weight: .regular, color: NSColor.white.withAlphaComponent(0.55))
    }

    /// Une ligne centrée, à `top` points sous le haut de `box` (coordonnées écran : l'axe
    /// Y monte, d'où la soustraction — la faire ici une fois plutôt qu'à chaque appel).
    private func draw(_ text: String, in box: NSRect, top: CGFloat, size: CGFloat,
                      weight: NSFont.Weight, color: NSColor) {
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        style.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: style,
        ]
        let line = NSRect(x: box.minX, y: box.maxY - top - size * 1.3,
                          width: box.width, height: size * 1.3)
        (text as NSString).draw(in: line, withAttributes: attrs)
    }
}
