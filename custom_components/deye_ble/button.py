"""Button entities — manual inverter clock sync."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.read_only:
        return  # single-phase map is monitoring-only: no control entities
    async_add_entities([DeyeSyncClockButton(coordinator, entry)])


class DeyeSyncClockButton(CoordinatorEntity, ButtonEntity):
    """Set the inverter's RTC to Home Assistant's local time, right now.

    Unconditional on purpose. The daily automation passes a min_drift threshold
    because at ~3 s/day the RTC rarely needs writing and every write is a chance
    to corrupt it. A person pressing this has already decided the clock needs
    setting, and a press that answers "close enough" would be answering a
    question nobody asked.

    Failures propagate: async_sync_clock raises when it cannot verify the
    read-back or cannot re-enable Time Sync, and that reaches the UI as a failed
    action. Swallowing it would leave a wrong clock looking like a good press —
    the exact silent-failure shape this feature exists to remove.
    """

    _attr_has_entity_name = True
    _attr_name = "Sync Clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-edit-outline"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_sync_clock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sn)},
            name=DEVICE_NAME,
            manufacturer="Deye",
            model=sn,
        )

    async def async_press(self) -> None:
        await self.coordinator.async_sync_clock_and_refresh()
