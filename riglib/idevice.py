"""Lire la batterie de l'iPhone DEPUIS le Mac, via libimobiledevice.

Le Mac ne sait rien du téléphone par lui-même — mesuré le 2026-08-19 : le Bluetooth
n'expose que son adresse et sa puissance de signal (pas de champ batterie, contrairement
aux AirPods), `pmset -g accps` ne connaît que la batterie interne, et le lien Bome prouve
que le téléphone est JOIGNABLE, pas qu'il est branché — un iPhone sur le Wi-Fi et
débranché tient ce lien pendant qu'il se vide.

`ideviceinfo` est la seule voie qui donne le niveau ET l'état d'alimentation sans rien
demander au téléphone. C'est ce qui la rend précieuse : elle peut BATTRE régulièrement,
là où une automatisation iOS ne part qu'au branchement. Le battement est ce qui fait
vivre la pente de la batterie (riglib/checks.py), donc le seul démenti possible d'un
« en charge » devenu faux.

Prérequis, une fois pour toutes : iPhone branché en USB, déverrouillé, « Se fier », puis
« Afficher cet iPhone lorsqu'il est en Wi-Fi » dans le Finder. Sans ça tout ici rend
None — c'est un SUPPLÉMENT, jamais un prérequis : le téléphone peut toujours publier son
état lui-même (POST /api/phone), et les deux sources alimentent le même historique.
"""

from __future__ import annotations

import shutil
import subprocess

BATTERY_DOMAIN = "com.apple.mobile.battery"

# Cherché explicitement, et pas seulement dans le PATH : sous launchd celui-ci se limite
# à /usr/bin:/bin:/usr/sbin:/sbin, où Homebrew n'est pas. Un `which` y échouerait alors
# que la commande marche parfaitement dans un terminal — et le dashboard resterait
# aveugle sans jamais dire pourquoi.
_CANDIDATES = ("/opt/homebrew/bin/ideviceinfo", "/usr/local/bin/ideviceinfo")


def binary(cfg: dict) -> str | None:
    explicit = str(cfg.get("server", {}).get("idevice_bin", "")).strip()
    if explicit:
        return explicit if shutil.which(explicit) or explicit.startswith("/") else None
    return shutil.which("ideviceinfo") or next((p for p in _CANDIDATES if shutil.which(p)), None)


def _run(exe: str, args: list[str]) -> dict[str, str] | None:
    """Une lecture du domaine batterie → {clé: valeur}, ou None si l'appareil est absent."""
    try:
        p = subprocess.run([exe, *args, "-q", BATTERY_DOMAIN],
                           capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    out = {}
    for line in p.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out or None


def read(cfg: dict) -> dict | None:
    """{"battery": int, "charging": bool, "via": "usb"|"wifi"} — None si rien à lire.

    USB d'abord, réseau ensuite : quand le câble est là, il répond toujours, alors que
    la lecture par le réseau est rapportée comme intermittente en amont
    (libimobiledevice#947). On ne veut pas d'un chemin fiable écarté par un chemin
    incertain.

    « Branché » se lit sur ExternalConnected, pas sur BatteryIsCharging : à 100 % un
    téléphone sur secteur ne charge plus, et prendre le second ferait clignoter la ligne
    en rouge précisément quand tout va bien.
    """
    exe = binary(cfg)
    if not exe:
        return None
    for via, args in (("usb", []), ("wifi", ["-n"])):
        d = _run(exe, args)
        if not d:
            continue
        try:
            level = int(d.get("BatteryCurrentCapacity", ""))
        except ValueError:
            continue
        plugged = d.get("ExternalConnected", d.get("BatteryIsCharging", "")).lower() == "true"
        return {"battery": max(0, min(100, level)), "charging": plugged, "via": via}
    return None
