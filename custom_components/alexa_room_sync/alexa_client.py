"""Thin client over the *unofficial* Alexa API for smart-home group management.

We do NOT authenticate ourselves. Instead we borrow the live, already-authenticated
``AlexaLogin`` object created by the ``alexa_media`` integration and drive alexapy's
``AlexaAPI._static_request`` primitive. That primitive already handles the session
cookie, the ``csrf`` header and OAuth token refresh, so no credentials, cookies or the
2FA login dance ever live in this integration.

Endpoints (private consumer Alexa API, reached at ``https://alexa.<amazon-domain>``):

* ``GET  /api/phoenix``          -> every smart-home appliance (applianceId, friendlyName, entityId)
* ``GET  /api/phoenix/group``    -> existing groups/rooms
* ``POST /api/phoenix/group``    -> create a group  {"applianceIds": [...], "name": "..."}
* ``DELETE /api/phoenix/group/{id}`` -> delete a group (undo)

The create/delete shape is the one proven by imduffy15's gist and used by the Alexa app;
alexa-remote (node) confirms the GET/DELETE shapes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    ALEXA_MEDIA_DOMAIN,
    EP_GRAPHQL,
    EP_GROUP,
    EP_LIST_ITEM,
    EP_LIST_ITEM_ID,
    EP_LIST_ITEMS,
    EP_LISTS,
    EP_PHOENIX,
    GQL_SMARTHOME_QUERY,
)

_LOGGER = logging.getLogger(__name__)


class AlexaClientError(Exception):
    """Raised when the Alexa side cannot be reached or returns an error."""


@dataclass
class Appliance:
    """A smart-home appliance as Alexa knows it."""

    appliance_id: str
    friendly_name: str
    entity_id: str | None  # HA entity_id if the appliance carries one (Nabu Casa does)


@dataclass
class AlexaGroup:
    """An existing Alexa group/room."""

    group_id: str
    name: str
    appliance_ids: list[str]
    raw: dict = field(default_factory=dict)  # Amazon's own document, echoed back on update


def get_login_obj(hass: HomeAssistant, email: str | None = None):
    """Return the authenticated alexapy AlexaLogin from the alexa_media integration.

    If ``email`` is None, return the first configured account. Raises AlexaClientError
    if alexa_media isn't set up or the account/session is missing.
    """
    accounts = (
        hass.data.get(ALEXA_MEDIA_DOMAIN, {}).get("accounts", {})
        if isinstance(hass.data.get(ALEXA_MEDIA_DOMAIN), dict)
        else {}
    )
    if not accounts:
        raise AlexaClientError(
            "alexa_media has no configured accounts. Install and log in to the "
            "Alexa Media Player integration first."
        )
    if email is None:
        email = next(iter(accounts))
    account = accounts.get(email)
    if not account:
        raise AlexaClientError(
            f"No alexa_media account for '{email}'. Available: {list(accounts)}"
        )
    login = account.get("login_obj")
    if login is None:
        raise AlexaClientError(f"alexa_media account '{email}' has no login object yet.")
    status = getattr(login, "status", {}) or {}
    if not status.get("login_successful"):
        raise AlexaClientError(
            f"alexa_media account '{email}' is not logged in "
            "(re-authenticate Alexa Media Player)."
        )
    return login


def list_configured_emails(hass: HomeAssistant) -> list[str]:
    """Return the emails alexa_media currently has configured."""
    data = hass.data.get(ALEXA_MEDIA_DOMAIN)
    if not isinstance(data, dict):
        return []
    return list(data.get("accounts", {}))


async def _request(login, method: str, uri: str, data: dict | None = None) -> Any:
    """Call the private Alexa API via alexapy's authenticated primitive."""
    # Imported lazily: alexapy is provided at runtime by alexa_media's requirements.
    from alexapy import AlexaAPI  # noqa: PLC0415

    try:
        resp = await AlexaAPI._static_request(  # noqa: SLF001 - the documented reuse path
            method, login, uri, data=data
        )
    except Exception as err:  # noqa: BLE001 - surface any alexapy/login failure uniformly
        raise AlexaClientError(f"{method.upper()} {uri} failed: {err}") from err
    if resp is None:
        raise AlexaClientError(f"{method.upper()} {uri} returned no response")
    if getattr(resp, "status", 200) >= 400:
        text = ""
        try:
            text = await resp.text()
        except Exception:  # noqa: BLE001
            pass
        raise AlexaClientError(
            f"{method.upper()} {uri} -> HTTP {resp.status}: {text[:300]}"
        )
    if method.lower() == "delete":
        return True
    try:
        return await resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _walk_appliance_dicts(obj: Any):
    """Yield every dict that looks like an appliance record.

    The ``/api/phoenix`` payload nests appliances deeply under stringified
    ``networkDetail`` -> locationDetails -> amazonBridgeDetails -> applianceDetails.
    Rather than hard-code the (occasionally-changing) path, walk the whole tree and
    pick out anything carrying an ``applianceId``.
    """
    if isinstance(obj, dict):
        if "applianceId" in obj and isinstance(obj.get("applianceId"), str):
            yield obj
        for value in obj.values():
            yield from _walk_appliance_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_appliance_dicts(value)


