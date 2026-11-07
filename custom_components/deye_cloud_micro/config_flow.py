"""Config flow de l'intégration Deye Cloud (micro-onduleur)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import creer_api
from .api import DeyeCloudAuthError, DeyeCloudConnectionError
from .const import (
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_HOST,
    CONF_PORT,
    CONF_REGION,
    CONF_SERIAL,
    CONF_STATION_ID,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
)

MOT_DE_PASSE = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
CONF_SANS_TEST = "sans_test"


def _schema(defaut: Mapping[str, Any] | None = None) -> vol.Schema:
    d = defaut or {}
    return vol.Schema(
        {
            vol.Required(CONF_REGION, default=d.get(CONF_REGION, "eu")): SelectSelector(
                SelectSelectorConfig(
                    options=["eu", "us"], translation_key="region", mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_APP_ID, default=d.get(CONF_APP_ID, "")): str,
            vol.Required(CONF_APP_SECRET): MOT_DE_PASSE,
            vol.Required(CONF_EMAIL, default=d.get(CONF_EMAIL, "")): str,
            vol.Required(CONF_PASSWORD): MOT_DE_PASSE,
        }
    )


def _schema_local(d: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_HOST, description={"suggested_value": d.get(CONF_HOST, "")}): str,
            vol.Required(CONF_PORT, default=int(d.get(CONF_PORT) or DEFAULT_PORT)): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_SERIAL, description={"suggested_value": d.get(CONF_SERIAL, "")}): str,
            vol.Required(CONF_SANS_TEST, default=False): BooleanSelector(),
        }
    )


async def valider_local(saisie: dict[str, Any], numero_cloud: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Vérifie la connexion locale. Renvoie les réglages à enregistrer et les erreurs.

    Champ IP vide → pas de connexion locale (cloud seul).
    Numéro de série vide → demandé au boîtier, sinon repris de Deye Cloud.
    """
    hote = str(saisie.get(CONF_HOST) or "").strip()
    if not hote:
        return {CONF_HOST: "", CONF_SERIAL: "", CONF_PORT: DEFAULT_PORT}, {}

    from .local import DeyeLocalClient, DeyeLocalError, detecter_numero_serie

    port = int(saisie.get(CONF_PORT) or DEFAULT_PORT)
    numero = str(saisie.get(CONF_SERIAL) or "").strip()
    if not numero:
        numero = await detecter_numero_serie(hote) or (numero_cloud or "")
    if not numero.isdigit():
        return {}, {CONF_SERIAL: "serial_required"}

    reglages = {CONF_HOST: hote, CONF_PORT: port, CONF_SERIAL: numero}
    if saisie.get(CONF_SANS_TEST):
        return reglages, {}
    client = DeyeLocalClient(hote, port, numero)
    try:
        await client.lire()
    except DeyeLocalError as err:
        LOGGER.debug("Test de la connexion locale : %s", err)
        return {}, {"base": "cannot_connect_local"}
    finally:
        await client.fermer()
    return reglages, {}


def _libelle(station: dict) -> str:
    nom = str(station.get("name") or station.get("id")).strip()
    puissance = station.get("installedCapacity")
    return f"{nom} ({puissance} kWc)" if puissance else nom


def numero_collecteur(appareils: list[dict]) -> str | None:
    """Numéro de série du boîtier Wi-Fi (« COLLECTOR ») d'après Deye Cloud."""
    for a in appareils:
        if "COLLECTOR" in str(a.get("deviceType") or "").upper() and str(a.get("deviceSn") or "").isdigit():
            return str(a["deviceSn"])
    return None


class DeyeCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ajout d'une station Deye Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._stations: list[dict] = []
        self._station: dict = {}
        self._numero_cloud: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DeyeCloudOptionsFlow()

    async def _tester(self, data: dict[str, Any]) -> tuple[list[dict], dict[str, str]]:
        api = creer_api(self.hass, data)
        try:
            await api.connect(force=True)
            return await api.get_stations(), {}
        except DeyeCloudAuthError as err:
            LOGGER.debug("Deye Cloud refuse les identifiants : %s", err)
            return [], {"base": "invalid_auth"}
        except DeyeCloudConnectionError as err:
            LOGGER.debug("Deye Cloud injoignable : %s", err)
            return [], {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            LOGGER.exception("Erreur inattendue")
            return [], {"base": "unknown"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Étape 1 : application developer.deyecloud.com et compte Deye Cloud."""
        errors: dict[str, str] = {}
        if user_input is not None:
            stations, errors = await self._tester(user_input)
            if not errors and not stations:
                errors["base"] = "no_stations"
            if not errors:
                self._data = dict(user_input)
                self._stations = stations
                if len(stations) == 1:
                    return await self._choisir(stations[0])
                return await self.async_step_station()
        return self.async_show_form(step_id="user", data_schema=_schema(user_input), errors=errors)

    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Étape 2 : choix de la station (si le compte en a plusieurs)."""
        if user_input is not None:
            station = next(s for s in self._stations if str(s["id"]) == user_input[CONF_STATION_ID])
            return await self._choisir(station)
        options = {str(s["id"]): _libelle(s) for s in self._stations}
        return self.async_show_form(
            step_id="station", data_schema=vol.Schema({vol.Required(CONF_STATION_ID): vol.In(options)})
        )

    async def _choisir(self, station: dict) -> ConfigFlowResult:
        await self.async_set_unique_id(str(station["id"]))
        self._abort_if_unique_id_configured()
        self._station = station
        try:
            appareils = await creer_api(self.hass, self._data).get_station_devices(int(station["id"]))
            self._numero_cloud = numero_collecteur(appareils)
        except (DeyeCloudAuthError, DeyeCloudConnectionError):
            self._numero_cloud = None
        return await self.async_step_local()

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Étape 3 (facultative) : lecture locale du boîtier Wi-Fi, comme Solarman."""
        errors: dict[str, str] = {}
        if user_input is not None:
            reglages, errors = await valider_local(user_input, self._numero_cloud)
            if not errors:
                station_id = str(self._station["id"])
                return self.async_create_entry(
                    title=f"Deye Cloud - {str(self._station.get('name') or station_id).strip()}",
                    data={**self._data, CONF_STATION_ID: int(station_id)},
                    options=reglages,
                )
        return self.async_show_form(
            step_id="local",
            data_schema=_schema_local(user_input or {CONF_SERIAL: self._numero_cloud or ""}),
            errors=errors,
        )

    # ------------------------------------------------------------ réauthentification
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Deye Cloud refuse les identifiants enregistrés."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, errors = await self._tester(user_input)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_schema(user_input or entry.data), errors=errors
        )


class DeyeCloudOptionsFlow(OptionsFlow):
    """Options : connexion locale (IP du boîtier Wi-Fi), modifiable à tout moment."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            coordinator = getattr(self.config_entry, "runtime_data", None)
            numero = numero_collecteur(coordinator.devices) if coordinator else None
            reglages, errors = await valider_local(user_input, numero)
            if not errors:
                return self.async_create_entry(data=reglages)
        actuel = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema_local(user_input or actuel), errors=errors
        )
