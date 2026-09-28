# Architecture de `ha-carnet-scolaire`

Ce document décrit l'architecture réelle de `ha-carnet-scolaire`, telle qu'elle est
implémentée dans `custom_components/carnet_scolaire/`. Il est écrit pour un lecteur
qui n'a jamais ouvert ce dépôt et qui doit pouvoir, à la fin, expliquer non
seulement *où* les choses se trouvent mais *pourquoi* elles sont arrangées
ainsi.

La source de vérité est le code. Les documents de conception —
[`SPECIFICATION.md`](SPECIFICATION.md),
[`annexe-a-entites.md`](annexe-a-entites.md),
[`annexe-b-rate-limit.md`](annexe-b-rate-limit.md) et la
[revue contradictoire](revue-contradictoire-v1.md) — ont produit les décisions
qui suivent, mais l'implémentation les a corrigés en plusieurs endroits. Les
divergences relevées sont rassemblées à la fin, dans
« [Écarts avec la spécification](#12-écarts-avec-la-spécification) ».

Un mot de vocabulaire, parce que la confusion coûte cher : le dépôt s'appelle
**`ha-carnet-scolaire`** et l'intégration s'affiche sous le nom **Carnet scolaire**. Le
domaine Home Assistant s'appelle
**`carnet_scolaire`**, et seulement lui. Ce choix est délibéré
(`DOMAIN`, `custom_components/carnet_scolaire/const.py`) : l'intégration collecte
auprès de PRONOTE **et** d'EcoleDirecte, donc aucun des deux noms d'éditeur ne
convient, et un domaine neutre ne peut entrer en collision avec aucune
intégration personnalisée qui occupe `pronote` ou `ecoledirecte` — deux
composants qui réclament le même domaine ne cohabitent pas, Home Assistant en
charge un et ignore l'autre. Jusqu'à la 0.0.32, l'intégration s'appelait
*Pronote NG* (domaine `pronote_ng`, dépôt `ha-pronote-ng`) ; Home Assistant ne
sait pas migrer un domaine, et une entrée créée sous l'ancien nom se supprime
puis se rajoute.

---

## Table des matières

1. [Objet de l'intégration](#1-objet-de-lintégration)
2. [Vue d'ensemble et insertion dans Home Assistant](#2-vue-densemble-et-insertion-dans-home-assistant)
3. [Le limiteur de débit](#3-le-limiteur-de-débit)
4. [L'ordonnanceur](#4-lordonnanceur)
5. [La session](#5-la-session)
6. [La passerelle](#6-la-passerelle)
7. [Le modèle de données](#7-le-modèle-de-données)
8. [Un cycle de collecte, de bout en bout](#8-un-cycle-de-collecte-de-bout-en-bout)
9. [Le flow de configuration](#9-le-flow-de-configuration)
10. [Sécurité et vie privée](#10-sécurité-et-vie-privée)
11. [La qualité](#11-la-qualité)
12. [Écarts avec la spécification](#12-écarts-avec-la-spécification)

---

## 1. Objet de l'intégration

`ha-carnet-scolaire` expose dans Home Assistant les données PRONOTE d'un compte parent
ou élève : emploi du temps, devoirs, notes, absences, évaluations, actualités,
messagerie, menus de cantine, équipe pédagogique et bulletins des périodes
closes.

L'objectif directeur, énoncé dans l'en-tête de `custom_components/carnet_scolaire/__init__.py`,
n'est pas « afficher PRONOTE » mais **rendre les automatisations simples à
écrire**, avec un critère de succès précis : toute automatisation scolaire
ordinaire doit pouvoir s'écrire sans template Jinja. Cet objectif est ce qui
explique la forme de presque tout le reste — des états numériques plutôt que
du texte formaté, des `binary_sensor` pour les prédicats, des entités `event`
pour les changements, des *device triggers* nommés, et huit blueprints livrés
en français et en anglais dans `blueprints/automation/carnet_scolaire/{fr,en}/`.

La contrainte structurante, elle, vient d'ailleurs. PRONOTE ne publie aucune
limite de débit : il applique des sanctions, et la plus coûteuse d'entre elles
— la suspension d'adresse — vise une **adresse IP** et non un compte, se
déclenche sur des échecs de connexion répétés, et n'est documentée nulle part
(l'en-tête de `custom_components/carnet_scolaire/ratelimit.py`). Tout le dimensionnement de
cette intégration découle de ce fait : une seule voie vers le réseau, un
budget compté, et une comptabilité de l'authentification séparée de celle des
requêtes.

La dépendance amont est `pronotepy==2.15.7`, épinglée exactement
(`custom_components/carnet_scolaire/manifest.json`). Le pin n'est pas de la
prudence rituelle : la moitié des décisions de la passerelle et du client
durci reposent sur des détails de comportement vérifiés dans cette version
précise, et un déplacement de version demande de relire deux fichiers.

---

## 2. Vue d'ensemble et insertion dans Home Assistant

### 2.1 Les couches

Le code se lit en quatre couches, de la plus abstraite à la plus contrainte.
La voie vers le réseau n'est plus la passerelle Pronote : c'est un
**connecteur** (spécification connecteurs §3.1). Auth, transport, budget et
décodage vivent dedans. Au-dessus, l'ordonnanceur, le delta, les coordinateurs
et les plateformes ne voient qu'une couture ; `tiers.py` ne fait plus que
`collect` + instantané + delta.

**Le cœur de domaine** ne connaît ni un protocole scolaire ni Home Assistant.
Il contient l'ordonnanceur (`scheduler.py`), le détecteur de delta
(`delta.py`), les DTO gelés (`models.py`), les constantes (`const.py`),
la lecture et l'estimation des options (`options.py`) et la normalisation
d'URL (`urls.py`). Ces modules prennent des horloges injectables et répondent
à des questions ; c'est ce qui les rend testables à 100 % sans réseau et sans
instance Home Assistant. Les DTO restent ceux de Pronote — le pivot interne
n'est pas un second modèle « école » (spécification connecteurs §3.2). Le
pivot **cartes**, lui, est le JSON d'attributs de
[l'annexe A §2.1](annexe-a-entites.md#21-forme-des-éléments).

**L'orchestration** relie le cœur au monde sans importer un protocole :
`account.py` possède le battement de cœur et la boucle de collecte, `tiers.py`
demande un palier au connecteur puis publie l'instantané, `coordinator.py`
détient les instantanés, `login_guard.py` conserve l'état punitif Pronote qui
doit survivre aux objets qui l'ont ouvert. Une entry Ecoledirecte ne construit
ni `SerialExecutor`, ni `SessionManager`, ni le `RateLimiter` Pronote.

**La couture `SchoolConnector`** *est* la voie réseau. `build_connector()`
lit `source` sur l'entrée (absente = Pronote, spécification connecteurs §3.6)
et rend une implémentation. Un connecteur = un compte d'une source ; deux
sources dans le foyer = deux entries. Les capacités (`tiers`, `writes`,
`services`) commandent ce qui est collecté, exposé en entité et proposé dans
les options (spécification connecteurs §3.4).

* **Pronote** — `PronoteConnector` possède encore toute la pile historique :
  `session.py`, `ratelimit.py` (annexe B : compteur chiffré, `Erreur.G`,
  5–7 requêtes par login), `gateway.py` et `hardened_client.py`. Ces deux
  derniers restent les seuls à importer des *types de données* `pronotepy`.
* **Ecoledirecte** — `EcoledirecteConnector` possède son limiteur
  (`ed_limiter.py`), un client aiohttp calqué sur le handshake
  `ecoledirecte_api` 0.3.0 (`ed_client.py`) et le mapping vers les DTO
  (`ed_mapping.py`). Seul `connectors/ecoledirecte/` voit le JSON Aplim. Le
  paquet PyPI `ecoledirecte` n'est **pas** une dépendance : son `backoff`
  relancerait un login hors admission, et son journal DEBUG fuit le jeton
  (spécification connecteurs §7.1). La version d'API est la constante
  `ECOLEDIRECTE_API_VERSION` dans `ed_client.py` ; le workflow
  `ecoledirecte-watch` la compare chaque semaine à `APIVERSION` lu dans le
  sdist ou la *wheel* PyPI, **échoue** s'ils divergent, et ouvre une *pull
  request* qui met à jour la constante. Un 517 en production, c'est cette
  sonde en retard, pas un « réessayer ».

Les deux limiteurs isolent leurs compteurs. Ils partagent la *forme* de
décision (`admit` / `TierDeferred` / `LimiterState`), les clés d'options et
la politique d'heures creuses, jamais les octets ni le garde-fou de flow
(spécification connecteurs §3.3). Ecoledirecte v1 ne collecte que
`TIMETABLE`, `HOMEWORK`, `MARKS` et `ATTENDANCE` ; `SESSION` n'est jamais
passé à `async_collect` (spécification connecteurs §3.5).

**La couche Home Assistant** : `__init__.py` (setup et teardown), les sept
plateformes d'entités, `config_flow.py` et `flow_login.py`, `services.py`,
`diagnostics.py`, et les trois modules de *device automation*. Les services
Pronote-only parlent à l'implémentation Pronote, pas au protocole commun :
une entry Ecoledirecte n'a rien à leur offrir.

```mermaid
flowchart TB
  subgraph HA["Home Assistant"]
    CE["Config entry et runtime_data"]
    PLAT["Plateformes sensor, binary_sensor, calendar, todo, event, image, button"]
    SVC["Services Pronote-only et device automations"]
    DIAG["Diagnostics et repairs"]
    REG["Device et entity registry"]
  end

  subgraph ORCH["Orchestration · ni protocole"]
    ACC["account.py · battement de coeur, batch, deltas"]
    TIERS["tiers.py · collect, snapshot, delta"]
    COORD["coordinator.py · un coordinateur par palier"]
    GUARD["login_guard.py · etat punitif Pronote hors entree"]
  end

  subgraph CORE["Coeur de domaine"]
    SCHED["scheduler.py · echeances, pas de minuteurs"]
    DELTA["delta.py · detection de changement"]
    MODELS["models.py · DTO geles, pivot interne"]
    OPT["options.py · lecture et estimation du budget"]
  end

  subgraph SEAM["Couture SchoolConnector · la voie reseau"]
    FACT["build_connector · source ou Pronote"]
    SC["SchoolConnector"]
  end

  subgraph PR["PronoteConnector"]
    SESS["session.py · SessionManager et SerialExecutor"]
    RL["ratelimit.py · annexe B"]
    GW["gateway.py · decodage vers DTO"]
    HC["hardened_client.py · client durci et transport"]
  end

  subgraph ED["EcoledirecteConnector"]
    EDLIM["ed_limiter.py"]
    EDHTTP["ed_client.py · handshake 0.3.0"]
    EDMAP["ed_mapping.py"]
  end

  PY["pronotepy 2.15.7"]
  NETP["Serveur PRONOTE de l etablissement"]
  NETE["api.ecoledirecte.com"]

  CE --> ACC
  PLAT --> COORD
  SVC --> PR
  DIAG --> ACC
  PLAT --> REG
  ACC --> TIERS
  ACC --> SCHED
  ACC --> DELTA
  ACC --> COORD
  ACC --> GUARD
  ACC --> OPT
  ACC --> FACT
  FACT --> SC
  TIERS --> SC
  SC --> PR
  SC --> ED
  SESS --> RL
  SESS --> HC
  GW --> MODELS
  ED --> MODELS
  GW --> PY
  HC --> PY
  PY --> NETP
  EDHTTP --> NETE
  COORD --> MODELS

  classDef boundary fill:#fde68a,stroke:#b45309,color:#111
  class GW,HC,EDHTTP,EDMAP boundary
```

La frontière `pronotepy` reste le trait le plus important **du connecteur
Pronote**. Elle existe pour trois raisons concrètes, énoncées dans
l'en-tête de `custom_components/carnet_scolaire/models.py` :

* un objet `pronotepy` lu après la fermeture de sa session lève
  `Erreur.G = 22` ;
* plusieurs attributs amont sont *paresseux* — `Information.content`,
  `Attachment.data`, `Lesson.content`, `Period.grades`,
  `Discussion.messages` — et appellent le réseau **au moment de la lecture**,
  donc potentiellement depuis la boucle d'événements et hors de tout budget ;
* une entité qui détiendrait une référence au client maintiendrait vivant tout
  le graphe d'objets, session comprise.

Le cas qui mérite d'être nommé est `ClientInfo._cache()`, qui poste
directement via `communication.post` en contournant `ClientBase.post` et — sur
un compte parent — la signature `membre` qui dit *de quel enfant* il s'agit.
Lire `ClientInfo.address`, `.email`, `.phone` ou `.ine_number` depuis n'importe
où déclenche donc un appel non budgété et **possiblement mal attribué**. C'est
la meilleure justification de cette frontière-là : elle transforme un piège
d'attribution en une impossibilité de construction. Côté Ecoledirecte, le
même principe s'applique sans `pronotepy` : le JSON Aplim s'arrête au
mapping, et aucun jeton ne franchit la mémoire du client.

### 2.2 Le cycle de vie de l'entrée de configuration

`async_setup_entry` (`custom_components/carnet_scolaire/__init__.py`) suit un
ordre qui n'est pas indifférent.

1. Construction de `PronoteAccount`. Le constructeur assemble la passerelle
   avec le fuseau de l'établissement, le limiteur, l'ordonnanceur, l'exécuteur
   sériel, le gestionnaire de session, le détecteur de delta et **un
   coordinateur par palier** — `Tier.SESSION` inclus, qui ne coûte aucune
   requête mais dont les entités ont besoin de quelque chose à quoi
   s'abonner.
2. Restauration de l'état hérité : `limiter.import_state(...)` et
   `scheduler.import_state(...)` relisent ce que l'instance précédente a
   déposé dans `hass.data` (`account.py`). C'est ce qui empêche une
   boucle de retry de setup d'effacer une pénalité — voir §3.7.
3. `account.async_setup()` : une connexion, la lecture de la forme du compte
   (enfants, périodes, période courante) à **zéro requête supplémentaire**,
   puis l'armement du battement de cœur et une première collecte immédiate.
4. Classement des échecs. C'est ici que le vocabulaire d'erreurs de
   `session.py` **et** de `connectors/errors.py` se traduit en vocabulaire
   Home Assistant (§7.2) : `InvalidCredentials`, `MfaRequired`,
   `ConnectorCredentialsError` et `ConnectorChallengeRequired` deviennent
   `ConfigEntryAuthFailed`, donc un flow de ré-authentification ;
   `BootstrapFailed` et `ConnectorTransportError` ouvrent la réparation
   `bootstrap_failed` puis `ConfigEntryNotReady` ; `AccountUnreadable`,
   `ConnectorUndecodableError` et un `ConnectorError` nu ouvrent
   `account_unreadable` puis `ConfigEntryNotReady` ; `LoginRefused`,
   `LoginRefusedByLimiter`, `IntegrationFault`, `ConnectorSessionExpiredError`,
   `PronoteAPIError`, `TimeoutError` et `OSError` deviennent
   `ConfigEntryNotReady` **sans** réparation. La position de ce dernier bras
   au-dessus de l'attrape-tout `ConnectorError` n'est pas cosmétique : un jeton
   de session expiré y ouvrait sinon une carte demandant un rapport de bug —
   qu'aucun chemin de code ne referme — pour une condition réparée au réessai
   suivant, le connecteur ayant déjà oublié ses jetons. La distinction compte :
   répondre « identifiants invalides » à un serveur qui répond mal envoie
   l'utilisateur ressaisir un mot de passe correct.
5. `entry.runtime_data = account`, et *aussi* `hass.data[DOMAIN][entry_id]`.
   Le second n'est pas une redondance : les *device automations* et les
   services résolvent un compte depuis un `device_id` sans avoir l'entrée sous
   la main.
6. **Le device compte est créé ici**, avant le forwarding des plateformes
   (`async_setup_entry`, `__init__.py`). Chaque device enfant déclare
   `via_device` sur lui,
   et les sept plateformes sont forwardées en parallèle : au premier
   démarrage, les devices enfants étaient créés avant l'existence de leur
   parent, ce que Home Assistant signale par « references a non existing
   via_device » et sanctionne en aplatissant l'arbre. Le défaut se réparait au
   redémarrage suivant, ce qui est exactement pourquoi il avait survécu à la
   revue.
7. Forwarding des plateformes, enregistrement des services, écoute des
   changements d'options.

Le déchargement (`async_unload_entry`, `__init__.py`, et
`PronoteAccount.async_unload`, `account.py`) fait trois choses dans cet
ordre : arrêter le battement, **exporter** l'état de l'ordonnanceur et du
limiteur vers `hass.data`, puis fermer la session. Et il ne joint **jamais** le
fil de travail depuis la boucle d'événements : le transport `pronotepy` peut
être au milieu d'une requête, et joindre gèlerait le rechargement. Un fil qui
fuit vaut mieux qu'une instance figée.

Un changement d'options provoque un **rechargement** de l'entrée
(`PronoteOptionsFlow`, `config_flow.py`, un `OptionsFlowWithReload`) et non une
reconfiguration en place. L'intégration n'enregistre **aucun** écouteur de mise
à jour : Home Assistant 2026.9 signale comme une erreur, cassante en 2026.12,
tout `async_update_reload_and_abort` sur une entrée qui en porte un, c'est-à-dire
chaque réauthentification et chaque reconfiguration d'un compte chargé. Le
formulaire ne recharge qu'une entrée **chargée**, comme l'écouteur, qui n'existait
qu'entre une mise en place réussie et le déchargement. C'est le comportement de
Home Assistant, et c'est pourquoi la
préservation des échéances passe par `export_state` / `import_state` et non
par `FetchScheduler.reconfigure` — voir §4.4.

### 2.3 Ce qui est exposé

| Surface | Où | Contenu |
| --- | --- | --- |
| Plateformes | `sensor`, `binary_sensor`, `calendar`, `todo`, `event`, `image`, `button` (`PLATFORMS`, `__init__.py`) | 15 capteurs primitifs, 17 capteurs « compteur + liste », 8 séries d'historique par période close, 8 capteurs de diagnostic, 9 `binary_sensor` métier plus un `throttled`, 3 calendriers, 1 liste de devoirs, 9 entités `event`, 1 image, 2 boutons |
| Services | `services.py`, `services.yaml` | `refresh`, `get_ical_url`, `get_identity`, `mark_homework_done`, `mark_information_read`, `send_message`, `generate_timetable_pdf`, `get_rate_limit_status` |
| Device automations | `device_trigger.py`, `device_condition.py`, `device_action.py` | 14 déclencheurs, 10 conditions, 4 actions |
| Registres | `_account_device` / `_student_device`, `entity.py` | un device par compte, un device par enfant en `via_device` |
| Diagnostics | `diagnostics.py` | téléchargement par entrée et par device |
| Repairs | `async_open_credentials_issue` et les trois autres, `account.py` | `daily_cap_near`, `invalid_credentials`, `bootstrap_failed`, `account_unreadable`, `mfa_required` |

Les devices sont la clé de lisibilité des automatisations. « Quand un cours est
annulé » doit se poser à propos de *quelqu'un* : un device par enfant sous un
device parent qui porte l'établissement est ce qui rend les *device triggers*
compréhensibles dans l'éditeur. Les entités de diagnostic, elles, sont
rattachées au device **compte** et non à un enfant, parce que le budget est
partagé entre les enfants d'un même compte — le serveur voit une session et
une adresse IP, quel que soit le nombre d'enfants suivis.

---

## 3. Le limiteur de débit

`custom_components/carnet_scolaire/ratelimit.py` est le cœur du design. Il est
aussi le module le plus long après `sensor.py` et `gateway.py`, et le seul dont
le docstring d'ouverture énumère les propriétés porteuses avant le premier
`import` — parce que chacune d'elles corrige un défaut réel trouvé en revue,
non un risque hypothétique.

### 3.1 Ce qui est puni, et par quoi

Le tableau que le module reproduit en tête de fichier
(`ratelimit.py`) est ce qui explique sa forme :

| Sanction | Déclencheur | Gravité |
| --- | --- | --- |
| Session cassée | deux appels concurrents désynchronisent le compteur de requêtes chiffré | immédiate, récupérable |
| `Erreur.G = 8` ou `10` | session expirée par inactivité | bénigne |
| `Erreur.G = 25` | trop de requêtes d'*autorisation* | attendre, ne pas insister |
| Suspension d'adresse | connexions échouées répétées | coûteuse, et non documentée |

Seule la dernière fait vraiment mal, et elle ne vise pas les appels : elle vise
les **connexions échouées**. Un limiteur qui ne compterait que des requêtes
manquerait sa cible. C'est pour cela que l'authentification a sa propre
comptabilité ici, et non parce que c'est plus propre.

### 3.2 Trois couches pour les appels

Les trois couches s'appliquent dans l'ordre et un appel doit franchir les
trois (`RateLimiter.check`, `ratelimit.py`).

**Couche 1 — espacement minimal.** `min_request_interval` secondes entre deux
requêtes, mesurées sur une horloge monotone (`_spacing_wait`,
`ratelimit.py`). Défaut : 1,0 s, réglable de 0,2 à 10 s.

**Couche 2 — seau à jetons.** Capacité `burst_size`, remplissage continu à
`max_requests_per_hour / 3600` jeton par seconde (`_refill`,
`ratelimit.py`). Défauts : 20 jetons, 240 requêtes/heure. Le seau
démarre **plein**, pour ne pas pénaliser la première salve du simple fait que
l'intégration vient de démarrer. Et il est autorisé à devenir **négatif** : un
découvert n'est pas écrêté à zéro, sinon chaque dépassement était pardonné, une
salve d'appels coûteux ne coûtait rien à rembourser et le débit horaire ne
signifiait plus rien (`_record_requests`, `ratelimit.py`).

**Couche 3 — plafond journalier.** `max_requests_per_day`, réinitialisé au
changement de date locale, et *seulement vers l'avant* (`_roll_day`,
`ratelimit.py`). Défaut : 2000 requêtes, contre une consommation
nominale d'environ 180 par jour — un facteur onze, assumé comme filet de
sécurité contre un défaut logiciel et non comme contrainte d'exploitation.
Reculer la date — correction NTP, machine mal réglée au démarrage — adopte la
nouvelle date **sans rien remettre à zéro** : réinitialiser sur tout changement
de date re-accordait le budget entier *et* le plafond de connexions à chaque
recul d'horloge. Le seau, lui, est laissé tranquille dans les deux cas : il
lisse l'heure et n'a pas d'opinion sur la date.

Le plafond journalier ne refuse pas tout d'un coup : il **sacrifie par
priorité** (`_SHED_THRESHOLD`, `ratelimit.py`).

| Priorité | Seuil d'abandon | Paliers concernés |
| --- | --- | --- |
| `CRITICAL` | `inf` — jamais abandonné | `session` |
| `HIGH` | 1,0 | `timetable`, `homework` |
| `NORMAL` | 0,8 | `news`, `marks`, `attendance` |
| `LOW` | 0,6 | `discussions`, `evaluations`, `menus`, `static`, `history` |

Le seuil de `CRITICAL` est **infini, et non un grand nombre**. La raison est
arithmétique : `calls_today` n'a pas de plafond, donc tout seuil fini est
atteignable — un utilisateur qui règle `max_requests_per_day` à son plancher
documenté de 100 franchit 2,0 dès le premier jour — et l'atteindre refuserait
la connexion dont tous les autres paliers dépendent.

Un quatrième seuil, `CAP_WARNING_FRACTION = 0.8`, n'abandonne rien : il ouvre
un *repair issue* informatif. Le plafond journalier n'est jamais silencieux,
parce que des données qui vieillissent sans erreur au journal sont le symptôme
le plus difficile à diagnostiquer qui existe.

### 3.3 Deux compteurs pour les connexions

`may_login` (`ratelimit.py`) applique **les mêmes couches** qu'un appel
ordinaire, avec exactement une exception : une connexion n'est jamais
*sacrifiée* par le plafond journalier, parce que refuser de se connecter laisse
toutes les entités périmées sans issue. Elle reste espacée, reste tirée du seau
et reste comptée — une connexion coûte cinq à sept requêtes HTTP, et un budget
qui ne les verrait pas décrirait une autre intégration que celle qui tourne.

À cela s'ajoutent deux compteurs propres :

* **`logins_today`**, plafonné par `max_logins_per_day`, défaut **24**. Pas
  120 : le design attend une à trois connexions par jour, et un plafond de 120
  ne pourrait pas attraper le défaut pour lequel il existe.
* **`failed_logins_last_hour`**, plafonné par `max_failed_logins_per_hour`,
  défaut **3**. C'est le garde-fou de l'adresse IP. Atteint, il ouvre un
  `credentials_hold` d'une heure.

Deux détails de ces compteurs sont porteurs.

Le premier est **la classification de l'échec**, et c'est ce qu'il y avait de
plus important à ne pas rater dans ce module. Un mot de passe erroné ne lève
**pas** `PronoteAPIError` : le déchiffrement du challenge échoue et `_login`
lève `CryptoError`, ou bien `_login` renvoie `False` parce que la clé `cle` est
absente de la réponse `Authentification`. Branché sur la mauvaise exception, le
compteur censé protéger l'adresse ne se serait jamais incrémenté — le garde-fou
aurait existé dans la documentation et pas dans le code qui tourne. C'est ce
que l'énumération `LoginOutcome` (`ratelimit.py`) nomme explicitement,
avec six issues : `SUCCESS`, `BAD_CREDENTIALS`, `TRANSPORT`, `BOOTSTRAP`,
`MFA_REQUIRED`, `UNDECODABLE`.

Le second est que **toute tentative compte, réussie ou non**. Ne compter que
les succès laissait un compte défaillant tenter sans borne pendant que le
compteur censé l'arrêter restait à zéro.

Enfin, les requêtes d'une connexion sont imputées à la clé
`LOGIN_COST_KEY = "login"` et non à `str(Tier.SESSION)`
(`ratelimit.py`). Le palier `session` ne coûte rien — périodes, classe
et établissement arrivent avec la connexion — donc imputer là les cinq à sept
requêtes faisait lire le seul palier gratuit comme la chose la plus chère que
l'intégration fasse, sur un attribut de diagnostic qu'on demande aux
utilisateurs de consulter pour régler leur budget. Et `login` n'est pas un
palier : il n'est donc jamais candidat au sacrifice et n'apparaît pas dans
`TIER_PRIORITY`.

### 3.4 L'admission plutôt que la sortie

C'est la décision centrale du module, et elle mérite d'être énoncée
précisément.

La décision, l'attente et **le débit** ont lieu tous les trois sous un unique
`asyncio.Lock`, avant que la requête ne parte (`RateLimiter.call`,
`ratelimit.py`) :

```python
async with self._admission:
    verdict = self.check(tier, priority, cost=float(cost))
    if not verdict.allowed:
        raise TierDeferred(verdict.reason or DeferReason.HOURLY_BUDGET, verdict.wait)
    if verdict.wait > 0:
        await sleeper(verdict.wait)
    self.commit(tier, cost)

result = await fn()
```

Débité **à la sortie**, le comportement était le suivant : deux appelants — un
batch programmé et un service invoqué pendant ce batch — lisaient le même
`_last_call`, calculaient la même attente, la dormaient ensemble et tiraient à
la même milliseconde. Le résultat est le compteur de requêtes désynchronisé de
la première ligne du tableau du §3.1, **produit par la couche même qui existe
pour l'empêcher**. Charger à l'admission, sous le verrou, est ce qui transforme
la couche 1 d'une intention en une garantie.

La même logique vaut pour les connexions (`RateLimiter.login`,
`ratelimit.py`), avec une seconde raison qui lui est propre : une
annulation pendant la poignée de main — entrée déchargée, sauvegarde
d'options, arrêt — lève `CancelledError` à travers tous les `except` de
`session.py`, donc `note_login` n'était jamais atteint alors que le fil de
travail achevait quand même la connexion. Cinq à sept requêtes atteignaient
l'établissement et n'apparaissaient dans aucun compteur.

Corollaire assumé : **rien n'est jamais remboursé**. La requête est partie au
moment où quoi que ce soit peut échouer, et un limiteur qui ne compterait que
les succès laisserait une boucle défaillante tourner sans borne
(`commit`, `ratelimit.py`).

### 3.5 La réconciliation `GatewayResult.calls` ↔ limiteur

Le coût demandé au seau doit être le coût débité. Demander un jeton en
prélevant six laissait le palier `history` vider quatre fois la capacité de
salve par heure sans aucune attente, ce qui supprime exactement le lissage pour
lequel la couche 2 existe.

Mais le coût *a priori* déclaré par l'appelant n'est pas toujours le coût réel.
Chaque fonction de la passerelle renvoie donc un `GatewayResult` qui porte
`facts` **et** `calls` (`gateway.py`), et `SessionManager._reconcile`
(`session.py`) débite la différence via `RateLimiter.reconcile`
(`ratelimit.py`) :

```mermaid
sequenceDiagram
    autonumber
    participant T as tiers.py
    participant S as SessionManager
    participant L as RateLimiter
    participant G as gateway.py

    T->>S: run tier, priority, fn, cost=1
    S->>L: call tier, priority, fn, cost=1
    Note over L: sous _admission · check, attente, commit de 1
    L->>G: fn client
    G-->>L: GatewayResult facts, calls=4
    L-->>S: resultat
    S->>L: reconcile tier, charged=1, actual=4
    Note over L: debite 3 requetes supplementaires
    S-->>T: GatewayResult
```

C'est ce qui rend `GatewayResult.calls` **porteur et non décoratif**. Sans
cela, le limiteur ne voyait jamais que le coût déclaré, et un accès accidentel
à une propriété paresseuse — `Information.content`, `Period.grades`,
`Discussion.messages` — plaçait de vraies requêtes que l'espacement, le seau
et le plafond journalier manquaient tous les trois, la vérité n'apparaissant
que dans un champ de diagnostic que personne ne compare.

La réconciliation est **asymétrique** : un appel qui coûte *moins* que déclaré
n'est pas remboursé. Le coût déclaré est déjà sorti du seau, et un limiteur qui
rendrait du budget ferait d'une déclaration optimiste une autorisation de
salve.

Le cas concret est le palier `discussions` : `tiers.py` déclare
`cost=1`, mais `gateway.discussions` peut développer jusqu'à
`MAX_DISCUSSION_EXPANSIONS = 3` fils et renvoyer `calls=4`. La réconciliation
est la seule chose qui empêche ces trois requêtes d'être invisibles.

### 3.6 Heures creuses, maintiens et repli exponentiel

**Heures creuses.** Fenêtre `[quiet_end, quiet_start[` active, donc silencieuse
de 22:00 à 06:00 par défaut, traversée de minuit gérée explicitement
(`_in_quiet_hours`, `ratelimit.py`). Un batch commencé **hors** heures
creuses peut les finir : `begin_batch` / `end_batch` marquent la frontière et
`BATCH_GRACE_SECONDS = 300` borne l'exemption, sinon un batch bloqué la
tiendrait ouverte toute la nuit (`ratelimit.py`). Seul `CRITICAL`
ignore les heures creuses.

`quiet_seconds_between` (`ratelimit.py`) existe pour une raison
inattendue et importante : la péremption doit **exclure la nuit**. Avec les
heures creuses activées — le défaut — une pause de huit heures dépasse
`stale_after` fois l'intervalle de tous les paliers rapides ; sans cette
exclusion, l'emploi du temps, les devoirs, les actualités et la messagerie
passaient `unavailable` à 06:00 **chaque matin**. Une entité indisponible
déclenche des automatisations à faux, ce qui en fait la pire façon disponible
de dire « rien ne s'est passé cette nuit ».

Le calcul de durée passe par `_elapsed` (`ratelimit.py`), qui
convertit en UTC avant de soustraire : retrancher deux `datetime` conscients
partageant le **même** objet `tzinfo` fait sauter à CPython la recherche
d'offset, donc « 01:30 jusqu'à 06:00 » valait quatre heures et demie le jour du
passage à l'heure d'été là où trois heures et demie s'écoulent réellement.

**Maintiens.** Un maintien (`hold`) refuse tout appel pendant une durée. Quatre
raisons, hiérarchisées par `_HOLD_SEVERITY` (`ratelimit.py`) :
`BACKOFF` (1) < `MFA_HOLD` (2) < `BOOTSTRAP_HOLD` (3) <
`CREDENTIALS_HOLD` (4). Un maintien n'est **jamais remplacé** par un moins
grave : sans cet ordre, une seule erreur de transport dégradait un maintien
d'une heure en un repli de trente secondes, et le succès suivant l'effaçait —
le maintien s'évaporait donc purement et simplement.

Le maintien MFA mérite un mot. Quand PRONOTE réclame le code à deux facteurs
que cette intégration refuse délibérément de stocker, aucune quantité de
tentatives ne peut réussir, et chacune coûte cinq à sept requêtes. Laissé sans
borne, cela produisait des milliers de tentatives par jour — précisément le
geste qui fait suspendre une adresse. C'est donc un **maintien** et non un
simple drapeau, et sa seule sortie est `reset_after_reauth()`, c'est-à-dire un
humain qui fournit le code (`ratelimit.py`).

**Repli exponentiel.** `min(backoff_max, base × 2^(échecs-1)) × (0,5 + alea)`
(`backoff_delay`, `ratelimit.py`), défauts 30 s de base et 3600 s de
plafond. Le *full jitter* évite que plusieurs instances Home Assistant du même
établissement se resynchronisent sur le même créneau après une panne. L'exposant
est écrêté **avant** le décalage à `_MAX_BACKOFF_EXPONENT = 40`, parce que
calculer `2**1024` d'abord lève `OverflowError` — ce qui transformait un repli
profond en plantage.

`retry_delay()` (`ratelimit.py`) existe séparément, et sa raison est
subtile : `backoff_delay()` vaut zéro tant qu'il n'y a pas eu d'échecs
*consécutifs*, ce qui est exactement le cas d'une connexion refusée — mauvais
identifiants, code réclamé, bootstrap illisible laissent tous ce compteur à
zéro. Reporter de zéro seconde rend le palier dû au tick suivant, et dix
paliers tentent alors dix connexions par tick : le geste de connexions
échouées répétées qui est la seule sanction contre laquelle on ne peut pas
ruser.

Symétriquement, `note_success()` remet le compteur d'échecs **entièrement** à
zéro, pas progressivement : un serveur qui répond une fois est un serveur qui
fonctionne. Mais il ne lève pas le drapeau `throttled` de sa propre autorité —
`_maybe_clear_throttled` (`ratelimit.py`) vérifie qu'aucun maintien
n'est actif, que la fraction du plafond est sous le plus bas des seuils de
sacrifice et que le plafond de connexions n'est pas atteint. L'effacer
inconditionnellement à chaque succès rendait le plafond journalier silencieux :
un seul appel `HIGH` réussi remettait le drapeau à zéro pendant que les paliers
`LOW` et `NORMAL` étaient encore abandonnés, et `binary_sensor` correspondant
repassait à `off` tous les quarts d'heure.

### 3.7 L'état qui doit survivre à l'objet

`PronoteAccount` construit son propre `RateLimiter` dans `__init__`. Home
Assistant répond à un `ConfigEntryNotReady` par un retry avec un repli plafonné
à quatre-vingts secondes. Chaque tentative construisait donc un limiteur qui
n'avait jamais entendu parler du maintien d'une heure que la tentative
précédente venait d'ouvrir : un établissement en panne un week-end se voyait
demander une connexion fraîche toutes les quatre-vingts secondes pendant
soixante-et-une heures, contre un plafond configuré de vingt-quatre par jour,
pendant que `calls_today` rapportait cinq — parce que chaque rapporteur était
un objet différent.

`export_state` / `import_state` (`ratelimit.py`) et le magasin
`login_guard.limiter_state_store` (`login_guard.py`) sont la réponse.
Trois choix y sont explicites :

* les valeurs monotones voyagent telles quelles, ce qui est correct parce que
  le magasin vit dans `hass.data` et meurt avec le processus ;
* l'horloge murale ne voyage **que sous forme de date**, pour qu'importer
  l'état d'hier ne puisse pas ressusciter les compteurs d'hier ;
* la moitié **punitive** — connexions échouées, échecs consécutifs, maintien en
  cours — traverse la frontière du jour. Un maintien est une promesse de ne pas
  marteler un serveur qui vient de refuser, et minuit n'est pas une preuve que
  quoi que ce soit a changé.

`import_state` est délibérément tolérant et ne peut rien lever : une clé absente
garde le défaut neuf, un état malformé est ignoré. Le pire cas d'ignorer un
transfert est une connexion de plus ; le pire cas d'une exception serait une
entrée qui ne démarre pas.

Le second occupant de `login_guard.py` est le **garde du flow de
configuration** (`login_guard.py`) : un limiteur par instance Home
Assistant, et non par entrée, parce que ce qui est protégé est l'**adresse**.
La conséquence est divulguée plutôt que masquée : une connexion de flow et une
connexion de setup sont comptées par deux compteurs distincts, donc le
garde-fou borne chacun séparément à `max_failed_logins_per_hour` au lieu de
borner leur somme.

### 3.8 Les réglages, et leurs bornes

Tous validés dans `OPTION_RANGES` (`const.py`) et relus par
`options.build_rate_limit_config`.

| Option | Défaut | Bornes |
| --- | --- | --- |
| `min_request_interval` | 1,0 s | 0,2 – 10 |
| `max_requests_per_hour` | 240 | 30 – 2000 |
| `burst_size` | 20 | 5 – 100 |
| `max_requests_per_day` | 2000 | 100 – 20000 |
| `max_wait` | 60 s | 5 – 300 |
| `max_logins_per_day` | 24 | 5 – 500 |
| `max_failed_logins_per_hour` | 3 | 1 – 10 |
| `credentials_hold` | 3600 s | 300 – 86400 |
| `bootstrap_hold` | 3600 s | 600 – 86400 |
| `backoff_base` | 30 s | 5 – 300 |
| `backoff_max` | 3600 s | 60 – 21600 |
| `quiet_hours_enabled` | vrai | — |
| `quiet_start` / `quiet_end` | 22:00 / 06:00 | — |

`max_wait` est la frontière entre « attendre » et « reporter » : au-delà, le
palier est différé et conserve son instantané, plutôt que d'immobiliser la
boucle.

---

## 4. L'ordonnanceur

`custom_components/carnet_scolaire/scheduler.py` répond à une seule question : *que
faut-il collecter maintenant, et dans quel ordre ?* Comme le limiteur, il ne
connaît ni PRONOTE ni Home Assistant et prend une horloge injectable.

### 4.1 Un seul battement, et des échéances

Des minuteurs indépendants finissent par coïncider et produisent des salves.
Il y a donc **exactement un battement de cœur** — le *master tick*, armé par
`async_track_time_interval` dans `PronoteAccount.async_setup`
(`account.py`), toutes les cinq minutes par défaut, réglable de 1 à 60.
Il demande à l'ordonnanceur quels paliers sont dus, et le batch les exécute
dans une seule session, espacés par le limiteur.

Le point important est que chaque palier porte une **échéance** et non un
minuteur. Trois propriétés en découlent, qu'aucun jeu de minuteurs ne donne :

1. **La granularité du battement ne fixe pas le budget.** Le budget est fixé
   par les échéances. Augmenter la cadence du battement ne coûte donc rien —
   ce qui est dit explicitement dans `next_due_in`
   (`scheduler.py`) — alors qu'avec des minuteurs, la fréquence du
   déclencheur *est* la fréquence des requêtes.
2. **Un palier différé n'est jamais abandonné.** `defer`
   (`scheduler.py`) déplace son `not_before` et conserve son instantané
   précédent, donc ses entités gardent leur valeur. Et le report est **borné
   par l'intervalle du palier lui-même** : un limiteur qui annonce « réessayez
   à minuit » ne doit pas faire taire un palier de quinze minutes pendant onze
   heures si le budget se libère plus tôt.
3. **Réduire un intervalle ne déclenche pas de collecte immédiate.** L'échéance
   suivante est recalculée depuis la **dernière collecte**, pas depuis le
   moment où l'option a changé (`reconfigure`, `scheduler.py`). Le
   docstring est honnête sur la portée de cette garantie : elle est plus faible
   que « raccourcir un intervalle ne déclenche jamais un batch immédiat », qui
   est simplement faux — un palier collecté il y a quarante minutes sur un
   intervalle de soixante *est* dû dès que l'intervalle passe à quinze, et il
   doit l'être. Ce que la préservation achète, c'est que le changement soit
   mesuré depuis l'histoire réelle et non depuis la sauvegarde.

Un second verrou complète le dispositif côté compte : `_tick_lock`
(`account.py`). Si le batch précédent tourne encore, le tick est
**sauté**, pas mis en file. C'est correct parce que les échéances n'ont pas
bougé — le tick suivant reprendra les mêmes paliers — alors que mettre en file
laisserait un serveur lent construire un arriéré qui arriverait ensuite sous
la forme exacte de la salve que le battement unique existe pour empêcher.

### 4.2 Les dix paliers et leur coût

`Tier` (`const.py`) énumère onze valeurs : dix paliers de données, plus
`SESSION` qui n'en est pas un. `SESSION` porte les faits qui arrivent avec la
connexion elle-même — périodes, classe, établissement, lus dans `func_options`
et `parametres_utilisateur`, déjà en mémoire une fois authentifié — et coûte
donc **zéro appel**.

```mermaid
flowchart LR
  subgraph CRIT["CRITICAL · jamais sacrifie"]
    SESSION["session · 0 req · gratuit avec la connexion"]
  end

  subgraph HIGH["HIGH · sacrifie a 100 pct du plafond"]
    TT["timetable · 15 min · 2 req"]
    HW["homework · 30 min · 1 req"]
  end

  subgraph NORM["NORMAL · sacrifie a 80 pct"]
    NEWS["news · 60 min · 1 req"]
    MARKS["marks · 180 min · 2 req"]
    ATT["attendance · 360 min · 1 req"]
  end

  subgraph LOW["LOW · sacrifie a 60 pct"]
    DISC["discussions · 60 min · 2 req en moyenne, jusqu a 4"]
    EVAL["evaluations · 720 min · 1 req"]
    MENUS["menus · 1440 min · 1.14 req"]
    STATIC["static · 1440 min · 1 req"]
    HIST["history · 1440 min · 4 req par periode close"]
  end

  TICK["master tick · 5 min par defaut"]
  TICK --> CRIT
  TICK --> HIGH
  TICK --> NORM
  TICK --> LOW
```

Le détail des coûts, tel que le code les déclare et les dépense :

| Palier | Intervalle | Priorité | Coût déclaré | Coût réel possible | Requête protocolaire |
| --- | --- | --- | --- | --- | --- |
| `session` | — | `CRITICAL` | 0 | 0 | aucune, lu en mémoire |
| `timetable` | 15 min | `HIGH` | 2 | 2, ou moins en bordure d'année scolaire | `PageEmploiDuTemps` 16, une par semaine |
| `homework` | 30 min | `HIGH` | 1 | 1 | `PageCahierDeTexte` 88, l'année entière |
| `news` | 60 min | `NORMAL` | 1 | 1 | `PageActualites` 8 |
| `discussions` | 60 min | `LOW` | 1 | 1 à 4 | `ListeMessagerie` 131, plus `ListeMessages` par fil développé |
| `marks` | 180 min | `NORMAL` | 2 | 2 | `DernieresNotes` 198, plus `PageBulletins` 13 |
| `attendance` | 360 min | `NORMAL` | 1 | 1 | `PagePresence` 19 |
| `evaluations` | 720 min | `LOW` | 1 | 1 | `DernieresEvaluations` 201 |
| `menus` | 1440 min | `LOW` | 1 | 1 ou 2 | `PageMenus` 10, par semaine |
| `static` | 1440 min | `LOW` | 1 | 1 | équipe pédagogique |
| `history` | 1440 min | `LOW` | 4 par période close | idem | les quatre onglets ci-dessus, par période |

Plusieurs de ces chiffres sont le résultat d'un comptage sur le code amont, et
non d'une déduction :

* **`timetable` à 2.** `Client.lessons()` boucle `for week in range(...)` et
  poste `PageEmploiDuTemps` une fois **par semaine**, puis filtre côté client.
  Demander « aujourd'hui et demain » coûte donc exactement la même requête que
  demander la semaine entière — ce qui a fait replier le palier « semaine »
  dans celui-ci. Le palier a longtemps coûté 1,14 : une semaine, plus la
  suivante le seul jour où *demain* la franchissait, c'est-à-dire le dimanche.
  Le lundi restait donc inconnu pendant tout le week-end. L'horizon est
  maintenant les deux semaines à chaque collecte, et le coût déclaré est
  l'ordre de grandeur haut : la passerelle écarte une semaine hors année
  scolaire (`gateway.PronoteGateway._timetable_weeks`) et dépense alors moins
  que déclaré, ce que le limiteur ne rembourse pas — délibérément.
* **`marks` à 2, avec quatre jeux de données pour une requête.** En amont,
  `Period.grades`, `.averages`, `.overall_average` et `.class_overall_average`
  re-postent chacun `DernieresNotes` avec le même corps. La passerelle poste
  une fois et décode les quatre (`gateway.py`). Le second appel est le
  bulletin de la période courante.
* **`attendance` à 1, avec trois jeux de données.** `PagePresence` renvoie
  absences, retards et punitions dans une seule liste `listeAbsences`,
  discriminées par le champ `G` valant 13, 14 ou 41. En amont, les trois
  propriétés re-postent la même requête et lisent la même liste.
* **`discussions` à 2 en moyenne.** La liste des fils est une requête. Les
  corps de messages ne le sont pas : lire `Discussion.messages` poste
  `ListeMessages` **à chaque lecture**, donc développer tous les fils coûterait
  une requête chacun — dix fils sur un palier horaire font environ 176 requêtes
  par jour, ce qui double à peu près le budget entier. Seuls les fils dont le
  compteur de non-lus a **augmenté** sont développés, plafonnés à
  `MAX_DISCUSSION_EXPANSIONS = 3` par cycle (`gateway.py`).
* **`history` à 4 par période close.** `DernieresNotes` 198, `PageBulletins`,
  `PagePresence` 19 et `DernieresEvaluations` 201 — soit huit requêtes pour
  deux périodes closes, ce que `REQUESTS_PER_BATCH` reflète
  (`options.py`). Une période close ne peut pas changer, donc la relire
  toutes les trois heures dépenserait des requêtes sur un résultat constant :
  c'est pourquoi ce palier est quotidien, et pourquoi il n'émet aucun
  événement.

Un mécanisme fin mérite d'être signalé sur `discussions` : un fil que la
passerelle n'a **pas** développé conserve son ancien compteur de non-lus
(`tiers.py`). Enregistrer le nouveau compteur pour tous les fils
signifiait qu'un fil poussé au-delà du plafond de développement ne redevenait
jamais éligible — son compteur ne remontait plus jamais depuis la valeur
stockée — et tous les messages qu'il recevait ensuite arrivaient avec
`author: null` et `created: null`.

### 4.3 L'ordre de service, et le bouton

`due()` (`scheduler.py`) rend les paliers dus **par priorité
décroissante**, puis par retard décroissant, puis par nom. L'ordre compte
quand le budget se resserre : le limiteur sacrifie par priorité, donc servir
dans l'ordre de priorité garantit que ceux qui tombent sont ceux que la table
de sacrifice a désignés.

Le bouton de rafraîchissement et le service `refresh` n'appellent rien : ils
**élèvent la priorité** des paliers demandés pour le prochain battement
(`request`, `scheduler.py`), puis réveillent le tick
(`account.async_request_tick`, `account.py`). Un bouton pressé dix fois
en une minute coûte **un** batch, pas dix, parce que la deuxième pression
trouve les paliers déjà demandés et le tick déjà en cours.

Le détail du calcul de l'élévation vaut la peine d'être lu
(`scheduler.py`) : un *boost* remonte le palier d'un rang, **plancher
juste au-dessous de `CRITICAL`**. Lui donner le rang −1 le faisait surclasser
le palier session, donc un bouton de rafraîchissement sur `history` — six
requêtes, priorité `LOW` — était servi le premier, vidait le seau et
l'allocation `max_wait`, et `timetable`, de priorité `HIGH`, était ensuite
différé : l'inverse exact de l'ordre de sacrifice. Le bouton demande un
laissez-passer de priorité, pas la suprématie.

Aucun bouton n'offre de forcer une **reconnexion** (`button.py`) :
c'est le seul geste capable de faire suspendre une adresse, donc il n'est pas
offert.

### 4.4 Survivre à un rechargement

Une sauvegarde d'options recharge l'entrée, et un rechargement construit un
ordonnanceur neuf dont chaque `last_collected` vaut `None` — ce qui rend les
dix paliers immédiatement dus. `export_state` / `import_state`
(`scheduler.py`) transportent les instants de dernière collecte à
travers le rechargement, via `hass.data` sous une clé propre
(`account._saved_schedule`, `account.py`) : c'est de l'état d'exécution
qui n'a rien à faire sur disque, et il n'a de sens que dans un seul processus
puisque les valeurs sont monotones.

`import_state` **écarte une valeur venue du futur** : cela signifie que
l'origine monotone a bougé — un redémarrage plutôt qu'un rechargement — donc
la valeur ne doit pas être crue.

Le docstring de `export_state` note l'histoire de cette correction :
préserver `last_collected` dans `reconfigure` était la bonne idée visant la
mauvaise couture, parce que rien n'appelait jamais `reconfigure` — Home
Assistant recharge, il ne reconfigure pas.

### 4.5 La péremption

`is_stale` (`scheduler.py`) est la frontière du régime de fraîcheur :
en dessous de `stale_after` fois l'intervalle du palier, une entité **garde sa
valeur** et signale son âge ; au-delà — ou sans donnée du tout — elle devient
`unavailable`. Défaut : `stale_after = 6`, réglable de 2 à 48.

Le paramètre `excused` retranche le temps que le compte n'a délibérément pas
collecté, en pratique les heures creuses. Ce n'est pas une commodité : voir
§3.6. Et `PronoteAccount.is_stale` (`account.py`) ajoute une exception
— le palier `session` n'est **jamais** périmé, parce qu'il porte ce que la
connexion a fourni (l'enfant, la classe, la liste des périodes), qui change une
fois par année scolaire et non sur un intervalle.

---

## 5. La session

Trois modules se partagent le cycle de vie de la session :
`session.py` (propriété du client et politique de reconnexion), `account.py`
(orchestration) et `login_guard.py` (l'état punitif qui doit survivre aux
objets).

### 5.1 Sérialisation : un fil, deux verrous

Le protocole numérote ses requêtes avec un compteur chiffré :
`_Communication.post` chiffre `request_number`, l'écrit aussi dans le chemin de
l'URL, et avance de deux. **Deux appels concurrents sur une même session
désynchronisent ce compteur et cassent la session.** Et
`hass.async_async_add_executor_job` utilise un pool de fils partagé, donc deux
travaux lancés ensemble *sont* concurrents.

D'où : un `ThreadPoolExecutor(max_workers=1)` et un `asyncio.Lock` **par
entrée de configuration** (`SerialExecutor`, `session.py`).

Le verrou couvre plus que l'appel, pour une raison précise. `set_child()`
**mute** le client : il réécrit
`parametres_utilisateur["dataSec"]["data"]["ressource"]`, qui est le même
emplacement que `Client.lessons()` lit pour construire son corps de requête
(`hardened_client.py`). L'unité de travail atomique est donc le couple
**`(enfant, palier)`** et jamais le palier seul — sinon un compte parent publie
l'emploi du temps d'un enfant sous les entités de l'autre, silencieusement.
C'est ce que `_bind` (`session.py`) matérialise : sélection de
l'enfant, appel de l'onglet et construction du DTO dans une seule fermeture
indivisible.

Un **second** verrou, `_gate` (`session.py`), est tenu à travers
« choisir un client, attendre le budget, appeler, réessayer » comme un tout.
Sans lui, un service invoqué pendant un tick capturait un client, attendait son
espacement, et se réveillait pour poster sur un client qu'un retry concurrent
sur `Erreur.G = 10` avait déjà fermé — produisant un échantillon d'expiration
fallacieux, une troisième connexion et un palier en échec à partir d'un seul
chevauchement.

L'arrêt de l'exécuteur n'appelle **jamais** `shutdown(wait=True)` depuis la
boucle d'événements. Un fil peut être au milieu d'une requête au moment du
déchargement, et le joindre gèlerait le rechargement de Home Assistant.

Enfin, `SerialExecutor.run` impose une **échéance** en plus du timeout HTTP :
`read_timeout × CALL_TIMEOUT_FACTOR`, soit 180 s par défaut. Elle n'est pas
redondante : elle borne le seul cas que le régime de péremption déguiserait,
un appel qui ne revient jamais, dont le symptôme est des entités qui
vieillissent en silence. Le docstring est précis sur ce qui se passe ensuite,
parce que la supposition évidente est fausse : la fonction abandonnée continue
de tourner dans le worker, mais le pool n'a qu'un fil, donc la soumission
**suivante** ne tourne pas à côté d'elle — elle attend dans la file du pool et
y consomme sa propre échéance. Rien ne peut donc désynchroniser le compteur
chiffré par ce chemin. Le coût réel est un blocage, ce qui est pourquoi
l'appelant marque le compte comme défaillant au lieu de réessayer.

### 5.2 La stratégie de session

Deux modes sont offerts en option d'entrée
(`SessionStrategy`, `const.py`) :

* **`LAZY`** — le défaut et la valeur recommandée : garder la session, et ne se
  reconnecter que lorsque le serveur dit qu'elle a expiré. Quels codes le
  disent est `SESSION_EXPIRED_CODES` et non un nombre unique : voir § 5.4.
* **`PER_BATCH`** — une connexion par batch. C'est ce que la spécification v1
  prescrivait, conservé comme échappatoire explicite pour un établissement dont
  la politique serait inhabituelle et qu'on préfère épingler plutôt que
  découvrir.

La défendabilité de `LAZY` repose sur un **argument de dominance**, et il faut
l'énoncer précisément parce qu'une version approximative est fausse.

`LAZY` n'est pas gratuit dans le mauvais cas. Si le délai d'inactivité de
l'établissement est *plus court* que l'intervalle du palier le plus rapide,
chaque batch dépense une requête perdue à découvrir que la session est morte
avant de se reconnecter — soit une requête par batch **de plus** que
`PER_BATCH`, et non l'égalité.

Ce qui borne ce mauvais cas est la **dégradation** : trois expirations
consécutives (`SHORT_TIMEOUT_EVIDENCE = 3`, `session.py`) font conclure
au module que le délai est court, et il se reconnecte alors avec anticipation.
La perte est donc de trois requêtes, une fois, puis d'une par jour venant de la
sonde qui garde la conclusion falsifiable
(`PROBE_INTERVAL_SECONDS = 86400`). Sous cette borne, `LAZY` n'est jamais
significativement pire pour aucune valeur du paramètre inconnu, et il est
spectaculairement meilleur pour la valeur commune.

Trois, et pas un : une expiration peut être un redémarrage de serveur, deux
peuvent être une coïncidence.

La dégradation ne fonctionne que si un succès ne la défait pas à la légère.
Lever la conclusion exige une **contre-preuve** — une session qui a survécu à
un intervalle d'inactivité au moins aussi long que le plus court de ceux qui
avaient tué une session — et non simplement le fait que l'appel suivant une
connexion fraîche réussisse, ce qui est toujours le cas
(`_note_survival`, `session.py`). Sans cela, la conclusion ne survivait
pas à un seul appel et la stratégie oscillait indéfiniment, payant une requête
supplémentaire à chaque batch.

La mesure elle-même est un minimum, pas une moyenne
(`SessionLifetime.observed_minutes`, `models.py`) : une expiration
courte prouve que le délai *peut* être aussi court, tandis qu'une longue prouve
seulement qu'il n'a pas été éprouvé. Et les intervalles inférieurs à
`MIN_LIFETIME_SAMPLE_SECONDS = 60` sont comptés comme expirations mais **pas**
enregistrés comme échantillons de durée de vie : le premier appel après une
connexion peut échouer en `Erreur.G = 10` pour des raisons étrangères à un
délai d'inactivité, et un tel échantillon épinglerait la durée mesurée près de
zéro pour le reste de l'exécution.

Un piège de comptabilité mérite d'être mentionné, parce qu'il a coûté cher :
`PER_BATCH` signifie une connexion par **batch**. Comparer la stratégie seule
rendait `_reconnect_required()` vrai à chaque `run()`, et `run()` est appelé une
fois par couple `(enfant, palier)` — donc un tick d'un compte à deux enfants
avec trois périodes closes demandait **36 connexions complètes** contre un
plafond de 24, et l'intégration mourait avant la fin de son premier batch. D'où
`_session_batch_id` (`session.py`).

### 5.3 Le cycle de vie, en machine à états

```mermaid
stateDiagram-v2
    [*] --> Setup

    state Setup {
        [*] --> Probe
        Probe --> Shape : connexion reussie
        Shape --> [*] : enfants, periodes, periode courante
    }

    Setup --> Connectee : runtime_data assigne, plateformes forwardees
    Setup --> Reauth : InvalidCredentials ou MfaRequired
    Setup --> NotReady : Bootstrap, Unreadable, LoginRefused, PronoteAPIError, OSError

    state Connectee {
        [*] --> Ouverte
        Ouverte --> Ouverte : appel reussi, note_success
        Ouverte --> Expiree : Erreur G egal 10
        Expiree --> Ouverte : reconnexion immediate et rejeu de l appel
        Ouverte --> Degradee : trois expirations consecutives
        Degradee --> Ouverte : contre preuve, gap superieur au minimum mesure
        Degradee --> Degradee : sonde quotidienne
    }

    Connectee --> Penalite : maintien ouvert par le limiteur
    state Penalite {
        [*] --> Backoff
        Backoff : erreur de transport ou Erreur G egal 25
        MfaHold : PIN reclame, aucune tentative ne peut reussir
        BootstrapHold : page de bootstrap sans bloc Start ou reponse indecodable
        CredentialsHold : trois connexions echouees dans l heure
    }

    Penalite --> Connectee : maintien expire, ou note_success sur un backoff
    Penalite --> Reauth : MfaHold ou CredentialsHold vus par l utilisateur

    Reauth --> Setup : reset_after_reauth puis rechargement de l entree
    NotReady --> Setup : retry Home Assistant, repli plafonne a 80 s

    Connectee --> Dechargee : async_unload_entry
    Dechargee : etat du limiteur et de l ordonnanceur exportes vers hass.data
    Dechargee --> [*]
```

Les transitions de `Penalite` vers `Connectee` ne sont pas symétriques : un
`Backoff` est levé par le premier succès (`note_success`), tandis qu'un
`MfaHold` ou un `CredentialsHold` attend soit l'expiration de sa durée, soit un
geste humain. `reset_after_reauth()` (`ratelimit.py`) efface les
compteurs d'échecs, les échecs consécutifs et le maintien courant, et c'est la
**seule** sortie du maintien MFA — ce qui est correct, puisque ce maintien
existe précisément parce qu'une personne doit agir.

`clear_login_penalties` (`login_guard.py`) nettoie les **trois**
détenteurs de cet état : le garde du flow, l'état sauvegardé pour l'entrée en
réparation, et le compte vivant si l'entrée se trouve chargée.

### 5.4 La gestion des erreurs protocolaires

`_handle_protocol_error` (`session.py`) traite trois familles de codes
`Erreur.G` et rien d'autre.

**`G = 8` ou `G = 10`, session expirée** (`SESSION_EXPIRED_CODES`). La seule
erreur qui vaille la peine d'être réessayée, et la seule raison pour laquelle
la stratégie paresseuse est mesurable. Le module enregistre l'échantillon de
durée de vie, ferme le client, en construit un neuf et rejoue l'appel — à
travers le limiteur, donc en payant le coût.

Les deux codes, et non le seul que documente la spécification : un
établissement réel a répondu `8`, avec pour `Erreur.Titre` « La page a
expiré ! », c'est-à-dire le même énoncé en d'autres mots. `pronotepy` n'a pas
de libellé pour `8` et le rend en « Unknown error from pronote: 8 » — une
chaîne, pas une classification ; le code, lui, est bien porté par l'exception.
N'avoir reconnu que `10` a coûté sept heures de données figées, et la forme de
cette panne est ce qui justifie le reste de ce paragraphe : la session était
morte côté serveur alors que `is_open` répondait encore vrai, aucune branche
n'atteignait donc `_reopen`, chaque tentative échouait, chaque échec creusait
le repli exponentiel, et aucun appel ne pouvait réussir pour le remettre à
zéro. Ce n'est pas une pause qui se résorbe : c'est un puits, dont seul un
redémarrage sort l'instance.

Le rejeu est **borné à un** (`_replay_on_a_fresh_session`), et cette borne est
ce qui rend l'élargissement de l'ensemble prudent plutôt que téméraire. Chaque
code de l'ensemble est une affirmation sur ce que le serveur voulait dire, et
une affirmation peut être fausse ailleurs. Réessayé sans plafond, un code mal
classé ne coûte pas un capteur périmé : il coûte une connexion par palier et
par tick — dix paliers contre un plafond de vingt-quatre épuisent la journée
en trois ticks puis continuent — c'est-à-dire exactement le geste des
connexions échouées répétées, la seule sanction que l'annexe B § 1 déclare
incontournable. Un second refus sur une session ouverte à l'instant part donc
au repli.

**Une session sans succès depuis une heure est abandonnée**
(`PRESUMED_DEAD_AFTER_SECONDS`). C'est la forme générale du correctif
précédent, pour le prochain code que personne n'a encore vu. `is_open` répond
« est-ce que je détiens un client », un fait sur ce processus qui n'énonce rien
sur le serveur. Un code inconnu, un serveur qui cesse de répondre sans le dire,
un établissement qui invalide les sessions selon son propre calendrier : tout
cela est indistinguable d'ici, et tout cela est rattrapé en refusant de faire
confiance à une session qui n'a pas fonctionné depuis une heure. Une heure est
dérivée et non devinée — le plafond de connexions par défaut est de 24 par
jour, soit une par heure, donc un seuil au-dessus de 3600 s ne peut jamais
être la raison pour laquelle ce plafond est atteint. Le contrôle est
paresseux, au prochain travail à faire : une nuit d'heures calmes provoque une
connexion supplémentaire à 06:00, pas une par heure.

**`G = 22`, objet d'une session précédente.** Reconnue **par type et non par
code**. `_Communication.post` lève `ExpiredObject` sur sa propre branche,
*avant* celle qui attache `pronote_error_code`, donc cette erreur arrive sans
code du tout. Ne tester que le code rendait toute cette branche morte, et un
`G = 22` tombait dans le repli générique — immobilisant un serveur parfaitement
sain parce que *nous* avions laissé fuir un objet à travers la frontière de
session. Ce cas est journalisé comme un bug de cette intégration, et il est
relevé : la frontière DTO l'exclut par construction, donc son apparition est un
défaut à signaler et jamais un incident réseau.

**`G = 25`, trop de requêtes d'autorisation.** Va au repli et ne provoque
**aucune** reconnexion. C'est précisément ce que le client durci existe pour
empêcher : `ClientBase.post` en amont répondrait à une sanction sur les requêtes
d'autorisation… par une requête d'autorisation.

Tout autre `PronoteAPIError` déclenche `note_failure()` et est relevé tel quel.

Les autres familles d'erreurs sont classées à la connexion (`_login`,
`session.py`), et le classement importe plus qu'il n'y paraît :

| Exception amont | Issue `LoginOutcome` | Conséquence |
| --- | --- | --- |
| `MFAError` | `MFA_REQUIRED` | maintien, puis flow de ré-authentification demandant le PIN |
| `CryptoError` | `BAD_CREDENTIALS` | compte contre le garde-fou IP |
| `logged_in is False` | `BAD_CREDENTIALS` | idem — clé `cle` absente de la réponse |
| `BootstrapUnavailable` | `BOOTSTRAP` | maintien d'une heure, repair sans cause nommée |
| `DataError`, `ValueError`, `KeyError`, `TypeError`, `IndexError` | `UNDECODABLE` | maintien de type bootstrap ; ne touche **jamais** le garde-fou IP |
| `PronoteAPIError` | `TRANSPORT` | relevée, devient `ConfigEntryNotReady` |
| `OSError` (dont `requests.Timeout`) | `TRANSPORT` | repli exponentiel |
| `ChildNotFound` | — | `IntegrationFault`, jamais imputé au repli du serveur |

Deux pièges sont documentés dans le code et valent d'être répétés.

`_TRANSPORT_ERRORS` vaut `(OSError,)` et non `(TimeoutError, ...)` : aucune des
défaillances que `requests` lève n'est un `PronoteAPIError`, et — le piège —
aucune n'est un `TimeoutError`, parce que `requests.Timeout` hérite de
`OSError`. Donc `except TimeoutError` n'attrapait **rien**, et une école en
panne un week-end produisait un GET de bootstrap non compté toutes les cinq
minutes, sans aucun repli, pendant que `calls_today` rapportait zéro.

`ChildNotFound` est un `PronoteAPIError` en amont, donc sans traitement séparé
il ouvrait un maintien de repli sur un serveur parfaitement sain parce que
*cette* intégration avait demandé un identifiant d'enfant qui n'est plus sur le
compte.

### 5.5 La rotation des identifiants

En mode QR code, PRONOTE renvoie un `jetonConnexionAppliMobile` **frais à
chaque `Authentification`** et `pronotepy` écrase `self.password` avec lui. Ne
pas le re-persister signifie perdre l'accès au démarrage suivant.
`_persist_rotated_credentials` (`session.py`) met à jour la mémoire
puis appelle le rappel de persistance, et `PronoteAccount._async_persist_credentials`
(`account.py`) écrit dans l'entrée de configuration.

Un échec d'écriture est **bruyant**. L'avaler silencieusement laissait le
serveur avec le nouveau jeton, la mémoire avec le nouveau jeton, et le stockage
avec un jeton mort — avec pour seul symptôme, au redémarrage suivant, un QR
code à rescanner.

C'est aussi une raison pour laquelle la stratégie paresseuse compte au-delà de
l'arithmétique : à une connexion par jour, il y a **une** fenêtre par jour dans
laquelle un arrêt brutal entre la rotation côté serveur et l'écriture locale
laisse un jeton mort. À soixante-quatre connexions par jour, il y en avait
soixante-quatre.

### 5.6 L'arrivée d'un enfant, sans rechargement

Un élève inscrit sur un compte parent en cours d'année n'était visible
qu'après un rechargement de l'entrée : la liste des enfants est lue une fois,
par `PronoteAccount._async_load_session_facts`, et le palier `SESSION` est
exclu des paliers collectés — aucun tick ne revenait donc la lire. Recharger
n'est pas un correctif : cela jette tous les instantanés que le compte
détient.

La liste est relue **au moment où une connexion a lieu**, jamais par une sonde
à elle. (`SessionManager._announce_children`, `session.py`) prévient le
connecteur après chaque connexion réussie, et son cache est mis à niveau par
(`PronoteConnector._note_children`, `pronote.py`). Cela ne coûte aucune requête : les enfants arrivent
dans la charge utile de l'`Authentification` que la connexion a déjà payée. Une
règle qui serait allée regarder à intervalle régulier aurait dépensé une
connexion — cinq à sept requêtes sur un plafond de vingt-quatre par jour —
pour un évènement qui survient une ou deux fois dans une scolarité.

(`PronoteAccount._async_adopt_announced_children`, `account.py`) compare en fin
de lot, hors du verrou de session que le lot tenait. Un identifiant qu'il n'a
pas encore vu relance l'appairage de
(`PronoteAccount._async_pair_children`, `account.py`), qui seul décide. Trois
choix méritent d'être énoncés :

* **La sélection est rangée en clés frappées**, jamais en identifiants de
  ressource. Rangée en identifiants, elle suivait la valeur même que PRONOTE
  renouvelle : chaque connexion qui renommait l'enfant en faisait un « nouveau
  venu », son identifiant était ajouté, et un compte à un enfant en a compté
  onze, dont aucun ne correspondait plus. La table des clés garde maintenant
  *chaque* enfant annoncé, suivi ou non : un enfant décoché dans le flow y a
  sa fiche et reste hors de la sélection, si bien qu'une rotation, reconnue
  par la fiche, ne le fait jamais revenir. Une sélection encore en
  identifiants est traduite une fois, par (`followed`, `child_keys.py`).
* **Inconnu n'est pas refusé.** Un enfant que la table n'a jamais vu n'a
  jamais été décliné — la sélection a été choisie parmi les enfants qui
  existaient *alors*. Il reçoit une clé, est suivi, et sa clé rejoint la
  sélection, réécrite dans l'entrée.
* **Une écriture de donnée ne recharge plus.** Seul le formulaire d'options
  recharge (`PronoteOptionsFlow`, `config_flow.py`), et il ne voit que les
  options : une écriture dans `entry.data` ne réveille plus rien. Du temps de
  l'écouteur de mise à jour, qui se déclenchait à toute écriture, il fallait une
  garde ; sans elle, appairer l'enfant rechargeait l'entrée — exactement ce que
  la manœuvre cherche à éviter.

Côté entités, (`async_add_per_student`, `entity.py`) garde le rappel
`async_add_entities` vivant et rejoue la différence à chaque publication du
coordinateur `SESSION`. La différence porte sur la **clé frappée**, jamais sur
l'identifiant PRONOTE : comparer des identifiants ferait lire chaque rotation
(§5.5) comme une arrivée, et fabriquerait un second appareil pour un enfant qui
en a déjà un. L'appareil n'est jamais créé explicitement — il apparaît parce
qu'une entité porte le `DeviceInfo` qui le nomme.

Les **périodes** tournent comme le reste : le `N` de chaque entrée de
`ListePeriodes`, et le `periodeParDefaut` qui désigne la période en cours, est
rechiffré à chaque connexion. Or les faits de session qui les portent ne sont
relus qu'à la mise en place et quand l'appairage recommence — pas à chaque
connexion, qui peut survenir au milieu de n'importe quel appel. Chaque requête
bornée à une période (notes, bulletin, présences, évaluations, périodes closes)
repasse donc par (`PronoteGateway.in_this_session`, `gateway.py`), dans l'appel
et sur le client qui le place : la période y est retrouvée **par sa position**,
et le `N` posté est celui de la session en cours. Sans cela, le lot qui se
reconnectait postait le `N` de la session précédente sur un compte parent, et
un compte élève — dont la liste d'enfants ne change jamais, donc dont les faits
n'étaient jamais relus — le faisait à chaque collecte jusqu'au redémarrage.
Aucune requête de plus : `ListePeriodes` est déjà en mémoire depuis la
connexion. Une position que la session ne liste plus fait échouer le palier
(`PeriodNotInSession`, `gateway.py`) plutôt que de poster l'ancien `N` ou de
publier une collecte vide.

Le retrait automatique reste hors sujet : voir `stale-devices` dans
`quality_scale.yaml`, et `async_remove_config_entry_device` pour la suppression
manuelle d'un appareil périmé.

---

## 6. La passerelle

`custom_components/carnet_scolaire/gateway.py` est l'adaptateur : une fonction
publique par **appel protocolaire**, des DTO gelés en sortie. Avec
`hardened_client.py`, c'est le seul module à importer les types de données
`pronotepy`, donc le seul à relire lors d'un changement de version et le seul
point à doubler dans les tests.

### 6.1 Deux règles de décodage

**Dédupliquer sans réimplémenter.** La spécification v1 concluait qu'il fallait
décoder les réponses brutes à la main, ce qui contredisait son propre refus de
reprendre la dette de décodage amont — puis la reprenait. Or les classes de
données de `pronotepy` acceptent le dictionnaire nu : `Average(json)`,
`Absence(json)`, `Delay(json)`, `Evaluation(json)`, `Report(data)`, et
`Lesson(client, json)` / `Punishment(client, json)` avec un client. La forme est
donc : **un `post()` brut par onglet, les sous-listes confiées aux classes
amont, puis conversion en DTO gelés**. On obtient la déduplication *et* les
correctifs amont.

**Une exception : `Grade`.** Deux raisons indépendantes, toutes deux
consignées dans le docstring d'ouverture (`gateway.py`). D'abord,
`Grade.__init__` résout `self.period` à travers
`Util.get(Period.instances, id=p)[0]`, et `Period.instances` est un attribut de
classe jamais vidé — le seul lecteur de ce registre dans tout
`dataClasses.py`. L'utiliser nous lierait à un global qu'on ne peut ni garder
(il fuit un client mort par période) ni vider (`[0]` sur une liste vide lève
`IndexError`, enveloppé en `ParsingError`, faisant échouer tout le batch de
notes). Ensuite, `Util.grade_parse` a **déjà** remplacé `|1`…`|8` par
`"Absent"`, `"Dispense"`… donc `Grade.grade` est déjà lossy, et décoder
`note.V` nous-mêmes est la seule route vers la sentinelle brute dont
`GradeStatus` a besoin. C'est une exception bornée, pas une politique — et
`Average` est la seconde, forcée par un conflit frontal avec la tolérance par
entrée.

Ce couplage a une conséquence non évidente : la confinement du registre de
périodes dans `hardened_client._ConfinedRegistry` est **sûr précisément parce
que** la passerelle décode les notes elle-même et ne construit donc jamais de
`pronotepy.Grade`. Si un changement futur se met à utiliser `pronotepy.Grade`,
il faudra revisiter le confinement en même temps.

### 6.2 La tolérance par entrée

C'est la propriété la plus visible de ce module en exploitation : **une entrée
illisible coûte cette entrée, jamais le palier entier.**

Elle est nécessaire parce que `pronotepy` construit ses listes dans des
compréhensions avec des résolveurs *stricts* : un seul élément servi sans un
champ optionnel levait `ParsingError` depuis l'intérieur de la compréhension et
faisait échouer **tout** le palier — emportant avec lui la liste de devoirs, le
calendrier et les deux capteurs correspondants.

Le mécanisme est un tuple partagé, `_ENTRY_ERRORS` (`gateway.py`), et
chaque membre y est pour une raison nommée :

| Exception | Pourquoi |
| --- | --- |
| `ParsingError` | l'enveloppe amont, levée par `Object._resolver` quand un champ *strict* manque |
| `DataError` | son parent, et **pas** un `PronoteAPIError`, donc jamais vu par la gestion d'erreurs protocolaires |
| `ValueError` | `strptime` sur une date écrite à la façon de l'établissement, `float()` sur ce qui n'est pas un nombre |
| `KeyError` | une lambda de conversion indexant une clé qui a bougé ; le résolveur amont ne garde que le *chemin*, jamais le corps du convertisseur |
| `IndexError` | `Util.grade_parse` fait `grade_translate[int(s[1]) - 1]` sur une table de huit, donc une future sentinelle `\|9` indexe hors bornes |
| `TypeError` | un segment de chemin valant `null` plutôt qu'absent, que la garde `KeyError` amont ne couvre pas |
| `ZeroDivisionError` | `Lesson.__init__` calcule `place % (len(end_times) - 1)`, donc un établissement publiant une seule entrée `ListeHeuresFin` divise par zéro |

Le tuple est partagé parce que `IndexError` était présent sur deux décodeurs et
absent de cinq.

Sept décodeurs l'utilisent,
et l'un d'eux va plus loin. Pour l'emploi du temps, quand `pronotepy` refuse
l'entrée, le JSON brut est décodé **directement** au lieu que le créneau soit
abandonné (`_lesson_raw`, `gateway.py`). La cause la plus fréquente est
la plus fâcheuse : `Lesson.__init__` résout `ListeContenus` sans
`strict=False`, et un créneau sans contenu publié — ce qui est précisément la
façon dont une **annulation** arrive souvent — n'a pas de `ListeContenus` du
tout. Abandonner ces entrées signifiait que l'événement pour lequel cette
intégration existe, `lesson_canceled`, ne pouvait pas se déclencher pour elles.

### 6.3 Absent n'est pas vide

La contrepartie exacte de la tolérance par entrée est `_required_list`
(`gateway.py`), qui distingue « la clé est absente » de « la liste est
vide » et **refuse** la première.

C'est le pire mode de défaillance possible d'une passerelle, et le raisonnement
mérite d'être cité : si PRONOTE renommait `ListeCours`, alors
`_get(...) or []` renvoyait un emploi du temps vide, le palier rapportait un
**succès**, l'instantané était remplacé, le `binary_sensor` « jour de classe »
passait à `off` et l'automatisation de réveil cessait simplement de se
déclencher — sans rien au journal, et sans que la péremption ne se déclenche
jamais, puisque la collecte avait réussi.

Lever à la place fait échouer le palier, ce qui conserve l'instantané
précédent, le marque comme daté et finit par ouvrir une réparation. « Je sais,
mais c'est vieux » est une réponse récupérable ; « il n'y a pas de cours cette
semaine » parce qu'un champ a été renommé ne l'est pas.

Les collections déclarées obligatoires sont celles qu'un parent lit pour agir :
`ListeCours` (emploi du temps), `ListeTravauxAFaire` (devoirs), `listeDevoirs`
et `listeServices` (notes et moyennes), `listeAbsences` (présence). Le critère
est explicite : « pas d'absences » est la réponse sur laquelle un parent agit,
et elle doit signifier que l'école l'a dite.

### 6.4 Le client durci

`hardened_client.py` n'est pas une option mais un **prérequis**, pour quatre
comportements amont vérifiés contre `pronotepy` 2.15.7 et énumérés en tête de
fichier (`hardened_client.py`) :

1. **Il se ré-authentifie derrière le dos de l'appelant.** `ClientBase.post`
   attrape *tous* les `PronoteAPIError` sauf `ExpiredObject`, appelle
   `self.refresh()` — `session.close()`, un GET sur la page HTML,
   `FonctionParametres`, `Identification`, `Authentification`,
   `ParametresUtilisateur` — puis rejoue l'appel. Un appel budgété peut donc
   coûter six requêtes réseau, ce qui rend fausse l'affirmation « le limiteur
   est la seule voie vers la passerelle ». Pire, `Erreur.G = 25` est un
   `PronoteAPIError` ordinaire, donc il provoque exactement la classe de
   requêtes que `G = 25` punit.
2. **`ParentClient.post` duplique ce gestionnaire sans la garde de
   récursion.** `ClientBase.post` se protège (`if self._refreshing: raise e`),
   `ParentClient.post` non. Un serveur qui erre pendant le `_login()` de
   `refresh()` produit une récursion non bornée de connexions complètes — sur
   un compte parent, c'est-à-dire le cas d'usage central de ce projet.
3. **`refresh()` perd silencieusement la sélection d'enfant.** Il appelle
   `_login()`, qui réécrit la ressource avec celle du *parent*, et
   `ParentClient` ne reconstruit `_selected_child` que dans `__init__`. Après
   un rafraîchissement automatique, `post()` estampille encore le bon `membre`
   mais `lessons()` envoie la ressource du parent dans le corps.
4. **Il n'y a aucun timeout HTTP.** `grep -rn timeout` sur le paquet ne
   renvoie rien : ni le GET de bootstrap ni les POST ne passent `timeout=`. Un
   serveur qui accepte la connexion et ne répond jamais immobilise le fil
   unique indéfiniment, le `asyncio.Lock` n'est jamais relâché, et le régime de
   péremption déguise un interblocage permanent en réseau lent.

`HardenedClient` (`hardened_client.py`) répond point par point :
`post()` poste **une fois** et ne se ré-authentifie jamais, estampillant
lui-même la signature `membre` de l'enfant sélectionné ; `refresh()` lève
`NotImplementedError` — le gestionnaire de session jette le client et en
construit un neuf, ce qui redérive la liste d'enfants par construction ;
`keep_alive()` lève également, parce que le maintien actif ne serait rentable
qu'en dessous d'un intervalle de 110 secondes et qu'aucun palier n'y va ; et le
transport est remplacé par `_TimeoutSession`, une `requests.Session` qui injecte
un défaut `(connect_timeout, read_timeout)` dans `request()`.

Une classe unique gère l'élève et le parent, plutôt que d'hériter de
`ParentClient` — c'est la sous-classe qui porte le bug de récursion et celui de
l'enfant perdu, et son `refresh()` n'est pas récupérable.

Deux détails d'hygiène complètent le tableau. `BootstrapUnavailable`
(`hardened_client.py`) ne dit **rien** de la cause : `pronotepy` décide
qu'une adresse est bannie avec `if "IP" in html`, soit deux majuscules
n'importe où dans la page — donc « Espace IP » dans un pied de page d'école,
une classe CSS, ou les lettres dans `SKIP`, `ZIP` ou `EQUIPE` déclenchent la
détection. Construire un maintien d'une heure et un message « explicite » sur
ce signal aurait annoncé à des parents que leur adresse personnelle était
bannie à cause d'un pied de page. Un état que l'intégration ne peut pas établir
de façon fiable ne doit pas exister dans son vocabulaire : c'est pourquoi
`LimiterState` ne contient pas d'`ip_suspended` (`const.py`) et pourquoi
la réparation énumère les causes possibles sans en choisir aucune.

Et `_ConfinedRegistry` / `_period_registry_confined`
(`hardened_client.py`) confine `dataClasses.Period.instances`, cet
attribut de classe global au processus qui n'est jamais vidé. Sans lui, chaque
connexion fuit : chaque `Period` reste dans l'ensemble pour toujours et
maintient vivant son `_client`, donc un client mort, une `requests.Session`
morte et son pool de sockets. Le retrait se fait **par différence**, jamais par
vidage, et sous un verrou **de processus** qui ne garde que l'arithmétique
d'ensembles — quelques microsecondes. Le tenir à travers tout
`ClientBase.__init__`, c'est-à-dire à travers une connexion, transformait chaque
`release_client` en attente bloquante sur l'aller-retour réseau d'une autre
entrée ; appelé depuis la boucle d'événements, cela figeait Home Assistant.

### 6.5 Les lectures à la demande

Trois fonctions de la passerelle sont appelées par des services et **jamais**
par un palier : `ical_url`, `identity` et `timetable_pdf_url`
(`gateway.py`). La raison est de sécurité, non de coût — voir §10.

`profile_picture` est un cas intermédiaire : lue paresseusement une fois par
rechargement par l'entité `image`, sous le verrou et **après** `set_child`,
parce que lire `ClientInfo.profile_picture` comme une propriété court-circuite
`ClientBase.post` et la signature `membre` — un compte parent afficherait donc
le visage du mauvais enfant.

Le cas `identity` est le plus instructif : la lecture passe par
`client.communication.post` avec une `ressource` explicite, et non par
`client.post`, qui estampille le **titulaire du compte** comme `membre`. Sur un
compte parent, `membre` renvoie la date de naissance, l'adresse e-mail, le
numéro de téléphone et le numéro INE **du parent** — que le service rendait
ensuite attribués à l'enfant. C'est une divulgation par mauvaise attribution
portant précisément sur les champs que §10 tient hors de la machine à états.

---

## 7. Le modèle de données

### 7.1 Des DTO gelés

Tous les objets de `models.py` sont des `@dataclass(frozen=True, slots=True)`.
`frozen` pour qu'un instantané publié ne puisse pas être muté par un
consommateur ; `slots` parce que ces objets se comptent par centaines et que
l'empreinte compte.

Chaque `datetime` y est **conscient du fuseau**, et la conversion a lieu une
seule fois, dans la passerelle, avec le fuseau de l'établissement
(`PronoteGateway._instant` / `_aware`, `gateway.py`). Les dates PRONOTE
sont du texte local naïf sans offset, et deux des six formes de
`Util.date_parse` complètent la partie manquante avec *aujourd'hui* ; comparer
cela à l'horloge d'une machine réglée en UTC — le défaut d'un conteneur —
produit des décalages silencieux et donc des automatisations qui se déclenchent
à la mauvaise heure. `_instant` et `_aware` sont deux méthodes distinctes pour
que les appelants qui ne peuvent pas recevoir `None` n'aient pas à écrire une
branche inatteignable.

Deux DTO portent des champs qui demandent une justification, et l'ont.

`Lesson.num` est le champ `P` du protocole, et il porte la déduplication :
`PageEmploiDuTemps` renvoie plusieurs entrées pour le même créneau — l'original
et ses remplacements — et le docstring amont dit que *pour une même heure de
cours, le plus grand `num` est celui affiché sur PRONOTE*. La spécification v1
déclarait `num` sans dire à quoi il servait, ce qui équivalait à ne pas
l'utiliser.

`Lesson.end_inferred` existe parce que `end` n'est pas toujours transmis :
quand `DateDuCoursFin` est absent, `pronotepy` le calcule via `Util.place2time`,
dont le commentaire amont dit *« might be wrong… works with demo »*. Tout dépend
de `end` — réveil, « en cours », le calendrier, la fin des cours — donc le
drapeau permet à une entité de **se taire** plutôt que d'affirmer une heure
douteuse.

`Grade` porte une invariante vérifiée à l'exécution
(`models.py`) : `value` et `status` sont mutuellement exclusifs, et
`__post_init__` lève si les deux sont présents. C'est une promesse que le
système de types ne peut pas faire et qu'aucun appelant ne vérifiait. La
passerelle est le seul producteur, donc cela ne peut se déclencher que sur un
bug de la passerelle — c'est-à-dire précisément quand il vaut la peine
d'échouer bruyamment, puisque le contrat de l'entité « dernière note » avec tous
les déclencheurs `numeric_state` en aval repose sur cette exclusivité.

`Snapshot[T]` (`models.py`) enveloppe les faits avec l'instant obtenu,
le palier, le coût réel et l'identifiant de l'élève. Le point porteur est dans
son docstring : **une collecte en échec ne remplace pas l'instantané
précédent**.

### 7.2 Les coordinateurs

Un coordinateur par palier (`PronoteTierCoordinator`, `coordinator.py`),
avec `update_interval=None`. Il n'y a **pas** de `_async_update_data` : ce
coordinateur n'a jamais le droit d'aller chercher quoi que ce soit lui-même. Un
coordinateur qui pourrait le faire serait une seconde voie vers le réseau, et
le limiteur en détient le monopole.

Les données sont indexées par élève, parce qu'une entrée de configuration est
un **compte**, et qu'un compte parent porte plusieurs enfants qui partagent une
session, un budget et une adresse IP.

Deux méthodes, et leur asymétrie est le mécanisme :

* `publish(student_id, snapshot)` fusionne l'instantané d'un élève en laissant
  les autres intacts, puis appelle `async_set_updated_data`. C'est le seul
  écrivain, et il n'est appelé qu'après un succès — voilà comment la règle « une
  collecte en échec ne remplace jamais un bon instantané » est appliquée : par
  construction, pas par vérification.
* `note_failure(error)` fait basculer `last_update_success` sans journaliser :
  un tick en échec ne doit pas imprimer une erreur par coordinateur (un par
  palier). La règle Silver `log-when-unavailable` vit sur la session : un INFO
  quand PRONOTE ne répond plus, un INFO quand il répond à nouveau
  (`session.py`, `_note_unreachable` / `_note_reachable`). `self.data` reste
  exactement ce qu'il était.

`always_update=False` évite les écritures d'état pour des données identiques.

### 7.3 De l'instantané à l'état d'une entité

`PronoteEntity` (`entity.py`) est la classe de base. La chaîne est
courte et vaut la peine d'être lue :

```
coordinator du palier
  -> snapshot_for(student.id)          # entity.py
    -> snapshot.data                   # entity.py, la propriete `facts`
      -> description.value_fn(facts, account)
```

Aucune propriété d'entité ne touche le réseau. Chaque plateforme lit
`self.facts` et renvoie `None` si l'instantané est absent.

L'`unique_id` est `f"{entry_id}_{student.id}_{key}"` (`entity.py`), et les
trois composantes sont nécessaires. L'`entry_id` y est parce que l'`unique_id`
de l'entrée est le **compte**, donc deux entrées suivant le même enfant sont
légitimes — un compte mère et un compte père, ou un compte parent à côté de
celui de l'enfant. Et `student.id` est le numéro de ressource PRONOTE `N`, qui
n'est unique qu'à l'intérieur de la base d'un établissement. Sans ce préfixe,
le registre ne rejette pas la seconde entrée : il **re-pointe** l'entité
existante vers elle, déplaçant l'entité sur l'autre device et cassant
silencieusement toutes les automatisations qui la référençaient. La clé finale
est une clé fonctionnelle stable — jamais un libellé, un nom de période ou un
rang : une automatisation qui casse en septembre parce qu'un capteur a été
renommé est une régression même si aucun code n'a échoué.

La **disponibilité** (`entity.py`) ne consulte volontairement **pas**
`coordinator.last_update_success`. Une entité est `unavailable` uniquement si
elle n'a jamais eu de données, ou si `account.is_stale(tier)`. Laisser un échec
transitoire basculer la disponibilité est exactement le déclenchement
fallacieux que le régime de fraîcheur existe pour éviter.

Les attributs communs sont `fetched_at` et `stale`. Et toutes les listes sont
**non enregistrées** : `_unrecorded_attributes` combine
`UNRECORDED_LIST_ATTRIBUTES` (`const.py`) et `fetched_at`, parce que les
listes PRONOTE dépassent la limite de 16 Kio d'attributs du recorder et que
l'historique qui vaut la peine d'être gardé est le compteur, pas la charge
utile. `PronoteAccountEntity` est un **frère** et non une sous-classe, donc
cette déclaration y est répétée — l'oubli faisait enregistrer `by_tier` et
`tiers_due` à chaque écriture d'état.

Le corollaire est plus contre-intuitif et a coûté une version : une
sous-classe, elle, ne redéclare **jamais**. Home Assistant ne réunit pas ces
ensembles le long d'une hiérarchie —
`Entity.__init_subclass__` calcule
`_entity_component_unrecorded_attributes | cls._unrecorded_attributes` et le
terme de droite est résolu par recherche d'attribut ordinaire, donc une
déclaration sur une sous-classe *remplace* celle du parent. `PronoteSensor` a
déclaré un unique nom en v0.0.22, en croyant l'ajouter : les dix-huit attributs
de liste de tous les capteurs sont retournés à l'enregistreur, et les adresses
signées des pièces jointes ont été mesurées dans l'historique. Un nom nouveau
s'ajoute donc à `UNRECORDED_LIST_ATTRIBUTES`, et un test refuse toute
déclaration de sous-classe qui ne couvre pas cette constante.

`ClockDrivenMixin` (`entity.py`) traite le cas des entités dont l'état
dépend de **l'heure** et pas seulement des données : « en cours », « absent en
ce moment », « prochain cours », « réveil ». Elles arment un rappel ponctuel à
leur prochaine transition connue via `async_track_point_in_time`, plutôt que de
changer d'état au rythme de leur palier — c'est-à-dire jusqu'à quinze minutes en
retard, ce qui sur un capteur de réveil supprime tout l'intérêt du capteur.

### 7.4 La détection de delta

`delta.py` implémente **deux** règles, parce qu'une seule ne peut pas faire le
travail.

La spécification v1 en avait une : « delta sur l'identifiant `N`, jamais sur le
contenu ». Elle est juste pour la plupart des collections et **incapable
d'implémenter l'événement le plus utile de l'ensemble**, le cours modifié, pour
deux raisons vérifiées : un changement de salle garde le même `N` et ne produit
donc aucun delta d'identifiant ; et un remplacement arrive avec un `N` neuf
pendant que l'entrée originale est encore présente, donc il se lit comme un
**ajout** — et `previous_start` / `previous_classroom` n'ont alors aucune
source.

La règle se dédouble donc :

**Collections en ajout seul** — notes, devoirs, actualités, absences, retards,
punitions, messages, évaluations — gardent la règle de l'identifiant telle
quelle. Un libellé qu'un professeur corrige ne doit pas réveiller la maison.

**L'emploi du temps** compare un **tuple de champs sur liste blanche**, par
identifiant de cours, sur la semaine **non dédupliquée**.
`Lesson.change_signature` (`models.py`) contient `canceled`, `status`,
`frozenset(classrooms)`, `frozenset(teachers)`, `start`, `end` — et exclut
`memo`, `background_color` et le contenu du cours, si bien qu'un mémo corrigé
ne déclenche rien, ce que la règle unique cherchait en réalité à acheter.

Les deux moitiés de cette phrase étaient fausses dans la première
implémentation, et fausses d'une façon qui ne produisait **aucune erreur** :

* elle indexait sur le **créneau** (jour plus position dans la journée). Comme
  le jour fait partie de la clé, un cours déplacé ne pouvait jamais être vu
  comme déplacé — `lesson_moved` était structurellement inatteignable — tandis
  qu'un changement de la **grille horaire** de l'établissement, les sonneries
  décalées de cinq minutes, déplaçait `start` pour tous les cours de tous les
  créneaux d'un coup et déclenchait un `lesson_moved` pour la semaine entière ;
* elle tournait sur la semaine **dédupliquée**. Or lors d'un remplacement,
  PRONOTE sert l'entrée originale avec `estAnnule` **plus** un remplacement au
  `num` supérieur, et la déduplication garde le remplacement — donc l'entrée
  qui portait l'annulation avait été écartée une couche plus bas, et
  l'annulation était inobservable.

C'est pour cela que `TimetableFacts` porte **deux** collections : `lessons`
dédupliquée pour l'affichage, et `all_lessons` triée mais non dédupliquée pour
le détecteur.

Les comparaisons de salles et de professeurs se font sur des **ensembles** et
non des séquences : rien dans le protocole n'ordonne `ListeSalles` ni
`ListeProfesseurs`, et PRONOTE les réordonne effectivement entre deux réponses.
En tuples, un cours co-enseigné émettait `lesson_changed` sur un sous-ensemble
arbitraire de collections, chaque nuit, pour le reste de l'année.

Un changement peut légitimement être plusieurs choses à la fois — un cours
déplacé *et* mis dans une autre salle — donc `_lesson_events`
(`delta.py`) émet un événement par aspect réellement modifié plutôt que
d'en élire un. Et il n'y a **plus** de repli « si rien n'a correspondu,
appelons ça une annulation » : chaque branche de la signature a maintenant son
propre nom, donc ce repli ne pouvait se déclencher que pour une transition sur
laquelle il avait tort — et la transition pour laquelle il se déclenchait
réellement était une annulation **levée**, qu'il rapportait comme
`lesson_canceled`. C'est ce que l'existence de `EVENT_LESSON_RESTORED`
(`const.py`) corrige : une automatisation qui notifie « pas de cours en
première heure, dors » se déclenchait le matin où le cours était rétabli.

Deux garanties générales complètent le dispositif. Le **premier** instantané
d'un palier après un démarrage ou un rechargement n'émet **rien** — d'où le
`None` distinct de la liste vide dans `_new_ids` (`delta.py`), parce que
« rien de neuf » et « c'est le premier instantané » doivent conduire à des
comportements différents et qu'un appelant incapable de les distinguer rejouera
le trimestre. Et huit notes nouvelles produisent **huit** événements, pas un
événement agrégé : agréger est trivial à refaire pour l'utilisateur, séparer ne
l'est pas.

Un plafond, `_NEW_THREAD_CAP = 3`, borne les événements `message_received`
qu'un fil peut émettre à sa **première** apparition : un fil devient visible
pour la première fois pour d'autres raisons qu'un message qui arrive — une
étiquette déplacée, une restauration depuis la corbeille, un établissement qui
désarchive — et traiter tout son compteur de non-lus comme des arrivées
rejouait le fil.

Les événements sont diffusés sur le bus Home Assistant sous le signal
`SIGNAL_DELTA`, défini **et** émis par `account.py` — `device_trigger.py` et
`event.py` l'importent de là, et non de `const.py`, ce qui est l'exception à la
règle « une constante partagée vit dans `const.py` ». Les neuf entités `event`
s'y abonnent en filtrant sur `entry_id`, `student_id` et `entity_key`. Le détecteur
ne conserve que le matériel d'identité dont il a besoin, jamais les instantanés
eux-mêmes, pour qu'un trimestre de notes ne siège pas deux fois en mémoire.

---

## 8. Un cycle de collecte, de bout en bout

Le diagramme suivant suit un palier unique, pour un élève unique, depuis
l'échéance jusqu'à l'état de l'entité — en faisant apparaître le passage par le
limiteur, la réconciliation et l'ordre entre publication et événement.

```mermaid
sequenceDiagram
    autonumber
    participant HA as Home Assistant
    participant A as PronoteAccount
    participant SC as FetchScheduler
    participant RL as RateLimiter
    participant SM as SessionManager
    participant EX as SerialExecutor
    participant GW as PronoteGateway
    participant HC as HardenedClient
    participant CO as Coordinateur du palier
    participant E as Entite
    participant EV as Entite event

    HA->>A: master tick, toutes les 5 min
    A->>A: _tick_lock deja pris ? si oui, on saute
    A->>SC: due
    SC-->>A: paliers dus, par priorite decroissante
    A->>RL: begin_batch
    A->>SM: begin_batch

    loop pour chaque palier du, puis chaque eleve
        A->>SM: run tier, priority, fn, student_id, cost
        Note over SM: _gate tenu sur choisir, attendre, appeler, reessayer
        SM->>SM: _ensure_client · connexion si la politique l exige
        SM->>RL: call tier, priority, fn, cost

        alt refuse
            RL-->>SM: TierDeferred reason, retry_after
            SM-->>A: TierDeferred
            A->>SC: defer tier, retry_after
            Note over CO: instantane conserve, entites inchangees
        else admis
            Note over RL: sous _admission · check, attente, commit du cout
            RL->>EX: run closure
            EX->>HC: set_child student_id puis post onglet
            HC-->>EX: reponse brute
            EX->>GW: decodage entree par entree, tolerant
            GW-->>RL: GatewayResult facts, calls reels
            RL->>RL: note_success
            RL-->>SM: GatewayResult
            SM->>RL: reconcile tier, charged, actual
            SM-->>A: GatewayResult
            A->>CO: publish student_id, snapshot
            CO->>E: async_set_updated_data
            E->>E: available, native_value, attributs recalcules
            A->>A: delta detecte les changements
        end
    end

    A->>RL: end_batch
    A->>SC: mark_collected tier
    A->>EV: bus async_fire SIGNAL_DELTA, une fois par changement
    Note over EV,E: les evenements partent APRES la publication
    A->>HA: _async_sync_issues · ouvre ou ferme daily_cap_near
```

Trois points de l'ordonnancement ne sont pas fortuits, et sont énoncés dans
`account.py` :

1. Les paliers sont servis **par priorité**, parce que le limiteur sacrifie par
   priorité et que servir dans le désordre laisserait tomber les mauvais.
2. Une collecte réussie publie son instantané **puis** lance le détecteur, si
   bien qu'un événement ne se déclenche jamais pour des données que les entités
   ne détiennent pas encore. Une automatisation qui réagit à « une note est
   arrivée » trouve le capteur qui la porte déjà à jour.
3. Une collecte différée déplace son échéance et garde son instantané ; une
   collecte en échec fait basculer le drapeau de succès du coordinateur et
   garde son instantané aussi. « Je sais, mais c'est vieux » bat « je ne sais
   plus » sur des données scolaires.

Le traitement des exceptions dans `_async_collect` (`account.py`)
distingue trois familles. `TierDeferred` reporte. Les refus
d'**authentification** — `LoginRefused`, `InvalidCredentials`, `MfaRequired`,
`BootstrapFailed`, `AccountUnreadable`, `IntegrationFault` — sont le problème du
*compte* et non du palier : ils ouvrent une réparation et arrêtent le batch au
lieu d'être réessayés palier par palier. Tout le reste est attrapé au sommet
d'un cycle, journalisé, et reporté avec `retry_delay()` — jamais
`backoff_delay()`, pour la raison donnée au §3.6.

---

## 9. Le flow de configuration

PRONOTE offre trois entrées, donc le flow en offre trois. Ce ne sont pas des
variantes d'un formulaire : l'enrôlement par QR code produit un jeton rotatif
et demande un UUID d'appareil, le mode identifiants non, et l'ENT demande un
fournisseur choisi dans une liste. Chacun a donc son étape, plutôt qu'un
formulaire unique avec des champs parfois ignorés.

```mermaid
flowchart TD
  START["async_step_user · menu a trois entrees"]

  START --> QR["async_step_qr_code · payload du QR, PIN du QR, PIN de compte optionnel"]
  START --> CRED["async_step_credentials · URL, identifiant, mot de passe"]
  START --> ENT["async_step_ent · URL, identifiant, mot de passe, fournisseur ENT"]

  QR --> TRY["_async_try_login"]
  CRED --> TRY
  ENT --> TRY

  TRY --> PROBE["_async_probe · login_guard.login puis executor puis flow_login.probe_account"]

  PROBE -->|"succes"| UID["async_set_unique_id · URL plus identifiant PRONOTE"]
  PROBE -->|"invalid_auth, mfa_required, bootstrap_failed, invalid_qr, rate_limited, cannot_connect"| RESHOW["_async_reshow · meme formulaire, avec erreurs"]
  RESHOW --> TRY

  UID --> ABORT{"unique_id deja configure ?"}
  ABORT -->|"oui"| STOP["abandon · deja configure"]
  ABORT -->|"non"| MULTI{"plus d un enfant ?"}

  MULTI -->|"oui"| KIDS["async_step_children · selection multiple"]
  MULTI -->|"non"| CREATE
  KIDS --> CREATE["_async_create · retire qr_payload, qr_pin, account_pin, account_id"]

  CREATE --> ENTRY["entree creee · children CONSERVE"]

  RA["async_step_reauth · declenche par ConfigEntryAuthFailed"]
  RA --> RAC["async_step_reauth_confirm · nouveau mot de passe et ou PIN"]
  RAC --> CLEAR["clear_login_penalties AVANT la tentative"]
  CLEAR --> PROBE2["_async_probe"]
  PROBE2 -->|"succes"| UPD["async_update_reload_and_abort · PIN retire a la sortie"]
  PROBE2 -->|"echec"| RAC

  OPTS["PronoteOptionsFlow · init, general, tiers, rate_limit"]
  OPTS --> EST["estimate_daily_requests affiche a chaque etape"]
  EST --> RELOAD["sauvegarde · OptionsFlowWithReload recharge une entree chargee"]
```

Quatre choses sont porteuses dans ce flow.

**Le PIN à deux facteurs n'est jamais stocké.** Il est demandé, utilisé pour la
connexion qui en a besoin, et abandonné. `_async_create`
(`config_flow.py`) exclut `qr_payload`, `qr_pin`, `account_pin` et
`account_id` de l'entrée créée ; le chemin de ré-authentification retire de
même `account_pin` à la sortie (`config_flow.py`), si bien que le PIN
n'existe dans l'entrée que le temps d'un rechargement. Corollaire assumé : un
secret qu'on refuse de garder est un secret qu'on doit savoir **redemander**,
ce qui est la raison d'être du flow de ré-authentification.

**Toute connexion du flow passe par le limiteur.** C'est
`_async_probe` (`config_flow.py`) qui l'assure, via
`login_guard(hass).login(...)`. L'omission comptait plus qu'il n'y paraît :
c'est le seul chemin de connexion qu'un humain peut répéter à volonté, puisque
le formulaire de ré-authentification se réaffiche à chaque échec. Un parent
convaincu que le mot de passe est bon pouvait le soumettre vingt fois en une
minute ; aucune de ces tentatives n'atteignait `may_login`, aucune n'était
comptée par `note_login`, et le garde-fou de trois tentatives — la défense
contre la seule sanction que PRONOTE applique à une adresse et non à un compte
— ne les voyait jamais.

Le coût déclaré tient compte du **doublement structurel** de l'enrôlement QR :
`qrcode_login` construit un client — dont le constructeur se connecte — poste
`PageInfosPerso` 49, puis appelle `token_login` avec les identifiants exportés.
Ce sont deux vraies connexions contre le serveur, donc le flow déclare
`REQUESTS_PER_LOGIN * 2`, et `note_login(counts_against_guard=False)` exempte ce
doublement délibéré du garde-fou plutôt que de lui laisser consommer les deux
tiers d'un budget de trois tentatives (`ratelimit.py`).

**La ré-authentification efface les pénalités *avant* la tentative.** Un geste
humain délibéré, avec la correction en main, n'est pas un retry automatique et
mérite une ardoise vierge. Et c'est la seule sortie du maintien MFA, ce qui est
correct puisque ce maintien existe précisément parce qu'une personne doit agir.
Le commentaire du code note que cela ne se produisait pas du tout :
`reset_after_reauth` n'avait aucun appelant hors des tests du limiteur, si bien
que le maintien MFA, dont la seule sortie est un humain fournissant le PIN, ne
pouvait pas être quitté par un humain fournissant le PIN.

**L'`unique_id` de l'entrée est l'URL de l'établissement plus l'identifiant
PRONOTE du compte** (`flow_login.py`). Pas l'identifiant de connexion :
une connexion ENT et une connexion directe pour le même compte utilisent des
identifiants différents. Pas le jeton non plus, puisqu'il tourne à chaque
connexion.

Un dernier détail, parce qu'il illustre la classe de bugs que ce dépôt
poursuit : `children` est délibérément **exclu** de la fusion du résultat de
sonde, et délibérément **inclus** dans l'entrée créée. Le probe renvoie
`children` sous forme de paires `(id, nom)`, et `CONF_CHILDREN` — la même
chaîne — est où la *sélection* de l'utilisateur est stockée quelques lignes plus
bas. Fusionner les paires puis retirer la clé à la sortie jetait la sélection
avec elles : un parent qui suivait délibérément un enfant sur deux en obtenait
deux quand même, silencieusement, avec le budget de requêtes du second.

`flow_login.py` existe séparément de `config_flow.py` pour une raison qui n'est
pas cosmétique : le flow ne doit **pas** importer `pronotepy` au niveau du
module, sinon une dépendance cassée transforme « l'intégration ne peut pas se
connecter » en « l'intégration ne peut pas être ajoutée du tout ». Tous les
imports amont y sont locaux à la fonction.

Le flow d'options a trois sections — `general`, `tiers`, `rate_limit` — et
affiche à chaque étape une **estimation du budget quotidien** calculée par
`estimate_daily_requests` (`options.py`). Ce n'est pas de la
décoration : un réglage dont on ne peut pas voir la conséquence se règle au
hasard. L'estimation utilise la même fonction que celle qui a produit les
chiffres de l'annexe, elle plafonne le nombre de connexions par
`max_logins_per_day` — sinon le cas dégénéré d'un délai serveur de cinq minutes
annonçait 64 connexions par jour contre un maximum configuré de 24, une
estimation que l'intégration ne pourrait pas produire même en essayant — et
elle compte une connexion à `REQUESTS_PER_LOGIN_WORST_CASE = 7` et non à 5,
parce qu'une estimation dont on peut être déçu est pire que pas d'estimation.

---

## 10. Sécurité et vie privée

### 10.1 L'inventaire des secrets

| Secret | Où il vit | Persisté ? |
| --- | --- | --- |
| Mot de passe, ou jeton d'application mobile | `entry.data["password"]`, en clair dans `.storage` de Home Assistant | oui — le jeton tourne à chaque `Authentification` et doit être réécrit |
| Identifiant de connexion | `entry.data["username"]` | oui |
| UUID d'appareil | `entry.data[uuid]` | oui — il ne doit pas changer entre deux connexions |
| `client_identifier` | `entry.data` | oui |
| PIN à deux facteurs (`account_pin`) | mémoire du flow, puis `entry.data` le temps d'un rechargement | **non** |
| Payload et PIN du QR code | mémoire du flow uniquement | **non** — usage unique, PRONOTE l'invalide à l'enrôlement |
| URL iCal | réponse de service, le temps d'un script | **non**, jamais |
| Bloc d'identité — date et lieu de naissance, e-mail, téléphone, adresse, numéro INE, tuteurs | réponse de service | **non**, jamais |
| URL du PDF d'emploi du temps | réponse de service | **non**, jamais |
| Jeton de session `h` de `pronotepy` | à l'intérieur du client, dans le fil de travail | **non** |

### 10.2 Pourquoi aucun n'entre dans un état d'entité

La règle est énoncée dans `services.py` : **un secret est renvoyé, jamais
stocké.** `get_ical_url`, `get_identity` et `generate_timetable_pdf` sont des
services `SupportsResponse.ONLY`.

L'URL iCal est le cas le plus net. Quiconque la détient lit l'emploi du temps
complet d'un enfant **sans identifiant et sans mot de passe**. Comme état, elle
atterrirait dans la base du recorder, donc dans chaque sauvegarde, dans les
captures d'écran et dans les rapports de bug. Comme réponse de service, elle
existe le temps d'une exécution de script.

C'est pour cela que le palier `static` ne la collecte **plus** :
`tiers.py` et `gateway.py` le disent tous deux. La collecter
sur un rythme mettrait un porteur d'authentification autonome dans le magasin
d'instantanés — une structure de longue durée dont la finalité est d'être
versée dans un rapport de diagnostic.

Le même raisonnement s'applique à un endroit moins évident : les pièces jointes
aux devoirs ne portent que des **noms**, jamais d'URL (`gateway.py`).
`Attachment.url` interpole `client.attributes["h"]`, le jeton de session vivant,
donc la garder écrirait une URL porteuse d'un identifiant dans l'instantané,
dans chaque ligne du recorder et dans le téléchargement de diagnostic — et elle
serait morte au moment où quelqu'un cliquerait dessus, puisque le jeton change à
la connexion suivante.

De même, `_information_dict` omet volontairement `content` : lire
`Information.content` est un accès paresseux qui *poste*.

### 10.3 Diagnostics : l'absence par construction, la redaction en filet

`diagnostics.py` est explicite : la redaction est un **filet de sécurité**
et non le mécanisme. Le mécanisme est que les secrets n'entrent jamais dans un
instantané, donc il n'y a rien à trouver dans ce fichier.

Ce qui est *dans* l'entrée de configuration est masqué explicitement :
`username`, `password`, `uuid`, `client_identifier`, `account_pin`,
`qr_payload`, `qr_pin` (`TO_REDACT`, `diagnostics.py`). L'entrée
`account_pin` y figure « par précaution » — le PIN n'est jamais persisté, donc
cette clé ne devrait jamais correspondre à rien ; si elle correspond un jour,
c'est le bug.

`pronote_url` **reste** : il identifie l'établissement, ce qui est exactement ce
dont un rapport de bug a besoin, et il n'accorde rien par lui-même.

Deux mesures complètent le tableau.

**Empreintes tronquées des identifiants d'élève.** `_short_hash`
(`diagnostics.py` et `account.py`) applique `blake2s` avec
`digest_size=4`. Une empreinte reste utile dans un rapport de bug — « les deux
enfants montrent le même palier en échec » — sans donner à personne le matériel
pour rejouer une session. C'est aussi ce que le diagnostic par device utilise, à
côté d'un rapport qui ne décrit que la **forme** de chaque instantané : quand il
a été récupéré, combien de requêtes il a coûté, combien d'éléments il contient
(`_counts`, `diagnostics.py`) — et pas son contenu. Une note, le
commentaire d'un professeur sur un bulletin ou le corps d'un message n'ont rien
à faire dans un fichier que les gens collent dans un ticket public.

**URL publique retaillée.** `urls.public_url` (`urls.py`) ne garde que le
schéma, l'hôte et le chemin : pas de chaîne de requête, pas de fragment, pas
d'informations d'authentification `user:password@`. La raison est concrète : ce
que les parents collent n'est très souvent pas une URL de page nue. Les
établissements envoient des liens profonds, les portails ENT renvoient à travers
une authentification unique avec un ticket dans l'URL, et un parent qui copie
depuis la barre d'adresse après s'être connecté copie le paramètre de session
qui s'y trouve. Garder cette chaîne signifiait qu'un paramètre de session
pouvait voyager dans l'entrée de configuration, de là dans les substitutions
d'un *repair issue* — écrites dans `.storage` et rendues dans le panneau
Réparations — et de là dans le téléchargement de diagnostic, le fichier qu'on
demande explicitement aux utilisateurs de joindre à un ticket public.

Avec des valeurs manifestement fictives, la transformation ressemble à ceci :

| Ce que l'utilisateur colle | Ce qui est stocké |
| --- | --- |
| `https://demo.example.invalid/pronote/parent.html?login=true&ticket=ST-000` | `https://demo.example.invalid/pronote/parent.html` |
| `https://compte-factice:secret-factice@demo.example.invalid/pronote/` | `https://demo.example.invalid/pronote/` |
| `https://demo.example.invalid:8443/pronote/eleve.html#page=1` | `https://demo.example.invalid:8443/pronote/eleve.html` |

Le port non standard est **conservé** — c'est `netloc` et non `hostname` qui est
repris, parce que certains établissements en publient un — tandis que les
informations d'authentification, la chaîne de requête et le fragment
disparaissent. Et si la chaîne ne s'analyse pas comme une URL, elle est rendue
telle quelle : refuser une adresse que l'utilisateur voit dans son navigateur
serait pire que d'en stocker une bizarre, puisque la tentative de connexion
échouera avec le message du serveur, qui est un meilleur diagnostic que le
nôtre.

L'adresse est donc retaillée à la frontière, une fois
(`config_flow.py`), et seule la forme retaillée est stockée, affichée ou
rapportée. `diagnostics.py` la retaille **une seconde fois** plutôt que de
lui faire confiance : une entrée créée avant que le flow ne fasse cela — ou
restaurée depuis une sauvegarde, ou éditée à la main dans `.storage` — peut
encore porter le lien profond qu'une école a envoyé. `url_host` réduit encore
davantage pour les cas qui n'ont besoin de dire que *quel* établissement.

### 10.4 Le journal

`manifest.json` ne porte **aucune** clé `loggers`, et c'est vérifié en CI
(`scripts/check_manifest.py`). Activer le logger `pronotepy` en DEBUG fait
écrire à `pronoteAPI.py` la charge utile de la requête en clair, identifiants
compris. Une exigence de ce type ne vaut rien si rien ne l'applique, parce
qu'une entrée `loggers` est exactement le genre de ligne qu'on ajoute en
déboguant et qu'on oublie de retirer.

Le corollaire pratique : le diagnostic de cette intégration se fait par les
entités de diagnostic, le service `get_rate_limit_status` — qui lit tout en
mémoire et coûte **zéro** requête, donc peut être interrogé depuis un tableau de
bord — et le téléchargement de diagnostic. Jamais par le journal amont.

### 10.5 Les écritures sont un choix enregistré

Cocher un devoir, marquer une actualité comme lue et envoyer un message
modifient ce que l'établissement voit. Les trois sont refusés quand les
opérations d'écriture sont coupées (`OPT_WRITE_OPERATIONS_ENABLED`), et refusés
**avec une explication** plutôt qu'ignorés (`_require_writes`, `services.py`).

Une nouvelle entrée PRONOTE naît avec l'option vraie, inscrite dans ses options
par `_async_create` (`config_flow.py`). Le repli, lui, reste faux
(`LEGACY_WRITE_OPERATIONS_ENABLED`) dans `write_enabled` comme dans le
formulaire : une entrée sans la clé date d'avant ce défaut, et la lire avec le
nouveau aurait ouvert l'écriture chez quelqu'un qui ne l'a jamais choisie — au
premier démarrage, ou au premier enregistrement de la page « Général ».

La liste de devoirs suit la même règle jusque dans son interface :
`todo.py` n'annonce `UPDATE_TODO_ITEM` que si les écritures sont activées,
parce qu'une case à cocher qui refuse est pire qu'une liste visiblement en
lecture seule.

Un test dédié tient cette ligne : `tests/test_no_secret_in_state.py` installe
l'intégration avec des sentinelles connues, exécute un cycle de collecte réel,
puis balaie **quatre** surfaces — états et attributs, téléchargement de
diagnostic, réponses de service, et le journal capturé en DEBUG. Les sentinelles
sont délibérément invraisemblables plutôt que réalistes : un test qui
chercherait un jeton d'allure réaliste tenterait quelqu'un d'y coller un vrai,
et ce fichier est publié.

---

## 11. La qualité

### 11.1 Les portails

Quatre portails, tous appliqués par `.github/workflows/validate.yml`.

**`ruff`**, en `check` et en `format --check`. Le jeu de règles est large
délibérément — 40 familles sélectionnées, dont `S` (bandit), `BLE` (except
aveugle), `DTZ` (datetimes naïfs, porteur ici pour les raisons du §7.1), `FBT`
(piège booléen), `TRY`, `SLF`, `T20` — avec l'argument que le projet démarre
propre et qu'il n'y a donc aucune raison d'ouvrir un registre d'exemptions. Les
dérogations existantes sont peu nombreuses et motivées ligne par ligne dans
`pyproject.toml`. Deux d'entre elles disent quelque chose de l'architecture :
`SLF001` — accès à un membre privé — n'est levé que pour `gateway.py` et
`hardened_client.py`, ce qui est la concentration de la surface amont rendue
exécutable par l'outillage.

**`mypy --strict`** sur `custom_components/carnet_scolaire` seulement, avec
`warn_unreachable` et `warn_unused_ignores`. Trois dérogations par module :
`pronotepy.*` (qui livre `py.typed` mais importe `autoslot.Slots` sous un
`type: ignore`), `autoslot.*`, et `homeassistant.*` pour `implicit_reexport` —
le cœur publie son API publique à travers des espaces de noms sans `__all__`, et
sa propre CI n'applique pas `no_implicit_reexport` contre eux.

**`pytest`** avec `asyncio_mode = "auto"`, `--strict-markers`,
`--strict-config`, et `filterwarnings = ["error::DeprecationWarning:custom_components.carnet_scolaire.*"]`
— une dépréciation dans notre propre code est une erreur, une dépréciation
ailleurs ne l'est pas. Plus de 1200 tests, répartis sur trente-huit fichiers.

**La couverture**, appliquée par `scripts/check_coverage.py`, qui sort en code
non nul :

* **95 % sur chaque module**, sans liste d'exceptions — la règle
  `test-coverage` de l'échelle de qualité Home Assistant (palier argent) ;
* **100 %** sur sept modules : `ratelimit.py`, `scheduler.py`, `gateway.py`,
  `delta.py`, et les trois `connectors/ecoledirecte/ed_*.py` ;
* **80 %** en global, lignes **et** branches — le script prend le minimum des
  deux, et ce seuil ne reste qu'en garde-fou plus faible.

Le seuil par module n'est pas une moyenne déguisée, et c'est tout son intérêt :
quatre modules de ce paquet dépassent les cinq cents instructions, donc un
module de quarante peut tomber à zéro sans faire bouger le chiffre global d'un
point. La liste des modules tenus à 95 % est **lue dans le rapport de
couverture**, jamais écrite dans le script — un module ajouté demain est tenu
au seuil sans que personne ait à penser à l'inscrire, ce qui est la panne
silencieuse qu'une liste aurait produite.

Le choix des sept est motivé, et le motif est le même pour chacun : ce
sont les modules où une erreur **ne produit aucun symptôme visible à
l'exécution**. Un limiteur qui laisse passer un appel, un ordonnanceur qui saute
un palier, une passerelle qui décode mal en silence, un détecteur qui rate un
changement — rien de tout cela ne lève, rien n'apparaît au journal, et le seul
signe est une donnée fausse ou absente que personne ne compare. Un seuil qui
n'échoue pas n'est pas un seuil.

Deux portails supplémentaires vivent hors de `validate.yml` :
`.github/workflows/hassfest.yml` (validation officielle Home Assistant),
`.github/workflows/hacs.yml` (validation HACS, y compris une exécution
hebdomadaire pour qu'un changement de règle amont apparaisse sans push), et
`.github/workflows/release.yml`, qui vérifie que `manifest.json` déclare
exactement la version du tag — une intégration HACS dont le manifest diverge du
tag s'installe une fois et ne se met plus jamais à jour.

Un dernier, qui ne garde rien mais surveille :
`.github/workflows/pronotepy-watch.yml` compare une fois par semaine l'épingle
`pronotepy` à ce que PyPI publie et, si l'amont est devant, ouvre une *pull
request* qui relève l'épingle dans ses deux déclarations, avec la date de
publication et les sujets de commit entre les deux étiquettes amont
(spécification §11.1.1). Il existe parce qu'une version épinglée sans veille est
une dette dont l'échéance a été une panne totale de connexion : le correctif
amont était public depuis six jours et son sujet de commit nommait la cause.
Rien n'y fusionne automatiquement — relever l'épingle oblige à relire chaque
divergence documentée dans `hardened_client.py`, et c'est le travail que la
proposition demande.

La matrice de test comporte deux lignes : la référence épinglée
(`pytest-homeassistant-custom-component==0.13.363`, qui porte Home Assistant
**2026.9.0**) qui **porte le portail de couverture**, et une ligne flottante
`latest` qui attrape une rupture amont tôt **sans bloquer** dessus.

Le choix de l'épingle n'est pas « une version récente » mais **exactement le
plancher déclaré dans `hacs.json`**. C'est la seule façon de tester ce que l'on
annonce : épingler plus récent laisserait le plancher lui-même inexercé, et un
plancher qu'on n'exécute jamais est une affirmation invérifiable adressée à
l'utilisateur au moment de l'installation. La ligne `latest` couvre l'autre
bout de l'intervalle.

### 11.2 Pourquoi la suite tourne sous Linux, WSL ou Docker

`pytest_homeassistant_custom_component` — plus exactement le harnais de test de
Home Assistant qu'il embarque — importe **`fcntl`**, qui n'existe pas sous
Windows. Le module ne peut donc pas être chargé du tout sur cette plateforme,
et pas seulement par ce que ce projet en fait.

la fixture `account` de `tests/conftest.py` traite cela de la seule façon honnête : le plugin est
enregistré **conditionnellement** (`HAS_HASS_HARNESS = sys.platform != "win32"`),
avec un message d'erreur qui nomme la cause et la solution — « exécutez-les sous
Linux, WSL ou Docker, comme le fait la CI ». Un contributeur sous Windows peut
alors exécuter la moitié de la suite qui n'a aucune dépendance Home Assistant :
le limiteur, l'ordonnanceur, la passerelle, le détecteur de delta, les DTO et
l'estimateur de budget.

Ce découpage n'affaiblit aucun portail, et c'est la propriété qui le rend
acceptable : **cette moitié est exactement celle qui porte le portail à 100 %.**
Les quatre modules critiques ne dépendent ni de Home Assistant ni du réseau,
par construction — ils prennent des horloges injectables et répondent à des
questions. La CI, elle, tourne sur Linux et charge tout.

### 11.3 Les coutures de test

Trois décisions de conception existent en partie pour rendre les tests
possibles, et il vaut la peine de les nommer parce qu'elles ressemblent
autrement à de la sur-ingénierie.

**Deux horloges injectables partout.** `RateLimiter`, `FetchScheduler`,
`SessionManager` et `PronoteGateway` prennent une horloge monotone `clock` et
une horloge murale `now` en argument de constructeur, plutôt que d'appeler
`time.monotonic` et `dt_util.now` eux-mêmes. `tests/clock.py` exploite cette
couture : un test avance le temps d'une heure en une instruction, sans
`freezegun` et sans dormir, et toute branche dépendant du temps écoulé devient
atteignable. Les deux horloges avancent **ensemble** par défaut, et les faire
diverger — une correction NTP reculant l'horloge murale sans bouger la
monotone — est exactement ce qui rend testable la branche « recul » de
`_roll_day`.

`rng` et `sleep` sont des coutures pour la même raison, résolues dans le
constructeur du limiteur plutôt qu'à chaque site d'appel, si bien qu'un test
construit **un** limiteur avec un générateur figé et un dormeur simulé, et tous
les chemins deviennent atteignables sans patcher un global de module.

**Le coût est renvoyé, pas déduit.** `GatewayResult.calls` rend possible le test
de contrat par palier : un accès accidentel à une propriété double le coût sans
que rien ne casse, et c'est la régression que ce projet existe pour empêcher.

**Les invariantes sont vérifiées par des tests, pas par des journaux.**
L'exemple le plus net est dans `_lesson_events` : chaque composante de
`Lesson.change_signature` doit correspondre à une branche d'événement, et cette
invariante est vérifiée dans `tests/test_delta.py` plutôt que gardée par un log
d'exécution que personne ne lit. Ajouter un champ à la signature sans ajouter la
branche fait échouer la CI, ce qui est le moment où cela peut encore être
corrigé à bon compte.

`tests/fixtures/protocol.py` fournit des charges utiles écrites à la main, et le
`pyproject.toml` explique pourquoi elles sont des datetimes **naïfs** : PRONOTE
transmet des heures locales sans offset, donc y attacher un fuseau testerait une
forme que le serveur n'envoie jamais. La passerelle est le seul endroit où un
fuseau est attaché, et c'est ce que `tests/test_gateway.py` vérifie.

### 11.4 L'internationalisation

Rien de visible par l'utilisateur n'est codé en dur dans
`custom_components/` : les libellés vivent dans `strings.json` (source anglaise)
et `translations/{en,fr}.json`. Les trois fichiers, plus `services.yaml`, sont
**générés** depuis une table unique par `scripts/build_translations.py`. Le
raisonnement est explicite : l'exigence est qu'un test vérifie que toute clé
présente dans une langue existe dans les autres avec les mêmes substitutions, et
maintenir trois fichiers à la main garde ce test durablement rouge pour des
raisons sans intérêt. Le générer depuis une table fait du test un vrai contrôle
**sur la table**.

---

## 12. Écarts avec la spécification

La source de vérité est le code. Ce qui suit relève les endroits où le dépôt
tel qu'il est diverge de ce que les documents de conception annoncent, ou bien
où une exigence est écrite mais pas appliquée. Les écarts sont classés par
nature, parce qu'ils ne demandent pas la même chose au lecteur : certains sont
des corrections délibérées que le code documente, d'autres des exigences
écrites que rien n'applique, d'autres encore de simples documents périmés.

### 12.0 Ce que ce chapitre ne contient pas

Sur l'essentiel, code et spécification concordent, et il vaut la peine de le
dire pour que la liste qui suit garde ses proportions. Concordent exactement :
les treize réglages du limiteur et leurs bornes, les trois seuils de sacrifice
(100 / 80 / 60 %) et le seuil d'avertissement à 80 %, la formule de repli et sa
gigue pleine, les dix paliers et leurs intervalles, les onze identifiants
d'onglet protocolaire, les trois codes `Erreur.G` traités, `stale_after = 6`,
les timeouts 30 s / 60 s et le facteur trois de détection d'interblocage, les
quinze capteurs primitifs et dix-sept capteurs de liste, les huit séries
d'historique indexées par période, les dix capteurs binaires — y compris le
retrait des trois « jumeaux » — les neuf entités de diagnostic, les huit
services et leurs quatre réponses `SupportsResponse.ONLY`, l'arithmétique de
budget à environ 180 requêtes de données par jour, et les portails de qualité
(95 % par module, 100 % sur sept, 80 % global).

### 12.1 Le domaine annoncé est `pronote`, le domaine réel est `carnet_scolaire`

`SPECIFICATION.md` déclare en en-tête le domaine **`pronote`** et présente le
renommage comme « une décision de fin de parcours, pas de début », puisqu'il
changerait tous les `entity_id`. L'annexe A nommait d'ailleurs les services
`pronote.refresh`, `pronote.get_ical_url`, etc., jusqu'à ce que son tableau soit
corrigé — un lecteur qui en recopiait une ligne appelait le domaine d'une autre
intégration. Le §1.3 de la spécification est
par ailleurs autocontradictoire sur ce point — « domaine `pronote` et non
`pronote` » — ce qui a tout l'air d'un artefact de recherche-remplacement.

Le code a tranché dans l'autre sens et l'a motivé
(`DOMAIN`, `const.py`) : le domaine est **`carnet_scolaire`**, les services sont
`carnet_scolaire.*`, et les blueprints vivent sous
`blueprints/automation/carnet_scolaire/` et non `blueprints/automation/pronote/`.
Le domaine `pronote` n'a jamais été pris, délibérément : une autre intégration
PRONOTE déjà installée peut l'occuper, et deux composants personnalisés
réclamant le même domaine ne cohabitent pas. Un domaine distinct permet
d'essayer cette intégration sans d'abord désinstaller celle dont on dépend — ce
qui est précisément l'objectif de cohabitation que la spécification énonçait,
atteint par le moyen inverse de celui qu'elle prescrivait. Le domaine a été
`pronote_ng` jusqu'à la 0.0.32, puis `carnet_scolaire` quand EcoleDirecte a
rejoint PRONOTE comme seconde source.

Le dépôt, lui, s'appelle `ha-carnet-scolaire`, et c'est sous le nom **Carnet scolaire**
que `hacs.json` et le champ `name` du manifest présentent l'intégration. Le
domaine est le seul endroit où la forme soulignée apparaît.

### 12.2 Le confinement de `pronotepy` n'est pas total

L'affirmation « `gateway.py` est le seul module à importer `pronotepy` »
(`gateway.py`) est vraie pour les **types de données**, et fausse pour les
**exceptions**. L'inventaire réel :

| Module | Ce qu'il importe |
| --- | --- |
| `gateway.py` | `dataClasses`, `DataError`, `ParsingError` |
| `hardened_client.py` | `pronotepy`, `dataClasses`, `pronoteAPI`, `PronoteAPIError` |
| `session.py` | `ChildNotFound`, `CryptoError`, `DataError`, `ExpiredObject`, `MFAError`, `PronoteAPIError` ; et `pronotepy.ent` à la ligne 870 |
| `__init__.py` | `PronoteAPIError` |
| `config_flow.py` | `pronotepy.ent` — import local, pour la liste des fournisseurs |
| `flow_login.py` | `CryptoError`, `ENTLoginError`, `MFAError`, `PronoteAPIError`, `QRCodeDecryptError` ; et `pronotepy.ent` |

La formulation exacte que le code soutient est donc : **aucun objet de données
`pronotepy` ne franchit la frontière**, et seuls `gateway.py` et
`hardened_client.py` en construisent ou en lisent. Les **classes
d'exception** et la résolution du fournisseur ENT traversent, et c'est
inévitable : `session.py` doit classer `CryptoError` contre `PronoteAPIError`
pour que le garde-fou IP fonctionne du tout (§5.4), et un module qui ne
connaîtrait pas ces types ne pourrait pas le faire. Cette traversée est bornée,
sans état, et sans risque de `Erreur.G = 22` — mais elle mérite d'être décrite
exactement, parce que « un seul fichier à relire lors d'un changement de
version » est faux : il y en a deux à relire pour les données, et quatre à
vérifier pour les exceptions.

### 12.3 La règle de détection de changement de l'emploi du temps est inversée

C'est l'écart le plus substantiel, et il est **assumé et documenté** par le
code (`delta.py`).

La spécification (§2.2.1) prescrit deux étapes obligatoires dans cet ordre :
dédoublonner par créneau en gardant le `num` maximal, puis comparer une liste
blanche `(canceled, status, classroom, teachers, start, end)` sur la **clé de
créneau `(date, place, subject_id)`** prise dans le JSON brut, et
**explicitement pas sur `N`**.

Le code fait exactement l'inverse sur les deux points, pour des raisons
vérifiées :

* il **indexe sur `N`** (`delta.py`) et non sur une clé de créneau,
  parce qu'une clé contenant le jour rend `lesson_moved` structurellement
  inatteignable — un cours déplacé change de clé et se lit comme deux entrées
  distinctes — tandis qu'un décalage de la grille horaire de l'établissement
  déplaçait `start` pour tous les cours de tous les créneaux et déclenchait un
  `lesson_moved` pour la semaine entière. Le `N` nomme toutefois le **cours**,
  pas une séance : un cours hebdomadaire porte le même `N` dans les deux
  semaines collectées. Il ne sert donc qu'à retrouver une séance dont le
  créneau s'est vidé (`_previous_key`) ; une entrée dont la clé figure parmi
  celles du `N` est appariée à elle-même. Une table `N` → clé unique gardait la
  séance de la semaine suivante, et chaque collecte comparait un cours à son
  jumeau sept jours plus tard : un `lesson_moved` par cours, et un
  `lesson_canceled` réannoncé toutes les vingt minutes pour un cours annulé
  cette semaine et pas la suivante ;
* il tourne sur la semaine **non dédoublonnée** (`facts.all_lessons`), parce
  que sur un remplacement PRONOTE sert l'entrée originale avec `estAnnule`
  *plus* un remplacement au `num` supérieur, et le dédoublonnage garde le
  remplacement — donc l'entrée portant l'annulation avait été écartée une
  couche plus bas et l'annulation était **inobservable**. Un créneau
  **couvert** — une autre entrée non annulée y est enseignée — n'émet
  pourtant pas `lesson_canceled` mais `lesson_replaced` (`_replacement`,
  §12.4) : c'est ainsi que PRONOTE sert aussi « Changement de salle » et
  « Cours maintenu », l'enfant a cours, et l'annonce d'une annulation y
  faisait lire un professeur absent ;
* `subject_id` est retiré de la clé de dédoublonnage, qui vaut
  `(date, place)` (`models.py`) : l'inclure défaisait le dédoublonnage
  qu'il sert, puisqu'une substitution — le cas pour lequel la règle est écrite —
  est justement deux entrées sur un créneau avec des matières différentes ;
* la liste blanche compare `frozenset(classrooms)` et `frozenset(teachers)` et
  non un `classroom` unique et une séquence de professeurs, parce que rien dans
  le protocole n'ordonne `ListeSalles` ni `ListeProfesseurs` et que PRONOTE les
  réordonne : en tuples, un cours co-enseigné émettait `lesson_changed` chaque
  nuit pour le reste de l'année.

Le dédoublonnage lui-même reste bien dans la passerelle
(`gateway.deduplicate_lessons`), comme la spécification l'exige. C'est
uniquement la **couche sur laquelle le détecteur travaille** qui change : la
passerelle expose les deux vues, `lessons` dédoublonnée pour l'affichage et
`all_lessons` pour le delta.

### 12.4 Les types d'événement du cours modifié sont sept et non quatre — divergence close

Cette divergence est **résolue** dans le même sens que la §12.9 : c'est
l'annexe qui a bougé, parce que c'est le code qui avait raison.

L'annexe A §4 ne déclarait que quatre `event_types` sur
`event.<élève>_cours_modifie` : `lesson_canceled`, `lesson_moved`,
`room_changed`, `teacher_changed`. Le code en déclare **sept**
(`LESSON_EVENT_TYPES`, `const.py`), et le tableau de l'annexe les porte
désormais tous — avec, au passage, les douze attributs de contexte réellement
émis par `_lesson_context` au lieu de sept. Les trois qui manquaient :

* **`lesson_restored`** — une annulation **levée**, le cours est rétabli. Son
  absence est ce qui faisait déclencher `lesson_canceled` pour l'événement
  exactement opposé par le repli « si rien d'autre n'a correspondu, appelons ça
  une annulation » : une automatisation qui notifie « pas de cours en première
  heure, dors » se déclenchait le matin où le cours revenait
  (`const.py`) ;
* **`lesson_status_changed`** — seul le libellé `Statut` a bougé, sans drapeau,
  sans horaire, sans salle et sans professeur. PRONOTE utilise ce champ pour
  des situations où il ne pose pas `estAnnule` — « Prof. absent », « Cours
  dépl. » — donc le changement est réel et vaut d'être rapporté ; il n'est
  simplement pas une annulation, ce qu'il était rapporté comme étant
  (`const.py`).

* **`lesson_replaced`** — le cours est annulé, mais une autre entrée est
  enseignée sur son créneau : remplacement, « Changement de salle », « Cours
  maintenu », que PRONOTE sert tous comme l'original marqué `estAnnule`
  plus l'entrée qui le remplace (`_replacement`, `delta.py`). L'enfant a
  cours ; le rapporter en `lesson_canceled` annonçait un professeur absent
  un matin d'évaluation dans une autre salle. L'évènement ajoute quatre
  attributs `replacement_*` (`_replacement_context`). Un créneau vidé deux
  fois — le remplaçant annulé à son tour — ne produit qu'un `lesson_canceled`,
  celui du remplaçant.

Corollaire côté automatisations : `TRIGGER_TYPES` (`device_trigger.py`) expose **quinze**
types de déclencheur, dont ces trois-là. Chacun n'est proposé — et accepté à la
validation — que si la source collecte le palier qui le produit : `TRIGGER_TIER`
lit ce palier sur les entités `event` (`EVENTS`, `event.py`) plutôt que de le
redire, si bien qu'une entrée EcoleDirecte, sans actualités, messagerie ni
évaluations, en reçoit douze.

Ce qui reste à surveiller n'est plus l'annexe mais la **duplication** : la même
liste vit maintenant dans `const.py`, dans le tableau du § 4.10 du guide et dans
celui du § 4 de l'annexe A. Un septième type ajouté au code sans toucher aux
deux documents recrée exactement l'écart qu'on vient de fermer.

### 12.5 Le plafond de connexions compte les tentatives, pas les succès

La spécification et l'annexe B §3 décrivent `max_logins_per_day` comme un
plafond sur les connexions **réussies**. Le code compte les **tentatives**
(`RateLimiter.logins_today`, `ratelimit.py`), et le dit :

> Attempts, not successes. The cap bounds how much authentication traffic this
> integration generates, and a failed attempt costs the server exactly as much
> as a successful one.

L'argument est solide et va dans le sens de l'objectif du plafond : ce qui doit
être borné est le trafic d'authentification envoyé à l'établissement, et une
tentative qui échoue coûte au serveur exactement ce qu'une réussie coûte. Mais
c'est bien un écart, et il déplace la sémantique du réglage : le plafond de 24
est un plafond de 24 *tentatives*, donc plus strict que celui que la
spécification décrit.

### 12.6 Le `README.md` décrit un état dépassé

`README.md` annonce en encadré : « **État : spécification.** Aucun code n'est
encore écrit. Ce dépôt contient pour l'instant la conception détaillée de
l'intégration. » Le dépôt contient environ 14 000 lignes de code de production
sur trente modules, plus de 400 tests, quatre workflows CI et quatorze
blueprints — les
jalons M1 à M7 de la spécification §12 sont donc tous atteints ou en cours,
alors que le document d'entrée du dépôt affirme qu'aucun ne l'est.

Le même fichier annonce « MIT (à créer avant la première publication) » alors
que `LICENSE` existe et que la spécification §11.1 faisait de son existence un
prérequis de publication : l'exigence est satisfaite, la note ne l'a pas suivi.

### 12.7 `MAX_INTERVAL_FOR_EVENT_TIERS` est déclaré mais jamais appliqué

La spécification §5.2 exige que « l'interface d'options refuse `timetable`
au-delà d'une heure et le signale », avec un argument juste : un cours annulé
annoncé quatre heures plus tard ne sert plus. `const.py` déclare la
table correspondante — `MAX_INTERVAL_FOR_EVENT_TIERS = {Tier.TIMETABLE: 60}` —
et reprend l'argument.

Cette constante n'a **aucun lecteur**. Le flow d'options borne les dix
intervalles par `TIER_INTERVAL_RANGE = (1.0, 1440.0)`
(`config_flow.py`) et `options.tier_intervals` les écrête contre la même
table globale. Rien n'empêche donc de régler `timetable` à 1440 minutes, ce qui
rend `event.<élève>_cours_modifie` structurellement inutile et supprime en
silence l'événement pour lequel l'intégration existe. L'exigence est écrite en
prose, déclarée en constante, et appliquée nulle part.

Quatre autres constantes sont déclarées sans lecteur, avec des conséquences
moindres : `CONFIGURABLE_TIERS`, `CONF_ACCOUNT_KIND`, `FUNC_TEACHING_STAFF` et
`FUNC_DISCUSSIONS` (les deux derniers parce que la passerelle passe par
`client.get_teaching_staff()` et `client.discussions()` plutôt que par un `post`
brut).

### 12.8 L'option `history_periods` n'est offerte nulle part

`const.py` déclare `OPT_HISTORY_PERIODS = "history_periods"` et
`DEFAULT_HISTORY_PERIODS = 0` (« 0 signifie toutes les périodes closes »).
`tiers.py` la lit — mais par une **chaîne littérale**, `"history_periods"`,
et non par la constante :

```python
limit = int(account.entry.options.get("history_periods", 0) or 0)
```

Et `config_flow.py` ne l'expose dans aucune de ses trois sections d'options,
alors que la spécification §7.3 la range explicitement dans la section
« Contenu » aux côtés de `establishment_timezone`, `homework_horizon`,
`wake_margin` et `write_operations_enabled` — les quatre autres, elles, sont
bien offertes. L'option est donc fonctionnelle mais inatteignable depuis
l'interface : seul un utilisateur qui édite `.storage` à la main peut la régler.
C'est aussi le seul endroit du dépôt où une clé d'option est écrite en dur au
lieu de passer par sa constante, ce qui prive le couple d'une vérification de
cohérence.

Sa sémantique diverge par ailleurs entre les deux documents et le code. Le code
pose `DEFAULT_HISTORY_PERIODS = 0` avec le commentaire « 0 signifie toutes les
périodes closes », et `tiers.py` traite bien 0 comme « aucune limite »
en ne tronquant la liste que si `limit` est vrai. La table de sensibilité de
l'annexe B §5.6, elle, attribue à `history_periods = 0` un effet de **−8
requêtes**, c'est-à-dire la suppression complète du palier — l'interprétation
opposée. Comme l'option n'est de toute façon pas atteignable, l'ambiguïté n'a
aucune conséquence observable aujourd'hui, mais il faudra la trancher avant de
l'exposer.

### 12.9 Le client durci n'hérite pas de `ParentClient` — divergence close

Cette divergence est **résolue**, et elle a été résolue dans le sens inverse de
celui qu'on attend d'ordinaire : c'est la spécification qui a bougé, parce que
c'est le code qui avait raison.

La spécification §3.6 nommait une sous-classe
`_NoRefreshParentClient(pronotepy.ParentClient)` surchargeant `post()`. Le code
implémente `HardenedClient(pronotepy.Client)` — une classe unique qui gère
l'élève **et** le parent — et l'écart n'était pas une commodité : `ParentClient`
est précisément la sous-classe qui porte le bug de récursion non bornée et celui
de l'enfant perdu. En hériter puis les neutraliser par surcharge laisse une
méthode d'amont ajoutée plus tard les réintroduire sans que rien ne le signale ;
réimplémenter la surface parent — liste d'enfants, `set_child()`, signature
`membre` — sur `Client` les rend **structurellement inatteignables** plutôt que
corrigés. Le §3.6 énonce désormais cette exigence, avec l'argument, et interdit
explicitement la forme par héritage.

Le confinement de `Period.instances`, que la spécification plaçait dans cette
même sous-classe, vit dans un objet et un gestionnaire de contexte séparés
(`_ConfinedRegistry` / `_period_registry_confined`) — ce qui permet de le poser
aussi sur le chemin d'enrôlement QR du flux de configuration, où il n'y a pas de
sous-classe du tout.

### 12.10 La stratégie de session est devenue une option, avec une dégradation mesurée

La spécification §6.5 décrit **une** stratégie, la reconnexion paresseuse,
accompagnée d'un argument de dominance : si le délai mesuré est inférieur à
l'intervalle du palier le plus rapide, la stratégie « dégénère exactement en la
conception v1 ».

Le code transforme cet argument en **deux mécanismes distincts**. D'une part,
`SessionStrategy` est une option d'entrée à deux valeurs — `LAZY` par défaut,
`PER_BATCH` conservé comme échappatoire explicite pour un établissement dont on
préfère épingler la politique plutôt que la découvrir (`const.py`).
D'autre part, la dégénérescence n'est plus seulement un argument : c'est
`effective_strategy`, une machine réelle déclenchée par trois expirations
consécutives et rendue falsifiable par une sonde quotidienne
(`session.py`), dont la lévée exige une contre-preuve. C'est une
extension de la spécification plutôt qu'une contradiction, mais elle ajoute une
option de configuration que le document ne prévoyait pas.

### 12.11 Les fixtures de test sont écrites à la main, pas enregistrées

La spécification §11 exige des « fixtures protocolaires **enregistrées et
anonymisées** », versionnées dans `tests/fixtures/`, et en fait la première
tâche du jalon M1.

Le dépôt contient bien `tests/fixtures/protocol.py` et `tests/fixtures/client.py`,
mais les charges utiles y sont **écrites à la main** : `tests/test_gateway.py`
le dit en toutes lettres — « the fake client answers from hand-written payloads.
See `tests/fixtures/` for why none of them came off a real server. »

L'objectif de la spécification — aucun test ne touche le réseau, aucune donnée
réelle d'élève dans le dépôt — est donc atteint, et même plus strictement,
puisqu'il n'y a jamais eu de réponse réelle à anonymiser. Mais le moyen prescrit
n'est pas celui employé, et la conséquence mérite d'être connue : les fixtures
reflètent la compréhension du protocole qu'a l'auteur, pas ce qu'un
établissement envoie réellement. C'est le point où le portail de couverture à
100 % sur `gateway.py` est le plus fragile, parce qu'une forme de réponse que
personne n'a imaginée n'a pas de test qui la couvre.

### 12.12 « Docker » n'existe pas dans le dépôt

Le dépôt ne contient ni `Dockerfile`, ni `docker-compose.yml`, ni `devcontainer`.
La seule mention de Docker est un message d'erreur dans `tests/conftest.py`
qui suggère « Linux, WSL ou Docker, comme le fait la CI » — et la CI, elle,
tourne directement sur `ubuntu-latest` avec `actions/setup-python`, pas dans un
conteneur.

La contrainte technique est réelle et correctement documentée : le harnais
importe `fcntl`, donc la suite complète ne peut pas tourner sous Windows. Mais
l'exécution « dans Docker » est une **façon possible** de contourner cela pour
un contributeur sous Windows, pas un dispositif présent dans le dépôt ni le
mécanisme employé par la CI. La formulation exacte est celle du §11.2.

La spécification, de son côté, ne mentionne Docker nulle part : ni image de
test, ni `devcontainer`, ni `docker-compose`. Elle exige seulement qu'aucun test
ne touche le réseau, ce que le dépôt respecte.

### 12.13 La version visée et la version testée coïncident désormais

Cette divergence est **résolue**, et la façon dont elle l'a été vaut d'être
notée : c'est un cas où l'écart documentaire cachait deux vrais défauts.

`SPECIFICATION.md` déclare en en-tête viser **Home Assistant 2026.9 ou
supérieur**. Le dépôt déclare maintenant le même plancher — **2026.9.0** dans
`hacs.json` — et `requirements_test.txt` l'épingle exactement :
`pytest-homeassistant-custom-component==0.13.363` embarque Home Assistant
2026.9.0. Les trois chiffres sont le même, la matrice exécute celui-là, et la
ligne flottante `latest` couvre l'autre bout de l'intervalle.

Le plancher était auparavant à 2026.2, puis à 2026.8. Chaque montée a été faite
en **exécutant** le socle visé, pas en modifiant le chiffre — et les deux fois
la seule exécution a trouvé un défaut produit qu'aucun portail ne voyait :
`DeviceInfo.via_device` (le tuple, retiré au profit de `via_device_id`), qui
faisait échouer l'ajout de six plateformes sur sept, et l'amplification par dix
des demandes de rafraîchissement du planificateur. C'est la leçon du §11 sous sa
forme la plus courte : **un plancher qu'on ne lance pas est une affirmation, pas
un plancher.**

Un effet de bord du plancher mérite d'être retenu : à partir de **2026.3**, le
dossier `brand/` local est servi par le *frontend* (voir le commentaire de
`BRAND_LOGO_URL` dans `const.py`), donc toute instance supportée affiche la
marque nativement. C'est ce qui rend envisageable le retrait de l'image
markdown de la description de la première étape du flux, sous réserve d'une
vérification visuelle.

### 12.14 Neuf blueprints, pas six

La spécification §2.3 énumère **six** blueprints. Le dépôt en livre **neuf** par
langue, soit dix-huit fichiers dans `blueprints/automation/carnet_scolaire/{fr,en}/` :
`wake_up_alarm`, `lesson_canceled`, `homework_reminder`, `new_grade`,
`absence_alert`, `canteen_menu` — les six prévus — plus trois :

- **`new_message`** couvre l'arrivée d'un message de la messagerie ou d'une
  actualité publiée par l'établissement ;
- **`end_of_day`** agit à la fin des cours, sur le capteur « Fin des cours » ;
- **`midday_return`** agit à la fin de la matinée, sur le capteur « Fin de
  matinée », avec une option qui dit si un cours annulé avance le retour ou si
  l'élève reste en permanence jusqu'à l'heure prévue (`scheduled_end`).

Ce sont des ajouts, pas des divergences de conception : ils exploitent des
déclencheurs et des capteurs qui existaient déjà au catalogue, et la
spécification exige un test par blueprint, ce qui vaut pour eux comme pour les
six autres — `tests/test_blueprint_midday_return.py` exécute le dernier dans
Home Assistant.

### 12.15 Divergences que le code a déjà consignées

Ces écarts-là ne sont pas des défauts : le code a corrigé la spécification et
l'a dit. Ils sont listés ici parce qu'un lecteur qui part des documents de
conception les rencontrera.

* **La stratégie de session est inversée.** La spécification v1 fermait la
  session entre les batches, sur un modèle de coût faux trois fois de suite.
  Le défaut est maintenant `LAZY`, avec l'argument de dominance et sa
  dégradation bornée (§5.2). `PER_BATCH` reste offert comme échappatoire.
* **La règle de détection de changement est dédoublée.** La règle unique
  « delta sur `N` » de la v1 ne pouvait pas implémenter le cours modifié
  (§7.4).
* **Le client durci est un prérequis**, pas une amélioration (§6.4).
* **Les paliers sont passés de treize à dix**, en repliant la semaine d'emploi
  du temps dans le palier `timetable` — puisque `Client.lessons()` facture par
  semaine — et en fusionnant les trois jeux de données de `PagePresence`.
* **Deux identifiants d'onglet de la v1 étaient faux** et sont corrigés avec un
  avertissement pour qu'un futur lecteur ne les « répare » pas en arrière
  (`FUNC_NEWS`, `const.py`) : la lecture du fil d'actualités est `PageActualites` et
  non `SaisieActualites`, qui est l'écriture ; la liste des discussions est
  `ListeMessagerie`.
* **Le budget par défaut est passé d'environ 423 à environ 180 requêtes par
  jour**, et le plafond journalier est fixé à 2000 comme filet de sécurité
  contre un défaut logiciel, soit un facteur onze.
* **Le palier `history` coûte 8 requêtes et non 6.** L'annexe déduisait trois
  requêtes par période close ; il en faut quatre, parce que
  `DernieresEvaluations` 201 y manquait (`REQUESTS_PER_BATCH`, `options.py`). Compter depuis le
  code bat compter depuis la prose. À noter que l'annexe a **depuis** été
  corrigée et affiche bien 8, avec les quatre onglets nommés : le commentaire
  du code qui reproche à l'annexe d'avoir déduit 6 est donc lui-même périmé, et
  c'est la seule contradiction interne encore ouverte entre les deux documents.
* **Le plafond de connexions journalières est 24 et non 120** : le design
  attend une à trois connexions par jour, et 120 ne pourrait pas attraper le
  défaut pour lequel le plafond existe (`const.py`).
* **`LimiterState` ne contient pas d'`ip_suspended`**, et la réparation de
  bootstrap ne nomme aucune cause, parce que `if "IP" in html` ne peut pas en
  établir une (§6.4).
* **Le palier `discussions` était budgété à une requête** ; la spécification
  n'avait pas anticipé que `Discussion.messages` poste à chaque lecture. Le
  développement sélectif plafonné à trois fils est la réponse (§4.2).
* **Le palier `static` ne collecte plus l'URL iCal**, pour la raison de §10.2.
