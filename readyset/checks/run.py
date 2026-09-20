"""The runner — the ORDER the checks run in, and the net that keeps one failure local.

The order is not cosmetic. Within the audio block, coreaudiod comes first because when
it is down it is the cause of everything below it, and a reader scanning the list top to
bottom should meet the cause before the consequences.
"""

from __future__ import annotations

from ..core.result import OK, WARN, FAIL, Result, _hint
from .apps import check_apps, check_amphetamine, check_unexpected_apps, check_accessibility, check_live_dialog
from .audio import check_audio, check_audio_devices, check_live_output, check_coreaudio, check_default_output
from .gear import check_streamdeck, check_lamps, check_mac_power
from .midi import check_midi, check_keyboard, check_breath
from .network import check_stage_network, check_bome_iphone, check_vpn
from .phone import check_iphone_charge


def _guard(label: str, fn) -> list:
    """Exécute un check ; s'il lève, rend une ligne EN ERREUR au lieu de tout emporter.

    Sans ce filet, une seule exception remontait jusqu'à `do_GET`, la connexion se
    fermait vide, et le client ne voyait pas « un check en panne » mais « serveur
    injoignable ». C'est ce qui a rendu la panne du 2026-08-18 si opaque : l'agent
    tournait, le port répondait, et rien ne sortait — pendant 18 heures.

    Un rig à 24 checks sur 25 reste utilisable ; un dashboard muet, non. Et le check qui
    a lâché le DIT, avec son exception : c'est une information, pas un silence.
    """
    try:
        out = fn()
    except Exception as exc:
        return [Result(f"err:{label}", label, WARN,
                       _hint(f"ce check a échoué : {type(exc).__name__} — {exc}",
                             "les autres checks restent valables ; celui-ci est à corriger "
                             "dans le code, pas sur le rig"))]
    if out is None:
        return []
    return out if isinstance(out, list) else [out]


def run_all(cfg: dict, mode: str = "live", with_audio: bool = True,
            manual: dict | None = None, phone: dict | None = None) -> list[Result]:
    manual = manual or {}
    todo = [
        ("Applications", lambda: check_apps(cfg)),
        ("Stream Decks", lambda: check_streamdeck(cfg)),
        ("Ports MIDI", lambda: check_midi(cfg)),
        ("Clavier", lambda: check_keyboard(cfg, mode)),
        ("Breath controller", lambda: check_breath(cfg, mode)),
        ("Réseau de scène", lambda: check_stage_network(cfg, mode)),
        ("Lampes", lambda: check_lamps(cfg, mode)),
        ("Lien iPhone", lambda: check_bome_iphone(cfg)),
        ("VPN", lambda: check_vpn(cfg)),
        ("Autorisation Accessibilité", lambda: check_accessibility(cfg)),
        ("Applis en trop", lambda: check_unexpected_apps(cfg, mode)),
        ("Alimentation Mac", lambda: check_mac_power(cfg, mode)),
        ("Charge iPhone", lambda: check_iphone_charge(
            cfg, mode, acked=bool(manual.get("iphone_charge")), phone=phone)),
        ("Amphetamine", lambda: check_amphetamine(cfg, mode)),
    ]
    if with_audio:
        todo += [
            # En tête du bloc audio : quand il est rouge, il EST la cause des suivants.
            ("Service audio", lambda: check_coreaudio(cfg)),
            ("Sortie par défaut", lambda: check_default_output(cfg)),
            ("Interface audio", lambda: check_audio(cfg, mode)),
            ("Périphériques audio", lambda: check_audio_devices(cfg, mode)),
            ("Sortie d'Ableton", lambda: check_live_output(cfg, mode)),
            ("Fenêtre d'Ableton", lambda: check_live_dialog(cfg)),
        ]
    results: list[Result] = []
    for label, fn in todo:
        results += _guard(label, fn)
    # Les checks réglés sur "off" rendent None : ils disparaissent ici, une bonne fois,
    # plutôt que chaque appelant ait à s'en soucier.
    return [r for r in results if r is not None]
