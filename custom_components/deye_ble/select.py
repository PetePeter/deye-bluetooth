"""Select entities for Deye BLE integration."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import (
    ENERGY_PRIORITY_LABELS,
    IO_MODE_LABELS,
    REG_ENERGY_PRIORITY,
    REG_IO_MODE,
    REG_WORK_MODE,
    WORK_MODE_LABELS,
    encode_energy_priority,
    encode_io_mode,
    encode_work_mode,
)

MODE_BLE_CLOUD = "BLE with Deye Cloud"
MODE_BLE_LOCAL = "BLE no Deye Cloud"
MODE_NO_BLE = "No BLE (Only Deye Cloud)"

CONNECTION_MODES = [
    MODE_BLE_CLOUD,
    MODE_BLE_LOCAL,
    MODE_NO_BLE,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        DeyeConnectionModeSelect(coordinator, entry),
        DeyeWorkModeSelect(coordinator, entry),
        DeyeEnergyPrioritySelect(coordinator, entry),
        DeyeIoModeSelect(coordinator, entry),
    ]
    async_add_entities(entities)


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class DeyeConnectionModeSelect(CoordinatorEntity, SelectEntity):
    """Dropdown entity to control BLE polling mode and cloud coexistence."""

    _attr_has_entity_name = True
    _attr_name = "BLE: Connection Mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:transit-connection-variant"
    _attr_options = CONNECTION_MODES

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_connection_mode"
        self._attr_device_info = _device_info(sn)

    @property
    def current_option(self) -> str:
        if not self.coordinator.ble_enabled:
            return MODE_NO_BLE
        if self.coordinator.fully_local:
            return MODE_BLE_LOCAL
        return MODE_BLE_CLOUD

    async def async_select_option(self, option: str) -> None:
        if option == MODE_NO_BLE:
            await self.coordinator.async_set_ble_enabled(False)
        elif option == MODE_BLE_LOCAL:
            await self.coordinator.async_set_ble_enabled(True)
            self.coordinator.set_fully_local(True)
        elif option == MODE_BLE_CLOUD:
            await self.coordinator.async_set_ble_enabled(True)
            self.coordinator.set_fully_local(False)

        self.async_write_ha_state()


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


class DeyeEnergyPrioritySelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Energy Priority Pattern"
    _attr_icon = "mdi:battery-sync"
    _attr_options = list(ENERGY_PRIORITY_LABELS.values())

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_energy_priority"
        self._attr_device_info = _device_info(sn)

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("energy_priority")

    async def async_select_option(self, option: str) -> None:
        reg_val = encode_energy_priority(option)
        await self.coordinator.async_write(REG_ENERGY_PRIORITY, reg_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "energy_priority": option},
        )
        self.coordinator.mark_config_dirty()


class DeyeIoModeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "IO Port Mode"
    _attr_icon = "mdi:swap-horizontal"
    _attr_options = list(IO_MODE_LABELS.values())

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_io_mode_select"
        self._attr_device_info = _device_info(sn)

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get("io_mode")

    async def async_select_option(self, option: str) -> None:
        reg_val = encode_io_mode(option)
        await self.coordinator.async_write(REG_IO_MODE, reg_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), "io_mode": option},
        )
        self.coordinator.mark_config_dirty()
