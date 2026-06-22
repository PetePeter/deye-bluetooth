"""Validate integration metadata files parse correctly and contain required keys.

Catches CI-breaking issues (missing keys, bad JSON) before hassfest/HACS run.
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "deye_ble"


def _load(path: pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestManifest:
    def test_manifest_is_valid_json(self):
        data = _load(COMPONENT / "manifest.json")
        assert data["domain"] == "deye_ble"
        assert data["name"] == "Deye Bluetooth (Local)"
        assert data["codeowners"]
        assert data["config_flow"] is True
        assert data["iot_class"] == "local_polling"
        assert data["version"]

    def test_manifest_has_bluetooth_service(self):
        data = _load(COMPONENT / "manifest.json")
        ble = data.get("bluetooth")
        assert ble, "manifest.json must have a 'bluetooth' list"
        assert any(
            entry.get("service_uuid") for entry in ble
        ), "bluetooth entries must include service_uuid"


class TestHacsJson:
    def test_hacs_is_valid_json(self):
        data = _load(ROOT / "hacs.json")
        assert data["name"] == "Deye Bluetooth (Local)"
        assert data["render_readme"] is True
        assert data["homeassistant"]
