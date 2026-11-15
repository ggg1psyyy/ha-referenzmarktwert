"""Diagnostics download (Settings > Devices & services > the entry > Download diagnostics)."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ReferenzmarktwertConfigEntry
from .const import CONF_ECONTROL_HEADER_VALUE, CONF_HEADER_VALUE

TO_REDACT = {CONF_HEADER_VALUE, CONF_ECONTROL_HEADER_VALUE}  # may hold an API key


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ReferenzmarktwertConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    state = coordinator.state
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "estimate": asdict(data.estimate) if data else None,
        "official_months": len(data.official) if data else 0,
        "latest_official": asdict(data.latest_official) if data and data.latest_official else None,
        "official_checked": data.official_checked.isoformat() if data and data.official_checked else None,
        # what is stored (no sunrise/sunset-based times: they would reveal the home location)
        "stored": {
            "generation_points": len(state.generation),
            "generation_since": state.generation.since,
            "price_points": len(state.price),
            "price_since": state.price.since,
            "reference_month": state.reference_month,
            "official_last_modified": state.official_last_modified,
            "rate_limited_until": state.not_before,
        },
    }
