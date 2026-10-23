"""Build the HA area -> Alexa room plan and (optionally) apply it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from . import alexa_client as ac
from .const import (
    ASSISTANT_ALEXA,
    CONF_AREA_NAME_OVERRIDES,
    CONF_EXCLUDED_AREAS,
    CONF_INCLUDE_CHILD_LOCKS,
    CONF_INCLUDE_ECHOS,
    CONF_INCLUDE_CLIMATE,
    CONF_ROOM_PREFIX,
    CONTROLLABLE_DOMAINS,
    DEFAULT_INCLUDE_CHILD_LOCKS,
    DEFAULT_INCLUDE_ECHOS,
    ALEXA_MEDIA_PLATFORM,
    DEFAULT_INCLUDE_CLIMATE,
    DEFAULT_ROOM_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PlannedRoom:
    """A room we intend to create/update in Alexa, plus its match diagnostics."""

    area_id: str
    room_name: str
    # entity_id -> friendly_name for entities we want in this room
    wanted: dict[str, str] = field(default_factory=dict)
    # entity_id -> applianceId for the ones we found on the Alexa side
    matched: dict[str, str] = field(default_factory=dict)
    # entities exposed+in-area but NOT found among Alexa appliances
    unmatched: list[str] = field(default_factory=list)

    @property
    def appliance_ids(self) -> list[str]:
        return list(self.matched.values())


@dataclass
class SyncPlan:
    rooms: list[PlannedRoom]
    existing_group_names: list[str]
    total_exposed: int
    total_in_area: int
    groups: list[ac.AlexaGroup] = field(default_factory=list)
    # Every applianceId known to be an HA entity, from any area: by embedded
    # entity_id or by the name fallback. Used to tell HA members from Echos.
    ha_appliance_ids: set[str] = field(default_factory=set)
    # applianceId -> the name Alexa shows for it, so results can say what changed.
    names: dict[str, str] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [
            f"Exposed-to-Alexa entities considered: {self.total_exposed} "
            f"({self.total_in_area} have an HA area).",
            f"Existing Alexa groups: {', '.join(self.existing_group_names) or '(none)'}",
            "",
        ]
        for room in sorted(self.rooms, key=lambda r: r.room_name.lower()):
            exists = " [exists]" if room.room_name in self.existing_group_names else ""
            lines.append(
                f"• {room.room_name}{exists}: "
                f"{len(room.matched)} device(s) to assign, "
                f"{len(room.unmatched)} unmatched"
            )
            for eid, aid in room.matched.items():
                lines.append(f"    ✓ {eid}  ->  {aid}")
            for eid in room.unmatched:
                lines.append(
                    f"    ✗ {eid} ({room.wanted.get(eid, '')}) — no Alexa appliance matched"
                )
        return lines


def _should_expose(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if entity_id is exposed to cloud.alexa."""
    try:
        from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
            async_should_expose,
        )

        return bool(async_should_expose(hass, ASSISTANT_ALEXA, entity_id))
    except Exception:  # noqa: BLE001 - be tolerant across HA versions
        return False


def _area_for_entity(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    dev_reg: dr.DeviceRegistry,
    entity_id: str,
) -> str | None:
    """Resolve an entity's area: its own area, else its device's area."""
    entry = ent_reg.async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = dev_reg.async_get(entry.device_id)
        if device and device.area_id:
            return device.area_id
    return None


# Alexa's own device id for an Echo speaker; the only kind we put in a group.
ECHO_APPLIANCE_PREFIX = "AAA_SonarCloudService_"
def _is_echo_speaker(appliance) -> bool:
    return (
        appliance.appliance_id.startswith(ECHO_APPLIANCE_PREFIX)
        and "ALEXA_VOICE_ENABLED" in appliance.types
    )


# AMP media_players that never have an Alexa smart-home device of their own.
_NON_ECHO_MODELS = ("Speaker Group", "Alexa Mobile Voice")
# Echo serials (upper case) that matched an Alexa device on the last plan, by account.
_MATCHED_ECHO_AREAS: dict[str | None, set[str]] = {}


