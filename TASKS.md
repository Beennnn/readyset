# TASKS — readyset

Items ouverts uniquement. Supprimer une ligne quand c'est shippé ; supprimer le fichier
quand tout est fait.

## 🤔 À valider en conditions réelles

- ☐ **Vrai `rig preflight --mode live`, rig branché** → jamais exécuté en vrai ; valider que
  tous les checks passent au vert dans les conditions du set ET que la bascule live/studio
  se déclenche bien sur le critère ping (`[mode].detect`).

## 🤔 Décision ouverte (mineure)

- ☐ **Portée de la sonde audio** : elle tape actuellement **Ableton** uniquement. La passer
  en **tap global** (`audiolevel/install-daemon.sh` sans argument) si on veut aussi couvrir
  **Stage Traxx** / toute source — au prix d'un faux vert possible sur notif système.
