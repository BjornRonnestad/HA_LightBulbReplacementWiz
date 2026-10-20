"""Real Zigpy endpoints and protocol types; only radio responses are simulated."""

import asyncio
import logging
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.entity_platform import EntityPlatform
from zigpy.device import Device
from zigpy.group import Groups as ZigpyGroups
from zigpy.state import State
from zigpy.types import EUI64, ExtendedPanId
from zigpy.zcl.clusters.general import Groups
from zigpy.zcl.foundation import Status
from zigpy.zdo import types as zdo

from custom_components.light_bulb_replacement import zha
from custom_components.light_bulb_replacement.replacement import (
    ReplacementError,
    ReplacementManager,
)

OLD = "00:11:22:33:44:55:66:01"
NEW = "00:11:22:33:44:55:66:02"
REMOTE = "00:11:22:33:44:55:66:03"
COORDINATOR = "00:11:22:33:44:55:66:ff"
GROUP = 0x1234


def raw_binding(
    source=REMOTE, endpoint=1, cluster=6, target=OLD, target_endpoint=1, group=None
):
    address = (
        zdo.MultiAddress(addrmode=1, nwk=group)
        if group is not None
        else zdo.MultiAddress(
            addrmode=3, ieee=EUI64.convert(target), endpoint=target_endpoint
        )
    )
    return zdo.Binding(
        SrcAddress=EUI64.convert(source),
        SrcEndpoint=endpoint,
        ClusterId=cluster,
        DstAddress=address,
    )


class Radio:
    """A network with paginated binding tables and native ZCL responses."""

    def __init__(self):
        self.app = SimpleNamespace(
            _dblistener=None,
            register_callback_listener=lambda **kwargs: lambda: None,
        )
        self.app.groups = ZigpyGroups(self.app)
        self.tables = {ieee: [] for ieee in (OLD, NEW, REMOTE, COORDINATOR)}
        self.memberships = {(OLD, 1): {GROUP}, (NEW, 3): set()}
        self.reads = []
        self.writes = []
        self.fail_reads = set()
        self.fail_groups = set()
        self.write_status = zdo.Status.SUCCESS
        self.ignore_bind = False
        self.lost_ack = False
        self.cancel_after_bind = False
        self.fail_unbind = False
        self.group_status = Status.SUCCESS
        state = State()
        state.node_info.ieee = EUI64.convert(COORDINATOR)
        state.network_info.extended_pan_id = ExtendedPanId.convert(
            "11:22:33:44:55:66:77:88"
        )
        self.gateway = SimpleNamespace(state=state, devices={}, groups={})
        for index, (ieee, endpoint_id) in enumerate(
            ((OLD, 1), (NEW, 3), (REMOTE, 1), (COORDINATOR, 1))
        ):
            device = Device(self.app, EUI64.convert(ieee), index)
            device.zdo.request = AsyncMock(side_effect=partial(self.request, ieee))
            endpoint = device.add_endpoint(endpoint_id)
            endpoint.profile_id = 0x0104
            for cluster in (4, 6, 8):
                endpoint.add_input_cluster(cluster)
            endpoint.add_output_cluster(6)
            groups = endpoint.in_clusters[4]
            groups.get_membership = AsyncMock(
                side_effect=partial(self.get_groups, ieee, endpoint_id)
            )
            groups.add = AsyncMock(
                side_effect=partial(self.add_group, ieee, endpoint_id)
            )
            groups.remove = AsyncMock(
                side_effect=partial(self.remove_group, ieee, endpoint_id)
            )
            self.gateway.devices[device.ieee] = SimpleNamespace(
                ieee=device.ieee,
                device=device,
                name=ieee,
                platform_entities={},
            )
        old_device = self.gateway.devices[EUI64.convert(OLD)]
        self.gateway.groups[GROUP] = SimpleNamespace(
            group_id=GROUP,
            name="Room group",
            members=[SimpleNamespace(device=old_device, endpoint_id=1)],
        )
        self.app.groups.add_group(GROUP, "Room group").add_member(
            old_device.device.endpoints[1]
        )

    async def request(self, ieee, command, *args):
        if command == zdo.ZDOCmd.Mgmt_Bind_req:
            self.reads.append((ieee, int(args[0])))
            if ieee in self.fail_reads:
                raise TimeoutError("Sleeping or offline device")
            start = int(args[0])
            return [
                zdo.Status.SUCCESS,
                len(self.tables[ieee]),
                start,
                self.tables[ieee][start : start + 2],
            ]
        self.writes.append((ieee, command))
        if self.write_status != zdo.Status.SUCCESS:
            return [self.write_status]
        binding = zdo.Binding(
            SrcAddress=args[0],
            SrcEndpoint=args[1],
            ClusterId=args[2],
            DstAddress=args[3],
        )
        if command == zdo.ZDOCmd.Bind_req:
            if not self.ignore_bind and binding not in self.tables[ieee]:
                self.tables[ieee].append(binding)
            if self.cancel_after_bind:
                self.cancel_after_bind = False
                raise asyncio.CancelledError
            if self.lost_ack:
                self.lost_ack = False
                raise TimeoutError("Acknowledgement lost after applying binding")
        elif command == zdo.ZDOCmd.Unbind_req:
            if self.fail_unbind:
                raise TimeoutError("Remote asleep during recovery")
            if binding in self.tables[ieee]:
                self.tables[ieee].remove(binding)
        else:
            raise AssertionError(command)
        return [zdo.Status.SUCCESS]

    async def get_groups(self, ieee, endpoint, *, groups):
        if ieee in self.fail_groups:
            raise TimeoutError
        return Groups.ClientCommandDefs.get_membership_response.schema(
            capacity=20, groups=sorted(self.memberships.get((ieee, endpoint), set()))
        )

    async def add_group(self, ieee, endpoint, group, name):
        if self.group_status == Status.SUCCESS:
            self.memberships.setdefault((ieee, endpoint), set()).add(int(group))
        return Groups.ClientCommandDefs.add_response.schema(
            status=self.group_status, group_id=group
        )

    async def remove_group(self, ieee, endpoint, group):
        self.memberships.setdefault((ieee, endpoint), set()).discard(int(group))
        return Groups.ClientCommandDefs.remove_response.schema(
            status=Status.SUCCESS, group_id=group
        )


