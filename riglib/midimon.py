"""Live MIDI monitor for the soundcheck / play-test.

Opens EVERY MIDI input passively (CoreMIDI fans a source out to all clients, so
this neither steals nor injects anything) and, in a background thread, records
what arrives. The soundcheck page polls a snapshot so checkmarks light up as you
play: sustain pedal (CC64), notes, breath, and per-port activity for the
controllers. The sound itself is not tracked: software cannot hear it, and asking
for a checkbox added a step for something you already know by listening.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

import mido

from .midi_lock import MIDI_LOCK

# Virtual routing / loopback / network ports carry no physical playing and — worse —
# opening the full set of ~17 CoreMIDI clients at once wedges rtmidi. We open only
# the physical controller inputs, so the soundcheck sees real gear and stays stable.
_EXCLUDE = ("loopback", "daw2", "2daw", "mackie", "xr18", "from max", "to max",
            "mpe", "netdevices", "réseau", "reseau", "session", "network", "rtp")
_MAX_PORTS = 12   # safety cap even after filtering

# Named CCs so the ShowMIDI-style view reads "Sustain=127" not "CC64=127".
CC_NAMES = {1: "Mod", 2: "Breath", 4: "Foot", 5: "Porta", 7: "Volume", 10: "Pan",
            11: "Expr.", 64: "Sustain", 65: "Porta", 66: "Sostenuto", 67: "Soft",
            71: "Reso", 74: "Cutoff", 91: "Reverb", 93: "Chorus",
            120: "All Off", 121: "Reset", 123: "Notes Off"}


def _physical_inputs() -> list[str]:
    with MIDI_LOCK:
        names = sorted(set(mido.get_input_names()))
    keep = [n for n in names if not any(x in n.lower() for x in _EXCLUDE)]
    return keep[:_MAX_PORTS]


def sleep_stamp() -> tuple[int, int]:
    """(démarrage, dernier réveil) en secondes — l'empreinte d'une « session machine ».

    Les deux ensemble, parce qu'aucun ne suffit : `kern.waketime` vaut 0 tant que le Mac
    n'a pas dormi depuis l'allumage (c'est le cas ici, allumé le 17/08 et tenu éveillé
    par Amphetamine), donc seul il ne verrait pas un redémarrage ; `kern.boottime` ne
    bouge pas au réveil. Le couple change dans les deux cas, et c'est ce qu'on veut.

    Pourquoi ça compte : un soundcheck prouve que le clavier, la pédale et le souffle
    répondaient À CE MOMENT-LÀ. Après une mise en veille, l'USB a pu se réassocier
    autrement, un port disparaître, un appareil ne pas revenir — c'est même le mode de
    panne le plus courant du rig. Un test d'avant-veille ne prouve donc plus rien, et le
    montrer encore vert serait un mensonge rassurant.
    """
    def sec(name: str) -> int:
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"sec = (\d+)", out)
            return int(m.group(1)) if m else 0
        except Exception:
            return 0
    return sec("kern.boottime"), sec("kern.waketime")


class MidiMonitor:
    def __init__(self):
        # La chaîne du breath, décrite dans rig.toml. Posée par le serveur via
        # configure() : le moniteur est construit avant que la config soit lue.
        self.chain: dict = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset()

    def _reset(self) -> None:
        with self._lock:
            self.ports: dict[str, dict] = {}     # name -> {count, last, ts}
            self.events: list[dict] = []         # most-recent-first, capped
            self.flags = {"pedal_cc64": None, "notes": 0, "breath": None}
            # Mouvements de tête du breath controller. Le TEControl envoie le souffle sur
            # CC2/CC11 et ses capteurs d'inclinaison sur d'AUTRES CC, dont le numéro est
            # réglable dans son éditeur — impossible à coder en dur sans mentir. On les
            # apprend donc : tout CC non-souffle venant du port « breath » est un axe, sa
            # PREMIÈRE valeur est le repos, et on retient jusqu'où il s'en écarte des deux
            # côtés. Un axe = deux directions (sous le repos / au-dessus), ce qui donne
            # exactement les quatre inclinaisons demandées.
            self.motion: dict[int, dict] = {}   # cc -> {rest, min, max, count}
            self.stamp = sleep_stamp()          # la session machine de CES résultats
            # Par geste : vu à la SOURCE (le capteur bouge) et vu EN SORTIE (Bome a
            # traduit). Les deux séparés, parce que l'écart entre les deux EST le
            # diagnostic — capteur muet, ou traducteur muet.
            self.chain_seen: dict[str, dict] = {}
            self.state: dict[str, dict] = {}     # "port|ch" -> live per-channel state
            self._watched: list[str] = []        # physical ports actually opened
            self.started = time.time()

    def configure(self, cfg: dict) -> None:
        """Donne au moniteur la chaîne du breath à surveiller (voir rig.toml)."""
        self.chain = dict(cfg.get("checks", {}).get("breath_chain", {}) or {})

    def _chain_ports(self) -> list[str]:
        """Le port de SORTIE de Bome, qu'il faut écouter en plus des contrôleurs.

        Il est écarté par `_EXCLUDE` (c'est un « loopback ») et c'est normal pour la
        liste des contrôleurs physiques — personne ne joue dessus. Mais c'est là que
        Bome écrit ce qu'Ableton recevra, donc c'est le seul endroit où l'on peut
        constater que la TRADUCTION a bien eu lieu, et pas seulement que le capteur
        bouge. Un préréglage Bome désactivé se voit exactement là : le geste part, rien
        n'arrive. On l'ajoute donc nommément, sans rouvrir la vanne des 17 ports.
        """
        want = str(self.chain.get("out_port", "")).lower()
        if not want:
            return []
        with MIDI_LOCK:
            names = list(mido.get_input_names())
        return [n for n in names if isinstance(n, str) and want in n.lower()]

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Démarre l'écoute SANS effacer ce qui a déjà été constaté.

        Le `_reset()` qui était ici partait d'une époque où l'on cliquait « Démarrer » :
        un nouveau test, une page blanche. Depuis que l'écoute s'allume et s'éteint toute
        seule — elle tourne tant qu'il manque quelque chose, elle s'arrête quand tout est
        arrivé — ce même reset efface le travail déjà fait dès que le moniteur repart pour
        une raison quelconque (thread arrêté, arrêt/relance rapproché). C'est le défaut
        signalé le 2026-08-18 : « note est passé vert et a disparu, il aurait dû rester ».

        Une seule chose invalide un soundcheck, et c'est le changement de session machine
        (réveil ou redémarrage), traité dans la boucle et dans snapshot(). Un simple
        redémarrage de l'écoute n'est pas un événement matériel : il ne prouve rien de
        nouveau, il ne doit donc rien détruire.
        """
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- worker -----------------------------------------------------------
    _RESCAN_EVERY = 2.0   # secondes

    def _sync_ports(self, opened: list) -> list:
        """Ouvre ce qui est apparu, ferme ce qui a disparu. Renvoie la liste à jour.

        Sans ce rescan, le moniteur ne voyait QUE les ports présents à la seconde où on
        appuyait sur « Démarrer l'écoute ». Or l'ordre naturel sur scène est l'inverse :
        on ouvre le soundcheck, PUIS on branche le clavier — et là, pédale enfoncée,
        rien ne s'allume, sans le moindre message d'erreur. Signalé le 2026-08-18 :
        « appuie sur la pédale de sustain, et quand j'appuie j'ai pas de feedback ».
        Le clavier était bien branché ; simplement, personne ne l'écoutait.

        Symétriquement, un port débranché est refermé : garder l'objet ouvert sur un
        appareil parti fait lever `iter_pending()` en boucle.
        """
        want = set(_physical_inputs()) | set(self._chain_ports())
        have = {p.name for p in opened}
        if want == have:
            return opened
        keep = []
        with MIDI_LOCK:
            for p in opened:                  # ce qui a disparu de CoreMIDI
                if p.name in want:
                    keep.append(p)
                    continue
                try:
                    p.close()
                except Exception:
                    pass
            for name in want - have:          # ce qui vient d'arriver
                try:
                    keep.append(mido.open_input(name))
                except Exception:
                    pass  # port tenu en exclusivité ailleurs — on réessaiera au prochain tour
        with self._lock:
            self._watched = sorted(p.name for p in keep)
        return keep

    def _run(self) -> None:
        opened: list = []
        next_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now >= next_scan:
                    # Réveil ou redémarrage → les résultats d'avant ne valent plus rien.
                    # On repart d'une page blanche plutôt que de garder des cases vertes
                    # qui parlent d'un état matériel qui n'existe peut-être plus.
                    if sleep_stamp() != self.stamp:
                        self._reset()
                    opened = self._sync_ports(opened)
                    next_scan = now + self._RESCAN_EVERY
                for port in opened:           # steady-state polling: no lock needed
                    try:
                        for msg in port.iter_pending():
                            self._record(port.name, msg)
                    except Exception:
                        # Appareil arraché en pleine lecture : le prochain rescan, dans
                        # 2 s au plus, le retirera proprement. On ne tue pas le thread
                        # pour un câble débranché.
                        pass
                time.sleep(0.01)
        finally:
            with MIDI_LOCK:
                for p in opened:
                    try:
                        p.close()
                    except Exception:
                        pass

    def _match_chain(self, port: str, msg) -> None:
        """Range un message dans la chaîne du breath, à la source ou en sortie."""
        ch = self.chain
        if not ch:
            return
        src, out = str(ch.get("source_port", "")).lower(), str(ch.get("out_port", "")).lower()
        low = port.lower()
        # Bome écrit `Channel num="1"` = canal 2 en numérotation humaine ; mido rend un
        # canal indexé à 0. D'où le -1, et non un oubli.
        want_ch = int(ch.get("out_channel", 2)) - 1
        for g in ch.get("gestures", []):
            e = self.chain_seen.setdefault(g["name"], {"raw": False, "out": False})
            if src and src in low and msg.type == "control_change" and msg.control == g.get("raw_cc"):
                e["raw"] = True
            if out and out in low and getattr(msg, "channel", None) == want_ch:
                kind = g.get("out")
                if ((kind == "cc" and msg.type == "control_change" and msg.control == g.get("out_cc"))
                        or (kind == "pressure" and msg.type == "aftertouch")
                        or (kind == "pitchbend" and msg.type == "pitchwheel")):
                    e["out"] = True

    def _record(self, port: str, msg) -> None:
        desc = self._desc(msg)
        self._match_chain(port, msg)
        with self._lock:
            e = self.ports.setdefault(port, {"count": 0, "last": "", "ts": 0.0})
            e["count"] += 1
            e["last"] = desc
            e["ts"] = time.time()
            self.events.insert(0, {"port": port, "msg": desc})
            del self.events[60:]
            if msg.type == "control_change" and msg.control == 64:
                self.flags["pedal_cc64"] = {"port": port, "value": msg.value}
            if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                self.flags["notes"] += 1
            # Breath: TEControl sends CC2 (breath) / CC11 (expression); also treat
            # any traffic on a port whose name mentions "breath" as the breath ctrl.
            if (msg.type == "control_change" and msg.control in (2, 11)) or "breath" in port.lower():
                self.flags["breath"] = {"port": port}
            if (msg.type == "control_change" and msg.control not in (2, 11)
                    and "breath" in port.lower()):
                m = self.motion.setdefault(msg.control, {"rest": msg.value,
                                                         "min": msg.value,
                                                         "max": msg.value, "count": 0})
                m["count"] += 1
                m["min"] = min(m["min"], msg.value)
                m["max"] = max(m["max"], msg.value)

            # Per-channel live state for the ShowMIDI-style view.
            ch = getattr(msg, "channel", None)
            if ch is not None:
                st = self.state.setdefault(f"{port}|{ch}", {
                    "port": port, "ch": ch + 1, "notes": {}, "cc": {},
                    "pitch": None, "prog": None, "at": None, "ts": 0.0})
                st["ts"] = e["ts"]
                if msg.type == "note_on" and msg.velocity > 0:
                    st["notes"][msg.note] = msg.velocity
                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    st["notes"].pop(msg.note, None)
                elif msg.type == "control_change":
                    st["cc"][msg.control] = msg.value
                elif msg.type == "pitchwheel":
                    st["pitch"] = msg.pitch
                elif msg.type == "program_change":
                    st["prog"] = msg.program
                elif msg.type == "aftertouch":
                    st["at"] = msg.value

    @staticmethod
    def _desc(msg) -> str:
        if msg.type in ("note_on", "note_off"):
            return f"{msg.type} {msg.note} v{getattr(msg, 'velocity', 0)}"
        if msg.type == "control_change":
            return f"CC{msg.control}={msg.value}"
        if msg.type in ("pitchwheel",):
            return f"pitch {msg.pitch}"
        return msg.type

    # Écart minimal, sur 127, pour distinguer une inclinaison voulue d'un tremblement.
    _TILT_MIN = 15

    def _head(self) -> dict:
        """Les quatre inclinaisons, déduites des axes appris. À appeler sous le verrou.

        Les axes sont pris dans l'ordre où ils se sont manifestés : le premier devient
        gauche/droite, le second haut/bas. Cet ordre est une CONVENTION, pas une mesure —
        rien dans le MIDI ne dit lequel est lequel. S'ils sortent inversés à l'écran, ce
        sont les deux libellés qu'il faut échanger, pas le capteur.
        """
        axes = sorted(self.motion.items(), key=lambda kv: -kv[1]["count"])[:2]
        names = [("gauche", "droite"), ("haut", "bas")]
        out = {}
        for (cc, m), (low, high) in zip(axes, names):
            out[low] = {"cc": cc, "hit": m["rest"] - m["min"] >= self._TILT_MIN,
                        "amp": m["rest"] - m["min"]}
            out[high] = {"cc": cc, "hit": m["max"] - m["rest"] >= self._TILT_MIN,
                         "amp": m["max"] - m["rest"]}
        return out

    _STAMP_TTL = 5.0
    _stamp_checked = 0.0

    def snapshot(self) -> dict:
        # La vérification du réveil vit AUSSI ici, pas seulement dans la boucle : une fois
        # le soundcheck complet, le moniteur est arrêté (inutile de tenir les ports MIDI
        # ouverts pour rien) — et un moniteur arrêté ne surveille plus rien. C'est donc
        # l'interrogation de l'état qui doit s'en charger, sinon un réveil passerait
        # inaperçu et laisserait des cases vertes périmées. Empreinte relue au plus toutes
        # les 5 s : deux sysctl à chaque sondage (toutes les 300 ms) seraient du gâchis.
        now = time.time()
        if now - self._stamp_checked > self._STAMP_TTL:
            self._stamp_checked = now
            if sleep_stamp() != self.stamp:
                self._reset()
        with self._lock:
            ports = [{"name": n, **v} for n, v in
                     sorted(self.ports.items(), key=lambda kv: -kv[1]["ts"])]
            channels = []
            for st in sorted(self.state.values(), key=lambda s: -s["ts"]):
                channels.append({
                    "port": st["port"], "ch": st["ch"],
                    "notes": [{"n": n, "v": v} for n, v in sorted(st["notes"].items())],
                    "cc": [{"n": n, "v": v, "name": CC_NAMES.get(n, "")}
                           for n, v in sorted(st["cc"].items())],
                    "pitch": st["pitch"], "prog": st["prog"], "at": st["at"],
                })
            return {
                "running": self.is_running(),
                "watched": list(self._watched),
                "ports": ports,
                "channels": channels,
                "flags": dict(self.flags),
                "head": self._head(),
                "stamp": list(self.stamp),
                "chain": [{"name": g["name"], "icon": g.get("icon", ""),
                           **self.chain_seen.get(g["name"], {"raw": False, "out": False})}
                          for g in self.chain.get("gestures", [])],
                "events": self.events[:36],
            }
