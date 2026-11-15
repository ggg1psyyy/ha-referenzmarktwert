"""Referenzmarktwert PV: the Austrian reference market value (§ 13 EAG) as Home Assistant sensors."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import CacheStore, ReferenzmarktwertConfigEntry, ReferenzmarktwertCoordinator, deprecation_issue_id

PLATFORMS: list[Platform] = [Platform.SENSOR]


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
    """The entry was deleted: delete its stored downloads and its repair notice too."""
    await CacheStore(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, deprecation_issue_id(entry.entry_id))
