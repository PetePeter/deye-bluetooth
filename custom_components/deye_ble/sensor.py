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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN, SYNC_RESULTS
from .helpers import daily_calc
from .registers import active_tou_slot

# (key, name, unit, device_class, state_class, icon, precision)
SENSORS: list[tuple] = [
    ("solar_power",             "Solar Power",            "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:solar-power",               0),
    ("house_load",              "House Load",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:home-lightning-bolt",       0),
    ("grid_power",              "Grid Power",             "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:transmission-tower",        0),
    ("battery_power",           "Battery Power",          "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:battery-charging",          0),
    ("ups_power",               "UPS Power",              "W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT,    "mdi:power-plug",                0),
    ("battery_soc",             "Battery SOC",            "%",   SensorDeviceClass.BATTERY,     SensorStateClass.MEASUREMENT,    None,                            1),
    ("battery_voltage",         "Battery Voltage",        "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    None,                            2),
    ("pv1_voltage",             "PV1 Voltage",            "V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               1),
    ("pv1_current",             "PV1 Current",            "A",   SensorDeviceClass.CURRENT,     SensorStateClass.MEASUREMENT,    "mdi:solar-panel",               1),
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


# Single-phase profile (registers.decode_single_phase). Telemetry: the shared
# rows minus the three-phase L1-L3 ones, plus PV2, CT, load, generator port,
# month/year counters, the BMS block and the inverter state/alarm/fault words.
SINGLE_PHASE_SENSORS: list[tuple] = [
    row for row in SENSORS
    if row[0] not in {
        "inverter_power_l1", "inverter_power_l2", "inverter_power_l3",
        "grid_voltage_l1", "grid_voltage_l2", "grid_voltage_l3", "max_sell_power",
    }
] + [
    ("battery_current", "Battery Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 2),
    ("pv1_power", "PV1 Power", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, "mdi:solar-panel", 0),
    ("pv2_power", "PV2 Power", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, "mdi:solar-panel", 0),
    ("pv2_voltage", "PV2 Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, "mdi:solar-panel", 1),
    ("pv2_current", "PV2 Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, "mdi:solar-panel", 1),
    ("grid_voltage", "Grid Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, None, 1),
    ("grid_current", "Grid Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 2),
    ("grid_inverter_side_power", "Grid Power (Inverter Side)", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, "mdi:transmission-tower", 0),
    ("external_ct_current", "External CT Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 2),
    ("external_ct_power", "External CT Power", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, None, 0),
    ("load_voltage", "Load Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, None, 1),
    ("load_current", "Load Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 2),
    ("load_frequency", "Load Frequency", "Hz", SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, None, 2),
    ("inverter_output_power", "Inverter Output", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, "mdi:lightning-bolt", 0),
    ("inverter_output_voltage", "Inverter Output Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, None, 1),
    ("inverter_output_current", "Inverter Output Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 2),
    ("inverter_output_frequency", "Inverter Output Frequency", "Hz", SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, None, 2),
    ("inverter_ac_temp", "Inverter AC Temperature", "°C", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, None, 1),
    ("gen_power", "Generator Power", "W", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT, "mdi:engine", 0),
    ("gen_voltage", "Generator Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, "mdi:engine", 1),
    ("gen_frequency", "Generator Frequency", "Hz", SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT, "mdi:engine", 2),
    ("daily_load", "Consumption Today", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt", 2),
    ("daily_battery_charge", "Battery Charge Today", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, None, 2),
    ("daily_battery_discharge", "Battery Discharge Today", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, None, 2),
    ("daily_gen", "Generator Today", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:engine", 2),
    ("daily_inverter_energy", "Inverter Energy Today", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, None, 2),
    ("month_solar", "Solar This Month", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:solar-power", 1),
    ("month_load", "Consumption This Month", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt", 1),
    ("month_grid_import", "Grid Import This Month", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:transmission-tower-import", 1),
    ("year_solar", "Solar This Year", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:solar-power", 1),
    ("year_load", "Consumption This Year", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt", 1),
    ("total_consumption", "Consumption Total", "kWh", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:home-lightning-bolt", 2),
    ("bms_soc", "BMS SOC", "%", SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT, None, 0),
    ("bms_voltage", "BMS Voltage", "V", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT, None, 2),
    ("bms_current", "BMS Current", "A", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT, None, 0),
    ("bms_temp", "BMS Temperature", "°C", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT, None, 1),
    ("device_state", "Inverter State", None, None, None, "mdi:state-machine", None),
    ("alarm", "Inverter Alarm", None, None, None, "mdi:alert-outline", None),
    ("fault", "Inverter Fault", None, None, None, "mdi:alert-octagon-outline", None),
]

# Single-phase settings, read on the slow cycle and shown as diagnostics: the
# profile is monitoring-only, so these are values to look at, not controls.
SINGLE_PHASE_SETTINGS: list[tuple] = [
    ("inverter_serial", "Inverter Serial", None, None, None, "mdi:identifier", None),
    ("firmware_control", "Control Board Firmware", None, None, None, "mdi:chip", None),
    ("firmware_comms", "Comms Board Firmware", None, None, None, "mdi:chip", None),
    ("protocol_version", "Modbus Protocol", None, None, None, None, None),
    ("rated_power", "Rated Power", "W", SensorDeviceClass.POWER, None, None, 0),
    ("mppt_count", "MPPT Count", None, None, None, "mdi:solar-panel", None),
    ("battery_mode", "Battery Mode", None, None, None, "mdi:battery-sync", None),
    ("bms_protocol", "BMS Protocol", None, None, None, "mdi:battery-sync", None),
    ("battery_capacity", "Battery Capacity", "Ah", None, None, "mdi:battery", 0),
    ("battery_absorption_voltage", "Absorption Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("battery_float_voltage", "Float Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("battery_empty_voltage", "Empty Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("max_charge_current", "Max Charge Current", "A", SensorDeviceClass.CURRENT, None, None, 0),
    ("max_discharge_current", "Max Discharge Current", "A", SensorDeviceClass.CURRENT, None, None, 0),
    ("batt_shutdown_soc", "Shutdown SOC", "%", None, None, "mdi:battery-off", 0),
    ("batt_restart_soc", "Restart SOC", "%", None, None, "mdi:battery-sync", 0),
    ("batt_low_soc", "Low SOC", "%", None, None, "mdi:battery-alert", 0),
    ("batt_shutdown_voltage", "Shutdown Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("batt_restart_voltage", "Restart Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("batt_low_voltage", "Low Voltage", "V", SensorDeviceClass.VOLTAGE, None, None, 2),
    ("grid_charge_start_soc", "Grid Charge Start SOC", "%", None, None, "mdi:transmission-tower-import", 0),
    ("grid_charge_current", "Grid Charge Current", "A", SensorDeviceClass.CURRENT, None, "mdi:transmission-tower-import", 0),
    ("gen_charge_start_soc", "Generator Charge Start SOC", "%", None, None, "mdi:engine", 0),
    ("gen_charge_current", "Generator Charge Current", "A", SensorDeviceClass.CURRENT, None, "mdi:engine", 0),
    ("energy_pattern", "Energy Pattern", None, None, None, "mdi:cog", None),
    ("work_mode", "Work Mode", None, None, None, "mdi:cog", None),
    ("io_mode", "GEN Port Mode", None, None, None, "mdi:engine", None),
    ("max_sell_power", "Max Sell Power", "W", SensorDeviceClass.POWER, None, "mdi:transmission-tower-export", 0),
    ("zero_export_power", "Zero Export Power", "W", SensorDeviceClass.POWER, None, None, 0),
    ("grid_reconnect_time", "Grid Reconnect Time", "s", SensorDeviceClass.DURATION, None, None, 0),
    ("grid_voltage_high_limit", "Grid Voltage High Limit", "V", SensorDeviceClass.VOLTAGE, None, None, 1),
    ("grid_voltage_low_limit", "Grid Voltage Low Limit", "V", SensorDeviceClass.VOLTAGE, None, None, 1),
    ("grid_frequency_high_limit", "Grid Frequency High Limit", "Hz", SensorDeviceClass.FREQUENCY, None, None, 2),
    ("grid_frequency_low_limit", "Grid Frequency Low Limit", "Hz", SensorDeviceClass.FREQUENCY, None, None, 2),
    ("gen_peak_power", "Generator Peak Shaving Power", "W", SensorDeviceClass.POWER, None, None, 0),
    ("grid_peak_power", "Grid Peak Shaving Power", "W", SensorDeviceClass.POWER, None, None, 0),
    ("alarm_code", "Alarm Code", None, None, None, None, None),
    ("fault_code", "Fault Code", None, None, None, None, None),
    ("bms_alarm_code", "BMS Alarm Code", None, None, None, None, None),
    ("bms_fault_code", "BMS Fault Code", None, None, None, None, None),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.read_only:
        entities = [DeyeSensor(coordinator, entry, *row) for row in SINGLE_PHASE_SENSORS]
        entities += [DeyeSettingSensor(coordinator, entry, *row) for row in SINGLE_PHASE_SETTINGS]
        entities.append(DeyeTouScheduleSensor(coordinator, entry))
        entities.append(DeyeInverterClockSensor(coordinator, entry))
        entities.append(DeyeClockDriftSensor(coordinator, entry))
        async_add_entities(entities)
        return
    entities = [DeyeSensor(coordinator, entry, *row) for row in SENSORS]
    entities.append(DeyeDailyConsumptionSensor(coordinator, entry))
    entities.append(DeyeInverterClockSensor(coordinator, entry))
    entities.append(DeyeClockSyncResultSensor(coordinator, entry))
    entities.append(DeyeClockDriftSensor(coordinator, entry))
    entities.append(DeyeClockTimeOfDayDriftSensor(coordinator, entry))
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


class DeyeSettingSensor(DeyeSensor):
    """A single-phase inverter setting, read-only (see SINGLE_PHASE_SETTINGS)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC


class DeyeTouScheduleSensor(DeyeSensor):
    """Time-of-use program: state is the slot active now, slots as attributes.

    The inverter compares slot times against its own RTC, so "now" is the
    inverter clock when it has been read, HA's local time otherwise.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "tou_schedule", "Time of Use", None,
            None, None, "mdi:calendar-clock", None,
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

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "inverter_clock", "Inverter Clock", None,
            SensorDeviceClass.TIMESTAMP, None, "mdi:clock-outline", None,
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
        self._attr_device_info = _device_info(sn)

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
            coordinator, entry, "inverter_clock_drift", "Inverter Clock Drift", "s",
            SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT,
            "mdi:clock-alert-outline", 0,
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
            "Inverter Clock Drift (Time of Day)", "s",
            SensorDeviceClass.DURATION, SensorStateClass.MEASUREMENT,
            "mdi:clock-fast", 0,
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
