# Annexe B — limiteur de débit

Référence du §6 de [`SPECIFICATION.md`](SPECIFICATION.md). Ce document décrit le
module `ratelimit.py` : ce qu'il compte, comment il refuse, comment il se remet,
et pourquoi les valeurs par défaut sont celles-là.

> **Version 2.** L'arithmétique du §5 a été entièrement refaite : le modèle de
> coût de la v1 était faux sur trois points (coût du maintien de session, coût
> d'une connexion, unité de facturation de l'emploi du temps). Le budget par
> défaut passe de ≈ 423 à **≈ 180 appels par jour**, et la stratégie de session
> est inversée. Détail des arbitrages au §14 de la spécification.

---

## 1. Le problème posé

PRONOTE ne publie aucune limite de débit. Il applique des sanctions, et elles ne
visent pas le même geste :

| Sanction | Déclencheur | Gravité |
| --- | --- | --- |
| Session cassée | deux appels concurrents désynchronisent le compteur chiffré | immédiate, récupérable par reconnexion |
| `Erreur.G = 8` ou `10` | session expirée par inactivité | bénigne, une reconnexion suffit |
| `Erreur.G = 25` | trop de demandes d'autorisation | il faut attendre ; insister aggrave |
| **Suspension d'adresse IP** | connexions échouées répétées | durée non documentée ; **la détection d'amont n'est pas fiable, cf. §3.4** |

La dernière ligne est la seule vraiment coûteuse : elle ne se contourne pas, ne
s'explique pas à un utilisateur, et touche toute la maison, pas seulement
l'intégration. Un limiteur qui ne compterait que les appels manquerait sa
cible — ce sont les **connexions échouées** qui font perdre l'accès.

D'où la structure : deux comptabilités séparées, l'une pour le débit, l'autre
pour l'authentification.

### 1.1 `G = 25` compte peut-être les connexions, pas les appels

Le libellé de cette erreur parle de « demandes d'**autorisation** ». La v1 la
nommait correctement puis budgétait contre le volume d'*appels* — deux grandeurs
différentes. Si la grandeur réellement mesurée par le serveur est celle des
autorisations, alors le risque se pilote par le **nombre de connexions par
jour**, et une conception qui reconnecte à chaque lot multiplie par soixante la
seule variable dangereuse pour économiser celle qui ne l'est probablement pas.

C'est le raisonnement qui a inversé la stratégie de session (§6.5 de la
spécification). Il repose sur une lecture du libellé, pas sur une mesure : d'où
l'exigence de **mesurer** la durée de vie de session au lieu de parier.

**Exigence.** Le limiteur compte les deux grandeurs séparément et expose les
deux. Tant que celle qu'observe PRONOTE reste inconnue, la conception minimise
celle dont la sanction est documentée.

### 1.2 Une requête, ce n'est pas un POST

**Exigence.** Le limiteur compte les **requêtes HTTP**, pas les appels
protocolaires. Une connexion vaut 1 GET (l'amorçage, qui rapporte les attributs
`Start({…})`) + 4 POST, plus 1 à 2 `SecurisationCompteDoubleAuth` en mode jeton :
**5 à 7 requêtes**. La v1 comptait 4 et sous-estimait donc son propre budget.

---

## 2. Comptabilité des appels — trois couches

Les trois couches s'appliquent dans l'ordre. Un appel doit franchir les trois.

### 2.1 Couche 1 — espacement minimal

**But** : interdire toute rafale, quelle que soit la logique en amont.

```python
async def _space(self) -> None:
    elapsed = self._clock() - self._last_call
    if elapsed < self.min_request_interval:
        await asyncio.sleep(self.min_request_interval - elapsed)
    self._last_call = self._clock()
```

Défaut : **1,0 s**. Un lot de treize requêtes — le pire cas calculé au §5 —
s'étale donc sur treize secondes, ce qui est indolore pour l'utilisateur et
invisible pour le serveur.

### 2.2 Couche 2 — seau à jetons horaire

**But** : lisser la charge sur l'heure, tout en laissant passer un lot complet
d'un coup.

Remplissage continu, à `max_requests_per_hour / 3600` jeton par seconde ;
capacité maximale `burst_size`.

```python
def _refill(self) -> None:
    now = self._clock()
    rate = self.max_requests_per_hour / 3600
    self._tokens = min(self.burst_size, self._tokens + (now - self._refilled) * rate)
    self._refilled = now
```

Défauts : **240 par heure**, **capacité 20**. La capacité est ce qui permet à un
lot complet de partir sans attendre ; le débit est ce qui empêche quatre lots par
heure de devenir quarante.

**Exigence.** Un appel qui ne trouve pas de jeton **attend**, il n'échoue pas —
sauf si l'attente dépasse `max_wait` (60 s par défaut), auquel cas la collecte
du palier est reportée à la prochaine échéance et le palier est marqué
`throttled`.

