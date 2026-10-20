"""Run the wizard through Home Assistant's real flow manager."""

from homeassistant import loader

from custom_components.light_bulb_replacement.const import DOMAIN


async def test_wizard_is_discoverable_under_add_integration(runtime):
    hass, _, _, _, _ = runtime
    descriptions = await loader.async_get_integration_descriptions(hass)
    assert DOMAIN in descriptions["custom"]["integration"]
    assert DOMAIN not in descriptions["custom"]["helper"]
    assert DOMAIN in await loader.async_get_config_flows(hass)


async def test_single_instance_and_entry_unload(wizard):
    hass, _, _, _, _, entry = wizard
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DOMAIN not in hass.data


async def test_preview_requires_confirmation_and_restore(wizard):
    hass, registry, manager, old, new, entry = wizard
    old_id, new_id = old.entity_id, new.entity_id
    old_unique_id = registry.async_get(old_id).unique_id
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    assert result["step_id"] == "select"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"old_entity_id": old_id, "new_entity_id": new_id}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["archived"] == f"{old_id}_old"
    assert registry.async_get(old_id).unique_id == old_unique_id
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    assert result["errors"] == {"base": "confirmation_required"}
    assert manager.journal is None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] == "create_entry"
    assert manager.journal["status"] == "completed"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "restore"}
    )
    assert result["step_id"] == "restore"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] == "create_entry"
    assert registry.async_get(old_id).unique_id == old_unique_id


async def test_validation_error_in_select(wizard):
    hass, _, _, old, _, entry = wizard
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "select"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": old.entity_id,
            "new_entity_id": old.entity_id,
        },
    )
    assert result["errors"] == {"base": "same_entity"}
