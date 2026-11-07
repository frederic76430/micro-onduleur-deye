"""Limitation de puissance du micro-onduleur (connexion locale uniquement)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import DeyeCloudConfigEntry
from .local import REG_LIMITE, DeyeLocalError
from .sensor import _LocalEntity

PARALLEL_UPDATES = 1

# Chaque changement est enregistré dans la mémoire EEPROM de l'onduleur, qui s'use :
# un changement au plus toutes les 5 minutes
DELAI_MIN = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant, entry: DeyeCloudConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if coordinator.local is not None:
        async_add_entities([LimitePuissance(coordinator, entry)])


class LimitePuissance(_LocalEntity, NumberEntity):
    """Puissance maximale de sortie en % (registre 0x28, « Active Power Regulation »)."""

    _attr_name = "Limitation de puissance"
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_local_power_limit"
        self._dernier_changement = None

    @property
    def available(self) -> bool:
        return self.coordinator.local.frais() and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.local.data or {}).get("power_limit")

    async def async_set_native_value(self, value: float) -> None:
        if self.native_value is not None and round(value) == round(self.native_value):
            return  # déjà à cette valeur : rien à écrire
        maintenant = dt_util.now()
        if self._dernier_changement and maintenant - self._dernier_changement < DELAI_MIN:
            reste = int((DELAI_MIN - (maintenant - self._dernier_changement)).total_seconds() // 60) + 1
            raise HomeAssistantError(
                f"Pour ménager la mémoire de l'onduleur, un seul changement toutes les 5 minutes "
                f"(réessayez dans {reste} min)"
            )
        try:
            await self.coordinator.local.async_ecrire(REG_LIMITE, round(value * 10))
        except DeyeLocalError as err:
            raise HomeAssistantError(f"Le Deye ne répond pas : {err}") from err
        self._dernier_changement = maintenant