def _extract_entity_id(appliance: dict) -> str | None:
    """Best-effort pull of the HA entity_id an Alexa appliance was built from.

    Nabu Casa appliances carry the HA entity_id, most reliably inside
    ``additionalApplianceDetails`` but sometimes as a ``domain.object_id``-shaped
    ``entityId``/alias. We only accept values that look like an HA entity_id.
    """

    def looks_like_entity_id(val: Any) -> str | None:
        if isinstance(val, str) and "." in val and " " not in val:
            head = val.split(".", 1)[0]
            if head.isidentifier():
                return val
        return None

    details = appliance.get("additionalApplianceDetails")
    if isinstance(details, dict):
        for key in ("entity_id", "entityId", "id"):
            found = looks_like_entity_id(details.get(key))
            if found:
                return found
    found = looks_like_entity_id(appliance.get("entityId"))
    if found:
        return found
    # HA's Alexa endpointId is the entity_id with "." swapped for "#"
    # (alexa.config.generate_alexa_id); Amazon embeds it at the end of the applianceId.
    for key in ("applianceId", "entityId"):
        found = _entity_id_from_alexa_id(appliance.get(key))
        if found:
            return found
    return None


_ALEXA_ID_RE = re.compile(r"(?:^|[_:])([a-z][a-z0-9_]*)#([a-z0-9_]+)$")


def _entity_id_from_alexa_id(val: Any) -> str | None:
    """Turn ``..._light#kitchen_cans`` into ``light.kitchen_cans``."""
    if not isinstance(val, str):
        return None
    match = _ALEXA_ID_RE.search(val)
    if not match:
        return None
    head, object_id = match.groups()
    # The applianceId prefix is joined with "_", so the captured head can carry part of
    # it; keep the longest known domain it ends with.
    domains = [d for d in _KNOWN_DOMAINS if head == d or head.endswith("_" + d)]
    if not domains:
        return None
    return f"{max(domains, key=len)}.{object_id}"


_KNOWN_DOMAINS = {
    "alarm_control_panel", "binary_sensor", "button", "camera", "climate", "cover",
    "event", "fan", "group", "humidifier", "image_processing", "input_boolean",
    "input_button", "input_number", "light", "lock", "media_player", "number",
    "scene", "script", "sensor", "switch", "timer", "vacuum", "valve", "water_heater",
}


async def _fetch_appliance_tree(login) -> Any:
    """Fetch /api/phoenix and return the parsed appliance tree.

    /api/phoenix returns {"networkDetail": "<stringified json>"} (plus other keys);
    we un-stringify networkDetail when present.
    """
    payload = await _request(login, "get", EP_PHOENIX)
    network_detail = payload.get("networkDetail") if isinstance(payload, dict) else None
    tree: Any = payload
    if isinstance(network_detail, str):
        try:
            tree = json.loads(network_detail)
        except json.JSONDecodeError:
            tree = payload
    return tree


async def get_raw_appliance_dicts(login, source: str = "phoenix") -> list[dict]:
    """Every raw appliance record (unflattened) from one source, for diagnostics.

    ``source`` is "phoenix" (``GET /api/phoenix``) or "graphql" (the Alexa app's
    device query that the sync actually uses). They are kept apart so a report
    says which one is broken.
    """
    if source == "graphql":
        tree: Any = await _fetch_gql_appliances(login)
    else:
        tree = await _fetch_appliance_tree(login)
    out: list[dict] = []
    seen_ids: set[str] = set()
    for rec in _walk_appliance_dicts(tree):
        aid = rec.get("applianceId")
        if aid and aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)
        out.append(rec)
    return out


async def raw_get(login, uri: str):
    """GET a private endpoint, returning (payload, error_str). Never raises — for probing."""
    try:
        payload = await _request(login, "get", uri)
        return payload, None
    except Exception as err:  # noqa: BLE001
        return None, str(err)[:200]


