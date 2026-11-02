"""Shared entity plumbing: device info and the option-backed CONFIG entities."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN


def device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class OptionEntity(CoordinatorEntity):
    """A CONFIG entity whose state IS one entry option.

    Writing goes through the entry options, so the value survives restarts and
    the options form, the entity and the coordinator never disagree: the entry
    update listener re-applies the options to the coordinator.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _option: str
    _default: object

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_{self._option}"
        self._attr_device_info = device_info(sn)

    @property
    def available(self) -> bool:
        return True  # a setting, not a reading: usable while the inverter is not

    @property
    def option_value(self):
        return self._entry.options.get(self._option, self._default)

    async def async_set_option(self, value) -> None:
        options = {**self._entry.options, self._option: value}
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        # Applied here too, not only by the entry update listener: that runs as
        # a separate task, and a write landing in between must already see
        # the new Read Only state.
        await self.coordinator.async_apply_options(options)
