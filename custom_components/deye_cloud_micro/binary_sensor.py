"""Alerte « problème onduleur » : alarme ou défaut signalé en local ou par Deye Cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeyeCloudConfigEntry
from .sensor import DeyeCloudEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: DeyeCloudConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ProblemeOnduleur(entry.runtime_data, entry)])


class ProblemeOnduleur(DeyeCloudEntity, BinarySensorEntity):
    """Allumé si l'onduleur signale une alarme ou un défaut (pas la nuit : éteint n'est pas un problème)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Problème onduleur"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.station_id}_problem"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.local is not None:
            self.async_on_remove(self.coordinator.local.async_add_listener(self._handle_coordinator_update))

    def _local(self) -> dict[str, Any]:
        local = self.coordinator.local
        return (local.data or {}) if local is not None and local.frais() else {}

    @property
    def is_on(self) -> bool:
        donnees = self._local()
        return bool(
            (self.coordinator.data or {}).get("status") == "alerte"
            or donnees.get("state") in ("alarme", "defaut")
            or donnees.get("alarm")
            or donnees.get("fault")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        donnees = self._local()
        return {
            "etat_cloud": (self.coordinator.data or {}).get("status"),
            "etat_local": donnees.get("state"),
            "code_alarme": donnees.get("alarm"),
            "code_defaut": donnees.get("fault"),
        }
