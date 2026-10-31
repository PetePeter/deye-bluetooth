"""Number entities for Deye BLE integration with multi-tier sliders and holding register controls."""
from __future__ import annotations

import logging

from homeassistant.const import EntityCategory
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DEVICE_NAME, DOMAIN
from .registers import (
    REG_BATT_LOW_SOC,
    REG_BATT_RESTART_SOC,
    REG_BATT_SHUTDOWN_SOC,
    REG_GEN_PEAK_SHAVING_POWER,
    REG_GRID_CHARGE_CURRENT,
    REG_GRID_PEAK_SHAVING_POWER,
    REG_MAX_CHARGE_CURRENT,
    REG_MAX_DISCHARGE_CURRENT,
    REG_MAX_SELL_POWER,
    REG_SMARTLOAD_MIN_SOLAR_POWER,
    REG_SMARTLOAD_OFF_SOC,
    REG_SMARTLOAD_ON_SOC,
    REG_ZERO_EXPORT_POWER,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]

    entities: list[NumberEntity] = [
        # Polling tier sliders
        DeyeLiveInterval(coordinator, entry, sn),
        DeyeFastInterval(coordinator, entry, sn),
        DeyeSlowInterval(coordinator, entry, sn),
        DeyeCloudSyncInterval(coordinator, entry, sn),
        DeyeCloudWindowDuration(coordinator, entry, sn),

        # Inverter holding register controls
        DeyeMaxSellPower(coordinator, entry, sn),
        DeyeZeroExportPower(coordinator, entry, sn),
        DeyeMaxChargeCurrent(coordinator, entry, sn),
        DeyeMaxDischargeCurrent(coordinator, entry, sn),
        DeyeGridChargeCurrent(coordinator, entry, sn),
        DeyeBattShutdownSoc(coordinator, entry, sn),
        DeyeBattRestartSoc(coordinator, entry, sn),
        DeyeBattLowSoc(coordinator, entry, sn),
        DeyeSmartloadOffSoc(coordinator, entry, sn),
        DeyeSmartloadOnSoc(coordinator, entry, sn),
        DeyeSmartloadMinSolarPower(coordinator, entry, sn),
        DeyeGridPeakShavingPower(coordinator, entry, sn),
        DeyeGenPeakShavingPower(coordinator, entry, sn),
    ]
    async_add_entities(entities)


def _device_info(sn: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, sn)},
        name=DEVICE_NAME,
        manufacturer="Deye",
        model=sn,
    )


class _SingleRegisterNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _register: int
    _data_key: str

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_{self._data_key}"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float | None:
        val = (self.coordinator.data or {}).get(self._data_key)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_val = int(value)
        # Note: If Safety Lock switch is OFF, async_write raises HomeAssistantError
        await self.coordinator.async_write(self._register, int_val)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), self._data_key: int_val},
        )
        self.coordinator.mark_config_dirty()


# --- HOLDING REGISTER CONTROLS ---

class DeyeMaxSellPower(_SingleRegisterNumber):
    _register = REG_MAX_SELL_POWER
    _data_key = "max_sell_power"
    _attr_name = "Max Sell Power"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 8000
    _attr_native_step = 10
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeZeroExportPower(_SingleRegisterNumber):
    _register = REG_ZERO_EXPORT_POWER
    _data_key = "zero_export_power"
    _attr_name = "Zero Export Power"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 500
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeMaxChargeCurrent(_SingleRegisterNumber):
    _register = REG_MAX_CHARGE_CURRENT
    _data_key = "max_charge_current"
    _attr_name = "Max Charge Current"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 185
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


class DeyeMaxDischargeCurrent(_SingleRegisterNumber):
    _register = REG_MAX_DISCHARGE_CURRENT
    _data_key = "max_discharge_current"
    _attr_name = "Max Discharge Current"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 185
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


