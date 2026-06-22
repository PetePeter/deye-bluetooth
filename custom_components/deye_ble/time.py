"""Time entities — charge window start/end."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import REG_TOU_SLOT2_START, REG_TOU_SLOT3_START, encode_hhmm


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        DeyeChargeWindowTime(coordinator, entry, "start"),
        DeyeChargeWindowTime(coordinator, entry, "end"),
    ])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


_SLOT_REGISTER = {
    "start": REG_TOU_SLOT2_START,   # 0x0095
    "end": REG_TOU_SLOT3_START,     # 0x0096
}


class DeyeChargeWindowTime(CoordinatorEntity, TimeEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, which: str):
        super().__init__(coordinator)
        self._which = which
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_name = f"Grid Charge {'Start' if which == 'start' else 'End'}"
        self._attr_unique_id = f"{sn}_charge_{which}"
        self._attr_icon = (
            "mdi:battery-charging" if which == "start"
            else "mdi:battery-charging-outline"
        )
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> dt_time | None:
        key = f"charge_{self._which}"
        val = (self.coordinator.data or {}).get(key)
        if not val:
            return None
        try:
            h, m = int(val[:2]), int(val[3:])
            return dt_time(h, m)
        except (ValueError, IndexError):
            return None

    async def async_set_value(self, value: dt_time) -> None:
        time_str = f"{value.hour:02d}:{value.minute:02d}"
        reg_val = encode_hhmm(time_str)
        await self.coordinator.async_write(_SLOT_REGISTER[self._which], reg_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}),
             f"charge_{self._which}": time_str},
        )
        self.coordinator.mark_config_dirty()
