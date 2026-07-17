# TASKS — readyset

Items ouverts uniquement. Supprimer une ligne quand c'est shippé ; supprimer le fichier
quand tout est fait.

## ☐ À faire

- ☐ **Boutons Stream Deck « problèmes du rig »** (profil ou page dédié) → afficher les
  problèmes en cours ET les corriger depuis le Stream Deck, sans ouvrir l'écran. Consomme
  `/api/state` (liste + statut par check) et `/api/fix` (bouton = POST `{key}`). Idée : une
  touche par problème actif (icône err/warn + libellé court) + une touche « tout corriger »,
  sur une page dédiée du profil de scène. Config côté repo `stream-deck` (DSL trevligaspel /
  plugin) ; l'API existe déjà côté readyset.

## 🤔 À valider en conditions réelles

- ☐ **Vrai `rig preflight --mode live`, rig branché** → jamais exécuté en vrai ; valider que
  tous les checks passent au vert dans les conditions du set ET que la bascule live/studio
  se déclenche bien sur le critère ping (`[mode].detect`).
