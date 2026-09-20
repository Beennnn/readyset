"""The device catalogue — hardware illustrations, loaded from devices.toml.

WHAT A BUNDLE CANNOT GIVE US. Checks about SOFTWARE already have a picture: macOS keeps
it inside the bundle, `apps.icon_png()` pulls it out, and you recognise a browser by its
logo well before reading its name. Checks about HARDWARE have nothing of the sort — a
keyboard, a router, a lamp are not apps. They fell back on their family's generic emoji:
🌐 for the network, 💡 for the lamps, the same pictogram for two devices that have
nothing in common.

WHY THE DRAWINGS ARE IN A .toml AND NOT IN THIS FILE. They used to be Python string
constants, and two of them named a specific manufacturer's keyboard models inside an
engine that is otherwise generic. A catalogue is data: as data the engine goes back to
being generic, one person's gear leaves the source, and anyone can add their own device
without writing Python. devices.toml carries the reasoning behind each drawing.

This module is only the loader, plus the two rules that are genuinely engine rather than
catalogue: an `app:` key names its own app, and an `xapp:` key is resolved against the
live inventory of unexpected apps.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import quote

_CATALOGUE = Path(__file__).resolve().parent / "devices.toml"


def _slab_piano(palette: dict, badge: str, accent: str) -> str:
    """A generic stage slab piano: dark body, keys, and the model number.

    The only parameterised drawing, because it is the only one that has to exist twice.
    `badge` is the number printed on it, `accent` the name of a [palette] colour.
    """
    acc = palette[accent]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">
  <rect x="2" y="7" width="44" height="18" rx="2.5" fill="#1b1f28" stroke="{palette['muted']}" stroke-width="1.4"/>
  <rect x="4.5" y="14" width="39" height="9" rx="1" fill="{palette['line']}"/>
  <g fill="#11141a">
    <rect x="7.5" y="14" width="2.2" height="5.5"/><rect x="12" y="14" width="2.2" height="5.5"/>
    <rect x="19" y="14" width="2.2" height="5.5"/><rect x="23.5" y="14" width="2.2" height="5.5"/>
    <rect x="28" y="14" width="2.2" height="5.5"/>
    <rect x="35" y="14" width="2.2" height="5.5"/><rect x="39.5" y="14" width="2.2" height="5.5"/>
  </g>
  <rect x="4.5" y="9.5" width="12" height="2.6" rx="1.3" fill="{acc}"/>
  <text x="43.5" y="12.4" font-family="-apple-system,Helvetica,sans-serif" font-size="6.5"
        font-weight="700" fill="{acc}" text-anchor="end">{badge}</text>
  <path d="M9 25v3M39 25v3" stroke="{palette['muted']}" stroke-width="1.6" stroke-linecap="round"/>
</svg>'''


TEMPLATES = {"slab-piano": _slab_piano}


def _load() -> tuple[dict[str, str], list[dict]]:
    with _CATALOGUE.open("rb") as fh:
        data = tomllib.load(fh)
    palette = data.get("palette", {})
    svg: dict[str, str] = {}
    for dev in data.get("device", []):
        if "svg" in dev:
            svg[dev["id"]] = dev["svg"].format(**palette)
        else:
            params = {k: v for k, v in dev.items()
                      if k not in ("id", "name", "template")}
            svg[dev["id"]] = TEMPLATES[dev["template"]](palette, **params)
    return svg, data.get("binding", [])


# One slug → one drawing, and the ordered rules that tie a check to its thumbnails.
SVG, BINDINGS = _load()


def app_path_for(cfg: dict, label: str) -> str | None:
    """The .app bundle behind a check label.

    First the apps the rig launches itself (exact paths, given by rig.toml), otherwise a
    glance in /Applications — a DAW is NOT in that list (the program opens a set, it does
    not launch the app) and its name carries its version, hence the glob.
    """
    for app in cfg.get("launch", {}).get("apps", []):
        if label.lower() in Path(app).stem.lower():
            return app
    hits = sorted(Path("/Applications").glob(f"{label}*.app"))
    return str(hits[0]) if hits else None


def icons_for(cfg: dict, key: str, label: str) -> list[str]:
    """A check's icon URLs, in display order. Empty = fall back to the generic emoji."""
    def gear(slug: str) -> str:
        return f"/api/gear?id={slug}"

    def app(name: str) -> list[str]:
        path = app_path_for(cfg, name)
        # quote(): an app path can hold spaces, and an unencoded URL only works by the
        # browser's goodwill.
        return [f"/api/appicon?path={quote(path)}"] if path else []

    # Catalogue rules first, read top to bottom — first match wins, so devices.toml
    # decides. See its [[binding]] section.
    for b in BINDINGS:
        if "key" in b and b["key"] != key:
            continue
        if "key_prefix" in b and not key.startswith(b["key_prefix"]):
            continue
        if "label_contains" in b and b["label_contains"] not in label:
            continue
        out: list[str] = []
        for name in b.get("apps", []):
            out += app(name)
        out += [gear(slug) for slug in b.get("devices", [])]
        return out

    # The two rules below are ENGINE, not catalogue: they derive the app from the key
    # itself, so no entry in devices.toml could say anything more about them.
    #
    # "<app> running" checks carry the app's name in their key: there it is the real
    # macOS icon we want, not a drawing.
    if key.startswith("app:"):
        return app(key.split(":", 1)[1])
    # Unexpected apps: their exact path is already known to the inventory (cached), and
    # that is where the icon counts most — you recognise a chat app by its coloured
    # bubble before reading its name, in a list where you are about to close things.
    if key.startswith("xapp:"):
        from .. import apps as _apps
        name = key.split(":", 1)[1]
        for a in _apps.unexpected(cfg):
            if a["name"] == name:
                return [f"/api/appicon?path={quote(a['path'])}"]
    return []
