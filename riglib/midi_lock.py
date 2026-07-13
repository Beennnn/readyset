"""One process-wide lock around CoreMIDI client open/close/enumerate.

python-rtmidi on macOS wedges when many MidiIn clients are opened concurrently
from different threads (the soundcheck monitor thread + the dashboard's check
threads). Serialising the create/destroy/enumerate operations through this lock
keeps them from racing. The monitor's steady-state polling (iter_pending on
already-open ports) does NOT take the lock — only the risky open/close/list do.
"""

import threading

MIDI_LOCK = threading.RLock()
