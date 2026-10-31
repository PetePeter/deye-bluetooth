"""The Sync Clock button.

Drives the real button entity against the real coordinator and the FakeInverter
from test_clock_coordinator — no mocks, and no re-implementation of the sync
sequence, which is already covered there. What is tested here is only what the
button itself adds: that pressing it runs an unconditional sync, that a failure
reaches the caller, and that the drift entity is not left stale afterwards.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.deye_ble import const
from custom_components.deye_ble.button import DeyeSyncClockButton
from tests.test_clock_coordinator import (
    NOW,
    SYNC_ON,
    FakeInverter,
    VirtualClock,
    _install_virtual_clock,
    _make_coordinator,
    _target_after,
)

LOGGER_SN = "D25618391540"


class FakeEntry:
    """Just the two fields the entity reads off a ConfigEntry."""

    def __init__(self, sn: str = LOGGER_SN):
        self.data = {const.CONF_LOGGER_SN: sn}


def _button(fake, *, read_only: bool = False):
    coordinator = _make_coordinator(fake, read_only=read_only)
    return DeyeSyncClockButton(coordinator, FakeEntry()), coordinator


@pytest.mark.asyncio
async def test_pressing_the_button_sets_the_clock(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    button, _ = _button(fake)

    await button.async_press()

    assert fake.latched == _target_after(const.CLOCK_SYNC_SETTLE)


@pytest.mark.asyncio
async def test_the_button_writes_even_when_the_drift_is_tiny(monkeypatch):
    """The button is "set it now", not "set it if it looks worth it".

    The DISCRIMINATING case: an RTC already inside the daily automation's
    threshold. Wired to the thresholded path this press would report skipped and
    write nothing, which is the wrong answer for a human standing at the
    inverter who has just decided the clock needs setting.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    # RTC one second out — far inside any threshold the automation would use.
    rtc = NOW.replace(tzinfo=None).replace(microsecond=0)
    fake = FakeInverter(clock, flags=SYNC_ON, rtc=rtc)
    button, coordinator = _button(fake)

    await button.async_press()

    assert fake.commits == 1
    assert coordinator.data["clock_sync_result"] == const.SYNC_OK


@pytest.mark.asyncio
async def test_a_failed_sync_reaches_the_person_who_pressed(monkeypatch):
    """A press that silently swallows the failure is the bug this feature is for.

    Raising is what puts the error in front of the user; returning quietly
    leaves a wrong clock looking like a successful button press.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, never_commits=True)
    button, _ = _button(fake)

    with pytest.raises(HomeAssistantError):
        await button.async_press()


@pytest.mark.asyncio
async def test_the_press_leaves_the_drift_entity_post_sync(monkeypatch):
    """Press, then look at the drift sensor — it must show what you just did.

    Config registers are only re-read every CONFIG_READ_INTERVAL (15 min), so
    without the refresh the drift sensor keeps showing the pre-sync value long
    after the clock is right. The discriminating assertion is the drift AFTER
    the press: the RTC starts four minutes out, so a stale snapshot reads -240.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    button, coordinator = _button(fake)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.data["inverter_clock_drift"] == -240  # pre-sync snapshot

    await button.async_press()

    assert abs(coordinator.data["inverter_clock_drift"]) <= const.CLOCK_SYNC_TOLERANCE


@pytest.mark.asyncio
async def test_the_button_identity_is_stable(monkeypatch):
    """The entity_id ends up on a dashboard and in automations — pin it.

    Both halves matter: the unique_id is what fixes the entity_id after the
    first setup, and the device identifiers are what keep the button on the same
    device as the clock sensors rather than orphaned on a second one.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    button, _ = _button(FakeInverter(clock, flags=SYNC_ON))

    assert button.unique_id == f"{LOGGER_SN}_sync_clock"
    assert button.device_info["identifiers"] == {(const.DOMAIN, LOGGER_SN)}


@pytest.mark.asyncio
async def test_a_read_only_press_is_refused_and_touches_nothing(monkeypatch):
    """The read-only lock is the safety the whole integration is built around.

    A press is the most direct write a user can issue, so it is the one most
    likely to be tried while the lock is still on during setup. It must say
    so in the UI rather than appear to have worked.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    button, _ = _button(fake, read_only=True)

    with pytest.raises(HomeAssistantError, match="Read Only"):
        await button.async_press()

    assert fake.writes == []
    assert fake.latched is None
