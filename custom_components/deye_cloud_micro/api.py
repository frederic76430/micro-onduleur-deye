"""Client de l'API officielle Deye Cloud (developer.deyecloud.com)."""

from __future__ import annotations

import hashlib
from time import time
from typing import Any

import aiohttp

from .const import LOGGER, REQUEST_TIMEOUT, TOKEN_INVALID_CODES, TOKEN_MARGIN


class DeyeCloudError(Exception):
    """Erreur générique de l'API Deye Cloud."""


class DeyeCloudConnectionError(DeyeCloudError):
    """Le cloud ne répond pas ou renvoie une erreur."""


class DeyeCloudAuthError(DeyeCloudError):
    """Identifiants (compte ou application) refusés."""


def hash_password(password: str) -> str:
    """Deye demande le mot de passe chiffré en SHA-256 (hexadécimal minuscule)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class DeyeCloudAPI:
    """Client Deye Cloud. Ne renvoie jamais de 0 inventé : en cas d'échec, une exception."""

    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        email: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._email = email.strip()
        self._password_hash = hash_password(password)
        self._session = session
        self._token = ""
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- connexion
    async def connect(self, force: bool = False) -> None:
        """Obtenir un jeton (ou réutiliser celui encore valable)."""
        if not force and self._token and self._token_expires_at > time():
            return
        result = await self._request(
            f"/account/token?appId={self._app_id}",
            {"appSecret": self._app_secret, "email": self._email, "password": self._password_hash},
            auth=False,
        )
        token = result.get("accessToken")
        if not token:
            raise DeyeCloudAuthError(result.get("msg") or "Jeton absent de la réponse")
        try:
            duree = float(result.get("expiresIn") or 0)
        except (TypeError, ValueError):
            duree = 0
        self._token = token
        self._token_expires_at = time() + max(3600.0, (duree or 5_184_000) - TOKEN_MARGIN)
        LOGGER.debug("Jeton Deye Cloud obtenu")

    async def _request(self, path: str, body: dict, auth: bool = True) -> dict[str, Any]:
        """POST JSON. Se reconnecte une fois si le jeton a expiré."""
        for tentative in (1, 2):
            if auth:
                await self.connect(force=tentative == 2)
            headers = {"authorization": f"Bearer {self._token}"} if auth else {}
            try:
                async with self._session.post(
                    f"{self._base}{path}",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as response:
                    if response.status in (401, 403):
                        if auth and tentative == 1:
                            continue
                        raise DeyeCloudAuthError(f"HTTP {response.status}")
                    if response.status != 200:
                        raise DeyeCloudConnectionError(f"{path} : HTTP {response.status}")
                    result = await response.json(content_type=None)
            except DeyeCloudError:
                raise
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise DeyeCloudConnectionError(f"{path} : {err}") from err

            if not isinstance(result, dict):
                raise DeyeCloudConnectionError(f"{path} : réponse inattendue")
            if result.get("success") is False:
                code = str(result.get("code") or "")
                msg = result.get("msg") or code
                if code in TOKEN_INVALID_CODES and auth and tentative == 1:
                    continue
                if not auth or code in TOKEN_INVALID_CODES:
                    # Échec de l'obtention du jeton : identifiants ou application refusés
                    raise DeyeCloudAuthError(msg)
                raise DeyeCloudConnectionError(f"{path} : {msg}")
            return result
        raise DeyeCloudAuthError("Jeton refusé")

    # ---------------------------------------------------------------- données
    async def get_stations(self) -> list[dict[str, Any]]:
        result = await self._request("/station/list", {"page": 1, "size": 100})
        return list(result.get("stationList") or [])

    async def get_station_latest(self, station_id: int) -> dict[str, Any]:
        return await self._request("/station/latest", {"stationId": station_id})

    async def get_station_history(
        self, station_id: int, granularity: int, start: str, end: str | None = None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"stationId": station_id, "granularity": granularity, "startAt": start}
        if end:
            body["endAt"] = end
        result = await self._request("/station/history", body)
        return list(result.get("stationDataItems") or [])

    async def get_station_devices(self, station_id: int) -> list[dict[str, Any]]:
        result = await self._request("/station/device", {"stationIds": [station_id], "page": 1, "size": 50})
        return list(result.get("deviceListItems") or [])

    async def get_devices_latest(self, serials: list[str]) -> list[dict[str, Any]]:
        if not serials:
            return []
        result = await self._request("/device/latest", {"deviceList": serials[:10]})
        return list(result.get("deviceDataList") or [])
