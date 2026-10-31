"""Binary sensor entities — grid-connection state and clock-sync health."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    async_add_entities([
        DeyeGridConnected(coordinator, entry),
        DeyeCloudClockSyncEnabled(coordinator, entry),
        DeyeInverterFaultBinarySensor(coordinator, entry),
        DeyeInverterAlarmBinarySensor(coordinator, entry),
    ])


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


class DeyeCloudClockSyncEnabled(CoordinatorEntity, BinarySensorEntity):
    """Time Sync (0x00E4 bit 0) — whether cloud clock calibration is allowed.

    OFF means the inverter refuses the logger's cloud time calibration: its RTC
    free-runs (~3 s/day slow) until something writes it. Nothing in the Deye app
    shows this, and setting the time from the app is what silently turns it off —
    the inverter ended up a full year out that way, uncorrected for months.
    Exposed so the state is visible instead of inferred from drift after the fact.
    """

    _attr_has_entity_name = True
    _attr_name = "Cloud Clock Sync Enabled"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-clock"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_time_sync"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("time_sync")


class DeyeInverterFaultBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Hardware Fault sensor decoded from Modbus registers 103â€“106 (0x0067..0x006A)."""

    _attr_has_entity_name = True
    _attr_name = "Inverter Fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_hardware_fault"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("has_fault")
        return bool(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "fault_codes": data.get("fault_codes", []),
            "fault_message": data.get("fault_message", "OK"),
        }


class DeyeInverterAlarmBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Hardware Warning sensor decoded from Modbus registers 101â€“102 (0x0065..0x0066)."""

    _attr_has_entity_name = True
    _attr_name = "Inverter Warning"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_hardware_alarm"
        self._attr_device_info = _device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("has_alarm")
        return bool(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "alarm_message": data.get("alarm_message", "OK"),
        }