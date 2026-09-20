"""Tempo Ableton Link du rig, servi par le dashboard (`/api/link`).

Ce module ne parle PAS à Link. Il pilote `bin/linkbridge.py`, qui est un process à
part — la raison est écrite en tête de ce fichier-là, et elle tient en un mot :
**licence**. `aalink` embarque Ableton Link, qui est GPL ; le projet a déjà décidé
et écrit que ce code reste confiné à un process isolé. Importer `aalink` ici
casserait cette décision, pas seulement une convention de style.

Le patron est celui de `spectrum.py` avec ffmpeg, volontairement, pour qu'il n'y ait
qu'une seule façon de faire dans ce dépôt : un sous-process qui produit un flux, un fil
qui le lit, un dernier état partagé, un démarrage à la demande et une extinction
automatique quand plus personne ne regarde.

TROIS CHOSES QUI SE LISENT MAL SI ON NE LES DIT PAS
====================================================

1. **`peers == 0` n'est PAS une panne, et ce n'est PAS non plus une réussite.** Link
   répond alors un tempo parfaitement bien formé — 120 par défaut — et une phase qui
   tourne, sans que rien ne soit synchronisé avec quoi que ce soit. Mesuré le
   2026-08-23 : Live 12 tournait, et le pont voyait `peers=0` parce que Link était
   simplement désactivé dans ses réglages. On remonte donc `peers` tel quel, et
   `synced` qui dit franchement s'il y a quelqu'un en face. **Une touche qui afficherait
   « 120 » dans ce cas mentirait**, et ce serait notre faute, pas celle de Link.

2. **`age` est aussi important que les valeurs.** Le pont peut mourir (Link retiré,
   `aalink` désinstallé, process tué) en laissant le dernier état en mémoire. Sans
   l'âge, ce cadavre se lit exactement comme une donnée fraîche. Au-delà de
   `link_stale_seconds`, on le dit : `fresh` passe à faux.

3. **Personne n'échantillonne la phase vite.** La phase est une fonction déterministe
   de l'horloge : le consommateur la recalcule chez lui à partir de (`tempo`, `beat`,
   `age`). Sonder ce point d'entrée dix fois par seconde n'apporterait rien de plus
   qu'une fois par seconde.

Et la même garantie qu'ailleurs : **rien ici ne doit pouvoir faire tomber le
dashboard**. Toute panne se traduit par `available: false` et sa raison en clair.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent.parent / "bin" / "linkbridge.py"

# Après un pont qui n'a jamais produit un seul tick (aalink absent, binaire cassé), on
# attend avant de réessayer. Sans ce frein, chaque appel HTTP relançait un process qui
# meurt aussitôt — une touche Stream Deck qui sonde à 1 Hz en aurait lancé 3600 par
# heure, et l'appelant aurait lu « en cours de démarrage » sans jamais voir la vraie
# raison, puisqu'un pont neuf efface l'erreur du précédent. Constaté en test.
_RETRY_AFTER_FAILURE = 5.0


class _Bridge:
    """Le sous-process pont + le fil qui le lit. Un seul, partagé par tous les appels."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.last: dict | None = None
        self.last_tick = 0.0
        self.error = ""
        self.last_read = 0.0
        # Raison du dernier démarrage qui a échoué + quand. Survit délibérément à
        # `_open()`, qui remet `error` à zéro : c'est la seule chose qui permette de
        # répondre « aalink absent » plutôt que « en cours de démarrage » à l'infini.
        self.fatal = ""
        self.failed_at = 0.0
        self.ticks = 0

    def _open(self, quantum: float) -> bool:
        if not _BRIDGE.exists():
            self.error = f"pont introuvable : {_BRIDGE}"
            return False
        try:
            # `sys.executable` et pas un chemin figé : le pont doit tourner dans
            # L'INTERPRÉTEUR QUI FAIT TOURNER LE RIG, puisque c'est là qu'`aalink` est
            # installé. Un `python3` du PATH serait un autre interpréteur, sans le paquet.
            self.proc = subprocess.Popen(
                [sys.executable, str(_BRIDGE), str(quantum)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except Exception as exc:
            self.error = f"pont Link impossible à lancer : {exc}"
            return False
        self.error = ""
        return True

    def _pump(self, idle_stop: float) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue  # un tick illisible se jette, il ne tue pas le flux
                    with self.lock:
                        self.last, self.last_tick = data, time.time()
                    self.ticks += 1
                # Même choix que spectrum.py : l'extinction est décidée dans le fil qui
                # lit, pas par un minuteur séparé qu'il faudrait tenir en vie et arrêter.
                if time.time() - self.last_read > idle_stop:
                    break
        except Exception as exc:
            self.error = f"lecture du pont interrompue : {exc}"
        finally:
            self._collect_stderr()
            if self.ticks == 0:
                # Le pont est mort sans jamais rien produire : c'est un échec de
                # démarrage, pas une extinction pour inactivité. On retient la raison et
                # on s'interdit de relancer tout de suite.
                self.fatal = self.error or "le pont Link s'est arrêté sans rien produire"
                self.failed_at = time.time()
            self.stop()

    def _collect_stderr(self) -> None:
        """Récupère la raison écrite par le pont — sans elle, un pont qui refuse de
        démarrer se lit comme un pont muet, ce qui n'aide personne."""
        p = self.proc
        if not p or not p.stderr:
            return
        try:
            tail = [l.strip() for l in p.stderr.read().splitlines() if l.strip()]
        except Exception:
            return
        # On ne garde que la dernière ligne utile : « aalink absent — installez-le… »
        # vaut mieux que trois lignes de démarrage nominal.
        for l in reversed(tail):
            if not l.startswith("link bridge"):
                self.error = l
                return

    def start(self, quantum: float, idle_stop: float) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        if self.fatal and time.time() - self.failed_at < _RETRY_AFTER_FAILURE:
            self.error = self.fatal
            return False
        self.ticks = 0
        if not self._open(quantum):
            return False
        self.last_read = time.time()
        self.thread = threading.Thread(target=self._pump, args=(idle_stop,), daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        with self.lock:
            # Le dernier état MEURT avec le pont : le garder ferait passer un cadavre
            # pour une lecture, ce qui est précisément le défaut qu'on veut éviter.
            self.last = None


_LINK = _Bridge()


def snapshot(cfg: dict) -> dict:
    """L'état Link courant. Ne lève jamais : au pire, `available` est faux."""
    srv = cfg.get("server", {})
    quantum = float(srv.get("link_quantum", 4))
    idle_stop = float(srv.get("link_idle_stop_seconds", 20))
    stale_after = float(srv.get("link_stale_seconds", 2))

    _LINK.last_read = time.time()
    if not _LINK.start(quantum, idle_stop):
        return {"available": False, "reason": _LINK.error}

    with _LINK.lock:
        data, ts = _LINK.last, _LINK.last_tick
    if data is None:
        # Le premier appel arrive avant le premier tick du pont (~100 ms). On le dit
        # plutôt que de rendre des zéros, qui se liraient comme un vrai tempo à 0.
        return {"available": False, "reason": _LINK.error or "pont Link en cours de démarrage"}

    age = time.time() - ts
    return {
        "available": True,
        "fresh": age <= stale_after,        # faux = le pont s'est tu, ne pas croire les valeurs
        "age": round(age, 2),
        "tempo": data.get("tempo"),
        "beat": data.get("beat"),
        "phase": data.get("phase"),
        "quantum": data.get("quantum", quantum),
        "peers": data.get("peers", 0),
        # `synced` est la question à laquelle une touche doit répondre : y a-t-il
        # quelqu'un en face ? Sans elle on affiche le tempo par défaut de Link comme
        # s'il venait de Live.
        "synced": bool(data.get("peers", 0)),
        "playing": bool(data.get("playing", False)),
    }
