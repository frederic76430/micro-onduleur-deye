"""Diagnostic : réponses brutes de Deye Cloud (sans identifiants ni données personnelles)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_APP_SECRET
from .coordinator import DeyeCloudConfigEntry

A_MASQUER = {
    CONF_EMAIL, CONF_PASSWORD, CONF_APP_SECRET,
    "ownerName", "contactPhone", "locationAddress", "locationLat", "locationLng",
    "name", "deviceSn", "accessToken", "refreshToken",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DeyeCloudConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "config": async_redact_data(dict(entry.data), A_MASQUER),
        "donnees": async_redact_data(
            {k: v for k, v in (coordinator.data or {}).items() if k != "devices"}, A_MASQUER
        ),
        "nombre_mesures_par_appareil": {
            f"appareil_{i + 1}": len((a.get("data") or {}))
            for i, a in enumerate(((coordinator.data or {}).get("devices") or {}).values())
        },
        "reponses_brutes": async_redact_data(coordinator.raw, A_MASQUER),
    }
