"""Switch entities — peak-shaving enable toggles.

Grid and generator peak-shaving enables both live in the packed 0x00B2
Advanced-Function-1 bitfield, alongside other (unmapped) function bits. Each
toggle therefore read-modify-writes: it takes the last-polled raw word, flips
only its own bit, and writes the whole register back — so enabling grid shaving
never disturbs the generator bit (or anything else in 0x00B2).
"""
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
    REG_PEAK_SHAVING_FLAGS,
    set_flag,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]
    async_add_entities([
        DeyeGridPeakShaving(coordinator, entry, sn),
        DeyeGenPeakShaving(coordinator, entry, sn),
    ])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class _PeakShaveSwitch(CoordinatorEntity, SwitchEntity):
    """One enable bit of the packed 0x00B2 peak-shaving flag register.

    Subclasses declare ``_mask`` (the enable bit) and ``_data_key`` (the decoded
    bool key + unique-id suffix). Toggling read-modify-writes 0x00B2 so the other
    function bits are preserved.
    """

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
            # Without the current word we cannot safely RMW — refuse rather than
            # write a value that could clear the other function bits.
            raise HomeAssistantError(
                "peak-shaving flag register (0x00B2) not read yet; cannot toggle safely"
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
