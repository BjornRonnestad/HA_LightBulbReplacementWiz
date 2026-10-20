"""Actual HA flows and registry changes with a simulated Zigbee2MQTT bridge."""

import asyncio
import json
import logging
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.components import mqtt
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.light_bulb_replacement import z2m
from custom_components.light_bulb_replacement.replacement import (
    ReplacementError,
    ReplacementManager,
)

OLD = "0x0011223344556601"
NEW = "0x0011223344556602"
REMOTE = "0x0011223344556603"
COORDINATOR = "0x00112233445566ff"
GROUP = 1234
BASE = "house/zigbee"


def binding(
    source=REMOTE,
    endpoint=1,
    cluster="genOnOff",
    target=OLD,
    target_endpoint=1,
    group=None,
):
    return z2m.Binding(
        source,
        endpoint,
        cluster,
        target if group is None else None,
        target_endpoint if group is None else None,
        group,
    )


class Bridge:
    """Public bridge JSON contracts; MQTT traffic is isolated from real brokers."""

    def __init__(self, hass):
        self.hass = hass
        self.callbacks = []
        self.requests = []
        self.physical = {binding(), binding(source=OLD, target=COORDINATOR)}
        self.devices = []
        for ieee, endpoint in ((OLD, 1), (NEW, 3), (REMOTE, 1), (COORDINATOR, 1)):
            self.devices.append(
                {
                    "ieee_address": ieee,
                    "friendly_name": ieee,
                    "endpoints": {
                        str(endpoint): {
                            "name": None,
                            "clusters": {
                                "input": ["genOnOff", "genLevelCtrl", "genGroups"],
                                "output": ["genOnOff"],
                            },
                            "bindings": [],
                        }
                    },
                }
            )
        self.groups = [{"id": GROUP, "members": [{"ieee_address": OLD, "endpoint": 1}]}]
        self.info = {
            "coordinator": {"ieee_address": COORDINATOR},
            "network": {"extended_pan_id": "0x1122334455667788"},
        }
        self.state = {"state": "online"}
        self.fail = None
        self.ignore_add = False
        self.lose_ack = False
        self.cancel_after_bind = False
        self.bad_response = False
        self.fail_undo = False
        self.sync_bindings()

    def endpoint(self, ieee, endpoint):
        return next(d for d in self.devices if d["ieee_address"] == ieee)["endpoints"][
            str(endpoint)
        ]

    def sync_bindings(self):
        for device in self.devices:
            for endpoint, data in device["endpoints"].items():
                data["bindings"] = [
                    {
                        "cluster": b.cluster,
                        "target": (
                            {"type": "group", "id": b.group}
                            if b.group is not None
                            else {
                                "type": "endpoint",
                                "ieee_address": b.target,
                                "endpoint": b.target_endpoint,
                            }
                        ),
                    }
                    for b in sorted(self.physical, key=lambda b: b.describe())
                    if b.source == device["ieee_address"]
                    and b.endpoint == int(endpoint)
                ]

    def emit(self, topic, data, *, retain=False, callback=None):
        message = SimpleNamespace(
            topic=f"{BASE}/bridge/{topic}", payload=json.dumps(data), retain=retain
        )
        for receive in [callback] if callback else list(self.callbacks):
            receive(message)

    async def subscribe(self, hass, topic, receive, **kwargs):
        assert hass is self.hass and topic == f"{BASE}/bridge/#"
        self.callbacks.append(receive)
        for name in ("state", "info", "devices", "groups"):
            self.hass.loop.call_soon(
                lambda name=name: self.emit(
                    name, getattr(self, name), retain=True, callback=receive
                )
            )
        return lambda: self.callbacks.remove(receive)

    async def publish(self, hass, topic, payload, *, qos, retain):
        assert hass is self.hass and qos == 0 and retain is False
        assert topic.startswith(f"{BASE}/bridge/request/")
        command = topic.removeprefix(f"{BASE}/bridge/request/")
        request = json.loads(payload)
        self.requests.append((command, request))
        data = {k: v for k, v in request.items() if k != "transaction"}
        failed = command == self.fail or (self.fail_undo and command == "device/unbind")
        metadata = None
        if command == "health_check":
            data = {"healthy": True}
        elif command in ("device/bind", "device/unbind"):
            b = z2m.Binding(
                request["from"],
                request["from_endpoint"],
                request["clusters"][0],
                request["to"] if "to_endpoint" in request else None,
                request.get("to_endpoint"),
                None if "to_endpoint" in request else int(request["to"]),
            )
            if not failed and not (self.ignore_add and command == "device/bind"):
                if command == "device/bind":
                    self.physical.add(b)
                else:
                    self.physical.discard(b)
            data["failed"] = request["clusters"] if failed else []
            data["clusters"] = [] if failed else request["clusters"]
            if command == "device/bind" and self.cancel_after_bind:
                self.cancel_after_bind = False
                raise asyncio.CancelledError
            if command == "device/bind" and self.lose_ack:
                # The physical bind succeeded, but neither acknowledgement nor
                # refreshed bridge metadata reached HA. Undo must still unbind.
                self.lose_ack = False
                return
            self.sync_bindings()
            metadata = "devices"
        elif command.startswith("group/members/"):
            member = {
                "ieee_address": request["device"],
                "endpoint": request["endpoint"],
            }
            group = next(g for g in self.groups if g["id"] == int(request["group"]))
            if not failed:
                if command.endswith("/add") and member not in group["members"]:
                    group["members"].append(member)
                elif command.endswith("/remove") and member in group["members"]:
                    group["members"].remove(member)
            metadata = "groups"
        else:
            raise AssertionError(command)
        if self.bad_response and command == "device/bind":
            data["failed"] = request["clusters"]
        response = {
            "transaction": request["transaction"],
            "status": "error" if failed else "ok",
            "data": data,
        }
        # Stale retained and unrelated transaction responses cannot satisfy a request.
        self.emit(f"response/{command}", {**response, "status": "error"}, retain=True)
        self.emit(
            f"response/{command}",
            {**response, "transaction": "another-wizard", "status": "error"},
        )
        self.emit(f"response/{command}", response)
        if metadata:
            self.hass.loop.call_soon(
                lambda: self.emit(metadata, getattr(self, metadata))
            )