def _amp_serial(identifier: str) -> str:
    """The raw Echo serial from AMP's device identifier.

    AMP's first account uses the serial as-is; later accounts use
    slugify(f"{serial}_{email}"), lower-cased. Serials are alphanumeric, so the
    part before the first "_" is the serial; compare case-insensitively.
    """
    return identifier.split("_", 1)[0].upper()


def _amp_entry_ids(hass: HomeAssistant, email: str | None) -> set[str] | None:
    """Config entry ids of the Alexa Media Player account for ``email`` (None = any)."""
    if not email:
        return None
    ids = {
        e.entry_id
        for e in hass.config_entries.async_entries(ALEXA_MEDIA_PLATFORM)
        if str(e.data.get("email", "")).lower() == email.lower()
    }
    return ids or None


def _echo_serials_by_area(
    hass: HomeAssistant, email: str | None = None
) -> dict[str, list[tuple[str, str]]]:
    """area_id -> [(entity_id, SERIAL)] for one account's Alexa Media Player Echos.

    Uses the area you gave each Echo in HA. Phone apps and speaker groups are left
    out: they have no Alexa smart-home device to put in a group.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    account = _amp_entry_ids(hass, email)
    out: dict[str, list[tuple[str, str]]] = {}
    for entry in ent_reg.entities.values():
        if entry.platform != ALEXA_MEDIA_PLATFORM or entry.domain != "media_player":
            continue
        if account is not None and entry.config_entry_id not in account:
            continue
        if not entry.device_id:
            continue
        device = dev_reg.async_get(entry.device_id)
        if device is None or str(device.model or "").startswith(_NON_ECHO_MODELS):
            continue
        ident = next(
            (ident for dom, ident in device.identifiers if dom == ALEXA_MEDIA_PLATFORM),
            None,
        )
        area_id = _area_for_entity(hass, ent_reg, dev_reg, entry.entity_id)
        if ident and area_id:
            out.setdefault(area_id, []).append((entry.entity_id, _amp_serial(ident)))
    return out


def _add_echos(hass, options, appliances, rooms, ha_appliance_ids,
               only_area_id, excluded_areas, email=None) -> None:
    """Put each Echo's Alexa device(s) in the group for the HA area it's in."""
    area_reg = ar.async_get(hass)
    by_serial: dict[str, list[str]] = {}
    for appliance in appliances:
        # Only the Echo speaker itself goes in a group. Other Alexa devices that
        # share its serial (an "AlexaBridge_" endpoint for a tablet's Alexa, a
        # built-in sensor or a Zigbee/Matter child on the same driver) can be
        # refused by Amazon and fail the whole update. The speaker is the one that
        # is voice-enabled, which is the test Alexa Media Player uses too.
        if not _is_echo_speaker(appliance):
            continue
        for serial in appliance.serials:
            by_serial.setdefault(serial.upper(), []).append(appliance.appliance_id)
    matched_areas: set[str] = set()
    for area_id, echos in _echo_serials_by_area(hass, email).items():
        if any(by_serial.get(serial) for _, serial in echos):
            matched_areas.add(area_id)
        if only_area_id and area_id != only_area_id:
            continue
        if not only_area_id and area_id in excluded_areas:
            continue
        area = area_reg.async_get_area(area_id)
        if area is None:
            continue
        for entity_id, serial in echos:
            ids = by_serial.get(serial, [])
            if not ids:
                continue  # e.g. a tablet or third-party speaker with no smart-home device
            room = rooms.get(area_id)
            if room is None:
                room = PlannedRoom(area_id=area_id, room_name=_room_name(area.name, options))
                rooms[area_id] = room
            for n, aid in enumerate(ids[:1]):  # one speaker per Echo
                key = entity_id if n == 0 else f"{entity_id}#{n}"
                room.wanted[key] = entity_id
                room.matched[key] = aid
                ha_appliance_ids.add(aid)
    # Remember which areas really have a matchable Echo, so the area picker can
    # list only those (it can't ask Amazon itself).
    _MATCHED_ECHO_AREAS[email] = matched_areas


def syncable_area_ids(
    hass: HomeAssistant, options: dict, email: str | None = None
) -> set[str]:
    """Areas a full sync would give a room: an exposed device of a room-worthy
    domain in it, and not on the skip list. Registry-only, so it's cheap."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    excluded = set(options.get(CONF_EXCLUDED_AREAS) or [])
    areas: set[str] = set()
    for entry in ent_reg.entities.values():
        if not _should_expose(hass, entry.entity_id):
            continue
        area_id = _area_for_entity(hass, ent_reg, dev_reg, entry.entity_id)
        if not area_id or area_id in excluded or area_id in areas:
            continue
        state = hass.states.get(entry.entity_id)
        friendly = (
            state.attributes.get("friendly_name") if state else None
        ) or entry.name or entry.original_name or entry.entity_id
        if _wanted_domain(entry.entity_id, friendly, options):
            areas.add(area_id)
    if options.get(CONF_INCLUDE_ECHOS, DEFAULT_INCLUDE_ECHOS):
        echo_areas = set(_echo_serials_by_area(hass, email))
        if email in _MATCHED_ECHO_AREAS:
            # After a preview or sync, only areas whose Echo matched an Alexa device.
            echo_areas &= _MATCHED_ECHO_AREAS[email]
        areas |= echo_areas - excluded
    return areas


