"""Number entities — write-through controls for inverter settings."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import (
    DISCHARGE_SOC_REGS,
    REG_BATT_LOW_SOC,
    REG_BATT_RESTART_SOC,
    REG_BATT_SHUTDOWN_SOC,
    REG_CHARGE_SOC,
    REG_LITHIUM_MODE,
    REG_MAX_CHARGE_CURRENT,
    REG_MAX_DISCHARGE_CURRENT,
    REG_MAX_SELL_POWER,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]
    async_add_entities([
        DeyeMaxSellPower(coordinator, entry, sn),
        DeyeChargeSoc(coordinator, entry, sn),
        DeyeDischargeSoc(coordinator, entry, sn),
        DeyeMaxChargeCurrent(coordinator, entry, sn),
        DeyeMaxDischargeCurrent(coordinator, entry, sn),
        DeyeBattShutdownSoc(coordinator, entry, sn),
        DeyeBattLowSoc(coordinator, entry, sn),
        DeyeBattRestartSoc(coordinator, entry, sn),
        DeyeLithiumMode(coordinator, entry, sn),
    ])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class _SingleRegisterNumber(CoordinatorEntity, NumberEntity):
    """Write-through number backed by a single holding register.

    Subclasses declare ``_register`` (address), ``_data_key`` (coordinator-data
    key + unique-id suffix) and the usual NumberEntity presentation attrs.
    Setting the value writes the register, optimistically mirrors it into
    coordinator data, and marks config dirty for reassert/drift handling.
    """

    _attr_has_entity_name = True
    _register: int
    _data_key: str

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_{self._data_key}"
        self._attr_device_info = _device_info(sn)

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
    _register = REG_MAX_SELL_POWER
    _data_key = "max_sell_power"
    _attr_name = "Max Sell Power"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 10000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeChargeSoc(_SingleRegisterNumber):
    _register = REG_CHARGE_SOC
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
        self._attr_device_info = _device_info(sn)

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

    _register = REG_MAX_CHARGE_CURRENT
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

    _register = REG_MAX_DISCHARGE_CURRENT
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

    _register = REG_BATT_SHUTDOWN_SOC
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

    _register = REG_BATT_LOW_SOC
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

    _register = REG_BATT_RESTART_SOC
    _data_key = "batt_restart_soc"
    _attr_name = "Battery Restart SOC"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeLithiumMode(_SingleRegisterNumber):
    """Battery (BMS) protocol / lithium-mode index. Reg 0x0071.

    A raw index (matches the Deye app's "Lithium Mode" field). Kept as a plain
    numeric box because the protocol-number→BMS-vendor mapping is model-specific.
    """

    _register = REG_LITHIUM_MODE
    _data_key = "lithium_mode"
    _attr_name = "Lithium Mode"
    _attr_icon = "mdi:battery-sync"
    _attr_native_min_value = 0
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
