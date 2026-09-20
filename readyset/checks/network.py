"""Checks on the NETWORK — the stage subnet, the phone's link, and the VPN that breaks both.

Also holds mode resolution: "auto" becomes studio or live depending on whether the
studio router answers, which is a network question and nothing else.

The VPN is here rather than under "system" for a reason worth remembering: an active
tunnel rewrites the machine's routing, so it is the single failure that can take out the
phone link, the stage modem and the lamps at once.
"""

from __future__ import annotations

import subprocess

from .. import vpn as vpn_control
from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint
from .apps import _pgrep


def _ping(host: str, timeout_s: int = 1) -> bool:
    try:
        return subprocess.run(["ping", "-c1", f"-t{timeout_s}", host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout_s + 2).returncode == 0
    except Exception:
        return False


def resolve_mode(cfg: dict, requested: str = "auto") -> str:
    """live | studio | auto → concrete mode. Auto = studio when the studio router
    (192.168.1.1) answers, else live. So the tool boots live and flips to studio
    only once it sees the home network."""
    if requested in ("live", "studio"):
        return requested
    return "studio" if _ping(cfg["checks"].get("studio_router", "192.168.1.1")) else "live"


def _network_severity(cfg: dict, mode: str) -> str:
    """Severity of the two stage-network checks, per mode. Both answer the same physical
    question — « suis-je branché sur le réseau de scène ? » — so they share one knob:
    blocking on stage (no modem = no iPhone, no lamps, no remote), informational at the
    desk, where that network is simply somewhere else and can never answer."""
    return cfg["modes"].get(mode, {}).get("network_severity", "fail")


def check_stage_network(cfg: dict, mode: str) -> Result | None:
    """Le réseau de scène, en UNE ligne à deux étages plutôt qu'en deux checks.

    Ils posaient déjà la même question physique — « suis-je sur le réseau de scène ? » —
    au point de partager un seul réglage de sévérité. Séparés, ils s'allumaient toujours
    ensemble et coûtaient deux lignes pour un seul fait.

    L'ordre du diagnostic va de la cause à la conséquence : le modem D'ABORD, parce que
    c'est lui qui distribue les adresses. Modem éteint → aucune IP possible, et annoncer
    « le Mac n'est pas sur le réseau » ferait chercher du côté du Mac un problème qui est
    dans la mallette. Modem debout mais pas d'IP → là seulement, c'est le Mac (câble,
    mauvais WiFi). Le cas inverse existe aussi : une IP et un modem muet, c'est-à-dire un
    modem qui a donné le bail puis a lâché.
    """
    sev = _network_severity(cfg, mode)
    if sev == OFF:
        return None
    prefix = cfg["checks"].get("stage_network", "192.168.1")
    host = cfg["checks"].get("modem_host", "192.168.1.1")
    label = f"Réseau de scène ({prefix}.x)"

    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return Result("net:stage", label, WARN, f"ifconfig: {exc}")
    ip = next((w for line in out.splitlines() if f"inet {prefix}." in line
               for w in line.split() if w.startswith(f"{prefix}.")), None)
    modem = _ping(host)

    if modem and ip:
        return Result("net:stage", label, OK, f"Mac en {ip} · modem {host} répond")

    if sev != FAIL:      # au bureau, ce réseau est simplement ailleurs
        return Result("net:stage", label, sev, "absent (normal hors scène)")

    if not modem and not ip:
        detail = _hint(f"le modem {host} ne répond pas, et le Mac n'a pas d'IP en {prefix}.x",
                       "allumer le modem de scène EN PREMIER : c'est lui qui distribue les "
                       "adresses, le Mac ne peut rien obtenir tant qu'il est éteint")
    elif not modem:
        detail = _hint(f"Mac en {ip}, mais le modem {host} ne répond pas",
                       "le modem a donné son adresse puis s'est tu — le rallumer ; "
                       "sans lui, ni iPhone, ni lampes, ni télécommande")
    else:
        detail = _hint(f"le modem {host} répond, mais le Mac n'a pas d'IP en {prefix}.x",
                       "côté Mac : vérifier le câble, ou qu'il est bien sur le WiFi de "
                       "scène et pas resté sur un autre réseau")
    return Result("net:stage", label, sev, detail)


def check_bome_iphone(cfg: dict) -> Result:
    """Detect the Bome Network ↔ iPhone link via an ESTABLISHED TCP connection on
    Bome Network's port (37000). The iPhone runs Bome Network and connects here."""
    port = cfg["checks"].get("bome_network_port", 37000)
    host = str(cfg["checks"].get("iphone_host", "")).strip()
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=6,
        ).stdout
    except Exception as exc:
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL, f"lsof: {exc}")

    lines = [l for l in out.splitlines() if "ESTABLISHED" in l]
    if host:
        lines = [l for l in lines if host in l]
    if not lines:
        # Le lien a DEUX bouts, et le conseil ne vaut que s'il désigne le bon. Bome
        # Network éteint sur le Mac est visible d'ici ; s'il tourne, alors le côté
        # muet est forcément le téléphone — c'est la seule chose qu'on ne voit pas.
        # remedy.py suit exactement la même règle : pas de bouton « relancer » quand
        # le Mac est déjà en ordre, sinon on relance ce qui marche.
        mac_side = cfg["checks"]["apps"].get("Bome Network", "Bome Network")
        if not _pgrep(mac_side):
            advice = "lancer Bome Network sur le MAC (il est éteint ici)"
        else:
            advice = ("Bome Network tourne sur le Mac → c'est côté IPHONE qu'il n'est "
                      "pas lancé. L'ouvrir sur le téléphone, et vérifier qu'il est sur "
                      "le même réseau que le Mac")
        return Result("net:iphone", "Bome Network ↔ iPhone", FAIL,
                      _hint("aucune connexion", advice))
    # NAME column looks like "192.168.1.10:37000->192.168.1.20:52345 (ESTABLISHED)"
    peer = ""
    for tok in lines[0].split():
        if "->" in tok:
            peer = tok.split("->", 1)[1]
            break
    return Result("net:iphone", "Bome Network ↔ iPhone", OK,
                  f"connecté{f' ({peer})' if peer else ''}")


# Le parsing vit dans readyset/vpn.py, avec la coupure : un seul lecteur de `scutil --nc
# list` pour les deux, sinon le check et le fix finissent par ne plus parler du même VPN.
# (Les entrées [PPP:Modem] y sont écartées : ce sont des gadgets série — pédale ToneX,
# cartes Seeed — que macOS range dans la même liste, pas des VPN.)
def check_vpn(cfg: dict) -> Result:
    try:
        active = vpn_control.connected(cfg)
    except Exception as exc:
        return Result("sys:vpn", "VPN inactif", WARN, f"scutil: {exc}")
    if not active:
        return Result("sys:vpn", "VPN inactif", OK, "")
    names = ", ".join(n for n, _ in active if n) or "?"
    return Result("sys:vpn", "VPN inactif", FAIL, f"VPN actif : {names}")
