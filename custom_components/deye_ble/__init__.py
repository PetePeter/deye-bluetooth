"""Deye Bluetooth (Local) integration.

HA imports are deferred to function bodies so that the package can be imported
without homeassistant installed (unit tests for protocol/registers/coordinator).
"""
from __future__ import annotations

import logging

from .const import CONF_ADDRESS, CONF_LOGGER_SN, CONF_PROFILE, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "binary_sensor", "button", "sensor", "number", "select", "switch", "time",
]


async def async_setup_entry(hass, entry) -> bool:
    """Set up Deye BLE from a config entry."""
    from homeassistant.config_entries import ConfigEntry  # noqa: F811

    address = entry.data[CONF_ADDRESS]
    logger_sn = entry.data[CONF_LOGGER_SN]

    _LOGGER.info("Setting up Deye BLE for %s (%s)", logger_sn, address)

    def _make_transport(ble_device):
        from .transport import DeyeBleTransport
        return DeyeBleTransport(ble_device)

    from homeassistant.exceptions import ConfigEntryNotReady

    from .coordinator import DeyeBleCoordinator
    from .helpers import migrate_options

    if (migrated := migrate_options(entry.options)) is not None:
        hass.config_entries.async_update_entry(entry, options=migrated)

    from . import registers as r

    coordinator = DeyeBleCoordinator(
        hass, address=address, transport_factory=_make_transport,
        profile=entry.data.get(CONF_PROFILE, r.PROFILE_THREE_PHASE),
    )
    await coordinator.async_apply_options(entry.options)

    # The register map is detected ONCE and cached in entry.data; re-adding the
    # device re-detects. Entries from before profiles existed detect here.
    if CONF_PROFILE not in entry.data:
        try:
            profile = await coordinator.async_detect_profile()
        except Exception as exc:  # noqa: BLE001 — HA retries setup later
            raise ConfigEntryNotReady(f"could not read the inverter type: {exc}") from exc
        _LOGGER.info("Detected %s register map for %s", profile, logger_sn)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PROFILE: profile}
        )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    async def _on_options_update(hass, entry):
        await coordinator.async_apply_options(entry.options)
        coordinator.async_update_listeners()  # option entities reflect the change

    entry.async_on_unload(entry.add_update_listener(_on_options_update))

    async def _dump_registers_service(call):
        """Diagnostic: sweep registers to /config/deye_register_dump.txt."""
        start = int(call.data.get("start", 0x0000))
        end = int(call.data.get("end", 0x0300))
        block = int(call.data.get("block", 16))
        text = await coordinator.async_dump_registers(start, end, block)
        path = hass.config.path("deye_register_dump.txt")
        await hass.async_add_executor_job(
            lambda: open(path, "w", encoding="utf-8").write(text)
        )
        _LOGGER.warning("Register dump written to %s (%d lines)", path, text.count("\n") + 1)

    hass.services.async_register(DOMAIN, "dump_registers", _dump_registers_service)

    async def _sync_clock_service(call):
        """Set the inverter RTC to HA's local time (see coordinator.async_sync_clock)."""
        # min_drift is optional and absent by default, so a human calling this
        # service always gets a sync. Only the automation passes a threshold.
        raw = call.data.get("min_drift")
        await coordinator.async_sync_clock_and_refresh(
            min_drift=None if raw is None else int(raw)
        )

    hass.services.async_register(DOMAIN, "sync_clock", _sync_clock_service)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload Deye BLE config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_close()  # release any held BLE link
    return unload_ok