@pytest.fixture
async def network(wizard, monkeypatch):
    hass, registry, manager, native_old, _, entry = wizard
    radio = Radio()
    monkeypatch.setattr(zha, "get_gateway", lambda _: radio.gateway)
    # A real HA entity platform with simulated ZHA lights. This exercises the
    # same registry rename listeners used by physical ZHA entities.
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="light",
        platform_name="zha",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.async_prepare()
    platform.config_entry = entry
    hass.data["light"]._platforms["simulated_zha"] = platform
    bulbs = []
    for ieee, endpoint_id, name in ((OLD, 1, "ZHA old"), (NEW, 3, "ZHA new")):
        bulb = type(native_old)(name, f"{ieee}-{endpoint_id}")
        bulb._attr_device_info = {"identifiers": {("zha", ieee)}}
        bulbs.append(bulb)
        radio.gateway.devices[EUI64.convert(ieee)].platform_entities[
            ("light", bulb.unique_id)
        ] = SimpleNamespace(endpoint=SimpleNamespace(id=endpoint_id))
    await platform.async_add_entities(bulbs)
    await hass.async_block_till_done()
    old, new = bulbs
    return hass, registry, manager, old, new, radio, entry


async def scan(network):
    hass, registry, _, old, new, _, _ = network
    return await zha.ZhaMigration(hass).async_scan(
        registry.async_get(old.entity_id), registry.async_get(new.entity_id)
    )


async def test_scan_incoming_outgoing_group_and_other_endpoint(network):
    _, _, _, _, _, radio, _ = network
    radio.tables[REMOTE] = [
        raw_binding(),
        raw_binding(cluster=8),
        raw_binding(group=GROUP),
    ]
    radio.tables[OLD] = [
        raw_binding(source=OLD, target=COORDINATOR),
        raw_binding(source=OLD, endpoint=2, target=COORDINATOR),
    ]
    plan = await scan(network)
    assert not plan.failed_reads and not plan.blockers
    assert plan.groups == (GROUP,)
    assert len(plan.bindings) == 3
    incoming = [b for b in plan.bindings if b.source == REMOTE]
    assert {b.cluster for b in incoming} == {6, 8}
    assert all(b.target == NEW and b.target_endpoint == 3 for b in incoming)
    assert any(
        b.source == NEW and b.endpoint == 3 and b.target == COORDINATOR
        for b in plan.bindings
    )
    assert (REMOTE, 2) in radio.reads  # All pages of the remote's table were read.
    assert radio.writes == []