@pytest.fixture
async def network(wizard, monkeypatch):
    hass, registry, manager, native_old, _, entry = wizard
    bridge = Bridge(hass)
    monkeypatch.setattr(mqtt, "is_connected", lambda _: True)
    monkeypatch.setattr(mqtt, "async_subscribe", bridge.subscribe)
    monkeypatch.setattr(mqtt, "async_publish", bridge.publish)
    monkeypatch.setattr(z2m, "READ_TIMEOUT", 0.05)
    monkeypatch.setattr(z2m, "COMMAND_TIMEOUT", 0.05)
    hass.config.components.add("mqtt")
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="light",
        platform_name="mqtt",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.async_prepare()
    platform.config_entry = entry
    hass.data["light"]._platforms["simulated_mqtt"] = platform
    bulbs = []
    for ieee, name in ((OLD, "MQTT old"), (NEW, "MQTT new")):
        bulb = type(native_old)(name, f"{ieee}_light_{BASE}")
        bulb._attr_device_info = {"identifiers": {("mqtt", f"zigbee2mqtt_{ieee}")}}
        bulbs.append(bulb)
    await platform.async_add_entities(bulbs)
    await hass.async_block_till_done()
    yield hass, registry, manager, *bulbs, bridge, entry
    assert bridge.callbacks == []


async def scan(network, **kwargs):
    hass, registry, _, old, new, _, _ = network
    return await z2m.Z2mMigration(hass, BASE).async_scan(
        registry.async_get(old.entity_id), registry.async_get(new.entity_id), **kwargs
    )


async def test_discover_incoming_outgoing_groups_and_ignore_other_channels(network):
    _, _, _, _, _, bridge, _ = network
    bridge.endpoint(OLD, 1)["name"] = "main"
    device = next(d for d in bridge.devices if d["ieee_address"] == OLD)
    device["endpoints"]["2"] = deepcopy(device["endpoints"]["1"])
    device["endpoints"]["2"]["name"] = "other"
    bridge.physical.add(binding(source=OLD, endpoint=2, target=COORDINATOR))
    bridge.physical.add(binding(source=OLD, group=GROUP))
    bridge.sync_bindings()
    plan = await scan(network, old_endpoint=1)
    assert plan.old == z2m.Endpoint(OLD, 1) and plan.new == z2m.Endpoint(NEW, 3)
    assert len(plan.bindings) == 3 and plan.groups == (GROUP,)
    assert binding(target=NEW, target_endpoint=3) in plan.bindings
    assert binding(source=NEW, endpoint=3, target=COORDINATOR) in plan.bindings
    assert binding(source=NEW, endpoint=3, group=GROUP) in plan.bindings
    assert plan.failed_reads == (z2m.CACHED_DISCOVERY,)
    assert not plan.blockers
    assert all(command == "health_check" for command, _ in bridge.requests)


