"""Rig app windows — everything must RUN, nothing must SHOW.

On stage the screen shows nothing but the Ableton set. Bome MIDI Translator, Bome Network,
Stream Deck and Stage Traxx have to run (they are what carries the MIDI routing, the
buttons and the backing tracks), but their windows are noise: one stray click and you are
looking at a MIDI translator log instead of your set. Hence a per-app policy.

Three policies:
  hide      — the ⌘H equivalent: the app disappears from the screen and keeps running.
              That is the default, and it is cleaner than "minimize": a hidden app
              leaves NOTHING behind (no window, no Dock thumbnail), and ⌘Tab brings
              it back exactly as it was.
  minimize  — each window goes down into the Dock. Useful for apps that ignore
              hiding (or whose Dock thumbnail we want to keep at hand).
  keep      — we do not touch it. That is Ableton's policy: the set IS the screen.

Two moments, two mechanisms — the first one is enough most of the time:
  at LAUNCH     `open -g -j` starts the app already hidden. No macOS permission
                required, and nothing ever flashes on screen (see launch.py).
  ON DEMAND     `readyset tidy` / the dashboard button tidies what is ALREADY open —
                the common case, since the rig apps stay launched for days.
                That one goes through System Events, so it requires the app running
                `readyset` (Terminal, or the menu-bar app) to be ticked in Settings →
                Privacy & Security → Accessibility. Without it macOS returns error
                -1743, and we say so explicitly rather than failing silently.

Apps are addressed by BUNDLE IDENTIFIER (com.bome.network…), never by process name: the
displayed name cannot be derived from the .app name — "Bome Network.app" runs under the
process "MT Player", "Ableton Live 12 Suite 3.app" under "Live". The bundle id, on the
other hand, is read inside the app and never moves.
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
    """All the rig apps: the bring-up ones + Ableton (which lives apart, in [set])."""
    apps = list(cfg["launch"]["apps"])
    ableton = cfg["set"].get("ableton_app", "")
    if ableton and ableton not in apps:
        apps.append(ableton)
    return apps


def policy_for(cfg: dict, app_path: str) -> str:
    """Window policy of an app. A [windows.apps] key = a substring of the .app name."""
    w = cfg.get("windows", {})
    for pattern, policy in (w.get("apps") or {}).items():
        if pattern.lower() in Path(app_path).stem.lower():
            return policy if policy in POLICIES else HIDE
    default = w.get("default", HIDE)
    return default if default in POLICIES else HIDE


def launch_hidden(cfg: dict, app_path: str) -> bool:
    """Should this app be launched hidden (`open -g -j`) rather than in the foreground?"""
    if not cfg.get("windows", {}).get("launch_hidden", True):
        return False
    return policy_for(cfg, app_path) != KEEP


@lru_cache(maxsize=64)
def bundle_id(app_path: str) -> str | None:
    """Bundle identifier, read from the app's Info.plist.

    plutil rather than `mdls` (which depends on the Spotlight index) or an `osascript
    'id of app …'` (which spins up the launch services): here we read a file, and that
    works even on an app never opened before or living outside /Applications.
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
    # -1743 = "is not allowed to send events". It is ALWAYS the missing Accessibility
    # permission, never a bug in the script — so we say it as such.
    if "1743" in err or "assistive" in err.lower() or "autoris" in err.lower():
        return False, ("macOS refuse le pilotage des fenêtres — coche l'app qui lance "
                       "readyset (Terminal / l'app de barre de menus) dans Réglages → Confidentialité et "
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
    """Minimize into the Dock — which assumes the app is VISIBLE, and that it has
    windows.

    Three things measured on 2026-08-22, the first time this path actually ran for real
    (it had been sleeping in the code since the beginning, see TASKS.md):

    - a HIDDEN app (⌘H) still exposes its windows to System Events, so we can COUNT
      them without disturbing anything — but setting AXMinimized on them shows nothing:
      the Dock makes no thumbnail for a hidden app. Hence the un-hiding beforehand, and
      only if there is a window to minimize;
    - Bome Network and Bome MIDI Translator Pro run with ZERO window open. Un-hiding
      them for nothing would put them back into ⌘Tab without minimizing anything;
    - the old version answered "réduite (0 fenêtre(s))" — an empty success, exactly the
      failure mode this repo has been fighting since the morning of 08-22. Zero windows
      is now stated as such, and windows of which NONE accepts AXMinimized is a REAL
      error, not a half-success.
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
        # Fall back on hiding rather than a red: the goal is that no window lingers on
        # screen, and ⌘H achieves it. The Dock thumbnail is lost — we SAY so, so as not
        # to let anyone believe it is there. Bome Network is the known case: its window
        # accepts AXMinimized and ignores it.
        hid, hmsg = _hide(bid)
        if hid:
            return True, f"refuse de se réduire ({total} fenêtre(s)) → masquée"
        return False, f"{total} fenêtre(s) ouverte(s), ni réductible ni masquable : {hmsg}"
    return True, f"réduite ({n} fenêtre(s))" + (f", {total - n} refusée(s)" if n < total else "")


def apply_one(cfg: dict, app_path: str, dry_run: bool = False,
              force: bool = False) -> tuple[bool, str]:
    """Apply ONE app's policy. force=True also handles the `keep` ones ("Ableton included")."""
    name = Path(app_path).stem
    policy = policy_for(cfg, app_path)
    if policy == KEEP and not force:
        return True, f"{name} — laissée visible (keep)"
    action = HIDE if policy == KEEP else policy   # forcing a `keep` = hiding it
    bid = bundle_id(app_path)
    if not bid:
        return False, f"{name} — identifiant de bundle introuvable ({app_path})"
    if dry_run:
        verb = "masquerait" if action == HIDE else "réduirait"
        return True, f"[dry-run] {verb} {name}"
    ok, msg = _hide(bid) if action == HIDE else _minimize(bid)
    if ok and msg == "absent":
        # Not an error: an app that is not launched has no window to tidy. Reporting a
        # missing app is `check_apps`'s job, not the window tidying's.
        return True, f"{name} — pas lancée"
    return ok, f"{name} — {msg}"


def tidy(cfg: dict, log=print, dry_run: bool = False, force: bool = False) -> tuple[bool, str]:
    """Tidy the windows of every rig app. Returns (all_ok, summary)."""
    lines: list[str] = []
    all_ok = True
    for app in rig_apps(cfg):
        ok, msg = apply_one(cfg, app, dry_run=dry_run, force=force)
        all_ok = all_ok and ok
        lines.append(("  ✔ " if ok else "  ✖ ") + msg)
        log(lines[-1])
        if not ok and "Accessibilité" in msg:
            break     # same cause for all the next ones: no point repeating it 5 times
    return all_ok, "\n".join(lines)


def snapshot(cfg: dict) -> list[dict]:
    """Current state, for display purposes: [{name, policy, running, visible}]."""
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
