# TASKS — readyset

☐ Wire the RIG key on the deck → port `rig-alert`, channel 15, CC 110 (family index) + CC 111 (total), press sends CC 100 to open the dashboard — so the verdict is readable without going back to the keyboard

☐ Confirm on the first real `readyset preflight` that `[set.start_scene].quiet_seconds = 7` is enough → the threshold has to exceed the cadence of the chattiest periodic writer in the DAW's log (a plugin writes one line every 5 s during start-up), otherwise the silence between two of its lines reads as "loading finished"

☐ See the alarm panel actually fire → it is built and deployed (buttons reordered, coloured, charge confirmation) but has never been seen at work: it only appears on a real failure following a clean screen

☐ Re-run `launchd/install.sh` after this restructure lands → the agent still points at the old `rig` path; the dashboard will not restart until the plist is re-substituted (see the report for why this is not optional)

☐ Decide what `plugins/` is for → its README says checks and fixes call the plugins by path, but no code reads `[checks.fixes]`; either wire an executor into `readyset/fix/` or say in the README that they are standalone tools
