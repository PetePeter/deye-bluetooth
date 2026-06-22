"""Select entity — work mode control."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import REG_WORK_MODE, WORK_MODE_LABELS, encode_work_mode


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DeyeWorkModeSelect(coordinator, entry)])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeWorkModeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Work Mode"
    _attr_icon = "mdi:solar-power-variant"
    _attr_options = list(WORK_MODE_LABELS.values())

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_work_mode"
        self._attr_device_info = _device_info(sn)

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("work_mode")

    async def async_select_option(self, option: str) -> None:
        reg_val = encode_work_mode(option)
        await self.coordinator.async_write(REG_WORK_MODE, reg_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "work_mode": option},
        )
        self.coordinator.mark_config_dirty()
