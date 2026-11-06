"""Deye Cloud (micro-onduleur) - Intégration Home Assistant.

Deux sources réunies dans un seul appareil :
  - Deye Cloud (API officielle) : production du jour / mois / année / totale, état ;
  - en local (facultatif) : le boîtier Wi-Fi du Deye, lu toutes les 10 s comme Solarman.
"""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DeyeCloudAPI
from .const import (
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_HOST,
    CONF_PORT,
    CONF_REGION,
    CONF_SERIAL,
    CONF_STATION_ID,
    DEFAULT_PORT,
    REGIONS,
)
from .coordinator import DeyeCloudConfigEntry, DeyeCloudCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


def creer_api(hass: HomeAssistant, data: dict) -> DeyeCloudAPI:
    return DeyeCloudAPI(
        REGIONS[data.get(CONF_REGION, "eu")],
        data[CONF_APP_ID],
        data[CONF_APP_SECRET],
        data[CONF_EMAIL],
        data[CONF_PASSWORD],
        async_get_clientsession(hass),
    )


def reglages_locaux(entry: DeyeCloudConfigEntry) -> dict:
    """Connexion locale : dans les options (modifiables) sinon dans les données d'origine."""
    reglages = {**entry.data, **entry.options}
    return reglages if reglages.get(CONF_HOST) and reglages.get(CONF_SERIAL) else {}


async def async_setup_entry(hass: HomeAssistant, entry: DeyeCloudConfigEntry) -> bool:
    """Configurer l'intégration."""
    coordinator = DeyeCloudCoordinator(
        hass, entry, creer_api(hass, dict(entry.data)), int(entry.data[CONF_STATION_ID])
    )
    await coordinator.async_config_entry_first_refresh()

    if local := reglages_locaux(entry):
        from .local import DeyeLocalClient, DeyeLocalCoordinator

        client = DeyeLocalClient(
            str(local[CONF_HOST]).strip(), int(local.get(CONF_PORT) or DEFAULT_PORT), str(local[CONF_SERIAL])
        )
        coordinator.local = DeyeLocalCoordinator(hass, entry, client)
        # Pas d'échec si le boîtier ne répond pas (la nuit il est éteint) : le cloud prend le relais
        await coordinator.local.async_refresh()
        entry.async_on_unload(client.fermer)

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_options_modifiees))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_modifiees(hass: HomeAssistant, entry: DeyeCloudConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DeyeCloudConfigEntry) -> bool:
    """Décharger l'intégration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
