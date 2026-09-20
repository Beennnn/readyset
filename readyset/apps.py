"""Applis en trop — celles qui tournent alors que le rig n'en a pas besoin.

Sur scène, chaque app ouverte en plus est un risque : une notification WhatsApp qui
passe devant le set, un navigateur qui réveille le WiFi, un updater qui décide de se
lancer pendant le deuxième morceau, de la RAM et du CPU pris à Ableton. Au bureau c'est
sans conséquence — d'où une sévérité par mode : **avertissement en live, simple info en
studio** (réglable par `unexpected_apps_severity`, `"off"` pour ne plus rien afficher).

Ce que « appli » veut dire ici : macOS distingue les apps ayant une vraie présence à
l'écran (icône dans le Dock, fenêtres, menus — `type="Foreground"`) des agents de barre de
menus (`type="UIElement"`) et des démons. Seules les premières comptent. Sans ce filtre la
liste remonterait Bartender, Stats, LuLu, MidiTray… — une vingtaine d'utilitaires qu'on ne
veut évidemment pas proposer de fermer, et qui noieraient les trois qui comptent. Bome
(Translator et Network) n'y figure pas non plus : ce sont des apps d'arrière-plan, elles
ne peuvent pas polluer l'écran.

L'inventaire vient de `lsappinfo list`, PAS de System Events. La version AppleScript
(`every application process whose background only is false`) marchait en terminal et
**expirait au bout de 20 s** une fois lancée par launchd : chaque `name of p` / `file of p`
réévalue le filtre `whose`, et hors session interactive c'est pathologiquement lent — le
dashboard ne voyait donc jamais aucune app en trop (constaté le 2026-08-18). `lsappinfo`
répond en ~10 ms, en un seul appel, sans autorisation d'accessibilité, et signale en prime
les apps masquées (⌘H) — qu'AppleScript aurait laissées passer alors qu'elles consomment
toujours CPU et notifications.

Sont retirées de la liste : les apps du rig lui-même (elles DOIVENT tourner) et tout ce
qui est dans `[checks.unexpected_apps].allow`. Le Finder y est par défaut : on ne « quitte »
pas le Finder, macOS le relance.

La fermeture est TOUJOURS un `quit` propre, jamais un `kill` : une app avec un document
non enregistré doit pouvoir poser sa question. Si elle ne part pas dans les 6 s, on le dit
au lieu d'insister — c'est presque toujours une boîte de dialogue qui attend un clic.
"""

from __future__ import annotations

import hashlib
import plistlib
import re
import subprocess
import time
import unicodedata
from pathlib import Path

from . import windows

_ENTRY = re.compile(r'^\s*\d+\)\s*"([^"]*)"\s*ASN:', re.M)
_BUNDLE_PATH = re.compile(r'^\s*bundle path="([^"]+)"', re.M)
_TYPE = re.compile(r'\btype="([^"]+)"')

_ICON_CACHE = Path.home() / ".cache" / "readyset" / "appicons"

# Mémo très court : un seul rendu du dashboard appelle cette liste une fois pour le check,
# une fois pour le panneau, puis une fois par bouton « Quitter » à résoudre — soit une
# dizaine d'appels pour une réponse identique. 2 s, c'est assez pour couvrir un cycle de
# rendu et assez peu pour qu'une app fermée disparaisse au rafraîchissement suivant.
_CACHE_TTL = 2.0
_cache: dict = {"at": 0.0, "apps": []}


def running_gui_apps(max_age: float = _CACHE_TTL) -> list[dict]:
    """Apps ayant une présence à l'écran → [{"name", "path"}], triées par nom."""
    now = time.monotonic()
    if max_age and _cache["apps"] and (now - _cache["at"]) < max_age:
        return _cache["apps"]
    try:
        r = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    # `lsappinfo list` sort un bloc par app, ouvert par une ligne « 48) "Signal" ASN:… ».
    # On repère ces en-têtes, puis on lit entre deux en-têtes le chemin du bundle et le type.
    heads = list(_ENTRY.finditer(r.stdout))
    for i, h in enumerate(heads):
        body = r.stdout[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(r.stdout)]
        # Certaines apps préfixent leur nom d'une marque de sens de lecture invisible
        # (WhatsApp expose « ‎WhatsApp ») : elle casse l'alignement en terminal et le
        # tri, pour un caractère que personne ne voit.
        name = "".join(c for c in h.group(1) if unicodedata.category(c) != "Cf").strip()
        path = _BUNDLE_PATH.search(body)
        kind = _TYPE.search(body)
        if not name or not path or not kind or kind.group(1) != "Foreground":
            continue
        out.append({"name": name, "path": path.group(1).rstrip("/")})
    out = sorted(out, key=lambda a: a["name"].lower())
    _cache.update(at=now, apps=out)
    return out