def _room_name(area_name: str, options: dict) -> str:
    overrides = options.get(CONF_AREA_NAME_OVERRIDES) or {}
    name = overrides.get(area_name, area_name)
    prefix = options.get(CONF_ROOM_PREFIX, DEFAULT_ROOM_PREFIX)
    return f"{prefix}{name}".strip()


def _wanted_domain(entity_id: str, friendly: str, options: dict) -> bool:
    domain = entity_id.split(".", 1)[0]
    if domain not in CONTROLLABLE_DOMAINS:
        return False
    if domain == "climate" and not options.get(
        CONF_INCLUDE_CLIMATE, DEFAULT_INCLUDE_CLIMATE
    ):
        return False
    if not options.get(CONF_INCLUDE_CHILD_LOCKS, DEFAULT_INCLUDE_CHILD_LOCKS):
        if "child_lock" in entity_id or "child lock" in friendly.lower():
            return False
    return True


def _match_appliance(
    entity_id: str,
    names: list[str],
    by_entity: dict[str, str],
    by_name: dict[str, str],
) -> str | None:
    """Return an applianceId for this HA entity, or None.

    Primary key: the HA entity_id embedded in the appliance (reliable for Nabu Casa).
    Fallback: exact case-insensitive match on the friendly name or any alias (unique
    names only). HA 2026.8+ sends the first alias to Alexa as the device name.
    """
    if entity_id in by_entity:
        return by_entity[entity_id]
    for name in names:
        key = name.strip().lower()
        if key and key in by_name:
            return by_name[key]
    return None


def _entity_names(entry: er.RegistryEntry, friendly: str) -> list[str]:
    """Aliases (in order, skipping the 'use entity name' placeholder), then the name."""
    aliases = [a for a in (entry.aliases or []) if isinstance(a, str)]
    return [*aliases, friendly]


