# TASKS — readyset

Items ouverts uniquement. Supprimer une ligne quand c'est shippé ; supprimer le fichier
quand tout est fait.

## À faire

- ☐ **Brancher le mètre audio (`audiolevel/`) dans le soundcheck** → aujourd'hui le
  soundcheck repose sur la confirmation manuelle « J'entends le son » ; un vrai niveau
  RMS live rendrait l'étape audio automatique. Bloqué par TCC (permission audio refusée
  au subprocess). Décider l'approche : (a) tourner `audiolevel` en service permanent
  pré-autorisé et le lire via socket/fichier, ou (b) intégrer la capture dans le binaire
  menubar déjà autorisé.

## 🤔 À valider en conditions réelles

- ☐ **Vrai `rig preflight --mode live` de bout en bout, rig branché** → jamais exécuté en
  vrai ; valider que tous les checks passent au vert dans les conditions du set ET que la
  bascule live/studio se déclenche bien sur le critère ping (`[mode].detect`).
