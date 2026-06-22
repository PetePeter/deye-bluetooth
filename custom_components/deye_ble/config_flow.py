"""Config flow for Deye BLE integration.

BLE discovery via service UUID 00000922-..., plus a manual confirmation
step where the user enters the logger serial number.  unique_id = logger SN.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_ADDRESS, CONF_LOGGER_SN, DOMAIN
from .helpers import validate_logger_sn

_LOGGER = logging.getLogger(__name__)

# Bluetooth service UUID for Deye loggers.
# The BLE device name is typically the logger serial number.
SERVICE_UUID = "00000922-0000-1000-8000-00805f9b34fb"


def _deye_devices(hass):
    """Discovered BLE devices advertising the Deye logger service UUID.

    ``async_discovered_service_info``'s second positional arg is ``connectable``
    (a bool), NOT a service-UUID filter — so we filter on service_uuids here.
    """
    return [
        info
        for info in async_discovered_service_info(hass)
        if SERVICE_UUID in info.service_uuids
    ]


class DeyeBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deye BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery."""
        _LOGGER.debug("BLE discovery: %s", discovery_info.name)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}

        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Handle manual user step — pick from discovered devices or abort."""
        if user_input is not None:
            # User selected a discovered device
            address = user_input[CONF_ADDRESS]
            for info in _deye_devices(self.hass):
                if info.address == address:
                    # The BLE MAC is the stable per-device identity for the
                    # config entry (HA Bluetooth convention). The logger SN is
                    # captured separately and drives the P4 entity unique_ids
                    # (<sn>_<key>) for drop-in parity with the cloud bridge.
                    await self.async_set_unique_id(address)
                    self._abort_if_unique_id_configured()
                    self._discovered_address = address
                    self._discovered_name = info.name
                    return await self.async_step_confirm()

        # Build list of discovered BLE devices matching our service UUID.
        discovered = _deye_devices(self.hass)
        if not discovered:
            return self.async_abort(reason="no_devices_found")

        addresses = sorted({d.address for d in discovered})
        schema = vol.Schema({
            vol.Required(CONF_ADDRESS): vol.In(
                {a: format_mac(a) for a in addresses}
            ),
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
        )

    async def async_step_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Confirm / enter logger serial number."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                sn = validate_logger_sn(user_input[CONF_LOGGER_SN])
            except ValueError as exc:
                errors[CONF_LOGGER_SN] = str(exc)
            else:
                return self.async_create_entry(
                    title=self._discovered_name or sn,
                    data={
                        CONF_ADDRESS: self._discovered_address,
                        CONF_LOGGER_SN: sn,
                    },
                )

        schema = vol.Schema({
            vol.Required(
                CONF_LOGGER_SN,
                default=self._discovered_name or "",
            ): str,
        })
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": self._discovered_name or "Unknown",
            },
        )