async def test_copy_and_persistent_undo_preserve_existing_entries(network):
    hass, registry, manager, old, new, bridge, _ = network
    preexisting = binding(target=NEW, target_endpoint=3)
    bridge.physical.add(preexisting)
    bridge.sync_bindings()
    originals = set(bridge.physical)
    old_id, new_id = old.entity_id, new.entity_id
    plan = await scan(network)
    await manager.async_replace(
        manager.preview(old_id, new_id), z2m_plan=plan, accept_partial=True
    )
    assert manager.journal["status"] == "completed"
    assert registry.async_get(old_id).unique_id == new.unique_id
    assert bridge.physical >= originals
    assert {"ieee_address": NEW, "endpoint": 3} in bridge.groups[0]["members"]
    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    await reloaded.async_restore()
    assert reloaded.journal["status"] == "restored"
    assert bridge.physical == originals
    assert bridge.groups[0]["members"] == [{"ieee_address": OLD, "endpoint": 1}]
    assert registry.async_get(old_id).unique_id == old.unique_id
    assert registry.async_get(new_id).unique_id == new.unique_id
    assert all(
        request["skip_disable_reporting"]
        for command, request in bridge.requests
        if command != "health_check"
    )


async def test_cached_discovery_requires_explicit_acceptance(network):
    _, _, manager, old, new, bridge, _ = network
    plan = await scan(network)
    with pytest.raises(ReplacementError, match="z2m_incomplete"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), z2m_plan=plan
        )
    assert manager.journal is None
    assert all(command == "health_check" for command, _ in bridge.requests)


@pytest.mark.parametrize(
    "failure,code",
    [
        ("command", "z2m_write_failed"),
        ("ignored", "z2m_verification_failed"),
        ("partial", "z2m_write_failed"),
        ("lost_ack", "z2m_timeout"),
    ],
)
async def test_command_failures_rollback_without_renaming(network, failure, code):
    _, registry, manager, old, new, bridge, _ = network
    plan = await scan(network)
    originals = set(bridge.physical)
    if failure == "command":
        bridge.fail = "device/bind"
    elif failure == "ignored":
        bridge.ignore_add = True
    elif failure == "partial":
        bridge.bad_response = True
    else:
        bridge.lose_ack = True
    old_id, new_id = old.entity_id, new.entity_id
    with pytest.raises(ReplacementError, match=code):
        await manager.async_replace(
            manager.preview(old_id, new_id), z2m_plan=plan, accept_partial=True
        )
    assert registry.async_get(old_id).unique_id == old.unique_id
    assert registry.async_get(new_id).unique_id == new.unique_id
    assert manager.journal["status"] == "restored"
    assert bridge.physical == originals
    assert any(command == "device/unbind" for command, _ in bridge.requests)


async def test_cancelled_write_is_recoverable_even_without_updated_bridge_cache(
    network,
):
    hass, registry, manager, old, new, bridge, _ = network
    plan = await scan(network)
    originals = set(bridge.physical)
    old_id = old.entity_id
    bridge.cancel_after_bind = True
    with pytest.raises(asyncio.CancelledError):
        await manager.async_replace(
            manager.preview(old_id, new.entity_id), z2m_plan=plan, accept_partial=True
        )
    assert bridge.physical != originals
    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    assert reloaded.journal["status"] == "pending"
    await reloaded.async_restore()
    assert bridge.physical == originals
    assert registry.async_get(old_id).unique_id == old.unique_id


