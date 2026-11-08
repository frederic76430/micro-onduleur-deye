"""Tests de l'intégration Deye Cloud (pytest-homeassistant-custom-component)."""

import hashlib
import json
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.deye_cloud_micro.const import DOMAIN  # noqa: E402

BASE = "https://eu1-developer.deyecloud.com/v1.0"
STATION = 4242
SN = "2401234567"
LOGGER_SN = "3912345678"
DATA = {
    "region": "eu", "app_id": "123456789", "app_secret": "secret",
    "email": "a@b.fr", "password": "pw", "station_id": STATION,
}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
async def _paris(hass):
    await hass.config.async_set_time_zone("Europe/Paris")


def _cloud(aioclient_mock, *, jour=0.45, mois=6.2, annees=((2025, 310.0), (2026, 280.5)),
           puissance=150.0, maj=None, latest_status=200, token_ok=True, dev_state=1, mesures=True, total=576.3):
    """Simule Deye Cloud. L'historique répond selon la granularité demandée."""
    aioclient_mock.clear_requests()
    if token_ok:
        aioclient_mock.post(f"{BASE}/account/token?appId=123456789",
                            json={"success": True, "accessToken": "T", "expiresIn": "5183999"})
    else:
        aioclient_mock.post(f"{BASE}/account/token?appId=123456789",
                            json={"success": False, "code": "2101006", "msg": "account or password error"})
    aioclient_mock.post(f"{BASE}/station/list", json={"success": True, "total": 1, "stationList": [
        {"id": STATION, "name": " Maison ", "installedCapacity": 0.8, "startOperatingTime": 1735725600}]})
    aioclient_mock.post(f"{BASE}/station/device", json={"success": True, "deviceListItems": [
        {"deviceSn": SN, "deviceType": "MICRO_INVERTER", "stationId": STATION},
        {"deviceSn": LOGGER_SN, "deviceType": "COLLECTOR", "stationId": STATION}]})
    maintenant = dt_util.utcnow().timestamp()
    aioclient_mock.post(f"{BASE}/station/latest", status=latest_status, json={
        "success": True, "generationPower": puissance,
        "lastUpdateTime": maintenant - 120 if maj is None else maj})

    async def historique(method, url, data):
        corps = json.loads(data) if isinstance(data, (str, bytes)) else data
        g, debut = corps["granularity"], corps["startAt"]
        if g == 2:
            items = [{"day": debut, "generationValue": jour}] if jour is not None else []
        elif g == 3:
            items = [{"month": "2026-10", "generationValue": 40.0},
                     {"month": dt_util.now().strftime("%Y-%m"), "generationValue": mois}]
        else:
            items = [{"year": str(a), "generationValue": v} for a, v in annees]
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        return AiohttpClientMockResponse(method, url, json={"success": True, "stationDataItems": items})

    aioclient_mock.post(f"{BASE}/station/history", side_effect=historique)
    aioclient_mock.post(f"{BASE}/device/latest", json={"success": True, "deviceDataList": [{
        "deviceSn": SN, "deviceState": dev_state, "collectionTime": int(maintenant), "dataList": [] if not mesures else [
            {"key": "PV1 Voltage", "value": "32.5", "unit": "V"},
            {"key": "PV1 Power", "value": "75", "unit": "W"},
            {"key": "Grid Frequency", "value": "50.01", "unit": "Hz"},
            {"key": "Total Production", "value": str(total), "unit": "kWh"},
            {"key": "PV1 Total Production", "value": "300.1", "unit": "kWh"},
            {"key": "Firmware", "value": "MI-2.33", "unit": ""},
        ]}]})


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=str(STATION), title="Deye Cloud - Maison")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _etat(hass, key):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{STATION}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id)


