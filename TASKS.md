# TASKS — readyset

Items ouverts uniquement. Supprimer une ligne quand c'est shippé ; supprimer le fichier
quand tout est fait.

## À faire (gestes manuels, sur la machine de Benoît)

- ☐ **Installer la sonde audio** → le mécanisme est codé + branché dans le soundcheck
  (mesure auto au lieu du « J'entends le son »). Reste le geste une fois : accorder la
  permission audio (`audiolevel/audiolevel 1.5 ableton` → Autoriser) puis
  `audiolevel/install-daemon.sh ableton`. Voir [audiolevel/README.md](audiolevel/README.md).

## 🤔 À valider en conditions réelles

- ☐ **Vrai `rig preflight --mode live`, rig branché** → jamais exécuté en vrai ; valider que
  tous les checks passent au vert dans les conditions du set ET que la bascule live/studio
  se déclenche bien sur le critère ping (`[mode].detect`).
