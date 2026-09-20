"""Checks on the PHONE — and in particular, is it really charging?

The one check in the program that cannot observe its subject directly: the Mac has no
way to see an iPhone's battery over the LAN. It is therefore the sharpest example of the
rule that governs all of them — measurement beats declaration, and "it went quiet" is a
state of its own rather than a reason to stay green.
"""

from __future__ import annotations

from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint, _ago


def _source_label(phone: dict | None) -> str:
    """Qui a fait le relevé — le téléphone qui publie, ou le Mac qui interroge.

    Ce n'est pas de la décoration : les deux n'ont pas les mêmes angles morts. Le
    téléphone ne parle qu'au branchement (donc peut se taire longtemps sans que rien
    n'aille mal) ; le Mac bat régulièrement mais s'arrête net si l'appairage saute.
    Savoir laquelle des deux vient de parler, c'est savoir quoi aller vérifier.
    """
    src = (phone or {}).get("source", "")
    if src.startswith("mac:"):
        return "le Mac en USB" if src.endswith("usb") else "le Mac en Wi-Fi"
    return "le téléphone"


def check_iphone_charge(cfg: dict, mode: str, acked: bool = False,
                       phone: dict | None = None) -> Result | None:
    """iPhone charging — not detectable from the Mac (the iPhone charges on a separate
    charger and talks to Bome over Wi-Fi, so it never appears here).

    Two ways to know, in order of trust: the PHONE says so (POST /api/phone, a shortcut
    on the phone — a real observation, timestamped, that stops being true on its own), or
    you tick it by hand (a declaration, which survives unplugging the phone). Neither →
    fail on stage, mere info at the desk.

    Rejected, and not to be retried: using the Bome link as a sign of life for THIS check.
    An established TCP connection proves the phone is reachable, not that it is plugged in
    — a phone on Wi-Fi and off its charger keeps that link up while it drains. It would
    have manufactured exactly the unobserved green light everything here avoids. The only
    thing that can contradict the flag is the battery going down; see the trend below.
    """
    sev = cfg["modes"][mode].get("iphone_power_severity", "warn")
    if sev == OFF:
        return None
    if phone and phone.get("fresh") and phone.get("charging") is not None:
        batt = f", {phone['battery']} %" if phone.get("battery") is not None else ""
        tr = phone.get("trend") or {}
        # La MESURE passe avant le DRAPEAU. « En charge » date du branchement et n'est
        # plus revérifié ; une batterie qui recule, elle, est en train de se produire.
        # Quand les deux se contredisent, c'est le drapeau qui a tort — câble sorti,
        # multiprise éteinte, chargeur mort. Aucun de ces trois ne se déclare.
        if tr.get("falling"):
            drop = (f"{tr['from']} → {tr['to']} % en {_ago(tr['span'])}")
            left = tr.get("hours_left")
            hmin = float(cfg.get("server", {}).get("autonomy_min_hours", 3.0))
            if left is not None and left < hmin:
                # Le vrai risque du soir : pas « est-il branché ? » mais « tiendra-t-il
                # jusqu'à la fin ? ». À ce rythme, non — et c'est une erreur, pas une
                # remarque, parce qu'il n'y a plus de rattrapage une fois sur scène.
                return Result("sys:iphonecharge", "iPhone en charge", FAIL,
                              _hint(f"il ne tiendra pas : {drop}, soit ~{left} h "
                                    f"d'autonomie (moins de {hmin:g} h)",
                                    "le brancher MAINTENANT et vérifier que le niveau "
                                    "remonte avant de partir"))
            if phone["charging"]:
                return Result("sys:iphonecharge", "iPhone en charge", sev,
                              _hint(f"branché d'après {_source_label(phone)}, mais la "
                                    f"batterie DESCEND : {drop}",
                                    "câble sorti, multiprise éteinte ou chargeur mort — "
                                    "aucun des trois ne se déclare tout seul"))
            est = f", soit ~{left} h d'autonomie" if left is not None else ""
            return Result("sys:iphonecharge", "iPhone en charge", sev,
                          _hint(f"pas en charge, il descend : {drop}{est}",
                                "le brancher sur SON chargeur (pas sur le Mac : il "
                                "puiserait dans les 90 W du dock)"))
        if phone["charging"]:
            climb = f" (+{tr['delta']} % en {_ago(tr['span'])})" if tr.get("rising") else ""
            return Result("sys:iphonecharge", "iPhone en charge", OK,
                          f"{_source_label(phone)} le dit{batt}{climb} "
                          f"— il y a {_ago(phone['age'])}")
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint(f"pas en charge d'après {_source_label(phone)}{batt}",
                            "le brancher sur SON chargeur (pas sur le Mac : il puiserait "
                            "dans les 90 W du dock). La ligne verdit seule, sans rien cocher"))
    if acked:
        return Result("sys:iphonecharge", "iPhone en charge", OK, "confirmé manuellement")
    if phone and phone.get("seen"):
        # Il a parlé, puis s'est tu. C'est un TROISIÈME état, et le seul qui désigne le
        # téléphone lui-même : « pas de nouvelles » n'est pas « pas en charge », et surtout
        # pas « on ne sait pas encore ». Un téléphone éteint, sorti du Wi-Fi ou dont
        # l'automatisation ne part plus, c'est aussi le lien Bome qui va tomber — autant
        # le dire ici plutôt que de laisser une ligne verte périmée le cacher.
        last = "en charge" if phone.get("charging") else "PAS en charge"
        batt = f", {phone['battery']} %" if phone.get("battery") is not None else ""
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint(f"aucun relevé depuis {_ago(phone.get('age'))} "
                            f"(dernier signe, par {_source_label(phone)} : {last}{batt})",
                            "téléphone éteint ou hors du Wi-Fi — le lien Bome en dépend "
                            "aussi — ou appairage perdu côté Mac ; sinon, confirmer à la main"))
    # En live c'est BLOQUANT tant que ce n'est pas coché, et la ligne doit le dire :
    # « à confirmer » tout seul se lit comme une formalité, alors que c'est la seule
    # chose qui retient le rig. Au bureau, même phrase mais sans l'avertissement — il n'y
    # bloque rien (voir iphone_power_severity par mode).
    if sev == FAIL:
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint("à confirmer — le Mac ne peut pas le détecter",
                            "brancher le téléphone sur SON chargeur (pas sur le Mac : "
                            "il puiserait dans les 90 W du dock), puis cocher. Le rig "
                            "reste bloqué tant que ce n'est pas fait"))
    return Result("sys:iphonecharge", "iPhone en charge", sev,
                  "à confirmer — non détectable depuis le Mac")
