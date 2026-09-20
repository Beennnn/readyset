"""The Result type — what every check returns, and the four levels it can return.

Lives in core/ rather than in checks/ because three different worlds depend on it and
none of them should have to import the checks to speak about their outcome: the CLI
prints Results, the web dashboard serialises them, and cascade.py reasons about them.

The checks themselves are in readyset/checks/, one family per file.
"""

from __future__ import annotations

from dataclasses import dataclass


# Quatre niveaux, du plus calme au plus grave. INFO est SOUS l'avertissement : il dit
# « absent, et c'est normal » — un équipement facultatif qu'on n'a simplement pas branché
# ce soir (lampes d'ambiance, interface audio au bureau). C'est le niveau qui manquait :
# tout mettre en WARN faisait clignoter en orange des choses dont personne ne se soucie,
# et à force on ne lit plus les oranges du tout — y compris les vrais.
# Un check INFO ne rend PAS le rig « non prêt » : `worst()` l'ignore, le code de sortie
# reste 0, et la pastille/le liseré ne s'allument pas.
OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
_ICON = {OK: "✅", INFO: "ℹ️ ", WARN: "⚠️ ", FAIL: "❌"}

# Valeur spéciale acceptée partout où une sévérité se règle (breath_severity,
# interface_severity, network_severity, lamp_severity, [[checks.audio_devices]].severity…) :
# le check ne s'exécute PAS et n'apparaît nulle part dans ce mode.
#
# À ne pas confondre avec INFO. "info" dit « absent, et c'est normal » — la ligne reste
# affichée, on sait que la question a été posée. "off" dit « la question n'a pas de sens
# ici » : sur scène le son sort du P-225, la RME n'est même pas dans le sac, donc afficher
# « RME non détectée » chaque soir n'apprend rien à personne et allonge la liste à lire
# avant de jouer. Ne l'utiliser que pour ça — masquer un vrai problème derrière "off",
# c'est se rendre aveugle.
OFF = "off"


@dataclass
class Result:
    key: str        # stable id for state tracking (e.g. "app:Ableton")
    label: str      # human label
    status: str     # ok | info | warn | fail
    detail: str = ""

    @property
    def icon(self) -> str:
        return _ICON[self.status]

    @property
    def ok(self) -> bool:
        return self.status == OK

    # Le contenu d'un check COMPOSITE : ses sous-éléments, chacun vu ou pas encore.
    # Un check reste UNE ligne — c'est ce qui garde la liste lisible d'un coup d'œil —
    # mais il énumérait alors ses manquants dans son texte, où toute surface étroite les
    # tronque (« … : Pédale, Notes, Souff… »). Détaillés ici, ceux qui ont la place les
    # déplient au lieu de les couper. None quand le check n'a rien à détailler.
    # Chacun : {"name": str, "ok": bool, "icon": str} — l'icône illustre le GESTE à faire,
    # et se lit avant le mot ; elle est facultative, un nom seul reste parfaitement lisible.
    parts: list[dict] | None = None

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label,
             "status": self.status, "detail": self.detail}
        if self.parts is not None:
            d["parts"] = [{"name": p["name"], "ok": bool(p.get("ok")),
                           "icon": p.get("icon") or ""} for p in self.parts]
        return d


def _ago(seconds: float | None) -> str:
    """« il y a 12 s » / « il y a 4 min » — l'âge d'une observation, en toutes lettres.

    Un horodatage brut oblige à faire la soustraction de tête, juste avant de jouer.
    """
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:                       # au-delà d'une heure et demie, « 120 min »
        return f"{int(seconds // 60)} min"   # oblige à diviser de tête pour situer
    return f"{seconds / 3600:.0f} h"


def _hint(observed: str, advice: str) -> str:
    """Colle un conseil au constat : « ce que je vois → ce que tu peux faire ».

    Un check rouge sans conseil oblige à se souvenir du geste — et sur scène, cinq
    minutes avant de jouer, c'est exactement ce qui manque. Le constat reste devant
    (c'est lui qui est vrai), le conseil suit. Réservé au matériel : quand une
    résolution automatique existe, c'est un bouton de remedy.py qu'il faut, pas une
    phrase (personne ne lit un conseil qu'une machine pourrait exécuter).
    """
    # Le saut de ligne est SÉMANTIQUE, pas décoratif : chaque affichage le rend à sa
    # façon. Le dashboard est en `white-space: pre-line` et met le conseil sur sa propre
    # ligne — avant, un `nowrap` + ellipse coupait les conseils en plein milieu, ce qui
    # est pire que pas de conseil. Le terminal, lui, les recolle et enroule tout seul.
    return f"{observed}\n→ {advice}"


def worst(results: list[Result]) -> str:
    """Pire niveau du lot. INFO n'apparaît JAMAIS ici : c'est une annotation par ligne,
    pas un état du rig — un rig dont tout l'optionnel est débranché reste « prêt »."""
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status == WARN for r in results):
        return WARN
    return OK
