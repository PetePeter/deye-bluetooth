"""Sensor definitions and entity implementations for Deye BLE."""
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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN, SYNC_RESULTS
from .helpers import daily_calc

# --- SENSORS CLASSIFICATION (BLE LD, BLE FD, BLE SD) ---
# Format: (key, name, unit, device_class, state_class, icon, precision)
SENSORS: list[tuple] = [
    # 🔴 TIER A: LIVE DATA (BLE LD)
    ("solar_power",             "Solar Power BLE LD",            "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power",               0),
    ("battery_power",           "Battery Power BLE LD",          "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:battery-charging",          0),
    ("battery_soc",             "Battery SOC BLE LD",            "%",   SensorDeviceClass.BATTERY,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("grid_power",              "Grid Power BLE LD",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:transmission-tower",        0),
    ("grid_voltage_l1",         "Grid Voltage L1 BLE LD",        "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("grid_current_l1",         "Grid Current L1 BLE LD",        "A",   SensorDeviceClass.CURRENT,     SensorStateClass.MEASUREMENT,    "mdi:transmission-tower",        2),
    ("internal_ct_l1_power",    "Internal CT L1 Power BLE LD",   "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:current-ac",                0),
    ("external_ct_l1_power",    "External CT L1 Power BLE LD",   "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:current-ac",                0),
    ("house_load",              "House Load BLE LD",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:home-lightning-bolt",       0),
    ("ups_power",               "UPS Power BLE LD",              "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:power-plug",                0),
    ("gen_power",               "SmartLoad Power BLE LD",        "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:home-battery",              0),
    ("ups_load_power",          "UPS Load Power BLE LD",         "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:power-plug",                0),
    ("ac_voltage_l1",           "AC Voltage L1 BLE LD",          "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            1),

    # 🟡 TIER STANDARD: FAST DATA (BLE FD)
    ("pv1_power",               "PV1 Power BLE FD",              "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               0),
    ("pv1_voltage",             "PV1 Voltage BLE FD",            "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               1),
    ("pv2_power",               "PV2 Power BLE FD",              "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               0),
    ("pv2_voltage",             "PV2 Voltage BLE FD",            "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               1),
    ("daily_solar",             "Daily Solar BLE FD",            "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:solar-power",             2),
    ("battery_voltage",         "Battery Voltage BLE FD",        "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            2),
    ("battery_temp",            "Battery Temperature BLE FD",    "°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT,    None,                            1),
    ("daily_battery_charge",    "Daily Battery Charge BLE FD",   "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:battery-plus",            2),
    ("daily_battery_discharge", "Daily Battery Discharge BLE FD","kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:battery-minus",           2),
    ("grid_frequency",          "Grid Frequency BLE FD",         "Hz",  SensorDeviceClass.FREQUENCY,   SensorStateClass.MEASUREMENT,    None,                            2),
    ("grid_total_reactive_power","Grid Total Reactive Power BLE FD","VAR", None,                       SensorStateClass.MEASUREMENT,    None,                            1),
    ("daily_grid_import",       "Daily Grid Import BLE FD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-import", 2),
    ("daily_grid_export",       "Daily Grid Export BLE FD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-export", 2),
    ("daily_load_energy",       "Daily Load Energy BLE FD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt",     2),
    ("inverter_temp",           "Inverter Temperature BLE FD",   "°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT,    None,                            1),
    ("dc_temperature",          "DC Temperature BLE FD",         "°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT,    None,                            1),
    ("total_dc_input_power",    "Total DC Input Power BLE FD",   "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power",               0),

    # 🔵 TIER B: SLOW DATA (BLE SD)
    ("total_solar",             "Total Solar BLE SD",            "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("total_grid_import",       "Total Grid Import BLE SD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-import", 2),
    ("total_grid_export",       "Total Grid Export BLE SD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-export", 2),
    ("total_battery_charge",    "Total Battery Charge BLE SD",   "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("total_battery_discharge", "Total Battery Discharge BLE SD","kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, None,                          2),
    ("total_consumption",       "Total Consumption BLE SD",      "kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt",     2),
    ("max_solar_power",         "Max Solar Power BLE SD",        "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power",               0),
    ("fault_message",           "Inverter Fault Message BLE SD", None,  None,                          None,                            "mdi:message-alert-outline",     None),
    ("alarm_message",           "Inverter Warning Message BLE SD",None, None,                          None,                            "mdi:alert-outline",             None),
    ("rated_power",             "Rated Inverter Power BLE SD",   "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power-variant",       0),
    ("battery_rated_capacity",  "Battery Rated Capacity BLE SD", "Ah",  None,                          None,                            "mdi:battery",                   1),
]

ENUM_SENSORS: list[tuple] = [
    ("battery_status", "Battery Status BLE LD",
     ["Charging", "Discharging", "Stand-by"],
     "mdi:battery-heart-variant"),
    ("grid_status",    "Grid Status BLE LD",
     ["BUY", "SELL", "IDLE"],
     "mdi:transmission-tower"),
    ("inverter_status","Inverter Status BLE LD",
     ["Standby", "Self-Checking", "Normal", "Alarm", "Fault"],
     "mdi:information-outline"),
    ("io_mode",        "IO Mode BLE FD",
     ["Disabled", "SmartLoad", "Microinverter", "Generator"],
     "mdi:swap-horizontal"),
    ("energy_priority", "Energy Priority BLE FD",
     ["Battery First", "Load First"],
     "mdi:battery-sync"),
    ("work_mode",      "Work Mode BLE FD",
     ["Sell to Grid", "Zero Export to Load", "Zero Export to CT"],
     "mdi:transmission-tower-export"),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [DeyeSensor(coordinator, entry, *row) for row in SENSORS]
    entities += [DeyeEnumSensor(coordinator, entry, *row) for row in ENUM_SENSORS]
    entities.append(DeyeInverterClockSensor(coordinator, entry))
    entities.append(DeyeClockDriftSensor(coordinator, entry))
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

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        icon: str | None,
        precision: int,
    ) -> None:
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._key = key
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon is not None:
            self._attr_icon = icon
        if precision is not None:
            self._attr_suggested_display_precision = precision
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self._key)


class DeyeEnumSensor(DeyeSensor):
    _attr_state_class = None

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str, options: list[str], icon: str | None):
        super().__init__(coordinator, entry, key, name, None, SensorDeviceClass.ENUM, None, icon, 0)
        self._attr_options = options


class DeyeInverterClockSensor(DeyeSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "inverter_clock", "Inverter Clock BLE FD", None,
            SensorDeviceClass.TIMESTAMP, None, "mdi:clock-outline", 0,
        )

    @property
    def native_value(self):
        val = (self.coordinator.data or {}).get("inverter_clock")
        if val is None:
            return None
        return dt_util.as_local(val.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE))


class DeyeClockDriftSensor(DeyeSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "inverter_clock_drift", "Inverter Clock Drift BLE FD", "s",
            SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT, "mdi:clock-alert-outline", 0,
        )


class DeyeDailyConsumptionSensor(CoordinatorEntity, SensorEntity):
    """Exposes the inverter's native hardware daily load consumption (0x0054)."""

    _attr_has_entity_name = True
    _attr_name = "Consumption Today BLE FD"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_consumption_today"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float | None:
        # Reads the native hardware register (0x0054 daily_load_energy)
        val = (self.coordinator.data or {}).get("daily_load_energy")
        if val is not None:
            return float(val)
        return (self.coordinator.data or {}).get("daily_consumption")
