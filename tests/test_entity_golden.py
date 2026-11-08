"""Pin the user-visible Home Assistant entity surface for every profile."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble import PLATFORMS, const, models, protocol
from custom_components.deye_ble.coordinator import DeyeBleCoordinator
from custom_components.deye_ble.transport import DeyeBleTransport
from tests.test_registers import FRAMES
from tests.test_single_phase import CONFIG as SINGLE_PHASE_CONFIG
from tests.test_single_phase import TELEMETRY as SINGLE_PHASE_TELEMETRY
from tests.test_string import CONFIG as STRING_CONFIG
from tests.test_string import TELEMETRY as STRING_TELEMETRY


LOGGER_SN = "P1196-GOLDEN"
_PLATFORM_MODULES = {
    platform: importlib.import_module(f"custom_components.deye_ble.{platform}")
    for platform in PLATFORMS
}


def _three_phase_data() -> dict:
    words = {
        address: protocol.parse_read(frame)
        for address, frame in FRAMES.items()
    }
    return models.decode(words)


def _single_phase_data() -> dict:
    words = {**SINGLE_PHASE_TELEMETRY, **SINGLE_PHASE_CONFIG}
    return models.decode_single_phase(words)


def _string_data() -> dict:
    words = {**STRING_TELEMETRY, **STRING_CONFIG}
    return models.decode_string(words)


def _metadata_value(value):
    return getattr(value, "value", value)


async def _snapshot(profile: str, decoded_data: dict) -> list[dict]:
    coordinator = DeyeBleCoordinator(
        hass=None,
        address="00:00:00:00:00:00",
        transport_factory=DeyeBleTransport,
        profile=profile,
    )
    coordinator.data = decoded_data

    entry = SimpleNamespace(
        data={const.CONF_LOGGER_SN: LOGGER_SN},
        options={},
        entry_id="p1196-golden",
    )
    hass = SimpleNamespace(data={const.DOMAIN: {entry.entry_id: coordinator}})
    rows = []

    for platform in PLATFORMS:
        entities = []
        await _PLATFORM_MODULES[platform].async_setup_entry(
            hass, entry, entities.extend,
        )
        for entity in entities:
            rows.append({
                "platform": platform,
                "unique_id": entity.unique_id,
                "name": entity.name,
                "unit": _metadata_value(
                    getattr(entity, "native_unit_of_measurement", None)
                ),
                "device_class": _metadata_value(
                    getattr(entity, "device_class", None)
                ),
                "entity_category": _metadata_value(
                    getattr(entity, "entity_category", None)
                ),
            })

    return sorted(
        rows,
        key=lambda row: tuple(
            "" if row[field] is None else str(row[field])
            for field in (
                "platform", "unique_id", "name", "unit",
                "device_class", "entity_category",
            )
        ),
    )


@pytest.mark.parametrize(
    ("profile_name", "profile", "decode"),
    [
        ("three_phase", models.PROFILE_THREE_PHASE, _three_phase_data),
        ("single_phase", models.PROFILE_SINGLE_PHASE, _single_phase_data),
        ("string", models.PROFILE_STRING, _string_data),
    ],
    ids=("three_phase", "single_phase", "string"),
)
@pytest.mark.asyncio
async def test_profile_entity_metadata_matches_golden(profile_name, profile, decode):
    actual = await _snapshot(profile, decode())
    golden_path = Path(__file__).parent / "golden" / f"{profile_name}.json"

    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return

    assert actual == json.loads(golden_path.read_text(encoding="utf-8"))
