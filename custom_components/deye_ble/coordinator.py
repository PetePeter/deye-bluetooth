"""BLE poll orchestration + HA DataUpdateCoordinator.

async_poll() is a pure async helper that does NOT import homeassistant — it
takes a transport-like object and returns decoded telemetry.  This keeps the
core polling loop unit-testable with a fake transport.

DeyeBleCoordinator is the thin HA glue around async_poll.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Protocol

from . import registers as r
from .const import CONFIG_READ_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


# --- Transport protocol (duck-typed, not bleak-dependent) --------------------

class _Transport(Protocol):
    async def handshake(self) -> None: ...
    async def read(self, address: int, count: int) -> list[int]: ...


# --- Pure async poll (unit-testable without HA) -----------------------------

async def async_poll(
    transport: _Transport,
    *,
    with_config: bool = False,
) -> dict[str, Any]:
    """Connect, handshake, read all register blocks, decode and return.

    Telemetry-block read failures propagate (the poll has failed). Control-block
    reads (only when *with_config*) are non-fatal: a failure is logged and the
    affected config keys are simply omitted, so a config hiccup never discards a
    good telemetry cycle. The caller carries forward the previous config values.
    """
    await transport.handshake()

    words_by_reg: dict[int, list[int]] = {}
    for start, count in r.READ_BLOCKS:
        words_by_reg[start] = await transport.read(start, count)

    if with_config:
        for start, count in r.CONTROL_BLOCKS:
            try:
                words_by_reg[start] = await transport.read(start, count)
            except Exception as e:  # noqa: BLE001 — config read is best-effort
                _LOGGER.debug("config block 0x%04X read failed (non-fatal): %s", start, e)

    return r.decode(words_by_reg)


# --- HA DataUpdateCoordinator wrapper --------------------------------------

# Keys that come from CONTROL_BLOCKS (slower poll).
_CONFIG_KEYS = ("work_mode", "max_sell_power")


class DeyeBleCoordinator:
    """Polls telemetry every cycle; config (work_mode + max_sell) only every
    CONFIG_READ_INTERVAL or when mark_config_dirty() is called."""

    def __init__(
        self,
        hass,  # homeassistant.core.HomeAssistant (avoid hard import)
        address: str,
        transport_factory,  # () -> DeyeBleTransport
        scan_interval: int = 300,
        config_interval: int = CONFIG_READ_INTERVAL,
    ) -> None:
        from homeassistant.helpers.update_coordinator import DataUpdateCoordinator  # noqa: F811

        self._address = address
        self._transport_factory = transport_factory
        self._scan_interval = scan_interval
        self._config_interval = config_interval
        self._last_config_read = 0.0
        self._config_dirty = True  # always read config on first cycle

        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
            update_method=self._async_update,
        )

    @property
    def data(self) -> dict:
        return self.coordinator.data

    def mark_config_dirty(self) -> None:
        self._config_dirty = True

    def _config_due(self) -> bool:
        if self._config_dirty or self._last_config_read == 0.0:
            return True
        return (time.monotonic() - self._last_config_read) >= self._config_interval

    async def _async_update(self) -> dict:
        from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: F811
        from homeassistant.components.bluetooth import async_ble_device_from_address

        hass = self.coordinator.hass

        ble_device = async_ble_device_from_address(hass, self._address)
        if ble_device is None:
            raise UpdateFailed(f"BLE device {self._address} not found")

        # Decide once per cycle — re-checking after the await could acknowledge a
        # dirty mark that arrived mid-poll without the config actually being read.
        config_due = self._config_due()

        try:
            async with self._transport_factory(ble_device) as transport:
                data = await async_poll(transport, with_config=config_due)
        except Exception as e:
            _LOGGER.warning("BLE poll failed for %s: %s", self._address, e)
            raise UpdateFailed(str(e)) from e

        # Carry forward config keys not present in this read (e.g. a non-fatal
        # config-block miss, or a telemetry-only cycle).
        prev = self.data
        if prev:
            for key in _CONFIG_KEYS:
                if key not in data and key in prev:
                    data[key] = prev[key]

        # Only acknowledge the config read if it was actually attempted AND the
        # config keys came back this cycle.
        if config_due and all(key in data for key in _CONFIG_KEYS):
            self._last_config_read = time.monotonic()
            self._config_dirty = False

        return data


# --- SN validation (pure, no HA import) ------------------------------------

def validate_logger_sn(sn: str) -> str:
    """Normalise and validate a Deye logger serial number.

    Returns the stripped uppercased string on success, raises ValueError
    otherwise.  Valid SNs are alphanumeric, 8–20 characters after stripping.
    """
    sn = sn.strip().upper()
    if not 8 <= len(sn) <= 20:
        raise ValueError("Logger serial must be 8-20 characters")
    if not sn.isalnum():
        raise ValueError("Logger serial must be alphanumeric")
    return sn
