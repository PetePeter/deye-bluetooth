"""HA DataUpdateCoordinator for the Deye BLE integration.

This module imports homeassistant (it subclasses DataUpdateCoordinator so that
CoordinatorEntity attaches correctly). The pure, HA-free poll orchestration and
SN validation live in helpers.py and stay unit-testable without HA.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import registers as r
from .const import (
    CLOCK_CACHE_MAX_AGE,
    CLOCK_SYNC_ATTEMPTS,
    CLOCK_SYNC_SETTLE,
    CLOCK_SYNC_TOLERANCE,
    CLOCK_KNOWN_BAD_ATTEMPTS,
    CLOCK_KNOWN_BAD_BACKOFF,
    CLOCK_KNOWN_BAD_CEILING,
    CLOCK_COMMIT_SETTLE,
    CONFIG_READ_INTERVAL,
    DEFAULT_DRY_RUN,
    DEFAULT_KEEPALIVE,
    DEFAULT_REASSERT,
    DOMAIN,
    MAX_POLL_FAILURES,
    MAX_WRITE_ATTEMPTS,
    DEFAULT_CLOUD_SYNC_INTERVAL,
    DEFAULT_CLOUD_WINDOW_DURATION,
    DEFAULT_FULLY_LOCAL,
    DEFAULT_BLE_ENABLED,
    SLOW_TELEMETRY_INTERVAL,
    SYNC_CLOCK_WRONG,
    SYNC_FAILED,
    SYNC_OK,
    SYNC_SKIPPED,
    VERIFY_OK,
    VERIFY_UNVERIFIED,
    VERIFY_WRONG,
    WRITE_RETRY_BACKOFF,
)
from .helpers import (
    async_poll,
    clock_drift_seconds,
    clock_within_tolerance,
    detect_drift,
    hold_spurious_total_resets,
    time_of_day_drift_seconds,
    verify_readback,
)

_LOGGER = logging.getLogger(__name__)

# Keys read on the slower config cycle, plus the write-only controls that only
# exist optimistically (no read-back until P5 adds TOU reads). All are carried
# forward across polls so a config value isn't dropped (blanking its entity) on
# the telemetry-only cycles between config reads.
_CONFIG_KEYS = (
    "work_mode",
    "max_sell_power",
    "zero_export_power",
    "max_charge_current",
    "max_discharge_current",
    "batt_shutdown_soc",
    "batt_restart_soc",
    "batt_low_soc",
    "grid_charge_current",
    "smartload_off_soc",
    "smartload_on_soc",
)
_SLOW_KEYS = (
    "total_solar", "total_grid_import", "total_grid_export",
    "total_battery_charge", "total_battery_discharge", "total_consumption",
    "daily_solar", "daily_grid_import", "daily_grid_export", "daily_load_energy",
    "daily_battery_charge", "daily_battery_discharge",
    "max_solar_power", "running_status", "inverter_status", "total_load_energy",
    "dc_temperature", "ac_temperature",
    "pv1_voltage", "pv1_current", "pv2_voltage", "pv2_current",
    "battery_temp", "battery_soc_bms", "inverter_temp", "io_mode", "has_fault", "has_alarm", "fault_message", "alarm_message",
    "grid_voltage_l1", "grid_current_l1", "grid_voltage_l2", "grid_voltage_l3", "grid_frequency",
    "bms_charge_voltage", "bms_discharge_voltage",
    "bms_charge_current_limit", "bms_discharge_current_limit",
    "rated_power", "battery_rated_capacity", "total_dc_input_power", "grid_total_reactive_power",
)
_CARRY_KEYS = _CONFIG_KEYS + _SLOW_KEYS + (
    "charge_soc", "discharge_soc", "charge_start", "charge_end",
    "energy_priority", "solar_sell", "fault_codes", "grid_charge_enabled", "tou_enabled",
    "smartload_min_solar_power", "gen_peak_shaving_power", "grid_peak_shaving_power",
    "peak_shaving_flags_raw", "grid_peak_shaving", "gen_peak_shaving",
    "gen_peak_power", "grid_peak_power",
    "inverter_clock", "inverter_clock_drift", "inverter_time_of_day_drift",
    "time_sync",
)


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
        write_attempts: int = MAX_WRITE_ATTEMPTS,
        keepalive: bool = DEFAULT_KEEPALIVE,
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
        self._write_attempts = max(1, write_attempts)
        self._write_backoff = WRITE_RETRY_BACKOFF
        self._consecutive_failures = 0
        self._tracked_values: dict[int, int | str] = {}
        # Last clock-sync result, re-published on every poll (see
        # _record_sync_outcome). Empty until a sync has actually run.
        self._clock_sync_state: dict[str, object] = {}
        self._clock_sync_seq = 0
        # Set when a cancelled sync hands its Time Sync re-enable to a task that
        # waits for the BLE lock; awaited on unload so it cannot outlive us.
        self._pending_enable = None
        self._keepalive = keepalive
        # The single reused transport when keepalive is on; None when there is no
        # live session (keepalive off, not yet connected, or dropped after error).
        self._persistent = None
        # The logger accepts ONE BLE central at a time, so polls and writes must
        # never hold a connection simultaneously, or bleak reports
        # "br-connection-canceled". This lock serializes all BLE sessions.
        self._ble_lock = asyncio.Lock()
        self._slow_interval = SLOW_TELEMETRY_INTERVAL
        self._fast_interval = 60
        self._scan_interval = scan_interval
        self._last_slow_read = 0.0
        self._cloud_sync_interval = DEFAULT_CLOUD_SYNC_INTERVAL
        self._cloud_window_duration = DEFAULT_CLOUD_WINDOW_DURATION
        self._fully_local = DEFAULT_FULLY_LOCAL
        self._ble_enabled = DEFAULT_BLE_ENABLED
        self._last_cloud_sync = time.monotonic()
        self._cloud_window_active = False
        self._cloud_window_start = 0.0

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

    @property
    def ble_enabled(self) -> bool:
        return self._ble_enabled

    async def async_set_ble_enabled(self, value: bool) -> None:
        self._ble_enabled = value
        if not value:
            async with self._ble_lock:
                await self._drop_persistent()

    @property
    def fully_local(self) -> bool:
        return self._fully_local

    def set_fully_local(self, value: bool) -> None:
        self._fully_local = value
        if value:
            self._cloud_window_active = False

    @property
    def cloud_sync_interval(self) -> int:
        return int(self._cloud_sync_interval)

    def set_cloud_sync_interval(self, value: int) -> None:
        self._cloud_sync_interval = value

    @property
    def cloud_window_duration(self) -> int:
        return int(self._cloud_window_duration)

    def set_cloud_window_duration(self, value: int) -> None:
        self._cloud_window_duration = value

    @property
    def scan_interval_seconds(self) -> int:
        return getattr(self, "_scan_interval", 5)

    def set_scan_interval(self, value: int) -> None:
        self._scan_interval = max(1, min(10, value))
        self.update_interval = timedelta(seconds=self._scan_interval)

    @property
    def fast_interval_seconds(self) -> int:
        return getattr(self, "_fast_interval", 60)

    def set_fast_interval(self, value: int) -> None:
        self._fast_interval = max(15, min(300, value))

    @property
    def slow_interval_seconds(self) -> int:
        return getattr(self, "_slow_interval", 1800)

    def set_slow_interval(self, value: int) -> None:
        self._slow_interval = max(300, min(3600, value))

    @property
    def keepalive(self) -> bool:
        return self._keepalive

    async def async_set_keepalive(self, value: bool) -> None:
        """Toggle persistent-connection mode, releasing the held link if turned
        off so the ESP proxy slot (and the logger's single central) frees up at
        once."""
        self._keepalive = value
        if not value:
            async with self._ble_lock:
                await self._drop_persistent()

    async def async_close(self) -> None:
        """Release any held BLE session (called on unload).

        Waits for a deferred Time Sync re-enable first: it holds the lock
        briefly and must not be abandoned mid-write, or unload becomes another
        way to leave cloud calibration disabled.
        """
        pending, self._pending_enable = self._pending_enable, None
        if pending is not None:
            try:
                await pending
            except Exception:  # noqa: BLE001 â€” already logged by the task
                pass
        async with self._ble_lock:
            await self._drop_persistent()

    async def _drop_persistent(self) -> None:
        """Disconnect and forget the reused transport, best-effort."""
        transport, self._persistent = self._persistent, None
        if transport is not None:
            try:
                await transport.disconnect()
            except Exception:  # noqa: BLE001 â€” teardown is best-effort
                _LOGGER.debug("dropping persistent BLE link failed", exc_info=True)

    @asynccontextmanager
    async def _session(self, ble_device):
        """Yield a connected transport following the keepalive strategy.

        keepalive OFF: open a fresh session and close it on exit (the resilient
        default â€” every poll/write reconnects). keepalive ON: connect once and
        reuse the link across polls and writes, but drop it on any error so the
        next call cleanly reconnects. Callers hold ``_ble_lock`` around this.
        """
        if not self._keepalive:
            async with self._transport_factory(ble_device) as transport:
                yield transport
            return

        if self._persistent is None:
            transport = self._transport_factory(ble_device)
            await transport.connect()
            self._persistent = transport
        try:
            yield self._persistent
        except Exception:
            # A reused link that just errored is suspect â€” drop it rather than
            # keep handing back a possibly half-open connection.
            await self._drop_persistent()
            raise

    def mark_config_dirty(self) -> None:
        self._config_dirty = True

    def _config_due(self) -> bool:
        if self._config_dirty or self._last_config_read == 0.0:
            return True
        return (time.monotonic() - self._last_config_read) >= self._config_interval

    def _resolve_device(self):
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"BLE device {self._address} not found")
        return ble_device

    async def _async_update_data(self) -> dict:
        try:
            # Cloud coexistence: the inverter logger shares one processor between
            # BLE and its Wi-Fi upload to the Deye cloud, so a permanently busy
            # link starves the cloud feed. While fully-local mode is OFF, give the
            # logger a scheduled quiet window with no BLE traffic at all.
            if not self._fully_local and self.data is not None:
                now = time.monotonic()
                if self._cloud_window_active:
                    elapsed = now - self._cloud_window_start
                    if elapsed < self._cloud_window_duration:
                        _LOGGER.debug(
                            "Cloud sync window active (%ds remaining); skipping BLE poll",
                            int(self._cloud_window_duration - elapsed),
                        )
                        return self.data  # keep last telemetry, touch no BLE
                    self._cloud_window_active = False
                    self._last_cloud_sync = now
                    _LOGGER.info("Cloud sync window ended; resuming fast BLE polling")
                elif now - self._last_cloud_sync >= self._cloud_sync_interval:
                    self._cloud_window_active = True
                    self._cloud_window_start = now
                    _LOGGER.info(
                        "Starting cloud sync window for %ds (releasing BLE for Wi-Fi upload)",
                        self._cloud_window_duration,
                    )
                    async with self._ble_lock:
                        await self._drop_persistent()
                    return self.data

            ble_device = self._resolve_device()

            # Decide once per cycle â€” re-checking after the await could acknowledge
            # a dirty mark that arrived mid-poll without config actually being read.
            config_due = self._config_due()

            # Fast cycles read only FAST_BLOCKS (the live power/SOC set); every
            # _slow_interval seconds a full read refreshes lifetime energy,
            # string volts/amps and temperatures. Those keys are carried forward
            # between slow reads so their entities never blank out.
            now = time.monotonic()
            slow_due = (
                self._last_slow_read == 0.0
                or (now - self._last_slow_read) >= self._slow_interval
            )

            async with self._ble_lock:
                async with self._session(ble_device) as transport:
                    data = await async_poll(
                        transport, with_config=config_due, full_telemetry=slow_due
                    )

            # Only remember the slow read once it actually succeeded; a failed
            # full poll leaves slow_due set so the next cycle retries it.
            if slow_due:
                self._last_slow_read = now
        except Exception as e:
            return self._handle_poll_failure(e)

        # A good cycle clears the transient-failure run.
        self._consecutive_failures = 0

        # Drift is measured against the clock reading it arrived with, BEFORE
        # the carry-forward below â€” recomputing it against a carried clock would
        # make it grow one second per second between config reads.
        if "inverter_clock" in data:
            measured_at = dt_util.now()
            data["inverter_clock_drift"] = clock_drift_seconds(
                data["inverter_clock"], measured_at
            )
            data["inverter_time_of_day_drift"] = time_of_day_drift_seconds(
                data["inverter_clock"], measured_at
            )

        data = self._with_sync_outcome(data)

        # Carry forward config + write-only control values not present this read.
        prev = self.data
        if prev:
            for key in _CARRY_KEYS:
                if key not in data and key in prev:
                    data[key] = prev[key]

        # A BLE frame that decodes a lifetime total as 0 must not reach a
        # total_increasing sensor â€” HA would log a meter reset and count the
        # recovery as a phantom spike. Hold the last good value instead.
        data = hold_spurious_total_resets(prev, data)

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

    def _with_sync_outcome(self, data: dict | None) -> dict:
        """Stamp the last clock-sync result onto a data snapshot.

        The outcome is coordinator state, not a device reading, so every poll
        rebuilds the snapshot and this re-applies it. Failure paths do not need
        it: the result is published into ``self.data`` the moment it is recorded
        (see :meth:`_record_sync_outcome`), so a sync that verified still reads
        as verified through any number of subsequent poll failures.
        """
        return {**(data or {}), **self._clock_sync_state}

    def _handle_poll_failure(self, exc: Exception) -> dict:
        """Ride out transient BLE failures by keeping the last good values.

        Returns the previous data (so entities stay available) until
        *max_failures* consecutive failures accumulate, then re-raises as
        UpdateFailed so a genuine outage surfaces. With no prior data (e.g. the
        first refresh), the failure propagates immediately â€” there is nothing to
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
            # No outcome stamp needed here: self.data already carries it.
            # _record_sync_outcome publishes into self.data the moment a sync
            # finishes, and every success path re-stamps, so the invariant
            # "outcome recorded => outcome in self.data" holds before we get
            # here. Re-merging would be a line no test could ever fail on.
            return self.data

        _LOGGER.warning("BLE poll failed for %s: %s", self._address, exc)
        raise UpdateFailed(str(exc)) from exc

    async def async_write(self, reg: int, value: int) -> None:
        """Write a single holding register over BLE.

        Opens a fresh transport session and handshakes before writing.
        When dry-run is ON, logs intent and returns without issuing a GATT write.
        When dry-run is OFF, writes, reads back, and verifies the value.
        Transient BLE failures are retried (see :meth:`_write_regs`); the value
        is tracked for optional drift detection (reassert) once it lands.
        """
        await self._write_regs({reg: value})

    async def async_write_many(self, regs: dict[int, int]) -> None:
        """Write several holding registers in a single BLE session.

        Used where one logical control spans multiple registers (e.g. the
        discharge floor written to every non-charge TOU slot). Reuses the same
        safety model as :meth:`async_write` â€” dry-run gate, per-register
        read-back verify, retry, and drift tracking â€” but opens just one
        connection so five slots don't mean five reconnects on a flaky link.
        """
        await self._write_regs(regs)

    async def async_dump_registers(
        self, start: int, end: int, block: int = 16
    ) -> str:
        """Sweep holding registers [start, end) and return a reg=value text dump.

        Diagnostic only: read-only, blocks that the inverter rejects are noted
        and skipped rather than aborting the sweep. Serialized against the poll
        via the BLE lock (one central at a time). 100-reg reads get no BLE reply,
        so the default block size matches the small-read limit.
        """
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(f"BLE device {self._address} not found")

        lines: list[str] = []
        async with self._ble_lock:
            async with self._transport_factory(ble_device) as transport:
                await transport.handshake()
                addr = start
                while addr < end:
                    count = min(block, end - addr)
                    try:
                        words = await transport.read(addr, count)
                        for i, w in enumerate(words):
                            lines.append(f"0x{addr + i:04X} {w}")
                    except Exception as exc:  # noqa: BLE001 â€” diagnostic, keep going
                        lines.append(f"# block 0x{addr:04X}+{count} failed: {exc}")
                    addr += count
        return "\n".join(lines)

    async def async_sync_clock(self, min_drift: int | None = None) -> None:
        """Set the inverter's real-time clock to HA's local time.

        The RTC ignores plain register writes: it commits whatever is staged in
        0x003E-0x0040 when the Time Sync bit (0x00E4 bit 0) goes on -> off. So a
        sync is a sequence, not a write:

            read 0x00E4 -> set bit0 -> settle -> stage clock -> clear bit0

        Four things this must get right, each paid for in evidence
        (local-deye-cloud/docs/inverter-clock-2026-08-09.md and the observation
        log for 2026-08-13):

        * The sequence ALWAYS ends with Time Sync ON â€” it does not restore the
          flag to whatever it was. Two reasons. Time Sync off means the logger's
          cloud calibration cannot reach the RTC, which is how the phone app's
          "set time" silently broke the clock in the first place; finding it off
          is a fault to repair, not a preference to preserve. And a final write
          of a CLEARED bit would itself be a falling edge â€” a second commit â€”
          which, if an error had interrupted the three clock writes, would latch
          a mixture of newly written words and stale ones: a new date against an
          old time, wrong by hours or days. Ending on a rising edge cannot
          commit anything, so that failure mode does not exist rather than being
          handled.
        * The whole sequence runs under ONE lock, so no poll can land inside an
          open commit window. A link that ERRORS is dropped before the next
          attempt â€” the exception is caught outside the session context, so
          keepalive's drop-on-error runs. A verification mismatch is different
          and deliberately keeps the link: the link demonstrably works (we just
          read back through it), the fault is in the write, and reconnect churn
          on this logger has its own history of wedging the radio.
        * The write is not reliable first time (a year byte 0x1A once landed as
          0x7A). The readback is verified by drift against a fresh now, and the
          target is re-derived on every attempt â€” never a replayed intent.
        * A failed re-enable fails the whole call. Cloud calibration really is
          left disabled in that case, and reporting success would reproduce the
          silent failure this feature exists to fix.

        Deliberately does not go through :meth:`_write_regs`: that path's strict
        read-back equality is right for every other control and wrong for a
        register that ticks while you look at it. The clock is also never added
        to ``_tracked_values`` â€” it legitimately changes every second, so
        reassert would fight the inverter forever.

        Raises HomeAssistantError if no attempt verifies, or if the clock landed
        but Time Sync could not be re-enabled.
        """
        if self._dry_run:
            for reg, value in regs.items():
                _LOGGER.warning(
                    "Inverter write blocked (Safety Lock ACTIVE / Dry-Run): Would write reg 0x%04X = %d", reg, value
                )
            raise HomeAssistantError(
                "Inverter write blocked! Turn ON 'Allow Inverter Writes' safety switch before modifying settings."
            )

        ble_device = self._resolve_device()
        pre_flags: int | None = None
        last_error: Exception | None = None
        enable_error: Exception | None = None
        attempts = 0
        verdict = VERIFY_UNVERIFIED
        # STICKY. Transport noise on a later attempt must not erase the fact
        # that we watched this RTC read a bad date: verdict is per-attempt, and
        # an UNVERIFIED attempt after a WRONG one would otherwise downgrade a
        # known-bad clock to a generic failure and skip the extension.
        ever_wrong = False
        skipped = False
        cancelled = False

        async with self._ble_lock:
            try:
                if min_drift is not None:
                    try:
                        async with self._session(ble_device) as transport:
                            await transport.handshake()
                            pre_flags = (await transport.read(r.REG_TIME_SYNC, 1))[0]
                            drift = await self._read_drift(transport)
                    except Exception as exc:  # noqa: BLE001 â€” unknown means sync
                        _LOGGER.warning(
                            "could not read the clock before deciding (%s) â€” "
                            "syncing rather than assuming", exc,
                        )
                        drift = None
                    # An unreadable drift is NOT a reason to skip. Skipping on
                    # unknown means an inverter we cannot read is an inverter we
                    # never fix, and it fails silently â€” the clock stays wrong
                    # and nothing reports it. Erring toward the write costs one
                    # unnecessary sync; erring toward the skip costs a clock
                    # nobody corrects. Keep this default through any refactor.
                    # The reading can be up to CLOCK_CACHE_MAX_AGE stale, so it
                    # must clear the threshold by that margin before we trust a
                    # skip. Erring toward the write is right while the write is
                    # only risky when the clock is actually wrong.
                    if drift is not None and abs(drift) + CLOCK_CACHE_MAX_AGE <= min_drift:
                        skipped = True
                        _LOGGER.info(
                            "inverter clock drift %+d s clears the %d s threshold "
                            "even allowing %.0f s of cache staleness â€” not writing",
                            drift, min_drift, CLOCK_CACHE_MAX_AGE,
                        )

                if not skipped:
                    for attempt in range(1, CLOCK_SYNC_ATTEMPTS + 1):
                        attempts = attempt
                        try:
                            async with self._session(ble_device) as transport:
                                await transport.handshake()
                                if pre_flags is None:
                                    # The other bits of 0x00E4 belong to the
                                    # System Time panel (AM/PM, Auto Dim, Beep,
                                    # Factory Reset) â€” read once and handed back
                                    # untouched. Never re-read on a later
                                    # attempt: that would capture our own arming
                                    # write as the original.
                                    pre_flags = (await transport.read(r.REG_TIME_SYNC, 1))[0]
                                verdict = await self._commit_clock(
                                    transport, pre_flags, attempt
                                )
                            ever_wrong = ever_wrong or verdict == VERIFY_WRONG
                            if verdict == VERIFY_OK:
                                break
                        except Exception as exc:  # noqa: BLE001 â€” a flaky link earns a retry
                            # Caught OUTSIDE the session context, so a failed
                            # link is dropped before the next attempt.
                            verdict = VERIFY_UNVERIFIED
                            last_error = exc
                            _LOGGER.warning(
                                "inverter clock sync attempt %d/%d failed: %s",
                                attempt, CLOCK_SYNC_ATTEMPTS, exc,
                            )

                    if ever_wrong and verdict != VERIFY_OK:
                        verdict, attempts = await self._push_while_known_bad(
                            ble_device, pre_flags, attempts
                        )
                        ever_wrong = ever_wrong or verdict == VERIFY_WRONG
                    # A clock last seen WRONG stays reported as wrong unless
                    # something later verified it good.
                    if ever_wrong and verdict != VERIFY_OK:
                        verdict = VERIFY_WRONG
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                # Never raises â€” see _enable_time_sync. A raise here would
                # replace the exception already unwinding (including a
                # CancelledError) and skip the outcome record below.
                enable_error = await self._enable_time_sync(ble_device, pre_flags)
                if cancelled:
                    # Cancellation would otherwise bypass the outcome entirely,
                    # leaving a verifier with no result at all for this run.
                    self._record_sync_outcome(SYNC_FAILED, attempts)

        result = self._classify(skipped, verdict, enable_error)
        self._record_sync_outcome(result, attempts)

        if enable_error is not None:
            # Checked FIRST and regardless of skip: the clock may be perfect
            # while cloud calibration sits disabled, which is the fault this
            # feature exists to repair. A skipped check that failed to re-enable
            # would otherwise report success and alert nobody.
            raise HomeAssistantError(
                "inverter clock: Time Sync could not be re-enabled, so cloud "
                f"clock calibration is left DISABLED: {enable_error}"
            )
        if result == SYNC_CLOCK_WRONG:
            raise HomeAssistantError(
                "INVERTER CLOCK LEFT WRONG: every attempt read back an "
                f"out-of-tolerance time and {attempts} attempts did not correct "
                "it. The RTC is on a bad date until the next successful sync."
            )
        if result == SYNC_FAILED:
            raise HomeAssistantError(
                f"inverter clock sync could not be verified after {attempts} "
                "attempt(s)" + (f": {last_error}" if last_error else "")
            )

        # The drift sensors are a snapshot of the last config read; without this
        # they would still show the pre-sync drift for up to CONFIG_READ_INTERVAL
        # â€” long enough for a verifying automation to call a good sync failed.
        self.mark_config_dirty()

        if enable_error is not None:
            raise HomeAssistantError(
                "inverter clock was set, but Time Sync could not be re-enabled "
                f"â€” cloud clock calibration is left disabled: {enable_error}"
            )

    async def async_sync_clock_and_refresh(self, min_drift: int | None = None) -> None:
        """Sync the clock, then poll, so the caller sees the result it caused.

        The two steps belong together at every entry point a human or an
        automation uses. mark_config_dirty alone only arms the NEXT poll, which
        can be a full scan interval away; a caller that verifies the sync by
        reading the drift entity would be looking at the pre-sync snapshot.

        async_refresh, not async_request_refresh: that one is debounced and
        returns before the poll has actually happened, which puts the same stale
        read back in front of the caller by a different route.
        """
        await self.async_sync_clock(min_drift=min_drift)
        await self.async_refresh()

    @staticmethod
    def _classify(skipped: bool, verdict: str, enable_error: Exception | None) -> str:
        """Map the sequence's end state to a published outcome.

        enable_error is checked FIRST, ahead of skipped: a failed re-enable
        leaves cloud calibration disabled whatever else happened, and reporting
        that run as "skipped" (which callers treat as success) would make the
        original fault of this whole feature reachable in silence.
        """
        if enable_error is not None:
            return SYNC_FAILED
        if skipped:
            return SYNC_SKIPPED
        if verdict == VERIFY_WRONG:
            return SYNC_CLOCK_WRONG
        if verdict == VERIFY_OK:
            return SYNC_OK
        return SYNC_FAILED

    async def _push_while_known_bad(
        self, ble_device, pre_flags: int | None, attempts: int
    ) -> tuple[str, int]:
        """Keep correcting while the RTC is VERIFIED wrong, on a bounded budget.

        Walking away from a clock we have just watched read 2074 is the worst
        outcome available: nothing else corrects this RTC, so it stays wrong
        until the next scheduled run. But unbounded retrying against a live
        inverter is its own hazard, so this is a few slower attempts under a hard
        ceiling â€” and if it ends still wrong, that is reported as its own
        outcome, not folded into a generic failure.
        """
        _LOGGER.warning(
            "inverter clock is verified WRONG after %d attempt(s) â€” continuing on "
            "the bounded extension rather than leaving it on a bad date", attempts,
        )
        verdict = VERIFY_WRONG
        # monotonic, not accumulated backoff: the earlier version added up only
        # the sleeps, so it excluded handshakes, BLE waits and up to 30 s of
        # verification per attempt â€” i.e. everything that makes an attempt hang,
        # which is the case a wall-clock stop exists for. monotonic also cannot
        # be stepped by an NTP correction, which matters in a clock feature.
        deadline = time.monotonic() + CLOCK_KNOWN_BAD_CEILING

        for extra in range(1, CLOCK_KNOWN_BAD_ATTEMPTS + 1):
            if time.monotonic() + CLOCK_KNOWN_BAD_BACKOFF > deadline:
                _LOGGER.warning(
                    "clock extension stopped at the %.0f s ceiling after %d attempt(s)",
                    CLOCK_KNOWN_BAD_CEILING, attempts,
                )
                break
            await asyncio.sleep(CLOCK_KNOWN_BAD_BACKOFF)
            attempts += 1
            try:
                async with self._session(ble_device) as transport:
                    await transport.handshake()
                    # Bound the ATTEMPT, not just the wait before it. Checking
                    # the deadline only before sleeping leaves a slow handshake,
                    # command or read free to run straight past the ceiling â€”
                    # which is the exact case a wall-clock stop exists for.
                    verdict = await asyncio.wait_for(
                        self._commit_clock(transport, pre_flags, attempts),
                        timeout=max(0.1, deadline - time.monotonic()),
                    )
                if verdict == VERIFY_OK:
                    _LOGGER.info(
                        "inverter clock corrected on extension attempt %d", extra
                    )
                    return verdict, attempts
            except Exception as exc:  # noqa: BLE001 â€” keep trying, it is still wrong
                _LOGGER.warning("clock extension attempt %d failed: %s", extra, exc)
                verdict = VERIFY_UNVERIFIED

        return verdict, attempts

    async def _read_drift(self, transport) -> int | None:
        """Drift in seconds from one read, or None if it cannot be decoded.

        Deliberately not a freshness hunt â€” on a self-ticking register there is
        no such test (see :meth:`_verify_commit`). The reading may be up to
        CLOCK_CACHE_MAX_AGE stale, and the caller compensates with a margin
        rather than pretending the number is exact.
        """
        frame = await transport.read(r.REG_CLOCK, r.CLOCK_WORD_COUNT)
        clock = r.decode_clock(frame)
        if clock is None:
            return None
        return clock_drift_seconds(clock, dt_util.now())

    async def _commit_clock(self, transport, pre_flags: int, attempt: int) -> str:
        """Run one arm/stage/commit cycle. Returns a VERIFY_* verdict.

        Transport errors propagate: the caller needs them to leave the session
        context so a broken link is dropped before the next attempt.
        """
        await transport.write(
            r.REG_TIME_SYNC, r.set_flag(pre_flags, r.TIME_SYNC_MASK, True)
        )
        # See CLOCK_SYNC_SETTLE: evidence for this gap is one rehearsal each
        # way, so it is deliberate but honestly unconfirmed.
        await asyncio.sleep(CLOCK_SYNC_SETTLE)

        # Re-derived here, inside the attempt: a retry that replayed the first
        # attempt's target would write a time stale by an attempt.
        #
        # The frame as it stands BEFORE the write. Not a freshness baseline â€”
        # a changed frame proves nothing on a ticking register (see
        # _verify_commit). This is the negative half: a read-back byte-identical
        # to this one cannot be a fresh read of a changed clock, so it proves
        # the logger's cache did not invalidate and the read-back is worthless.
        # Optional, deliberately: this is an extra guard, not a prerequisite.
        # Failing the whole attempt because a diagnostic read failed would make
        # the feature more brittle than it was before the guard existed â€” an
        # inverter we cannot read is exactly one whose clock still needs setting.
        try:
            pre_frame = tuple(await transport.read(r.REG_CLOCK, r.CLOCK_WORD_COUNT))
        except Exception as exc:  # noqa: BLE001 â€” the guard is best-effort
            _LOGGER.debug("no pre-write frame for the cache guard: %s", exc)
            pre_frame = None

        # ONE contiguous block frame, not three single-register writes. Measured
        # 2026-08-15: single writes corrupt the year byte 4 times in 5, block
        # writes 0 in 5, interleaved so logger drift cannot explain it. See
        # protocol.build_write_block. It also removes partial staging by
        # construction â€” there is no longer a gap between register writes for an
        # error to land in.
        target = dt_util.now()
        await transport.write_block(r.REG_CLOCK, r.encode_clock(target))

        await transport.write(
            r.REG_TIME_SYNC, r.set_flag(pre_flags, r.TIME_SYNC_MASK, False)
        )

        verdict = await self._verify_commit(transport, attempt, pre_frame)
        if verdict == VERIFY_OK:
            _LOGGER.info(
                "inverter clock synced to %s (attempt %d/%d)",
                target.strftime("%Y-%m-%d %H:%M:%S"), attempt, CLOCK_SYNC_ATTEMPTS,
            )
        return verdict

    async def _verify_commit(self, transport, attempt: int, pre_frame: tuple | None) -> str:
        """Judge the commit on CONTENT versus INTENT. Returns a VERIFY_* verdict.

        Not on frame changes: the RTC ticks, so consecutive reads differ whether
        or not the write landed. A probe on 2026-08-15 produced 14 distinct
        frames across 68 s of a commit that had never taken â€” change means time
        passed, and nothing else.

        An undecodable frame is UNVERIFIED rather than WRONG. We cannot see what
        the clock holds, which is an absence of evidence; only a frame that
        decodes and disagrees is evidence the RTC is bad, and only that earns
        the bounded extension.
        """
        await asyncio.sleep(CLOCK_COMMIT_SETTLE)
        frame = await transport.read(r.REG_CLOCK, r.CLOCK_WORD_COUNT)

        if pre_frame is not None and tuple(frame) == pre_frame:
            # The RTC ticks unconditionally, so a byte-identical frame cannot be
            # a fresh read of a changed clock â€” the logger served the same cached
            # entry across the write. Hardware says the cache does invalidate on
            # a write (32 post-commit frames, two runs, all fresh), so this
            # should never fire; if it ever does, the read-back is meaningless
            # and must not be judged.
            #
            # A benign false positive is possible in principle: if the clock
            # happened to be running exactly CLOCK_COMMIT_SETTLE ahead, the
            # post-write frame could match the pre-write one. That retries, which
            # costs an attempt and nothing else.
            _LOGGER.warning(
                "inverter clock unverified on attempt %d/%d: the read-back is "
                "byte-identical to the pre-write frame, so the logger served a "
                "cached value and it cannot be judged",
                attempt, CLOCK_SYNC_ATTEMPTS,
            )
            return VERIFY_UNVERIFIED

        if r.decode_clock(frame) is None:
            _LOGGER.warning(
                "inverter clock unverified on attempt %d/%d: read back an "
                "undecodable frame", attempt, CLOCK_SYNC_ATTEMPTS,
            )
            return VERIFY_UNVERIFIED

        if not clock_within_tolerance(frame, dt_util.now(), CLOCK_SYNC_TOLERANCE):
            _LOGGER.warning(
                "inverter clock did not take on attempt %d/%d: read back %s",
                attempt, CLOCK_SYNC_ATTEMPTS, r.decode_clock(frame),
            )
            return VERIFY_WRONG

        return VERIFY_OK

    async def _enable_time_sync(self, ble_device, pre_flags: int | None) -> Exception | None:
        """Leave Time Sync ON, keeping every other bit of 0x00E4 as found.

        Always ON, never "as it was": see :meth:`async_sync_clock`. This is a
        rising edge, which does not latch, so it cannot commit a half-written
        clock â€” that is precisely why the policy is safe.

        NEVER raises: it is called from a finally during unwinding, so raising
        would replace whatever is already propagating (including a
        CancelledError) and skip the outcome record.

        On cancellation the write is handed to a task that ACQUIRES THE BLE LOCK
        ITSELF. An earlier version shielded the write instead, which kept it
        alive but let it run outside the lock â€” and the logger accepts a single
        central, so a background write could collide with a poll or a later
        sync, with its exception observed by nobody. A write that escapes the
        lock is not a smaller problem than one that dies; it is a less visible
        one.
        """
        if pre_flags is None:
            return None  # never read the flag, so never touched it

        try:
            await self._write_time_sync_on(ble_device, pre_flags)
            return None
        except asyncio.CancelledError as exc:
            _LOGGER.warning(
                "cancelled while re-enabling Time Sync â€” retrying it after the "
                "BLE lock is released"
            )
            self._pending_enable = asyncio.ensure_future(
                self._enable_after_release(ble_device, pre_flags)
            )
            return exc
        except Exception as exc:  # noqa: BLE001 â€” reported, never masked
            _LOGGER.error(
                "could not re-enable Time Sync (0x%04X) â€” cloud clock "
                "calibration is left disabled: %s", pre_flags, exc,
            )
            return exc

    async def _enable_after_release(self, ble_device, pre_flags: int) -> None:
        """Re-enable Time Sync once the sequence's lock is free.

        Takes the lock rather than racing it, so the single-central rule still
        holds, and publishes what happened so a cancelled sync does not leave
        cloud calibration disabled with nobody the wiser.
        """
        async with self._ble_lock:
            try:
                await self._write_time_sync_on(ble_device, pre_flags)
            except Exception:  # noqa: BLE001 â€” last chance, must be reported
                _LOGGER.error(
                    "Time Sync could NOT be re-enabled after a cancelled sync â€” "
                    "cloud clock calibration is left DISABLED", exc_info=True,
                )
                self._record_sync_outcome(
                    SYNC_FAILED,
                    int(self._clock_sync_state.get("clock_sync_attempts", 0)),
                )
            else:
                _LOGGER.warning("Time Sync re-enabled after a cancelled sync")

    async def _write_time_sync_on(self, ble_device, pre_flags: int) -> None:
        """The re-enable write itself, in its own session (see the caller)."""
        async with self._session(ble_device) as transport:
            await transport.handshake()
            await transport.write(
                r.REG_TIME_SYNC, r.set_flag(pre_flags, r.TIME_SYNC_MASK, True)
            )

    def _record_sync_outcome(self, result: str, attempts: int) -> None:
        """Publish the sync result as fact, for anything that verifies it.

        The drift sensor cannot serve this purpose: it is a snapshot that is
        carried forward when a config read fails, so it cannot distinguish a
        verified sync from a stale reading â€” an automation reading it can alert
        after a good sync and stay silent after a bad one.
        """
        # The identity of THIS sync, and the only field a verifier should
        # compare. Timestamps cannot serve that purpose here: both sides would
        # come from the wall clock, and an NTP or manual step during the call
        # can make a fresh result look older than the run that produced it â€”
        # in a feature whose entire subject is clock correction, assuming a
        # monotonic wall clock is the one assumption not available to us.
        self._clock_sync_seq += 1
        self._clock_sync_state = {
            "clock_sync_id": self._clock_sync_seq,
            "clock_sync_result": result,
            "clock_sync_attempts": attempts,
            # Informational only â€” for humans reading the entity, never the
            # basis of a pass/fail decision.
            "clock_sync_at": dt_util.now(),
        }
        # Published HERE, not on the next successful poll. The result is known
        # now, and making its visibility contingent on a later read means a poll
        # failure can bury a verified sync â€” including the escalation path,
        # where _handle_poll_failure raises UpdateFailed and stamps nothing at
        # all. Listeners are notified directly rather than via
        # async_set_updated_data, which would also reset the refresh schedule.
        self.data = self._with_sync_outcome(self.data)
        self.async_update_listeners()

    async def _write_regs(self, regs: dict[int, int]) -> None:
        """Write one or more registers over BLE, retrying transient failures.

        A flaky link can drop a connection or time out mid-write; each attempt
        opens a fresh session and re-issues every register, confirming with a
        read-back. A *readback mismatch* (the inverter clamped/rejected the
        value) and a *missing device* are not transient â€” they surface at once
        rather than burning the retry budget. The value is tracked for drift
        detection only after the whole batch has verified.
        """
        # Dry-run is the safety default: never touch the radio, just log intent.
        # Checked first so it works even when the device is momentarily absent.
        if self._dry_run:
            for reg, value in regs.items():
                _LOGGER.warning(
                    "Inverter write blocked (Safety Lock ACTIVE / Dry-Run): Would write reg 0x%04X = %d", reg, value
                )
            raise HomeAssistantError(
                "Inverter write blocked! Turn ON 'Allow Inverter Writes' safety switch before modifying settings."
            )

        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(f"BLE device {self._address} not found")

        for attempt in range(1, self._write_attempts + 1):
            try:
                # Serialize against the poll â€” one BLE central at a time.
                async with self._ble_lock:
                    async with self._session(ble_device) as transport:
                        await transport.handshake()
                        for reg, value in regs.items():
                            await transport.write(reg, value)
                            readback = await transport.read(reg, 1)
                            verify_readback(reg, value, readback[0])
                break
            except ValueError:
                # Read-back mismatch â€” a retry would only mask an inverter clamp.
                raise
            except Exception as exc:
                if attempt >= self._write_attempts:
                    _LOGGER.warning(
                        "BLE write failed after %d attempt(s): %s", attempt, exc
                    )
                    raise
                _LOGGER.info(
                    "BLE write attempt %d/%d failed, retrying: %s",
                    attempt, self._write_attempts, exc,
                )
                if self._write_backoff:
                    await asyncio.sleep(self._write_backoff * attempt)

        # Track for drift detection only once the batch has fully verified.
        for reg, value in regs.items():
            self._tracked_values[reg] = value


