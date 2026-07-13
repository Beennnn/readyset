"""Bridge to the Swift `audiolevel` helper — is audio SIGNAL flowing (not silence)?

On-demand only (each call runs a ~1.5s native tap), used by the soundcheck's
'measure signal' button — not a per-poll dashboard check. Software still can't
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
    if not available():
        return {"error": "helper non compilé — lance audiolevel/build.sh"}
    try:
        out = subprocess.run(
            [_BIN, str(seconds), target],
            capture_output=True, text=True, timeout=seconds + 8,
        ).stdout.strip()
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