async def build_plan(
    hass: HomeAssistant,
    login,
    options: dict,
    only_area_id: str | None = None,
) -> SyncPlan:
    """Build a SyncPlan from live HA registries + live Alexa appliances (read-only)."""
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    appliances = await ac.get_appliances(login)
    if not appliances:
        # An empty list means the device lookup failed, not that you have no devices;
        # reconciling against it would delete every managed room.
        raise ac.AlexaClientError("Alexa returned no smart-home devices; not syncing")
    by_entity = {a.entity_id: a.appliance_id for a in appliances if a.entity_id}
    by_name: dict[str, str] = {}
    name_counts: dict[str, int] = {}
    for a in appliances:
        key = a.friendly_name.strip().lower()
        if not key:
            continue
        name_counts[key] = name_counts.get(key, 0) + 1
        by_name[key] = a.appliance_id
    # Drop ambiguous (non-unique) friendly names from the name index.
    by_name = {k: v for k, v in by_name.items() if name_counts.get(k) == 1}

    existing = await ac.get_groups(login)
    existing_names = [g.name for g in existing]

    rooms: dict[str, PlannedRoom] = {}
    ha_appliance_ids: set[str] = set(by_entity.values())
    excluded_areas = set(options.get(CONF_EXCLUDED_AREAS) or [])
    total_exposed = 0
    total_in_area = 0

    for entry in ent_reg.entities.values():
        entity_id = entry.entity_id
        if not _should_expose(hass, entity_id):
            continue
        total_exposed += 1

        state = hass.states.get(entity_id)
        friendly = (
            state.attributes.get("friendly_name") if state else None
        ) or entry.name or entry.original_name or entity_id

        area_id = _area_for_entity(hass, ent_reg, dev_reg, entity_id)
        if not area_id:
            continue
        total_in_area += 1

        name_match = _match_appliance(
            entity_id, _entity_names(entry, friendly), by_entity, by_name
        )
        if name_match:
            ha_appliance_ids.add(name_match)

        if only_area_id and area_id != only_area_id:
            continue
        # An explicit sync_area wins; the skip list only applies to a full sync.
        if not only_area_id and area_id in excluded_areas:
            continue
        if not _wanted_domain(entity_id, friendly, options):
            continue

        area = area_reg.async_get_area(area_id)
        if area is None:
            continue
        room_name = _room_name(area.name, options)

        room = rooms.get(area_id)
        if room is None:
            room = PlannedRoom(area_id=area_id, room_name=room_name)
            rooms[area_id] = room
        room.wanted[entity_id] = friendly

        aid = _match_appliance(
            entity_id, _entity_names(entry, friendly), by_entity, by_name
        )
        if aid:
            room.matched[entity_id] = aid
        else:
            room.unmatched.append(entity_id)

    if options.get(CONF_INCLUDE_ECHOS, DEFAULT_INCLUDE_ECHOS):
        _add_echos(hass, options, appliances, rooms, ha_appliance_ids,
                   only_area_id, excluded_areas, email=getattr(login, "email", None))

    return SyncPlan(
        rooms=list(rooms.values()),
        existing_group_names=existing_names,
        total_exposed=total_exposed,
        total_in_area=total_in_area,
        groups=existing,
        ha_appliance_ids=ha_appliance_ids,
        names={a.appliance_id: a.friendly_name for a in appliances if a.friendly_name},
    )


CREATE = "create"
UPDATE = "update"
DELETE = "delete"
UNCHANGED = "unchanged"
SKIP = "skip"
FORGET = "forget"


def device_label(appliance_id: str, names: dict[str, str]) -> str:
    """A readable name for an Alexa applianceId: Alexa's own name for it, else the
    HA entity_id it carries, else the id itself."""
    return (
        names.get(appliance_id)
        or ac.entity_id_from_appliance_id(appliance_id)
        or appliance_id
    )


@dataclass
class RoomAction:
    """One reconcile step for one HA area."""

    kind: str
    area_id: str
    room_name: str
    group_id: str = ""
    appliance_ids: list[str] = field(default_factory=list)  # full member list to write
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed_from: str = ""
    adopted: bool = False
    release: bool = False  # stop managing the room after this update
    members: list[str] = field(default_factory=list)  # HA members we leave in it
    note: str = ""
    # applianceId -> Alexa name, for the devices this action adds or removes
    labels: dict[str, str] = field(default_factory=dict)

    def _names(self, appliance_ids: list[str]) -> str:
        return ", ".join(device_label(a, self.labels) for a in appliance_ids)

    def describe(self) -> str:
        if self.kind == CREATE:
            return (f"create with {len(self.appliance_ids)} device(s): "
                    f"{self._names(self.appliance_ids)}")
        if self.kind == UPDATE and self.release:
            return (f"remove {len(self.removed)} HA device(s) "
                    f"({self._names(self.removed)}), keep "
                    f"{len(self.appliance_ids)} other(s), stop managing")
        if self.kind == UPDATE:
            bits = []
            if self.added:
                bits.append(f"added {self._names(self.added)}")
            if self.removed:
                bits.append(f"removed {self._names(self.removed)}")
            if self.renamed_from:
                bits.append(f"renamed from '{self.renamed_from}'")
            return "update: " + ("; ".join(bits) or "(no member change)")
        if self.kind == DELETE:
            text = "delete" + (f" ({self.note})" if self.note else "")
            if self.removed:
                text += f", had {self._names(self.removed)}"
            return text
        if self.kind == FORGET:
            return "forget (group already gone from Alexa)"
        if self.kind == UNCHANGED:
            return "unchanged" + (" (adopted)" if self.adopted else "")
        return f"skip ({self.note})"


