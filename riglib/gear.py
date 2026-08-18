"""Illustrations du matériel — ce qu'aucun bundle .app ne peut fournir.

Les checks qui portent sur un LOGICIEL ont déjà leur image : macOS la range dans le
bundle, `apps.icon_png()` l'en sort, et on reconnaît Chrome à son logo bien avant
d'avoir lu son nom. Les checks qui portent sur du MATÉRIEL n'ont rien de tel — un
clavier, un modem, une lampe ne sont pas des apps. Ils tombaient donc sur l'emoji
générique de leur famille : 🌐 pour le réseau, 💡 pour les lampes, le même pictogramme
pour deux appareils que tout oppose.

D'où ce module : un dessin par appareil réellement posé sur scène. Ce ne sont pas des
photos produit — c'est délibéré :

  · aucun réseau, aucun fichier à télécharger, rien à mettre en cache ni à réparer le
    jour où une URL meurt. Le dashboard reste servi entièrement en local, ce qui est
    tout son intérêt sur un réseau de scène sans Internet ;
  · aucune question de droits sur une image de catalogue ;
  · à 22 px dans une ligne de tableau, une photo devient une tache grise. Une silhouette
    tranchée reste lisible — c'est la SILHOUETTE qu'on reconnaît à cette taille, pas le
    produit.

Le critère de réussite est donc : reconnaissable du premier coup d'œil, à 22 px, sur
fond sombre. Chaque dessin garde le trait qui distingue vraiment l'objet — les deux
antennes et le boîtier jaune du Mango, les touches noires et blanches du piano, le
faisceau de la lampe — et laisse tomber tout le reste.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

# Palette alignée sur celle du dashboard (voir server.py), pour que les dessins ne
# jurent pas avec le reste de la page.
_TX = "#e7ebf2"      # trait principal
_MUT = "#8a93a3"     # trait secondaire
_ACC = "#4a9eff"     # accent bleu
_WARM = "#f4b942"    # jaune (Mango, lumière)


def _piano(badge: str, accent: str) -> str:
    """Un piano de scène slab : corps sombre, clavier, et le numéro du modèle.

    Le P-225 et le P-125 sont deux slabs Yamaha quasi identiques — les distinguer par
    la silhouette serait un mensonge de dessinateur. C'est donc le NUMÉRO qui les
    sépare, plus une couleur d'accent : à 22 px on ne lira pas le chiffre, mais on
    verra qu'il y a DEUX claviers acceptés, et au survol le titre le dit en toutes
    lettres. Mieux vaut deux vignettes honnêtement semblables qu'un faux détail.
    """
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">
  <rect x="2" y="7" width="44" height="18" rx="2.5" fill="#1b1f28" stroke="{_MUT}" stroke-width="1.4"/>
  <rect x="4.5" y="14" width="39" height="9" rx="1" fill="{_TX}"/>
  <g fill="#11141a">
    <rect x="7.5" y="14" width="2.2" height="5.5"/><rect x="12" y="14" width="2.2" height="5.5"/>
    <rect x="19" y="14" width="2.2" height="5.5"/><rect x="23.5" y="14" width="2.2" height="5.5"/>
    <rect x="28" y="14" width="2.2" height="5.5"/>
    <rect x="35" y="14" width="2.2" height="5.5"/><rect x="39.5" y="14" width="2.2" height="5.5"/>
  </g>
  <rect x="4.5" y="9.5" width="12" height="2.6" rx="1.3" fill="{accent}"/>
  <text x="43.5" y="12.4" font-family="-apple-system,Helvetica,sans-serif" font-size="6.5"
        font-weight="700" fill="{accent}" text-anchor="end">{badge}</text>
  <path d="M9 25v3M39 25v3" stroke="{_MUT}" stroke-width="1.6" stroke-linecap="round"/>
</svg>'''


# Le modem de scène est un GL.iNet « Mango » : un petit boîtier JAUNE à deux antennes.
# C'est le seul objet jaune de la mallette — la couleur suffit à l'identifier de loin,
# et c'est exactement ce qu'on demande à une vignette de 22 px.
_MANGO = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">
  <path d="M11 13V5.5M37 13V5.5" stroke="{_MUT}" stroke-width="2.4" stroke-linecap="round"/>
  <circle cx="11" cy="4" r="1.6" fill="{_MUT}"/><circle cx="37" cy="4" r="1.6" fill="{_MUT}"/>
  <rect x="6" y="12.5" width="36" height="15" rx="3" fill="{_WARM}" stroke="#c8901f" stroke-width="1.2"/>
  <rect x="9.5" y="16" width="29" height="4.5" rx="1.2" fill="#1b1f28" opacity=".35"/>
  <circle cx="12" cy="24" r="1.35" fill="#1b1f28"/>
  <circle cx="17" cy="24" r="1.35" fill="#1b1f28" opacity=".45"/>
</svg>'''


# Lampe de scène. Le faisceau est la moitié qui compte : un projecteur éteint et un
# projecteur allumé, c'est toute la question que pose le check.
_LAMP = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">
  <path d="M24 20 L13 30 h22 Z" fill="{_WARM}" opacity=".22"/>
  <rect x="15" y="5" width="18" height="14" rx="2.5" fill="#1b1f28" stroke="{_MUT}" stroke-width="1.4"/>
  <ellipse cx="24" cy="19" rx="9" ry="2.6" fill="{_WARM}"/>
  <path d="M24 3.5v-2M15.5 5.5 14 4M32.5 5.5 34 4" stroke="{_MUT}" stroke-width="1.4" stroke-linecap="round"/>
  <rect x="21" y="26" width="6" height="4" rx="1" fill="{_MUT}"/>
</svg>'''


# iPhone. L'îlot dynamique est le seul détail qui date un iPhone au premier regard —
# c'est donc le seul détail dessiné.
_IPHONE = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">
  <rect x="17" y="2" width="14" height="28" rx="3.4" fill="#1b1f28" stroke="{_TX}" stroke-width="1.5"/>
  <rect x="18.8" y="4.4" width="10.4" height="23.2" rx="2" fill="{_ACC}" opacity=".18"/>
  <rect x="21.4" y="5.6" width="5.2" height="1.9" rx=".95" fill="{_TX}"/>
</svg>'''


# Un slug → un dessin. Les deux pianos ne diffèrent que par le badge et l'accent.
SVG: dict[str, str] = {
    "p225": _piano("225", _ACC),
    "p125": _piano("125", _MUT),
    "mango": _MANGO,
    "lamp": _LAMP,
    "iphone": _IPHONE,
}


def app_path_for(cfg: dict, label: str) -> str | None:
    """Le bundle .app derrière un libellé de check.

    D'abord les apps que le rig lance lui-même (chemins exacts, donnés par rig.toml),
    sinon un coup d'œil dans /Applications — Ableton n'est PAS dans cette liste (le rig
    ouvre un set, il ne lance pas l'app) et son nom porte sa version, d'où le glob.
    """
    for app in cfg.get("launch", {}).get("apps", []):
        if label.lower() in Path(app).stem.lower():
            return app
    hits = sorted(Path("/Applications").glob(f"{label}*.app"))
    return str(hits[0]) if hits else None


def icons_for(cfg: dict, key: str, label: str) -> list[str]:
    """Les URL d'icônes d'un check, dans l'ordre d'affichage. Vide = emoji générique.

    Un check peut en porter PLUSIEURS, et ce n'est pas de la décoration : « Bome Network
    ↔ iPhone » est un lien entre deux appareils, et montrer les deux dit d'un coup d'œil
    où chercher. Le clavier en montre deux aussi — les deux modèles acceptés.
    """
    def gear(slug: str) -> str:
        return f"/api/gear?id={slug}"

    def app(name: str) -> list[str]:
        path = app_path_for(cfg, name)
        # quote() : « /Applications/Bome Network.app » contient des espaces, et une URL
        # non encodée ne marche que par la tolérance du navigateur.
        return [f"/api/appicon?path={quote(path)}"] if path else []

    if key == "kbd:keyboard":
        return [gear("p225"), gear("p125")]
    if key == "net:stage":
        return [gear("mango")]
    if key == "lamp:all":
        return [gear("lamp")]
    if key == "net:iphone":
        return app("Bome Network") + [gear("iphone")]
    if key == "pow:iphone":
        return [gear("iphone")]
    if key.startswith("usb:"):
        return app("Elgato Stream Deck")
    if key == "audio:live" or key.startswith("midi:Ableton"):
        return app("Ableton")
    if key.startswith("audio:") and "P-Series" in label:
        return [gear("p225")]
    # Les checks « <app> lancée » et les apps en trop portent le nom de l'app dans leur
    # clé : c'est la vraie icône macOS qu'on veut là, pas un dessin.
    if key.startswith("app:"):
        return app(key.split(":", 1)[1])
    # Apps en trop : leur chemin exact est déjà connu de l'inventaire (mis en cache), et
    # c'est justement là que l'icône compte le plus — on reconnaît Chrome à son rond
    # coloré avant d'avoir lu son nom, dans une liste où l'on s'apprête à fermer.
    if key.startswith("xapp:"):
        from . import apps as _apps
        name = key.split(":", 1)[1]
        for a in _apps.unexpected(cfg):
            if a["name"] == name:
                return [f"/api/appicon?path={quote(a['path'])}"]
    return []
