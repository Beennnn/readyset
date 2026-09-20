# readyset — le dépôt PUBLIABLE

## Le vocabulaire : un mot, un concept

Six mots désignaient la même chose (`rig`, `iRig`, `readyset`, `rig-control`, `riglib`,
`live-rig`). Il n'en reste que deux, et ils ne se recouvrent pas :

| mot | ce qu'il désigne, et rien d'autre |
|---|---|
| **rig** | l'**installation physique** — claviers, câbles, le DAW, le Stream Deck. Jamais un nom de programme. |
| **readyset** | le **logiciel** : l'exécutable `bin/readyset` et le paquet `readyset/`. |
| **check** | une vérification · **fix** : sa correction · **surface** : un endroit où ça s'affiche |

`iRig` est une marque déposée d'IK Multimedia : elle ne doit jamais revenir.

⚠️ **L'exécutable est dans `bin/`, pas à la racine.** Un fichier et un dossier ne peuvent
pas porter le même nom dans le même répertoire, et le paquet s'appelle `readyset/`.
`bin/readyset` ajoute donc la racine du dépôt à `sys.path` avant d'importer le paquet.

`rig.toml` et `rig.example.toml` gardent leur nom : ils décrivent bien l'installation
PHYSIQUE, pas le logiciel.

## Rien qui appartienne à une installation particulière

Tout le spécifique vit dans `rig.toml` (git-ignored) ; `rig.example.toml` en est le mode
d'emploi versionné. Le code ne nomme aucun appareil, aucun morceau, aucun numéro de série,
aucune machine, aucune personne — si une modification l'exige, elle est mal placée : elle
va dans la config, ou dans `readyset/devices/devices.toml` quand c'est un catalogue
d'appareils.

Le dépôt privé `rig-control` a été absorbé le 2026-09-20 (commit `b42d90f`) : il n'y a
plus qu'une base de code, celle-ci.

## L'app de barre de menus vit ICI

C'est celle qui est réellement installée (`/Applications/RigMenuBar.app`, agent
`com.readyset.menubar`). La copie qui traînait dans l'ancien dépôt jumeau a été supprimée
le 2026-08-19 — elle datait d'avant et son `install.sh` posait une SECONDE icône 🎹 dans
la barre.

## Construire et installer l'app

```bash
cd readyset/surfaces/menubar && ./build.sh
ditto RigMenuBar.app /Applications/RigMenuBar.app   # COPIER D'ABORD
pkill -f 'RigMenuBar.app/Contents/MacOS/RigMenuBar' # PUIS tuer
```

⚠️ **L'ordre compte** : launchd relance l'app en ~1 s. Tuer avant d'avoir copié relance
l'ANCIEN binaire — on croit alors que la modification n'a rien changé et on cherche un bug
qui n'existe pas. Vécu deux fois le 2026-08-19.

## Le menu de la barre : une seule construction pour deux surfaces

`fill(_ menu:)` remplit **à la fois** le menu de l'icône de la barre de menus et celui que
la barre flottante ouvre au clic. Deux listes séparées finiraient par diverger, et il
faudrait se rappeler laquelle des deux sait faire quoi un soir de concert. Ne pas en
refaire une seconde.

Trois pièges macOS déjà payés, tous commentés dans le code :

- le menu se pose en **coordonnées écran** (`in: nil`) ; ancré à la vue, macOS cale la
  liste sur le point et fait déborder l'en-tête au-dessus du bord haut ;
- `autoenablesItems = false` : ouvert depuis une fenêtre qui ne devient jamais active,
  macOS grise des entrées pourtant valides — et un parent de sous-menu grisé perd sa flèche ;
- une entrée sans action est grisée : une ligne qui n'a rien à corriger ouvre le dashboard
  plutôt que d'être inerte, sinon la lisibilité qu'on est venu chercher est perdue.

## Le principe qui gouverne tous les checks

**Aucune ligne verte qui n'ait été observée.** Un check qui ne sait pas le dit ; il ne
suppose jamais que tout va bien. Voir `readyset/checks/phone.py` pour le cas d'école :
mesure contre déclaration, et « il s'est tu » comme état à part entière.

`Result.parts` sert aux checks COMPOSITES (le soundcheck et ses gestes) : le check reste
UNE ligne — c'est ce qui garde la liste lisible — mais les surfaces qui ont la place
déplient le détail au lieu de le tronquer.

## L'arborescence, et pourquoi elle est comme ça

```
bin/readyset      le CLI (preflight · check · monitor · serve · tidy · apps · vpn · alert-test)
readyset/
  core/       config · cascade · le type Result — ne dépend de rien
  checks/     une FAMILLE par fichier : apps · audio · midi · network · phone · gear
  fix/        remedy · launch
  surfaces/   web/ (http · state · page.html) · menubar/ (Swift) · alerts/
  devices/    devices.toml + son chargeur
plugins/      des outils shell autonomes, appelés à la main pour l'instant
```

**La flèche des dépendances ne pointe que vers l'intérieur.** `core/` n'importe ni
`checks/`, ni `fix/`, ni `surfaces/`. C'est ce qui permet à un check, à un correctif et à
un affichage de parler du même `Result` sans qu'aucun des trois n'ait à connaître les deux
autres.

**`devices/` est un CATALOGUE, pas du code.** Deux modèles de claviers y étaient codés en
dur dans un moteur censé être générique. En données (`devices.toml`), trois choses se
règlent d'un coup : le moteur redevient générique, le matériel d'une personne sort du
code, et quelqu'un d'autre peut ajouter le sien sans écrire de Python.

**La page du dashboard est un FICHIER** (`readyset/surfaces/web/page.html`), pas une
chaîne littérale dans un `.py`. Elle est lue au démarrage par un chemin relatif AU MODULE
et jamais au répertoire courant : l'agent launchd lance le dashboard sans `cd`, donc un
chemin relatif se résoudrait depuis `/`.

## Longueur des fichiers

Plafond du projet : 1 000 lignes. Aucun fichier ne le dépasse aujourd'hui — le plus gros
est `readyset/checks/audio.py`. Avant d'ajouter à un fichier proche du plafond, extraire :
c'est pour ça que la lecture `ideviceinfo` vit dans `readyset/idevice.py` plutôt que
d'avoir été glissée dans le serveur.

## Plusieurs sessions Claude travaillent dans `~/dev`

Ne jamais éditer ce clone directement : `git worktree add <scratchpad>/wt-<slug> -b
claude/<slug> origin/main`, puis `git worktree remove` à la fin. Vérifier `git worktree
list` et `git status` avant de commencer.
