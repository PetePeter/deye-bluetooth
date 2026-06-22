"""Constants for the Deye BLE integration."""
from __future__ import annotations

DOMAIN = "deye_ble"

CONF_LOGGER_SN = "logger_sn"
CONF_ADDRESS = "address"
CONF_DRY_RUN = "dry_run"
CONF_REASSERT = "reassert"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_DRY_RUN = True   # writes blocked until user explicitly enables
DEFAULT_REASSERT = False  # local-wins reassert is opt-in

DEFAULT_SCAN_INTERVAL = 60            # seconds — telemetry poll (user-configurable)
MIN_SCAN_INTERVAL = 30               # floor to protect BLE reconnect stability
MAX_SCAN_INTERVAL = 600
CONFIG_READ_INTERVAL = 900           # seconds — work_mode + max_sell re-read

DEVICE_NAME = "Deye Inverter (BLE)"  # coexistence with deyecloud "Deye Inverter"

# Re-export for convenience
from .registers import WORK_MODE_LABELS  # noqa: F401
