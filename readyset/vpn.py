"""VPN — le détecter, et surtout savoir le COUPER.

Pourquoi le rig s'en préoccupe : un VPN actif réécrit le routage de la machine. Le Mac
peut alors ne plus voir l'iPhone qui pilote Bome, ni le modem de scène, ni les lampes —
tout ce qui vit sur le réseau local. On joue, et la télécommande ne répond plus.

Pourquoi couper un VPN n'est PAS trivial (et pourquoi ce module existe plutôt qu'un
`scutil --nc stop` en une ligne) : les clients modernes (Surfshark en tête) installent
un profil « on-demand » avec une règle `Connect` inconditionnelle. `scutil --nc stop`
réussit… et le tunnel se reforme au paquet suivant, dans la demi-seconde. On ne bat pas
l'on-demand depuis l'espace utilisateur. La seule parade fiable est de DÉSACTIVER LE
SERVICE RÉSEAU : on-demand ne peut pas connecter un service administrativement éteint.
Analyse complète : github.com/Beennnn/surfshark-toggle.

Conséquence à assumer, et c'est pour ça que chaque message le répète : la coupure est
PERSISTANTE — elle survit au redémarrage. C'est voulu (sinon elle ne tiendrait pas une
soirée), mais ça veut dire qu'un `readyset vpn on` est nécessaire pour retrouver son VPN.

La désactivation de service demande root. La règle sudoers posée par surfshark-toggle
(`install.sh`) l'autorise sans mot de passe, et UNIQUEMENT pour ce sous-verbe :
    <user> ALL=(root) NOPASSWD: /usr/sbin/networksetup -setnetworkserviceenabled *
Sans elle, on retombe sur `scutil --nc stop` — suffisant pour un VPN sans on-demand
(Tailscale), inopérant contre Surfshark. Le message le dit au lieu de mentir.
"""

from __future__ import annotations

import re
import subprocess
import time

_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _nc_list() -> list[str]:
    try:
        return _run(["scutil", "--nc", "list"], timeout=5).stdout.splitlines()
    except Exception:
        return []


def connected(cfg: dict | None = None) -> list[tuple[str, str]]:
    """VPN actuellement connectés → [(nom du service, UUID)].

    Les entrées `[PPP:Modem]` sont écartées : ici ce sont des gadgets série (pédale
    ToneX, cartes Seeed) que macOS range dans la même liste, pas des VPN.
    `[checks.vpn].ignore` permet d'exclure un VPN qu'on assume (sous-chaîne du nom).
    """
    ignore = [s.lower() for s in ((cfg or {}).get("checks", {}).get("vpn", {}) or {}).get("ignore", [])]
    out = []
    for line in _nc_list():
        if "(Connected)" not in line or "[PPP:Modem]" in line:
            continue
        name = line.split('"')[1] if '"' in line else ""
        if any(pat in name.lower() for pat in ignore):
            continue
        m = _UUID.search(line)
        out.append((name, m.group(0) if m else ""))
    return out


def _network_services() -> set[str]:
    """Services réseau connus de networksetup (le `*` en tête = service désactivé)."""
    try:
        lines = _run(["networksetup", "-listallnetworkservices"], timeout=10).stdout.splitlines()
    except Exception:
        return set()
    return {l.lstrip("*").strip() for l in lines[1:] if l.strip()}


def disabled_services() -> list[str]:
    """Services réseau actuellement DÉSACTIVÉS — ceux qu'un `readyset vpn on` doit rallumer."""
    try:
        lines = _run(["networksetup", "-listallnetworkservices"], timeout=10).stdout.splitlines()
    except Exception:
        return []
    return [l.lstrip("*").strip() for l in lines[1:] if l.startswith("*")]


def _vpn_service_names() -> set[str]:
    """Noms de service qui sont des VPN (d'après scutil), connectés ou non."""
    names = set()
    for line in _nc_list():
        if "[PPP:Modem]" in line or '"' not in line:
            continue
        if "VPN" in line:
            names.add(line.split('"')[1])
    return names