def _ha_member_test(plan: SyncPlan, record: dict | None):
    """Return a predicate telling HA-entity members of a group from everything else.

    An appliance counts as HA if its id carries an entity_id, if the plan matched it to
    an HA entity (including by name), or if this integration put it in the room last
    time. The last case catches entities that have since been unexposed.
    """
    known = set(plan.ha_appliance_ids) | set((record or {}).get("members", []))

    def is_ha(appliance_id: str) -> bool:
        return (
            appliance_id in known
            or ac.entity_id_from_appliance_id(appliance_id) is not None
        )

    return is_ha


def reconcile(
    plan: SyncPlan,
    managed: dict[str, dict],
    only_area_id: str | None = None,
) -> list[RoomAction]:
    """Work out create/update/delete steps. Pure: no I/O, so it can be tested offline.

    ``managed`` maps area_id -> {"group_id", "name"} for the rooms this integration
    created (or adopted). Only those are ever updated or deleted; a hand-made group
    is only touched if it has exactly the room's name and nothing manages that area
    yet, in which case it is adopted. Non-HA members of a group (Echos you added in
    the Alexa app) are always kept.
    """
    by_id = {g.group_id: g for g in plan.groups}
    by_name = {g.name: g for g in plan.groups}
    claimed = {m.get("group_id") for m in managed.values()}
    actions: list[RoomAction] = []

    for room in plan.rooms:
        record = managed.get(room.area_id)
        group = by_id.get(record["group_id"]) if record else None
        adopted = False
        if group is None:
            candidate = by_name.get(room.room_name)
            if candidate is not None and candidate.group_id not in claimed:
                group, adopted = candidate, True
        want = list(dict.fromkeys(room.appliance_ids))

        if group is None:
            if want:
                actions.append(RoomAction(CREATE, room.area_id, room.room_name,
                                          appliance_ids=want, members=want))
            else:
                actions.append(RoomAction(SKIP, room.area_id, room.room_name,
                                          note="no matched devices"))
            continue

        is_ha = _ha_member_test(plan, record)
        others = [a for a in group.appliance_ids if not is_ha(a)]
        have = [a for a in group.appliance_ids if is_ha(a)]
        added = [a for a in want if a not in have]
        removed = [a for a in have if a not in want]
        renamed_from = group.name if group.name != room.room_name else ""

        if not want and not others:
            actions.append(RoomAction(DELETE, room.area_id, room.room_name,
                                      group_id=group.group_id, adopted=adopted,
                                      removed=list(group.appliance_ids),
                                      note="area has no matched devices"))
        elif added or removed or renamed_from:
            actions.append(RoomAction(UPDATE, room.area_id, room.room_name,
                                      group_id=group.group_id,
                                      appliance_ids=others + want, added=added,
                                      removed=removed, renamed_from=renamed_from,
                                      adopted=adopted, members=want))
        else:
            actions.append(RoomAction(UNCHANGED, room.area_id, room.room_name,
                                      group_id=group.group_id, adopted=adopted,
                                      members=want))

    if only_area_id is None:
        planned = {r.area_id for r in plan.rooms}
        for area_id, record in managed.items():
            if area_id in planned:
                continue
            group = by_id.get(record.get("group_id"))
            name = record.get("name", area_id)
            if group is None:
                actions.append(RoomAction(FORGET, area_id, name,
                                          group_id=record.get("group_id", "")))
                continue
            is_ha = _ha_member_test(plan, record)
            others = [a for a in group.appliance_ids if not is_ha(a)]
            if others:
                # Something you added by hand (an Echo) is in it: strip our devices,
                # leave the room to you.
                actions.append(RoomAction(
                    UPDATE, area_id, group.name, group_id=group.group_id,
                    appliance_ids=others,
                    removed=[a for a in group.appliance_ids if is_ha(a)],
                    release=True,
                ))
            else:
                actions.append(RoomAction(DELETE, area_id, group.name,
                                          group_id=group.group_id,
                                          removed=list(group.appliance_ids),
                                          note="area skipped, removed or empty"))
    for action in actions:
        action.labels = plan.names
    return actions


