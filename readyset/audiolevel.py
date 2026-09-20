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
    """Mesure une fenêtre de signal. À N'APPELER QUE SUR DEMANDE, jamais en boucle.

    Deux pièges documentés dans audiolevel/README.md, et c'est pour eux que cette
    fonction ne ressemble pas à un `subprocess.run` ordinaire :

    1. L'autorisation audio (TCC) se demande au PROCESSUS APPELANT. Lancé depuis un
       terminal, l'aide obtient le tap ; lancé par le service launchd, elle peut rester
       bloquée sur une autorisation qu'un processus d'arrière-plan ne sait pas réclamer.
       On rend alors un message qui dit quoi faire, au lieu de figer le dashboard.
    2. **Tuer une mesure bloquée coince CoreAudio** : un SIGKILL saute le nettoyage
       (`AudioHardwareDestroyProcessTap`, destruction du périphérique agrégé) et laisse
       des taps orphelins qui empêchent les suivants — donc, potentiellement, du son
       perturbé en plein concert. D'où l'arrêt en DEUX temps : SIGTERM, un délai de
       grâce pour que le nettoyage tourne, et le SIGKILL seulement en dernier recours.
    """
    if not available():
        return {"error": "helper non compilé — lance audiolevel/build.sh"}
    proc = subprocess.Popen([_BIN, str(seconds), target],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out = (proc.communicate(timeout=seconds + 8)[0] or "").strip()
    except subprocess.TimeoutExpired:
        proc.terminate()                      # laisse le nettoyage CoreAudio se faire
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
