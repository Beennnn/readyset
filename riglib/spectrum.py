"""Spectre audio du flux de scène, servi par le dashboard (`/api/audio/spectrum`).

Ce que ça donne : RMS, crête, et ~16 bandes logarithmiques — de quoi dessiner un
analyseur sur une touche de Stream Deck, pas de quoi faire de la mesure acoustique.

QUATRE CHOIX QUI ONT UNE RAISON, et qui ne doivent pas être « simplifiés » par la suite.

1. **La source se résout par son NOM, jamais par son index.** Les index avfoundation
   bougent d'un redémarrage à l'autre : « Wave Link Stream » était `:0` le 2026-08-19 et
   rien ne garantit qu'il le reste. Un index figé finirait par écouter le micro du
   MacBook en croyant écouter le mix.

2. **Capture UNIQUE et partagée.** Un ffmpeg par appel HTTP, avec plusieurs touches qui
   interrogent en boucle, ouvrirait et fermerait le périphérique sans arrêt. Un seul flux
   tourne, tout le monde lit le même tampon glissant.

3. **Elle démarre à la demande et s'arrête toute seule.** Tenir un périphérique audio
   ouvert en permanence sur une machine de scène est exactement ce qu'on ne veut pas :
   ça peut gêner une réouverture ailleurs, et ça consomme pour rien quand personne ne
   regarde. Premier appel = démarrage, plus d'appel pendant `idle_stop` = extinction.

4. **Goertzel, pas de FFT.** Pour 16 bandes c'est O(16·N), quelques lignes, et surtout
   AUCUNE dépendance : `numpy` n'est pas installé sur cette machine (vérifié le
   2026-08-19), et ajouter une dépendance lourde à un outil de scène pour dessiner une
   icône serait un mauvais marché.

Et une garantie : **rien ici ne doit pouvoir faire tomber le dashboard**. Toute panne —
ffmpeg absent, périphérique disparu, permission refusée — se traduit par un état
« indisponible » avec sa raison. Le spectre est un confort ; le rig doit rester utilisable
sans lui.
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import threading
import time
from collections import deque

# Résolu explicitement : sous launchd le PATH se limite à /usr/bin:/bin:/usr/sbin:/sbin,
# où Homebrew n'est pas. Même piège que pour live-output et sd-power.
_FFMPEG_CANDIDATES = ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")

_RATE = 24000          # Nyquist à 12 kHz : la brillance est là, sans payer 96 kHz de calcul
_WINDOW = 2048         # ~85 ms : assez long pour 40 Hz, assez court pour suivre le jeu
_BANDS = 16            # ce qu'une icône de 96 px peut montrer sans mentir
_F_MIN, _F_MAX = 40.0, 12000.0


def _ffmpeg() -> str | None:
    for c in _FFMPEG_CANDIDATES:
        p = shutil.which(c) if "/" not in c else (c if shutil.which(c) else None)
        if p:
            return p
    return None


def devices() -> list[tuple[int, str]]:
    """[(index, nom)] des entrées audio avfoundation, telles que ffmpeg les voit."""
    exe = _ffmpeg()
    if not exe:
        return []
    try:
        # -list_devices sort TOUJOURS en erreur (il n'a pas d'entrée à ouvrir) : c'est
        # normal, on lit stderr et on ignore le code de retour.
        out = subprocess.run([exe, "-hide_banner", "-f", "avfoundation",
                              "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True, timeout=15).stderr
    except Exception:
        return []
    found, audio = [], False
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            audio = True
            continue
        if not audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m:
            found.append((int(m.group(1)), m.group(2)))
    return found


_CYCLES = 8        # nombre de périodes observées par bande — fixe la largeur du filtre


def _window_for(freq: float) -> int:
    """Combien d'échantillons regarder pour cette bande — À QUALITÉ CONSTANTE.

    C'est le correctif du premier essai. Avec une fenêtre unique pour toutes les bandes,
    le filtre a la même largeur en Hz partout : étroit, il tombe juste dans le grave (où
    les bandes log sont serrées) et rate presque tout dans l'aigu (où une bande couvre des
    milliers de hertz). Mesuré le 2026-08-19 sur des sinus purs d'amplitude 0,5 : 0,087
    rendu à 110 Hz contre 0,002 à 5 kHz — un analyseur dont les aigus restent noirs.

    En observant un nombre FIXE de périodes, la largeur du filtre grandit avec la
    fréquence, comme les bandes elles-mêmes. C'est le principe du constant-Q. Bénéfice
    secondaire : les bandes hautes coûtent beaucoup moins de calcul que les basses.
    """
    return max(256, min(_WINDOW, int(_RATE * _CYCLES / max(freq, 1.0))))


def _goertzel(buf: list[float], freq: float) -> float:
    """Amplitude d'UNE fréquence, sans transformer tout le spectre.

    La fenêtre est prise à la FIN du tampon : c'est le son de maintenant qu'on affiche.
    """
    n = min(len(buf), _window_for(freq))
    if n < 32:
        return 0.0
    seg = buf[-n:]
    k = max(1, int(0.5 + n * freq / _RATE))
    w = 2.0 * math.pi * k / n
    cw, sw = math.cos(w), math.sin(w)
    coeff = 2.0 * cw
    s1 = s2 = 0.0
    for x in seg:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return math.hypot(s1 - s2 * cw, s2 * sw) / (n / 2.0)


def _band(buf: list[float], center: float, ratio: float) -> float:
    """La valeur d'une bande : trois sondes, on garde la plus forte.

    Une sonde unique au centre suppose que le son tombe pile au milieu de la bande. Un
    la à 440 Hz dans une bande centrée sur 573 rendait 0,041 là où le même la centré rend
    trois fois plus — l'affichage aurait dépendu de l'accord du morceau, pas du niveau.
    Trois points (le centre et les deux bords géométriques) suffisent à supprimer ce
    creux, et le coût reste sous la milliseconde grâce aux fenêtres à qualité constante.
    """
    # Cinq points plutôt que trois : avec trois, un son tombant entre deux sondes reste
    # sous-évalué — mesuré à 5 kHz, qui atterrissait entre 4 636 et 5 609 Hz et ne rendait
    # que 0,03 quand ses voisins rendaient 0,3. L'écart entre sondes voisines vaut alors
    # au plus un quart de bande, ce qu'un filtre à Q constant couvre sans creux.
    return max(_goertzel(buf, center * ratio ** e) for e in (-0.5, -0.25, 0.0, 0.25, 0.5))


class _Capture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buf: deque[float] = deque(maxlen=_WINDOW)
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.source = ""
        self.error = ""
        self.last_read = 0.0

    def _open(self, want: str) -> bool:
        exe = _ffmpeg()
        if not exe:
            self.error = "ffmpeg introuvable"
            return False
        idx = next((i for i, n in devices() if want.lower() in n.lower()), None)
        if idx is None:
            self.error = f"source « {want} » absente des entrées audio"
            return False
        try:
            self.proc = subprocess.Popen(
                [exe, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-f", "avfoundation", "-i", f":{idx}",
                 "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.error = f"capture impossible : {exc}"
            return False
        self.source, self.error = want, ""
        return True

    def _pump(self, idle_stop: float) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                chunk = self.proc.stdout.read(1024)
                if not chunk:
                    break
                vals = struct.unpack(f"<{len(chunk)//2}h", chunk[:len(chunk)//2*2])
                with self.lock:
                    self.buf.extend(v / 32768.0 for v in vals)
                # L'extinction est décidée ICI, dans le fil qui lit : un minuteur séparé
                # serait un objet de plus à tenir en vie et à arrêter proprement.
                if time.time() - self.last_read > idle_stop:
                    break
        except Exception as exc:
            self.error = f"lecture interrompue : {exc}"
        finally:
            self.stop()

    def start(self, want: str, idle_stop: float) -> bool:
        if self.thread and self.thread.is_alive():
            return True
        if not self._open(want):
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
            self.buf.clear()


_CAP = _Capture()


def snapshot(cfg: dict) -> dict:
    """L'état spectral courant. Ne lève jamais : au pire, `available` est faux."""
    srv = cfg.get("server", {})
    want = str(srv.get("spectrum_source", "Wave Link Stream"))
    idle_stop = float(srv.get("spectrum_idle_stop_seconds", 20))
    bands_n = int(srv.get("spectrum_bands", _BANDS))

    _CAP.last_read = time.time()
    if not _CAP.start(want, idle_stop):
        return {"available": False, "reason": _CAP.error, "source": want}

    with _CAP.lock:
        buf = list(_CAP.buf)
    if len(buf) < _WINDOW:
        # Le premier appel arrive avant que le tampon soit plein : on le dit plutôt que
        # de rendre un spectre calculé sur du vide, qui se lirait comme un silence.
        return {"available": False, "reason": "capture en cours de démarrage",
                "source": want, "filled": len(buf)}

    rms = math.sqrt(sum(x * x for x in buf) / len(buf))
    peak = max(abs(x) for x in buf)
    ratio = (_F_MAX / _F_MIN) ** (1.0 / max(1, bands_n - 1))
    freqs = [_F_MIN * ratio ** i for i in range(bands_n)]
    bands = [round(min(1.0, _band(buf, f, ratio) * 4.0), 4) for f in freqs]
    return {"available": True, "source": _CAP.source, "sample_rate": _RATE,
            "rms": round(rms, 5), "peak": round(peak, 5),
            "bands": bands, "freqs": [round(f) for f in freqs]}
