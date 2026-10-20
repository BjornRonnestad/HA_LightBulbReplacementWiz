"""Replacement behavior, real services, references and failure recovery."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from custom_components.light_bulb_replacement.replacement import (
    ReplacementError,
    ReplacementManager,
)


async def test_replace_and_control_original_id(runtime):
    hass, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    old_entry, new_entry = registry.async_get(old_id), registry.async_get(new_id)
    plan = manager.preview(old_id, new_id)
    await manager.async_replace(plan)
    await hass.async_block_till_done()

    assert registry.async_get(old_id).unique_id == new_entry.unique_id
    assert registry.async_get(old_id).id == new_entry.id
    archived = registry.async_get(f"{old_id}_old")
    assert archived.unique_id == old_entry.unique_id
    assert archived.name == "Living room_OLD"
    assert archived.disabled_by == er.RegistryEntryDisabler.USER
    assert registry.async_get(old_id).name == "Living room"
    assert registry.async_get(new_id) is None
    assert hass.states.get(new_id) is None
    assert manager.journal["status"] == "completed"

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": old_id}, blocking=True
    )
    assert len(new.calls) == 1
    assert old.calls == []


async def test_copy_metadata_and_restore_after_manager_reload(runtime):
    hass, registry, manager, old, new = runtime
    area = ar.async_get(hass).async_create("Living room")
    old_id, new_id = old.entity_id, new.entity_id
    registry.async_update_entity(
        old_id, area_id=area.id, aliases=["Reading lamp"], icon="mdi:lamp"
    )
    before_old, before_new = registry.async_get(old_id), registry.async_get(new_id)
    await manager.async_replace(manager.preview(old_id, new_id))
    replacement = registry.async_get(old_id)
    assert replacement.area_id == area.id
    assert replacement.aliases == ["Reading lamp"]
    assert replacement.icon == "mdi:lamp"
    assert registry.async_get(f"{old_id}_old").aliases == []

    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    assert reloaded.journal["status"] == "completed"
    await reloaded.async_restore()
    await hass.async_block_till_done()
    for before in (before_old, before_new):
        after = registry.async_get(before.entity_id)
        for field in (
            "id",
            "unique_id",
            "area_id",
            "name",
            "aliases",
            "icon",
            "disabled_by",
        ):
            assert getattr(after, field) == getattr(before, field)
    assert reloaded.journal["status"] == "restored"


@pytest.mark.parametrize("target", ["same", "missing", "unavailable", "collision"])
async def test_invalid_selection_does_not_change_registry(runtime, target):
    hass, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    expected = {
        "same": "same_entity",
        "missing": "invalid_light",
        "unavailable": "new_unavailable",
        "collision": "archive_exists",
    }[target]
    if target == "same":
        new_id = old_id
    elif target == "missing":
        new_id = "light.missing"
    elif target == "unavailable":
        hass.states.async_set(new_id, "unavailable")
    elif target == "collision":
        hass.states.async_set(f"{old_id}_old", "off")
    before = list(registry.entities.values())
    with pytest.raises(ReplacementError, match=expected):
        manager.preview(old_id, new_id)
    assert list(registry.entities.values()) == before


async def test_stale_preview_rejected(runtime):
    _, registry, manager, old, new = runtime
    plan = manager.preview(old.entity_id, new.entity_id)
    registry.async_update_entity(new.entity_id, name="Changed in another browser")
    with pytest.raises(ReplacementError, match="selection_changed"):
        await manager.async_replace(plan)
    assert manager.journal is None


async def test_failed_second_rename_rolls_back(runtime):
    hass, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    old_entry, new_entry = registry.async_get(old_id), registry.async_get(new_id)
    update = registry.async_update_entity

    def fail_once(entity_id, **kwargs):
        if entity_id == new_id and kwargs.get("new_entity_id") == old_id:
            raise ValueError("Injected rename failure")
        return update(entity_id, **kwargs)

    with patch.object(registry, "async_update_entity", side_effect=fail_once):
        with pytest.raises(ReplacementError, match="replacement_failed"):
            await manager.async_replace(manager.preview(old_id, new_id))
    await hass.async_block_till_done()
    assert registry.async_get(old_id).id == old_entry.id
    assert registry.async_get(new_id).id == new_entry.id
    assert registry.async_get(f"{old_id}_old") is None
    assert manager.journal["status"] == "restored"


async def test_journal_write_failure_leaves_registry_unchanged(runtime):
    _, registry, manager, old, new = runtime
    before = list(registry.entities.values())
    with patch.object(
        manager.store, "async_save", AsyncMock(side_effect=OSError("Disk full"))
    ):
        with pytest.raises(OSError):
            await manager.async_replace(manager.preview(old.entity_id, new.entity_id))
    assert list(registry.entities.values()) == before
    assert manager.journal is None


async def test_interruption_is_recoverable_from_persistent_journal(runtime):
    hass, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    old_entry = registry.async_get(old_id)
    with patch.object(
        manager, "_wait_released", AsyncMock(side_effect=asyncio.CancelledError)
    ):
        with pytest.raises(asyncio.CancelledError):
            await manager.async_replace(manager.preview(old_id, new_id))
    await hass.async_block_till_done()
    reloaded = ReplacementManager(hass)
    await reloaded.async_load()
    assert reloaded.journal["status"] == "pending"
    with pytest.raises(ReplacementError, match="recovery_required"):
        reloaded.preview(f"{old_id}_old", new_id)
    await reloaded.async_restore()
    assert registry.async_get(old_id).id == old_entry.id


async def test_restore_does_not_overwrite_an_unrelated_entity(runtime):
    _, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    await manager.async_replace(manager.preview(old_id, new_id))
    stranger = registry.async_get_or_create(
        "light", "test", "stranger", suggested_object_id=new_id.split(".")[1]
    )
    assert stranger.entity_id == new_id
    with pytest.raises(ReplacementError, match="recovery_conflict"):
        await manager.async_restore()
    assert registry.async_get(new_id).id == stranger.id
    assert registry.async_get(old_id).unique_id == "new-bulb"


async def test_rollback_failure_retains_journal_for_recovery(runtime):
    hass, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    update = registry.async_update_entity

    def fail_moving_back_to_old_id(entity_id, **kwargs):
        if kwargs.get("new_entity_id") == old_id:
            raise ValueError("Injected failure")
        return update(entity_id, **kwargs)

    with patch.object(
        registry, "async_update_entity", side_effect=fail_moving_back_to_old_id
    ):
        with pytest.raises(ReplacementError, match="recovery_required"):
            await manager.async_replace(manager.preview(old_id, new_id))
    await hass.async_block_till_done()
    assert manager.journal["status"] == "recovery_required"
    await manager.async_restore()
    assert registry.async_get(old_id).unique_id == "old-bulb"
    assert registry.async_get(new_id).unique_id == "new-bulb"


async def test_metadata_edit_while_journal_is_saved_aborts_without_changes(runtime):
    _, registry, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    save = manager.store.async_save
    changed = False

    async def edit_while_saving(data):
        nonlocal changed
        await save(data)
        if not changed:
            registry.async_update_entity(new_id, name="External edit")
            changed = True

    with patch.object(manager.store, "async_save", side_effect=edit_while_saving):
        with pytest.raises(ReplacementError, match="selection_changed"):
            await manager.async_replace(manager.preview(old_id, new_id))
    assert registry.async_get(old_id).unique_id == "old-bulb"
    assert registry.async_get(new_id).name == "External edit"


async def test_offline_old_bulb_can_be_replaced(runtime):
    hass, _, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    hass.states.async_set(old_id, "unavailable")
    await manager.async_replace(manager.preview(old_id, new_id))
    assert new.entity_id == old_id


async def test_device_automations_block_replacement(wizard):
    hass, registry, manager, old, new, entry = wizard

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "old-bulb")},
        name="Old physical bulb",
    )
    registry.async_update_entity(
        old.entity_id, device_id=device.id, config_entry_id=entry.entry_id
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Device reference",
                    "triggers": [{"trigger": "event", "event_type": "test_device"}],
                    "actions": [
                        {"action": "light.turn_on", "target": {"device_id": device.id}}
                    ],
                }
            ],
        },
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "device_reference": {
                    "sequence": [
                        {"action": "light.turn_on", "target": {"device_id": device.id}},
                    ]
                }
            },
        },
    )
    await hass.async_block_till_done()
    plan = manager.preview(old.entity_id, new.entity_id)
    assert set(plan.blockers) == {
        "automation.device_reference",
        "script.device_reference",
    }
    with pytest.raises(ReplacementError, match="unsupported_references"):
        await manager.async_replace(plan)
    assert manager.journal is None


async def test_device_area_and_labels_are_copied_without_moving_devices(wizard):
    hass, registry, manager, old, new, entry = wizard
    area = ar.async_get(hass).async_create("Living room")
    labels = lr.async_get(hass)
    entity_label = labels.async_create("Entity label")
    device_label = labels.async_create("Device label")
    new_label = labels.async_create("New label")
    devices = dr.async_get(hass)
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "old")}, name="Old device"
    )
    old_device = devices.async_update_device(
        old_device.id, area_id=area.id, labels={device_label.label_id}
    )
    new_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "new")}, name="New device"
    )
    old_id, new_id = old.entity_id, new.entity_id
    # Model a config-entry-backed hardware platform so HA can re-add these
    # simulated lights on rename without clearing their device association.
    old.platform.config_entry = entry
    old._attr_device_info = {"identifiers": {("test", "old")}}
    new._attr_device_info = {"identifiers": {("test", "new")}}
    registry.async_update_entity(
        old_id,
        device_id=old_device.id,
        labels={entity_label.label_id},
        config_entry_id=entry.entry_id,
    )
    registry.async_update_entity(
        new_id,
        device_id=new_device.id,
        labels={new_label.label_id},
        config_entry_id=entry.entry_id,
    )
    plan = manager.preview(old_id, new_id)
    await manager.async_replace(plan)
    replacement = registry.async_get(old_id)
    assert replacement.area_id == area.id
    assert replacement.labels == {
        entity_label.label_id,
        device_label.label_id,
        new_label.label_id,
    }
    assert replacement.device_id == new_device.id
    assert devices.async_get(old_device.id) == old_device
    assert devices.async_get(new_device.id) == new_device
    assert registry.async_get(f"{old_id}_old").labels == set()
    await manager.async_restore()
    assert registry.async_get(old_id).labels == {entity_label.label_id}
    assert registry.async_get(new_id).labels == {new_label.label_id}


@pytest.mark.parametrize("use_uuid", [False, True])
async def test_light_group_helper_reference_handling(wizard, use_uuid):
    hass, registry, manager, old, new, _ = wizard
    old_id, new_id = old.entity_id, new.entity_id
    member = registry.async_get(old_id).id if use_uuid else old_id
    group_entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="group",
        data={},
        options={"group_type": "light", "entities": [member], "hide_members": False},
        source="user",
        title="Reading group",
        unique_id=None,
        discovery_keys={},
        subentries_data=[],
    )
    await hass.config_entries.async_add(group_entry)
    await hass.async_start()
    await hass.async_block_till_done()
    plan = manager.preview(old_id, new_id)
    if use_uuid:
        assert plan.blockers == ("group: Reading group",)
        with pytest.raises(ReplacementError, match="unsupported_references"):
            await manager.async_replace(plan)
        return
    await manager.async_replace(plan)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.reading_group"}, blocking=True
    )
    assert len(new.calls) == 1
    assert old.calls == []
    assert await hass.config_entries.async_reload(group_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.reading_group"}, blocking=True
    )
    assert len(new.calls) == 2


async def test_entity_references_in_group_scene_and_automation(runtime):
    hass, _, manager, old, new = runtime
    old_id, new_id = old.entity_id, new.entity_id
    assert await async_setup_component(
        hass,
        "group",
        {
            "group": {"test_lights": {"entities": [old_id]}},
        },
    )
    assert await async_setup_component(
        hass,
        "scene",
        {
            "scene": [
                {
                    "name": "Test scene",
                    "entities": {old_id: {"state": "on", "brightness": 77}},
                }
            ],
        },
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Test bulb automation",
                    "triggers": [{"trigger": "event", "event_type": "test_bulb"}],
                    "actions": [
                        {"action": "light.turn_on", "target": {"entity_id": old_id}}
                    ],
                }
            ],
        },
    )
    await hass.async_start()
    await hass.async_block_till_done()
    await manager.async_replace(manager.preview(old_id, new_id))
    await hass.async_block_till_done()

    await hass.services.async_call(
        "homeassistant", "turn_on", {"entity_id": "group.test_lights"}, blocking=True
    )
    assert len(new.calls) == 1
    await hass.services.async_call(
        "scene", "turn_on", {"entity_id": "scene.test_scene"}, blocking=True
    )
    assert new.calls[-1]["brightness"] == 77
    before = len(new.calls)
    hass.bus.async_fire("test_bulb")
    await hass.async_block_till_done()
    assert len(new.calls) == before + 1
    assert old.calls == []
    assert hass.states.get(new_id) is None
