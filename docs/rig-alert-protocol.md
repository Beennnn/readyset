# Protocole « rig-alert » — alerte visuelle Stream Deck par MIDI

But : allumer un bouton Stream Deck en rouge quand un check du rig tombe en panne,
sans que le logiciel ait à *repeindre* la touche (impossible depuis l'extérieur). On
passe par un **feedback MIDI** : `rig` émet une note sur un port dédié, une touche
configurée en feedback réagit à cette note.

## Vue d'ensemble

```
  rig monitor ──note MIDI──►  IAC « rig-alert »  ──►  touche Stream Deck (feedback)
   (émetteur)                  (cul-de-sac)             (récepteur → rouge/normal)
```

- **Émetteur** : le backend `streamdeck` de `riglib/alerts.py` (utilisé par `rig monitor`
  et `rig alert-test`).
- **Transport** : un port MIDI virtuel **IAC dédié**, `rig-alert` — un cul-de-sac que
  *rien d'autre* dans le rig ne lit, donc une note d'alerte ne déclenche jamais un son
  ou une action dans Ableton/Bome.
- **Récepteur** : une touche Stream Deck configurée en **feedback MIDI entrant**.

## Spécification du message

| Événement | Message MIDI | Canal | Note | Vélocité | Octets bruts |
|---|---|---|---|---|---|
| **Alerte** (un check `fail` ou `warn` apparaît) | Note On | 15 | 60 | 127 | `9E 3C 7F` |
| **Rétabli** (le check repasse OK) | Note Off | 15 | 60 | 0 | `8E 3C 00` |

- Canal **15** (1-16, humain) = mido channel 14 (0-based) → nibble de statut `0xE`.
- Note **60** (Do central, C3/C4 selon convention).
- Le canal 15 est choisi *hors* des canaux musicaux du set pour ne rien perturber.
- Configurable : `[alerts.streamdeck]` dans `rig.toml` (`port`, `channel`, `note`).

## Émission (déjà en place)

`rig monitor` envoie **Note On (vel 127)** à la première panne (`fail`/`warn`) et
**Note Off** au rétablissement. `rig alert-test --alerts streamdeck` envoie une Note On
de test. L'émetteur ouvre le port dont le nom contient `rig-alert` ; s'il est absent,
il journalise « port absent » et ne fait rien (pas d'erreur).

```toml
[alerts.streamdeck]
port    = "rig-alert"    # port IAC dédié (créé dans Configuration audio et MIDI)
channel = 15
note    = 60
```

## Réception (à câbler côté Stream Deck — une fois)

1. **Port IAC** *(fait)* : Configuration audio et MIDI → Studio MIDI → double-clic
   **IAC Driver** → **+** → renommer **`rig-alert`** → Appliquer.
2. **Touche** : sur une touche libre, poser l'action **trevligaspel MIDI**, la passer en
   **entrée / feedback MIDI** :
   - Port d'entrée : `rig-alert`
   - Message : **Note On, note 60, canal 15**
   - Image **rouge** sur Note On (vel > 0), image **normale** sur Note Off (vel 0).

   ⚠️ Vérifier que la version du plugin trevligaspel gère le *MIDI entrant → état de la
   touche*. Sinon, utiliser un autre plugin de feedback MIDI Stream Deck.

## Test

```bash
./rig alert-test --alerts streamdeck   # → la touche doit virer au rouge
```

Émission vérifiée le 2026-07-13 : la note part bien vers `rig-alert` (« Envoyé »).
Reste la config de la touche réceptrice.

## Pourquoi ce design

- **Pas trevligaspel en sortie** : ce plugin fait touche → MIDI (sortant), il ne peut
  pas repeindre une touche depuis un script. Le feedback MIDI *entrant* est le vrai
  canal pour changer l'état d'une touche.
- **Port dédié `rig-alert`** : tous les autres ports IAC (`Ableton Loopback`, `Daw2Mackie`…)
  sont câblés dans le routing live ; y envoyer une note risquerait de déclencher un son
  ou une action. Un cul-de-sac isole l'alerte.
