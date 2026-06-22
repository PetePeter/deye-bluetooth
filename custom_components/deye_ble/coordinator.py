"""HA DataUpdateCoordinator for the Deye BLE integration.

This module imports homeassistant (it subclasses DataUpdateCoordinator so that
CoordinatorEntity attaches correctly). The pure, HA-free poll orchestration and
SN validation live in helpers.py and stay unit-testable without HA.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONFIG_READ_INTERVAL,
    DEFAULT_DRY_RUN,
    DEFAULT_REASSERT,
    DOMAIN,
    MAX_POLL_FAILURES,
)
from .helpers import async_poll, detect_drift, verify_readback

_LOGGER = logging.getLogger(__name__)

# Keys read on the slower config cycle, plus the write-only controls that only
# exist optimistically (no read-back until P5 adds TOU reads). All are carried
# forward across polls so an optimistic value isn't dropped on the next cycle.
_CONFIG_KEYS = ("work_mode", "max_sell_power")
_CARRY_KEYS = _CONFIG_KEYS + ("charge_soc", "charge_start", "charge_end")


class DeyeBleCoordinator(DataUpdateCoordinator):
    """Polls telemetry every cycle; config (work_mode + max_sell) only every
    CONFIG_READ_INTERVAL or when mark_config_dirty() is called.

    Supports P5 write safety: dry-run (default ON), read-back verify, and
    optional local-wins reassert.
    """

    def __init__(
        self,
        hass,
        address: str,
        transport_factory,  # (BLEDevice) -> DeyeBleTransport
        scan_interval: int = 300,
        config_interval: int = CONFIG_READ_INTERVAL,
        dry_run: bool = DEFAULT_DRY_RUN,
        reassert: bool = DEFAULT_REASSERT,
        max_failures: int = MAX_POLL_FAILURES,
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
        self._dry_run = dry_run
        self._reassert = reassert
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._tracked_values: dict[int, int | str] = {}
        # The logger accepts ONE BLE central at a time, so polls and writes must
        # never hold a connection simultaneously, or bleak reports
        # "br-connection-canceled". This lock serializes all BLE sessions.
        self._ble_lock = asyncio.Lock()

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._dry_run = value

    @property
    def reassert(self) -> bool:
        return self._reassert

    @reassert.setter
    def reassert(self, value: bool) -> None:
        self._reassert = value

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
        try:
            ble_device = self._resolve_device()

            # Decide once per cycle — re-checking after the await could acknowledge
            # a dirty mark that arrived mid-poll without config actually being read.
            config_due = self._config_due()

            async with self._ble_lock:
                async with self._transport_factory(ble_device) as transport:
                    data = await async_poll(transport, with_config=config_due)
        except Exception as e:
            return self._handle_poll_failure(e)

        # A good cycle clears the transient-failure run.
        self._consecutive_failures = 0

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

        # Reassert: if enabled, detect drift and re-apply drifted values once.
        if self._reassert and self._tracked_values:
            drifted = detect_drift(self._tracked_values, data)
            if drifted:
                _LOGGER.info("reassert: correcting %d drifted register(s)", len(drifted))
                for reg, expected in drifted:
                    try:
                        await self.async_write(reg, expected)
                    except Exception as e:
                        _LOGGER.warning("reassert write to 0x%04X failed: %s", reg, e)

        return data

    def _handle_poll_failure(self, exc: Exception) -> dict:
        """Ride out transient BLE failures by keeping the last good values.

        Returns the previous data (so entities stay available) until
        *max_failures* consecutive failures accumulate, then re-raises as
        UpdateFailed so a genuine outage surfaces. With no prior data (e.g. the
        first refresh), the failure propagates immediately — there is nothing to
        carry forward.
        """
        self._consecutive_failures += 1

        if self.data is not None and self._consecutive_failures < self._max_failures:
            _LOGGER.warning(
                "BLE poll failed for %s (%d/%d), keeping last values: %s",
                self._address,
                self._consecutive_failures,
                self._max_failures,
                exc,
            )
            return self.data

        _LOGGER.warning("BLE poll failed for %s: %s", self._address, exc)
        raise UpdateFailed(str(exc)) from exc

    async def async_write(self, reg: int, value: int) -> None:
        """Write a single holding register over BLE.

        Opens a fresh transport session and handshakes before writing.
        When dry-run is ON, logs intent and returns without issuing a GATT write.
        When dry-run is OFF, writes, reads back, and verifies the value.
        Tracks the value for optional drift detection (reassert).
        """
        # Dry-run is the safety default: never touch the radio, just log intent.
        # Checked first so it works even when the device is momentarily absent.
        if self._dry_run:
            _LOGGER.info(
                "dry-run: would write reg 0x%04X = %d (no GATT write issued)", reg, value
            )
            return

        ble_device = async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise HomeAssistantError(f"BLE device {self._address} not found")

        # Serialize against the poll — one BLE central at a time on the logger.
        async with self._ble_lock:
            async with self._transport_factory(ble_device) as transport:
                await transport.handshake()
                await transport.write(reg, value)
                # Read back to confirm.
                readback = await transport.read(reg, 1)
                actual = readback[0]
                verify_readback(reg, value, actual)

        # Track for drift detection.
        self._tracked_values[reg] = value
