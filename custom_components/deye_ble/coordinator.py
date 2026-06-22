"""HA DataUpdateCoordinator for the Deye BLE integration.

This module imports homeassistant (it subclasses DataUpdateCoordinator so that
CoordinatorEntity attaches correctly). The pure, HA-free poll orchestration and
SN validation live in helpers.py and stay unit-testable without HA.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONFIG_READ_INTERVAL, DOMAIN
from .helpers import async_poll

_LOGGER = logging.getLogger(__name__)

# Keys read on the slower config cycle, plus the write-only controls that only
# exist optimistically (no read-back until P5 adds TOU reads). All are carried
# forward across polls so an optimistic value isn't dropped on the next cycle.
_CONFIG_KEYS = ("work_mode", "max_sell_power")
_CARRY_KEYS = _CONFIG_KEYS + ("charge_soc", "charge_start", "charge_end")


class DeyeBleCoordinator(DataUpdateCoordinator):
    """Polls telemetry every cycle; config (work_mode + max_sell) only every
    CONFIG_READ_INTERVAL or when mark_config_dirty() is called."""

    def __init__(
        self,
        hass,
        address: str,
        transport_factory,  # (BLEDevice) -> DeyeBleTransport
        scan_interval: int = 300,
        config_interval: int = CONFIG_READ_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._address = address
        self._transport_factory = transport_factory
        self._config_interval = config_interval
        self._last_config_read = 0.0
        self._config_dirty = True  # always read config on first cycle

    def mark_config_dirty(self) -> None:
        self._config_dirty = True

    def _config_due(self) -> bool:
        if self._config_dirty or self._last_config_read == 0.0:
            return True
        return (time.monotonic() - self._last_config_read) >= self._config_interval

    def _resolve_device(self):
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise UpdateFailed(f"BLE device {self._address} not found")
        return ble_device

    async def _async_update_data(self) -> dict:
        ble_device = self._resolve_device()

        # Decide once per cycle — re-checking after the await could acknowledge a
        # dirty mark that arrived mid-poll without the config actually being read.
        config_due = self._config_due()

        try:
            async with self._transport_factory(ble_device) as transport:
                data = await async_poll(transport, with_config=config_due)
        except Exception as e:
            _LOGGER.warning("BLE poll failed for %s: %s", self._address, e)
            raise UpdateFailed(str(e)) from e

        # Carry forward config + write-only control values not present this read.
        prev = self.data
        if prev:
            for key in _CARRY_KEYS:
                if key not in data and key in prev:
                    data[key] = prev[key]

        # Only acknowledge the config read if it was attempted AND came back.
        if config_due and all(key in data for key in _CONFIG_KEYS):
            self._last_config_read = time.monotonic()
            self._config_dirty = False

        return data

    async def async_write(self, reg: int, value: int) -> None:
        """Write a single holding register over BLE.

        Opens a fresh transport session and handshakes before writing.
        Write/poll serialization and dry-run safety are hardened in P5.
        """
        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise RuntimeError(f"BLE device {self._address} not found")
        async with self._transport_factory(ble_device) as transport:
            await transport.handshake()
            await transport.write(reg, value)
