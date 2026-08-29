"""Alert backends — pluggable, chosen per-config so you pick what fits the venue.

Why not trevligaspel for the Stream Deck alert? That plugin is button → MIDI
(outgoing) — it cannot repaint a key from an external script. To make a button
react, the honest path is MIDI *feedback*: this backend emits on a virtual port;
a Stream Deck key configured with MIDI feedback (most MIDI SD plugins, incl.
trevligaspel, support incoming-MIDI state) then reacts. The emit side lives here;
the one-time button-side mapping is yours to set (see rig.example.toml).

  macos       — osascript banner + sound. Zero setup, but the laptop is closed on stage.
  push        — HTTP POST to ntfy.sh (stdlib, no install). Buzzes your phone anywhere.
  midi        — MIDI CCs to a virtual port → a feedback-configured key shows the
                number of failing checks AND which families they belong to, and
                goes to its alert state when the total is not zero. *Counts*,
                not events: see Alerter.gauge().
"""

from __future__ import annotations

import ssl
import subprocess
import threading
import time
import urllib.request

import mido


# One letter per family of checks, and the order fixes the CC numbers: family i
# is published on CC_FAMILY_BASE + i. It is therefore an ordering that must not be
# re-sorted — a letter that changes CC between two versions would silently make a
# key display something else, which is worse than displaying nothing.
#
# The families are not invented here: they are the prefix that check keys already
# carry ("app:Ableton", "midi:P-Series", "sys:macpower"). Grouping by prefix means
# a new check joins its family on its own, with no table to keep in step.
FAMILIES = [
    ("app",   "A", "une application du rig ne tourne pas"),
    ("midi",  "M", "un port MIDI manque"),
    ("kbd",   "K", "clavier ou contrôleur de souffle"),
    ("audio", "S", "son : interface, périphérique, sortie d'Ableton"),
    ("net",   "N", "réseau de scène"),
    ("usb",   "D", "Stream Deck"),
    ("lamp",  "L", "lampes de scène"),
    ("sys",   "Y", "système : alimentation, veille, accessibilité, sortie son du Mac"),
    ("xapp",  "X", "une application de trop tourne"),
    ("sc",    "B", "balance : une étape du soundcheck n'est pas passée"),
]
CC_FAMILY_BASE = 112     # CC 111 porte le total, 112..121 les dix familles

# La PREMIÈRE famille tombée dans l'ordre ci-dessus, en un seul nombre : 0 si tout va
# bien, sinon son rang + 1. Elle existe parce que la surface ne sait pas composer une
# chaîne : dix conditions concaténées figent le plugin trevligaspel dans une boucle
# infinie — mesuré le 2026-08-29, 95 % de CPU, un fil CoreMIDI bloqué dans
# midiInputCallback, et le même script retiré fait retomber le plugin à 0,4 %. Un
# indice, lui, se lit d'un seul appel de fonction sur la touche.
#
# Les comptes par famille (112..121) restent émis : ils ne coûtent rien et une surface
# capable de les lire aura tout. Celle-ci ne l'est pas.
CC_FIRST = 110


class Alerter:
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
    def gauge(self, failing_keys: list[str]) -> None:
        """Publish WHAT is failing right now: a total, and a count per family.

        Replaces the old note-on/note-off alert (a single lamp). A lamp could not
        say whether one check or five were down, nor which; and an unlit lamp was
        ambiguous — never lit, recovered, and note-off missed all looked alike.
        Here 0 says all good and nothing else, and the family counts let a key
        spell out the letters of what broke.

        Resent on every change and every heartbeat, deliberately: MIDI has no
        acknowledgement, so a surface that boots after the monitor — or misses a
        message — would otherwise keep showing a stale number forever. Sending
        the truth repeatedly costs a handful of 3-byte messages and makes the
        display self-healing.
        """
        if "midi" not in self.active:
            return
        mc = self.cfg["alerts"]["midi"]
        base = int(mc.get("cc", 111))
        ch = int(mc.get("channel", 15)) - 1      # mido is 0-based

        per = {}
        for k in failing_keys:
            per[k.split(":", 1)[0]] = per.get(k.split(":", 1)[0], 0) + 1
        # A CC carries 0..127 and nothing wider; a rig with 128 failing checks has
        # problems this key will not help with.
        rangs = [i for i, (prefix, _l, _w) in enumerate(FAMILIES) if per.get(prefix)]
        msgs = [(CC_FIRST, rangs[0] + 1 if rangs else 0),
                (base, min(len(failing_keys), 127))]
        msgs += [(CC_FAMILY_BASE + i, min(per.get(prefix, 0), 127))
                 for i, (prefix, _letter, _why) in enumerate(FAMILIES)]

        unknown = set(per) - {f[0] for f in FAMILIES}
        if unknown:   # a new prefix appeared: it still counts in the total, but has no letter
            self.log(f"  (jauge : famille sans lettre — {', '.join(sorted(unknown))})")

        if self.dry_run:
            shown = " ".join(f"{cc}={v}" for cc, v in msgs if v)
            self.log(f"  [dry-run] jauge canal {ch + 1} → {shown or f'{base}=0'} (rien envoyé)")
            return
        target = mc["port"]
        match = next((p for p in mido.get_output_names() if target.lower() in p.lower()), None)
        if match is None:
            self.log(f"  (jauge : port de sortie « {target} » absent)")
            return
        try:
            with mido.open_output(match) as port:
                for cc, value in msgs:
                    port.send(mido.Message("control_change", channel=ch, control=cc, value=value))
        except Exception as exc:   # an alert must never take the monitor down
            self.log(f"  (jauge a échoué: {exc})")


# La touche du rig sait dire « montre-moi » : appuyer dessus envoie ce CC sur le même
# port d'alerte, et le tableau de bord s'ouvre. Le retour, dans l'autre sens, sur le
# même câble — une surface de contrôle qui affiche un verdict sans pouvoir en montrer le
# détail oblige à retourner au clavier, ce qui est précisément le geste qu'elle évite.
#
# CC 100 et non un des 111..121 : ceux-là PORTENT l'état, et une valeur d'affichage qui
# déclencherait aussi une action serait un piège. 100 est en dehors du bloc, et en dehors
# des 120..127 que le MIDI réserve aux messages de mode.
CC_OPEN = 100


def listen_for_open(cfg: dict, on_trigger, log=print) -> None:
    """Ouvre le port d'alerte en écoute et appelle `on_trigger` sur CC_OPEN.

    Le port peut ne pas exister au démarrage (le pilote IAC arrive parfois après nous) et
    peut disparaître en cours de route : la boucle réessaie plutôt que d'abandonner une
    fois pour toutes, sinon un ordre de branchement malheureux suffirait à retirer la
    fonction jusqu'au prochain redémarrage.
    """
    mc = cfg["alerts"]["midi"]
    cible, canal = mc["port"], int(mc.get("channel", 15)) - 1

    def boucle() -> None:
        while True:
            nom = next((p for p in mido.get_input_names() if cible.lower() in p.lower()), None)
            if nom is None:
                time.sleep(5)
                continue
            try:
                with mido.open_input(nom) as port:
                    log(f"  (retour : à l'écoute de « {nom} » CC {CC_OPEN} canal {canal + 1})")
                    for msg in port:
                        if (msg.type == "control_change" and msg.channel == canal
                                and msg.control == CC_OPEN and msg.value > 0):
                            on_trigger()
            except Exception as exc:      # le port a disparu, ou le backend a hoqueté
                log(f"  (retour : écoute interrompue — {exc})")
                time.sleep(5)

    threading.Thread(target=boucle, daemon=True).start()
