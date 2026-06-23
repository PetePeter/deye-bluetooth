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
from .registers import DISCHARGE_SOC_REGS, REG_CHARGE_SOC, REG_MAX_SELL_POWER

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
    ])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeMaxSellPower(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Max Sell Power"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 10000
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_max_sell_power"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float | None:
        val = (self.coordinator.data or {}).get("max_sell_power")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_val = int(value)
        await self.coordinator.async_write(REG_MAX_SELL_POWER, int_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "max_sell_power": int_val},
        )
        self.coordinator.mark_config_dirty()


class DeyeChargeSoc(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Charge Target SOC"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 10
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_charge_soc"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float | None:
        val = (self.coordinator.data or {}).get("charge_soc")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_val = int(value)
        await self.coordinator.async_write(REG_CHARGE_SOC, int_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "charge_soc": int_val},
        )
        self.coordinator.mark_config_dirty()


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
