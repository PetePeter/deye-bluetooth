"""Sensor entities mirroring the ha-deyecloud-bridge sensor set.

All telemetry sensors are thin CoordinatorEntity wrappers — logic lives in
registers.decode (P2) and helpers.daily_calc (P4). No non-trivial logic here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_LOGGER_SN, DOMAIN, SYNC_RESULTS
from .entity import device_info
from .helpers import daily_calc
from .models.base import Sensor as SensorDescription
from .registers import active_tou_slot


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    model = coordinator.model
    keys = {point.key for point in model.points}
    entities: list[SensorEntity] = []
    for point in model.points:
        for description in point.shows:
            if not isinstance(description, SensorDescription):
                continue
            if point.key == "inverter_clock":
                entities.append(DeyeInverterClockSensor(coordinator, entry, description))
            else:
                entities.append(DeyeSensor(coordinator, entry, point.key, description))

    if (
        model.capabilities.has_battery
        and "total_consumption" in keys
        and "daily_load" not in keys
    ):
        entities.append(DeyeDailyConsumptionSensor(coordinator, entry))
    if model.capabilities.has_tou and "tou_slots" in keys:
        entities.append(DeyeTouScheduleSensor(coordinator, entry))
    if "inverter_clock" in keys:
        entities.append(DeyeClockSyncResultSensor(coordinator, entry))
        entities.append(DeyeClockDriftSensor(coordinator, entry))
    if model.capabilities.cloud_time_sync:
        entities.append(DeyeClockTimeOfDayDriftSensor(coordinator, entry))
    async_add_entities(entities)



class DeyeSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator, entry: ConfigEntry, key: str, description: SensorDescription
    ):
        super().__init__(coordinator)
        self._key = key
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_name = description.name
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = (
            SensorDeviceClass(description.device_class) if description.device_class else None
        )
        self._attr_state_class = (
            SensorStateClass(description.state_class) if description.state_class else None
        )
        self._attr_suggested_display_precision = description.precision
        if description.icon:
            self._attr_icon = description.icon
        if description.entity_category:
            self._attr_entity_category = EntityCategory(description.entity_category)
        self._attr_device_info = device_info(sn)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)


class DeyeTouScheduleSensor(DeyeSensor):
    """Time-of-use program: state is the slot active now, slots as attributes.

    The inverter compares slot times against its own RTC, so "now" is the
    inverter clock when it has been read, HA's local time otherwise.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "tou_schedule",
            SensorDescription("Time of Use", icon="mdi:calendar-clock"),
        )

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        slots = data.get("tou_slots")
        if not slots:
            return None
        if not data.get("tou_enabled"):
            return "Disabled"
        now = data.get("inverter_clock") or dt_util.now()
        index = active_tou_slot(slots, now.strftime("%H:%M"))
        if index is None:
            return None
        slot = slots[index]
        return f"Program {index + 1} ({slot['start']}-{slot['end']}, {slot['soc']} %)"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        if not data.get("tou_slots"):
            return None
        return {
            "enabled": data.get("tou_enabled"),
            "days": data.get("tou_days"),
            "slots": data["tou_slots"],
        }


# -- Inverter clock (diagnostic) ----------------------------------------------

