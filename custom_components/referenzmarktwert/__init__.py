"""Referenzmarktwert PV: the Austrian reference market value (§ 13 EAG) as Home Assistant sensors."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .cache import STORAGE_VERSION
from .coordinator import ReferenzmarktwertCoordinator, storage_key

PLATFORMS: list[Platform] = [Platform.SENSOR]

# A config entry whose `runtime_data` is our coordinator (typed, so editors and type checkers know).
type ReferenzmarktwertConfigEntry = ConfigEntry[ReferenzmarktwertCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry) -> bool:
    """Set up one config entry: first refresh (fails -> HA retries later), then the sensors."""
    coordinator = ReferenzmarktwertCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry) -> bool:
    """Unload a config entry (on removal, reload or options change)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry) -> None:
    """The entry was deleted: delete its stored downloads too."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
