"""Constants for the Alexa Room Sync integration."""

from __future__ import annotations

DOMAIN = "alexa_room_sync"

# The integration this one piggy-backs on for an authenticated Alexa session.
ALEXA_MEDIA_DOMAIN = "alexa_media"

# The Assist/Cloud exposure key we mirror. Alexa Room Sync only ever considers
# entities that are exposed to Amazon Alexa (via HA Cloud / Nabu Casa).
ASSISTANT_ALEXA = "cloud.alexa"

# Private Alexa API endpoints (reached through alexapy's authenticated session).
EP_GROUP = "/api/phoenix/group"          # GET list, POST create, DELETE /{id}
EP_PHOENIX = "/api/phoenix"              # GET all smart-home appliances (legacy; null on many accounts)
EP_GRAPHQL = "/nexus/v1/graphql"         # POST smart-home endpoint list (what the Alexa app uses)

# Same query alexapy's get_devices_gql sends; each item's legacyAppliance carries the
# applianceId that /api/phoenix/group expects.
GQL_SMARTHOME_QUERY = """
query CustomerSmartHome {
    endpoints(
      endpointsQueryParams: { paginationParams: { disablePagination: true } }
    ) {
        items {
            legacyAppliance {
                applianceId
                friendlyName
                friendlyDescription
                manufacturerName
                entityId
                aliases
                alexaDeviceIdentifierList
                applianceTypes
            }
        }
    }
}
"""

# Config / options keys
CONF_EMAIL = "email"
CONF_DRY_RUN = "dry_run"
CONF_ROOM_PREFIX = "room_prefix"
CONF_INCLUDE_CLIMATE = "include_climate"
CONF_INCLUDE_CHILD_LOCKS = "include_child_locks"
CONF_INCLUDE_ECHOS = "include_echos"
CONF_AREA_NAME_OVERRIDES = "area_name_overrides"
CONF_CONFIRMED = "confirmed_not_dry_run"
CONF_EXCLUDED_AREAS = "excluded_areas"
CONF_FLOOR_GROUPS = "floor_groups"
CONF_AUTO_SYNC = "auto_sync"

DEFAULT_DRY_RUN = True
DEFAULT_ROOM_PREFIX = ""
DEFAULT_INCLUDE_CLIMATE = True
DEFAULT_INCLUDE_CHILD_LOCKS = False
DEFAULT_INCLUDE_ECHOS = False
DEFAULT_FLOOR_GROUPS = False
DEFAULT_AUTO_SYNC = False

# Auto-sync: wait this long after the last area/device/entity change before syncing,
# so a burst of edits is one sync. The hourly run catches devices Alexa discovers late.
AUTO_SYNC_DELAY_SECONDS = 60
AUTO_SYNC_INTERVAL_HOURS = 1

# Floor groups are kept in the managed-rooms record under this prefix + floor_id.
FLOOR_KEY_PREFIX = "floor:"
ALEXA_MEDIA_PLATFORM = "alexa_media"

# Services
SERVICE_PREVIEW = "preview"
SERVICE_SYNC_AREA = "sync_area"
SERVICE_SYNC = "sync"
SERVICE_LIST_GROUPS = "list_groups"
SERVICE_DUMP_APPLIANCES = "dump_appliances"
SERVICE_REMOVE_MANAGED_ROOMS = "remove_managed_rooms"
SERVICE_PROBE_ENDPOINTS = "probe_endpoints"

ATTR_AREA_ID = "area_id"

# Domains Alexa treats as controllable "devices" that make sense inside a room.
CONTROLLABLE_DOMAINS = {
    "light",
    "switch",
    "fan",
    "cover",
    "climate",
    "lock",
    "media_player",
    "humidifier",
    "water_heater",
    "vacuum",
    "camera",
}

# ── Alexa lists (shopping list / to-do list) ────────────────────────────────────────────
# The same private API, different endpoints. Amazon has been retiring these, so the client
# probes several shapes and the todo platform simply creates no entities if none answer.
EP_LISTS = "/api/namedLists"                                   # GET every list
EP_LIST_ITEMS = "/api/namedLists/{list_id}/items"              # GET items in one list
EP_LIST_ITEM = "/api/namedLists/{list_id}/item"                # POST add an item
EP_LIST_ITEM_ID = "/api/namedLists/{list_id}/item/{item_id}"   # PUT tick off · DELETE remove

SERVICE_PROBE_LISTS = "probe_lists"

# Lists change when someone talks to an Echo, so poll rather than wait for a page load.
# Two minutes keeps "Alexa, add milk" feeling live without hammering an unofficial API.
LIST_SCAN_INTERVAL_SECONDS = 120
