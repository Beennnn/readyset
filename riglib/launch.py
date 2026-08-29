"""Bring-up sequence — launch the rig apps in order, then open the gig set.

Poll-for-readiness rather than fixed sleeps: after launching Bome we wait until
its virtual MIDI ports actually appear before opening the Ableton set, so the set
binds to live ports instead of racing an app that is still booting.
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
import time
from pathlib import Path

import mido

from . import windows


# Message-sentinelle : dire « déjà lancée » n'est pas dire « lancée », et l'appelant a
# besoin de la nuance — pour le glyphe qu'il affiche comme pour le délai qu'il s'épargne.
ALREADY = "déjà lancée"


def running_from(app_path: str) -> list[str]:
    """Les processus qui tournent DEPUIS ce bundle précis.

    On compare des CHEMINS, pas des noms : deux installations de la même application
    portent le même nom ET le même identifiant de bundle — Ableton en est la preuve, avec
    « Suite » et « Suite 3 » tous deux en com.ableton.live. Seul le chemin les sépare.

    Sans ce garde-fou, `open -a` reste inoffensif sur une app déjà lancée (il ne fait que
    la mettre au premier plan) SAUF si une autre copie tourne : là il en démarre une
    seconde, et deux instances se disputent les mêmes interfaces audio et MIDI.
    """
    prefix = str(Path(app_path)).rstrip("/") + "/Contents/MacOS/"
    r = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True)
    return [c for c in r.stdout.splitlines() if c.startswith(prefix)]


def _open_app(app_path: str, hidden: bool = False) -> tuple[bool, str]:
    if not Path(app_path).exists():
        return False, f"introuvable : {app_path}"
    if running_from(app_path):
        return True, ALREADY
    # -g : ne pas passer au premier plan. -j : démarrer masquée. Les deux se règlent au
    # LANCEMENT, donc sans autorisation Accessibilité — c'est le moyen le plus propre de
    # ne jamais voir clignoter la fenêtre d'une app qui n'a rien à faire à l'écran.
    cmd = ["open", "-a", app_path] + (["-g", "-j"] if hidden else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or "open a échoué"
    return True, "lancé (masqué)" if hidden else "lancé"


def _wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _midi_port_present(substr: str) -> bool:
    try:
        return any(substr.lower() in p.lower() for p in mido.get_input_names())
    except Exception:
        return False


def launch_apps(cfg: dict, log=print, dry_run: bool = False) -> None:
    settle = cfg["launch"].get("settle_seconds", 2)
    for app in cfg["launch"]["apps"]:
        name = Path(app).stem
        hidden = windows.launch_hidden(cfg, app)
        if dry_run:
            exists = "" if Path(app).exists() else "  (introuvable !)"
            log(f"  [dry-run] lancerait {name}{' masquée' if hidden else ''}{exists}")
            continue
        ok, msg = _open_app(app, hidden=hidden)
        glyphe = "↷" if msg == ALREADY else ("▶" if ok else "✖")
        log(f"  {glyphe} {name} — {msg}")
        # Le délai de stabilisation attend qu'une app FRAÎCHEMENT lancée soit prête. Une
        # app déjà là l'est depuis longtemps : attendre deux secondes de plus par app
        # allongeait la mise en place pour rien, précisément dans le cas le plus courant.
        if ok and msg != ALREADY:
            time.sleep(settle)

    if dry_run:
        return

    # The virtual ports come from the macOS IAC Driver (already online), so they
    # normally exist immediately; this wait just guards the rare cold-boot race.
    required = cfg["checks"]["midi_required"]
    if required:
        anchor = required[0]
        log(f"  … attente du port MIDI « {anchor} »")
        if _wait_for(lambda: _midi_port_present(anchor), timeout=15):
            log(f"  ✔ ports MIDI virtuels présents")
        else:
            log(f"  ⚠️  « {anchor} » toujours absent après 15s (Bome pas prêt ?)")


def _live_processes() -> list[str]:
    """Command line of every running Ableton Live, whatever the install."""
    r = subprocess.run(["pgrep", "-fl", "/MacOS/Live"], capture_output=True, text=True)
    return [l.split(" ", 1)[1] for l in r.stdout.splitlines() if " " in l]


def open_set(cfg: dict, log=print, dry_run: bool = False) -> None:
    if not cfg["set"].get("open_after_launch", True):
        log("  (ouverture du set désactivée : [set].open_after_launch = false)")
        return
    project = cfg["set"]["project"]
    app = cfg["set"]["ableton_app"]
    if dry_run:
        pe = "" if Path(project).exists() else "  (set introuvable !)"
        ae = "" if Path(app).exists() else "  (Ableton introuvable !)"
        log(f"  [dry-run] ouvrirait « {Path(project).name} »{pe}")
        log(f"  [dry-run]   dans {Path(app).stem}{ae}")
        return
    if not Path(project).exists():
        log(f"  ✖ set introuvable : {project}")
        log(f"    → corrige [set].project dans rig.toml")
        return
    if not Path(app).exists():
        log(f"  ✖ Ableton introuvable : {app}")
        return
    # Ne JAMAIS ajouter un second Live. Le 2026-08-29, une clé de config mal nommée a
    # fait retomber le moteur sur une autre installation d'Ableton, et ce « open » a
    # lancé Suite à côté de Suite 3 qui tournait : deux Live se disputant les mêmes
    # interfaces audio et MIDI. Ouvrir le projet dans l'instance ATTENDUE reste bon —
    # macOS le charge dans le Live déjà là. C'est la mauvaise install qu'on refuse.
    autres = [c for c in _live_processes() if not c.startswith(str(app))]
    if autres:
        log(f"  ✖ un autre Ableton tourne déjà — {Path(app).stem} ne sera pas lancé :")
        for c in autres:
            log(f"      {c}")
        log("    → quitte-le d'abord : deux Live ouverts se disputent audio et MIDI")
        return
    # Armé AVANT d'ouvrir : le dialogue de récupération apparaît dans les premières
    # secondes, et il bloque tout ce qui suit — y compris l'attente du port MIDI.
    surveiller_dialogue_recuperation(cfg, log=log)
    r = subprocess.run(["open", "-a", app, project], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  ✖ ouverture du set : {r.stderr.strip()}")
        return
    log(f"  ▶ ouverture de « {Path(project).name} » dans {Path(app).stem}")
    log("  … attente du port « Ableton Loopback »")
    if _wait_for(lambda: _midi_port_present("Ableton Loopback"), timeout=45, interval=1):
        log("  ✔ Ableton en ligne")
    else:
        log("  ⚠️  Ableton pas encore prêt après 45s (gros set / plugins qui chargent)")


def ensure_amphetamine_session(cfg: dict, log=print, dry_run: bool = False) -> None:
    if not cfg["launch"].get("amphetamine_session", True):
        return
    if dry_run:
        log("  [dry-run] démarrerait une session Amphetamine (anti-veille)")
        return
    # Amphetamine exposes an AppleScript command; a bare session runs indefinitely.
    r = subprocess.run(
        ["osascript", "-e", 'tell application "Amphetamine" to start new session'],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        log("  ☕ session Amphetamine démarrée")
    else:
        log(f"  ⚠️  Amphetamine : {r.stderr.strip() or 'session non démarrée (autorisation ?)'}")


def tidy_windows(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Range les fenêtres en fin de bring-up (voir riglib/windows.py).

    Après le lancement, même masquées au démarrage, des apps déjà ouvertes avant le
    préflight peuvent traîner à l'écran — et Ableton, lui, vient de passer devant en
    ouvrant le set. Ce passage final laisse donc l'écran dans l'état de scène : le set
    devant, le reste rangé.
    """
    if not cfg.get("windows", {}).get("after_preflight", True):
        return
    log("  🪟 rangement des fenêtres…")
    windows.tidy(cfg, log=log, dry_run=dry_run)