class DeyeInverterClockSensor(DeyeSensor):
    """The inverter's own real-time clock, as read on the config cycle.

    The device reports a bare wall clock with no timezone; HA's timestamp
    device class needs an aware datetime, so site time is attached here — at the
    entity boundary, where the assumption is visible — rather than in the decode.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        description: SensorDescription | None = None,
    ):
        if description is None:
            point = next(point for point in coordinator.model.points if point.key == "inverter_clock")
            description = next(
                show for show in point.shows if isinstance(show, SensorDescription)
            )
        super().__init__(
            coordinator, entry, "inverter_clock", description,
        )

    @property
    def native_value(self):
        value = super().native_value
        if value is None:
            return None
        return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)



class DeyeClockSyncResultSensor(CoordinatorEntity, SensorEntity):
    """The result of the last clock sync — deliberately NOT a device reading.

    Availability is fixed True rather than following the coordinator, and that
    is the whole point of the entity existing separately. Every other entity
    here reports what the inverter currently says, so when polling fails they
    are rightly unavailable — and an unavailable entity has its attributes
    stripped by HA entirely. That would take the sync result down with the
    telemetry: a sync that verified, followed by one failed poll, would leave a
    verifier seeing nothing and calling a good sync a failure. What we know
    about a completed sync does not stop being true when the radio drops.

    Reads "unknown" until a sync has actually run — never a fabricated success.
    """

    _attr_has_entity_name = True
    _attr_name = "Clock Sync Result"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SYNC_RESULTS

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_clock_sync_result"
        self._attr_device_info = device_info(sn)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("clock_sync_result")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        if "clock_sync_result" not in data:
            return None
        return {
            # sync_id identifies the run: a verifier compares it against the
            # value it saw before calling the service. sync_at is for humans —
            # comparing timestamps would assume a monotonic wall clock, which
            # is precisely what this feature exists to fix.
            "sync_id": data["clock_sync_id"],
            "sync_attempts": data["clock_sync_attempts"],
            "sync_at": data["clock_sync_at"],
        }


class DeyeClockDriftSensor(DeyeSensor):
    """How far the inverter clock had wandered at the last clock read.

    Positive means the inverter is running ahead of HA. The value is a snapshot
    taken when the clock was read (up to CONFIG_READ_INTERVAL old), not a live
    countdown — see helpers.clock_drift_seconds.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "inverter_clock_drift",
            SensorDescription(
                "Inverter Clock Drift", "s", "duration", "measurement",
                "mdi:clock-alert-outline", 0, "diagnostic",
            ),
        )


class DeyeClockTimeOfDayDriftSensor(DeyeSensor):
    """Clock drift with the date ignored — the time-of-day component alone.

    Kept alongside the total drift, not instead of it: a wrong year is a real
    fault worth seeing, but it swamps the minutes-scale offset that identifies
    something actively rewriting the RTC. Wrapped to +/-12h so a small offset
    straddling midnight can't masquerade as a near-24h jump.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "inverter_time_of_day_drift",
            SensorDescription(
                "Inverter Clock Drift (Time of Day)", "s", "duration", "measurement",
                "mdi:clock-fast", 0, "diagnostic",
            ),
        )


# -- Daily consumption (RestoreEntity, midnight baseline) ---------------------

@dataclass
class _DailyExtraData(ExtraStoredData):
    baseline: float | None
    day: str | None

    def as_dict(self) -> dict:
        return {"baseline": self.baseline, "day": self.day}


class DeyeDailyConsumptionSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Daily energy consumption derived from the lifetime total register.

    Records the cumulative ``total_consumption`` at midnight and reports the
    difference.  Baseline persists across HA restarts via RestoreEntity extra
    data.  The pure math lives in ``helpers.daily_calc`` (unit-tested).
    """

    _attr_has_entity_name = True
    _attr_name = "Consumption Today"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_daily_consumption"
        self._attr_device_info = device_info(sn)
        self._baseline: float | None = None
        self._day: date | None = None
        self._value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            self._baseline = data.get("baseline")
            stored_day = data.get("day")
            self._day = date.fromisoformat(stored_day) if stored_day else None
        self._recalculate()

    @property
    def extra_restore_state_data(self) -> _DailyExtraData:
        return _DailyExtraData(
            self._baseline,
            self._day.isoformat() if self._day else None,
        )

    @property
    def native_value(self):
        return self._value

    @callback
    def _handle_coordinator_update(self) -> None:
        self._recalculate()
        super()._handle_coordinator_update()

    def _recalculate(self) -> None:
        if self.coordinator.data is None:
            return
        total = self.coordinator.data.get("total_consumption")
        today = dt_util.now().date()
        self._baseline, self._day, self._value = daily_calc(
            self._baseline, self._day, total, today,
        )