async def test_failed_undo_keeps_recovery_journal(network):
    _, _, manager, old, new, bridge, _ = network
    plan = await scan(network)
    bridge.lose_ack = True
    bridge.fail_undo = True
    with pytest.raises(ReplacementError, match="recovery_required"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id),
            z2m_plan=plan,
            accept_partial=True,
        )
    assert manager.journal["status"] == "recovery_required"
    bridge.fail_undo = False
    await manager.async_restore()
    assert manager.journal["status"] == "restored"


async def test_network_change_blocks_undo_without_unbinding_on_new_network(network):
    _, _, manager, old, new, bridge, _ = network
    plan = await scan(network)
    await manager.async_replace(
        manager.preview(old.entity_id, new.entity_id),
        z2m_plan=plan,
        accept_partial=True,
    )
    original_info = deepcopy(bridge.info)
    bridge.info["coordinator"]["ieee_address"] = "0x00112233445566ee"
    before = len(bridge.requests)
    with pytest.raises(ReplacementError, match="z2m_network_changed"):
        await manager.async_restore()
    assert all(command == "health_check" for command, _ in bridge.requests[before:])
    assert manager.journal["status"] == "recovery_required"
    bridge.info = original_info
    await manager.async_restore()


async def test_incompatible_clusters_block_before_writes(network):
    _, _, manager, old, new, bridge, _ = network
    bridge.endpoint(NEW, 3)["clusters"]["input"].remove("genOnOff")
    # Keep on/off present to identify the light, but remove level support.
    bridge.endpoint(NEW, 3)["clusters"]["input"].append("genOnOff")
    bridge.endpoint(NEW, 3)["clusters"]["input"].remove("genLevelCtrl")
    bridge.physical.add(binding(cluster="genLevelCtrl"))
    bridge.sync_bindings()
    plan = await scan(network)
    assert plan.blockers
    with pytest.raises(ReplacementError, match="z2m_incompatible"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id),
            z2m_plan=plan,
            accept_partial=True,
        )
    assert manager.journal is None
    assert all(command == "health_check" for command, _ in bridge.requests)


async def test_ambiguous_channel_requires_endpoint_and_named_channel_is_resolved(
    network,
):
    _, registry, _, old, _, bridge, _ = network
    device = next(d for d in bridge.devices if d["ieee_address"] == OLD)
    device["endpoints"]["2"] = deepcopy(device["endpoints"]["1"])
    with pytest.raises(ReplacementError, match="z2m_endpoint_required"):
        await scan(network)
    assert (await scan(network, old_endpoint=1)).old.endpoint == 1
    bridge.endpoint(OLD, 2)["name"] = "right"
    registry.async_update_entity(
        old.entity_id, new_unique_id=f"{OLD}_light_right_{BASE}"
    )
    assert (await scan(network)).old.endpoint == 2
    with pytest.raises(ReplacementError, match="z2m_endpoint_required"):
        await scan(network, old_endpoint=1)


async def test_other_bridge_or_non_zigbee_mqtt_light_rejected(network):
    _, registry, _, old, _, _, _ = network
    registry.async_update_entity(
        old.entity_id, new_unique_id=f"{OLD}_light_otherbridge"
    )
    with pytest.raises(ReplacementError, match="z2m_lights_required"):
        await scan(network)


@pytest.mark.parametrize(
    "topic", ["", "/zigbee2mqtt", "zigbee2mqtt/", "a/#", "a/+", "bad\x00topic"]
)
async def test_invalid_base_topic_rejected(network, topic):
    hass, *_ = network
    with pytest.raises(ReplacementError, match="z2m_invalid_topic"):
        z2m.Z2mMigration(hass, topic)


async def test_missing_metadata_or_broker_is_a_clear_error(network, monkeypatch):
    hass, _, _, _, _, bridge, _ = network
    monkeypatch.setattr(mqtt, "is_connected", lambda _: False)
    with pytest.raises(ReplacementError, match="z2m_unavailable"):
        await scan(network)
    monkeypatch.setattr(mqtt, "is_connected", lambda _: True)
    bridge.devices = None
    with pytest.raises(ReplacementError, match="z2m_invalid_data"):
        await scan(network)
    assert "mqtt" in hass.config.components


