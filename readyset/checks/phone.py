"""Checks on the PHONE — and in particular, is it really charging?

The one check in the program that cannot observe its subject directly: the Mac has no
way to see an iPhone's battery over the LAN. It is therefore the sharpest example of the
rule that governs all of them — measurement beats declaration, and "it went quiet" is a
state of its own rather than a reason to stay green.
"""

from __future__ import annotations

from ..core.result import OK, INFO, WARN, FAIL, OFF, Result, _hint, _ago


def _source_label(phone: dict | None) -> str:
    """Who took the reading — the phone publishing, or the Mac polling.

    This is not decoration: the two do not have the same blind spots. The phone only
    speaks on plug-in (so it can stay quiet a long time without anything being wrong);
    the Mac beats regularly but stops dead if the pairing drops. Knowing which of the
    two has just spoken is knowing what to go and check.
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
        # The MEASUREMENT comes before the FLAG. « Charging » dates from the plug-in and
        # is never rechecked; a battery going backwards, on the other hand, is happening
        # right now. When the two contradict each other, it is the flag that is wrong —
        # cable pulled out, power strip off, dead charger. None of those three announces
        # itself.
        if tr.get("falling"):
            drop = (f"{tr['from']} → {tr['to']} % en {_ago(tr['span'])}")
            left = tr.get("hours_left")
            hmin = float(cfg.get("server", {}).get("autonomy_min_hours", 3.0))
            if left is not None and left < hmin:
                # The real risk of the evening: not « is it plugged in? » but « will it
                # last to the end? ». At this rate, no — and that is an error, not a
                # remark, because there is no catching up once you are on stage.
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
        # It spoke, then went quiet. This is a THIRD state, and the only one that points
        # at the phone itself: « no news » is not « not charging », and above all not
        # « we do not know yet ». A phone that is off, off the Wi-Fi or whose automation
        # no longer fires also means the Bome link is about to drop — better to say so
        # here than to let a stale green line hide it.
        last = "en charge" if phone.get("charging") else "PAS en charge"
        batt = f", {phone['battery']} %" if phone.get("battery") is not None else ""
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint(f"aucun relevé depuis {_ago(phone.get('age'))} "
                            f"(dernier signe, par {_source_label(phone)} : {last}{batt})",
                            "téléphone éteint ou hors du Wi-Fi — le lien Bome en dépend "
                            "aussi — ou appairage perdu côté Mac ; sinon, confirmer à la main"))
    # In live it is BLOCKING until it is ticked, and the line has to say so: « à
    # confirmer » on its own reads like a formality, whereas it is the only thing holding
    # the rig back. At the desk, same sentence but without the warning — it blocks
    # nothing there (see iphone_power_severity per mode).
    if sev == FAIL:
        return Result("sys:iphonecharge", "iPhone en charge", sev,
                      _hint("à confirmer — le Mac ne peut pas le détecter",
                            "brancher le téléphone sur SON chargeur (pas sur le Mac : "
                            "il puiserait dans les 90 W du dock), puis cocher. Le rig "
                            "reste bloqué tant que ce n'est pas fait"))
    return Result("sys:iphonecharge", "iPhone en charge", sev,
                  "à confirmer — non détectable depuis le Mac")
