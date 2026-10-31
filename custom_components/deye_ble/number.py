"""Number entities — write-through controls for inverter settings."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLOUD_SYNC_INTERVAL,
    CONF_CLOUD_WINDOW,
    CONF_LIVE_INTERVAL,
    CONF_LOGGER_SN,
    CONF_SCAN_INTERVAL,
    CONF_SLOW_INTERVAL,
    DEFAULT_CLOUD_SYNC_INTERVAL,
    DEFAULT_CLOUD_WINDOW,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    MAX_CLOUD_SYNC_INTERVAL,
    MAX_CLOUD_WINDOW,
    MAX_LIVE_INTERVAL,
    MAX_SCAN_INTERVAL,
    MAX_SLOW_INTERVAL,
    MIN_CLOUD_SYNC_INTERVAL,
    MIN_CLOUD_WINDOW,
    MIN_LIVE_INTERVAL,
    MIN_SCAN_INTERVAL,
    MIN_SLOW_INTERVAL,
)
from .entity import OptionEntity, device_info
from .registers import DISCHARGE_SOC_REGS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]
    controls = coordinator.register_profile.controls
    entities: list[NumberEntity] = [cls(coordinator, entry) for cls in OPTION_NUMBERS]
    entities += [
        cls(coordinator, entry, sn) for cls in REGISTER_NUMBERS if cls._data_key in controls
    ]
    if not coordinator.is_single_phase:
        # Three-phase TOU layout only: the floor spans five slot registers.
        entities.append(DeyeDischargeSoc(coordinator, entry, sn))
    async_add_entities(entities)


class _SingleRegisterNumber(CoordinatorEntity, NumberEntity):
    """Write-through number backed by a single holding register.

    Subclasses declare ``_data_key`` (coordinator-data key + unique-id suffix)
    and the usual NumberEntity presentation attrs; the register comes from the
    profile control table. Setting the value writes the register (refused by
    the read-only lock), mirrors it into coordinator data, and marks config
    dirty for reassert/drift handling.
    """

    _attr_has_entity_name = True
    _data_key: str

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._register = coordinator.register_profile.controls[self._data_key]
        self._attr_unique_id = f"{sn}_{self._data_key}"
        self._attr_device_info = device_info(sn)

    @property
    def native_value(self) -> float | None:
        val = (self.coordinator.data or {}).get(self._data_key)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_val = int(value)
        await self.coordinator.async_write(self._register, int_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), self._data_key: int_val},
        )
        self.coordinator.mark_config_dirty()


class DeyeMaxSellPower(_SingleRegisterNumber):
    _data_key = "max_sell_power"
    _attr_name = "Max Sell Power"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 10000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeZeroExportPower(_SingleRegisterNumber):
    """Zero-export power / grid-compensation offset (W).

    Three-phase (0x0068, signed): in Zero Export to CT this biases the CT
    target; positive holds a small grid import, a *negative* value (blocked by
    the phone app but accepted by the inverter — live-probed) forces export up
    to that magnitude. Single-phase (206) decodes unsigned, so its floor is 0.
    """

    _data_key = "zero_export_power"
    _attr_name = "Zero Export Power"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = "W"
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER

    @property
    def native_min_value(self) -> float:
        return 0 if self.coordinator.is_single_phase else -15000


class DeyeChargeSoc(_SingleRegisterNumber):
    _data_key = "charge_soc"
    _attr_name = "Charge Target SOC"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 10
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER


class DeyeDischargeSoc(CoordinatorEntity, NumberEntity):
    """Battery discharge floor — the SOC the inverter holds when not charging.

    There is no dedicated discharge-SOC register: it is the TOU per-slot target
    SOC on every non-charge slot. Setting it writes the same value to all five
    non-charge slots (slot 2 is the grid-charge slot, left to Charge Target SOC).
    """

    _attr_has_entity_name = True
    _attr_name = "Discharge SOC"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 10
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_discharge_soc"
        self._attr_device_info = device_info(sn)

    @property
    def native_value(self) -> float | None:
        val = (self.coordinator.data or {}).get("discharge_soc")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_val = int(value)
        await self.coordinator.async_write_many({reg: int_val for reg in DISCHARGE_SOC_REGS})
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "discharge_soc": int_val},
        )
        self.coordinator.mark_config_dirty()


class DeyeMaxChargeCurrent(_SingleRegisterNumber):
    """Battery max charge current (A) — inverter setpoint at register 0x006C."""

    _data_key = "max_charge_current"
    _attr_name = "Max Charge Current"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 280
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


class DeyeMaxDischargeCurrent(_SingleRegisterNumber):
    """Battery max discharge current (A) — inverter setpoint at register 0x006D."""

    _data_key = "max_discharge_current"
    _attr_name = "Max Discharge Current"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 280
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


class DeyeBattShutdownSoc(_SingleRegisterNumber):
    """Battery shutdown SOC (%) — inverter cuts the battery below this. Reg 0x0073."""

    _data_key = "batt_shutdown_soc"
    _attr_name = "Battery Shutdown SOC"
    _attr_icon = "mdi:battery-off"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeBattLowSoc(_SingleRegisterNumber):
    """Battery low-warning SOC (%). Reg 0x0075."""

    _data_key = "batt_low_soc"
    _attr_name = "Battery Low SOC"
    _attr_icon = "mdi:battery-alert"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeBattRestartSoc(_SingleRegisterNumber):
    """Battery restart SOC (%) — inverter re-enables the battery at this. Reg 0x0074."""

    _data_key = "batt_restart_soc"
    _attr_name = "Battery Restart SOC"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeGridPeakPower(_SingleRegisterNumber):
    """Grid peak-shaving power cap (W) — max grid import while grid shaving is on.
    Reg 0x00BF. The enable bit lives in 0x00B2 (see the Grid Peak Shaving switch)."""

    _data_key = "grid_peak_power"
    _attr_name = "Grid Peak Shave Power"
    _attr_icon = "mdi:transmission-tower"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 30000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeGenPeakPower(_SingleRegisterNumber):
    """Generator peak-shaving power cap (W). Reg 0x00BE. Enable bit in 0x00B2
    (see the Gen Peak Shaving switch)."""

    _data_key = "gen_peak_power"
    _attr_name = "Gen Peak Shave Power"
    _attr_icon = "mdi:engine"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 30000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeGridChargeCurrent(_SingleRegisterNumber):
    """Grid charge current limit (A)."""

    _data_key = "grid_charge_current"
    _attr_name = "Grid Charge Current"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 280
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


REGISTER_NUMBERS: list[type[_SingleRegisterNumber]] = [
    DeyeMaxSellPower, DeyeZeroExportPower, DeyeChargeSoc, DeyeMaxChargeCurrent,
    DeyeMaxDischargeCurrent, DeyeBattShutdownSoc, DeyeBattLowSoc, DeyeBattRestartSoc,
    DeyeGridPeakPower, DeyeGenPeakPower, DeyeGridChargeCurrent,
]


# --- Option-backed CONFIG numbers (polling + connection) ---------------------

class _OptionNumber(OptionEntity, NumberEntity):
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    @property
    def native_value(self) -> float:
        return float(self.option_value)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set_option(int(value))


class DeyeScanInterval(_OptionNumber):
    _option, _default = CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    _attr_name = "Standard Poll Interval"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL


class DeyeLiveInterval(_OptionNumber):
    _option, _default = CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL
    _attr_name = "Live Poll Interval"
    _attr_icon = "mdi:timer-play-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_LIVE_INTERVAL, MAX_LIVE_INTERVAL


class DeyeSlowInterval(_OptionNumber):
    _option, _default = CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL
    _attr_name = "Slow Poll Interval"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value, _attr_native_max_value = MIN_SLOW_INTERVAL, MAX_SLOW_INTERVAL
    _attr_native_step = 60


class DeyeCloudSyncInterval(_OptionNumber):
    _option, _default = CONF_CLOUD_SYNC_INTERVAL, DEFAULT_CLOUD_SYNC_INTERVAL
    _attr_name = "Cloud Quiet Window Every"
    _attr_icon = "mdi:cloud-sync-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_CLOUD_SYNC_INTERVAL, MAX_CLOUD_SYNC_INTERVAL


class DeyeCloudWindow(_OptionNumber):
    _option, _default = CONF_CLOUD_WINDOW, DEFAULT_CLOUD_WINDOW
    _attr_name = "Cloud Quiet Window Length"
    _attr_icon = "mdi:cloud-clock-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_CLOUD_WINDOW, MAX_CLOUD_WINDOW


OPTION_NUMBERS: list[type[_OptionNumber]] = [
    DeyeScanInterval, DeyeLiveInterval, DeyeSlowInterval, DeyeCloudSyncInterval, DeyeCloudWindow,
]