def bring_up(cfg: dict, log=print, dry_run: bool = False) -> None:
    log("Lancement des apps du rig…" if not dry_run else "Séquence de mise en place (dry-run) :")
    launch_apps(cfg, log=log, dry_run=dry_run)
    ensure_amphetamine_session(cfg, log=log, dry_run=dry_run)
    open_set(cfg, log=log, dry_run=dry_run)
    tidy_windows(cfg, log=log, dry_run=dry_run)


def _script_dialogue(mots: list[str], boutons: list[str]) -> str:
    """AppleScript qui cherche le dialogue de récupération et clique « Non »."""
    cond = " or ".join(f'txt contains "{m}"' for m in mots) or "false"
    test = " or ".join(f'name of b is "{b}"' for b in boutons) or "false"
    return f'''
tell application "System Events"
  if not (exists process "Live") then return "absent"
  tell process "Live"
    set vu to false
    repeat with w in windows
      set txt to ""
      try
        set txt to (value of every static text of w) as text
      end try
      if txt is not "" then set vu to true
      if {cond} then
        repeat with b in buttons of w
          if {test} then
            click b
            return "clique:" & (name of b)
          end if
        end repeat
        return "vu-sans-bouton"
      end if
    end repeat
    if vu then return "rien"
  end tell
end tell
return "aveugle"'''


