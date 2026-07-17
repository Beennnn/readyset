# TASKS — readyset

Items ouverts uniquement. Supprimer une ligne quand c'est shippé ; supprimer le fichier
quand tout est fait.

## ☐ Actions côté Benoît — au retour d'Irlande

Tout le code est fait/poussé ; il ne reste que des gestes manuels (téléphone, routeur, deck) :

- ☐ **Raccourcis iOS** (état poussé du tel → ntfy) : automatisations *Chargeur connecté/déconnecté*
  → `charging=1/0`, et *Stage Traxx ouvert* → `stagetraxx=1`, POST sur
  `https://ntfy.sh/benoit-rig-phone-e3744245`. Guide : [docs/phone-shortcuts.md](docs/phone-shortcuts.md).
  → active les checks « iPhone en charge » et « Stage Traxx ouvert ».
- ☐ **iPhone → Wi-Fi → BEN-MUSIC → Adresse Wi-Fi → « Fixe »** → sinon la MAC privée tourne et
  casse le check « iPhone sur le réseau » (`ca:c3:71:93:20:4c`).
- ☐ **Configurer l'interrupteur du Mango** (GL-MT300N-V2) : admin `http://192.168.8.1` → System →
  *Button Settings* → mettre sur **« No Function »** (ou garder le switch en position ON) → le
  routeur sert le WiFi en permanence, plus besoin de le basculer au branchement.
- ☐ **Poser les touches Stream Deck** (plugin [Beennnn/streamdeck-rig](https://github.com/Beennnn/streamdeck-rig),
  déjà installé) : catégorie *Rig* → *Rig status* + quelques *Rig problem* (régler le slot 1/2/3…)
  + *Rig fix all*.
- ☐ *(optionnel)* **Brouillon Elgato** (`maker@elgato.com`, éligibilité Marketplace de DeckShift) :
  scratchpad `elgato-message.md` — à envoyer seulement si tu veux explorer le Marketplace.

## 🤔 À valider en conditions réelles

- ☐ **Vrai `rig preflight --mode live`, rig branché sur scène** → jamais exécuté en vrai ; valider
  que tous les checks passent au vert dans les conditions du set ET que la bascule studio se
  déclenche bien sur la MAC de passerelle (`[mode].detect` = gateway_mac maison).
