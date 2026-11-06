"""Lecture locale du micro-onduleur Deye par son boîtier Wi-Fi (protocole Solarman V5, port 8899).

Registres repris du profil « deye_micro.yaml » de ha-solarman (Deye Modbus protocol V118).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

INTERVAL_LOCAL = timedelta(seconds=10)
# Boîtier injoignable (onduleur éteint la nuit) : on réessaie moins souvent
INTERVAL_LOCAL_HORS_LIGNE = timedelta(minutes=2)
# Après ce délai sans réponse locale, les mesures locales passent « indisponible »
LOCAL_STALE = timedelta(minutes=15)
# Une mesure locale plus vieille que ça n'est plus utilisée pour la puissance
LOCAL_FRAIS = timedelta(seconds=90)

DEBUT, FIN = 0x3B, 0x74  # bloc lu en une seule requête (58 registres)
# Réglages : limitation de puissance (0x28) → marche / arrêt (0x2B)
REG_LIMITE, REG_MARCHE = 0x28, 0x2B
MARCHE, ARRET = 1, 2

ETATS = {0: "attente", 1: "auto_test", 2: "normal", 3: "alarme", 4: "defaut"}


class DeyeLocalError(Exception):
    """Le boîtier Wi-Fi ne répond pas (onduleur éteint, mauvaise IP ou mauvais numéro de série)."""


async def detecter_numero_serie(hote: str, timeout: float = 3.0) -> str | None:
    """Demande au boîtier son numéro de série (requête de découverte Solarman, UDP 48899)."""
    loop = asyncio.get_running_loop()
    reponse: asyncio.Future[bytes] = loop.create_future()

    class _Protocole(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            if not reponse.done():
                reponse.set_result(data)

    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(_Protocole, remote_addr=(hote, 48899))
        transport.sendto(b"WIFIKIT-214028-READ")
        data = await asyncio.wait_for(reponse, timeout)
    except (OSError, TimeoutError):
        return None
    finally:
        if transport:
            transport.close()
    # Réponse : « IP,MAC,NUMÉRO_DE_SÉRIE »
    parties = data.decode(errors="ignore").strip().split(",")
    if len(parties) >= 3 and parties[2].strip().isdigit():
        return parties[2].strip()
    return None


def decoder(r: dict[int, int]) -> dict[str, Any]:
    """Registres bruts → mesures (mêmes échelles que ha-solarman)."""

    def u(a: int) -> int:
        return r.get(a, 0)

    def s(a: int) -> int:
        v = u(a)
        return v - 0x10000 if v & 0x8000 else v

    d: dict[str, Any] = {
        "state": ETATS.get(u(0x3B), "inconnu"),
        "e_today": round(u(0x3C) * 0.1, 1),
        "e_total": round((u(0x3F) | (u(0x40) << 16)) * 0.1, 1),
        "grid_voltage": round(u(0x49) * 0.1, 1),
        "grid_current": round(s(0x4C) * 0.1, 1),
        "grid_frequency": round(u(0x4F) * 0.01, 2),
        "power": round((u(0x56) | (u(0x57) << 16)) * 0.1, 1),
        "temperature": round((u(0x5A) - 1000) * 0.01, 1),
        "alarm": u(0x65) | (u(0x66) << 16),
        "fault": u(0x67) | (u(0x68) << 16) | (u(0x69) << 32) | (u(0x6A) << 48),
    }
    # Réglages (lus seulement si le bloc 0x28-0x2B a été lu)
    if REG_LIMITE in r:
        d["power_limit"] = round(u(REG_LIMITE) * 0.1, 1)
    if REG_MARCHE in r:
        d["inverter_on"] = {MARCHE: True, ARRET: False}.get(u(REG_MARCHE))
    for n in range(1, 5):
        v = round(u(0x6D + 2 * (n - 1)) * 0.1, 1)
        a = round(u(0x6E + 2 * (n - 1)) * 0.1, 1)
        d[f"pv{n}_voltage"], d[f"pv{n}_current"], d[f"pv{n}_power"] = v, a, round(v * a, 1)
        d[f"e_today_{n}"] = round(u(0x40 + n) * 0.1, 1)
    return d


class DeyeLocalClient:
    """Connexion au boîtier Wi-Fi (bibliothèque pysolarmanv5)."""

    def __init__(self, hote: str, port: int, numero_serie: str) -> None:
        self.hote, self.port, self.numero_serie = hote, port, int(numero_serie)
        self._client = None
        self._verrou = asyncio.Lock()

    async def lire(self) -> dict[int, int]:
        async with self._verrou:
            return await self._lire()

    async def ecrire(self, registre: int, valeur: int) -> None:
        """Écrit un réglage (fonction Modbus 0x10, comme Solarman)."""
        async with self._verrou:
            try:
                await self._connecter()
                await asyncio.wait_for(
                    self._client.write_multiple_holding_registers(registre, [int(valeur)]), 10
                )
            except Exception as err:  # noqa: BLE001
                await self.fermer()
                raise DeyeLocalError(str(err) or type(err).__name__) from err

    async def _connecter(self) -> None:
        from pysolarmanv5 import PySolarmanV5Async  # import tardif : chargé seulement si utilisé

        if self._client is None:
            self._client = PySolarmanV5Async(
                self.hote, self.numero_serie, port=self.port, mb_slave_id=1,
                socket_timeout=8, auto_reconnect=False,
            )
            await self._client.connect()

    async def _lire(self) -> dict[int, int]:
        try:
            await self._connecter()
            valeurs = await asyncio.wait_for(
                self._client.read_holding_registers(DEBUT, FIN - DEBUT + 1), 10
            )
            reglages = await asyncio.wait_for(
                self._client.read_holding_registers(REG_LIMITE, REG_MARCHE - REG_LIMITE + 1), 10
            )
        except Exception as err:  # noqa: BLE001 - toute erreur réseau / protocole
            await self.fermer()
            raise DeyeLocalError(str(err) or type(err).__name__) from err
        registres = {DEBUT + i: v for i, v in enumerate(valeurs)}
        registres.update({REG_LIMITE + i: v for i, v in enumerate(reglages)})
        return registres

    async def fermer(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


class DeyeLocalCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Mesures locales toutes les 10 s.

    Règles :
      - boîtier injoignable (la nuit) → puissances et courants à 0, autres mesures gardées 15 min ;
      - production du jour : le Deye remet son compteur à 0 en s'éteignant le soir, et renvoie
        parfois la valeur de la veille au réveil → on garde le maximum de la journée, remis à 0 à minuit.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DeyeLocalClient) -> None:
        super().__init__(hass, LOGGER, config_entry=entry, name=f"{DOMAIN}_local",
                         update_interval=INTERVAL_LOCAL, always_update=False)
        self.client = client
        self.last_success: datetime | None = None
        self.jour: date | None = None
        # Production du jour mémorisée : totale (e_today) et par entrée PV (e_today_1 à 4)
        self.max_jour: dict[str, float] = {}
        self.hier: dict[str, float] = {}
        self.derniere_erreur: str | None = None

    async def async_ecrire(self, registre: int, valeur: int) -> None:
        """Écrit un réglage puis relit l'onduleur pour afficher la nouvelle valeur."""
        await self.client.ecrire(registre, valeur)
        await self.async_refresh()

    def frais(self) -> bool:
        return self.last_success is not None and dt_util.now() - self.last_success < LOCAL_FRAIS

    def disponible(self) -> bool:
        return self.last_success is not None and dt_util.now() - self.last_success < LOCAL_STALE

    def production_jour(self, cle: str = "e_today") -> float | None:
        """Production du jour mémorisée (None si pas encore de mesure aujourd'hui)."""
        return self.max_jour.get(cle, 0.0) if self.jour == dt_util.now().date() else None

    async def _async_update_data(self) -> dict[str, Any]:
        maintenant = dt_util.now()
        if self.jour is not None and self.jour != maintenant.date():
            self.hier, self.max_jour, self.jour = self.max_jour, {}, maintenant.date()
        try:
            mesures = decoder(await self.client.lire())
        except DeyeLocalError as err:
            self.derniere_erreur = str(err)
            self.update_interval = INTERVAL_LOCAL_HORS_LIGNE
            LOGGER.debug("Deye local injoignable : %s", err)
            donnees = dict(self.data or {})
            for cle in list(donnees):
                if cle == "power" or cle.endswith(("_power", "_current")):
                    donnees[cle] = 0.0
            donnees["state"] = "hors_ligne"
            return donnees

        self.derniere_erreur = None
        self.update_interval = INTERVAL_LOCAL
        self.last_success = maintenant
        if self.jour is None:
            self.jour = maintenant.date()

        # Puissance aberrante (trame corrompue) : valeur précédente
        if mesures["power"] > 3000:
            mesures["power"] = (self.data or {}).get("power", 0.0)

        for cle in ("e_today", "e_today_1", "e_today_2", "e_today_3", "e_today_4"):
            v, memoire = mesures.get(cle, 0.0), self.max_jour.get(cle, 0.0)
            if memoire == 0 and v > 0 and v == self.hier.get(cle):
                LOGGER.debug("%s = %s : valeur de la veille, ignorée", cle, v)
            elif v > memoire:
                self.max_jour[cle] = v
        return mesures
