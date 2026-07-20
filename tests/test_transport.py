"""Transport resilience tests — the disconnect must never wedge the BLE lock.

A hung ``BleakClient.disconnect()`` on a flaky ESP32-proxy link used to hold the
coordinator's ``_ble_lock`` forever (the ``async with transport`` teardown never
returned), leaving every entity ``unavailable`` until HA restarted. These tests
pin the bounded-teardown contract that prevents that wedge.

Fakes ``establish_connection`` so no radio/bleak backend is needed.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.deye_ble import transport as t


class FakeBleakClient:
    """Minimal BleakClient stand-in with controllable teardown behaviour."""

    def __init__(self, *, disconnect_hangs: bool = False):
        self._disconnect_hangs = disconnect_hangs
        self.start_notify_called = False
        self.stop_notify_called = False
        self.disconnect_called = False

    async def start_notify(self, _char, _cb) -> None:
        self.start_notify_called = True

    async def stop_notify(self, _char) -> None:
        self.stop_notify_called = True

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self._disconnect_hangs:
            await asyncio.Event().wait()  # never resolves


@pytest.fixture
def patch_connect(monkeypatch):
    """Return a helper that wires a given FakeBleakClient into connect()."""

    def _install(client: FakeBleakClient) -> None:
        async def _fake_establish(_cls, _device, _name, max_attempts=0):
            return client

        monkeypatch.setattr(t, "establish_connection", _fake_establish)

    return _install


@pytest.mark.asyncio
async def test_disconnect_returns_when_client_disconnect_hangs(patch_connect):
    # The wedge fix: a disconnect that never completes must not block the caller
    # (and therefore must not hold the coordinator's BLE lock) indefinitely.
    client = FakeBleakClient(disconnect_hangs=True)
    patch_connect(client)

    transport = DeyeBleTransport_with_short_timeout(client_timeout=0.05)
    await transport.connect()

    await asyncio.wait_for(transport.disconnect(), timeout=1.0)

    assert client.disconnect_called is True
    assert transport._client is None  # cleared even though the disconnect hung


@pytest.mark.asyncio
async def test_disconnect_calls_client_disconnect_on_happy_path(patch_connect):
    # The timeout wrapper must not skip the real teardown on a healthy link.
    client = FakeBleakClient()
    patch_connect(client)

    transport = t.DeyeBleTransport(ble_device=object())
    await transport.connect()
    await transport.disconnect()

    assert client.stop_notify_called is True
    assert client.disconnect_called is True
    assert transport._client is None


def DeyeBleTransport_with_short_timeout(*, client_timeout: float):
    """Build a transport whose disconnect timeout is short, so the hang test is
    fast. Kept as a helper so the production default stays untouched."""
    transport = t.DeyeBleTransport(ble_device=object())
    transport._disconnect_timeout = client_timeout
    return transport