async def test_replace_and_undo_preserve_original_and_preexisting_bindings(network):
    hass, registry, manager, old, new, radio, _ = network
    original = raw_binding()
    existing = raw_binding(target=NEW, target_endpoint=3)
    radio.tables[REMOTE] = [original, existing]
    radio.tables[OLD] = [raw_binding(source=OLD, target=COORDINATOR)]
    old_id, new_id = old.entity_id, new.entity_id
    await manager.async_replace(manager.preview(old_id, new_id), await scan(network))
    assert registry.async_get(old_id).unique_id == new.unique_id
    assert radio.memberships[(NEW, 3)] == {GROUP}
    assert (EUI64.convert(NEW), 3) in radio.app.groups[GROUP]
    assert original in radio.tables[REMOTE] and existing in radio.tables[REMOTE]
    assert raw_binding(source=NEW, endpoint=3, target=COORDINATOR) in radio.tables[NEW]
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": old_id}, blocking=True
    )
    assert len(new.calls) == 1 and old.calls == []
    # Reload from disk: recovery must not depend on in-memory radio objects.
    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    await reloaded.async_restore()
    assert radio.memberships[(NEW, 3)] == set()
    assert (EUI64.convert(NEW), 3) not in radio.app.groups[GROUP]
    assert radio.memberships[(OLD, 1)] == {GROUP}
    assert radio.tables[REMOTE] == [original, existing]
    assert radio.tables[NEW] == []
    assert registry.async_get(old_id).unique_id == old.unique_id
    assert registry.async_get(new_id).unique_id == new.unique_id


async def test_incomplete_scan_requires_explicit_acknowledgement(network):
    _, _, manager, old, new, radio, _ = network
    radio.tables[REMOTE] = [raw_binding()]
    radio.fail_reads.add(OLD)
    radio.fail_groups.add(OLD)
    report = await scan(network)
    assert report.failed_reads and report.groups == (GROUP,)
    with pytest.raises(ReplacementError, match="zha_incomplete"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), report
        )
    assert manager.journal is None and radio.writes == []
    await manager.async_replace(
        manager.preview(old.entity_id, new.entity_id), report, accept_partial=True
    )
    assert raw_binding(target=NEW, target_endpoint=3) in radio.tables[REMOTE]


@pytest.mark.parametrize("failure", ["status", "ignored", "lost_ack", "cancel"])
async def test_failed_or_interrupted_copy_is_recoverable(network, failure):
    hass, registry, manager, old, new, radio, _ = network
    original = raw_binding()
    radio.tables[REMOTE] = [original]
    report = await scan(network)
    old_id, new_id = old.entity_id, new.entity_id
    if failure == "status":
        radio.write_status = zdo.Status.TABLE_FULL
    elif failure == "ignored":
        radio.ignore_bind = True
    elif failure == "lost_ack":
        radio.lost_ack = True
    else:
        radio.cancel_after_bind = True
    exception = asyncio.CancelledError if failure == "cancel" else ReplacementError
    with pytest.raises(exception):
        await manager.async_replace(manager.preview(old_id, new_id), report)
    if failure == "cancel":
        assert manager.journal["status"] == "pending"
        reloaded = ReplacementManager(hass)
        await reloaded.async_load()
        await reloaded.async_restore()
    else:
        assert manager.journal["status"] == "restored"
    assert radio.tables[REMOTE] == [original]
    assert registry.async_get(old_id).unique_id == old.unique_id
    assert registry.async_get(new_id).unique_id == new.unique_id


async def test_unsupported_clusters_and_network_change_block_before_writing(network):
    _, _, manager, old, new, radio, _ = network
    radio.gateway.devices[EUI64.convert(OLD)].device.endpoints[1].add_input_cluster(
        0x0300
    )
    radio.tables[REMOTE] = [raw_binding(cluster=0x0300)]
    report = await scan(network)
    assert report.blockers
    with pytest.raises(ReplacementError, match="zha_incompatible"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), report
        )
    radio.tables[REMOTE] = [raw_binding()]
    report = await scan(network)
    radio.gateway.state.network_info.extended_pan_id = ExtendedPanId.convert(
        "88:77:66:55:44:33:22:11"
    )
    with pytest.raises(ReplacementError, match="zha_network_changed"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), report
        )
    assert radio.writes == [] and manager.journal is None


