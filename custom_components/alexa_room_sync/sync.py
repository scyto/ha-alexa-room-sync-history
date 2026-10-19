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
    CONF_INCLUDE_CHILD_LOCKS,
    CONF_INCLUDE_CLIMATE,
    CONF_ROOM_PREFIX,
    CONTROLLABLE_DOMAINS,
    DEFAULT_INCLUDE_CHILD_LOCKS,
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

        if only_area_id and area_id != only_area_id:
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

    return SyncPlan(
        rooms=list(rooms.values()),
        existing_group_names=existing_names,
        total_exposed=total_exposed,
        total_in_area=total_in_area,
    )


async def apply_plan(
    hass: HomeAssistant,
    login,
    plan: SyncPlan,
) -> dict[str, str]:
    """Create groups for rooms that don't already exist. Returns name -> result.

    Conservative by design: rooms whose name already exists in Alexa are SKIPPED
    (never duplicated, never edited, never deleted). Empty rooms are skipped.
    """
    results: dict[str, str] = {}
    for room in plan.rooms:
        if not room.appliance_ids:
            results[room.room_name] = "skipped: no matched devices"
            continue
        if room.room_name in plan.existing_group_names:
            results[room.room_name] = (
                "skipped: a group with this name already exists (not modified)"
            )
            continue
        try:
            resp = await ac.create_group(login, room.room_name, room.appliance_ids)
            gid = ""
            if isinstance(resp, dict):
                gid = resp.get("groupId") or resp.get("id") or ""
            results[room.room_name] = (
                f"created with {len(room.appliance_ids)} device(s)"
                + (f" (groupId={gid})" if gid else "")
            )
            _LOGGER.info(
                "Alexa Room Sync: created room '%s' (%d devices)%s",
                room.room_name,
                len(room.appliance_ids),
                f" groupId={gid}" if gid else "",
            )
        except ac.AlexaClientError as err:
            results[room.room_name] = f"ERROR: {err}"
            _LOGGER.error("Alexa Room Sync: failed to create '%s': %s", room.room_name, err)
    return results