async def _tick(hass, freezer, delta):
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_capteurs(hass, freezer, aioclient_mock):
    freezer.move_to("2026-11-06T12:00:00+01:00")
    _cloud(aioclient_mock)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert float(_etat(hass, "power").state) == 150
    assert float(_etat(hass, "e_today").state) == 0.45
    assert float(_etat(hass, "e_month").state) == 6.2
    assert float(_etat(hass, "e_year").state) == 280.5
    assert float(_etat(hass, "e_total").state) == 576.3
    assert float(_etat(hass, f"{SN}_pv1_voltage").state) == 32.5
    assert float(_etat(hass, f"{SN}_pv1_power").state) == 75
    assert _etat(hass, "status").state == "en_ligne"
    assert float(_etat(hass, "capacity").state) == 0.8
    assert float(_etat(hass, "full_hours").state) == 0.56
    # Le mot de passe est envoyé en SHA-256, jamais en clair
    corps = next(c[2] for c in aioclient_mock.mock_calls if "account/token" in str(c[1]))
    assert corps["password"] == hashlib.sha256(b"pw").hexdigest()


async def test_nuit_puissance_zero_compteurs_gardes(hass, freezer, aioclient_mock):
    freezer.move_to("2026-11-06T20:30:00+01:00")
    trois_h = dt_util.utcnow().timestamp() - 3 * 3600
    _cloud(aioclient_mock, maj=trois_h, puissance=12.0, dev_state=3)
    await _setup(hass)
    assert float(_etat(hass, "power").state) == 0
    assert float(_etat(hass, "e_today").state) == 0.45
    assert float(_etat(hass, f"{SN}_pv1_power").state) == 0
    assert float(_etat(hass, f"{SN}_pv1_voltage").state) == 32.5
    assert _etat(hass, "status").state == "hors_ligne"


async def test_coupure_cloud(hass, freezer, aioclient_mock):
    freezer.move_to("2026-11-06T12:00:00+01:00")
    _cloud(aioclient_mock)
    await _setup(hass)
    _cloud(aioclient_mock, latest_status=500)
    await _tick(hass, freezer, timedelta(minutes=6))
    assert float(_etat(hass, "e_today").state) == 0.45
    for _ in range(6):
        await _tick(hass, freezer, timedelta(minutes=6))
    assert _etat(hass, "e_today").state == "unavailable"
    _cloud(aioclient_mock)
    await _tick(hass, freezer, timedelta(minutes=6))
    assert float(_etat(hass, "e_today").state) == 0.45


async def test_compteur_ne_baisse_pas_puis_minuit(hass, freezer, aioclient_mock):
    freezer.move_to("2026-11-06T16:00:00+01:00")
    _cloud(aioclient_mock)
    await _setup(hass)
    # Valeur en retard (plus basse) dans la journée : ignorée
    _cloud(aioclient_mock, jour=0.1, total=500.0)
    await _tick(hass, freezer, timedelta(minutes=16))
    assert float(_etat(hass, "e_today").state) == 0.45
    assert float(_etat(hass, "e_total").state) == 576.3
    # Nouveau jour, Deye n'a encore rien publié : 0
    freezer.move_to("2026-11-07T00:05:00+01:00")
    _cloud(aioclient_mock, jour=None)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(_etat(hass, "e_today").state) == 0
    assert float(_etat(hass, "e_month").state) == 6.2


async def test_config_flow_et_reauth(hass, aioclient_mock):
    _cloud(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    saisie = {k: v for k, v in DATA.items() if k != "station_id"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], saisie)
    assert result["step_id"] == "local"
    # Connexion locale laissée vide : Deye Cloud seul
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"port": 8899, "sans_test": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Deye Cloud - Maison"
    assert result["data"] == DATA
    assert result["options"]["host"] == ""
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
    )
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**saisie, "password": "nouveau"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "nouveau"


