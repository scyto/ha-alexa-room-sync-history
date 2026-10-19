# Alexa Room Sync

Mirror your **Home Assistant areas** into **Alexa rooms/groups** — one Alexa room per HA area,
with every Alexa-exposed HA device dropped into the room matching its HA area. So "Alexa, turn
off the kitchen lights" lines up with how HA is organised.

It does this through the **unofficial Alexa API** — the same private endpoints the
[Alexa Media Player](https://github.com/alandtse/alexa_media_player) integration already speaks.
**No official Alexa Smart Home skill, no Nabu Casa group magic** (Nabu Casa exposes devices but
cannot create Alexa rooms). The gap this fills — `POST /api/phoenix/group` — isn't wrapped by any
existing library.

> ⚠️ **This uses an undocumented Amazon API.** Amazon can change it without notice. Treat it as a
> convenience tool, keep the default dry-run on until you've reviewed a preview, and test one room
> first.

## How it works

- **Auth is borrowed, never handled here.** The integration reuses the authenticated
  `AlexaLogin` object from Alexa Media Player (`hass.data["alexa_media"]["accounts"][email]["login_obj"]`)
  and drives `alexapy`'s request primitive. No password, cookies, or 2FA dance live in this code.
- It reads which entities you've **exposed to Alexa** (HA Cloud) and each entity's **HA area**
  (entity area, else its device's area).
- It fetches your **Alexa smart-home appliances** (`GET /api/phoenix`) and matches each exposed HA
  entity to its Alexa `applianceId` — by embedded HA entity_id first, then by exact friendly name.
- It groups the matched appliance IDs per area and creates one Alexa room per area
  (`POST /api/phoenix/group`, body `{"applianceIds":[...],"name":"Room"}`).

### Endpoints used

| Purpose | Call |
|---|---|
| List Alexa appliances | `GET /api/phoenix` |
| List existing rooms | `GET /api/phoenix/group` |
| Create a room | `POST /api/phoenix/group` |
| Delete a room (manual undo) | `DELETE /api/phoenix/group/{id}` |

## Requirements

- Home Assistant 2024.8+.
- **Alexa Media Player** installed and logged in (this is a hard prerequisite — it provides both
  the `alexapy` library and the authenticated session).
- Your HA devices already exposed to Amazon Alexa (via HA Cloud / Nabu Casa) and appearing in the
  Alexa app.

## Install (HACS custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/neilellis/ha-alexa-room-sync`,
   category **Integration**.
2. Install **Alexa Room Sync**, restart HA.
3. **Settings → Devices & Services → Add Integration → Alexa Room Sync**, pick your Alexa account.

## Use it — safe order

Everything is **dry-run by default**. Nothing is written to Alexa until you explicitly opt in.

1. **Preview (read-only).** Developer Tools → Actions → `alexa_room_sync.preview` → *Run*. Read the
   log (and the returned response): it lists every proposed room, which devices matched to an Alexa
   appliance (`✓`), and which didn't (`✗`). Fix any unmatched devices (usually a name mismatch or a
   device not yet exposed to Alexa).
2. **Enable writes.** Integration → **Configure** → turn **Dry-run OFF** and tick
   **"I understand 'apply' will write to my Alexa account"**.
3. **Test ONE room.** `alexa_room_sync.sync_area` with `area_id: <your area>` and `apply: true`.
   Check the Alexa app: the room should appear with the right devices. If it's wrong, delete it in
   the Alexa app (or via `DELETE /api/phoenix/group/{id}`) — creation is fully reversible.
4. **Do the rest.** `alexa_room_sync.sync` with `apply: true`.

### What a sync changes

The integration remembers which Alexa rooms it created, keyed by HA area. A sync then:

- **creates** a room for an area that doesn't have one yet;
- **updates** a managed room when the area's devices change or the area is renamed;
- **deletes** a managed room when its area is added to *Areas to skip*, removed, or has no
  exposed devices left (full `sync` only; `sync_area` never deletes other rooms).

Devices that aren't from Home Assistant (for example an Echo you put in the room in the
Alexa app) are always kept. Groups it didn't create are left alone, with one exception: if
an Alexa group has exactly the room's name and no room is managed for that area yet, it is
adopted (useful after upgrading from a create-only version).

### Syncing one area

**Configure > Sync one area** lets you pick an area and either preview it (Apply off) or sync
just that room (Apply on). Other rooms are left alone, and it works for areas on the skip list.
The `alexa_room_sync.sync_area` action does the same from scripts.

### Removing it

Removing the integration does **not** change Alexa. Your rooms keep working, and a reinstall
picks them up again by name. Only the integration's saved record of its rooms is deleted.

To remove the rooms too, go to **Configure > Remove managed rooms from Alexa** first, or call
`alexa_room_sync.remove_managed_rooms` with `confirm: true`. Rooms that also hold something
you added yourself, such as an Echo, keep it and are just released.

### Buttons

The integration adds a **Preview room sync** button, which shows the planned changes as a
notification and never writes. It also adds **Sync rooms now**, which runs a full sync when
dry-run is off and writes are confirmed, and otherwise just previews.

## Services

| Service | Writes? | Notes |
|---|---|---|
| `alexa_room_sync.preview` | No | Read-only plan + match diagnostics. Optional `area_id`. |
| `alexa_room_sync.list_groups` | No | List existing Alexa rooms. |
| `alexa_room_sync.sync_area` | Only with `apply:true` + options confirm | One area — the single-room test. |
| `alexa_room_sync.sync` | Only with `apply:true` + options confirm | All areas. |

## Options

- **Dry-run** (default on) — hard stop on all writes.
- **Write confirmation** — must be on before `apply:true` does anything.
- **Room name prefix** — e.g. `HA ` to keep synced rooms visually distinct.
- **Include climate** — include Tado/climate entities in rooms (default on).
- **Include child-lock switches** — usually noise in a room (default off).
- **Areas to skip** — HA areas a full `sync` never turns into Alexa rooms. An explicit `sync_area` on one of them still runs.

## Naming notes (multi-kitchen households)

Rooms are named after the HA area. If two areas would collide by voice (e.g. two kitchens), rename
the HA areas to something voice-unambiguous ("Ground Floor Kitchen" / "Top Floor Kitchen"), or set
a prefix. Alexa matches rooms by what you say, so ambiguous names cause "which one?" prompts.

## Caveats

- HA switches that drive lights are **switches** to Alexa, so Alexa's per-room "turn off the lights"
  may skip them. Expose them as `light` in HA if you want them caught by "lights".
- Devices with no HA area can't be placed — assign the area in HA first.
- The `/api/phoenix` payload shape is Amazon's and undocumented; the parser walks it defensively.
  If a future Amazon change breaks matching, `preview` will show it as unmatched before any write.

## Credits & licence

MIT. Relies on [alexa_media_player](https://github.com/alandtse/alexa_media_player) /
[alexapy](https://gitlab.com/keatontaylor/alexapy) (Apache-2.0) for authentication. Endpoint shapes
are the public knowledge documented by
[alexa-remote](https://github.com/Apollon77/alexa-remote) (MIT) and community reverse-engineering.
Not affiliated with or endorsed by Amazon.
