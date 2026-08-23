"""Régler la SORTIE audio d'Ableton dans l'application déjà ouverte.

Le travail réel est fait par `live-output`, qui vit dans son PROPRE dépôt
(github.com/Beennnn/ableton-live-output) et s'installe en lien symbolique dans
`~/.local/bin`. Il n'a rien à voir avec un rig : il règle la sortie audio de Live en
ligne de commande, point — donc il se teste, se publie et se corrige tout seul, et les
deux jumeaux le CONSOMMENT au lieu d'en porter chacun une copie qui dériverait.

Pourquoi ce détour par l'interface plutôt qu'un réglage propre : Live n'a pas de
dictionnaire AppleScript, son fichier de préférences est binaire et réécrit à la
fermeture, et son API Python ne touche pas au matériel audio. Le README du dépôt le
détaille.

Ce module ne fait que le CHOIX et l'APPEL : quelle sortie veut-on dans ce mode, et
qu'est-ce que le script en a fait. Il est utilisé aux deux endroits qui en ont besoin —
la mise en place (`rig preflight`) et le bouton de correction du dashboard — pour que les
deux fassent exactement la même chose.

⚠️ Rien ici ne LIT la sortie courante. Ce serait tentant, et ce serait un piège : lire
par l'accessibilité oblige à mettre Live au premier plan et à ouvrir sa fenêtre de
réglages. Toutes les 4 secondes dans la boucle d'état, ce serait ingérable — et sur scène,
catastrophique. La lecture passive reste le Log.txt (voir checks.check_live_output).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# macOS refuse de piloter une interface de trois façons différentes, et les trois sortent
# ici sous forme d'un message AppleScript brut que personne ne peut interpréter à cinq
# minutes du concert. Elles disent pourtant toutes la même chose : « ce processus-ci n'a
# pas le droit ». Le 2026-08-22, la mise en place a rendu « 3960:4307: execution error:
# Erreur dans System Events : osascript n'est pas autorisé à un accès d'aide. (-25211) »
# — vrai, illisible, et surtout affiché à côté d'un « ✔ » puisque le préflight comptait
# alors les échecs comme des correctifs appliqués.
#
# ⚠️ L'autorisation se donne PAR PROCESSUS APPELANT : un terminal autorisé ne donne rien
# au service lancé par launchd, qui est un autre processus responsable. C'est exactement
# le piège tombé le 2026-08-22 — la sortie se réglait à la main depuis un terminal, et le
# même script échouait depuis le dashboard.
_DENIED_MARKERS = (
    "-25211", "accès d’aide", "accès d'aide", "assistive access",   # lecture d'interface
    "(1002)", "envoyer de saisies", "envoyer des saisies", "send keystrokes",  # envoi de frappes
    "-1743", "not allowed to send apple events", "envoyer des apple",    # automatisation
)

DENIED_HINT = ("le service n'a pas le droit de piloter Ableton — cocher le processus qui "
               "lance le dashboard dans Réglages Système › Confidentialité et sécurité › "
               "Accessibilité, puis relancer l'agent "
               "(launchctl kickstart -k gui/$UID/com.readyset.dashboard)")


def denied(text: str) -> bool:
    """Ce message est-il un refus d'autorisation macOS (et pas un vrai échec du réglage) ?"""
    low = (text or "").lower()
    return any(m.lower() in low for m in _DENIED_MARKERS)


# Cherché par chemin, comme sd-power : le script est installé, pas embarqué. Absent, tout
# ici rend un message clair — le rig ne dépend pas de lui pour démarrer, il perd seulement
# la capacité de corriger la sortie tout seul.
_CANDIDATES = (Path.home() / ".local/bin/live-output",
               Path("/opt/homebrew/bin/live-output"),
               Path("/usr/local/bin/live-output"))


def script() -> Path | None:
    return next((p for p in _CANDIDATES if p.exists()), None)


def wanted(cfg: dict, mode: str) -> str | None:
    """La sortie visée dans ce mode : la PREMIÈRE de `live_output`.

    La liste est un ensemble d'acceptables (le check en valide n'importe lequel) ; pour
    agir il faut en désigner une, et l'ordre de la liste porte déjà cette préférence.
    """
    wants = cfg.get("modes", {}).get(mode, {}).get("live_output") or []
    return wants[0] if wants else None


def apply(cfg: dict, mode: str, dry: bool = False) -> tuple[bool, str]:
    want = wanted(cfg, mode)
    if not want:
        return False, f"aucune sortie attendue déclarée pour le mode {mode}"
    exe = script()
    if exe is None:
        return False, ("live-output n'est pas installé — "
                       "github.com/Beennnn/ableton-live-output, puis ./install.sh")
    if dry:
        return True, f"[dry-run] réglerait la sortie d'Ableton sur « {want} »"
    try:
        p = subprocess.run([str(exe), want], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # 60 s est très large pour quelques clics : si on y arrive, c'est que Live ne
        # répond plus à l'accessibilité, pas que l'opération est longue.
        return False, "Live n'a pas répondu (accessibilité bloquée ?)"
    # Les DEUX flux, pas l'un OU l'autre : `live-output` réémet l'erreur brute d'AppleScript
    # sur stdout ET écrit sa traduction actionnable sur stderr. Prendre `stdout or stderr`
    # gardait donc systématiquement le message illisible et jetait celui qui sert.
    out = [l for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if l.strip()]
    joined = "\n".join(out)
    # code 4 = le script a lui-même reconnu le refus d'accessibilité (voir son README).
    if p.returncode == 4 or denied(joined):
        return False, DENIED_HINT
    msg = out[-1] if out else f"code {p.returncode}"
    return p.returncode == 0, msg