### 2.3 Couche 3 — plafond du jour

**But** : filet de sécurité contre un défaut logiciel — une boucle, un palier
mal configuré, une régression qui rétablit un accès de propriété coûteux.

Défaut : **2 000 appels**, remis à zéro à minuit dans le fuseau de Home
Assistant.

**Exigence.** À 80 % du plafond, l'intégration ouvre une réparation
informative. À 100 %, seuls les paliers de priorité `critique` passent, et le
capteur `etat_limiteur` vaut `throttled` avec le motif `daily_cap`.

**Exigence.** Le plafond n'est jamais silencieux. Un limiteur qui coupe sans le
dire produit exactement le symptôme le plus difficile à diagnostiquer : des
données qui vieillissent sans erreur dans le journal.

### 2.4 Dégradation par priorité

Quand le budget se resserre, l'ordre de sacrifice est déterminé, pas arbitraire.

| Priorité | Paliers | Comportement en budget contraint |
| --- | --- | --- |
| `critique` | `session` | toujours servi |
| `haute` | `timetable`, `homework` | servi jusqu'au plafond du jour |
| `normale` | `news`, `marks`, `attendance` | reporté dès 80 % du plafond |
| `basse` | `discussions`, `evaluations`, `menus`, `static`, `history` | reporté dès 60 % du plafond |

**Exigence.** Un palier reporté n'est jamais abandonné : son échéance est
repoussée, jamais annulée. Et il conserve son instantané précédent, donc ses
entités gardent leur valeur (§5.4 de la spécification).

---

## 3. Comptabilité de l'authentification

Séparée, avec des seuils bien plus stricts, parce que la sanction est bien plus
lourde.

| Option | Défaut | Rôle |
| --- | --- | --- |
| `max_logins_per_day` | **24** | garde-fou sur les connexions **réussies** |
| `max_failed_logins_per_hour` | 3 | garde-fou sur les connexions **échouées** |
| `credentials_hold` | 3 600 s | attente après épuisement des tentatives |
| `bootstrap_hold` | 3 600 s | attente après un amorçage impossible |

Le déséquilibre entre 24 et 3 est voulu et c'est le cœur du dispositif. Une
connexion réussie est un geste normal et bon marché ; une connexion échouée est
le symptôme d'un identifiant faux, et **rien de bon ne sort de la réessayer**.

**Exigence.** `max_logins_per_day` vaut **24**, et non 120 comme en v1. La
conception attend une à trois connexions par jour (§6.5 de la spécification) :
un plafond à 120 ne pourrait pas attraper le défaut pour lequel il existe.

### 3.1 Classer l'échec sur la bonne exception

C'est le point le plus important de ce module, et la v1 ne nommait pas
l'exception.

Un mot de passe faux **ne lève pas** de `PronoteAPIError` : le déchiffrement du
défi échoue et `_login` lève **`CryptoError`**, ou bien `_login` renvoie
`False` quand la clé `cle` est absente de la réponse d'`Authentification`.

**Exigence.** Le compteur d'échecs s'incrémente sur `CryptoError` **et** sur
`logged_in is False`. Il ne s'incrémente **pas** sur une erreur HTTP ni sur une
`PronoteAPIError` — celles-là relèvent du repli du §4.

Accroché à la mauvaise exception, le compteur censé protéger l'adresse IP ne
s'incrémenterait jamais : le garde-fou existerait dans la documentation et pas
dans le fonctionnement.

### 3.2 Épuisement des tentatives

**Exigence.** Au troisième échec dans l'heure :

1. arrêt de toute tentative de connexion ;
2. ouverture d'une réparation `invalid_credentials`, traduite, invitant à
   vérifier les identifiants ou à relancer l'enrôlement par QR code ;
3. attente `credentials_hold` avant toute nouvelle tentative ;
4. `etat_limiteur` = `credentials_hold`, avec `until`.

**Exigence.** Une ré-authentification déclenchée par l'utilisateur
(`async_step_reauth`) remet les compteurs d'échec à zéro : c'est un geste humain
délibéré, avec un identifiant potentiellement corrigé, pas un réessai
automatique.

### 3.3 L'enrôlement QR est exclu du compteur

**Exigence.** `qrcode_login` effectue **deux** connexions complètes par
construction — un client construit, un `PageInfosPerso` 49, puis un
`token_login` avec les identifiants exportés. Ce doublement volontaire est exclu
du compteur d'échecs, sans quoi le chemin d'installation le plus recommandé
consommerait les deux tiers du garde-fou.

### 3.4 N'affirmez jamais la suspension d'adresse IP

