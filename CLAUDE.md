# readyset — le jumeau PUBLIABLE

Version générique de l'outil : `plugins/`, licence, et **rien qui appartienne à une
installation particulière**. Tout le spécifique vit dans `rig.toml` (git-ignored) ;
`rig.example.toml` en est le mode d'emploi versionné. Le code ne nomme aucun appareil,
aucun morceau, aucun numéro de série — si une modification l'exige, elle est mal placée :
elle va dans la config, ou dans le dépôt privé [`rig-control`](https://github.com/Beennnn/rig-control).

## Une exception à « tout arrive d'abord dans rig-control »

**L'app de barre de menus vit ICI**, et c'est celle qui est réellement installée
(`/Applications/RigMenuBar.app`, agent `com.readyset.menubar`). La copie qui traînait dans
`rig-control/menubar/` a été supprimée le 2026-08-19 — elle datait d'avant et son
`install.sh` posait une SECONDE icône 🎹 dans la barre. Le moteur qui tourne, lui, vient
toujours de `rig-control`.

## Construire et installer l'app

```bash
cd menubar && ./build.sh
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
suppose jamais que tout va bien. Voir `riglib/checks.py` (charge du téléphone) pour le cas
d'école : mesure contre déclaration, et « il s'est tu » comme état à part entière.

`Result.parts` sert aux checks COMPOSITES (le soundcheck et ses gestes) : le check reste
UNE ligne — c'est ce qui garde la liste lisible — mais les surfaces qui ont la place
déplient le détail au lieu de le tronquer.

## Longueur des fichiers

`riglib/server.py` dépasse 1 000 lignes (1 178 au 2026-08-19) : **ne rien y ajouter sans
extraire**. C'est pour ça que la lecture `ideviceinfo` est partie dans son propre module
`riglib/idevice.py` plutôt que d'y être glissée.

## Plusieurs sessions Claude travaillent dans `~/dev`

Ne jamais éditer ce clone directement : `git worktree add <scratchpad>/wt-<slug> -b
claude/<slug> origin/main`, puis `git worktree remove` à la fin. Vérifier `git worktree
list` et `git status` avant de commencer.
