"""The bluetooth integration."""
from __future__ import annotations

import datetime
import logging
import platform
from typing import TYPE_CHECKING

from awesomeversion import AwesomeVersion
from bluetooth_adapters import (
    ADAPTER_ADDRESS,
    ADAPTER_HW_VERSION,
    ADAPTER_SW_VERSION,
    DEFAULT_ADDRESS,
    AdapterDetails,
    adapter_human_name,
    adapter_unique_name,
    get_adapters,
)
from home_assistant_bluetooth import BluetoothServiceInfo, BluetoothServiceInfoBleak

from homeassistant.components import usb
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_INTEGRATION_DISCOVERY,
    ConfigEntry,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback as hass_callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, discovery_flow
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.loader import async_get_bluetooth

from . import models
from .api import (
    _get_manager,
    async_address_present,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_get_advertisement_callback,
    async_get_scanner,
    async_last_service_info,
    async_process_advertisements,
    async_rediscover_address,
    async_register_callback,
    async_register_scanner,
    async_scanner_count,
    async_track_unavailable,
)
from .base_scanner import BaseHaRemoteScanner, BaseHaScanner
from .const import (
    BLUETOOTH_DISCOVERY_COOLDOWN_SECONDS,
    CONF_ADAPTER,
    CONF_DETAILS,
    CONF_PASSIVE,
    DATA_MANAGER,
    DOMAIN,
    FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS,
    LINUX_FIRMWARE_LOAD_FALLBACK_SECONDS,
    SOURCE_LOCAL,
)
from .manager import BluetoothManager
from .match import BluetoothCallbackMatcher, IntegrationMatcher
from .models import BluetoothCallback, BluetoothChange, BluetoothScanningMode
from .scanner import HaScanner, ScannerStartError
from .wrappers import HaBluetoothConnector

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

__all__ = [
    "async_address_present",
    "async_ble_device_from_address",
    "async_discovered_service_info",
    "async_get_scanner",
    "async_last_service_info",
    "async_process_advertisements",
    "async_rediscover_address",
    "async_register_callback",
    "async_register_scanner",
    "async_track_unavailable",
    "async_scanner_count",
    "BaseHaScanner",
    "BaseHaRemoteScanner",
    "BluetoothCallbackMatcher",
    "BluetoothChange",
    "BluetoothServiceInfo",
    "BluetoothServiceInfoBleak",
    "BluetoothScanningMode",
    "BluetoothCallback",
    "HaBluetoothConnector",
    "SOURCE_LOCAL",
    "FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS",
]

_LOGGER = logging.getLogger(__name__)

RECOMMENDED_MIN_HAOS_VERSION = AwesomeVersion("9.0.dev0")


async def _async_get_adapter_from_address(
    hass: HomeAssistant, address: str
) -> str | None:
    """Get an adapter by the address."""
    return await _get_manager(hass).async_get_adapter_from_address(address)


@hass_callback
def _async_haos_is_new_enough(hass: HomeAssistant) -> bool:
    """Check if the version of Home Assistant Operating System is new enough."""
    # Only warn if a USB adapter is plugged in
    if not any(
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.source != SOURCE_IGNORE
    ):
        return True
    if (
        not hass.components.hassio.is_hassio()
        or not (os_info := hass.components.hassio.get_os_info())
        or not (haos_version := os_info.get("version"))
        or AwesomeVersion(haos_version) >= RECOMMENDED_MIN_HAOS_VERSION
    ):
        return True
    return False


