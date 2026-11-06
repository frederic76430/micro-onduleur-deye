# Deye Cloud (micro-onduleur) pour Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/frederic76430/deye-cloud-micro/actions/workflows/validate.yml/badge.svg)](https://github.com/frederic76430/deye-cloud-micro/actions/workflows/validate.yml)

Intégration Home Assistant pour **micro-onduleur Deye** (SUN-M80G3, SUN-800G3, SUN2000G3…) qui réunit **deux sources dans un seul appareil** :

- **Deye Cloud** : l'API officielle ([developer.deyecloud.com](https://developer.deyecloud.com)) — production du jour, d'hier, du mois, de l'année, totale, état ;
- **en local** (facultatif) : le boîtier Wi-Fi du Deye (IP, port 8899), lu **toutes les 10 secondes**, sans passer par internet — comme l'intégration Solarman.

Chaque valeur est prise à la meilleure source, avec repli automatique sur l'autre.

> Version bêta : si une valeur est absente ou étrange, joignez le diagnostic à votre ticket
> (**Paramètres → Appareils et services → Deye Cloud → ⋮ → Télécharger le diagnostic** ;
> identifiants, adresse et numéros de série y sont masqués).

## Capteurs

| Capteur | Source |
|---|---|
| **Puissance solaire** (W) | locale (10 s) ; Deye Cloud si le boîtier ne répond plus ; 0 la nuit |
| **Production aujourd'hui** (kWh) | la plus haute des deux sources — **ne retombe jamais à 0 le soir** (le Deye remet son compteur à 0 en s'éteignant) ; remise à 0 à minuit |
| Production hier / ce mois / cette année (kWh) | Deye Cloud |
| **Production totale** (kWh) | compteur de l'onduleur (local ou Deye Cloud) |
| Heures pleine puissance aujourd'hui (h) | production du jour ÷ puissance installée |
| État de l'onduleur | Deye Cloud : en ligne / alerte / hors ligne |
| **Problème onduleur** (binaire) | alarme ou défaut signalé en local ou par Deye Cloud (jamais la nuit) |
| PV1, PV2 : puissance, tension, courant, production du jour | locale (PV3 / PV4 désactivés par défaut) |
| Tension, courant, fréquence du réseau, température, état (local) | locale |
| Puissance installée, dernière mesure (diagnostic) | Deye Cloud |

L'attribut `source` de la puissance, de la production du jour et du total indique d'où vient la valeur (`local`, `cloud`, `memoire`).

### Commandes (connexion locale, Deye allumé)

- **Production de l'onduleur** : marche / arrêt ;
- **Limitation de puissance** (%) : puissance maximale de sortie. Au plus **un changement toutes les 5 minutes** : chaque changement est enregistré dans la mémoire EEPROM de l'onduleur, qui s'use.

Les réglages de protection réseau (limites de tension et de fréquence, anti-îlotage, GFDI, RISO…) ne sont **volontairement pas modifiables**.

## Comportement

- Cloud injoignable : dernières valeurs gardées 30 min, puis « indisponible » (jamais de 0 inventé).
- Compteurs d'énergie : ne baissent jamais dans la journée (valeur en retard ignorée), remise à zéro au changement de jour / mois / année.
- La nuit : puissances et courants à 0, tensions et température « indisponibles » au bout de 15 min, production gardée.
- Après un redémarrage de Home Assistant : production du jour reprise (si elle date du jour même), total repris.
- Identifiants refusés : Home Assistant propose de les ressaisir.
- Deye Cloud n'étant mis à jour que toutes les 3 à 5 min, il est interrogé toutes les 5 min (historique toutes les 15 min).

## Installation

### 1. Application Deye Cloud
Sur [developer.deyecloud.com](https://developer.deyecloud.com), créez une application avec les accès **Station Monitoring** et **Device Monitoring**. Notez l'**App ID**, l'**App Secret** et le **Data center** (EMEA = Europe).

### 2. HACS
HACS → ⋮ → **Dépôts personnalisés** → `https://github.com/frederic76430/deye-cloud-micro`, catégorie **Intégration** → Télécharger, puis redémarrez Home Assistant.

(Sans HACS : copiez `custom_components/deye_cloud_micro` dans `/config/custom_components/`.)

### 3. Configuration
**Paramètres → Appareils et services → Ajouter une intégration → Deye Cloud (micro-onduleur)**

1. Centre de données, App ID, App Secret, e-mail et mot de passe de votre compte **Deye Cloud** (celui de l'appli). Le mot de passe est envoyé à Deye chiffré en SHA-256, comme l'exige l'API.
2. **Connexion locale (facultatif)** : adresse IP du boîtier Wi-Fi du Deye (ex. `192.168.1.50`), port `8899`. Le numéro de série du boîtier est trouvé automatiquement (ou repris de Deye Cloud, sinon il est sur l'étiquette du boîtier). La nuit le Deye est éteint : cochez « Enregistrer sans tester ».

La connexion locale se modifie à tout moment : **Deye Cloud → Configurer**.

Conseil : réservez l'adresse IP du boîtier dans votre box (bail DHCP statique) pour qu'elle ne change pas.

### Avec l'intégration Solarman
Les deux peuvent fonctionner ensemble (elles interrogent le même boîtier), mais une seule connexion locale suffit : une fois celle-ci validée, Solarman peut être supprimée.

## Remerciements
- Registres du micro-onduleur : profil `deye_micro.yaml` de [ha-solarman](https://github.com/davidrapan/ha-solarman) (Deye Modbus protocol V118).
- Communication locale : [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5).

Projet indépendant, sans lien avec Deye.

## Licence
MIT
