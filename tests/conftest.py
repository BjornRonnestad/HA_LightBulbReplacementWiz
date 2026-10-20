"""A real Home Assistant instance with two controllable, registered lights."""

from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.light_bulb_replacement.const import DOMAIN
from custom_components.light_bulb_replacement.replacement import ReplacementManager


class Bulb(LightEntity):
    """Exercise HA's real entity-registry rename listeners and light services."""

    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_is_on = False

    def __init__(self, name: str, unique_id: str) -> None:
        self._attr_name = name
        self._attr_unique_id = unique_id
        self.calls: list[dict] = []

    async def async_turn_on(self, **kwargs) -> None:
        self.calls.append(kwargs)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


@pytest.fixture
async def runtime(tmp_path):
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components",
        target_is_directory=True,
    )
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    loader.async_setup(hass)
    assert await async_load_base_functionality(hass)
    assert await async_setup_component(hass, "light", {})
    registry = er.async_get(hass)
    old, new = Bulb("Living room", "old-bulb"), Bulb("New bulb", "new-bulb")
    await hass.data["light"].async_add_entities([old, new])
    await hass.async_block_till_done()
    manager = ReplacementManager(hass)
    await manager.async_load()
    hass.data[DOMAIN] = manager
    try:
        yield hass, registry, manager, old, new
    finally:
        await hass.async_stop(force=True)
        await hass.async_block_till_done()


@pytest.fixture
async def wizard(runtime):
    """Install through HA's actual config-flow and entry lifecycle."""
    hass, registry, _, old, new = runtime
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = result["result"]
    return hass, registry, hass.data[DOMAIN], old, new, entry