async def probe_read_endpoints(login) -> list[dict]:
    """Try candidate READ-ONLY endpoints for the smart-home device list and report each's
    shape — to find one that (unlike /api/phoenix, which returns null) actually lists devices."""
    from . import diag as _diag  # noqa: PLC0415

    candidates = [
        "/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome",
        "/api/behaviors/entities",
        "/api/phoenix",
        "/api/smarthome/v1/appliances",
        "/api/phoenix/appliance",
        "/api/smarthome/v2/appliances",
    ]

    def skel(o, depth=0):
        if depth > 4:
            return "..."
        if isinstance(o, dict):
            return {k: skel(v, depth + 1) for k, v in list(o.items())[:25]}
        if isinstance(o, list):
            return [f"list[{len(o)}]"] + ([skel(o[0], depth + 1)] if o else [])
        return type(o).__name__

    out = []
    for uri in candidates:
        payload, err = await raw_get(login, uri)
        eids = _diag._find_entity_ids(payload) if payload is not None else []
        out.append({
            "endpoint": uri,
            "error": err,
            "type": type(payload).__name__,
            "top_keys": list(payload.keys())[:25] if isinstance(payload, dict) else None,
            "list_len": len(payload) if isinstance(payload, list) else None,
            "entity_id_hits": len(eids),
            "entity_id_example": eids[0][1] if eids else None,
            "skeleton": skel(payload),
        })
    return out


async def get_raw_phoenix_shape(login) -> dict:
    """Structural skeleton of the RAW /api/phoenix response — no values, for diagnosing
    an empty appliance walk. Shows top-level keys, whether networkDetail exists and parses,
    and a depth-limited skeleton (dict keys / list lengths / leaf types) of the tree."""
    def skel(o, depth=0):
        if depth > 6:
            return "..."
        if isinstance(o, dict):
            return {k: skel(v, depth + 1) for k, v in list(o.items())[:40]}
        if isinstance(o, list):
            return [f"list[{len(o)}]"] + ([skel(o[0], depth + 1)] if o else [])
        return type(o).__name__
    payload = await _request(login, "get", EP_PHOENIX)
    nd = payload.get("networkDetail") if isinstance(payload, dict) else None
    parsed = None
    if isinstance(nd, str):
        try:
            parsed = json.loads(nd)
        except json.JSONDecodeError:
            parsed = "UNPARSEABLE"
    tree = await _fetch_appliance_tree(login)
    return {
        "payload_type": type(payload).__name__,
        "payload_top_keys": list(payload.keys()) if isinstance(payload, dict) else None,
        "networkDetail_present": nd is not None,
        "networkDetail_is_string": isinstance(nd, str),
        "networkDetail_len": len(nd) if isinstance(nd, str) else None,
        "tree_skeleton": skel(tree),
    }


async def _fetch_gql_appliances(login) -> list[dict]:
    """List smart-home appliances via the Alexa app's GraphQL endpoint.

    Returns the ``legacyAppliance`` dicts, which carry the applianceId the group API wants.
    """
    payload = await _request(login, "post", EP_GRAPHQL, data={"query": GQL_SMARTHOME_QUERY})
    if not isinstance(payload, dict):
        return []
    if payload.get("errors"):
        raise AlexaClientError(f"GraphQL device list failed: {str(payload['errors'])[:300]}")
    items = ((payload.get("data") or {}).get("endpoints") or {}).get("items") or []
    return [
        item["legacyAppliance"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("legacyAppliance"), dict)
    ]


async def _fetch_appliance_records(login) -> Any:
    """GraphQL first; fall back to the legacy /api/phoenix tree if it lists nothing."""
    try:
        records = await _fetch_gql_appliances(login)
    except AlexaClientError as err:
        _LOGGER.warning("GraphQL device list failed, trying /api/phoenix: %s", err)
        records = []
    if records:
        return records
    return await _fetch_appliance_tree(login)


async def get_appliances(login) -> list[Appliance]:
    """Fetch and flatten every smart-home appliance Alexa knows about."""
    tree = await _fetch_appliance_records(login)

    seen: dict[str, Appliance] = {}
    for rec in _walk_appliance_dicts(tree):
        aid = rec.get("applianceId")
        if not aid or aid in seen:
            continue
        name = rec.get("friendlyName") or rec.get("friendlyDescription") or ""
        seen[aid] = Appliance(
            appliance_id=aid,
            friendly_name=str(name).strip(),
            entity_id=_extract_entity_id(rec),
        )
    return list(seen.values())


