"""Set Ableton's audio OUTPUT inside the already-open application.

The real work is done by `live-output`, which lives in its OWN repository
(github.com/Beennnn/ableton-audio-output) and installs itself as a symlink into
`~/.local/bin`. It has nothing to do with a rig: it sets Live's audio output from the
command line, full stop — so it is tested, published and fixed on its own, and the two
twins CONSUME it instead of each carrying a copy that would drift.

Why this detour through the user interface rather than a clean setting: Live has no
AppleScript dictionary, its preferences file is binary and rewritten on quit, and its
Python API does not touch the audio hardware. The repository's README spells it out in
detail.

This module only does the CHOICE and the CALL: which output do we want in this mode, and
what did the script make of it. It is used at the two places that need it — the bring-up
(`readyset preflight`) and the dashboard's fix button — so that the two do exactly the
same thing.

⚠️ Nothing here READS the current output. It would be tempting, and it would be a trap:
reading through accessibility forces Live to the foreground and opens its settings
window. Every 4 seconds in the state loop, that would be unmanageable — and on stage,
catastrophic. Passive reading stays the Log.txt (see checks.check_live_output).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# macOS refuses to drive a user interface in three different ways, and all three come out
# here as a raw AppleScript message that nobody can interpret five minutes before the
# gig. They all say the same thing, though: "this particular process is not allowed".
# On 2026-08-22, the bring-up returned "3960:4307: execution error: Erreur dans System
# Events : osascript n'est pas autorisé à un accès d'aide. (-25211)" — true, unreadable,
# and above all displayed next to a "✔" since the preflight counted failures as applied
# fixes back then.
#
# ⚠️ The permission is granted PER CALLING PROCESS: an authorised terminal grants nothing
# to the service launched by launchd, which is another responsible process. That is
# exactly the trap fallen into on 2026-08-22 — the output was being set by hand from a
# terminal, and the very same script failed from the dashboard.
_DENIED_MARKERS = (
    "-25211", "accès d’aide", "accès d'aide", "assistive access",   # reading the interface
    "(1002)", "envoyer de saisies", "envoyer des saisies", "send keystrokes",  # sending keystrokes
    "-1743", "not allowed to send apple events", "envoyer des apple", "envoyer des Apple",  # automation
)

DENIED_HINT = ("le service n'a pas le droit de piloter Ableton — cocher le processus qui "
               "lance le dashboard dans Réglages Système › Confidentialité et sécurité › "
               "Accessibilité, puis relancer l'agent "
               "(launchctl kickstart -k gui/$UID/com.readyset.dashboard)")


def denied(text: str) -> bool:
    """Is this message a macOS permission denial (and not a real failure of the setting)?"""
    low = (text or "").lower()
    return any(m.lower() in low for m in _DENIED_MARKERS)

# Looked up by path, like sd-power: the script is installed, not bundled. If it is
# missing, everything here returns a clear message — the rig does not depend on it to
# start, it only loses the ability to fix the output on its own.
_CANDIDATES = (Path.home() / ".local/bin/live-output",
               Path("/opt/homebrew/bin/live-output"),
               Path("/usr/local/bin/live-output"))


def script() -> Path | None:
    return next((p for p in _CANDIDATES if p.exists()), None)


def wanted(cfg: dict, mode: str) -> str | None:
    """The output aimed for in this mode: the FIRST one in `live_output`.

    The list is a set of acceptable values (the check validates any of them); to act we
    have to designate one, and the order of the list already carries that preference.
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
                       "github.com/Beennnn/ableton-audio-output, puis ./install.sh")
    if dry:
        return True, f"[dry-run] réglerait la sortie d'Ableton sur « {want} »"
    # FIRST the modal dialog, THEN the setting: `live-output` opens Live's settings with
    # ⌘, — a keystroke Live ignores as long as a modal dialog is open. Without this step,
    # the fix timed out after 60 s while blaming accessibility, when the only obstacle was
    # an OK button to click. And that is the MOST FREQUENT case, since the dialog in
    # question is precisely the one Live displays when its output is missing — that is,
    # exactly when this fix gets called.
    cleared, note = dismiss_dialog()
    if not cleared:
        return False, note
    try:
        p = subprocess.run([str(exe), want], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # 60 s is very generous for a few clicks: if we get there, it means Live no
        # longer answers accessibility, not that the operation is a long one.
        return False, "Live n'a pas répondu (accessibilité bloquée ?)"
    # BOTH streams, not one OR the other: `live-output` re-emits the raw AppleScript error
    # on stdout AND writes its actionable translation on stderr. Taking `stdout or stderr`
    # therefore systematically kept the unreadable message and threw away the useful one.
    out = [l for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if l.strip()]
    joined = "\n".join(out)
    # code 4 = the script itself recognised the accessibility denial (see its README).
    if p.returncode == 4 or denied(joined):
        return False, DENIED_HINT
    msg = out[-1] if out else f"code {p.returncode}"
    if note:
        msg = f"{note} ; {msg}"
    return p.returncode == 0, msg


# ─── Live's modal dialog ──────────────────────────────────────────────────────────────
#
# "La section audio est désactivée. Veuillez sélectionner un périphérique de sortie audio
# dans les Réglages Audio." — that is what Live displays when it opens on a missing
# device (typically "No Device", restored from the previous session).
#
# It deserves its own handling for a reason that has nothing to do with aesthetics: it is
# MODAL. As long as it is there, Live listens to nothing any more — neither ⌘, to open its
# settings, nor the window tidying, nor the output fix. So the rig finds itself trying to
# repair an app that cannot answer it, and returning errors that describe the symptom
# ("Live did not answer") instead of the cause. Experienced on 2026-08-22, just after the
# launch: Ableton open, silent, stuck on that, and the rig blind.
#
# ⚠️ SAFETY RULE: we only dismiss dialogs whose single button is "OK". A dialog offering a
# CHOICE ("Save / Don't Save / Cancel") does not get clicked on its own — clicking at
# random in it can lose an unsaved set. A dialog with several buttons is reported, never
# resolved.
_DIALOG_SCAN = """
tell application "System Events"
  if not (exists process "Live") then return "NOPROC"
  tell process "Live"
    set dlgs to (windows whose subrole is "AXDialog")
    if (count of dlgs) is 0 then return ""
    set txt to ""
    set btns to ""
    repeat with e in (entire contents of item 1 of dlgs)
      try
        if role of e is "AXStaticText" then
          set v to value of e
          if v is not missing value and v is not "" then set txt to txt & v & " "
        else if role of e is "AXButton" then
          set d to description of e
          if d is missing value then set d to name of e
          if d is not missing value then set btns to btns & d & "|"
        end if
      end try
    end repeat
    return txt & "@@" & btns
  end tell
end tell
"""

# The button has NO `name` — only a `description` (read from accessibility on
# 2026-08-22). So a classic `click button "OK"` never finds it; one has to walk the tree
# and compare the description. The kind of detail that costs an hour of searching.
_DIALOG_CLICK = """
tell application "System Events" to tell process "Live"
  set dlgs to (windows whose subrole is "AXDialog")
  if (count of dlgs) is 0 then return "NODIALOG"
  repeat with e in (entire contents of item 1 of dlgs)
    try
      if role of e is "AXButton" then
        set d to description of e
        if d is missing value then set d to name of e
        if d is "OK" then
          click e
          return "CLICKED"
        end if
      end if
    end try
  end repeat
  return "NOBUTTON"
end tell
"""


def _osascript(src: str, timeout: float = 15) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "System Events n'a pas répondu"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, out


def dialog() -> tuple[str, list[str]] | None:
    """The modal dialog open in Live: (text, buttons). None if there is none.

    Also returns None when reading is impossible (Live absent, permission denied): this
    module then does not KNOW whether there is a dialog, and stating that inability is
    the accessibility check's job — not a false alarm here.
    """
    ok, out = _osascript(_DIALOG_SCAN)
    if not ok or out in ("", "NOPROC") or "@@" not in out:
        return None
    txt, _, btns = out.partition("@@")
    return txt.strip(), [b for b in btns.split("|") if b.strip()]


def dismiss_dialog() -> tuple[bool, str]:
    """Click OK on Live's modal dialog, if and only if that is its only button.

    Returns (True, "") when there is nothing to dismiss: the caller does not have to tell
    "no dialog" from "dialog dismissed", both leave it free to act.
    """
    d = dialog()
    if d is None:
        return True, ""
    txt, btns = d
    if [b.upper() for b in btns] != ["OK"]:
        return False, (f"Ableton attend une réponse à une fenêtre qui propose un choix "
                       f"({', '.join(btns) or 'boutons non lus'}) — à traiter à la main : "
                       f"« {txt} »")
    ok, out = _osascript(_DIALOG_CLICK)
    if ok and out == "CLICKED":
        return True, f"fenêtre congédiée : « {txt} »"
    return False, f"fenêtre non congédiée ({out or 'sans détail'}) : « {txt} »"