class DeyeGridChargeCurrent(_SingleRegisterNumber):
    _register = REG_GRID_CHARGE_CURRENT
    _data_key = "grid_charge_current"
    _attr_name = "Grid Charge Current Limit"
    _attr_icon = "mdi:battery-charging-wireless"
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 0
    _attr_native_max_value = 185
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.CURRENT


class DeyeBattShutdownSoc(_SingleRegisterNumber):
    _register = REG_BATT_SHUTDOWN_SOC
    _data_key = "batt_shutdown_soc"
    _attr_name = "Battery Shutdown SOC"
    _attr_icon = "mdi:battery-off"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeBattRestartSoc(_SingleRegisterNumber):
    _register = REG_BATT_RESTART_SOC
    _data_key = "batt_restart_soc"
    _attr_name = "Battery Restart SOC"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeBattLowSoc(_SingleRegisterNumber):
    _register = REG_BATT_LOW_SOC
    _data_key = "batt_low_soc"
    _attr_name = "Battery Low SOC"
    _attr_icon = "mdi:battery-alert"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeSmartloadOffSoc(_SingleRegisterNumber):
    _register = REG_SMARTLOAD_OFF_SOC
    _data_key = "smartload_off_soc"
    _attr_name = "SmartLoad OFF SOC"
    _attr_icon = "mdi:lightning-bolt-circle"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeSmartloadOnSoc(_SingleRegisterNumber):
    _register = REG_SMARTLOAD_ON_SOC
    _data_key = "smartload_on_soc"
    _attr_name = "SmartLoad ON SOC"
    _attr_icon = "mdi:lightning-bolt-circle"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DeyeSmartloadMinSolarPower(_SingleRegisterNumber):
    _register = REG_SMARTLOAD_MIN_SOLAR_POWER
    _data_key = "smartload_min_solar_power"
    _attr_name = "SmartLoad Min Solar Power"
    _attr_icon = "mdi:solar-power"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 8000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeGridPeakShavingPower(_SingleRegisterNumber):
    _register = REG_GRID_PEAK_SHAVING_POWER
    _data_key = "grid_peak_shaving_power"
    _attr_name = "Grid Peak Shaving Power Limit"
    _attr_icon = "mdi:transmission-tower"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 16000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


class DeyeGenPeakShavingPower(_SingleRegisterNumber):
    _register = REG_GEN_PEAK_SHAVING_POWER
    _data_key = "gen_peak_shaving_power"
    _attr_name = "Gen Peak Shaving Power Limit"
    _attr_icon = "mdi:engine"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 0
    _attr_native_max_value = 16000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_device_class = NumberDeviceClass.POWER


# --- POLLING INTERVAL SLIDERS ---

class DeyeLiveInterval(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "BLE: Live Polling Interval (LD)"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:speedometer"
    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_live_polling_interval"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.scan_interval_seconds)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_scan_interval(int(value))
        self.async_write_ha_state()


class DeyeFastInterval(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "BLE: Fast Polling Interval (FD)"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 300
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_fast_polling_interval"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.fast_interval_seconds)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_fast_interval(int(value))
        self.async_write_ha_state()


class DeyeSlowInterval(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "BLE: Slow Polling Interval (SD)"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-sand"
    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_slow_polling_interval"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float:
        return round(self.coordinator.slow_interval_seconds / 60.0, 1)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_slow_interval(int(value * 60))
        self.async_write_ha_state()


class DeyeCloudSyncInterval(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "BLE: Cloud Sync Frequency"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cloud-upload"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_cloud_sync_frequency"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cloud_sync_interval / 60.0, 1)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_cloud_sync_interval(int(value * 60))
        self.async_write_ha_state()


class DeyeCloudWindowDuration(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "BLE: Cloud Window Duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry: ConfigEntry, sn: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{sn}_cloud_window_duration"
        self._attr_device_info = _device_info(sn)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.cloud_window_duration)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.set_cloud_window_duration(int(value))
        self.async_write_ha_state()
