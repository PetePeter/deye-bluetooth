# Single-phase hybrid monitoring

Tested over BLE on SUN-6K-SG04LP1-EU, protocol 2.1, with a logger reporting
+ok=21511,21511. Two live captures confirmed the read framing and sensor values.

The logger's DTYPE reply does not identify the inverter's register map.
The transport reads inverter identification registers 0-2 and selects the
single-phase profile for device type 0x003. Other types retain the existing map.

The profile reads battery, solar, load, grid, temperature, energy, and inverter
clock values. Cumulative energy uses both words of each 32-bit total; grid-import
words are non-contiguous. Consumption Today uses the inverter's daily register.

This profile is monitoring only. Existing control addresses overlap telemetry
on this inverter, so control platforms are not loaded, coordinator write/clock
services reject control operations, and the transport also blocks single and
block writes after identifying the inverter. Raw commands on an identified
single-phase session are limited to identification and valid function-03 reads.
Dry Run/Reassert options cannot enable controls for this profile.

Unsupported L2/L3, BMS-limit, UPS, and configuration sensors are not created.
The existing grid-connection sensor continues to infer grid presence from
voltage; fast outage polling and event notifications are outside this change.

No diagnostic runner, hardware-global read-only override, manifest version bump,
or extra handshake reply is included in this change. The legacy profile's
control behavior is retained. Register fixtures contain no BLE identifiers or
inverter serial numbers.

Run unit tests in the repository virtual environment:

    python -m pytest -q tests/test_single_phase.py tests/test_transport.py
    python -m pytest -q

These tests use captures and fake transports. Home Assistant-dependent tests
are skipped when HA is not installed. Live HA setup, polling and reload testing
remain necessary before an upstream PR is considered complete.
