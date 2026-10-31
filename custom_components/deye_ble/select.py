"""Select entities — connection mode and the enum settings (work mode etc.)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_MODE,
    CONF_LOGGER_SN,
    CONNECTION_MODE_LABELS,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
)
from .entity import OptionEntity, device_info
from .registers import SP_ENERGY_PATTERNS, SP_IO_MODES, WORK_MODE_LABELS

# (data key, name, icon, value -> label). Created only where the profile maps
# the key to a register (registers.PROFILES[...].controls).
ENUM_SELECTS: list[tuple[str, str, str, dict[int, str]]] = [
    ("work_mode", "Work Mode", "mdi:solar-power-variant", WORK_MODE_LABELS),
    ("energy_pattern", "Energy Pattern", "mdi:cog", SP_ENERGY_PATTERNS),
    ("io_mode", "GEN Port Mode", "mdi:engine", SP_IO_MODES),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]
    controls = coordinator.register_profile.controls
    entities: list[SelectEntity] = [DeyeConnectionModeSelect(coordinator, entry)]
    entities += [
        DeyeEnumSelect(coordinator, sn, *row) for row in ENUM_SELECTS if row[0] in controls
    ]
    async_add_entities(entities)


class DeyeConnectionModeSelect(OptionEntity, SelectEntity):
    _option = CONF_CONNECTION_MODE
    _default = DEFAULT_CONNECTION_MODE
    _attr_name = "Connection Mode"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_options = list(CONNECTION_MODE_LABELS.values())

    @property
    def current_option(self) -> str | None:
        return CONNECTION_MODE_LABELS.get(self.option_value)

    async def async_select_option(self, option: str) -> None:
        mode = next(m for m, label in CONNECTION_MODE_LABELS.items() if label == option)
        await self.async_set_option(mode)


class DeyeEnumSelect(CoordinatorEntity, SelectEntity):
    """An enum holding register, written through the read-only lock."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, sn: str, key: str, name: str, icon: str,
                 labels: dict[int, str]):
        super().__init__(coordinator)
        self._key = key
        self._ids = {label: value for value, label in labels.items()}
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = list(labels.values())
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_device_info = device_info(sn)

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get(self._key)

    async def async_select_option(self, option: str) -> None:
        reg = self.coordinator.register_profile.controls[self._key]
        await self.coordinator.async_write(reg, self._ids[option])
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), self._key: option},
        )
        self.coordinator.mark_config_dirty()