`pronotepy` décide « adresse IP suspendue » avec `if "IP" in html` : deux
majuscules, n'importe où dans la page. « Espace IP » dans un pied de page,
« SKIP », « ZIP », « EQUIPE », une classe CSS, une bannière ENT — tout déclenche
la détection.

La v1 bâtissait là-dessus une attente d'une heure, l'arrêt complet des appels et
une réparation « au texte explicite ». Elle aurait donc annoncé à des parents
que leur adresse est bannie à cause d'un pied de page, tout en s'arrêtant
réellement.

**Exigence.** Un amorçage impossible est traité comme tel : attente
`bootstrap_hold`, `etat_limiteur` = `bootstrap_failed`, et une réparation dont le
texte dit que la connexion n'a pas pu être établie et **énumère** les causes
possibles sans en choisir une.

**Exigence.** Le seul test d'amorçage retenu est l'**absence du bloc
d'attributs `Start({…})`**. Même dans ce cas, la réparation ne nomme pas la
cause. L'état `ip_suspended` de la v1 est supprimé : un état que l'intégration ne
peut pas établir de façon fiable ne doit pas exister dans son vocabulaire.

**Pourquoi cette exigence, plutôt qu'une détection simplement plus fine.** Le
coût d'une cause fausse n'est pas symétrique. « La connexion n'a pas pu être
établie » fait chercher : on vérifie le mot de passe, on ouvre PRONOTE dans un
navigateur, on attend une heure. « Votre adresse est bannie » fait **renoncer**,
et fait porter l'accusation à l'établissement. Une intégration qui se trompe
dans le premier sens coûte du temps ; dans le second, elle fait abandonner une
installation qui fonctionnait. C'est pourquoi la règle n'est pas « détecter
mieux » mais « nommer le symptôme et énumérer les causes », et elle se généralise
bien au-delà du cas de l'adresse IP.

**Et cette exigence est tenue par deux tests, pas seulement écrite.** La
divergence dans `hardened_client.py` est une **suppression**, ce qui est
invisible à la lecture : quelqu'un comparant le code à celui d'amont verra un
contrôle manquant et sera tenté de le rétablir.
`test_a_page_without_a_start_block_is_a_bootstrap_condition`
(`tests/test_hardened_client.py`) exige `"IP" not in str(error)` sur plusieurs
pages inutilisables ; `test_the_two_capitals_upstream_reads_as_a_ban_are_ignored`
pose trois mots français ordinaires contenant les lettres `IP` sur une page
saine et vérifie qu'aucun ne produit de suspension. Le second existe pour
**expliquer le contrôle absent** à qui diffe, et non seulement pour couvrir une
branche.

---

## 4. Repli exponentiel

S'applique aux erreurs de transport et à `Erreur.G = 25`, par compte.

```
attente = min(backoff_max, backoff_base × 2 ** (échecs - 1)) × (0,5 + random())
```

Défauts : `backoff_base` = 30 s, `backoff_max` = 3 600 s. La gigue pleine est
là pour éviter que plusieurs instances Home Assistant d'un même établissement
ne se resynchronisent sur le même créneau après une panne du serveur.

| Échec consécutif | Attente nominale | Plage avec gigue |
| --- | --- | --- |
| 1 | 30 s | 15 – 45 s |
| 2 | 60 s | 30 – 90 s |
| 3 | 120 s | 60 – 180 s |
| 4 | 240 s | 120 – 360 s |
| 5 | 480 s | 240 – 720 s |
| 8 et plus | 3 600 s | 1 800 – 5 400 s |

**Exigence.** Le compteur d'échecs revient à zéro sur la **première** collecte
réussie, pas progressivement.

**Exigence — prérequis.** `Erreur.G = 25` ne doit **jamais** atteindre le
gestionnaire d'amont. `ClientBase.post` intercepte toute `PronoteAPIError` et
déclenche une poignée de main complète : sur `G = 25`, `pronotepy` répond à une
sanction sur les demandes d'autorisation par… une demande d'autorisation. Le
client durci du §3.6 de la spécification est donc un prérequis de ce repli, pas
une amélioration séparée.

**Exigence.** Sans ce client durci, l'exigence du §6 — « le limiteur est le seul
chemin » — est **fausse** : un appel budgété peut consommer six requêtes réseau
sans que le limiteur en voie une seule.

