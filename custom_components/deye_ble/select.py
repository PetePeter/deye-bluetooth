"""Select entities built from model points plus the connection-mode option."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
from .models.base import Select as SelectDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [DeyeConnectionModeSelect(coordinator, entry)]
    for point in coordinator.model.write_points:
        if any(isinstance(show, SelectDescription) for show in point.shows):
            entities.append(DeyeModelSelect(coordinator, entry, point))
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


class DeyeModelSelect(CoordinatorEntity, SelectEntity):
    """An enum setting described by a model point and verified write."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, point):
        super().__init__(coordinator)
        description = next(
            show for show in point.shows if isinstance(show, SelectDescription)
        )
        if point.write is None:
            raise ValueError(f"select point {point.key!r} has no write descriptor")
        self._key = point.key
        self._labels = description.options
        self._write = point.write
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_options = list(description.options.values())
        self._attr_unique_id = f"{entry.data[CONF_LOGGER_SN]}_{point.key}"
        self._attr_device_info = device_info(entry.data[CONF_LOGGER_SN])

    @property
    def current_option(self) -> str | None:
        return (self.coordinator.data or {}).get(self._key)

    async def async_select_option(self, option: str) -> None:
        if option not in self._labels.values():
            raise HomeAssistantError(f"Unsupported option for {self._key}: {option}")
        current = self.coordinator.data or {}
        writes = self._write.values(option, current)
        if len(writes) == 1:
            register, raw_value = next(iter(writes.items()))
            await self.coordinator.async_write(register, raw_value)
        else:
            await self.coordinator.async_write_many(writes)
        decoded = self.coordinator.model.decode(
            {register: [raw_value] for register, raw_value in writes.items()},
        )
        self.coordinator.async_set_updated_data(
            {**current, **decoded, self._key: option},
        )
        self.coordinator.mark_config_dirty()
