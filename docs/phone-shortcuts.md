# Phone-pushed state (iPhone → rig) via Shortcuts + ntfy

The rig can't ask the iPhone anything over the LAN — iOS exposes no battery/app state to
network peers. So the phone **pushes** its own state: a Shortcuts *personal automation*
sends a tiny HTTP POST to an **ntfy topic** on each change, and the rig reads the latest
value with [`bin/phone-state.sh`](../bin/phone-state.sh).

## Model — why "no notification ≠ off"

The phone reports **events** (charger connected/disconnected, app opened), not a continuous
signal. A dropped push (WiFi off, ntfy hiccup, iOS throttling) must **not** become a false
"off". So the rig only ever turns a check **green on a fresh positive** ("charging=1" newer
than the freshness window); with no fresh positive it stays **"à confirmer" (warning)** —
never a hard red claiming the phone is definitely not charging.

Freshness window default: **6 h** (last arg to `phone-state.sh`). In practice you plug the
phone in before the set, so the "charging=1" event is recent enough for the whole gig.
Limitation: iOS can't cheaply heartbeat every few minutes in the background, and ntfy.sh
retains messages ~12 h — fine for a gig, not for "is it charging right now, 2 days later".

## The topic

Pick a hard-to-guess topic (anyone who knows it can post to it). It lives in **`rig.toml`**
(private), not here. Example used by the checks: `benoit-rig-phone-<suffix>`.

## Message format

Each push is one line, `key=value`, sent as the POST **body** (ntfy makes the body the
message). The rig greps for `"<key>=..."`:

| Key | Values | Meaning |
|---|---|---|
| `charging` | `1` / `0` | charger connected / disconnected |
| `stagetraxx` | `1` / `0` | Stage Traxx opened / closed (open is reliable; close is not — see below) |

## Setup — Shortcuts personal automations (on the iPhone)

**Charger connected → charging=1**

1. Shortcuts app → *Automation* tab → **+** → *Create Personal Automation*.
2. Trigger: **Charger** → *Is Connected*.
3. Add action **Get Contents of URL**:
   - URL: `https://ntfy.sh/benoit-rig-phone-<suffix>`
   - (expand) **Method: POST**, **Request Body: Text**, body = `charging=1`
4. **Run Immediately** (toggle off "Ask Before Running"). Done.

**Charger disconnected → charging=0**: same, trigger *Charger → Is Disconnected*, body
`charging=0`.

**(Optional) Stage Traxx opened → stagetraxx=1**

1. New automation, trigger: **App** → *Stage Traxx* → *Is Opened*.
2. Same POST action, body `stagetraxx=1`. Run Immediately.
3. A matching *Is Closed* → `stagetraxx=0` **can** be added, but iOS fires it unreliably for
   backgrounded/force-quit apps — so treat Stage Traxx as "confirmed opened recently", i.e.
   a warning when there's no fresh `stagetraxx=1`, exactly like charging.

> **Bome Network is NOT here on purpose.** The rig already proves it a better way: the
> `Bome ↔ iPhone` check watches the established TCP link (port 37000). If Bome is running
> and connected to the Mac, that link exists — a direct signal, more reliable than a
> Shortcut. No phone push needed for Bome.

## Test

From any machine, simulate the phone:

```bash
curl -d "charging=1" https://ntfy.sh/benoit-rig-phone-<suffix>
bin/phone-state.sh benoit-rig-phone-<suffix> charging 21600 && echo GREEN || echo "à confirmer"
```

(There's a ~25 s cache in `phone-state.sh`; wait or delete `/tmp/rig-phone-charging.cache`.)
