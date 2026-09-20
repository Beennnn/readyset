"""The checks — one FAMILY per file, and the runner that sequences them.

  apps     the machine's software: running, exactly once, unobstructed
  audio    the kernel service, the outputs, the interface, the DAW's own setting
  midi     the keyboard, the breath controller, the required ports
  network  the stage subnet, the phone link, the VPN — and mode resolution
  gear     decks, lamps, the Mac's power: hardware nothing can fix from here
  phone    is it really charging — the one subject that cannot be observed directly

THE PRINCIPLE THAT GOVERNS ALL OF THEM: no green line that has not been observed.
A check that does not know says so; it never assumes things are fine.

This module re-exports the whole surface (`checks.OK`, `checks.run_all`,
`checks.check_live_output`…) so callers never have to know which family a check
belongs to. Moving a check between families is then an internal matter.
"""

from __future__ import annotations

from ..core.result import (OK, INFO, WARN, FAIL, OFF, Result, worst,  # noqa: F401
                           _ICON, _hint, _ago)
from .apps import (_pgrep, _pgrep_cmds, _process_age, check_apps,  # noqa: F401
                   check_amphetamine, check_unexpected_apps,
                   check_accessibility, check_live_dialog)
from .audio import (audio_cache_reset, _audio_items, _audio_ready,  # noqa: F401
                    check_audio, check_audio_devices, check_live_output,
                    check_coreaudio, check_default_output)
from .midi import _midi_inputs, _present, check_midi, check_keyboard, check_breath  # noqa: F401
from .network import (_ping, resolve_mode, check_stage_network,  # noqa: F401
                      check_bome_iphone, check_vpn)
from .gear import check_streamdeck, check_lamps, check_mac_power  # noqa: F401
from .phone import check_iphone_charge  # noqa: F401
from .run import _guard, run_all  # noqa: F401