def teardown(groups: list[ac.AlexaGroup], managed: dict[str, dict]) -> list[RoomAction]:
    """Steps to undo every managed room: delete it, or strip HA devices if it holds
    something you added yourself (an Echo), then stop managing it."""
    by_id = {g.group_id: g for g in groups}
    plan = SyncPlan(rooms=[], existing_group_names=[], total_exposed=0,
                    total_in_area=0, groups=groups)
    actions: list[RoomAction] = []
    for area_id, record in managed.items():
        group = by_id.get(record.get("group_id"))
        if group is None:
            actions.append(RoomAction(FORGET, area_id, record.get("name", area_id),
                                      group_id=record.get("group_id", "")))
            continue
        # Take out only what this integration put there. Anything else, including
        # an HA device you added by hand in the Alexa app, stays. Rooms recorded
        # before members were tracked fall back to "anything with an HA id".
        if "members" in record:
            ours = set(record["members"])
            is_ours = ours.__contains__
        else:
            is_ours = _ha_member_test(plan, record)
        others = [a for a in group.appliance_ids if not is_ours(a)]
        if others:
            actions.append(RoomAction(
                UPDATE, area_id, group.name, group_id=group.group_id,
                appliance_ids=others,
                removed=[a for a in group.appliance_ids if is_ours(a)], release=True,
            ))
        else:
            actions.append(RoomAction(DELETE, area_id, group.name,
                                      group_id=group.group_id,
                                      removed=list(group.appliance_ids), note="teardown"))
    return actions


async def apply_actions(
    login,
    plan: SyncPlan,
    actions: list[RoomAction],
    managed: dict[str, dict],
) -> dict[str, str]:
    """Carry out the actions, updating ``managed`` in place. Returns name -> result."""
    by_id = {g.group_id: g for g in plan.groups}
    results: dict[str, str] = {}
    for action in actions:
        label = action.room_name
        try:
            if action.kind == CREATE:
                resp = await ac.create_group(login, action.room_name, action.appliance_ids)
                gid = ""
                if isinstance(resp, dict):
                    gid = resp.get("groupId") or resp.get("id") or ""
                if not gid:
                    # The create call doesn't always echo the id; look it up by name.
                    for group in await ac.get_groups(login):
                        if group.name == action.room_name:
                            gid = group.group_id
                            break
                if gid:
                    managed[action.area_id] = {"group_id": gid, "name": action.room_name,
                                               "members": action.members}
                results[label] = action.describe().replace("create", "created", 1)
                if not gid:
                    results[label] += " (no groupId returned; adopted by name on next sync)"
            elif action.kind == UPDATE:
                await ac.update_group(login, by_id[action.group_id], action.room_name,
                                      action.appliance_ids)
                if action.release:
                    managed.pop(action.area_id, None)
                    results[label] = action.describe()
                    continue
                managed[action.area_id] = {"group_id": action.group_id,
                                           "name": action.room_name,
                                           "members": action.members}
                results[label] = action.describe().replace("update", "updated", 1)
            elif action.kind == DELETE:
                await ac.delete_group(login, action.group_id)
                managed.pop(action.area_id, None)
                results[label] = action.describe().replace("delete", "deleted", 1)
            elif action.kind == FORGET:
                managed.pop(action.area_id, None)
                results[label] = action.describe()
            elif action.kind == UNCHANGED:
                managed[action.area_id] = {"group_id": action.group_id,
                                           "name": action.room_name,
                                           "members": action.members}
                results[label] = action.describe()
            else:
                results[label] = action.describe()
        except ac.AlexaClientError as err:
            results[label] = f"ERROR: {err}"
            _LOGGER.error("Alexa Room Sync: %s '%s' failed: %s",
                          action.kind, action.room_name, err)
        else:
            _LOGGER.info("Alexa Room Sync: %s: %s", label, results[label])
    return results
