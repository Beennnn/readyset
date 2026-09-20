"""LINKED failures — the fault that explains the others.

The table says what is wrong, device by device. It does not say what falls TOGETHER —
and on stage, that is almost always the real question. The Stream Deck Plus powers the
XL, the keyboard and the breath controller: when its cable gives out, four lines go red
within the same second and only one of them is the fault. The other three are not
problems to fix, they are SYMPTOMS. Reading them as four distinct failures means looking
for four gestures where there is only one — at the precise moment when there is only
time for a single one.

Hence this module. It tests NOTHING: it links. From a topology declared in rig.toml
(`[[depends]]`), it marks each failure as a cause or as a consequence, so that the
surfaces can put the cause in front and tuck the rest away behind it.

Three guard-rails, each one paid for by a mistake it prevents:

  · a consequence is only one IF its cause is itself down. An XL unplugged while the
    Plus is perfectly fine is a failure in its own right — erasing it behind a
    theoretical link would amount to hiding a real problem.
  · the chain is walked all the way up (the Plus powers a hub which powers the rest),
    so the cause displayed is the ROOT cause, not the intermediate link. A loop in the
    declaration must not freeze the engine: hence the depth limit.
  · the topology can depend on the MODE. The breath controller is only plugged into the
    Plus on stage; at the desk it is not there at all, and a link that does not hold
    tonight is a link that lies.
"""

from __future__ import annotations

# A badly closed declaration (A depends on B, B depends on A) must not loop forever in a
# server that is polled every 5 s. Eight links go far beyond any real power chain:
# past that, it is a config error, not a topology.
_MAX_DEPTH = 8

# What counts as « down » for the propagation. INFO is excluded from it: it says
# « absent, and that is normal » — unplugged optional gear explains nobody's fall.
_BROKEN = ("fail", "warn")


def edges(cfg: dict, mode: str) -> dict[str, tuple[str, str]]:
    """key of the powered device → (key of its source, why it depends on it).

    A child has only ONE source: two blocks claiming the same key is a config
    contradiction, and the last one read wins — silently, because a server that refuses
    to start over a dubious topology would be worse than the problem it is reporting.
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
            if key != source:           # depending on yourself makes no sense
                out[key] = (source, why)
    return out


def _root(key: str, links: dict[str, tuple[str, str]], broken: set[str]) -> str | None:
    """Walk up as long as the source is ALSO down. None = no upstream cause.

    This is where the nuance that makes the whole module is played out: we do not walk
    up the topology, we walk up the FAILURE. A healthy link stops the climb, because a
    device that answers explains nothing's fall.
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
    """Set `caused_by` / `caused_why` on the consequences, `causes` on the causes.

    Modifies the items in place (and returns them, so it can be written on one line). No
    item is removed: a narrow surface may choose to fold the consequences away, a wide
    one may show them all — that is its business, not the engine's.
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
        # The « why » of the DIRECT link, not the root's: that is the one that describes
        # the cable you are going to go and look at.
        it["caused_why"] = links[it["key"]][1]
        cause = by_key[root]
        cause.setdefault("causes", []).append({"key": it["key"], "label": it.get("label", it["key"])})
    return items


def summary(items: list[dict]) -> str:
    """One sentence for the cause that explains others, or "" — for the surfaces that
    only have room for a single line (a notification, a menu title).
    """
    for it in items:
        n = len(it.get("causes") or [])
        if n:
            return f"{it.get('label', it['key'])} — et {n} panne{'s' if n > 1 else ''} qui en découle{'nt' if n > 1 else ''}"
    return ""
