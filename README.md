# Alexa Room Sync

> **Archived: development history only.** Alexa Room Sync now lives at
> **[scyto/ha-alexa-room-sync](https://github.com/scyto/ha-alexa-room-sync)**, a standalone repo
> with the same commits. This repo is kept read-only for its pull requests (#1–#23) and their
> reviews. Don't install from here.

Keep your **Alexa groups** in step with your **Home Assistant areas**. There is one Alexa group per
HA area, holding every Alexa-exposed HA device in that area, so "Alexa, turn off the kitchen
lights" matches how HA is organised. Groups are created, updated when an area's devices change or
the area is renamed, and removed when you skip or delete the area.

> **This is a fork** of [neilellis/ha-alexa-room-sync](https://github.com/neilellis/ha-alexa-room-sync).
> It fixes device matching (the original's device list came back empty), the `sync` services,
> and adds the full group lifecycle, buttons and an area picker. See [Changes in this fork](#changes-in-this-fork).

Nabu Casa / HA Cloud exposes your devices to Alexa but can't create Alexa groups. This fills that
gap through the **unofficial Alexa API**, using the private endpoints the Alexa app and
[Alexa Media Player](https://github.com/alandtse/alexa_media_player) use.

> ⚠️ **This uses an undocumented Amazon API.** Amazon can change it without notice. Keep dry-run on
> until you've looked at a preview, and test one area first.

## Requirements

- Home Assistant. This fork is tested on 2026.10.
- A signed-in Alexa account in either:
  - **Alexa Devices**, Home Assistant's built-in integration (recommended: its login is a normal
    setup screen with a one-time code), or
  - **Alexa Media Player** (HACS).

  This integration borrows that Amazon session and never asks for your password itself. If both
  are signed in to the same account, it uses Alexa Devices.

  > Alexa Devices in HA 2026.10.2 can't sign in since Amazon changed its sign-in pages: setup
  > spins on "Logging in and scanning" ([aioamazondevices#1188](https://github.com/chemelli74/aioamazondevices/issues/1188),
  > fixed in aioamazondevices 16.9.1). Until your HA ships that, use Alexa Media Player.
- Your HA devices **exposed to Alexa through HA Cloud** (Settings > Voice assistants > Expose) and
  discovered in the Alexa app.

## Install

| Step | With My Home Assistant | By hand |
|---|---|---|
| 1. Get it from HACS | [![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=scyto&repository=ha-alexa-room-sync&category=integration) | HACS > ⋮ > **Custom repositories**, add `https://github.com/scyto/ha-alexa-room-sync` as an **Integration** |
| 2. Download | **Download** in HACS, then restart HA | Same |
| 3. Set it up | [![Add Alexa Room Sync to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=alexa_room_sync) | **Settings > Devices & services > Add integration > Alexa Room Sync** |

Then pick your Alexa account. After installing or updating, reload the browser (F5) if menu or
field labels show up blank.

## Using it

Everything is read-only until you turn writes on.

1. On the integration's device page, press **Whole house – Preview**. The summary appears in
   **Last result**, and the device's **Activity** lists, by name, the devices each Alexa group
   would gain or lose.
2. To allow writes, go to **Configure > Settings**, turn **Dry-run** off and tick **I understand
   'apply' will write to my Alexa account**.
3. Try one area first: pick it in **One area** and press **One area – Sync**.
4. Then press **Whole house – Sync** to do the rest. Groups can take a minute or two to show up in the
   Alexa app; pull to refresh or reopen the app.

So that "turn on the lights" works without saying the area name, each Echo needs to be in its
area's group. Either turn on **Put Echos in their area's Alexa group** (it uses the area each Echo
has in Alexa Devices or Alexa Media Player), or add them by hand in the Alexa app. Echos you add by hand are kept.

### Device page

| Control | What it does |
|---|---|
| **One area** | Picks an HA area. Lists only areas that get an Alexa group (something exposed to Alexa, not skipped). Follows renames. |
| **One area – Preview** | Shows what syncing that area would change. Never writes. |
| **One area – Sync** | Syncs only that area's Alexa group. |
| **Whole house – Preview** | Shows what syncing every area would change. Never writes. |
| **Whole house – Sync** | Syncs every area. |
| **Last result** | One-line summary of the last run. Each run also adds an entry to the device's **Activity** listing what changed in each Alexa group, and the latest run's detail is in the sensor's attributes (`scope`, `mode`, `ran_at`, `groups`). |

Both Sync buttons write only when dry-run is off and writes are confirmed; otherwise they preview.
HA lists the controls by name, which keeps the *One area* controls together, followed by
*Whole house*.

**Wording:** an **area** is Home Assistant's; an **Alexa group** is what this creates for it in Alexa.

### What a sync changes

The integration remembers which Alexa groups it created or adopted, and which devices it put in each, keyed by
HA area. A full sync then:

- **creates** a group for an area that doesn't have one;
- **updates** a managed group when the area's devices change or the area is renamed;
- **deletes** a managed group when its area is skipped, deleted, or has no exposed devices left.
  If the group also holds something you added yourself, such as an Echo, the HA devices are taken
  out, your additions stay, and the group is released instead of deleted.

A single-area sync never touches other groups. Groups the integration didn't create are left alone,
with one exception: an Alexa group with exactly the area's name, in an area nothing manages yet, is
adopted. Only Home Assistant devices are matched, plus Echos if you turn that option on. Nothing else is added.

As safety checks, a sync refuses to run if Alexa returns no devices, or no groups while groups are
managed. Without that, a glitch on Amazon's side could look like "everything is gone".

### Removing it

Removing the integration **doesn't change Alexa**. Your groups keep working, and a reinstall picks
them up again by name. Only the integration's saved record of its groups is deleted. Its settings
go too: after re-adding it, dry-run is back on, so turn it off again before syncing.

To take the groups down too, first use **Configure > Remove the Alexa groups it manages**, which asks
you to confirm. It removes the groups it manages, which are the ones it created **plus any it
adopted** (an existing Alexa group with exactly an area's name). It keeps any device it didn't put
there. So an adopted group you made by hand is deleted too if it only held HA devices. Rename such
a group in Alexa before a sync if you want it left alone.

## Options (Configure > Settings)

- **Dry-run** (on by default): blocks all writes.
- **I understand 'apply' will write to my Alexa account**: must be ticked for any write.
- **Alexa group name prefix**: for example `HA `, to tell synced groups apart.
- **Include climate entities** (on by default): thermostats go in their area's group.
- **Include child-lock switches** (off by default).
- **Put Echos in their area's Alexa group** (off by default): each Echo that has an HA area joins
  that area's group, matched by serial number. The areas come from whichever integration the
  session comes from (Alexa Devices or Alexa Media Player). Phone apps and speaker groups are
  skipped. Echos placed this way are managed like HA devices: moved when you change their area, and
  removed by *Remove the Alexa groups it manages*.
- **Also make one Alexa group per Home Assistant floor** (off by default): each HA floor
  gets a group holding the HA devices of its synced areas, so "Alexa, turn off the ground
  floor" works. Floors with nothing synced get no group, Echos stay only in their area's
  group, and a floor named the same as an area's group is skipped. Turning this off
  removes the floor groups on the next whole-house sync.
- **Floor groups: lights and light switches only** (on by default): floor groups hold only
  lights and switches, so "Alexa, turn off the ground floor" doesn't also turn off thermostats,
  purifiers or other devices. Turn it off to give floor groups everything their areas have.
- **Sync automatically** (off by default): runs a whole-house sync a minute after you
  change areas, floors, devices or what's exposed to Alexa, and once an hour to catch
  devices Alexa discovers late. It only writes when dry-run is off and the confirmation is
  ticked, and it only adds to Activity when something changed.
- **Areas to skip**: areas that never get an Alexa group from a whole-house sync and don't appear
  in *One area*. If a skipped area already has an Alexa group from this integration, the next
  whole-house sync removes it. The `sync_area` action can still target a skipped area directly.

## Actions (services)

For scripts and automations. With more than one Alexa account set up, pass `config_entry_id`.

| Action | Writes? | Notes |
|---|---|---|
| `alexa_room_sync.preview` | No | Plan and match details. Optional `area_id`. |
| `alexa_room_sync.sync` | With `apply: true` and the confirmation | Full sync. |
| `alexa_room_sync.sync_area` | With `apply: true` and the confirmation | One area. |
| `alexa_room_sync.list_groups` | No | Lists the groups in your Alexa account and the devices in each. |
| `alexa_room_sync.remove_managed_rooms` | With `confirm: true` | Removes the rooms it created. |
| `alexa_room_sync.dump_appliances`, `probe_endpoints`, `probe_lists` | No | Diagnostics. |

## How it works

- **Login:** it reuses an already signed-in session, so no credentials live here: the HTTP
  wrapper of Alexa Devices' `aioamazondevices` API, or Alexa Media Player's `AlexaLogin` and
  alexapy's request helper. Both are private to those integrations, so an update to either can
  break this until it's adapted.
- **Devices:** it lists Alexa's smart-home devices with the Alexa app's GraphQL query
  (`POST /nexus/v1/graphql`), falling back to `GET /api/phoenix`. It matches each exposed HA entity
  by the entity_id that HA Cloud embeds in the Alexa device id (`…_light#kitchen_cans`). Otherwise
  it matches by name, where the first alias is what HA sends Alexa as the name.
- **Groups:** `GET /api/phoenix/group` to list, `POST` to create, `PUT /api/phoenix/group/{id}` to
  update (it sends Amazon's own group document back with the name and members changed), and
  `DELETE` to remove.
- **Memory:** the managed groups and their members are kept in HA storage
  (`.storage/alexa_room_sync.<entry>.rooms`).

## Naming tips

Groups take the HA area's name. If two areas sound alike ("Kitchen" and "Kitchenette"), rename one
or use a prefix, otherwise Alexa will ask "which one?". Device names come from HA too: since HA
2026.8 Alexa gets an entity's **first alias**, so put the area-qualified name ("kitchen cans")
first.

## Caveats

- Switches that drive lights are **switches** to Alexa, so "turn off the lights" in a group may skip
  them. Use HA's *Show as light* if you want them included.
- Devices with no HA area can't be placed. Give them an area first.
- Alexa lists are also offered as HA to-do entities when Amazon still serves them. On many accounts
  it doesn't, and the integration then logs a warning and creates none.
- **Alexa Media Player login:** if you use AMP and its login fails with "failed to login", restart HA and
  finish the AMP login in the **first** attempt after the restart. This works around an AMP bug
  where a retry reuses the first attempt's login proxy (see
  [alexa_media_player#3559](https://github.com/alandtse/alexa_media_player/issues/3559)).

## Changes in this fork

- **v0.4.0:** devices are listed via GraphQL, matched by HA's `domain#object_id` ids and by alias.
  The original's `/api/phoenix` lookup returns nothing on current accounts.
- **v0.4.1:** `sync` / `sync_area` actually run. They were registered as lambdas HA never awaited.
- **v0.5.0:** *Rooms to skip* area picker.
- **v0.6.0:** full lifecycle (create, update, delete, adopt, keep Echos), plus Preview and Sync
  buttons (results went to a notification until 0.10.2).
- **v0.7.0:** removing the integration leaves Alexa alone; *Remove managed rooms* in Configure;
  per-call account selection; *Last result* sensor.
- **v0.8.0:** *Sync one area* in Configure (moved to the device page in 0.9.0).
- **v0.9.0:** *Area to sync* picker and per-area buttons on the device page; *Last result* and the
  picked area survive restarts.
- **v0.9.1:** *Area to sync* lists only areas that get an Alexa group.
- **v0.9.2:** `dump_appliances` reports the GraphQL and `/api/phoenix` sources separately.
- **v0.9.3:** removing the integration also removes the `probe_endpoints` action.
- **v0.10.0:** clearer names: *One area* / *Whole house* controls, "area" for HA and "Alexa group" for what's created.
- **v0.10.1:** *Last result* keeps each run's per-group outcome as attributes, viewable in History.
- **v0.10.2:** each run's per-group changes are written to the Activity (logbook); the notification is gone.
- **v0.11.0:** optional *Put Echos in their area's Alexa group*, using Alexa Media Player's areas.
- **v0.11.1:** Echos: only the Echo speaker device is added to a group (a tablet's AlexaBridge endpoint made Amazon reject the update).
- **v0.12.0:** Activity and *Last result* name the devices each run added, removed or deleted, not just counts; `list_groups` lists each group's devices.
- **v0.13.0:** optional floor groups and auto-sync; Add to Home Assistant buttons in the README.
- **v0.14.0:** uses Home Assistant's built-in Alexa Devices integration instead of Alexa Media Player.
- **v0.14.1:** Alexa Media Player works again as a fallback; Alexa Devices is used when it's signed in to the account.
- **v0.14.2:** an Echo that Alexa lists in a group by serial counts as already there, so it no longer shows as added on every sync; such entries show as "<Echo> (by serial)".
- **v0.14.3:** floor groups hold only lights and light switches by default; the "Alexa lists unavailable" message is logged once at info level instead of a warning on every start.

## Credits & licence

MIT. Original integration by [neilellis](https://github.com/neilellis/ha-alexa-room-sync). Relies
on [alexa_media_player](https://github.com/alandtse/alexa_media_player) /
[alexapy](https://gitlab.com/keatontaylor/alexapy) (Apache-2.0) for authentication. Endpoint
shapes come from [alexa-remote](https://github.com/Apollon77/alexa-remote) (MIT) and community
reverse-engineering. Not affiliated with or endorsed by Amazon.
