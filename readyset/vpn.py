"""VPN — detecting it, and above all knowing how to CUT it.

Why the rig cares: an active VPN rewrites the machine's routing. The Mac may then stop
seeing the iPhone driving Bome, or the stage modem, or the lamps — everything that lives
on the local network. You play, and the remote control stops answering.

Why cutting a VPN is NOT trivial (and why this module exists rather than a one-line
`scutil --nc stop`): modern clients (Surfshark first among them) install an "on-demand"
profile with an unconditional `Connect` rule. `scutil --nc stop` succeeds… and the tunnel
re-forms on the next packet, within half a second. You do not beat on-demand from user
space. The only reliable counter is to DISABLE THE NETWORK SERVICE: on-demand cannot
connect a service that is administratively switched off.
Full analysis: github.com/Beennnn/surfshark-toggle.

A consequence to own, and that is why every message repeats it: the cut is PERSISTENT —
it survives a reboot. That is intentional (otherwise it would not hold for a whole
evening), but it means a `readyset vpn on` is needed to get one's VPN back.

Disabling a service requires root. The sudoers rule installed by surfshark-toggle
(`install.sh`) allows it without a password, and ONLY for that sub-verb:
    <user> ALL=(root) NOPASSWD: /usr/sbin/networksetup -setnetworkserviceenabled *
Without it, we fall back on `scutil --nc stop` — enough for a VPN without on-demand
(Tailscale), powerless against Surfshark. The message says so instead of lying.
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
    """VPNs currently connected → [(service name, UUID)].

    `[PPP:Modem]` entries are discarded: here those are serial gadgets (a ToneX pedal,
    Seeed boards) that macOS files in the same list, not VPNs.
    `[checks.vpn].ignore` allows excluding a VPN we accept (a substring of the name).
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
    """Network services known to networksetup (a leading `*` = a disabled service)."""
    try:
        lines = _run(["networksetup", "-listallnetworkservices"], timeout=10).stdout.splitlines()
    except Exception:
        return set()
    return {l.lstrip("*").strip() for l in lines[1:] if l.strip()}


def disabled_services() -> list[str]:
    """Network services currently DISABLED — the ones a `readyset vpn on` must switch back on."""
    try:
        lines = _run(["networksetup", "-listallnetworkservices"], timeout=10).stdout.splitlines()
    except Exception:
        return []
    return [l.lstrip("*").strip() for l in lines[1:] if l.startswith("*")]


def _vpn_service_names() -> set[str]:
    """Service names that are VPNs (according to scutil), connected or not."""
    names = set()
    for line in _nc_list():
        if "[PPP:Modem]" in line or '"' not in line:
            continue
        if "VPN" in line:
            names.add(line.split('"')[1])
    return names


def _override_cmd(cfg: dict, name: str) -> str | None:
    """Custom cut-off command for this VPN (the key = a substring of the name)."""
    cmds = (cfg.get("checks", {}).get("vpn", {}) or {}).get("off_cmds", {}) or {}
    for pattern, cmd in cmds.items():
        if pattern.lower() in name.lower():
            return cmd
    return None


def _disable_service(name: str) -> tuple[bool, str]:
    # -n: never wait on a password prompt. Without the sudoers rule, we fail right away
    # with an actionable message rather than freezing the dashboard.
    r = _run(["sudo", "-n", "/usr/sbin/networksetup", "-setnetworkserviceenabled", name, "off"])
    if r.returncode == 0:
        return True, "service désactivé"
    err = (r.stderr or r.stdout).strip()
    if "password" in err.lower() or "sudo:" in err.lower():
        return False, ("sudo refuse sans mot de passe — installe la règle : "
                       "~/dev/music/surfshark-toggle/install.sh")
    return False, err or "networksetup a échoué"


def turn_off(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Cut every connected VPN. Returns (ok, multi-line message)."""
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
        # The tunnel may still be standing at this point: we bring it down right away.
        # The service disabled just before is what keeps it from coming back.
        if uuid:
            _run(["scutil", "--nc", "stop", uuid], timeout=10)

    if dry_run:
        return True, "\n".join(lines)

    time.sleep(1.5)      # the scutil state takes a moment to settle back down
    still = connected(cfg)
    if still:
        lines.append("⚠️ toujours connecté : " + ", ".join(n for n, _ in still))
        ok_all = False
    else:
        lines.append("🔒 VPN coupé — coupure PERSISTANTE (survit au redémarrage) ; "
                     "`readyset vpn on` pour le rétablir")
    return ok_all, "\n".join(lines)


def turn_on(cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Re-enable the VPN services a cut had switched off (the reverse of turn_off)."""
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
