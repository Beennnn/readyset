"""Fenêtres des apps du rig — tout doit TOURNER, rien ne doit se VOIR.

Sur scène l'écran ne montre que le set Ableton. Bome MIDI Translator, Bome Network,
Stream Deck et Stage Traxx doivent tourner (c'est eux qui portent le routing MIDI, les
boutons et les bandes), mais leurs fenêtres sont du bruit : un clic de travers et on
regarde un journal de traducteur MIDI au lieu de son set. D'où une politique par app.

Trois politiques :
  hide      — équivalent ⌘H : l'app disparaît de l'écran et continue de tourner.
              C'est le défaut, et c'est plus propre que « réduire » : une app masquée
              ne laisse RIEN (ni fenêtre, ni vignette dans le Dock), et ⌘Tab la
              rappelle telle quelle.
  minimize  — chaque fenêtre part dans le Dock. Utile pour les apps qui ignorent le
              masquage (ou dont on veut garder la vignette sous la main).
  keep      — on n'y touche pas. C'est la politique d'Ableton : le set EST l'écran.

Deux moments, deux mécanismes — le premier suffit la plupart du temps :
  au LANCEMENT  `open -g -j` fait démarrer l'app déjà masquée. Aucune autorisation
                macOS requise, et rien ne clignote jamais à l'écran (cf. launch.py).
  À LA DEMANDE  `readyset tidy` / le bouton du dashboard range ce qui est DÉJÀ ouvert —
                le cas courant, puisque les apps du rig restent lancées des jours.
                Celui-là passe par System Events, donc exige que l'app qui exécute
                `readyset` (Terminal, or the menu-bar app) soit cochée dans Réglages → Confidentialité
                et sécurité → Accessibilité. Sans ça macOS renvoie l'erreur -1743 et
                on le dit explicitement plutôt que d'échouer en silence.

Les apps sont adressées par IDENTIFIANT DE BUNDLE (com.bome.network…), jamais par nom
de process : le nom affiché ne se déduit pas du nom du .app — « Bome Network.app »
tourne sous le process « MT Player », « Ableton Live 12 Suite 3.app » sous « Live ».
Le bundle id, lui, se lit dans l'app et ne bouge pas.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

HIDE = "hide"
MINIMIZE = "minimize"
KEEP = "keep"
POLICIES = (HIDE, MINIMIZE, KEEP)


def rig_apps(cfg: dict) -> list[str]:
    """Toutes les apps du rig : celles du bring-up + Ableton (qui est à part dans [set])."""
    apps = list(cfg["launch"]["apps"])
    ableton = cfg["set"].get("ableton_app", "")
    if ableton and ableton not in apps:
        apps.append(ableton)
    return apps


def policy_for(cfg: dict, app_path: str) -> str:
    """Politique de fenêtre d'une app. Clé de [windows.apps] = sous-chaîne du nom du .app."""
    w = cfg.get("windows", {})
    for pattern, policy in (w.get("apps") or {}).items():
        if pattern.lower() in Path(app_path).stem.lower():
            return policy if policy in POLICIES else HIDE
    default = w.get("default", HIDE)
    return default if default in POLICIES else HIDE


def launch_hidden(cfg: dict, app_path: str) -> bool:
    """Faut-il lancer cette app masquée (`open -g -j`) plutôt qu'au premier plan ?"""
    if not cfg.get("windows", {}).get("launch_hidden", True):
        return False
    return policy_for(cfg, app_path) != KEEP


@lru_cache(maxsize=64)
def bundle_id(app_path: str) -> str | None:
    """Identifiant de bundle lu dans l'Info.plist de l'app.

    plutil plutôt que `mdls` (qui dépend de l'index Spotlight) ou qu'un `osascript
    'id of app …'` (qui met en route les services de lancement) : ici on lit un
    fichier, ça marche même sur une app jamais ouverte ou hors /Applications.
    """
    plist = Path(app_path) / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    r = subprocess.run(
        ["plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-", str(plist)],
        capture_output=True, text=True,
    )
    bid = r.stdout.strip()
    return bid or None