async def test_config_flow_refus(hass, aioclient_mock):
    _cloud(aioclient_mock, token_ok=False)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {k: v for k, v in DATA.items() if k != "station_id"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_mesures_appareil_ajoutees_plus_tard(hass, freezer, aioclient_mock):
    """La nuit, Deye ne renvoie aucune mesure : les capteurs sont créés dès qu'elles arrivent."""
    freezer.move_to("2026-11-06T21:00:00+01:00")
    _cloud(aioclient_mock, mesures=False)
    await _setup(hass)
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{STATION}_{SN}_pv1_voltage") is None
    _cloud(aioclient_mock)
    await _tick(hass, freezer, timedelta(minutes=6))
    assert float(_etat(hass, f"{SN}_pv1_voltage").state) == 32.5



# ------------------------------------------------------------------ lecture locale
def _registres(jour=4, total=5763, puissance=1500, etat=2):
    r = {a: 0 for a in range(0x3B, 0x75)}
    r.update({0x28: 100, 0x29: 0, 0x2A: 0, 0x2B: 1})
    r.update({0x3B: etat, 0x3C: jour, 0x3F: total, 0x45: 2503, 0x47: 3109, 0x49: 2312, 0x4C: 6, 0x4F: 5001,
              0x56: puissance, 0x5A: 3800, 0x6D: 325, 0x6E: 23, 0x6F: 318, 0x70: 22})
    return r


class _FauxBoitier:
    def __init__(self):
        self.reponse = _registres()
        self.ecrits = []

    async def ecrire(self, registre, valeur):
        self.ecrits.append((registre, valeur))
        self.reponse[registre] = valeur

    async def lire(self):
        if isinstance(self.reponse, Exception):
            raise self.reponse
        return dict(self.reponse)


@pytest.fixture
def boitier(monkeypatch):
    from custom_components.deye_cloud_micro import local

    faux = _FauxBoitier()

    async def lire(self):
        return await faux.lire()

    async def fermer(self):
        return None

    async def ecrire(self, registre, valeur):
        await faux.ecrire(registre, valeur)

    monkeypatch.setattr(local.DeyeLocalClient, "lire", lire)
    monkeypatch.setattr(local.DeyeLocalClient, "ecrire", ecrire)
    monkeypatch.setattr(local.DeyeLocalClient, "fermer", fermer)
    return faux


async def _setup_local(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=str(STATION), title="Deye Cloud - Maison",
                            options={"host": "192.168.1.50", "port": 8899, "logger_serial": LOGGER_SN})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_local_et_cloud_reunis(hass, freezer, aioclient_mock, boitier):
    freezer.move_to("2026-11-06T12:00:00+01:00")
    _cloud(aioclient_mock, puissance=120.0)
    await _setup_local(hass)
    # Puissance : local (150 W, toutes les 10 s) plutôt que le cloud (120 W)
    etat = _etat(hass, "power")
    assert float(etat.state) == 150 and etat.attributes["source"] == "local"
    # Production du jour : le plus haut des deux (cloud 0,45 > local 0,4)
    assert float(_etat(hass, "e_today").state) == 0.45
    assert float(_etat(hass, "e_total").state) == 576.3
    assert float(_etat(hass, "local_pv1_voltage").state) == 32.5
    assert float(_etat(hass, "local_pv1_power").state) == 74.8
    assert float(_etat(hass, "local_temperature").state) == 28.0
    assert float(_etat(hass, "local_grid_frequency").state) == 50.01
    assert _etat(hass, "local_state").state == "normal"
    assert float(_etat(hass, "local_pv_power").state) == 74.8 + 70.0
    assert float(_etat(hass, "local_e_total_1").state) == 250.3
    assert float(_etat(hass, "local_e_total_2").state) == 310.9
    # Micro à 2 entrées : pas de PV3 / PV4
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{STATION}_local_pv3_voltage") is None
    assert float(_etat(hass, "local_power_losses").state) == 0  # 144,8 W DC < 150 W AC dans l'exemple
    # La mesure cloud en double est désactivée par défaut
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{STATION}_{SN}_pv1_voltage") is None or \
        reg.async_get(reg.async_get_entity_id("sensor", DOMAIN, f"{STATION}_{SN}_pv1_voltage")).disabled


async def test_local_soir_compteur_remis_a_zero(hass, freezer, aioclient_mock, boitier):
    freezer.move_to("2026-11-06T17:00:00+01:00")
    _cloud(aioclient_mock, jour=0.3)
    await _setup_local(hass)
    boitier.reponse = _registres(jour=6)
    await _tick(hass, freezer, timedelta(seconds=11))
    assert float(_etat(hass, "e_today").state) == 0.6
    # Le Deye remet son compteur à 0 en s'éteignant : ignoré
    boitier.reponse = _registres(jour=0, puissance=0)
    await _tick(hass, freezer, timedelta(seconds=11))
    assert float(_etat(hass, "e_today").state) == 0.6
    # Puis il ne répond plus (nuit) : puissance 0, production gardée, état hors ligne
    from custom_components.deye_cloud_micro.local import DeyeLocalError

    boitier.reponse = DeyeLocalError("timeout")
    vieux = dt_util.utcnow().timestamp() - 3600
    _cloud(aioclient_mock, jour=0.3, maj=vieux)
    await _tick(hass, freezer, timedelta(minutes=6))
    assert float(_etat(hass, "power").state) == 0
    assert float(_etat(hass, "e_today").state) == 0.6
    assert float(_etat(hass, "local_pv1_power").state) == 0
    assert _etat(hass, "local_state").state == "hors_ligne"
    # Au bout de 15 min sans réponse : tension indisponible
    await _tick(hass, freezer, timedelta(minutes=16))
    assert _etat(hass, "local_pv1_voltage").state == "unavailable"


async def test_local_minuit_et_valeur_de_la_veille(hass, freezer, aioclient_mock, boitier):
    freezer.move_to("2026-11-06T16:00:00+01:00")
    _cloud(aioclient_mock, jour=0.3)
    entry = await _setup_local(hass)
    boitier.reponse = _registres(jour=6)
    await _tick(hass, freezer, timedelta(seconds=11))
    # Lendemain matin : le Deye renvoie encore 0,6 (la veille) → ignoré
    freezer.move_to("2026-11-07T08:00:00+01:00")
    _cloud(aioclient_mock, jour=None)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _tick(hass, freezer, timedelta(seconds=11))
    assert entry.runtime_data.local.production_jour() == 0
    assert float(_etat(hass, "e_today").state) == 0
    boitier.reponse = _registres(jour=1)
    await _tick(hass, freezer, timedelta(seconds=11))
    assert float(_etat(hass, "e_today").state) == 0.1


async def test_config_flow_avec_local(hass, aioclient_mock, boitier):
    _cloud(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    saisie = {k: v for k, v in DATA.items() if k != "station_id"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], saisie)
    assert result["step_id"] == "local"
    # Numéro de série laissé vide : repris de Deye Cloud (boîtier « COLLECTOR »)
    from custom_components.deye_cloud_micro import config_flow as cf

    async def pas_de_reponse(hote, timeout=3.0):
        return None

    cf_local = __import__("custom_components.deye_cloud_micro.local", fromlist=["x"])
    cf_local.detecter_numero_serie = pas_de_reponse
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "port": 8899, "sans_test": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {"host": "192.168.1.50", "port": 8899, "logger_serial": LOGGER_SN}
    assert cf.numero_collecteur([{"deviceSn": "1", "deviceType": "COLLECTOR"}]) == "1"


async def test_options_boitier_injoignable(hass, freezer, aioclient_mock, boitier):
    freezer.move_to("2026-11-06T21:00:00+01:00")
    _cloud(aioclient_mock)
    entry = await _setup(hass)
    from custom_components.deye_cloud_micro.local import DeyeLocalError

    boitier.reponse = DeyeLocalError("timeout")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    saisie = {"host": "192.168.1.50", "port": 8899, "logger_serial": LOGGER_SN, "sans_test": False}
    result = await hass.config_entries.options.async_configure(result["flow_id"], saisie)
    assert result["errors"] == {"base": "cannot_connect_local"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**saisie, "sans_test": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["host"] == "192.168.1.50"
    assert _etat(hass, "local_state").state == "hors_ligne"



async def test_marche_arret_et_limitation(hass, freezer, aioclient_mock, boitier):
    from homeassistant.exceptions import HomeAssistantError

    freezer.move_to("2026-11-06T12:00:00+01:00")
    _cloud(aioclient_mock)
    await _setup_local(hass)
    reg = er.async_get(hass)
    interrupteur = reg.async_get_entity_id("switch", DOMAIN, f"{STATION}_local_inverter_on")
    limite = reg.async_get_entity_id("number", DOMAIN, f"{STATION}_local_power_limit")
    assert hass.states.get(interrupteur).state == "on"
    assert float(hass.states.get(limite).state) == 100

    await hass.services.async_call("switch", "turn_off", {"entity_id": interrupteur}, blocking=True)
    await hass.async_block_till_done()
    assert boitier.ecrits[-1] == (0x2B, 2)
    assert hass.states.get(interrupteur).state == "off"

    await hass.services.async_call("number", "set_value", {"entity_id": limite, "value": 50}, blocking=True)
    await hass.async_block_till_done()
    assert boitier.ecrits[-1] == (0x28, 50)
    assert float(hass.states.get(limite).state) == 50
    # Deuxième changement trop rapproché : refusé (usure de l'EEPROM)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("number", "set_value", {"entity_id": limite, "value": 60}, blocking=True)
    assert boitier.ecrits[-1] == (0x28, 50)

    # La nuit (boîtier muet) : commandes indisponibles
    from custom_components.deye_cloud_micro.local import DeyeLocalError

    boitier.reponse = DeyeLocalError("timeout")
    await _tick(hass, freezer, timedelta(minutes=3))
    assert hass.states.get(interrupteur).state == "unavailable"
    assert hass.states.get(limite).state == "unavailable"


def test_periodes_formats_deye():
    from custom_components.deye_cloud_micro.coordinator import choisir

    separe = [{"year": 2026, "month": m, "generationValue": m} for m in range(1, 12)]
    assert choisir(separe, "2026-11")["generationValue"] == 11
    assert choisir(separe, "2026-01")["generationValue"] == 1
    assert choisir([{"month": "2026-10"}, {"month": "2026-11", "v": 1}], "2026-11")["v"] == 1
    assert choisir([{"year": "2026", "month": "11", "day": "6", "v": 2}, {"year": 2026, "month": 11, "day": 5}],
                   "2026-11-06")["v"] == 2
    assert choisir([{"month": "2026-10"}, {"month": "2026-12"}], "2026-11") is None


async def test_problème_et_production_pv(hass, freezer, aioclient_mock, boitier):
    freezer.move_to("2026-11-06T12:00:00+01:00")
    _cloud(aioclient_mock)
    await _setup_local(hass)
    reg = er.async_get(hass)
    probleme = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{STATION}_problem")
    assert hass.states.get(probleme).state == "off"
    assert float(_etat(hass, "e_yesterday").state) == 0.45
    r = _registres(etat=3)
    r[0x41] = 3
    boitier.reponse = r
    await _tick(hass, freezer, timedelta(seconds=11))
    assert hass.states.get(probleme).state == "on"
    assert float(_etat(hass, "local_e_today_1").state) == 0.3
    boitier.reponse = _registres()
    await _tick(hass, freezer, timedelta(seconds=11))
    assert float(_etat(hass, "local_e_today_1").state) == 0.3


async def test_redemarrage_le_soir(hass, freezer, aioclient_mock, boitier):
    """Redémarrage le soir (Deye éteint) : production PV et total reprennent leur dernière valeur."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

    from custom_components.deye_cloud_micro.local import DeyeLocalError

    freezer.move_to("2026-11-06T21:00:00+01:00")
    reg = er.async_get(hass)
    # Entités existantes avant le redémarrage
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=str(STATION), title="Deye Cloud - Maison",
                            options={"host": "192.168.1.50", "port": 8899, "logger_serial": LOGGER_SN})
    entry.add_to_hass(hass)
    pv1 = reg.async_get_or_create("sensor", DOMAIN, f"{STATION}_local_e_today_1", config_entry=entry).entity_id
    total = reg.async_get_or_create("sensor", DOMAIN, f"{STATION}_e_total", config_entry=entry).entity_id
    mock_restore_cache_with_extra_data(hass, [
        (State(pv1, "0.3"), {"native_value": 0.3, "native_unit_of_measurement": "kWh"}),
        (State(total, "576.3"), {"native_value": 576.3, "native_unit_of_measurement": "kWh"}),
    ])
    boitier.reponse = DeyeLocalError("timeout")
    _cloud(aioclient_mock, mesures=False, maj=dt_util.utcnow().timestamp() - 4 * 3600)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(pv1).state) == 0.3
    assert float(hass.states.get(total).state) == 576.3
    # Le lendemain : PV repart de 0
    freezer.move_to("2026-11-07T00:01:00+01:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _tick(hass, freezer, timedelta(minutes=3))
    assert float(hass.states.get(pv1).state) == 0



def test_noms_francais():
    from custom_components.deye_cloud_micro.sensor import nom_francais

    assert nom_francais("TotalProduction1") == "Production totale PV1"
    assert nom_francais("TotalProduction2") == "Production totale PV2"
    assert nom_francais("PV1 Voltage") == "PV1 Voltage"
