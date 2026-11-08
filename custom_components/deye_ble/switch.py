"""Switch entities — the read-only lock, peak shaving, single-phase toggles.

Grid and generator peak-shaving enables (three-phase) both live in the packed
0x00B2 Advanced-Function-1 bitfield, alongside other (unmapped) function bits.
Each toggle therefore read-modify-writes: it takes the last-polled raw word,
flips only its own bit, and writes the whole register back — so enabling grid
shaving never disturbs the generator bit (or anything else in 0x00B2).
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, CONF_READ_ONLY, DEFAULT_READ_ONLY, DOMAIN
from .entity import OptionEntity, device_info
from .models.base import Switch as SwitchDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [DeyeReadOnlySwitch(coordinator, entry)]
    for point in coordinator.model.write_points:
        if any(isinstance(show, SwitchDescription) for show in point.shows):
            entities.append(DeyeModelSwitch(coordinator, entry, point))
    async_add_entities(entities)


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


class DeyeModelSwitch(CoordinatorEntity, SwitchEntity):
    """Switch entity built from a model point and its write descriptor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, point):
        super().__init__(coordinator)
        description = next(
            show for show in point.shows if isinstance(show, SwitchDescription)
        )
        if point.write is None:
            raise ValueError(f"switch point {point.key!r} has no write descriptor")
        self._key = point.key
        self._write = point.write
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_name = description.name
        self._attr_unique_id = f"{sn}_{point.key}"
        self._attr_device_class = SwitchDeviceClass(
            description.device_class or SwitchDeviceClass.SWITCH
        )
        if description.icon:
            self._attr_icon = description.icon
        if description.entity_category:
            self._attr_entity_category = EntityCategory(description.entity_category)
        self._attr_device_info = device_info(sn)

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get(self._key)
        return bool(val) if val is not None else None

    async def _async_set(self, on: bool) -> None:
        current = self.coordinator.data or {}
        try:
            writes = self._write.values(on, current)
        except (KeyError, TypeError, ValueError) as exc:
            raise HomeAssistantError(
                "switch source registers have not been read; cannot toggle safely"
            ) from exc
        if len(writes) == 1:
            register, value = next(iter(writes.items()))
            await self.coordinator.async_write(register, value)
        else:
            await self.coordinator.async_write_many(writes)
        decoded = self.coordinator.model.decode({
            register: [value] for register, value in writes.items()
        })
        self.coordinator.async_set_updated_data({**current, **decoded, self._key: on})
        self.coordinator.mark_config_dirty()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)
