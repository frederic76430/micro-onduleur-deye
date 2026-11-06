"""Marche / arrêt du micro-onduleur (connexion locale uniquement)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeyeCloudConfigEntry
from .local import ARRET, MARCHE, REG_MARCHE, DeyeLocalError
from .sensor import _LocalEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: DeyeCloudConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if coordinator.local is not None:
        async_add_entities([MarcheOnduleur(coordinator, entry)])


class MarcheOnduleur(_LocalEntity, SwitchEntity):
    """Allume ou coupe la production de l'onduleur (registre 0x2B, comme Solarman)."""

    _attr_name = "Production de l'onduleur"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_local_inverter_on"

    @property
    def available(self) -> bool:
        return self.coordinator.local.frais() and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.local.data or {}).get("inverter_on")

    async def _ecrire(self, valeur: int) -> None:
        try:
            await self.coordinator.local.async_ecrire(REG_MARCHE, valeur)
        except DeyeLocalError as err:
            raise HomeAssistantError(f"Le Deye ne répond pas : {err}") from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._ecrire(MARCHE)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._ecrire(ARRET)
