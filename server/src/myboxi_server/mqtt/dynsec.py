"""Mosquitto's dynamic security plugin: one account and one role per box (SPEC §6).

A box may only publish its own ``reported``, ``events``, ``cmd/ack`` and ``online`` and only
subscribe to its own ``notify`` and ``cmd``. The server's account gets its role once
(``myboxi-server mqtt-setup``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from myboxi_protocol.topics import FROM_BOX, PREFIX, TO_BOX, topic

CONTROL = "$CONTROL/dynamic-security/v1"
RESPONSE = f"{CONTROL}/response"
SERVER_ROLE = "myboxi-server"
Command = dict[str, Any]


def box_role(device_id: uuid.UUID) -> str:
    return f"box-{device_id}"


def _acl(rolename: str, acltype: str, topic_: str) -> Command:
    return {"command": "addRoleACL", "rolename": rolename, "acltype": acltype,
            "topic": topic_, "allow": True, "priority": 0}  # fmt: skip


def remove_box(device_id: uuid.UUID) -> list[Command]:
    """ "Not found" answers are fine: removing twice is harmless."""
    return [
        {"command": "deleteClient", "username": str(device_id)},
        {"command": "deleteRole", "rolename": box_role(device_id)},
    ]


def provision_box(device_id: uuid.UUID, password: str) -> list[Command]:
    """A fresh account (the old one, if any, goes first): re-pairing replaces the password."""
    role = box_role(device_id)
    return [
        *remove_box(device_id),
        {"command": "createRole", "rolename": role},
        *(_acl(role, "publishClientSend", topic(device_id, leaf)) for leaf in FROM_BOX),
        *(_acl(role, "subscribePattern", topic(device_id, leaf)) for leaf in TO_BOX),
        {
            "command": "createClient",
            "username": str(device_id),
            "password": password,
            "roles": [{"rolename": role}],
        },
    ]


def server_role(username: str) -> list[Command]:
    """Send and receive under ``myboxi/v1/#`` next to the admin rights from ``dynsec init``."""
    everything = f"{PREFIX}/#"
    return [
        {"command": "deleteRole", "rolename": SERVER_ROLE},
        {"command": "createRole", "rolename": SERVER_ROLE},
        _acl(SERVER_ROLE, "publishClientSend", everything),
        _acl(SERVER_ROLE, "subscribePattern", everything),
        _acl(SERVER_ROLE, "publishClientReceive", everything),
        {
            "command": "addClientRole",
            "username": username,
            "rolename": SERVER_ROLE,
            "priority": 10,
        },
    ]


def failures(commands: list[Command], responses: list[Command]) -> list[str]:
    """Errors that matter: everything except "not found" on deletes."""
    bad: list[str] = []
    for command, response in zip(commands, responses, strict=False):
        error = response.get("error")
        if not error:
            continue
        if command["command"] in ("deleteClient", "deleteRole") and "not found" in str(error):
            continue
        bad.append(f"{command['command']}: {error}")
    if len(responses) < len(commands):
        bad.append("missing responses")
    return bad


def responses(payload: bytes) -> list[Command]:
    try:
        data: object = json.loads(payload)
    except ValueError:
        return []
    items = cast(Command, data).get("responses") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [cast(Command, r) for r in cast(list[object], items) if isinstance(r, dict)]


def _dict(value: object) -> Command:
    return cast(Command, value) if isinstance(value, dict) else {}


def client_roles(payload: bytes) -> set[str]:
    """Role names from a ``getClient`` answer."""
    for response in responses(payload):
        client = _dict(_dict(response.get("data")).get("client"))
        roles = client.get("roles")
        if not isinstance(roles, list):
            return set()
        return {str(_dict(r).get("rolename")) for r in cast(list[object], roles)}
    return set()
