"""Coordinator de l'intégration Deye Cloud (micro-onduleur)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DeyeCloudAPI, DeyeCloudAuthError, DeyeCloudConnectionError
from .const import (
    DOMAIN,
    INTERVAL,
    INTERVAL_HISTORY,
    INTERVAL_SLOW,
    LOGGER,
    MAX_STALE,
    OFFLINE_AFTER,
)

# Compteurs qui ne baissent jamais, sauf au changement de période indiqué
COUNTERS: dict[str, str | None] = {
    "e_today": "jour",
    "e_month": "mois",
    "e_year": "annee",
    "e_total": None,
}

# Unités des mesures d'appareil remises à 0 quand l'onduleur est éteint
UNITES_A_ZERO = {"W", "kW", "A"}

type DeyeCloudConfigEntry = ConfigEntry[DeyeCloudCoordinator]


def nombre(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _periode_changee(periode: str | None, avant: date, apres: date) -> bool:
    if periode == "jour":
        return apres != avant
    if periode == "mois":
        return (apres.year, apres.month) != (avant.year, avant.month)
    if periode == "annee":
        return apres.year != avant.year
    return False


def lire_date(valeur: Any) -> datetime | None:
    """Horodatage Deye : secondes ou millisecondes Unix, ou texte ISO."""
    if valeur in (None, ""):
        return None
    if (n := nombre(valeur)) is not None:
        if n > 1e12:
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    if (d := dt_util.parse_datetime(str(valeur))) is not None:
        return d if d.tzinfo else d.replace(tzinfo=dt_util.get_default_time_zone())
    return None


def _periodes(item: dict[str, Any]) -> list[str]:
    """Écritures possibles de la période d'une ligne d'historique.

    Deye peut renvoyer « 2026-11 » dans « month », ou bien year=2026 / month=11 / day=6 séparés.
    """
    textes = [str(item[c]) for c in ("day", "month", "year") if item.get(c) not in (None, "")]
    annee, mois, jour = (nombre(item.get(c)) for c in ("year", "month", "day"))
    if annee and 1900 < annee < 3000:
        a = int(annee)
        textes.append(str(a))
        if mois and 1 <= mois <= 12:
            textes.append(f"{a}-{int(mois):02d}")
            if jour and 1 <= jour <= 31:
                textes.append(f"{a}-{int(mois):02d}-{int(jour):02d}")
    if (d := lire_date(item.get("timeStamp"))) is not None:
        textes.append(dt_util.as_local(d).date().isoformat())
    return textes


def _correspond(item: dict[str, Any], texte: str, longueur: int) -> bool:
    """La ligne d'historique concerne-t-elle la période voulue (yyyy, yyyy-MM ou yyyy-MM-dd) ?"""
    for t in _periodes(item):
        # « 2026-11 » ne doit pas correspondre à « 2026-1 », d'où la longueur exacte ou un séparateur
        if t[:longueur] == texte and (len(t) == longueur or t[longueur] in "-T "):
            return True
    return False


def choisir(items: list[dict[str, Any]], texte: str) -> dict[str, Any] | None:
    """Ligne correspondant à la période, sinon la seule ligne renvoyée."""
    for item in items:
        if _correspond(item, texte, len(texte)):
            return item
    return items[0] if len(items) == 1 else None


MOTS_TOTAL = ("total", "cumulative", "cumulated", "lifetime", "accumulated")
MOTS_ENERGIE = ("production", "generation", "energy", "yield", "power")


def total_appareils(appareils: dict[str, Any]) -> float | None:
    """Somme des compteurs cumulés (kWh) des appareils, ex. « Total Production » / « Cumulative Production »."""
    somme, trouve = 0.0, False
    for appareil in appareils.values():
        meilleur = None
        for cle, point in (appareil.get("data") or {}).items():
            nom = cle.lower()
            if str(point.get("unit") or "").lower() not in ("kwh", "mwh"):
                continue
            if not any(m in nom for m in MOTS_TOTAL) or not any(m in nom for m in MOTS_ENERGIE):
                continue
            if re.search(r"consum|purchase|buy|grid|load|charge|battery|pv\d|mppt", nom):
                continue  # pas la production totale de l'onduleur
            if (v := nombre(point.get("value"))) is None:
                continue
            if str(point.get("unit")).lower() == "mwh":
                v *= 1000
            meilleur = v if meilleur is None else max(meilleur, v)
        if meilleur is not None:
            somme += meilleur
            trouve = True
    return round(somme, 3) if trouve else None


def _valeur(ligne: dict[str, Any] | None) -> float:
    v = nombre((ligne or {}).get("generationValue"))
    return v if v is not None else 0.0


class DeyeCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Récupère les données de la station Deye Cloud.

    Règles (comme pour l'intégration Hypontech) :
      - le cloud ne répond pas → dernières valeurs gardées, puis « indisponible » après MAX_STALE ;
      - un compteur d'énergie ne baisse jamais, sauf au changement de jour / mois / année ;
      - plus de mesure récente (onduleur éteint la nuit) → puissances à 0, compteurs conservés ;
      - identifiants refusés → Home Assistant propose de les ressaisir.
    """

    config_entry: DeyeCloudConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: DeyeCloudConfigEntry, api: DeyeCloudAPI, station_id: int
    ) -> None:
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=INTERVAL)
        self.api = api
        self.station_id = station_id
        self.station: dict[str, Any] = {}
        self.devices: list[dict[str, Any]] = []
        self.raw: dict[str, Any] = {}
        # Lecture locale (facultative) : DeyeLocalCoordinator, ajouté par __init__.py
        self.local = None
        self._last_success: datetime | None = None
        self._last_history: datetime | None = None
        self._last_slow: datetime | None = None
        self._jour_donnees: date | None = None
        self._hier_lu: date | None = None
        self._last_hier: datetime | None = None

    @property
    def jour_donnees(self) -> date | None:
        """Jour auquel se rapportent les données du cloud."""
        return self._jour_donnees

    # ------------------------------------------------------------------ cycle
    async def _async_update_data(self) -> dict[str, Any]:
        maintenant = dt_util.now()
        jour = maintenant.date()
        precedent: dict[str, Any] = dict(self.data or {})
        nouveau: dict[str, Any] = dict(precedent)
        nouveau.setdefault("devices", {})

        try:
            # Station et appareils : au démarrage puis toutes les 6 h
            if self._last_slow is None or maintenant - self._last_slow >= INTERVAL_SLOW:
                await self._lire_station()

            latest = await self.api.get_station_latest(self.station_id)
        except DeyeCloudAuthError as err:
            raise ConfigEntryAuthFailed("Identifiants Deye Cloud refusés") from err
        except DeyeCloudConnectionError as err:
            return self._sans_reponse(precedent, maintenant, err)
        self.raw["station_latest"] = latest

        # ---- Puissance
        mesure = lire_date(latest.get("lastUpdateTime"))
        nouveau["last_update"] = mesure
        eteint = mesure is None or maintenant - mesure > OFFLINE_AFTER
        puissance = nombre(latest.get("generationPower"))
        nouveau["power"] = 0.0 if eteint or puissance is None else puissance
        nouveau["online"] = not eteint
        nouveau["capacity"] = nombre(self.station.get("installedCapacity"))

        # ---- Énergie du jour / mois / année : toutes les 15 min, et dès le changement de jour
        jour_change = self._jour_donnees is not None and jour != self._jour_donnees
        if self._last_history is None or jour_change or maintenant - self._last_history >= INTERVAL_HISTORY:
            try:
                await self._lire_historique(nouveau, jour, maintenant)
                self._last_history = maintenant
            except DeyeCloudAuthError as err:
                raise ConfigEntryAuthFailed("Identifiants Deye Cloud refusés") from err
            except DeyeCloudConnectionError as err:
                LOGGER.debug("Historique Deye Cloud indisponible, nouvel essai au prochain passage : %s", err)

        # ---- Mesures détaillées des appareils (tension, PV1…)
        try:
            nouveau["devices"] = await self._lire_appareils(precedent.get("devices") or {})
        except DeyeCloudAuthError as err:
            raise ConfigEntryAuthFailed("Identifiants Deye Cloud refusés") from err
        except DeyeCloudConnectionError as err:
            LOGGER.debug("Mesures des appareils indisponibles : %s", err)

        # ---- État de l'onduleur (1 en ligne, 2 alerte, 3 hors ligne)
        etats = [nombre(a.get("state")) for a in (nouveau.get("devices") or {}).values()]
        if 2 in etats:
            nouveau["status"] = "alerte"
        elif 1 in etats:
            nouveau["status"] = "en_ligne"
        elif etats:
            nouveau["status"] = "hors_ligne"
        else:
            nouveau["status"] = "en_ligne" if nouveau["online"] else "hors_ligne"

        # ---- Production totale : compteur cumulé de l'onduleur (comme « Total Production » du site)
        if (total := total_appareils(nouveau.get("devices") or {})) is not None:
            nouveau["e_total"] = total

        # ---- Les compteurs ne baissent jamais (sauf changement de période)
        if precedent and self._jour_donnees is not None:
            for cle, periode in COUNTERS.items():
                avant, apres = nombre(precedent.get(cle)), nombre(nouveau.get(cle))
                if avant is None or apres is None or apres >= avant:
                    continue
                if _periode_changee(periode, self._jour_donnees, jour):
                    continue
                LOGGER.debug("%s : %s < %s ignoré (valeur du cloud en retard)", cle, apres, avant)
                nouveau[cle] = avant

        self._last_success = maintenant
        self._jour_donnees = jour
        return nouveau

    # ------------------------------------------------------------------ lectures
    async def _lire_station(self) -> None:
        stations = await self.api.get_stations()
        self.raw["station_list"] = stations
        for station in stations:
            if nombre(station.get("id")) == self.station_id:
                self.station = station
                break
        try:
            self.devices = await self.api.get_station_devices(self.station_id)
            self.raw["station_device"] = self.devices
        except DeyeCloudConnectionError as err:
            LOGGER.debug("Liste des appareils indisponible : %s", err)
        self._last_slow = dt_util.now()

    async def _lire_historique(self, nouveau: dict[str, Any], jour: date, maintenant: datetime) -> None:
        demain = jour + timedelta(days=1)
        mois_suivant = (jour.replace(day=1) + timedelta(days=32)).replace(day=1)

        items = await self.api.get_station_history(
            self.station_id, 2, jour.isoformat(), demain.isoformat()
        )
        self.raw["history_day"] = items
        # Pas de ligne pour la période = rien produit (encore) : 0.
        # Un 0 en cours de période est de toute façon ignoré par la règle « ne baisse jamais ».
        ligne_jour = choisir(items, jour.isoformat())
        nouveau["e_today"] = _valeur(ligne_jour)
        # Heures équivalentes à pleine puissance aujourd'hui (production / puissance installée)
        nouveau["full_hours"] = nombre((ligne_jour or {}).get("fullPowerHours"))
        if nouveau["full_hours"] is None and (kwc := nombre(self.station.get("installedCapacity"))):
            nouveau["full_hours"] = round(nouveau["e_today"] / kwc, 2)

        # Hier (pour les bilans) : une fois par heure suffit, la valeur ne change plus
        if self._hier_lu != jour or maintenant - (self._last_hier or maintenant) >= timedelta(hours=1):
            hier = jour - timedelta(days=1)
            items_hier = await self.api.get_station_history(self.station_id, 2, hier.isoformat(), jour.isoformat())
            self.raw["history_yesterday"] = items_hier
            nouveau["e_yesterday"] = _valeur(choisir(items_hier, hier.isoformat()))
            self._hier_lu, self._last_hier = jour, maintenant

        # Mois de l'année en cours (janvier → mois en cours)
        items = await self.api.get_station_history(
            self.station_id, 3, f"{jour.year}-01", mois_suivant.strftime("%Y-%m")
        )
        self.raw["history_month"] = items
        ligne = choisir(items, jour.strftime("%Y-%m"))
        if ligne is None and items and len(items) in (jour.month, jour.month + 1):
            # Période non reconnue : une ligne par mois depuis janvier, le mois en cours est le n° jour.month
            ligne = items[jour.month - 1]
        nouveau["e_month"] = _valeur(ligne)
        mois_annee = [
            v for i in items
            if _correspond(i, str(jour.year), 4) and (v := nombre(i.get("generationValue"))) is not None
        ]
        if not mois_annee and len(items) in (jour.month, jour.month + 1):
            mois_annee = [v for i in items[: jour.month] if (v := nombre(i.get("generationValue"))) is not None]
        somme_mois = round(sum(mois_annee), 3) if mois_annee else 0.0

        # Année : historique « par année » de Deye (même valeur que le graphique annuel du site),
        # sinon somme des mois
        items = await self.api.get_station_history(self.station_id, 4, str(jour.year), str(jour.year + 1))
        self.raw["history_year"] = items
        ligne = choisir(items, str(jour.year))
        annee = nombre((ligne or {}).get("generationValue"))
        nouveau["e_year"] = annee if annee is not None else somme_mois

    async def _lire_appareils(self, precedent: dict[str, Any]) -> dict[str, Any]:
        serials = [str(d["deviceSn"]) for d in self.devices if d.get("deviceSn")]
        if not serials:
            return precedent
        liste = await self.api.get_devices_latest(serials)
        self.raw["device_latest"] = liste
        resultat: dict[str, Any] = dict(precedent)
        for appareil in liste:
            sn = str(appareil.get("deviceSn") or "")
            if not sn:
                continue
            eteint = nombre(appareil.get("deviceState")) == 3
            mesures: dict[str, Any] = {}
            for point in appareil.get("dataList") or []:
                cle = str(point.get("key") or "").strip()
                if not cle:
                    continue
                unite = point.get("unit") or None
                valeur = nombre(point.get("value"))
                if eteint and unite in UNITES_A_ZERO and valeur is not None:
                    valeur = 0.0
                mesures[cle] = {"value": valeur if valeur is not None else point.get("value"), "unit": unite}
            resultat[sn] = {
                "type": appareil.get("deviceType"),
                "state": appareil.get("deviceState"),
                "collection_time": lire_date(appareil.get("collectionTime")),
                "data": mesures,
            }
        return resultat

    def _sans_reponse(self, precedent: dict[str, Any], maintenant: datetime, err: Exception) -> dict[str, Any]:
        """Le cloud ne répond pas : dernière valeur connue, puis indisponible."""
        if precedent and self._last_success and maintenant - self._last_success < MAX_STALE:
            LOGGER.debug("Deye Cloud injoignable, dernières valeurs conservées : %s", err)
            return precedent
        raise UpdateFailed(translation_domain=DOMAIN, translation_key="connection_error") from err
