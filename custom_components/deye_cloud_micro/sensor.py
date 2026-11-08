"""Capteurs de l'intégration Deye Cloud (micro-onduleur)."""

from __future__ import annotations

import re

from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN
from .coordinator import DeyeCloudConfigEntry, DeyeCloudCoordinator, nombre

PARALLEL_UPDATES = 0

# (clé, nom, unité, device_class, state_class, icône, précision)
STATION_SENSORS = [
    ("power",   "Puissance solaire",      UnitOfPower.WATT,            SensorDeviceClass.POWER,  SensorStateClass.MEASUREMENT,      "mdi:solar-power-variant", 0),
    ("e_today", "Production aujourd'hui", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:solar-power",         2),
    ("e_yesterday", "Production hier",    UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, None,                              "mdi:calendar-arrow-left", 2),
    ("e_month", "Production ce mois",     UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:calendar-month",      2),
    ("e_year",  "Production cette année", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:calendar",            2),
    ("e_total", "Production totale",      UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING, "mdi:counter",             2),
    ("full_hours", "Heures pleine puissance aujourd'hui", "h",         None,                     SensorStateClass.MEASUREMENT,      "mdi:timer-sun",           2),
]

# Capteurs qui combinent la lecture locale et Deye Cloud
CLES_MIXTES = {"power", "e_today", "e_total"}

# Mesures locales : (clé, nom, unité, device_class, précision, activé par défaut)
LOCAL_SENSORS = [
    ("pv1_power",      "PV1 puissance",       "W",  SensorDeviceClass.POWER,       0, True),
    ("pv1_voltage",    "PV1 tension",         "V",  SensorDeviceClass.VOLTAGE,     1, True),
    ("pv1_current",    "PV1 courant",         "A",  SensorDeviceClass.CURRENT,     1, True),
    ("pv2_power",      "PV2 puissance",       "W",  SensorDeviceClass.POWER,       0, True),
    ("pv2_voltage",    "PV2 tension",         "V",  SensorDeviceClass.VOLTAGE,     1, True),
    ("pv2_current",    "PV2 courant",         "A",  SensorDeviceClass.CURRENT,     1, True),
    ("pv3_power",      "PV3 puissance",       "W",  SensorDeviceClass.POWER,       0, False),
    ("pv3_voltage",    "PV3 tension",         "V",  SensorDeviceClass.VOLTAGE,     1, False),
    ("pv3_current",    "PV3 courant",         "A",  SensorDeviceClass.CURRENT,     1, False),
    ("pv4_power",      "PV4 puissance",       "W",  SensorDeviceClass.POWER,       0, False),
    ("pv4_voltage",    "PV4 tension",         "V",  SensorDeviceClass.VOLTAGE,     1, False),
    ("pv4_current",    "PV4 courant",         "A",  SensorDeviceClass.CURRENT,     1, False),
    ("grid_voltage",   "Tension réseau",      "V",  SensorDeviceClass.VOLTAGE,     1, True),
    ("grid_current",   "Courant réseau",      "A",  SensorDeviceClass.CURRENT,     1, True),
    ("grid_frequency", "Fréquence réseau",    "Hz", SensorDeviceClass.FREQUENCY,   2, True),
    ("temperature",    "Température",         "°C", SensorDeviceClass.TEMPERATURE, 1, True),
    ("pv_power",       "Puissance PV (panneaux)", "W", SensorDeviceClass.POWER,    0, True),
    ("power_losses",   "Pertes de l'onduleur", "W",  SensorDeviceClass.POWER,       0, True),
]

# Unité Deye → (unité HA, device_class, state_class)
UNITES: dict[str, tuple[str, SensorDeviceClass | None, SensorStateClass | None]] = {
    "W":   ("W",   SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT),
    "kW":  ("kW",  SensorDeviceClass.POWER,       SensorStateClass.MEASUREMENT),
    "V":   ("V",   SensorDeviceClass.VOLTAGE,     SensorStateClass.MEASUREMENT),
    "A":   ("A",   SensorDeviceClass.CURRENT,     SensorStateClass.MEASUREMENT),
    "Hz":  ("Hz",  SensorDeviceClass.FREQUENCY,   SensorStateClass.MEASUREMENT),
    "℃":   ("°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    "°C":  ("°C",  SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    "kWh": ("kWh", SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING),
    "Wh":  ("Wh",  SensorDeviceClass.ENERGY,      SensorStateClass.TOTAL_INCREASING),
    "%":   ("%",   None,                          SensorStateClass.MEASUREMENT),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: DeyeCloudConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Créer les capteurs : station, puis toutes les mesures renvoyées par les appareils."""
    coordinator = entry.runtime_data
    entites: list[SensorEntity] = [StationSensor(coordinator, entry, *d) for d in STATION_SENSORS]
    entites.append(DerniereMesureSensor(coordinator, entry))
    entites.append(EtatSensor(coordinator, entry))
    entites.append(PuissanceInstalleeSensor(coordinator, entry))

    if coordinator.local is not None:
        # Micro-onduleur à 2 entrées : PV3 / PV4 seulement si une tension y a déjà été mesurée
        donnees = coordinator.local.data or {}
        nb_pv = 4 if (donnees.get("pv3_voltage") or 0) > 0 or (donnees.get("pv4_voltage") or 0) > 0 else 2
        garder = lambda cle: not any(cle.startswith(f"pv{n}_") for n in range(nb_pv + 1, 5))
        entites += [MesureLocaleSensor(coordinator, entry, *d) for d in LOCAL_SENSORS if garder(d[0])]
        entites += [ProductionPVSensor(coordinator, entry, n) for n in range(1, nb_pv + 1)]
        entites += [ProductionTotalePVSensor(coordinator, entry, n) for n in range(1, nb_pv + 1)]
        entites.append(EtatLocalSensor(coordinator, entry))

    async_add_entities(entites)

    # Mesures des appareils : créées dès que Deye les renvoie (la nuit, la liste peut être vide)
    connues: set[tuple[str, str]] = set()

    @callback
    def _ajouter_mesures() -> None:
        # Avec la connexion locale, les mesures détaillées viennent du boîtier : pas de doublons cloud
        if coordinator.local is not None:
            return
        appareils: dict[str, Any] = (coordinator.data or {}).get("devices") or {}
        plusieurs = len(appareils) > 1
        nouvelles = []
        for sn, appareil in appareils.items():
            for cle, point in (appareil.get("data") or {}).items():
                if (sn, cle) in connues or nombre(point.get("value")) is None:
                    continue  # textes (n° de série, version…) : visibles dans le diagnostic
                connues.add((sn, cle))
                nouvelles.append(MesureAppareilSensor(
                    coordinator, entry, sn, cle, point.get("unit"), plusieurs, coordinator.local is not None
                ))
        if nouvelles:
            async_add_entities(nouvelles)

    _ajouter_mesures()
    entry.async_on_unload(coordinator.async_add_listener(_ajouter_mesures))


async def _valeur_du_jour(entite: RestoreSensor, meme_jour: bool) -> float | None:
    """Dernière valeur enregistrée avant le redémarrage (si demandé : seulement si elle date d'aujourd'hui)."""
    etat = await entite.async_get_last_state()
    donnees = await entite.async_get_last_sensor_data()
    if etat is None or donnees is None or (valeur := nombre(donnees.native_value)) is None or valeur <= 0:
        return None
    if meme_jour and dt_util.as_local(etat.last_updated).date() != dt_util.now().date():
        return None
    return valeur


def _amorcer(local, cle: str, valeur: float) -> None:
    """Reprend la production du jour mémorisée par la lecture locale."""
    aujourdhui = dt_util.now().date()
    if local.jour is None:
        local.jour = aujourdhui
    if local.jour == aujourdhui and valeur > local.max_jour.get(cle, 0.0):
        local.max_jour[cle] = valeur


def nom_francais(cle: str) -> str:
    """Nom lisible pour les mesures Deye Cloud courantes.

    « TotalProduction1 » → « Production totale PV1 » : rangé juste sous « Production totale »
    dans la page de l'appareil (tri alphabétique).
    """
    if m := re.fullmatch(r"TotalProduction\s*(\d)", cle.strip(), re.IGNORECASE):
        return f"Production totale PV{m.group(1)}"
    if m := re.fullmatch(r"(?:Daily|Today)Production\s*(\d)", cle.strip(), re.IGNORECASE):
        return f"Production aujourd'hui PV{m.group(1)} (cloud)"
    return cle


class DeyeCloudEntity(CoordinatorEntity[DeyeCloudCoordinator]):
    """Entité rattachée à l'appareil de la station."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DeyeCloudCoordinator, entry: DeyeCloudConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.station_id))},
            name=entry.title,
            manufacturer="Deye",
            model="Micro-onduleur (Deye Cloud)",
            configuration_url="https://www.deyecloud.com",
        )


class StationSensor(DeyeCloudEntity, RestoreSensor):
    def __init__(self, coordinator, entry, cle, nom, unite, classe, etat, icone, precision) -> None:
        super().__init__(coordinator, entry)
        self._cle = cle
        self._attr_unique_id = f"{coordinator.station_id}_{cle}"
        self._attr_name = nom
        self._attr_native_unit_of_measurement = unite
        self._attr_device_class = classe
        self._attr_state_class = etat
        self._attr_icon = icone
        self._attr_suggested_display_precision = precision

    _restaure: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Après un redémarrage : production du jour (si elle date d'aujourd'hui) et total repris
        if self._cle in ("e_today", "e_total"):
            valeur = await _valeur_du_jour(self, self._cle == "e_today")
            if valeur is not None:
                self._restaure = valeur
                if self._cle == "e_today" and (local := self.coordinator.local) is not None:
                    _amorcer(local, "e_today", valeur)
        # Valeurs mixtes : se mettre à jour aussi à chaque lecture locale (toutes les 10 s)
        if (local := self.coordinator.local) is not None and self._cle in CLES_MIXTES:
            self.async_on_remove(local.async_add_listener(self._handle_coordinator_update))

    def _mixte(self) -> tuple[float | None, str]:
        """Valeur et source : local quand il répond, sinon Deye Cloud."""
        cloud = nombre((self.coordinator.data or {}).get(self._cle))
        local = self.coordinator.local
        if local is None or self._cle not in CLES_MIXTES:
            return cloud, "cloud"
        donnees = local.data or {}
        if self._cle == "power":
            if local.frais():
                return nombre(donnees.get("power")), "local"
            return cloud, "cloud"
        if self._cle == "e_today":
            # Le plus haut des deux (le Deye remet son compteur à 0 en s'éteignant),
            # le cloud seulement s'il parle bien d'aujourd'hui
            jour_cloud = cloud if self.coordinator.jour_donnees == dt_util.now().date() else None
            valeurs = {"local": local.production_jour(), "cloud": jour_cloud}
        else:  # e_total : le compteur de l'onduleur, en local ou via le cloud
            valeurs = {"local": nombre(donnees.get("e_total")) or None, "cloud": cloud}
        valeurs = {k: v for k, v in valeurs.items() if v is not None}
        if not valeurs:
            if self._cle == "e_total" and self._restaure is not None:
                return self._restaure, "memoire"
            return (0.0 if self._cle == "e_today" else None), "aucune"
        source = max(valeurs, key=valeurs.get)
        return valeurs[source], source

    @property
    def native_value(self) -> float | None:
        return self._mixte()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.local is None or self._cle not in CLES_MIXTES:
            return None
        return {"source": self._mixte()[1]}


class DerniereMesureSensor(DeyeCloudEntity, SensorEntity):
    """Heure de la dernière mesure reçue par Deye Cloud (utile pour voir si l'onduleur répond)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"
    _attr_name = "Dernière mesure"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_last_update"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("last_update")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"en_ligne": bool((self.coordinator.data or {}).get("online"))}


class MesureAppareilSensor(DeyeCloudEntity, SensorEntity):
    """Une mesure de l'appareil telle que renvoyée par Deye (ex. « PV1 Voltage »)."""

    def __init__(
        self, coordinator, entry, sn: str, cle: str, unite: str | None, plusieurs: bool, doublon: bool = False
    ) -> None:
        super().__init__(coordinator, entry)
        self._sn, self._cle = sn, cle
        self._attr_unique_id = f"{coordinator.station_id}_{sn}_{slugify(cle)}"
        nom = nom_francais(cle)
        self._attr_name = f"{nom} ({sn[-4:]})" if plusieurs else nom
        ha_unite, classe, etat = UNITES.get(unite or "", (unite, None, SensorStateClass.MEASUREMENT))
        self._attr_native_unit_of_measurement = ha_unite
        self._attr_device_class = classe
        self._attr_state_class = etat
        # Mesures sans unité connue, ou déjà lues en local : désactivées par défaut (à activer si utiles)
        self._attr_entity_registry_enabled_default = unite in UNITES and not doublon

    @property
    def native_value(self) -> float | None:
        appareil = ((self.coordinator.data or {}).get("devices") or {}).get(self._sn) or {}
        return nombre(((appareil.get("data") or {}).get(self._cle) or {}).get("value"))


class EtatSensor(DeyeCloudEntity, SensorEntity):
    """État de l'onduleur selon Deye Cloud : en ligne, alerte ou hors ligne (la nuit)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["en_ligne", "alerte", "hors_ligne"]
    _attr_translation_key = "status"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_status"

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("status")


class PuissanceInstalleeSensor(DeyeCloudEntity, SensorEntity):
    """Puissance crête installée déclarée sur Deye Cloud (kWc)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "kWc"
    _attr_icon = "mdi:solar-panel-large"
    _attr_name = "Puissance installée"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_capacity"

    @property
    def native_value(self) -> float | None:
        return nombre((self.coordinator.data or {}).get("capacity"))


class _LocalEntity(DeyeCloudEntity):
    """Entité lue en local, mise à jour toutes les 10 s."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.local.async_add_listener(self._handle_coordinator_update))


class MesureLocaleSensor(_LocalEntity, SensorEntity):
    """Mesure lue directement sur le boîtier Wi-Fi du Deye."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, cle, nom, unite, classe, precision, actif) -> None:
        super().__init__(coordinator, entry)
        self._cle = cle
        self._attr_unique_id = f"{coordinator.station_id}_local_{cle}"
        self._attr_name = nom
        self._attr_native_unit_of_measurement = unite
        self._attr_device_class = classe
        self._attr_suggested_display_precision = precision
        self._attr_entity_registry_enabled_default = actif
        # Mesures techniques rangées dans « Diagnostic » : la partie « Capteurs » ne garde que l'essentiel
        if cle not in ("pv1_power", "pv2_power", "pv3_power", "pv4_power", "pv_power"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        local = self.coordinator.local
        # Puissances et courants : 0 la nuit ; tensions, fréquence, température : gardées 15 min
        if self._cle.endswith(("_power", "_current")):
            return local.data is not None and self._cle in local.data
        return local.disponible()

    @property
    def native_value(self) -> float | None:
        return nombre((self.coordinator.local.data or {}).get(self._cle))


class EtatLocalSensor(_LocalEntity, SensorEntity):
    """État de l'onduleur lu en local (attente, normal, alarme, défaut, hors ligne)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["attente", "auto_test", "normal", "alarme", "defaut", "hors_ligne", "inconnu"]
    _attr_translation_key = "local_state"
    _attr_icon = "mdi:state-machine"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_local_state"

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.local.data or {}).get("state", "hors_ligne")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        donnees = self.coordinator.local.data or {}
        return {
            "code_alarme": donnees.get("alarm", 0),
            "code_defaut": donnees.get("fault", 0),
            "derniere_erreur_connexion": self.coordinator.local.derniere_erreur,
        }


class ProductionPVSensor(_LocalEntity, RestoreSensor):
    """Production du jour d'une entrée PV (ne retombe pas à 0 le soir, remise à 0 à minuit)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator, entry, n: int) -> None:
        super().__init__(coordinator, entry)
        self._cle = f"e_today_{n}"
        self._attr_unique_id = f"{coordinator.station_id}_local_e_today_{n}"
        self._attr_name = f"Production aujourd'hui PV{n}"
        self._attr_entity_registry_enabled_default = n <= 2

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (valeur := await _valeur_du_jour(self, True)) is not None:
            _amorcer(self.coordinator.local, self._cle, valeur)

    @property
    def native_value(self) -> float | None:
        v = self.coordinator.local.production_jour(self._cle)
        return 0.0 if v is None else v


class ProductionTotalePVSensor(_LocalEntity, RestoreSensor):
    """Compteur total d'une entrée PV (comme « Total Production 1 / 2 » de Solarman)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator, entry, n: int) -> None:
        super().__init__(coordinator, entry)
        self._cle = f"e_total_{n}"
        self._attr_unique_id = f"{coordinator.station_id}_local_e_total_{n}"
        self._attr_name = f"Production totale PV{n}"
        self._restaure: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (donnees := await self.async_get_last_sensor_data()) is not None:
            self._restaure = nombre(donnees.native_value)

    @property
    def native_value(self) -> float | None:
        v = nombre((self.coordinator.local.data or {}).get(self._cle))
        if v and v > 0:
            return max(v, self._restaure or 0)
        return self._restaure