@hass_callback
def _async_check_haos(hass: HomeAssistant) -> None:
    """Create or delete an the haos_outdated issue."""
    if _async_haos_is_new_enough(hass):
        async_delete_issue(hass, DOMAIN, "haos_outdated")
        return
    async_create_issue(
        hass,
        DOMAIN,
        "haos_outdated",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        learn_more_url="/config/updates",
        translation_key="haos_outdated",
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the bluetooth integration."""
    integration_matcher = IntegrationMatcher(await async_get_bluetooth(hass))
    integration_matcher.async_setup()
    bluetooth_adapters = get_adapters()
    manager = BluetoothManager(hass, integration_matcher, bluetooth_adapters)
    await manager.async_setup()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, manager.async_stop)
    hass.data[DATA_MANAGER] = models.MANAGER = manager
    adapters = await manager.async_get_bluetooth_adapters()

    async_migrate_entries(hass, adapters, bluetooth_adapters.default_adapter)
    await async_discover_adapters(hass, adapters)

    async def _async_rediscover_adapters() -> None:
        """Rediscover adapters when a new one may be available."""
        discovered_adapters = await manager.async_get_bluetooth_adapters(cached=False)
        _LOGGER.debug("Rediscovered adapters: %s", discovered_adapters)
        await async_discover_adapters(hass, discovered_adapters)

    discovery_debouncer = Debouncer(
        hass,
        _LOGGER,
        cooldown=BLUETOOTH_DISCOVERY_COOLDOWN_SECONDS,
        immediate=False,
        function=_async_rediscover_adapters,
    )

    async def _async_call_debouncer(now: datetime.datetime) -> None:
        """Call the debouncer at a later time."""
        await discovery_debouncer.async_call()

    def _async_trigger_discovery() -> None:
        # There are so many bluetooth adapter models that
        # we check the bus whenever a usb device is plugged in
        # to see if it is a bluetooth adapter since we can't
        # tell if the device is a bluetooth adapter or if its
        # actually supported unless we ask DBus if its now
        # present.
        _LOGGER.debug("Triggering bluetooth usb discovery")
        hass.async_create_task(discovery_debouncer.async_call())
        # Because it can take 120s for the firmware loader
        # fallback to timeout we need to wait that plus
        # the debounce time to ensure we do not miss the
        # adapter becoming available to DBus since otherwise
        # we will never see the new adapter until
        # Home Assistant is restarted
        async_call_later(
            hass,
            BLUETOOTH_DISCOVERY_COOLDOWN_SECONDS + LINUX_FIRMWARE_LOAD_FALLBACK_SECONDS,
            _async_call_debouncer,
        )

    cancel = usb.async_register_scan_request_callback(hass, _async_trigger_discovery)
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, hass_callback(lambda event: cancel())
    )

    # Wait to check until after start to make sure
    # that the system info is available.
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STARTED,
        hass_callback(lambda event: _async_check_haos(hass)),
    )
    return True


@hass_callback
def async_migrate_entries(
    hass: HomeAssistant, adapters: dict[str, AdapterDetails], default_adapter: str
) -> None:
    """Migrate config entries to support multiple."""
    current_entries = hass.config_entries.async_entries(DOMAIN)

    for entry in current_entries:
        if entry.unique_id:
            continue

        address = DEFAULT_ADDRESS
        adapter = entry.options.get(CONF_ADAPTER, default_adapter)
        if adapter in adapters:
            address = adapters[adapter][ADAPTER_ADDRESS]
        hass.config_entries.async_update_entry(
            entry, title=adapter_unique_name(adapter, address), unique_id=address
        )


async def async_discover_adapters(
    hass: HomeAssistant,
    adapters: dict[str, AdapterDetails],
) -> None:
    """Discover adapters and start flows."""
    if platform.system() == "Windows":
        # We currently do not have a good way to detect if a bluetooth device is
        # available on Windows. We will just assume that it is not unless they
        # actively add it.
        return

    for adapter, details in adapters.items():
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={CONF_ADAPTER: adapter, CONF_DETAILS: details},
        )


async def async_update_device(
    hass: HomeAssistant, entry: ConfigEntry, adapter: str
) -> None:
    """Update device registry entry.

    The physical adapter can change from hci0/hci1 on reboot
    or if the user moves around the usb sticks so we need to
    update the device with the new location so they can
    figure out where the adapter is.
    """
    manager: BluetoothManager = hass.data[DATA_MANAGER]
    adapters = await manager.async_get_bluetooth_adapters()
    details = adapters[adapter]
    registry = dr.async_get(manager.hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        name=adapter_human_name(adapter, details[ADAPTER_ADDRESS]),
        connections={(dr.CONNECTION_BLUETOOTH, details[ADAPTER_ADDRESS])},
        sw_version=details.get(ADAPTER_SW_VERSION),
        hw_version=details.get(ADAPTER_HW_VERSION),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry for a bluetooth scanner."""
    address = entry.unique_id
    assert address is not None
    adapter = await _async_get_adapter_from_address(hass, address)
    if adapter is None:
        raise ConfigEntryNotReady(
            f"Bluetooth adapter {adapter} with address {address} not found"
        )

    passive = entry.options.get(CONF_PASSIVE)
    mode = BluetoothScanningMode.PASSIVE if passive else BluetoothScanningMode.ACTIVE
    new_info_callback = async_get_advertisement_callback(hass)
    scanner = HaScanner(hass, mode, adapter, address, new_info_callback)
    try:
        scanner.async_setup()
    except RuntimeError as err:
        raise ConfigEntryNotReady(
            f"{adapter_human_name(adapter, address)}: {err}"
        ) from err
    try:
        await scanner.async_start()
    except ScannerStartError as err:
        raise ConfigEntryNotReady from err
    entry.async_on_unload(async_register_scanner(hass, scanner, True))
    await async_update_device(hass, entry, adapter)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = scanner
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    scanner: HaScanner = hass.data[DOMAIN].pop(entry.entry_id)
    await scanner.async_stop()
    return True
