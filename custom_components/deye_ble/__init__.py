"""Deye Bluetooth (Local) integration.

HA imports are deferred to function bodies so that the package can be imported
without homeassistant installed (unit tests for protocol/registers/coordinator).
"""
from __future__ import annotations

import logging

from .const import (
    CONF_ADDRESS,
    CONF_DRY_RUN,
    CONF_LOGGER_SN,
    CONF_REASSERT,
    CONF_SCAN_INTERVAL,
    DEFAULT_DRY_RUN,
    DEFAULT_REASSERT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "number", "select", "time"]


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
    options = entry.options
    coordinator = DeyeBleCoordinator(
        hass,
        address=address,
        transport_factory=_make_transport,
        scan_interval=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        dry_run=options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
        reassert=options.get(CONF_REASSERT, DEFAULT_REASSERT),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    async def _on_options_update(hass, entry):
        from datetime import timedelta
        coordinator.dry_run = entry.options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)
        coordinator.reassert = entry.options.get(CONF_REASSERT, DEFAULT_REASSERT)
        coordinator.update_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

    entry.async_on_unload(entry.add_update_listener(_on_options_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload Deye BLE config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
