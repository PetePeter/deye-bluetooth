"""Switch entities — the read-only lock, peak shaving, single-phase toggles.

Grid and generator peak-shaving enables (three-phase) both live in the packed
0x00B2 Advanced-Function-1 bitfield, alongside other (unmapped) function bits.
Each toggle therefore read-modify-writes: it takes the last-polled raw word,
flips only its own bit, and writes the whole register back — so enabling grid
shaving never disturbs the generator bit (or anything else in 0x00B2).
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, CONF_READ_ONLY, DEFAULT_READ_ONLY, DOMAIN
from .entity import OptionEntity, device_info
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
    entities: list[SwitchEntity] = [DeyeReadOnlySwitch(coordinator, entry)]
    if coordinator.is_single_phase:
        controls = coordinator.register_profile.controls
        entities += [
            DeyeRegisterSwitch(coordinator, sn, key, name, icon)
            for key, name, icon in SINGLE_PHASE_SWITCHES
            if key in controls
        ]
    else:
        entities += [
            DeyeGridPeakShaving(coordinator, entry, sn),
            DeyeGenPeakShaving(coordinator, entry, sn),
        ]
    async_add_entities(entities)


# (data key, name, icon) — plain 0/1 enable registers on the single-phase map.
SINGLE_PHASE_SWITCHES: list[tuple[str, str, str]] = [
    ("grid_charge_enabled", "Grid Charging", "mdi:transmission-tower-import"),
    ("solar_sell_enabled", "Solar Sell", "mdi:transmission-tower-export"),
]


class DeyeReadOnlySwitch(OptionEntity, SwitchEntity):
    """The read-only lock. ON (the default) refuses every inverter write."""

    _option = CONF_READ_ONLY
    _default = DEFAULT_READ_ONLY
    _attr_name = "Read Only"

    @property
    def is_on(self) -> bool:
        return bool(self.option_value)

    @property
    def icon(self) -> str:
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant"

    async def async_turn_on(self, **kwargs) -> None:
        await self.async_set_option(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_set_option(False)


class DeyeRegisterSwitch(CoordinatorEntity, SwitchEntity):
    """A whole-register 0/1 enable, written through the read-only lock."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, sn: str, key: str, name: str, icon: str):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_device_info = device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get(self._key)
        return bool(val) if val is not None else None

    async def _async_set(self, on: bool) -> None:
        reg = self.coordinator.register_profile.controls[self._key]
        await self.coordinator.async_write(reg, int(on))
        self.coordinator.async_set_updated_data({**(self.coordinator.data or {}), self._key: on})
        self.coordinator.mark_config_dirty()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


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
        self._attr_device_info = device_info(sn)

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