async def test_options_flow_uses_english_preview_confirm_copy_and_undo(network):
    hass, registry, manager, old, new, _, entry = network
    old_id, new_id = old.entity_id, new.entity_id
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    result = await options.async_configure(
        result["flow_id"],
        {"old_entity_id": old_id, "new_entity_id": new_id, "copy_z2m": True},
    )
    assert result["step_id"] == "z2m"
    result = await options.async_configure(result["flow_id"], {"base_topic": BASE})
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await options.async_configure(result["flow_id"])
    assert result["step_id"] == "confirm_z2m"
    assert REMOTE in result["description_placeholders"]["z2m_bindings"]
    result = await options.async_configure(result["flow_id"], {"confirm": True})
    assert result["errors"] == {"base": "z2m_incomplete"}
    result = await options.async_configure(
        result["flow_id"], {"confirm": True, "accept_partial": True}
    )
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await options.async_configure(result["flow_id"])
    assert result["type"] == "create_entry"
    assert manager.journal["status"] == "completed"
    assert registry.async_get(old_id).unique_id == new.unique_id
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "restore"}
    )
    result = await options.async_configure(result["flow_id"], {"confirm": True})
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await options.async_configure(result["flow_id"])
    assert result["type"] == "create_entry"
    assert registry.async_get(old_id).unique_id == old.unique_id


async def test_both_backends_cannot_be_selected(network):
    hass, _, _, old, new, bridge, entry = network
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": old.entity_id,
            "new_entity_id": new.entity_id,
            "copy_zha": True,
            "copy_z2m": True,
        },
    )
    assert result["errors"] == {"base": "binding_backend_conflict"}
    assert bridge.requests == []


async def test_preexisting_group_membership_is_preserved_on_undo(network):
    _, _, manager, old, new, bridge, _ = network
    member = {"ieee_address": NEW, "endpoint": 3}
    bridge.groups[0]["members"].append(member)
    plan = await scan(network)
    await manager.async_replace(
        manager.preview(old.entity_id, new.entity_id),
        z2m_plan=plan,
        accept_partial=True,
    )
    await manager.async_restore()
    assert member in bridge.groups[0]["members"]
    assert not any(
        command.startswith("group/members/") for command, _ in bridge.requests
    )


async def test_changed_binding_preview_requires_rescan_before_writing(network):
    _, _, manager, old, new, bridge, _ = network
    plan = await scan(network)
    bridge.physical.add(binding(cluster="genLevelCtrl"))
    bridge.sync_bindings()
    with pytest.raises(ReplacementError, match="selection_changed"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id),
            z2m_plan=plan,
            accept_partial=True,
        )
    assert manager.journal is None
    assert all(command == "health_check" for command, _ in bridge.requests)


async def test_network_change_during_write_retains_recovery_without_more_writes(
    network, monkeypatch
):
    _, registry, manager, old, new, bridge, _ = network
    plan = await scan(network)
    info = deepcopy(bridge.info)
    publish = bridge.publish

    async def change_network(hass, topic, payload, **kwargs):
        if topic.endswith("/device/bind"):
            bridge.info["network"]["extended_pan_id"] = "0x8877665544332211"
            bridge.emit("info", bridge.info)
        await publish(hass, topic, payload, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(mqtt, "async_publish", change_network)
        with pytest.raises(ReplacementError, match="recovery_required"):
            await manager.async_replace(
                manager.preview(old.entity_id, new.entity_id),
                z2m_plan=plan,
                accept_partial=True,
            )
    assert registry.async_get(old.entity_id).unique_id == old.unique_id
    assert manager.journal["status"] == "recovery_required"
    writes = [command for command, _ in bridge.requests if command != "health_check"]
    assert writes == ["device/bind"]
    bridge.info = info
    await manager.async_restore()


async def test_missing_retained_topic_times_out_and_unsubscribes(network, monkeypatch):
    _, _, _, _, _, bridge, _ = network
    subscribe = bridge.subscribe

    async def missing_groups(hass, topic, receive, **kwargs):
        return await subscribe(
            hass,
            topic,
            lambda msg: receive(msg) if not msg.topic.endswith("/groups") else None,
            **kwargs,
        )

    with monkeypatch.context() as patches:
        patches.setattr(mqtt, "async_subscribe", missing_groups)
        with pytest.raises(ReplacementError, match="z2m_timeout"):
            await scan(network)
    assert bridge.callbacks == []
    assert bridge.requests == []