def _osascript(script: str) -> tuple[bool, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        return True, r.stdout.strip()
    err = r.stderr.strip()
    # -1743 = « n'est pas autorisé à envoyer des événements ». C'est TOUJOURS la
    # permission Accessibilité qui manque, jamais un bug du script — on le dit tel quel.
    if "1743" in err or "assistive" in err.lower() or "autoris" in err.lower():
        return False, ("macOS refuse le pilotage des fenêtres — coche l'app qui lance "
                       "readyset (Terminal / the menu-bar app) dans Réglages → Confidentialité et "
                       "sécurité → Accessibilité")
    return False, err or "osascript a échoué"


def _running(bid: str) -> bool:
    ok, out = _osascript(
        f'tell application "System Events" to return (count of (every process '
        f'whose bundle identifier is "{bid}")) > 0')
    return ok and out == "true"


def _hide(bid: str) -> tuple[bool, str]:
    return _osascript(f'''
tell application "System Events"
  set ps to (every process whose bundle identifier is "{bid}")
  if (count of ps) is 0 then return "absent"
  repeat with p in ps
    set visible of p to false
  end repeat
  return "masquée"
end tell''')


def _minimize(bid: str) -> tuple[bool, str]:
    """Réduire dans le Dock — ce qui suppose que l'app soit VISIBLE, et qu'elle ait des
    fenêtres.

    Trois choses mesurées le 2026-08-22, la première fois que ce chemin a tourné en vrai
    (il dormait dans le code depuis le début, cf. TASKS.md) :

    - une app MASQUÉE (⌘H) expose quand même ses fenêtres à System Events, donc on peut
      les COMPTER sans rien déranger — mais poser AXMinimized dessus ne montre rien : le
      Dock ne fait pas de vignette pour l'app masquée. D'où le démasquage préalable, et
      seulement s'il y a une fenêtre à réduire ;
    - Bome Network et Bome MIDI Translator Pro tournent avec ZÉRO fenêtre ouverte. Les
      démasquer pour rien les remettrait dans le ⌘Tab sans rien réduire ;
    - l'ancienne version répondait « réduite (0 fenêtre(s)) » — un succès vide, exactement
      le mode d'échec que ce dépôt combat depuis le 22/08 au matin. Zéro fenêtre est
      désormais dit comme tel, et des fenêtres dont AUCUNE n'accepte AXMinimized est
      une VRAIE erreur, pas un demi-succès.
    """
    ok, out = _osascript(f'''
tell application "System Events"
  set ps to (every process whose bundle identifier is "{bid}")
  if (count of ps) is 0 then return "absent"
  set p to item 1 of ps
  set ws to windows of p
  if (count of ws) is 0 then return "sans-fenetre"
  -- Démasquer AVANT de réduire : une fenêtre minimisée depuis une app masquée ne
  -- laisse aucune vignette dans le Dock, donc rien de ce qu'on vient chercher.
  set visible of p to true
  set n to 0
  repeat with w in ws
    try
      set value of attribute "AXMinimized" of w to true
      -- ON RELIT. Écrire l'attribut peut « réussir » sans que la fenêtre bouge :
      -- Bome Network l'accepte et reste ouverte (mesuré le 2026-08-22). Compter
      -- l'écriture, c'est compter la tentative — le défaut que ce dépôt corrige
      -- partout depuis ce matin. Seul l'état relu fait foi.
      if (value of attribute "AXMinimized" of w) is true then set n to n + 1
    end try
  end repeat
  return "n=" & n & "/" & (count of ws)
end tell''')
    if not ok:
        return False, out
    if out == "absent":
        return True, "absent"
    if out == "sans-fenetre":
        return True, "aucune fenêtre ouverte"
    n, total = (int(x) for x in out.removeprefix("n=").split("/"))
    if n == 0:
        # Repli sur le masquage plutôt qu'un rouge : le but est qu'aucune fenêtre ne
        # traîne à l'écran, et ⌘H l'atteint. La vignette du Dock est perdue — on le DIT,
        # pour ne pas laisser croire qu'elle est là. Bome Network est le cas connu : sa
        # fenêtre accepte AXMinimized et l'ignore.
        hid, hmsg = _hide(bid)
        if hid:
            return True, f"refuse de se réduire ({total} fenêtre(s)) → masquée"
        return False, f"{total} fenêtre(s) ouverte(s), ni réductible ni masquable : {hmsg}"
    return True, f"réduite ({n} fenêtre(s))" + (f", {total - n} refusée(s)" if n < total else "")


def apply_one(cfg: dict, app_path: str, dry_run: bool = False,
              force: bool = False) -> tuple[bool, str]:
    """Applique la politique d'UNE app. force=True traite aussi les `keep` (« Ableton compris »)."""
    name = Path(app_path).stem
    policy = policy_for(cfg, app_path)
    if policy == KEEP and not force:
        return True, f"{name} — laissée visible (keep)"
    action = HIDE if policy == KEEP else policy   # forcer un `keep` = le masquer
    bid = bundle_id(app_path)
    if not bid:
        return False, f"{name} — identifiant de bundle introuvable ({app_path})"
    if dry_run:
        verb = "masquerait" if action == HIDE else "réduirait"
        return True, f"[dry-run] {verb} {name}"
    ok, msg = _hide(bid) if action == HIDE else _minimize(bid)
    if ok and msg == "absent":
        # Pas une erreur : une app pas lancée n'a pas de fenêtre à ranger. C'est
        # `check_apps` qui signale une app manquante, pas le rangement des fenêtres.
        return True, f"{name} — pas lancée"
    return ok, f"{name} — {msg}"


def tidy(cfg: dict, log=print, dry_run: bool = False, force: bool = False) -> tuple[bool, str]:
    """Range les fenêtres de toutes les apps du rig. Retourne (tout_ok, résumé)."""
    lines: list[str] = []
    all_ok = True
    for app in rig_apps(cfg):
        ok, msg = apply_one(cfg, app, dry_run=dry_run, force=force)
        all_ok = all_ok and ok
        lines.append(("  ✔ " if ok else "  ✖ ") + msg)
        log(lines[-1])
        if not ok and "Accessibilité" in msg:
            break     # même cause pour toutes les suivantes : inutile de répéter 5 fois
    return all_ok, "\n".join(lines)


def snapshot(cfg: dict) -> list[dict]:
    """État courant, pour un affichage : [{name, policy, running, visible}]."""
    out = []
    for app in rig_apps(cfg):
        bid = bundle_id(app)
        entry = {"name": Path(app).stem, "policy": policy_for(cfg, app),
                 "running": False, "visible": None}
        if bid:
            ok, res = _osascript(
                f'tell application "System Events"\n'
                f'  set ps to (every process whose bundle identifier is "{bid}")\n'
                f'  if (count of ps) is 0 then return "absent"\n'
                f'  return (visible of item 1 of ps) as text\n'
                f'end tell')
            if ok and res != "absent":
                entry["running"] = True
                entry["visible"] = (res == "true")
        out.append(entry)
    return out
