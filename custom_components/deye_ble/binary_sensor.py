"""Binary sensor entities — grid-connection state inferred from grid voltage."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .helpers import infer_grid_connected


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DeyeGridConnected(coordinator, entry)])


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeGridConnected(CoordinatorEntity, BinarySensorEntity):
    """On-grid state, inferred from whether any grid phase is energised.

    There is no verified grid-relay status register over BLE, so this is
    derived from the per-phase grid voltages (see helpers.infer_grid_connected).
    """

    _attr_has_entity_name = True
    _attr_name = "Grid Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_grid_connected"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        return infer_grid_connected(self.coordinator.data or {})