async def get_groups(login) -> list[AlexaGroup]:
    """Fetch existing Alexa groups/rooms."""
    payload = await _request(login, "get", EP_GROUP)
    groups: list[AlexaGroup] = []
    raw = []
    if isinstance(payload, dict):
        raw = payload.get("applianceGroups") or payload.get("groups") or []
    elif isinstance(payload, list):
        raw = payload
    for g in raw:
        if not isinstance(g, dict):
            continue
        gid = g.get("groupId") or g.get("id") or g.get("entityId") or ""
        groups.append(
            AlexaGroup(
                group_id=str(gid),
                name=str(g.get("name", "")).strip(),
                appliance_ids=list(g.get("applianceIds", []) or []),
                raw=g,
            )
        )
    return groups


async def create_group(login, name: str, appliance_ids: list[str]) -> dict:
    """Create a new Alexa group/room containing the given appliances."""
    if not appliance_ids:
        raise AlexaClientError(f"Refusing to create empty group '{name}'")
    return await _request(
        login,
        "post",
        EP_GROUP,
        data={"applianceIds": appliance_ids, "name": name},
    )


async def update_group(
    login, group: AlexaGroup, name: str, appliance_ids: list[str]
) -> Any:
    """Replace a group's name and members, sending Amazon's own document back.

    Same resource as create/delete; the body is the group as GET returned it, with
    only ``name`` and ``applianceIds`` changed, so fields we don't know about survive.
    """
    if not group.group_id:
        raise AlexaClientError("update_group requires a group_id")
    if not appliance_ids:
        raise AlexaClientError(f"Refusing to empty group '{group.name}'; delete it instead")
    body = {**group.raw, "name": name, "applianceIds": appliance_ids}
    return await _request(login, "put", f"{EP_GROUP}/{group.group_id}", data=body)


def entity_id_from_appliance_id(appliance_id: str) -> str | None:
    """The HA entity_id behind an Alexa applianceId, or None for non-HA devices (Echos)."""
    return _entity_id_from_alexa_id(appliance_id)


async def delete_group(login, group_id: str) -> bool:
    """Delete an Alexa group by id (undo)."""
    if not group_id:
        raise AlexaClientError("delete_group requires a group_id")
    return await _request(login, "delete", f"{EP_GROUP}/{group_id}")


# ── Alexa lists ─────────────────────────────────────────────────────────────────────────
# Shopping list / to-do list / custom lists, over the same borrowed session. These endpoints
# are the ones the Alexa web app uses; Amazon has been retiring list access, so everything
# here degrades to "no lists" rather than raising into HA's setup.


@dataclass
class AlexaList:
    """One Alexa list (Shopping List, To-do List, or a custom one)."""

    list_id: str
    name: str
    type: str | None


@dataclass
class AlexaListItem:
    """One item on an Alexa list. `raw` is kept so updates can echo Amazon's own
    document back with its version — the API rejects writes that drop it."""

    item_id: str
    value: str
    completed: bool
    raw: dict


def _first_list(payload: Any, *keys: str) -> list:
    """Pull the first list-shaped value out of a payload, by key then by scan.

    Amazon returns `{"lists": [...]}` on some accounts and `{"list": [...]}` or a bare
    array on others; guessing one shape is how this breaks silently.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list) and (not value or isinstance(value[0], dict)):
                return value
    return []


async def get_lists(login) -> list[AlexaList]:
    """Every named list on the account. Empty if Amazon no longer serves them."""
    payload = await _request(login, "get", EP_LISTS)
    out: list[AlexaList] = []
    for raw in _first_list(payload, "lists", "list"):
        if not isinstance(raw, dict):
            continue
        list_id = raw.get("itemId") or raw.get("listId") or raw.get("id")
        name = raw.get("name") or raw.get("title")
        if not list_id or not name:
            continue
        out.append(AlexaList(list_id=str(list_id), name=str(name), type=raw.get("type")))
    return out


async def get_list_items(login, list_id: str) -> list[AlexaListItem]:
    """Items on one list, completed ones included (the todo entity shows both)."""
    payload = await _request(login, "get", EP_LIST_ITEMS.format(list_id=list_id))
    out: list[AlexaListItem] = []
    for raw in _first_list(payload, "list", "items", "values"):
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("id") or raw.get("itemId")
        value = raw.get("value") or raw.get("text") or raw.get("name")
        if not item_id or value is None:
            continue
        out.append(
            AlexaListItem(
                item_id=str(item_id),
                value=str(value),
                completed=bool(raw.get("completed")),
                raw=raw,
            )
        )
    return out


async def add_list_item(login, list_id: str, value: str) -> Any:
    """Add an item. Alexa's own app posts {value, status} to /item."""
    return await _request(
        login,
        "post",
        EP_LIST_ITEM.format(list_id=list_id),
        data={"value": value, "status": "active"},
    )


