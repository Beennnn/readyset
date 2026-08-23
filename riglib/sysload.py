"""System load — memory pressure, swap, load average, and who is eating the RAM.

Pourquoi ce module existe, et pourquoi il regarde la MÉMOIRE avant le CPU.

Le 2026-08-23, un set qui « faisait exploser le CPU » a été mesuré : Live à 61 % d'un
cœur (rien du tout sur 10 cœurs), mais `coreaudiod` à 92 %, `WindowServer` à 93 %,
un load average de 94, et surtout **119 Mo de RAM libre sur 32 Go**, 13 Go dans le
compresseur, 8 Go de swap sur 9,2 Go disponibles. Live pesait 17 Go à lui seul.

L'enchaînement est toujours le même, et il se lit à l'envers de l'intuition :

    la RAM manque → macOS compresse puis pagine sur le SSD → chaque accès mémoire du
    thread audio peut déclencher une décompression ou une lecture disque → le thread
    rate sa deadline → coreaudiod brûle du CPU à essayer de tenir le buffer → on
    accuse le CPU.

Autrement dit : **un « CPU qui explose » sur un Mac audio est le plus souvent une
panne de mémoire**. Le CPU est le symptôme, la pagination est la maladie. C'est
pourquoi les checks d'ici pèsent la pression mémoire et le swap en premier, la charge
en second, et ne prétendent jamais mesurer « le CPU de Live » — qui ne veut rien dire
d'utile pris isolément.

Toutes les mesures sont bon marché (sysctl, ps, vm_stat) et peuvent tourner à chaque
cycle du monitor, contrairement à `system_profiler` côté audio. `memory_pressure(8)`
est volontairement ÉVITÉ : il coûte ~1 s, là où `kern.memorystatus_vm_pressure_level`
donne le même verdict instantanément.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import apps, windows

# Niveaux de pression mémoire du noyau (kern.memorystatus_vm_pressure_level).
# 1 = normal, 2 = warning, 4 = critical. Il n'y a pas de 3.
_PRESSURE_NORMAL, _PRESSURE_WARN, _PRESSURE_CRITICAL = 1, 2, 4

_PRESSURE_LABEL = {
    _PRESSURE_NORMAL: "normale",
    _PRESSURE_WARN: "élevée",
    _PRESSURE_CRITICAL: "critique",
}


def _sysctl(name: str) -> str | None:
    try:
        r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def ncpu() -> int:
    raw = _sysctl("hw.ncpu")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def pressure_level() -> int | None:
    """Verdict du noyau sur la mémoire : 1 normal, 2 élevé, 4 critique.

    C'est la MÊME grandeur que celle qui pilote l'indicateur de pression mémoire du
    Moniteur d'activité — donc ce qu'on lit ici est ce que Benoît verrait à l'écran,
    pas une reconstitution maison à partir de pages libres.
    """
    raw = _sysctl("kern.memorystatus_vm_pressure_level")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def swap() -> tuple[float, float]:
    """(utilisé Mo, total Mo). (0, 0) si la valeur est illisible.

    Le swap est le meilleur signal unique dont on dispose : contrairement à « RAM
    libre », il ne descend pas tout seul. Une fois que macOS a paginé, les pages
    restent sur le SSD jusqu'à ce qu'on en ait besoin — donc un swap élevé raconte
    aussi ce qui S'EST passé, pas seulement l'instant présent.
    """
    raw = _sysctl("vm.swapusage") or ""
    # Format : "total = 9216,00M  used = 8012,25M  free = 1203,75M  (encrypted)"
    # Le séparateur décimal suit la locale du système — d'où le remplacement de la virgule.
    used = total = 0.0
    for token, target in (("used", "used"), ("total", "total")):
        marker = f"{token} = "
        if marker not in raw:
            continue
        chunk = raw.split(marker, 1)[1].split()[0].replace(",", ".")
        try:
            value = float(chunk.rstrip("MGK"))
        except ValueError:
            continue
        if chunk.endswith("G"):
            value *= 1024
        elif chunk.endswith("K"):
            value /= 1024
        if target == "used":
            used = value
        else:
            total = value
    return used, total


def load_average() -> float | None:
    """La charge sur 1 minute, brute. `{ 94,31 134,14 179,21 }` → 94.31."""
    raw = _sysctl("vm.loadavg") or ""
    parts = raw.replace("{", " ").replace("}", " ").split()
    if not parts:
        return None
    try:
        return float(parts[0].replace(",", "."))
    except ValueError:
        return None


@dataclass
class Snapshot:
    pressure: int | None            # 1 / 2 / 4, cf. pressure_level()
    swap_used_mb: float
    swap_total_mb: float
    load1: float | None
    cpus: int
    hogs: list[dict] = field(default_factory=list)

    @property
    def swap_pct(self) -> float:
        return 100.0 * self.swap_used_mb / self.swap_total_mb if self.swap_total_mb else 0.0

    @property
    def load_per_cpu(self) -> float | None:
        return self.load1 / self.cpus if self.load1 is not None else None

    @property
    def pressure_label(self) -> str:
        return _PRESSURE_LABEL.get(self.pressure or 0, "inconnue")


def _rss_by_bundle() -> dict[str, float]:
    """RSS agrégé en Mo, par bundle d'application (`/Applications/Foo.app`).

    Deux précautions qui font toute la différence sur un Mac de scène :

    - **Agrégation par bundle**, pas par processus. Chrome, Ableton et Claude éclatent
      leur travail sur des dizaines de processus enfants ; les compter séparément
      donnerait « 12 helpers à 300 Mo » au lieu de « Chrome : 4 Go », c'est-à-dire
      exactement l'information qu'on cherche à ne PAS avoir.
    - **Le RSS surestime** : la mémoire partagée entre processus d'un même bundle est
      comptée plusieurs fois. On l'assume — l'objet ici est de CLASSER les gloutons,
      pas de facturer des octets. Une app en tête de ce classement l'est vraiment.
    """
    try:
        r = subprocess.run(["ps", "-Ao", "rss=,comm="], capture_output=True,
                           text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}

    totals: dict[str, float] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rss_str, _, comm = line.partition(" ")
        try:
            rss_mb = int(rss_str) / 1024.0
        except ValueError:
            continue
        comm = comm.strip()
        marker = ".app/"
        idx = comm.find(marker)
        if idx == -1:
            continue
        bundle = comm[: idx + len(marker) - 1]  # garde le ".app", jette le "/"
        totals[bundle] = totals.get(bundle, 0.0) + rss_mb
    return totals


def hogs(cfg: dict, min_mb: float = 800.0) -> list[dict]:
    """Apps GUI **non nécessaires au rig** qui dépassent `min_mb` de RSS.

    Le filtre est le même que celui de `apps.unexpected()` — même notion de « le rig
    n'en a pas besoin », même liste d'exceptions dans la config — mais le critère de
    sélection change : ici on ne demande pas « est-elle ouverte ? » mais « pèse-t-elle
    lourd ? ». Une app peut être légitimement ouverte et rester un problème parce
    qu'elle tient trois giga.

    Les apps du rig sont exclues volontairement. Live à 17 Go est une observation, pas
    un défaut : c'est le travail qu'on lui demande. Rien ici ne proposera jamais de
    fermer l'instrument.
    """
    by_bundle = _rss_by_bundle()
    if not by_bundle:
        return []
    out = []
    for app in apps.unexpected(cfg):
        path = app["path"].rstrip("/")
        mb = by_bundle.get(path)
        if mb is None or mb < min_mb:
            continue
        out.append({"name": app["name"], "path": path, "mb": round(mb)})
    return sorted(out, key=lambda a: a["mb"], reverse=True)


def rig_footprint(cfg: dict) -> list[dict]:
    """Empreinte mémoire des apps DU rig — purement informatif, jamais un défaut.

    Sert à répondre à « d'où viennent les 30 Go ? » sans que personne ait à ouvrir le
    Moniteur d'activité pendant les balances.
    """
    by_bundle = _rss_by_bundle()
    out = []
    for path in windows.rig_apps(cfg):
        path = path.rstrip("/")
        mb = by_bundle.get(path)
        if mb:
            out.append({"name": Path(path).stem, "path": path, "mb": round(mb)})
    return sorted(out, key=lambda a: a["mb"], reverse=True)


def snapshot(cfg: dict, min_hog_mb: float = 800.0) -> Snapshot:
    used, total = swap()
    return Snapshot(
        pressure=pressure_level(),
        swap_used_mb=used,
        swap_total_mb=total,
        load1=load_average(),
        cpus=ncpu(),
        hogs=hogs(cfg, min_hog_mb),
    )
