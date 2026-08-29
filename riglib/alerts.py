"""Alert backends — pluggable, chosen per-config so you pick what fits the venue.

Why not trevligaspel for the Stream Deck alert? That plugin is button → MIDI
(outgoing) — it cannot repaint a key from an external script. To make a button
react, the honest path is MIDI *feedback*: this backend emits on a virtual port;
a Stream Deck key configured with MIDI feedback (most MIDI SD plugins, incl.
trevligaspel, support incoming-MIDI state) then reacts. The emit side lives here;
the one-time button-side mapping is yours to set (see rig.example.toml).

  macos       — osascript banner + sound. Zero setup, but the laptop is closed on stage.
  push        — HTTP POST to ntfy.sh (stdlib, no install). Buzzes your phone anywhere.
  midi        — MIDI CC to a virtual port → a feedback-configured key shows the
                number of failing checks, and goes to its alert state when it is
                not zero. A *count*, not an event: see Alerter.gauge().
"""

from __future__ import annotations

import ssl
import subprocess
import urllib.request

import mido


class Alerter:
    # Backends that speak in *counts* rather than events. Feeding them one
    # message per transition would make the key flicker and lose the total,
    # so notify() skips them; they publish through gauge() instead.
    GAUGE_ONLY = ("midi",)

    def __init__(self, cfg: dict, active: list[str], log=print, dry_run: bool = False):
        self.cfg = cfg
        self.active = active
        self.log = log
        self.dry_run = dry_run

    def notify(self, title: str, message: str, level: str = "fail") -> None:
        if self.dry_run:
            self.log(f"  [dry-run] alerte {level} → {', '.join(self.active)} : "
                     f"« {title} — {message} » (rien envoyé)")
            return
        for backend in self.active:
            if backend in self.GAUGE_ONLY:
                continue
            fn = getattr(self, f"_{backend}", None)
            if fn is None:
                self.log(f"  (alerte inconnue: {backend})")
                continue
            try:
                fn(title, message, level)
            except Exception as exc:  # never let an alert crash the monitor
                self.log(f"  (alerte {backend} a échoué: {exc})")

    # -- backends ---------------------------------------------------------
    def _macos(self, title: str, message: str, level: str) -> None:
        sound = "Basso" if level == "fail" else "Glass" if level == "warn" else "Ping"
        safe_t = title.replace('"', "'")
        safe_m = message.replace('"', "'")
        script = (
            f'display notification "{safe_m}" with title "{safe_t}" sound name "{sound}"'
        )
        subprocess.run(["osascript", "-e", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _hdr(s: str) -> str:
        # HTTP header values are latin-1 only; drop emoji, keep accents, fix arrows.
        s = s.replace("—", "-").replace("→", "->")
        return s.encode("latin-1", "ignore").decode("latin-1").strip() or "Rig"

    def _push(self, title: str, message: str, level: str) -> None:
        pc = self.cfg["alerts"]["push"]
        topic = pc.get("topic", "").strip()
        if not topic:
            self.log("  (push ignoré : aucun [alerts.push].topic configuré)")
            return
        url = f"{pc['server'].rstrip('/')}/{topic}"
        prio = {"fail": "urgent", "warn": "high", "ok": "default"}.get(level, "high")
        tag = {"fail": "rotating_light", "warn": "warning", "ok": "white_check_mark"}[level]
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), method="POST",
            headers={"Title": self._hdr(title), "Priority": prio, "Tags": tag},
        )
        # This LAN does SSL interception (self-signed cert on all HTTPS). insecure=true
        # skips cert verification so the alert still gets through the proxy; the payload
        # is only rig status, so the trade-off is acceptable. Off by default.
        ctx = None
        if pc.get("insecure"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=5, context=ctx).read()

    # -- gauge (counts, not events) ---------------------------------------
    def gauge(self, failing: int) -> None:
        """Publish how many checks are failing RIGHT NOW, as a MIDI CC value.

        Replaces the old note-on/note-off alert (a single lamp): a count says
        *how bad* it is, and 0 is unambiguously "all good" — a lamp that never
        got its note-off looks identical to one that was never lit.

        Resent on every change and on every heartbeat, deliberately: MIDI has no
        acknowledgement, so a surface that boots after the monitor — or misses a
        message — would otherwise keep showing a stale number forever. Sending
        the truth repeatedly costs one 3-byte message and makes the display
        self-healing.
        """
        if "midi" not in self.active:
            return
        mc = self.cfg["alerts"]["midi"]
        cc = int(mc.get("cc", 111))
        ch = int(mc.get("channel", 15)) - 1      # mido is 0-based
        value = max(0, min(int(failing), 127))   # a CC carries 0..127, nothing else
        if self.dry_run:
            self.log(f"  [dry-run] jauge → CC {cc} canal {ch + 1} = {value} (rien envoyé)")
            return
        target = mc["port"]
        match = next((p for p in mido.get_output_names() if target.lower() in p.lower()), None)
        if match is None:
            self.log(f"  (jauge : port de sortie « {target} » absent)")
            return
        try:
            with mido.open_output(match) as port:
                port.send(mido.Message("control_change", channel=ch, control=cc, value=value))
        except Exception as exc:   # an alert must never take the monitor down
            self.log(f"  (jauge a échoué: {exc})")
