"""Constantes de l'intégration Deye Cloud (micro-onduleur)."""

from datetime import timedelta
from logging import Logger, getLogger

DOMAIN = "deye_cloud_micro"
LOGGER: Logger = getLogger(__package__)

CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_REGION = "region"
CONF_STATION_ID = "station_id"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_SERIAL = "logger_serial"
DEFAULT_PORT = 8899

# Centres de données Deye Cloud (champ « DATA CENTER » sur developer.deyecloud.com)
REGIONS = {
    "eu": "https://eu1-developer.deyecloud.com/v1.0",  # EMEA, APAC
    "us": "https://us1-developer.deyecloud.com/v1.0",  # Amériques
}

REQUEST_TIMEOUT = 20
# Code renvoyé par Deye quand le jeton n'est plus valable (HTTP 200 + success=false)
TOKEN_INVALID_CODES = {"2101019"}
# Le jeton dure ≈ 60 jours ; on le renouvelle un jour avant la fin
TOKEN_MARGIN = 86400

# Rythme des mises à jour (le cloud Deye ne se met à jour que toutes les 3 à 5 min)
INTERVAL = timedelta(minutes=5)
# Production du jour / mois / année
INTERVAL_HISTORY = timedelta(minutes=15)
# Production totale (somme des années) et liste des appareils
INTERVAL_SLOW = timedelta(hours=6)
# Sans nouvelle mesure de la station depuis ce délai : puissance ramenée à 0 (onduleur éteint)
OFFLINE_AFTER = timedelta(minutes=20)
# Au-delà de cette durée sans réponse du cloud, les capteurs passent « indisponible »
MAX_STALE = timedelta(minutes=30)
