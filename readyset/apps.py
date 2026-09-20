"""Extra apps — the ones running while the rig has no need for them.

On stage, every extra open app is a risk: a WhatsApp notification popping up in front of
the set, a browser waking up the WiFi, an updater deciding to launch during the second
song, RAM and CPU taken away from Ableton. At the office it has no consequence — hence a
per-mode severity: **warning in live, plain info in studio** (tunable through
`unexpected_apps_severity`, `"off"` to stop displaying anything at all).

What "app" means here: macOS distinguishes apps with a real on-screen presence (Dock
icon, windows, menus — `type="Foreground"`) from menu-bar agents (`type="UIElement"`) and
from daemons. Only the former count. Without that filter the list would surface Bartender,
Stats, LuLu, MidiTray… — a score of utilities we obviously do not want to offer to close,
and which would drown out the three that matter. Bome (Translator and Network) is not
there either: those are background apps, they cannot clutter the screen.

The inventory comes from `lsappinfo list`, NOT from System Events. The AppleScript version
(`every application process whose background only is false`) worked in a terminal and
**timed out after 20 s** once launched by launchd: each `name of p` / `file of p`
re-evaluates the `whose` filter, and outside an interactive session that is pathologically
slow — so the dashboard never saw a single extra app (observed on 2026-08-18). `lsappinfo`
answers in ~10 ms, in a single call, without any accessibility permission, and reports as
a bonus the hidden apps (⌘H) — which AppleScript would have let through even though they
still consume CPU and notifications.

Removed from the list: the rig's own apps (they MUST be running) and everything listed in
`[checks.unexpected_apps].allow`. The Finder is in there by default: one does not "quit"
the Finder, macOS relaunches it.

Closing is ALWAYS a clean `quit`, never a `kill`: an app with an unsaved document must be
able to ask its question. If it does not leave within 6 s, we say so instead of insisting
— it is almost always a dialog box waiting for a click.
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

# Very short memo: a single dashboard render calls this list once for the check, once for
# the panel, then once per "Quitter" button to resolve — i.e. about ten calls for one
# identical answer. 2 s is enough to cover one render cycle, and little enough that a
# closed app disappears on the next refresh.
_CACHE_TTL = 2.0
_cache: dict = {"at": 0.0, "apps": []}


def running_gui_apps(max_age: float = _CACHE_TTL) -> list[dict]:
    """Apps with an on-screen presence → [{"name", "path"}], sorted by name."""
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
    # `lsappinfo list` outputs one block per app, opened by a line like `48) "Signal" ASN:…`.
    # We spot those headers, then read the bundle path and the type between two headers.
    heads = list(_ENTRY.finditer(r.stdout))
    for i, h in enumerate(heads):
        body = r.stdout[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(r.stdout)]
        # Some apps prefix their name with an invisible reading-direction mark
        # (WhatsApp exposes "‎WhatsApp"): it breaks terminal alignment and sorting,
        # for a character nobody ever sees.
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
    """Open apps the rig has no need for → [{"name", "path"}]."""
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
    """PNG thumbnail of the app's icon, cached on disk.

    Converting an .icns with `sips` costs ~100 ms; the dashboard refreshes every
    4 s, so without a cache we would re-run sips in a loop for identical images.
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
    """Ask an app to quit cleanly, and check that it really did leave."""
    name = Path(app_path).stem
    if dry_run:
        return True, f"[dry-run] demanderait à {name} de quitter"
    bid = windows.bundle_id(app_path)
    if not bid:
        return False, f"{name} — identifiant de bundle introuvable"
    r = subprocess.run(["osascript", "-e", f'tell application id "{bid}" to quit'],
                       capture_output=True, text=True, timeout=20)
    # An app may refuse to hand control back right away (a save dialog): we give it time
    # to disappear rather than concluding from the return code.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        # max_age=0: here we want the REAL state, not the memo — this is precisely the
        # moment the list changes, and a 2 s cache would wrongly conclude "still there".
        if not any(a["path"] == app_path.rstrip("/") for a in running_gui_apps(max_age=0)):
            return True, f"{name} — fermée"
        time.sleep(0.7)
    err = r.stderr.strip()
    return False, (f"{name} — toujours là (document non enregistré ? une fenêtre attend "
                   f"peut-être un clic){f' — {err}' if err else ''}")


def quit_many(cfg: dict, paths: list[str], dry_run: bool = False) -> tuple[bool, str]:
    """Close a selection of apps. Accepts ONLY apps that really are "extra".

    The dashboard listens on 127.0.0.1 only, but nothing forces us to trust the body of
    the request: we re-validate the selection against the list computed server-side, so
    that an arbitrary path cannot be passed to `quit`.
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
