"""Sensors: running-month estimate and latest official value, each also minus the deduction.

All values are EUR/kWh, the unit the Energy Dashboard expects for prices.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .calc import Month, payout
from .const import DOMAIN
from .coordinator import ReferenzmarktwertConfigEntry, ReferenzmarktwertCoordinator, ReferenzmarktwertData

# Coordinator-based entities never update themselves, so no limit on parallel updates is needed.
PARALLEL_UPDATES = 0

# "EUR/kWh": the Energy Dashboard only accepts prices ending in /kWh (or another energy unit).
# The currency is the ISO code, matching Home Assistant's currency setting.
PRICE_UNIT = f"EUR/{UnitOfEnergy.KILO_WATT_HOUR}"


def month_text(month: Month) -> str:
    return f"{month[0]}-{month[1]:02d}"


def timestamp(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None else None


@dataclass(frozen=True, kw_only=True)
class ReferenzmarktwertSensorDescription(SensorEntityDescription):
    """A sensor's description plus how to get its value and attributes from the data."""

    value_fn: Callable[[ReferenzmarktwertData], float | None]
    attributes_fn: Callable[[ReferenzmarktwertData], dict[str, Any]] = lambda _: {}


def _estimate_attributes(data: ReferenzmarktwertData) -> dict[str, Any]:
    e = data.estimate
    return {
        "month": month_text(e.month),
        "month_so_far": e.month_so_far,
        "known_share": round(e.known_share, 4) if e.known_share is not None else None,
        "prior": e.prior,
        "running_base_price": e.running_base,
        "previous_month": e.previous_month,
        "previous_month_source": e.previous_source.value,
        "generation_until": timestamp(e.generation_until),
        "prices_until": timestamp(e.prices_until),
        "method": e.method.value,
    }


def _official_attributes(data: ReferenzmarktwertData) -> dict[str, Any]:
    latest = data.latest_official
    if latest is None:
        return {}
    return {
        "month": month_text(latest.month),
        "published": latest.published.isoformat() if latest.published else None,
        "base_price": latest.base,
        "checked": data.official_checked.isoformat() if data.official_checked else None,
        "history": {month_text(m): v.value for m, v in sorted(data.official.items())},
    }


def _payout(value: float | None, data: ReferenzmarktwertData) -> float | None:
    return payout(value, data.deduction) if value is not None else None


def _official_value(data: ReferenzmarktwertData) -> float | None:
    latest = data.latest_official
    return latest.value if latest else None


SENSORS: tuple[ReferenzmarktwertSensorDescription, ...] = (
    ReferenzmarktwertSensorDescription(
        key="estimate",
        translation_key="estimate",
        value_fn=lambda d: d.estimate.value,
        attributes_fn=_estimate_attributes,
    ),
    ReferenzmarktwertSensorDescription(
        key="payout_estimate",
        translation_key="payout_estimate",
        value_fn=lambda d: _payout(d.estimate.value, d),
        attributes_fn=lambda d: {"month": month_text(d.estimate.month), "deduction": d.deduction},
    ),
    ReferenzmarktwertSensorDescription(
        key="official",
        translation_key="official",
        value_fn=_official_value,
        attributes_fn=_official_attributes,
    ),
    ReferenzmarktwertSensorDescription(
        key="payout_official",
        translation_key="payout_official",
        value_fn=lambda d: _payout(_official_value(d), d),
        attributes_fn=lambda d: (
            {"month": month_text(d.latest_official.month), "deduction": d.deduction} if d.latest_official else {}
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReferenzmarktwertConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ReferenzmarktwertSensor(coordinator, description) for description in SENSORS)


class ReferenzmarktwertSensor(CoordinatorEntity[ReferenzmarktwertCoordinator], SensorEntity):
    """One value from the coordinator's data."""

    entity_description: ReferenzmarktwertSensorDescription
    _attr_has_entity_name = True  # name = device name + translated entity name
    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _unrecorded_attributes = frozenset({"history"})  # the month table would bloat the database

    def __init__(
        self, coordinator: ReferenzmarktwertCoordinator, description: ReferenzmarktwertSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="E-Control / Energy-Charts",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attributes_fn(self.coordinator.data)
