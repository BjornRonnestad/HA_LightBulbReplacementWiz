"""Replace a light without changing its hardware identity."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .replacement import ReplacementManager


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the persistent replacement journal."""
    manager = ReplacementManager(hass)
    await manager.async_load()
    hass.data[DOMAIN] = manager
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Keep the journal on disk when the integration is unloaded."""
    manager: ReplacementManager = hass.data[DOMAIN]
    if manager.lock.locked():
        return False
    hass.data.pop(DOMAIN)
    return True
