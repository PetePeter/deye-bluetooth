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

from .const import (
    CONF_ADDRESS,
    CONF_DRY_RUN,
    CONF_LOGGER_SN,
    CONF_REASSERT,
    CONF_SCAN_INTERVAL,
    DEFAULT_DRY_RUN,
    DEFAULT_REASSERT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
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

    @staticmethod
    def async_get_options_flow(config_entry):
        return DeyeBleOptionsFlowHandler()

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

        return await self._finish_or_confirm()

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
                    return await self._finish_or_confirm()

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

    def _create_entry(self, sn: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._discovered_name or sn,
            data={
                CONF_ADDRESS: self._discovered_address,
                CONF_LOGGER_SN: sn,
            },
        )

    async def _finish_or_confirm(self) -> ConfigFlowResult:
        """Skip the SN step when the advertised name is already a valid serial.

        Deye loggers advertise their serial as the BLE name, so in the normal
        case the user has nothing to type. Fall back to the confirm form only
        when the advert name isn't a usable SN.
        """
        try:
            sn = validate_logger_sn(self._discovered_name or "")
        except ValueError:
            return await self.async_step_confirm()
        return self._create_entry(sn)

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
                return self._create_entry(sn)

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


class DeyeBleOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for scan interval, dry-run and reassert."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        schema = vol.Schema({
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
            vol.Required(
                CONF_DRY_RUN,
                default=current.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
            ): bool,
            vol.Required(
                CONF_REASSERT,
                default=current.get(CONF_REASSERT, DEFAULT_REASSERT),
            ): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