def _override_cmd(cfg: dict, name: str) -> str | None:
    """Commande de coupure sur mesure pour ce VPN (clé = sous-chaîne du nom)."""
    cmds = (cfg.get("checks", {}).get("vpn", {}) or {}).get("off_cmds", {}) or {}
    for pattern, cmd in cmds.items():
        if pattern.lower() in name.lower():
            return cmd
    return None


def _disable_service(name: str) -> tuple[bool, str]:
    # -n : jamais d'attente sur un prompt de mot de passe. Sans la règle sudoers, on
    # échoue tout de suite avec un message actionnable plutôt que de figer le dashboard.
    r = _run(["sudo", "-n", "/usr/sbin/networksetup", "-setnetworkserviceenabled", name, "off"])
    if r.returncode == 0:
        return True, "service désactivé"
    err = (r.stderr or r.stdout).strip()
    if "password" in err.lower() or "sudo:" in err.lower():
        return False, ("sudo refuse sans mot de passe — installe la règle : "
                       "~/dev/music/surfshark-toggle/install.sh")
    return False, err or "networksetup a échoué"


def turn_off(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Coupe tous les VPN connectés. Retourne (ok, message multi-lignes)."""
    conns = connected(cfg)
    if not conns:
        return True, "aucun VPN actif — rien à couper"

    services = _network_services()
    lines: list[str] = []
    ok_all = True
    for name, uuid in conns:
        custom = _override_cmd(cfg, name)
        if dry_run:
            how = (f"via la commande configurée : {custom}" if custom
                   else "en désactivant le service réseau" if name in services
                   else "via scutil --nc stop (pas de service réseau correspondant)")
            lines.append(f"[dry-run] couperait « {name} » {how}")
            continue

        if custom:
            r = subprocess.run(custom, shell=True, capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            lines.append(f"{'✔' if ok else '✖'} {name} — "
                         f"{(r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else 'commande exécutée'}")
            ok_all = ok_all and ok
            continue

        if name in services:
            ok, msg = _disable_service(name)
            ok_all = ok_all and ok
            lines.append(f"{'✔' if ok else '✖'} {name} — {msg}")
        else:
            ok = True
            lines.append(f"• {name} — pas de service réseau : seulement scutil stop "
                         f"(se reconnectera si l'app le redemande)")
        # Le tunnel peut être encore debout à cet instant : on le fait tomber tout de
        # suite. Le service désactivé juste avant est ce qui l'empêche de revenir.
        if uuid:
            _run(["scutil", "--nc", "stop", uuid], timeout=10)

    if dry_run:
        return True, "\n".join(lines)

    time.sleep(1.5)      # l'état scutil met un instant à retomber
    still = connected(cfg)
    if still:
        lines.append("⚠️ toujours connecté : " + ", ".join(n for n, _ in still))
        ok_all = False
    else:
        lines.append("🔒 VPN coupé — coupure PERSISTANTE (survit au redémarrage) ; "
                     "`readyset vpn on` pour le rétablir")
    return ok_all, "\n".join(lines)


def turn_on(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Réactive les services VPN qu'une coupure avait éteints (l'inverse de turn_off)."""
    targets = [s for s in disabled_services() if s in _vpn_service_names()]
    if not targets:
        return True, "aucun service VPN désactivé — rien à rétablir"
    if dry_run:
        return True, "[dry-run] réactiverait : " + ", ".join(targets)
    lines = []
    ok_all = True
    for name in targets:
        r = _run(["sudo", "-n", "/usr/sbin/networksetup", "-setnetworkserviceenabled", name, "on"])
        ok = r.returncode == 0
        ok_all = ok_all and ok
        lines.append(f"{'✔' if ok else '✖'} {name} — "
                     f"{'service réactivé' if ok else (r.stderr or r.stdout).strip()}")
    lines.append("Le VPN peut se reconnecter tout seul (on-demand) — c'est le but.")
    return ok_all, "\n".join(lines)


def status(cfg: dict) -> str:
    conns = connected(cfg)
    off = [s for s in disabled_services() if s in _vpn_service_names()]
    lines = []
    lines.append("VPN actif : " + (", ".join(n for n, _ in conns) if conns else "aucun"))
    if off:
        lines.append("Service(s) VPN désactivé(s) : " + ", ".join(off) + "  (`readyset vpn on` pour rétablir)")
    return "\n".join(lines)