**Exigence.** `Erreur.G = 22` (objet d'une session antérieure) n'entre pas dans
le repli. Ce n'est pas une surcharge du serveur, c'est un défaut de conception
côté client : l'architecture du §3.1 de la spécification l'exclut, et si
l'erreur apparaît, elle doit être journalisée comme un bug de l'intégration, pas
absorbée comme un aléa réseau.

---

## 5. Arithmétique du budget par défaut

Fenêtre active de 06:00 à 22:00, soit **16 heures**. Battement maître de 5 min.
Le palier le plus rapide, `timetable`, est à 15 min, soit **64 lots par jour**.

### 5.1 Le modèle de coût de la v1 était faux sur trois points

La v1 concluait qu'il fallait fermer la session entre les lots. La conclusion
était fausse, et les trois erreurs valent d'être nommées : ce sont des erreurs
de *modèle*, invisibles à la relecture du document et visibles en relisant la
source.

| Ce que disait la v1 | Ce que dit la source | Effet |
| --- | --- | --- |
| Maintien = 523 appels sur 16 h | `_KeepAlive.alive()` ne ping que si la session est inactive depuis 110 s, et `last_ping` est remis à jour par **chaque** POST : le trafic de données déplace les pings, soit ≈ 523 − 167 ≈ **356** | l'écart tombe de ×2 à ×1,4 |
| Connexion = 4 appels | 1 GET d'amorçage + 4 POST, plus 1 à 2 `SecurisationCompteDoubleAuth` en mode jeton : **5 à 7 requêtes** | 64 × 5 = 320, pas 256 |
| Emploi du temps facturé au jour | `Client.lessons()` boucle **par semaine** et filtre côté client | « aujourd'hui + demain » coûte le même appel que la semaine |

La comparaison honnête devient donc **356 contre 320 à 448** : un match nul, pas
un facteur deux.

### 5.2 Trois coûts que la v1 ne chiffrait pas

Ils sont ce qui a réellement inversé la décision.

**Il n'existe aucune déconnexion dans `pronotepy`.** « Fermer la session » se
réduit à `communication.session.close()`, qui jette le pool TCP côté client. La
session serveur vit jusqu'à son propre délai d'inactivité. La v1 ne créait donc
pas 64 sessions successives mais **64 sessions qui se chevauchent**, et
64 `derniereConnexion` par jour dans le journal que PRONOTE présente à
l'établissement — exactement la signature que cette annexe cherche à ne pas
dessiner.

**La rotation du jeton.** En mode jeton, chaque `Authentification` renvoie un
nouveau `jetonConnexionAppliMobile` que le §7.2 de la spécification exige de
réenregistrer. Soit 64 réécritures de `.storage` par jour, et surtout **64
fenêtres par jour** pendant lesquelles un arrêt brutal entre la rotation serveur
et la persistance locale laisse un jeton mort et une intégration verrouillée,
récupérable seulement en rescannant un QR code.

**La double authentification.** PRONOTE décide seul quand redemander le PIN, et
le §8.1 interdit de le conserver. 64 connexions par jour, c'est 64 occasions
quotidiennes d'être interrogé sans PIN sous la main.

### 5.3 La stratégie retenue et son coût

Reconnexion **paresseuse** sur les codes d'expiration
(`SESSION_EXPIRED_CODES` : `Erreur.G = 8` et `10`), durée de vie de session
**mesurée** (§6.5 de la spécification). Et, indépendamment de tout code, une
session sans un seul appel réussi depuis `PRESUMED_DEAD_AFTER_SECONDS` est
abandonnée plutôt que réutilisée : détenir un client prouve qu'une connexion a
réussi un jour, pas que le serveur la reconnaît encore. Si le délai d'inactivité de
l'établissement dépasse l'intervalle de `timetable` — le réglage courant est
30 min pour un `timetable` à 15 min — le trafic de données maintient la session
seul, et les connexions tombent à **une à trois par jour**.

| Palier | Intervalle | Lots / jour | Requêtes par lot | Total |
| --- | --- | --- | --- | --- |
| `timetable` | 15 min | 64 | 2 (semaine courante + suivante, cf. §5.2 de la spéc.) | 128 |
| `homework` | 30 min | 32 | 1 (l'année entière, cf. §5.2 de la spéc.) | 32 |
| `news` | 1 h | 16 | 1 | 16 |
| `discussions` | 1 h | 16 | 2 (liste + une expansion moyenne, cf. note) | 32 |
| `marks` | 3 h | 6 | 2 (`DernieresNotes` + bulletin courant) | 12 |
| `attendance` | 6 h | 3 | 1 | 3 |
| `evaluations` | 12 h | 2 | 1 | 2 |
| `menus` | 24 h | 1 | 1,14 | 1 |
| `agenda` | 24 h | 1 | 1 (`PageAgenda` 9, PRONOTE seulement) | 1 |
| `static` | 24 h | 1 | 1 (équipe pédagogique seule) | 1 |
| `history` | 24 h | 1 | 8 (2 périodes closes × 198/13/19/**201**) | 8 |
| | | | **Données** | **236** |
| Connexions | paresseuses | 1 à 3 | 5 à 7 | **5 à 21** |
| | | | **Total** | **≈ 254** |

Contre ≈ 423 annoncés en v1 — et ≈ 487 réels avec le modèle corrigé. La
conception v2 coûte donc **un peu plus de la moitié** de la v1, pour une
fraîcheur *supérieure* sur la semaine d'emploi du temps.

`timetable` a coûté 1,14 jusqu'à ce que l'horizon soit corrigé, et les deux
chiffres méritent d'être comparés parce que l'écart est la mesure du défaut.
Le 0,14 était le jour sur sept où *demain* tombait dans la semaine suivante :
autrement dit, la semaine suivante n'était demandée que le dimanche, et l'état
du lundi restait inconnu du samedi 00:00 jusqu'à la première collecte du lundi.
Les 55 requêtes par jour que coûte la correction se lisent contre le plafond de
2 000 et contre les ≈ 253 dépensées, soit un quart de requête par minute de
plus. Hors année scolaire le palier ne coûte rien du tout, l'été effaçant à lui
seul plus que ce que la correction ajoute sur l'année.

**Deux lignes de ce tableau ont été corrigées en relisant le code plutôt que
la prose, et il vaut la peine de dire lesquelles.**

`history` coûte **huit** requêtes, non six : quatre par période close, pas
trois. `DernieresNotes` 198, `PageBulletins` 13, `PagePresence` 19 — et
`DernieresEvaluations` **201**, qui n'apparaît ni dans la spécification ni dans
la première version de cette annexe. L'onglet existe pourtant, le palier
`evaluations` l'interroge pour la période courante, et rien ne justifiait de
l'omettre pour les périodes closes.

`discussions` coûte **deux** requêtes en moyenne, non une. La liste des fils
en vaut une ; `pronotepy.Discussion.messages` republie `ListeMessages` à
**chaque lecture**, et la passerelle déplie les fils dont le compteur de
non-lus a monté, plafonnés à `MAX_DISCUSSION_EXPANSIONS` = 3 par cycle. Un
compte qui reçoit un message par cycle paie donc deux requêtes ; un compte
inactif en paie une, un compte très actif jusqu'à quatre.

Ces deux chiffres ne sont plus critiques pour la sûreté du limiteur, et c'est
délibéré : `GatewayResult.calls` remonte le **coût réel** de chaque appel, et
`RateLimiter.reconcile()` en débite la différence. Une déclaration trop basse
se rattrape à l'arrivée ; seule une déclaration trop *haute* gaspillerait du
budget, ce qu'aucune de ces valeurs ne fait.

### 5.4 La stratégie paresseuse ne peut pas être pire

C'est l'argument décisif, et il ne dépend d'aucune mesure.

Si le délai d'inactivité mesuré se révèle **inférieur** à l'intervalle de
`timetable`, chaque lot trouve la session morte et ouvre une connexion : la
stratégie paresseuse **dégénère exactement en la conception v1**. Elle n'est
donc jamais pire, pour toute valeur du paramètre inconnu, et strictement
meilleure dès que le délai est généreux.

**Exigence.** Avant la première mesure, le comportement par défaut est le
pessimiste. La mesure relâche la contrainte, elle ne la pose pas — sans quoi la
propriété ci-dessus ne tient plus.

**Exigence.** Le maintien actif (`_KeepAlive`) n'est pas utilisé. Il n'aurait
d'intérêt que pour un intervalle inférieur à 110 s, et aucun palier n'y descend.

### 5.5 Marges

| Grandeur | Consommation | Plafond par défaut | Marge |
| --- | --- | --- | --- |
| Requêtes par jour | ≈ 198 | 2 000 | ×10 |
| Requêtes dans l'heure la plus chargée | ≈ 25 | 240 | ×9,5 |
| Requêtes dans le lot le plus chargé | ≈ 20 | 20 (capacité) | ×1,0 |
| Connexions par jour | 1 à 3 | 24 | ×8 |

**Conclusion.** Les plafonds ne contraignent pas le fonctionnement normal ; ils
attrapent une anomalie. C'est la posture voulue : un limiteur qui bride en usage
courant serait réglé trop bas, et l'utilisateur le désactiverait.

Le seul rapport serré reste la capacité du seau face au lot maximal, et il
s'est **resserré** avec la correction de `history` (×1,05 contre ×1,5
annoncé), puis avec le palier `agenda` (×1,0). Le lot maximal — les onze
paliers échus au même instant, ce qui arrive au démarrage et une fois par jour — vaut ≈ 20 requêtes pour une capacité de 20.

Ce n'est pas un défaut, mais il faut dire pourquoi.

D'abord, le seau se **remplit pendant** que le lot se déroule : 240 requêtes
par heure, soit 4 par minute, et l'espacement minimal de 1 s étale déjà un lot
de 20 requêtes sur au moins 20 s. Le seau n'est jamais vidé d'un coup.

Ensuite, si la capacité venait quand même à manquer, la dégradation est celle
que §2.4 prescrit et non une panne : les quatre paliers qui font le lot
maximal — `history`, `static`, `menus`, `evaluations` — sont tous de priorité
**basse**, donc les premiers reportés, et `timetable` (haute) passe avant eux.
Le lot maximal se contente d'être le lot qui exerce le mécanisme de sacrifice.

Enfin, agrandir la capacité serait le mauvais remède : un seau beaucoup plus
grand resterait plein en permanence et annulerait le lissage qui est sa raison
d'être. Le bon réglage, si un établissement se montrait tatillon, est
d'échelonner les paliers quotidiens.

**Une réserve sur ce remède, à lever avant de s'en servir.** `history_periods`
est le levier naturel — borner le nombre de périodes closes suivies borne le
palier le plus lourd — et il est **inatteignable depuis l'interface** :
`config_flow.py` ne l'expose dans aucune de ses trois sections d'options, si
bien que seul un utilisateur qui édite `.storage` à la main peut le régler
(ARCHITECTURE § 12.8, où sa sémantique est également ambiguë). Tant que ce
n'est pas corrigé, ce paragraphe décrit un réglage que personne ne peut
appliquer, et il faut le lire ainsi.

### 5.6 Sensibilité

| Changement | Effet sur le total |
| --- | --- |
| Délai d'inactivité serveur < 15 min | 198 → **≈ 347** (dégénérescence en v1, cf. §5.4) |
| `timetable` porté à 30 min | 198 → **≈ 160** |
| Heures creuses désactivées | 198 → **≈ 284** |
| `master_tick` porté à 1 min | aucun effet : les échéances, pas le battement, fixent le coût |
| Un second enfant sur le même compte | +180 environ (données seulement ; la session est partagée) |
| `history_periods` = 0 | −8 |

La première ligne est la borne haute du risque : même dans le pire cas du
paramètre inconnu, on reste sous les 487 requêtes du modèle v1 corrigé.

**La dernière ligne, elle, est la seule du tableau qui se périme toute seule, et
dans le sens qui sous-estime.** `REQUESTS_PER_BATCH[Tier.HISTORY]` vaut **8**,
et son commentaire dit d'où sortent ces huit : **quatre requêtes par période
close** — `DernieresNotes` 198, `PageBulletins`, `PagePresence` 19 et
`DernieresEvaluations` 201 — pour **deux** périodes closes. Ce facteur deux est
une constante : l'estimateur ne compte les périodes de personne, et
`periods_for(Tier.HISTORY)` n'y apparaît pas.

Or `periods_for` suit **toutes** les périodes closes, sans plafond. Un
établissement qui publie plusieurs découpages de la même année — trimestres et
semestres — en annonce jusqu'à huit, donc sept closes en fin d'année, donc
**vingt-huit requêtes par jour là où l'estimation en compte huit**. Le total
affiché à l'utilisateur est juste en début d'année scolaire et le sous-estime
d'une vingtaine de requêtes en fin d'année. La marge de ×10 encaisse l'écart
sans difficulté — ce n'est pas un risque de sanction, c'est une estimation qui
cesse d'être vraie sans que rien ne le signale, et c'est précisément ce que la
phrase ci-dessous s'engage à éviter.

Conséquence pour qui lit les capteurs de diagnostic : `calls_today` monte
lentement de mois en mois à réglages constants, et **c'est normal**. Chaque
trimestre qui se ferme ajoute quatre requêtes quotidiennes définitives.

Les chiffres de cette section ne sont pas recalculés à la main : ils sortent de
`options.estimate_daily_requests()`, la fonction que l'interface de
configuration affiche à l'utilisateur, et un test les fige. Une annexe qui
diverge du code est pire qu'une annexe absente, puisqu'on la croit. La
troisième mesure ce que les heures creuses font gagner ; la quatrième dit que le
battement maître peut être réglé finement sans coût.

## 6. Interaction avec l'ordonnanceur

**Exigence.** Le limiteur est le **seul** chemin vers la passerelle. Signature
unique :

```python
async def call(self, tier: str, priority: Priority, fn: Callable[[], T]) -> T
```

Il n'existe aucune fonction publique de la passerelle qui ne passe pas par là.
Un bouton de rafraîchissement obtient une priorité relevée pour le prochain
battement — il n'obtient pas de dérogation, et il ne court-circuite pas
l'espacement.

**Exigence.** Un palier qui n'a **rien produit** n'est pas réessayé plus de
quatre fois dans son propre intervalle.

Le repli du §4 ne peut pas porter cette règle : il compte les échecs
*consécutifs du compte* et revient à zéro dès qu'un autre palier réussit. Il ne
dit donc rien d'un palier qui échoue pour une raison qui n'appartient qu'à lui —
un établissement qui ne publie pas d'équipe pédagogique, par exemple. Un tel
palier était reporté d'une seule base de repli, environ trente secondes, ce qui
est **inférieur au battement maître** : un palier déclarant une collecte par
jour était tenté à chaque battement, 288 fois par jour, et le budget que lui
donne le §5.3 était faux du même facteur, sans que rien n'échoue pour le dire.

Le plancher vaut donc `intervalle / 4`. C'est un minimum et non un
remplacement : une attente plus longue demandée par le limiteur est respectée,
et le plafond de l'intervalle exigé au §2.4 continue de s'appliquer par-dessus.
Quatre plutôt qu'une parce qu'un échec passager mérite un nouvel essai avant
demain.

**Exigence.** Un palier simplement **reporté** n'est pas concerné par ce
plancher. Reporter et échouer sont deux situations distinctes et ne doivent pas
recevoir la même réponse : un palier que le limiteur a retenu doit revenir dès
que le budget le permet. L'y appliquer dépenserait une pression sur le bouton de
rafraîchissement pour une collecte qui n'a pas eu lieu, puis garderait le
silence pendant les trois quarts de l'intervalle.

**Exigence.** Un palier qui n'a **jamais** produit d'instantané pour un enfant
collecte en priorité `critical`, et passe donc devant les heures creuses. C'est
la seule dispense, et elle est celle que le §2.4 accorde déjà à la connexion
elle-même.

La raison est le §2.5 de la spécification : une entité `unavailable` casse les
automatisations, elle ne se contente pas d'être vide. Sans cette dispense, une
instance redémarrée à 23h00 — ou installée à 23h00 — ne publiait **rien** avant
06h00 et donnait toutes les apparences d'une intégration cassée, au moment
précis où quelqu'un la regarde.

Elle s'auto-limite, et c'est ce qui la rend acceptable : dès que le palier
détient un instantané, la dispense cesse de s'appliquer. C'est donc un lot par
palier et par enfant, jamais une exemption permanente. Un palier qui **échoue**
reste sans donnée mais ne s'emballe pas : le plancher `intervalle / 4` ci-dessus
borne ses reprises, et le maintien de repli est évalué **avant** la branche des
heures creuses, donc il s'applique aussi à lui.

**Exigence.** L'horaire et la donnée traversent un rechargement **ensemble**, ou
ni l'un ni l'autre. Transmettre l'échéance d'un palier dont l'instantané n'a pas
survécu est le défaut qui a vidé un tableau de bord réel : le nouvel
ordonnanceur croyait les paliers frais, `due()` ne rendait rien, et les entités
sont restées `unavailable` pendant tout un intervalle — vingt-quatre heures pour
`menus`, `static` et `history`, et jusqu'à 06h00 pendant les heures creuses,
où le limiteur ne pouvait pas les regarnir. Un palier sans donnée est donc dû
immédiatement, quelle que soit l'échéance héritée.

Les instantanés voyagent dans `hass.data` et **jamais** sur disque. Un
instantané relu depuis le disque après un redémarrage serait une donnée d'âge
inconnu présentée comme courante, ce que `fetched_at` et la règle de péremption
existent précisément pour éviter. Dans un même processus l'appariement est sain ;
après un vrai redémarrage le magasin est vide, tous les paliers sont donc dus, et
la première collecte a lieu — ce qu'un redémarrage doit faire.

**Exigence.** Le limiteur ne connaît ni PRONOTE ni Home Assistant. Il prend une
horloge injectable et rend des décisions. C'est ce qui le rend testable à 100 %
sans réseau ni instance, comme l'exige le §11 de la spécification.

```python
class RateLimiter:
    def __init__(self, config: RateLimitConfig, clock: Callable[[], float]) -> None
```

---

## 7. Options — référence

| Option | Défaut | Plage acceptée | Effet |
| --- | --- | --- | --- |
| `min_request_interval` | 1,0 s | 0,2 – 10 | espacement entre deux appels |
| `max_requests_per_hour` | 240 | 30 – 2 000 | débit de remplissage du seau |
| `burst_size` | 20 | 5 – 100 | taille du lot autorisé d'un coup |
| `max_requests_per_day` | 2 000 | 100 – 20 000 | plafond du jour |
| `max_wait` | 60 s | 5 – 300 | au-delà, le palier est reporté |
| `max_logins_per_day` | 24 | 5 – 500 | connexions réussies |
| `max_failed_logins_per_hour` | 3 | 1 – 10 | connexions échouées |
| `credentials_hold` | 3 600 s | 300 – 86 400 | attente après échecs |
| `bootstrap_hold` | 3 600 s | 600 – 86 400 | attente après un amorçage impossible |
| `backoff_base` | 30 s | 5 – 300 | base du repli exponentiel |
| `backoff_max` | 3 600 s | 60 – 21 600 | plafond du repli |
| `quiet_hours_enabled` | vrai | booléen | activation des heures creuses |
| `quiet_start` | 22:00 | heure | début des heures creuses |
| `quiet_end` | 06:00 | heure | fin des heures creuses |

**Exigence.** Les bornes sont validées dans le flux d'options, avec un message
traduit. `min_request_interval` en dessous de 0,2 s est refusé : il n'existe
aucun usage légitime, et le protocole est de toute façon sériel.

**Exigence.** Le flux d'options affiche, à côté des champs, **l'estimation du
budget quotidien** résultant des valeurs saisies, calculée par la même fonction
que le §5. Un réglage dont on ne voit pas la conséquence se règle au hasard.

---

## 8. Exigences de test

`ratelimit.py` est à 100 % de couverture. Les cas qui doivent être couverts
nommément, parce qu'ils sont ceux qui échouent en production :

| Cas | Attendu |
| --- | --- |
| Horloge injectée, avance simulée | aucun `sleep` réel dans les tests |
| Lot de `burst_size` appels | passe sans attente |
| Lot de `burst_size + 1` | le dernier attend le remplissage |
| Attente supérieure à `max_wait` | palier reporté, pas d'exception |
| Palier à 1 440 min en échec, repli à zéro | réessayé 4 fois par jour, pas 288 |
| Palier reporté par le limiteur pour 60 s | revient au bout de 60 s, sans plancher |
| Plafond du jour à 80 % | paliers `normale` et `basse` reportés |
| Plafond du jour atteint | seul `critique` passe |
| Passage de minuit | compteur du jour remis à zéro, seau inchangé |
| Trois `CryptoError` | arrêt, réparation ouverte, `until` correct |
| Trois fois `logged_in is False` | même traitement que `CryptoError` |
| Trois erreurs HTTP | **n'incrémentent pas** le compteur d'échecs ; repli seulement |
| Enrôlement QR (deux connexions) | compteur d'échecs inchangé |
| Réparation puis ré-authentification manuelle | compteurs remis à zéro |
| Amorçage sans bloc `Start({…})` | `bootstrap_failed`, réparation sans cause nommée |
| Page contenant « EQUIPE » et amorçage valide | **aucune** détection de suspension |
| `Erreur.G = 25` | entre dans le repli, et **ne provoque aucune reconnexion** |
| `Erreur.G = 10` | reconnexion comptée, durée de vie enregistrée |
| `Erreur.G = 8` | même verdict que `10` : « la page a expiré » est le même énoncé |
| Second refus sur une session ouverte à l'instant | part au repli, **pas** de seconde reconnexion |
| Session sans succès depuis plus d'une heure | abandonnée à la prochaine unité de travail |
| Session vivante entre deux lots | **zéro** connexion supplémentaire |
| Délai d'inactivité simulé < intervalle `timetable` | dégénère en une connexion par lot (§5.4) |
| `Erreur.G = 22` | **n'entre pas** dans le repli, journalisé comme bug |
| Succès après cinq échecs | compteur d'échecs à zéro immédiatement |
| Entrée en heures creuses pendant un lot | le lot en cours se termine, le suivant est reporté |
| Palier sans instantané en heures creuses | collecte en `critical`, la dispense cesse dès qu'il en a un |
| Rechargement, instantané retiré de la transmission | palier redemandé malgré l'échéance héritée |
| Rechargement, instantané conservé | palier **non** redemandé, §7.3 tenu |
| Deux comptes simultanés | compteurs indépendants, exécuteurs indépendants |
| Gigue | bornée dans `[0,5 ; 1,5] × nominal`, testée avec un générateur figé |

**Exigence.** Deux tests bornent l'arithmétique du §5, et ce sont des contrats :

- avec les options par défaut et un délai d'inactivité serveur simulé à
  30 minutes, une simulation de vingt-quatre heures consomme **entre 160 et 210
  requêtes** ;
- avec le même jeu d'options et un délai simulé à 5 minutes — le pire cas du
  paramètre inconnu —, elle en consomme **au plus 500**, ce qui vérifie la
  propriété du §5.4 : la stratégie paresseuse ne peut pas être pire que la
  reconnexion par lot.

La borne de la v1 (« entre 380 et 460 appels ») était calibrée sur un modèle de
coût faux et aurait échoué pour la mauvaise raison. Une borne est utile ; une
borne dérivée d'un modèle non vérifié donne surtout de la confiance mal placée.

**Exigence.** Le compteur porte sur les **requêtes HTTP**, GET d'amorçage
compris (§1.2). Un test vérifie qu'une connexion en incrémente de 5 au moins.
