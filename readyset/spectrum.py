"""Audio spectrum of the stage stream, served by the dashboard (`/api/audio/spectrum`).

What it gives: RMS, peak, and ~16 logarithmic bands — enough to draw an analyser on a
Stream Deck key, not enough to do acoustic measurement.

FOUR CHOICES THAT HAVE A REASON, and that must not be "simplified" later on.

1. **The source is resolved by its NAME, never by its index.** avfoundation indices move
   from one reboot to the next: "Wave Link Stream" was `:0` on 2026-08-19 and nothing
   guarantees it stays there. A frozen index would end up listening to the laptop's
   microphone while believing it is listening to the mix.

2. **A SINGLE, shared capture.** One ffmpeg per HTTP call, with several keys polling in a
   loop, would open and close the device non-stop. A single stream runs, everybody reads
   the same sliding buffer.

3. **It starts on demand and stops on its own.** Holding an audio device open permanently
   on a stage machine is exactly what we do not want: it can get in the way of a reopen
   elsewhere, and it consumes for nothing when nobody is watching. First call = start, no
   more calls for `idle_stop` = shutdown.

4. **Goertzel, no FFT.** For 16 bands it is O(16·N), a few lines, and above all NO
   dependency: `numpy` is not installed on this machine (verified on 2026-08-19), and
   adding a heavyweight dependency to a stage tool in order to draw an icon would be a
   bad bargain.

And one guarantee: **nothing here must be able to bring the dashboard down**. Any failure
— ffmpeg missing, device gone, permission denied — is turned into an "unavailable" state
with its reason. The spectrum is a comfort; the rig must stay usable without it.
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

# Resolved explicitly: under launchd the PATH is limited to /usr/bin:/bin:/usr/sbin:/sbin,
# where Homebrew is not. Same trap as for live-output and sd-power.
_FFMPEG_CANDIDATES = ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")

_RATE = 24000          # Nyquist at 12 kHz: the brightness is there, without paying for 96 kHz of maths
_WINDOW = 2048         # ~85 ms: long enough for 40 Hz, short enough to follow the playing
_BANDS = 16            # what a 96 px icon can show without lying
_F_MIN, _F_MAX = 40.0, 12000.0


def _ffmpeg() -> str | None:
    for c in _FFMPEG_CANDIDATES:
        p = shutil.which(c) if "/" not in c else (c if shutil.which(c) else None)
        if p:
            return p
    return None


def devices() -> list[tuple[int, str]]:
    """[(index, name)] of the avfoundation audio inputs, as ffmpeg sees them."""
    exe = _ffmpeg()
    if not exe:
        return []
    try:
        # -list_devices ALWAYS exits with an error (it has no input to open): that is
        # normal, we read stderr and ignore the return code.
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


_CYCLES = 8        # number of periods observed per band — sets the width of the filter


def _window_for(freq: float) -> int:
    """How many samples to look at for this band — AT CONSTANT QUALITY.

    This is the fix for the first attempt. With one single window for every band, the
    filter has the same width in Hz everywhere: narrow, it lands right in the low end
    (where the log bands are tight) and misses almost everything in the high end (where
    one band covers thousands of hertz). Measured on 2026-08-19 on pure sines of amplitude
    0.5: 0.087 returned at 110 Hz against 0.002 at 5 kHz — an analyser whose highs stay
    black.

    By observing a FIXED number of periods, the width of the filter grows with the
    frequency, just like the bands themselves. That is the constant-Q principle. Side
    benefit: the high bands cost far less computation than the low ones.
    """
    return max(256, min(_WINDOW, int(_RATE * _CYCLES / max(freq, 1.0))))


def _goertzel(buf: list[float], freq: float) -> float:
    """Amplitude of ONE frequency, without transforming the whole spectrum.

    The window is taken at the END of the buffer: it is the sound of now that we display.
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
    """The value of a band: three probes, we keep the strongest.

    A single probe at the centre assumes the sound falls exactly in the middle of the
    band. An A at 440 Hz in a band centred on 573 returned 0.041 where the same A, centred,
    returns three times more — the display would have depended on the tuning of the song,
    not on the level. Three points (the centre and the two geometric edges) are enough to
    remove that dip, and the cost stays under the millisecond thanks to the
    constant-quality windows.
    """
    # Five points rather than three: with three, a sound falling between two probes stays
    # under-estimated — measured at 5 kHz, which landed between 4,636 and 5,609 Hz and
    # returned only 0.03 when its neighbours returned 0.3. The gap between neighbouring
    # probes is then at most a quarter of a band, which a constant-Q filter covers without
    # a dip.
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
                # The shutdown is decided HERE, in the reading thread: a separate timer
                # would be one more object to keep alive and to stop cleanly.
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
    """The current spectral state. Never raises: at worst, `available` is false."""
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
        # The first call arrives before the buffer is full: we say so rather than return
        # a spectrum computed on emptiness, which would read as a silence.
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