def surveiller_dialogue_recuperation(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Répond « Non » au dialogue de récupération, en tâche de fond.

    En tâche de fond parce que le dialogue BLOQUE le chargement : le port MIDI n'apparaît
    pas tant qu'il est là, donc l'attente qui le guette ne peut pas être celle qui le
    ferme. La mise en place s'arrêtait sinon sur une fenêtre que personne ne regardait.

    Ne clique JAMAIS sans avoir reconnu le dialogue à son texte. Cliquer au jugé dans une
    fenêtre du DAW serait la seule façon de faire pire que le dialogue lui-même — et les
    mots comme les boutons sont dans la config, parce qu'ils changent avec la langue et
    la version, là où le code ne devrait pas.

    Demande l'autorisation d'Accessibilité, celle que le check « sys:accessibility »
    surveille déjà. Sans elle, System Events rend des fenêtres vides et on ne voit rien —
    d'où le message explicite plutôt qu'un silence.
    """
    rd = cfg["set"].get("recovery_dialog") or {}
    if not rd.get("enabled", True) or dry_run:
        return
    mots = [m for m in rd.get("keywords", []) if m]
    boutons = [b for b in rd.get("refuse_buttons", []) if b]
    if not mots or not boutons:
        return
    script = _script_dialogue(mots, boutons)
    plafond = float(rd.get("timeout_seconds", 45))

    def boucle() -> None:
        debut, vide = time.monotonic(), 0
        while time.monotonic() - debut < plafond:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True)
            sortie = r.stdout.strip()
            if sortie.startswith("clique:"):
                log(f"  ↩ dialogue de récupération refusé (bouton « {sortie[7:]} »)")
                return
            if sortie == "vu-sans-bouton":
                log("  ⚠️  dialogue de récupération vu, mais aucun bouton connu — "
                    "ajoute son libellé dans [set.recovery_dialog].refuse_buttons")
                return
            # « aveugle » = le DAW a des fenêtres mais aucune ne rend son texte, ce qui
            # est la signature de l'autorisation d'Accessibilité manquante. Sans cette
            # distinction, « je ne vois pas de dialogue » et « je ne vois rien du tout »
            # se ressemblent, et on cherche le problème du mauvais côté.
            if sortie == "aveugle" and vide == 0:
                vide = 1
                log("  ⚠️  dialogue : fenêtres illisibles — autorisation d'Accessibilité "
                    "manquante pour ce service (le check « sys:accessibility » la surveille)")
            if r.returncode != 0 and vide == 0:
                vide = 1
                log(f"  (dialogue : System Events injoignable — {r.stderr.strip()[:90]})")
            time.sleep(1.0)

    threading.Thread(target=boucle, daemon=True).start()


def _attendre_chargement(motif: str, calme: float, plafond: float, log) -> bool:
    """Attend que le DAW se taise. Rend faux s'il n'y a rien à observer.

    Une durée fixe est un pari sur la taille du set, et elle se trompe des deux côtés :
    trop courte, la scène part dans un set à moitié chargé ; trop longue, on regarde
    l'écran sans rien faire pendant la mise en place. Le journal, lui, dit la vérité —
    le DAW y écrit sans arrêt pendant qu'il charge, et cesse quand il a fini (mesuré :
    silence quatre secondes après la dernière action).
    """
    fichiers = [f for f in glob.glob(os.path.expanduser(motif)) if os.path.exists(f)]
    if not fichiers:
        return False
    f = max(fichiers, key=os.path.getmtime)
    debut = time.monotonic()
    mtime, dernier_ecrit = os.path.getmtime(f), time.monotonic()
    while time.monotonic() - debut < plafond:
        time.sleep(0.4)
        m = os.path.getmtime(f)
        if m != mtime:
            mtime, dernier_ecrit = m, time.monotonic()
        elif time.monotonic() - dernier_ecrit >= calme:
            log(f"  ✔ set chargé — journal silencieux depuis {calme:.0f}s "
                f"({time.monotonic() - debut:.0f}s d'attente)")
            return True
    log(f"  ⚠️  journal toujours actif après {plafond:.0f}s — on lance quand même")
    return True


def start_scene(cfg: dict, log=print, dry_run: bool = False) -> None:
    """Lance une scène du set, une fois celui-ci chargé.

    Deux messages, dans cet ordre : un CC dont la VALEUR est le numéro de scène, puis
    une note qui déclenche la scène sélectionnée. Ce couple n'est pas inventé ici — c'est
    le protocole que la surface de contrôle du rig parle déjà (scène 0 = remise à zéro,
    1 = arrêt, 3 et au-delà = les morceaux). Le réutiliser évite un second mapping dans
    le DAW, et surtout évite deux vérités sur la même chose.

    Séparé de open_set volontairement : ouvrir un set et le faire JOUER sont deux
    décisions distinctes, et la seconde ne doit pas partir quand on rouvre le set en
    cours de soirée pour vérifier un réglage. C'est la mise en place qui l'appelle, après
    avoir posé la sortie audio — dans cet ordre, sinon les premières mesures sortiraient
    sur l'interface qu'on vient de corriger.
    """
    sc = cfg["set"].get("start_scene") or {}
    port = sc.get("port", "")
    if not port:
        return                       # non configuré : rien à faire, et rien à dire
    canal = int(sc.get("channel", 1)) - 1
    num_cc, scene = int(sc.get("select_cc", 2)), int(sc.get("scene", 0))
    note = int(sc.get("trigger_note", 38))
    motif = sc.get("log_glob", "")
    if dry_run:
        log(f"  [dry-run] attendrait la fin du chargement puis lancerait la scène {scene} "
            f"(CC {num_cc} puis note {note}, canal {canal + 1}) sur « {port} »")
        return
    log("  … attente de la fin du chargement du set")
    if not (motif and _attendre_chargement(motif, float(sc.get("quiet_seconds", 2)),
                                           float(sc.get("max_seconds", 25)), log)):
        attente = float(sc.get("delay_seconds", 12))
        log(f"  (pas de journal à observer — attente fixe de {attente:.0f}s)")
        time.sleep(attente)
    cible = next((p for p in mido.get_output_names() if port.lower() in p.lower()), None)
    if cible is None:
        log(f"  ✖ départ du set : port « {port} » absent")
        return
    try:
        with mido.open_output(cible) as out:
            out.send(mido.Message("control_change", channel=canal,
                                  control=num_cc, value=scene))
            # Note-on SEUL, comme la surface : {cc:1,2,0} puis {noteon:1,38,127}. J'y
            # avais ajouté un note-off par hygiène ; le rig tourne ainsi depuis des mois
            # sans note suspendue, donc l'inquiétude était théorique et l'ajout une
            # divergence gratuite avec le protocole de référence.
            out.send(mido.Message("note_on", channel=canal, note=note, velocity=127))
        log(f"  ▶ scène {scene} lancée — CC {num_cc} puis note {note}, canal {canal + 1}")
    except Exception as exc:         # un démarrage raté ne doit pas couler la mise en place
        log(f"  ✖ départ du set : {exc}")