async def test_truncated_binding_table_is_rejected(network):
    hass, _, _, _, _, radio, _ = network
    radio.gateway.devices[EUI64.convert(REMOTE)].device.zdo.request = AsyncMock(
        return_value=[zdo.Status.SUCCESS, 3, 0, []]
    )
    with pytest.raises(ReplacementError, match="zha_invalid_table"):
        await zha.ZhaMigration(hass).read_bindings(REMOTE)


async def test_group_failure_rolls_back_binding_additions(network):
    _, _, manager, old, new, radio, _ = network
    original = raw_binding()
    radio.tables[REMOTE] = [original]
    radio.group_status = Status.FAILURE
    report = await scan(network)
    with pytest.raises(ReplacementError, match="zha_write_failed"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), report
        )
    assert radio.tables[REMOTE] == [original]
    assert manager.journal["status"] == "restored"


async def test_failed_radio_recovery_remains_pending_until_devices_return(network):
    hass, _, manager, old, new, radio, _ = network
    original = raw_binding()
    radio.tables[REMOTE] = [original]
    report = await scan(network)
    radio.lost_ack = True
    radio.fail_unbind = True
    with pytest.raises(ReplacementError, match="recovery_required"):
        await manager.async_replace(
            manager.preview(old.entity_id, new.entity_id), report
        )
    assert manager.journal["status"] == "recovery_required"
    with pytest.raises(ReplacementError, match="recovery_required"):
        manager.preview(old.entity_id, new.entity_id)
    radio.fail_unbind = False
    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    await reloaded.async_restore()
    assert radio.tables[REMOTE] == [original]
    assert reloaded.journal["status"] == "restored"


async def test_existing_groups_and_bindings_are_not_owned_or_removed(network):
    _, _, manager, old, new, radio, _ = network
    radio.tables[REMOTE] = [raw_binding(), raw_binding(target=NEW, target_endpoint=3)]
    radio.memberships[(NEW, 3)] = {GROUP, 0x7777}
    await manager.async_replace(
        manager.preview(old.entity_id, new.entity_id), await scan(network)
    )
    await manager.async_restore()
    assert len(radio.tables[REMOTE]) == 2
    assert radio.memberships[(NEW, 3)] == {GROUP, 0x7777}
    assert radio.writes == []


async def test_zha_options_flow_scan_copy_and_restore(network):
    hass, registry, manager, old, new, radio, entry = network
    radio.tables[REMOTE] = [raw_binding()]
    old_id, new_id = old.entity_id, new.entity_id
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    result = await options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": old_id,
            "new_entity_id": new_id,
            "copy_zha": True,
        },
    )
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert radio.writes == []
    await hass.async_block_till_done()
    result = await options.async_configure(result["flow_id"])
    assert result["step_id"] == "confirm_zha"
    assert REMOTE in result["description_placeholders"]["zha_bindings"]
    result = await options.async_configure(result["flow_id"], {"confirm": True})
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
    assert manager.journal["status"] == "restored"


async def test_partial_scan_cannot_be_confirmed_by_default(network):
    hass, _, manager, old, new, radio, entry = network
    radio.fail_reads.add(REMOTE)
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    result = await options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": old.entity_id,
            "new_entity_id": new.entity_id,
            "copy_zha": True,
        },
    )
    await hass.async_block_till_done()
    result = await options.async_configure(result["flow_id"])
    assert result["step_id"] == "confirm_zha"
    assert REMOTE in result["description_placeholders"]["zha_missing"]
    result = await options.async_configure(result["flow_id"], {"confirm": True})
    assert result["errors"] == {"base": "zha_incomplete"}
    assert manager.journal is None and radio.writes == []


async def test_adapter_reuses_home_assistant_zha_gateway(wizard):
    hass, _, _, _, _, _ = wizard
    with pytest.raises(ReplacementError, match="zha_unavailable"):
        zha.ZhaMigration(hass)
    # Exercise the actual HA helper, including its optional USB/radio imports.
    # Merely retrieving this object must not create a second radio connection.
    from homeassistant.components.zha.helpers import HAZHAData

    gateway = object()
    hass.data["zha"] = HAZHAData(gateway_proxy=SimpleNamespace(gateway=gateway))
    hass.config.components.add("zha")
    assert zha.ZhaMigration(hass).gateway is gateway
