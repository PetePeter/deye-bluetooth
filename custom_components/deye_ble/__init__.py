"""Deye Bluetooth (Local) integration.

HA imports are deferred to function bodies so that the package can be imported
without homeassistant installed (unit tests for protocol/registers/coordinator).
"""
from __future__ import annotations

import logging

from .const import CONF_ADDRESS, CONF_LOGGER_SN, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Platforms are added in P4 (sensor, select, number, etc.)
PLATFORMS: list[str] = []


async def async_setup_entry(hass, entry) -> bool:
    """Set up Deye BLE from a config entry."""
    from homeassistant.config_entries import ConfigEntry  # noqa: F811

    address = entry.data[CONF_ADDRESS]
    logger_sn = entry.data[CONF_LOGGER_SN]

    _LOGGER.info("Setting up Deye BLE for %s (%s)", logger_sn, address)

    def _make_transport(ble_device):
        from .transport import DeyeBleTransport
        return DeyeBleTransport(ble_device)

    from .coordinator import DeyeBleCoordinator
    coordinator = DeyeBleCoordinator(
        hass,
        address=address,
        transport_factory=_make_transport,
    )

    await coordinator.coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload Deye BLE config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
