#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pont Ableton Link → une ligne JSON par tick sur stdout.

POURQUOI CE FICHIER EST UN PROCESS À PART, ET NON UN MODULE DE `riglib`
======================================================================

**Licence.** `aalink` embarque [Ableton Link](https://github.com/Ableton/link), qui est
**GPLv2-or-later** (double licence : le closed-source exige un accord commercial avec
Ableton). Le projet a déjà tranché ce point ailleurs et l'a écrit : « GPL (via
aalink/Link) est confiné à UN process — `beatsync.py` » (README de `~/dev/music/midi`).
Ce fichier est le second occupant de cette règle, pas une exception à celle-ci. Le
corollaire vaut surtout pour l'aval : `readyset`, lui, porte une LICENSE et a vocation
à être publié — **ne jamais y remonter ce fichier sans traiter la licence d'abord**, et
ne jamais importer `aalink` depuis `riglib/`.

**Boucle d'événements.** `aalink` impose asyncio ; le serveur du rig est un
`http.server` synchrone servi par des fils. Deux modèles de concurrence dans le même
process, c'est une source de blocages pour un gain nul.

**Cycle de vie.** Un pair Link qui rejoint et quitte la session en boucle pollue la
session de TOUS les autres pairs, Live compris. Un process dédié qu'on démarre et
qu'on arrête franchement est un pair propre ; un thread accroché à la vie du serveur
ne l'est pas.

C'est le même patron que `riglib/spectrum.py` avec ffmpeg : un sous-process qui produit
un flux, un fil qui le lit, un dernier état partagé.

CE QU'IL FAUT SAVOIR DE LINK POUR LIRE CE QUI SUIT
==================================================

- `tempo` est le BPM de la **session**, pas celui de Live : n'importe quel pair peut
  l'écrire et tout le monde suit. Ce pont **ne l'écrit jamais** — il regarde, il ne
  mène pas. (`beatsync.py`, lui, écrit ; deux écrivains se battraient.)
- `beat` est une timeline de battements **continue et monotone** qui avance **même sans
  aucun pair et même à l'arrêt**. Ce n'est pas la position dans le morceau de Live.
- `phase` est la position dans la mesure, dans `[0, quantum)`. C'est elle qui rend un
  affichage de tempo lisible en jouant : sans elle on affiche un nombre qui ne bouge pas.
- `peers` est le nombre d'AUTRES pairs. **`peers == 0` est le piège de ce module** :
  Link renvoie alors un tempo parfaitement bien formé (120 par défaut) et une phase qui
  tourne, alors que rien n'est synchronisé avec rien. Un afficheur qui ne distingue pas
  ce cas ment. On le remonte tel quel et c'est au consommateur de le traiter.
- `playing` est le transport partagé (start/stop sync), un axe SÉPARÉ du beat : un pair
  peut être à l'arrêt pendant que `beat` continue d'avancer.

PROTOCOLE DE SORTIE
===================

Une ligne JSON par tick sur **stdout**, rien d'autre — les journaux vont sur **stderr**,
sinon ils se mêleraient au flux que le lecteur analyse (piège déjà rencontré en testant
un add-on maison). Un tick illisible se jette sans tuer le flux.
"""

from __future__ import annotations

import asyncio
import json
import sys

# Cadence d'émission. 10 Hz est très au-dessus du besoin : le consommateur EXTRAPOLE la
# phase localement à partir de (tempo, beat, âge) — la phase est une fonction
# déterministe de l'horloge, pas une valeur qu'il faut échantillonner vite. Ce qu'on
# gagne à 10 Hz, c'est la fraîcheur du tempo et du nombre de pairs quand ils changent.
TICK_SECONDS = 0.1

# Quantum par défaut : 4 battements = une mesure à 4 temps. Link ne garantit
# l'alignement de phase qu'entre pairs de MÊME quantum, d'où le fait qu'il soit réglable.
DEFAULT_QUANTUM = 4.0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _run(quantum: float) -> None:
    import aalink  # importé ici pour que l'absence du paquet soit un message, pas une trace

    # Sans le paramètre `loop` : aalink le déprécie et le déduit de la boucle courante.
    link = aalink.Link(120)
    link.quantum = quantum
    link.enabled = True
    _log(f"link bridge up (quantum={quantum})")
    try:
        while True:
            # Lecture à la demande depuis le fil applicatif : Link n'exige un callback
            # audio que pour du placement d'événements à l'échantillon près, ce qui
            # n'est pas notre affaire ici.
            print(json.dumps({
                "tempo": round(link.tempo, 3),
                "beat": round(link.beat, 4),
                "phase": round(link.phase, 4),
                "quantum": link.quantum,
                "peers": link.num_peers,
                "playing": bool(link.playing),
            }), flush=True)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        # Quitter la session proprement plutôt que de disparaître : les autres pairs
        # voient le départ tout de suite au lieu d'attendre une expiration.
        link.enabled = False
        _log("link bridge down")


def main() -> int:
    quantum = DEFAULT_QUANTUM
    if len(sys.argv) > 1:
        try:
            quantum = float(sys.argv[1])
        except ValueError:
            _log(f"quantum illisible : {sys.argv[1]!r}")
            return 2
    try:
        asyncio.run(_run(quantum))
    except ModuleNotFoundError:
        # Le cas le plus probable, et il a une réponse en une ligne — la donner plutôt
        # que de laisser une trace Python que personne ne lira dans un journal d'add-on.
        _log("aalink absent — installez-le : python3 -m pip install aalink")
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — le pont ne doit jamais faire tomber son parent
        _log(f"pont Link interrompu : {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
