"""Erreurs LIÉES — la panne qui en explique d'autres.

Le tableau dit ce qui ne va pas, appareil par appareil. Il ne dit pas ce qui tombe
ENSEMBLE — et sur scène, c'est presque toujours la vraie question. Le Stream Deck Plus
alimente le XL, le clavier et le breath : quand son câble saute, quatre lignes passent
au rouge dans la même seconde et une seule est la panne. Les trois autres ne sont pas
des problèmes à régler, ce sont des SYMPTÔMES. Les lire comme quatre pannes distinctes,
c'est chercher quatre gestes là où il n'y en a qu'un — au moment précis où l'on n'a le
temps que d'un seul.

D'où ce module. Il ne teste RIEN : il relie. À partir d'une topologie déclarée dans
rig.toml (`[[depends]]`), il marque chaque panne comme cause ou comme conséquence, pour
que les surfaces puissent mettre la cause devant et ranger le reste derrière elle.

Trois garde-fous, chacun payé par une erreur qu'ils évitent :

  · une conséquence n'en est une QUE si sa cause est elle-même en panne. Un XL débranché
    pendant que le Plus va très bien est une panne à part entière — l'effacer derrière un
    lien théorique reviendrait à cacher un vrai problème.
  · la chaîne se remonte jusqu'au bout (le Plus alimente un hub qui alimente le reste),
    donc la cause affichée est la cause RACINE, pas le maillon intermédiaire. Une boucle
    de déclaration ne doit pas figer le moteur : d'où la limite de profondeur.
  · la topologie peut dépendre du MODE. Le breath n'est branché sur le Plus que sur
    scène ; au bureau il n'est pas là du tout, et un lien qui ne vaut pas ce soir est un
    lien qui ment.
"""

from __future__ import annotations

# Une déclaration mal fermée (A dépend de B, B dépend de A) ne doit pas boucler
# indéfiniment dans un serveur sondé toutes les 5 s. Huit maillons dépassent de loin
# toute chaîne d'alimentation réelle : au-delà, c'est une erreur de config, pas une
# topologie.
_MAX_DEPTH = 8

# Ce qui compte comme « en panne » pour la propagation. INFO en est exclu : il dit
# « absent, et c'est normal » — un optionnel non branché n'explique la chute de personne.
_BROKEN = ("fail", "warn")


def edges(cfg: dict, mode: str) -> dict[str, tuple[str, str]]:
    """clé de l'appareil alimenté → (clé de sa source, pourquoi il en dépend).

    Un enfant n'a qu'UNE source : deux blocs qui revendiquent la même clé, c'est une
    contradiction de config, et le dernier lu gagne — silencieusement, parce qu'un
    serveur qui refuse de démarrer sur une topologie douteuse serait pire que le
    problème qu'il signale.
    """
    out: dict[str, tuple[str, str]] = {}
    for dep in cfg.get("depends", []) or []:
        modes = dep.get("modes")
        if modes and mode not in modes:
            continue
        source, why = dep.get("on", ""), dep.get("why", "")
        if not source:
            continue
        for key in dep.get("keys", []) or []:
            if key != source:           # se dépendre de soi-même n'a pas de sens
                out[key] = (source, why)
    return out


def _root(key: str, links: dict[str, tuple[str, str]], broken: set[str]) -> str | None:
    """Remonte tant que la source est ELLE AUSSI en panne. None = pas de cause amont.

    C'est ici que se joue la nuance qui fait tout le module : on ne remonte pas la
    topologie, on remonte la PANNE. Un maillon sain arrête la remontée, parce qu'un
    appareil qui répond n'explique la chute de rien.
    """
    seen, cur = {key}, key
    for _ in range(_MAX_DEPTH):
        parent = links.get(cur, (None, ""))[0]
        if parent is None or parent not in broken or parent in seen:
            break
        seen.add(parent)
        cur = parent
    return cur if cur != key else None


def annotate(cfg: dict, mode: str, items: list[dict]) -> list[dict]:
    """Pose `caused_by` / `caused_why` sur les conséquences, `causes` sur les causes.

    Modifie les items sur place (et les rend, pour l'écriture en une ligne). Aucun item
    n'est retiré : une surface étroite peut choisir de replier les conséquences, une
    large peut les montrer toutes — c'est son affaire, pas celle du moteur.
    """
    links = edges(cfg, mode)
    if not links:
        return items
    broken = {it["key"] for it in items if it.get("status") in _BROKEN}
    by_key = {it["key"]: it for it in items}

    for it in items:
        if it["key"] not in broken:
            continue
        root = _root(it["key"], links, broken)
        if root is None or root not in by_key:
            continue
        it["caused_by"] = root
        # Le « pourquoi » du lien DIRECT, pas celui de la racine : c'est celui-là qui
        # décrit le câble qu'on va aller regarder.
        it["caused_why"] = links[it["key"]][1]
        cause = by_key[root]
        cause.setdefault("causes", []).append({"key": it["key"], "label": it.get("label", it["key"])})
    return items


def summary(items: list[dict]) -> str:
    """Une phrase pour la cause qui en explique d'autres, ou "" — pour les surfaces
    qui n'ont la place que d'une ligne (une notification, un titre de menu).
    """
    for it in items:
        n = len(it.get("causes") or [])
        if n:
            return f"{it.get('label', it['key'])} — et {n} panne{'s' if n > 1 else ''} qui en découle{'nt' if n > 1 else ''}"
    return ""
