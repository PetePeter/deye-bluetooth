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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .helpers import daily_calc

# (key, name, unit, device_class, state_class, icon, precision)
SENSORS: list[tuple] = [
    ("solar_power",             "Solar Power",            "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power",               0),
    ("house_load",              "House Load",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:home-lightning-bolt",       0),
    ("grid_power",              "Grid Power",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:transmission-tower",        0),
    ("battery_power",           "Battery Power",          "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:battery-charging",          0),
    ("ups_power",               "UPS Power",              "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:power-plug",                0),
    ("battery_soc",             "Battery SOC",            "%",   SensorDeviceClass.BATTERY,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("battery_voltage",         "Battery Voltage",        "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            2),
    ("battery_temp",            "Battery Temperature",    "°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT,    None,                            1),
    ("inverter_temp",           "Inverter Temperature",   "°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT,    None,                            1),
    ("daily_solar",             "Solar Today",            "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:solar-power",             2),
    ("daily_grid_import",       "Grid Import Today",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-import", 2),
    ("daily_grid_export",       "Grid Export Today",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-export", 2),
    ("total_solar",             "Solar Total",            "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("total_grid_import",       "Grid Import Total",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-import", 2),
    ("total_grid_export",       "Grid Export Total",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-export", 2),
    ("total_battery_charge",    "Battery Charge Total",   "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("total_battery_discharge", "Battery Discharge Total","kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("max_sell_power",          "Max Sell Power",         "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:transmission-tower-export", 0),
    ("inverter_power_l1",      "Inverter Output L1",    "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:lightning-bolt",            0),
    ("inverter_power_l2",      "Inverter Output L2",    "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:lightning-bolt",            0),
    ("inverter_power_l3",      "Inverter Output L3",    "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:lightning-bolt",            0),
    ("grid_voltage_l1",        "Grid Voltage L1",       "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("grid_voltage_l2",        "Grid Voltage L2",       "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("grid_voltage_l3",        "Grid Voltage L3",       "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("grid_frequency",         "Grid Frequency",        "Hz",  SensorDeviceClass.FREQUENCY,   SensorStateClass.MEASUREMENT,    None,                            2),
    ("bms_charge_voltage",     "BMS Charge Voltage",    "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            2),
    ("bms_discharge_voltage",  "BMS Discharge Voltage", "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            2),
    ("bms_charge_current_limit",    "BMS Charge Current Limit",    "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None,                       0),
    ("bms_discharge_current_limit", "BMS Discharge Current Limit", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None,                       0),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [DeyeSensor(coordinator, entry, *row) for row in SENSORS]
    entities.append(DeyeDailyConsumptionSensor(coordinator, entry))
    async_add_entities(entities)


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key, name, unit,
                 device_class, state_class, icon, precision):
        super().__init__(coordinator)
        self._key = key
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_name = name
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_suggested_display_precision = precision
        if icon:
            self._attr_icon = icon
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)


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
        self._attr_device_info = _device_info(sn)
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