async def update_list_item(login, item: AlexaListItem, *, value: str | None = None,
                           completed: bool | None = None) -> Any:
    """Tick off or rename an item.

    Sends Amazon's own item document back with the fields changed — the API uses the
    `version` field for optimistic concurrency and 400s if it is missing or stale.
    """
    body = dict(item.raw)
    if value is not None:
        body["value"] = value
    if completed is not None:
        body["completed"] = completed
    return await _request(
        login,
        "put",
        EP_LIST_ITEM_ID.format(list_id=body.get("listId", ""), item_id=item.item_id),
        data=body,
    )


async def delete_list_item(login, list_id: str, item: AlexaListItem) -> Any:
    """Remove an item from the list."""
    return await _request(
        login,
        "delete",
        EP_LIST_ITEM_ID.format(list_id=list_id, item_id=item.item_id),
        data=item.raw,
    )


async def raw_probe(login, uri: str) -> dict:
    """Lowest-level GET: report HTTP status, content type and a body prefix.

    `raw_get` goes through `_request`, which turns a non-2xx into an exception and a
    non-JSON body into `{}` — both of which hide WHY an endpoint is failing. When an
    endpoint answers `{}` you need to know whether that is really an empty JSON object or
    an HTML sign-in page, and only the raw response says.
    """
    from alexapy import AlexaAPI  # noqa: PLC0415

    out: dict[str, Any] = {"endpoint": uri}
    try:
        resp = await AlexaAPI._static_request("get", login, uri)  # noqa: SLF001
    except Exception as err:  # noqa: BLE001
        out["exception"] = str(err)[:200]
        return out
    if resp is None:
        out["response"] = None  # alexapy itself swallowed it (redirect / auth failure)
        return out
    out["status"] = getattr(resp, "status", None)
    try:
        out["content_type"] = resp.headers.get("Content-Type")
    except Exception:  # noqa: BLE001
        pass
    try:
        text = await resp.text()
        out["length"] = len(text)
        stripped = text.lstrip()[:15].lower()
        out["looks_html"] = stripped.startswith(("<!doctype", "<html"))
        # returned to the caller only, never logged: this can contain list contents
        out["body_prefix"] = text[:220]
    except Exception as err:  # noqa: BLE001
        out["text_error"] = str(err)[:120]
    return out


async def probe_lists_raw(login) -> list[dict]:
    """Try every list endpoint shape anyone has used, with full HTTP detail."""
    candidates = [
        "/api/namedLists",
        "/api/namedLists?_=1700000000000",
        "/api/v2/namedLists",
        "/api/v2/namedLists?listIds=",
        "/api/v2/namedLists/list",
        "/api/lists",
        "/api/v1/lists",
        "/api/todos?type=SHOPPING_ITEM&size=100",
        "/api/cardsForDevice?deviceType=ALL",
    ]
    return [await raw_probe(login, uri) for uri in candidates]


async def probe_lists(login) -> list[dict]:
    """Read-only: try the candidate list endpoints and report what still answers.

    Run this BEFORE trusting the todo platform — Amazon has been closing list access, and
    a dead endpoint should be visible as a diagnostic rather than as empty cards.
    """
    candidates = [
        EP_LISTS,
        "/api/v2/namedLists",
        "/api/namedLists?listIds=",
        "/api/todos?type=SHOPPING_ITEM&size=100",
        "/api/todos?type=TASK&size=100",
    ]
    out: list[dict] = []
    for uri in candidates:
        payload, err = await raw_get(login, uri)
        entry: dict[str, Any] = {"endpoint": uri, "error": err}
        if err is None:
            rows = _first_list(payload, "lists", "list", "items", "values")
            entry["type"] = type(payload).__name__
            entry["top_level_keys"] = (
                list(payload.keys())[:12] if isinstance(payload, dict) else None
            )
            entry["row_count"] = len(rows)
            # names only — never dump the contents of someone's shopping list into a log
            entry["row_keys"] = sorted(rows[0].keys())[:20] if rows and isinstance(rows[0], dict) else None
            entry["list_names"] = [
                r.get("name") or r.get("title")
                for r in rows[:12]
                if isinstance(r, dict) and (r.get("name") or r.get("title"))
            ]
        out.append(entry)
    return out
