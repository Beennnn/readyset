"""Bridge to the Swift `audiolevel` helper — is audio SIGNAL flowing (not silence)?

On-demand only (each call runs a ~1.5s native tap), behind the soundcheck's
« Mesurer » button — never on the polling path. Software still can't
*hear* content; this only answers "is there a non-silent signal on Live's output".
"""

from __future__ import annotations

import os
import subprocess

_BIN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "audiolevel", "audiolevel")
THRESHOLD = 0.001   # silence measured 0.000000; real audio ~0.08 → 0.001 cleanly separates


def available() -> bool:
    return os.path.exists(_BIN)


def measure(seconds: float = 1.5, target: str = "ableton") -> dict:
    """Measure a window of signal. TO BE CALLED ON DEMAND ONLY, never in a loop.

    Two traps documented in audiolevel/README.md, and it is because of them that this
    function does not look like an ordinary `subprocess.run`:

    1. The audio permission (TCC) is requested from the CALLING PROCESS. Launched from a
       terminal, the helper gets the tap; launched by the launchd service, it may stay
       stuck on a permission that a background process does not know how to claim. We
       then return a message saying what to do, instead of freezing the dashboard.
    2. **Killing a stuck measurement jams CoreAudio**: a SIGKILL skips the cleanup
       (`AudioHardwareDestroyProcessTap`, destruction of the aggregate device) and leaves
       orphaned taps behind that block the following ones — hence, potentially, disturbed
       sound in the middle of a gig. Hence the TWO-step stop: SIGTERM, a grace period so
       the cleanup can run, and the SIGKILL only as a last resort.
    """
    if not available():
        return {"error": "helper non compilé — lance audiolevel/build.sh"}
    proc = subprocess.Popen([_BIN, str(seconds), target],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out = (proc.communicate(timeout=seconds + 8)[0] or "").strip()
    except subprocess.TimeoutExpired:
        proc.terminate()                      # lets the CoreAudio cleanup happen
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        return {"error": "mesure bloquée — autorisation audio refusée au service. "
                         "Lance une fois `audiolevel/audiolevel 1.5 ableton` depuis le "
                         "Terminal pour accorder l'accès, puis réessaie ici."}
    except Exception as exc:
        return {"error": str(exc)}
    parts = out.split()
    kv = {parts[i]: parts[i + 1] for i in range(0, len(parts) - 1, 2)}
    try:
        rms = float(kv.get("RMS", 0))
    except ValueError:
        rms = 0.0
    return {
        "rms": rms,
        "target": kv.get("TARGET", ""),
        "frames": int(kv.get("FRAMES", 0) or 0),
        "signal": rms > THRESHOLD,
    }