def unexpected(cfg: dict) -> list[dict]:
    """Apps ouvertes dont le rig n'a pas besoin → [{"name", "path"}]."""
    rig_paths = {p.rstrip("/") for p in windows.rig_apps(cfg)}
    rig_stems = {Path(p).stem.lower() for p in rig_paths}
    allow = [a.lower() for a in
             (cfg["checks"].get("unexpected_apps", {}) or {}).get("allow", [])]
    out = []
    for app in running_gui_apps():
        if app["path"] in rig_paths or Path(app["path"]).stem.lower() in rig_stems:
            continue
        hay = f'{app["name"]} {app["path"]}'.lower()
        if any(pat in hay for pat in allow):
            continue
        out.append(app)
    return out


def icon_png(app_path: str, size: int = 64) -> bytes | None:
    """Vignette PNG de l'icône de l'app, mise en cache sur disque.

    La conversion `sips` d'un .icns coûte ~100 ms ; le dashboard rafraîchit toutes les
    4 s, donc sans cache on relancerait sips en boucle pour des images identiques.
    """
    app = Path(app_path)
    plist = app / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    key = hashlib.sha1(f"{app_path}:{size}".encode()).hexdigest()[:16]
    cached = _ICON_CACHE / f"{key}.png"
    if cached.exists():
        return cached.read_bytes()
    try:
        info = plistlib.loads(plist.read_bytes())
    except Exception:
        return None
    name = info.get("CFBundleIconFile") or ""
    if not name:
        return None
    if not name.lower().endswith(".icns"):
        name += ".icns"
    src = app / "Contents" / "Resources" / name
    if not src.exists():
        return None
    _ICON_CACHE.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["sips", "-s", "format", "png", str(src),
         "--resampleHeightWidthMax", str(size), "--out", str(cached)],
        capture_output=True, text=True)
    if r.returncode != 0 or not cached.exists():
        return None
    return cached.read_bytes()


def quit_app(app_path: str, dry_run: bool = False) -> tuple[bool, str]:
    """Demande à une app de quitter proprement, et vérifie qu'elle est bien partie."""
    name = Path(app_path).stem
    if dry_run:
        return True, f"[dry-run] demanderait à {name} de quitter"
    bid = windows.bundle_id(app_path)
    if not bid:
        return False, f"{name} — identifiant de bundle introuvable"
    r = subprocess.run(["osascript", "-e", f'tell application id "{bid}" to quit'],
                       capture_output=True, text=True, timeout=20)
    # Une app peut refuser de rendre la main tout de suite (dialogue d'enregistrement) :
    # on lui laisse le temps de disparaître plutôt que de conclure sur le code de retour.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        # max_age=0 : ici on veut l'état RÉEL, pas le mémo — c'est précisément le moment
        # où la liste change, et un cache de 2 s ferait conclure « toujours là » à tort.
        if not any(a["path"] == app_path.rstrip("/") for a in running_gui_apps(max_age=0)):
            return True, f"{name} — fermée"
        time.sleep(0.7)
    err = r.stderr.strip()
    return False, (f"{name} — toujours là (document non enregistré ? une fenêtre attend "
                   f"peut-être un clic){f' — {err}' if err else ''}")


def quit_many(cfg: dict, paths: list[str], dry_run: bool = False) -> tuple[bool, str]:
    """Ferme une sélection d'apps. N'accepte QUE des apps effectivement « en trop ».

    Le dashboard n'écoute que sur 127.0.0.1, mais rien n'oblige à faire confiance au corps
    de la requête : on revalide la sélection contre la liste calculée côté serveur, pour
    qu'un chemin arbitraire ne puisse pas être passé à `quit`.
    """
    allowed = {a["path"]: a for a in unexpected(cfg)}
    lines, ok_all = [], True
    for p in paths:
        p = p.rstrip("/")
        if p not in allowed:
            lines.append(f"  ✖ {Path(p).stem} — pas dans la liste des apps en trop, ignorée")
            ok_all = False
            continue
        ok, msg = quit_app(p, dry_run=dry_run)
        ok_all = ok_all and ok
        lines.append(("  ✔ " if ok else "  ✖ ") + msg)
    if not lines:
        return True, "aucune app sélectionnée"
    return ok_all, "\n".join(lines)
