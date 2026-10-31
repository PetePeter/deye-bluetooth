"""Switch entities for Deye BLE integration."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import (
    GEN_PEAK_SHAVE_MASK,
    GRID_PEAK_SHAVE_MASK,
    REG_GRID_CHARGE_ENABLE,
    REG_PEAK_SHAVING_FLAGS,
    REG_SOLAR_SELL_ENABLE,
    REG_TOU_ENABLE,
    set_flag,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]

    entities: list[SwitchEntity] = [
        # Master Safety Write Lock Switch (Default OFF = dry-run / locked)
        DeyeInverterWriteLockSwitch(coordinator, entry, sn),

        # Inverter configuration holding switches
        DeyeSolarSellSwitch(coordinator, entry, sn),
        DeyeGridChargeSwitch(coordinator, entry, sn),
        DeyeTimeOfUseSwitch(coordinator, entry, sn),
        DeyeGridPeakShaving(coordinator, entry, sn),
        DeyeGenPeakShaving(coordinator, entry, sn),
    ]
    async_add_entities(entities)


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeInverterWriteLockSwitch(CoordinatorEntity, SwitchEntity):
    """Master Safety Write Lock Toggle Switch.

    When OFF (Default): All inverter holding register writes are strictly blocked
    (dry_run = True) to prevent accidental UI touches and unintended automations.
    When ON: Inverter holding register writes are permitted (dry_run = False).
    """

    _attr_has_entity_name = True
    _attr_name = "Allow Inverter Writes"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._sn = sn
        self._attr_unique_id = f"{sn}_allow_inverter_writes"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool:
        return not self.coordinator.dry_run

    @property
    def icon(self) -> str:
        return "mdi:lock-open-variant" if self.is_on else "mdi:lock"

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.dry_run = False
        _LOGGER.warning("Inverter Write Safety Switch turned ON: Holding register writes are now ENABLED")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.dry_run = True
        _LOGGER.info("Inverter Write Safety Switch turned OFF: Holding register writes are now LOCKED")
        self.async_write_ha_state()


class DeyeSolarSellSwitch(CoordinatorEntity, SwitchEntity):
    """Solar Sell / Export Surplus holding register (0x00F7)."""

    _attr_has_entity_name = True
    _attr_name = "Solar Sell"
    _attr_icon = "mdi:solar-power-variant"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_solar_sell"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("solar_sell")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_write(REG_SOLAR_SELL_ENABLE, 1)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "solar_sell": True,
        })
        self.coordinator.mark_config_dirty()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write(REG_SOLAR_SELL_ENABLE, 0)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "solar_sell": False,
        })
        self.coordinator.mark_config_dirty()


class DeyeGridChargeSwitch(CoordinatorEntity, SwitchEntity):
    """Grid Charge Master holding register (0x00E8)."""

    _attr_has_entity_name = True
    _attr_name = "Grid Charge Master"
    _attr_icon = "mdi:battery-charging-wireless"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_grid_charge_enabled"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("grid_charge_enabled")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_write(REG_GRID_CHARGE_ENABLE, 1)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "grid_charge_enabled": True,
        })
        self.coordinator.mark_config_dirty()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write(REG_GRID_CHARGE_ENABLE, 0)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "grid_charge_enabled": False,
        })
        self.coordinator.mark_config_dirty()


class DeyeTimeOfUseSwitch(CoordinatorEntity, SwitchEntity):
    """Time of Use (ToU) Master holding register (0x00F8)."""

    _attr_has_entity_name = True
    _attr_name = "Time of Use"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_tou_enabled"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("tou_enabled")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        # 0x00FF enables ToU for all days of the week
        await self.coordinator.async_write(REG_TOU_ENABLE, 0x00FF)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "tou_enabled": True,
        })
        self.coordinator.mark_config_dirty()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write(REG_TOU_ENABLE, 0x0000)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "tou_enabled": False,
        })
        self.coordinator.mark_config_dirty()


class _PeakShaveSwitch(CoordinatorEntity, SwitchEntity):
    """One enable bit of the packed 0x0118 peak-shaving flag register."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _mask: int
    _data_key: str

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_{self._data_key}"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get(self._data_key)
        return bool(val) if val is not None else None

    async def _async_set(self, on: bool) -> None:
        raw = (self.coordinator.data or {}).get("peak_shaving_flags_raw")
        if raw is None:
            raise HomeAssistantError(
                "Peak-shaving flag register (0x0118) not read yet; cannot toggle safely"
            )
        new_raw = set_flag(int(raw), self._mask, on)
        await self.coordinator.async_write(REG_PEAK_SHAVING_FLAGS, new_raw)
        self.coordinator.async_set_updated_data({
            **(self.coordinator.data or {}),
            "peak_shaving_flags_raw": new_raw,
            "grid_peak_shaving": bool(new_raw & GRID_PEAK_SHAVE_MASK),
            "gen_peak_shaving": bool(new_raw & GEN_PEAK_SHAVE_MASK),
        })
        self.coordinator.mark_config_dirty()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class DeyeGridPeakShaving(_PeakShaveSwitch):
    _mask = GRID_PEAK_SHAVE_MASK
    _data_key = "grid_peak_shaving"
    _attr_name = "Grid Peak Shaving"
    _attr_icon = "mdi:transmission-tower"


class DeyeGenPeakShaving(_PeakShaveSwitch):
    _mask = GEN_PEAK_SHAVE_MASK
    _data_key = "gen_peak_shaving"
    _attr_name = "Gen Peak Shaving"
    _attr_icon = "mdi:engine"
