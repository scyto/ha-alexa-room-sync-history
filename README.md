# Alexa Room Sync

Keep your **Alexa rooms** in step with your **Home Assistant areas**. There is one Alexa room per
HA area, holding every Alexa-exposed HA device in that area, so "Alexa, turn off the kitchen
lights" matches how HA is organised. Rooms are created, updated when an area's devices change or
the area is renamed, and removed when you skip or delete the area.

> **This is a fork** of [neilellis/ha-alexa-room-sync](https://github.com/neilellis/ha-alexa-room-sync).
> It fixes device matching (the original's device list now comes back empty), the `sync` services,
> and adds the full room lifecycle, buttons and an area picker. See [Changes in this fork](#changes-in-this-fork).

Nabu Casa / HA Cloud exposes your devices to Alexa but can't create Alexa rooms. This fills that
gap through the **unofficial Alexa API**, using the private endpoints the Alexa app and
[Alexa Media Player](https://github.com/alandtse/alexa_media_player) use.

> ⚠️ **This uses an undocumented Amazon API.** Amazon can change it without notice. Keep dry-run on
> until you've looked at a preview, and test one room first.

## Requirements

- Home Assistant. This fork is tested on 2026.10.
- **Alexa Media Player**, installed and logged in. This integration borrows its Amazon session and
  its `alexapy` library, and never asks for your password itself.
- Your HA devices **exposed to Alexa through HA Cloud** (Settings > Voice assistants > Expose) and
  discovered in the Alexa app.

## Install

1. HACS > ⋮ > **Custom repositories**: add `https://github.com/scyto/ha-alexa-room-sync`, with the
   type **Integration**.
2. Download **Alexa Room Sync** and restart HA.
3. **Settings > Devices & services > Add integration > Alexa Room Sync**, then pick your Alexa
   account.
4. After installing or updating, reload the browser (F5) if menu or field labels show up blank.

## Using it

Everything is read-only until you turn writes on.

1. On the integration's device page, press **Preview room sync**. The plan arrives in
   **Notifications** (the bell in the sidebar) and is summarised in **Last result**.
2. To allow writes, go to **Configure > Settings**, turn **Dry-run** off and tick **I understand
   'apply' will write to my Alexa account**.
3. Try one room first: pick it in **Area to sync** and press **Sync selected area**.
4. Then press **Sync rooms now** to do the rest. Rooms can take a minute or two to show up in the
   Alexa app; pull to refresh or reopen the app.

Afterwards, add each Echo to its room in the Alexa app, so "turn on the lights" works without
saying the room name. The sync keeps Echos you add.

### Device page

| Control | What it does |
|---|---|
| **Preview room sync** | Shows what a full sync would change. Never writes. |
| **Sync rooms now** | Full sync. Writes only when dry-run is off and writes are confirmed; otherwise it previews. |
| **Area to sync** | Picks the area for the two buttons below. Lists only areas that get a room (something exposed to Alexa, not skipped). Follows renames. |
| **Preview selected area** | Shows what syncing that one area would change. |
| **Sync selected area** | Syncs only that area's room (same write rules). |
| **Last result** | One-line summary of the last run; the details are in Notifications. |

### What a sync changes

The integration remembers which Alexa rooms it created, and which devices it put in each, keyed by
HA area. A full sync then:

- **creates** a room for an area that doesn't have one;
- **updates** a managed room when the area's devices change or the area is renamed;
- **deletes** a managed room when its area is skipped, deleted, or has no exposed devices left.
  If the room also holds something you added yourself, such as an Echo, the HA devices are taken
  out, your additions stay, and the room is released instead of deleted.

A single-area sync never touches other rooms. Groups the integration didn't create are left alone,
with one exception: an Alexa group with exactly the room's name, in an area nothing manages yet, is
adopted. Only Home Assistant devices are matched; Echos and other devices are never added.

As safety checks, a sync refuses to run if Alexa returns no devices, or no groups while rooms are
managed. Without that, a glitch on Amazon's side could look like "everything is gone".

### Removing it

Removing the integration **doesn't change Alexa**. Your rooms keep working, and a reinstall picks
them up again by name. Only the integration's saved record of its rooms is deleted.

To take the rooms down too, first use **Configure > Remove managed rooms from Alexa**, which asks
you to confirm. It removes the rooms it manages, which are the ones it created **plus any it
adopted** (an existing Alexa group with exactly an area's name). It keeps any device it didn't put
there. So an adopted room you made by hand is deleted too if it only held HA devices. Rename such
a room in Alexa before a sync if you want it left alone.

## Options (Configure > Settings)

- **Dry-run** (on by default): blocks all writes.
- **I understand 'apply' will write to my Alexa account**: must be ticked for any write.
- **Room name prefix**: for example `HA `, to tell synced rooms apart.
- **Include climate entities** (on by default): thermostats go in their room.
- **Include child-lock switches** (off by default).
- **Rooms to skip**: areas a full sync never gives a room. If a skipped area already has a managed
  room, the next full sync removes it.

## Actions (services)

For scripts and automations. With more than one Alexa account set up, pass `config_entry_id`.

| Action | Writes? | Notes |
|---|---|---|
| `alexa_room_sync.preview` | No | Plan and match details. Optional `area_id`. |
| `alexa_room_sync.sync` | With `apply: true` and the confirmation | Full sync. |
| `alexa_room_sync.sync_area` | With `apply: true` and the confirmation | One area. |
| `alexa_room_sync.list_groups` | No | Lists the groups in your Alexa account. |
| `alexa_room_sync.remove_managed_rooms` | With `confirm: true` | Removes the rooms it created. |
| `alexa_room_sync.dump_appliances`, `probe_endpoints`, `probe_lists` | No | Diagnostics. |

## How it works

- **Login:** it reuses Alexa Media Player's logged-in `AlexaLogin` and alexapy's request helper,
  so no credentials live here.
- **Devices:** it lists Alexa's smart-home devices with the Alexa app's GraphQL query
  (`POST /nexus/v1/graphql`), falling back to `GET /api/phoenix`. It matches each exposed HA entity
  by the entity_id that HA Cloud embeds in the Alexa device id (`…_light#kitchen_cans`). Otherwise
  it matches by name, where the first alias is what HA sends Alexa as the name.
- **Rooms:** `GET /api/phoenix/group` to list, `POST` to create, `PUT /api/phoenix/group/{id}` to
  update (it sends Amazon's own group document back with the name and members changed), and
  `DELETE` to remove.
- **Memory:** the managed rooms and their members are kept in HA storage
  (`.storage/alexa_room_sync.<entry>.rooms`).

## Naming tips

Rooms take the HA area's name. If two areas sound alike ("Kitchen" and "Kitchenette"), rename one
or use a prefix, otherwise Alexa will ask "which one?". Device names come from HA too: since HA
2026.8 Alexa gets an entity's **first alias**, so put the room-qualified name ("kitchen cans")
first.

## Caveats

- Switches that drive lights are **switches** to Alexa, so "turn off the lights" in a room may skip
  them. Use HA's *Show as light* if you want them included.
- Devices with no HA area can't be placed. Give them an area first.
- Alexa lists are also offered as HA to-do entities when Amazon still serves them. On many accounts
  it doesn't, and the integration then logs a warning and creates none.
- **Alexa Media Player login:** if the AMP login fails with "failed to login", restart HA and
  finish the AMP login in the **first** attempt after the restart. This works around an AMP bug
  where a retry reuses the first attempt's login proxy (see
  [alexa_media_player#3559](https://github.com/alandtse/alexa_media_player/issues/3559)).

## Changes in this fork

- **v0.4.0:** devices are listed via GraphQL, matched by HA's `domain#object_id` ids and by alias.
  The original's `/api/phoenix` lookup returns nothing on current accounts.
- **v0.4.1:** `sync` / `sync_area` actually run. They were registered as lambdas HA never awaited.
- **v0.5.0:** *Rooms to skip* area picker.
- **v0.6.0:** full lifecycle (create, update, delete, adopt, keep Echos), plus Preview and Sync
  buttons with notifications.
- **v0.7.0:** removing the integration leaves Alexa alone; *Remove managed rooms* in Configure;
  per-call account selection; *Last result* sensor.
- **v0.8.0:** *Sync one area* in Configure (moved to the device page in 0.9.0).
- **v0.9.0:** *Area to sync* picker and per-area buttons on the device page; *Last result* and the
  picked area survive restarts.
- **v0.9.1:** *Area to sync* lists only areas that get a room.
- **v0.9.2:** `dump_appliances` reports the GraphQL and `/api/phoenix` sources separately.

## Credits & licence

MIT. Original integration by [neilellis](https://github.com/neilellis/ha-alexa-room-sync). Relies
on [alexa_media_player](https://github.com/alandtse/alexa_media_player) /
[alexapy](https://gitlab.com/keatontaylor/alexapy) (Apache-2.0) for authentication. Endpoint
shapes come from [alexa-remote](https://github.com/Apollon77/alexa-remote) (MIT) and community
reverse-engineering. Not affiliated with or endorsed by Amazon.
