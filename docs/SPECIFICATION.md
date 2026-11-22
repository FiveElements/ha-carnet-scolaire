# Spécification — Carnet scolaire, intégration Home Assistant pour PRONOTE

| | |
| --- | --- |
| **Domaine** | `carnet_scolaire` |
| **Cible** | Home Assistant 2026.11 ou supérieur |
| **Dépendance** | `pronotepy == 2.15.7` (épinglée) |
| **Distribution** | HACS, dépôt personnalisé |

Ce document énonce les exigences de l'intégration côté PRONOTE ;
[`ARCHITECTURE.md`](ARCHITECTURE.md) décrit comment le code les tient. Là où
les deux divergeraient, le code fait foi.

---

## 1. Objet

**Rendre les automatisations sur les données PRONOTE simples à écrire.**

C'est l'objectif directeur, et il commande tout le reste. Afficher les
informations dans une carte en découle — une donnée exploitable par un
déclencheur est *a fortiori* affichable —, mais l'inverse n'est pas vrai : une
liste JSON dans un attribut se montre très bien dans un tableau et ne se
déclenche pas.

Le critère de réussite est donc précis : **toute automatisation scolaire
courante doit s'écrire sans template Jinja**.

```yaml
# Ce que la spécification doit rendre possible.
triggers:
  - trigger: state
    entity_id: binary_sensor.emma_cours_annules
    to: "on"
```

```yaml
# Ce qu'elle doit rendre inutile.
triggers:
  - trigger: template
    value_template: >
      {{ state_attr('sensor.emma_lessons_today','lessons')
         | selectattr('canceled') | list | count > 0 }}
```

### 1.1 Le périmètre fonctionnel

Exposer l'ensemble de ce qu'un compte PRONOTE rend accessible — emploi du
temps, devoirs, notes, moyennes, absences, retards, punitions, évaluations par
compétences, bulletins, actualités, discussions, menus, identité, équipe
pédagogique — pour un ou plusieurs enfants, **sans jamais dépasser une cadence
d'appels que l'administrateur n'a pas choisie**.

S'y ajoutent les écritures que PRONOTE autorise et qui ont un sens domotique :
marquer un devoir fait, marquer une actualité lue, envoyer un message.

### 1.2 Hors périmètre

- Le compte « Vie scolaire » (`VieScolaireClient`) : public différent, données
  d'autres élèves, aucun cas d'usage domestique.
- La lecture d'un QR code par la webcam. Vérifié : le sélecteur `qr_code` de
  Home Assistant *affiche* un code, il n'en lit pas ; la seule voie serait une
  étape externe décodant côté navigateur, or `getUserMedia` exige un contexte
  sécurisé (HTTPS ou localhost) qu'une instance domestique n'a généralement
  pas. La saisie reste le collage de la charge utile du QR code et du PIN.
- La réécriture du client protocolaire. Voir §3.4.

### 1.3 Hypothèses assumées

Trois décisions de départ :

- **Documentation et messages en français**, identifiants de code en anglais
  (convention Home Assistant).
- **Domaine `carnet_scolaire`**, neutre : l'intégration collecte auprès de
  PRONOTE et d'EcoleDirecte, et un domaine distinct de `pronote` cohabite avec
  une autre intégration PRONOTE déjà installée — deux composants réclamant le
  même domaine ne cohabitent pas, Home Assistant n'en charge qu'un. Le nom
  affiché est **Carnet scolaire**, le dépôt `ha-carnet-scolaire`.
- **`pronotepy` comme dépendance**, encapsulée derrière un adaptateur (§3.4).

---

## 2. Conception orientée automatisation

Six exigences découlent directement de l'objectif du §1. Elles ont priorité sur
toute considération d'élégance interne : si un choix rend le code plus propre et
une automatisation plus difficile, il est écarté.

### 2.1 Un fait, une entité, un état primitif

**Exigence.** Tout fait sur lequel on peut vouloir déclencher possède sa propre
entité, dont l'**état** porte le fait — un horodatage, un nombre, un booléen —
et non une structure à parcourir.

Conséquence concrète, non exhaustive :

| Question posée par une automatisation | Entité dédiée | Type d'état |
| --- | --- | --- |
| À quelle heure commence le prochain cours ? | `sensor.<élève>_prochain_cours` | `timestamp` |
| À quelle heure faut-il réveiller ? | `sensor.<élève>_prochain_reveil` | `timestamp` |
| Combien de devoirs restent à faire ? | `sensor.<élève>_devoirs_a_faire` | nombre |
| Y a-t-il cours aujourd'hui ? | `binary_sensor.<élève>_jour_de_classe` | booléen |
| Un cours a-t-il été annulé ? | `binary_sensor.<élève>_cours_annules` | booléen |
| L'élève est-il en cours en ce moment ? | `binary_sensor.<élève>_en_cours` | booléen |
| Quelle est la dernière note reçue ? | `sensor.<élève>_derniere_note` | nombre |
| La moyenne générale a-t-elle bougé ? | `sensor.<élève>_moyenne_generale` | nombre |
| Quand est le prochain contrôle ? | `sensor.<élève>_prochain_controle` | `timestamp` |
| Quand est la prochaine retenue ? | `sensor.<élève>_prochaine_punition` | `timestamp` |
| Combien d'actualités non lues ? | `sensor.<élève>_actualites_non_lues` | nombre |

Les listes complètes restent disponibles en attributs, pour les cartes. Elles
ne sont jamais le **seul** chemin vers un fait.

**Exigence — le test du jumeau.** Un capteur binaire n'est justifié que s'il
**n'est pas** un seuil sur un nombre déjà exposé. `binary_sensor.devoirs_a_faire`
en regard de `sensor.devoirs_a_faire` n'apporte rien : `numeric_state above: 0`
est déjà une ligne de YAML sans template, donc au niveau qu'exige le §1. En
revanche `cours_annules`, `devoirs_en_retard`, `jour_de_classe`, `en_cours`,
`absence_en_cours`, `punition_a_venir`, `controle_prevu`, `sortie_pedagogique` et
`vacances` encodent un prédicat qu'il faudrait sinon écrire en template : ceux-là
sont gardés.

Ce test s'applique à tout ajout au catalogue. Multiplier les entités sans qu'un
template disparaisse n'est pas servir l'objectif du §1, c'est l'encombrer.

**Exigence — entités pilotées par l'horloge.** `prochain_cours`,
`fin_des_cours`, `prochain_reveil`, `prochain_controle`, `prochaine_punition`,
`en_cours` et `absence_en_cours` doivent basculer **à un instant d'horloge**, pas
quand leur palier repasse. Ils sont réévalués par `async_track_point_in_time`
positionné sur la prochaine bascule connue. Sans cela un capteur de réveil accuse
jusqu'à quinze minutes de retard, ce qui lui retire son objet.

**Exigence.** Chaque capteur numérique déclare `device_class`,
`state_class` et `native_unit_of_measurement` lorsqu'ils existent, afin qu'un
déclencheur de seuil (`numeric_state`) fonctionne sans conversion.

### 2.2 Les changements sont des évènements, pas des différences à calculer

Détecter « une nouvelle note est arrivée » en comparant deux états successifs
d'une liste est le travail que cette intégration doit retirer à l'utilisateur.

**Exigence.** L'intégration calcule les deltas entre deux instantanés et les
publie comme entités `event` de Home Assistant, avec leur contexte en
attributs :

| Entité `event` | Déclenchée par | Attributs du contexte |
| --- | --- | --- |
| `event.<élève>_nouvelle_note` | une note absente de l'instantané précédent | matière, note, barème, coefficient, date |
| `event.<élève>_nouveau_devoir` | un devoir nouvellement publié | matière, description, échéance |
| `event.<élève>_cours_modifie` | annulation, rétablissement, remplacement, déplacement, changement de salle, de professeur ou de statut — sept types, annexe A §4 | matière, ancien et nouveau créneau, motif |
| `event.<élève>_nouvelle_actualite` | une actualité nouvellement visible | auteur, titre, catégorie |
| `event.<élève>_nouvelle_absence` | une absence ou un retard nouvellement saisi | dates, justifiée ou non, motifs |
| `event.<élève>_nouvelle_punition` | une punition nouvellement saisie | nature, motif, créneaux programmés |
| `event.<élève>_nouveau_message` | un message reçu dans une discussion | discussion, auteur |

Une automatisation « prévenir quand une note arrive » devient alors trois
lignes, sans état antérieur à mémoriser :

```yaml
triggers:
  - trigger: state
    entity_id: event.emma_nouvelle_note
actions:
  - action: notify.parents
    data:
      message: >
        {{ trigger.to_state.attributes.subject }} :
        {{ trigger.to_state.attributes.grade }}
```

### 2.2.1 Deux règles de détection, pas une

Une règle unique — « delta sur l'identifiant, jamais sur le contenu » — est
juste pour la plupart des collections et **incapable d'implémenter
`cours_modifie`**, l'évènement le plus utile du lot. Raison : un
changement de salle conserve le même `N`, donc ne produit aucun delta
d'identifiant ; et un remplacement arrive avec un `N` neuf pendant que l'entrée
d'origine reste présente, donc se lit comme un ajout et non comme une
modification.

**Exigence — collections en ajout seul** (notes, devoirs, actualités, absences,
retards, punitions, messages, évaluations) : delta sur l'identifiant, jamais
sur le contenu. Un libellé corrigé par un professeur ne produit pas de faux
évènement. Le `N` de PRONOTE étant rechiffré à chaque connexion, l'identifiant
comparé est une clé frappée depuis le contenu qui identifie l'élément, le `N`
restant à côté pour la seule session qui l'a produit.

**Exigence — emploi du temps** : deux vues, deux usages.

1. **Dédoublonner par créneau pour l'affichage**, en ne gardant que l'entrée de
   `num` maximal. `PageEmploiDuTemps` renvoie plusieurs entrées pour un même
   créneau — l'originale et ses remplacements — et `Client.lessons()` ne filtre
   pas. La docstring d'amont le dit : *« for the same lesson time, the biggest
   num is the one shown on pronote »*. La clé de créneau est `(date, place)`,
   sans la matière : une substitution est précisément deux entrées de matières
   différentes sur un même créneau.
2. **Détecter sur la semaine non dédoublonnée**, en comparant un tuple de champs
   en liste blanche : `(canceled, status, frozenset(classrooms),
   frozenset(teachers), start, end)`. Sur un remplacement, PRONOTE sert
   l'entrée originale marquée `estAnnule` *plus* l'entrée qui la remplace, et
   le dédoublonnage garde la seconde : l'annulation n'est observable qu'avant
   lui. Salles et professeurs sont des ensembles, parce que rien n'ordonne
   `ListeSalles` ni `ListeProfesseurs` et que PRONOTE les réordonne. Sont
   exclus `memo`, `background_color` et le contenu de cours, donc un mémo
   corrigé ne déclenche rien.

**Exigence — appariement.** Une entrée est rapprochée de son état précédent
par une clé frappée — matière, début, fin — qui survit à une reconnexion. Un
cours déplacé change de clé : le `N`, quand l'instantané précédent vient de la
même session, le retrouve dans le créneau qu'il a quitté. Le `N` nomme le
cours et non la séance — un cours hebdomadaire porte le même dans les deux
semaines collectées — et ne départage donc qu'entre des entrées dont le
créneau s'est vidé.

**Exigence — remplacement.** Une entrée annulée dont le créneau est enseigné
par une autre entrée non annulée émet `lesson_replaced`, pas
`lesson_canceled` : c'est aussi la forme de « Changement de salle » et de
« Cours maintenu », et l'enfant a cours. Une annulation levée émet
`lesson_restored` ; un changement du seul libellé `Statut` émet
`lesson_status_changed`. Aucun repli ne requalifie en annulation une
transition non reconnue.

Formulation générale : **delta sur l'identité là où l'identité est stable, sur
un sous-ensemble de champs en liste blanche là où le protocole supersède au lieu
d'ajouter.**

**Exigence.** L'étape 1 n'est pas propre aux évènements : sans elle,
`sensor.<élève>_cours_du_jour` surcompte tous les jours où un cours a été
modifié, `calendar.<élève>_emploi_du_temps` affiche des évènements superposés,
et `sensor.<élève>_prochain_cours` peut s'accrocher à une entrée périmée. Le
dédoublonnage appartient donc à la passerelle, pas au détecteur de delta.

**Exigence.** Aucun évènement n'est émis lors du **premier** instantané d'un
palier après un démarrage ou un rechargement. Sinon chaque redémarrage de Home
Assistant rejouerait le trimestre.

### 2.3 Les déclencheurs sont proposés dans l'interface

Écrire du YAML n'est pas le point de départ de la plupart des utilisateurs.

**Exigence.** L'intégration fournit `device_trigger.py`, `device_condition.py`
et `device_action.py`, de sorte que l'éditeur d'automatisation propose
directement, pour l'appareil d'un enfant : *« Quand un cours est annulé »*,
*« Quand une note arrive »*, *« Quand un devoir est publié »*, *« S'il y a
cours aujourd'hui »*, *« Marquer un devoir comme fait »*.

**Exigence.** Le dépôt livre neuf **blueprints** prêts à l'emploi dans
`blueprints/automation/carnet_scolaire/` :

- réveil adapté à l'heure du premier cours, avec marge configurable
  (`wake_up_alarm`) ;
- notification à l'annulation ou au déplacement d'un cours
  (`lesson_canceled`) ;
- rappel des devoirs le soir, si la liste n'est pas vide
  (`homework_reminder`) ;
- notification à l'arrivée d'une note, avec seuil optionnel (`new_grade`) ;
- alerte sur absence ou punition nouvellement saisie (`absence_alert`) ;
- annonce du menu de la cantine le matin (`canteen_menu`) ;
- notification à l'arrivée d'un message ou d'une actualité (`new_message`) ;
- action à la fin des cours, sur le capteur « Fin des cours »
  (`end_of_day`) ;
- action à la fin de la matinée, sur le capteur « Fin de matinée », avec le
  choix de faire avancer ou non le retour par un cours annulé
  (`midday_return`).

Un blueprint qui fonctionne à l'installation vaut mieux qu'un chapitre de
documentation expliquant comment l'écrire.

### 2.4 La stabilité des identifiants est une garantie

Une automatisation qui casse à la rentrée parce qu'un capteur a changé de nom
est une régression, même si aucun code n'a échoué.

**Exigence.** Le `unique_id` d'une entité est dérivé de l'identifiant PRONOTE
de l'élève et d'une **clé fonctionnelle stable**, jamais d'un libellé, d'un nom
de période ni d'un rang. Les entités liées à une période close portent l'index
de la période (`p1`, `p2`, `p3`), pas son nom : « Trimestre 1 » peut changer de
libellé d'une année sur l'autre.

**Exigence.** Le passage d'une année scolaire à la suivante ne crée pas de
nouvelles entités pour les mêmes faits. Les périodes sont réindexées ; les
entités de l'année précédente sont retirées, pas dupliquées.

### 2.5 Une entité indisponible casse une automatisation

**Exigence.** Une coupure réseau ne doit pas rendre les entités
`unavailable` — voir le régime d'obsolescence du §5.4. Un `numeric_state` sur
une entité qui devient `unavailable` puis revient produit des déclenchements
parasites ; garder la dernière valeur connue en la marquant comme datée est
plus sûr que d'avouer l'ignorance toutes les dix minutes.

### 2.6 Les devoirs sont une liste de tâches, pas un texte

**Exigence.** Les devoirs sont exposés comme entité `todo`, avec
`TodoListEntityFeature.UPDATE_ITEM`. Cocher une tâche dans Home Assistant
appelle `Homework.set_done(True)` sur PRONOTE ; l'échéance devient la `due` de
l'élément.

C'est la forme que Home Assistant sait déjà afficher, trier, rappeler et
automatiser, sans qu'on écrive une carte pour cela.

---

## 3. Architecture

### 3.1 Couches

```
                      ┌─────────────────────────────────────┐
   config_flow   ───► │          PronoteAccount             │
   options_flow       │      (une par entrée de config)     │
                      ├─────────────────────────────────────┤
                      │  SessionManager   RateLimiter       │
                      │  FetchScheduler   SnapshotStore     │
                      │  DeltaDetector                      │
                      └──────────────┬──────────────────────┘
                                     │  un thread, appels sérialisés
                      ┌──────────────▼──────────────────────┐
                      │      PronoteGateway (adaptateur)    │
                      │   pronotepy + résolution immédiate  │
                      └──────────────┬──────────────────────┘
                                     │  DTO figés (frozen dataclass)
        ┌────────────────────────────▼────────────────────────────┐
        │  Coordinateurs par palier  ───►  Entités par plateforme │
        │  sensor · binary_sensor · calendar · todo · button ·    │
        │  event · image · diagnostics                            │
        └─────────────────────────────────────────────────────────┘
```

Le point important est la frontière du milieu : **rien de ce que produit
`pronotepy` ne franchit la couche adaptateur**. Ce qui remonte est un jeu de
`dataclass` gelées, sans référence au client, sans attribut paresseux, avec des
dates conscientes du fuseau. Trois défauts connus disparaissent par
construction :

- Un objet lu après la fermeture de sa session déclenche l'erreur `G=22`.
- Un attribut paresseux (`Information.content`, `Attachment.data`,
  `Lesson.content`) rappelle le réseau au moment où on le lit, donc
  potentiellement depuis la boucle d'événements.
- Une entité qui garde une référence au client garde vivant tout le graphe
  d'objets, session comprise.

Un exemple concret vaut mieux que la formulation générique : `ClientInfo._cache()`
appelle `self._client.communication.post(...)` **en direct**, court-circuitant
`ClientBase.post` et, sur un compte parent, la signature `membre` qui désigne
l'enfant. Autrement dit, lire `ClientInfo.address`, `.email`, `.phone` ou
`.ine_number` depuis n'importe où déclenche un appel non budgété et
potentiellement mal attribué. C'est la frontière DTO qui l'empêche, et c'est la
meilleure justification de son existence.

### 3.2 Sérialisation des appels — non négociable

Le protocole numérote les requêtes avec un compteur chiffré : deux appels
concurrents sur une même session désynchronisent le compteur et cassent la
session. Or `hass.async_add_executor_job` emploie un pool de threads : deux
tâches lancées ensemble *sont* concurrentes.

**Exigence.** Chaque `PronoteAccount` possède son propre
`ThreadPoolExecutor(max_workers=1)` et un `asyncio.Lock`. Tout accès au client
passe par :

```python
async def _call(self, fn, *args):
    async with self._lock:
        return await self._hass.loop.run_in_executor(self._executor, fn, *args)
```

Le verrou couvre l'appel **et** toute résolution d'attribut qui en découle : un
`fn` doit retourner un DTO complet, jamais un objet `pronotepy`.

Chaque entrée de configuration a son exécuteur et son verrou. Cela ne suffit
**pas** à les isoler : `Period.instances` est un attribut de classe muable et
global au processus (§3.3), donc deux comptes partagent un canal que l'un peut
écrire pendant que l'autre lit. L'isolation complète exige le confinement décrit
au §3.6.

**Exigence — délai d'expiration HTTP.** `pronotepy` n'en pose aucun : la
recherche de `timeout` dans ses sources ne renvoie rien, ni sur le GET
d'amorçage ni sur les POST. Un serveur qui accepte la connexion et ne répond
jamais bloque donc définitivement l'unique thread. La passerelle substitue à
`client.communication.session` une sous-classe de `requests.Session` dont
`request()` renseigne un `timeout` par défaut (30 s connexion, 60 s lecture,
configurable).

**Exigence — arrêt qui ne fige rien.** `async_unload_entry` n'appelle jamais
`shutdown(wait=True)` depuis la boucle : `shutdown(wait=False,
cancel_futures=True)`, et tout appel en vol est borné par `asyncio.wait_for`. Un
thread fuité vaut mieux qu'un Home Assistant figé.

**Exigence.** L'obsolescence du §5.4 ne doit pas masquer un interblocage. Un
appel dont la durée dépasse trois fois le délai de lecture lève, journalise à
`ERROR` et marque le compte en défaut — sans quoi « obsolescence plutôt
qu'indisponibilité » transforme une panne définitive en réseau lent pendant six
intervalles.

**Exigence — l'unité atomique est `(enfant, palier)`.** `set_child()` **mute**
le client en réécrivant `parametres_utilisateur["dataSec"]["data"]["ressource"]`,
et `Client.lessons()` relit ce même emplacement pour construire le corps de sa
requête. Un entrelacement `set_child(A) · post() · set_child(B) · post()` au
mauvais grain publie donc **l'emploi du temps de B sous les entités de A**,
silencieusement. Le `fn` passé à `_call` fait lui-même le `set_child`, l'appel
d'onglet et la construction des DTO ; jamais un palier seul.

**Exigence.** `ClientBase.__init__` se termine par une connexion réseau
(`self.logged_in = self._login()`). La construction du client a donc lieu dans
l'exécuteur, sous verrou. `async_setup_entry` ne construit jamais le client
depuis la boucle.

### 3.3 Déduplication au niveau de l'appel

C'est le gain principal de cette conception. `pronotepy` ne met rien en cache et
expose chaque donnée comme une propriété qui refait un `post()` :

| Propriété `pronotepy` | Appel émis | Onglet |
| --- | --- | --- |
| `Period.grades` | `DernieresNotes` | 198 |
| `Period.averages` | `DernieresNotes` | 198 |
| `Period.overall_average` | `DernieresNotes` | 198 |
| `Period.class_overall_average` | `DernieresNotes` | 198 |
| `Period.absences` | `PagePresence` | 19 |
| `Period.delays` | `PagePresence` | 19 |
| `Period.punishments` | `PagePresence` | 19 |

Sept propriétés, sept appels, **deux réponses distinctes suffisent** — les trois
propriétés de présence lisent d'ailleurs la même liste `listeAbsences` en la
filtrant sur `G` valant 13, 14 ou 41.

### 3.3.1 Dédupliquer sans réimplémenter

Décoder les réponses brutes à la main contredirait le §3.4, qui refuse de
reprendre la dette de décodage d'amont.

Les classes de données de `pronotepy` acceptent **le seul dictionnaire brut** —
`Grade(json)`, `Average(json)`, `Absence(json)`, `Delay(json)`,
`Evaluation(json)`, `Report(data)` ; `Lesson(client, json)` et
`Punishment(client, json)` prennent un client et rien d'autre. La bonne forme
est donc un `post()` brut par onglet, puis **les sous-listes brutes passées aux
classes d'amont**, puis correspondance vers les DTO gelés :

```python
# Un appel, quatre jeux de données, décodage d'amont conservé.
raw = client.post("DernieresNotes", 198, {"donnees": {"Periode": period_ref}})
data = raw["dataSec"]["data"]
return MarksSnapshot(
    grades=tuple(_grade_dto(g) for g in _list(data, "listeDevoirs")),  # cf. §3.3.2
    averages=tuple(_dto(pronotepy.Average(a)) for a in _list(data, "listeServices")),
    overall_average=_grade_value(_get(data, "moyGenerale", "V")),
    class_overall_average=_grade_value(_get(data, "moyGeneraleClasse", "V")),
)
```

**Exigence.** La passerelle expose une fonction par *appel protocolaire*, pas une
par donnée affichée, et réutilise les classes d'amont pour le décodage. La
correspondance appel → DTO figure en annexe A.

### 3.3.2 Une seule exception : `Grade`

`Grade.__init__` résout `self.period` par `Util.get(Period.instances, id=p)[0]`,
où `Period.instances` est un **`set` d'attribut de classe jamais vidé** —
vérifié : c'est la seule référence à ce registre dans tout `dataClasses.py`.
Trois conséquences, et aucune n'a de correctif propre :

- **C'est une fuite.** Chaque `Period` construite y reste et retient son
  `_client`, donc un client mort et sa session HTTP morte. La frontière DTO du
  §3.1 ne corrige pas cela : le registre est peuplé par `pronotepy` à la
  connexion, pas par nos entités.
- **C'est un canal entre entrées de configuration.** `Util.get(...)` ne compare
  que `id`, et les `N` sont propres à l'établissement : la note d'un compte peut
  résoudre la période d'un autre. C'est la raison pour laquelle le §3.2 ne
  promet pas l'isolation par le seul exécuteur.
- **C'est porteur.** Vider le registre fait renvoyer `[]`, donc `[0]` lève
  `IndexError`, enveloppée en `ParsingError` : tout le lot de notes échoue.

Seconde raison, indépendante : `Util.grade_parse` remplace déjà `|1`…`|8` par
`"Absent"`, `"Dispense"`… donc `pronotepy.Grade.grade` est **déjà lossy**.
Décoder `note.V` nous-mêmes est la seule voie vers la sentinelle brute dont
l'énumération `GradeStatus` du §4.3 a besoin.

**Exigence.** Le décodage maison est limité à `Grade`, et cette limite est
documentée dans `gateway.py` avec les deux raisons ci-dessus. Ce n'est pas une
politique générale.

### 3.3.3 Décodage non strict

**Exigence.** Un champ absent donne `None`, jamais une exception. Les moyennes
de classe, minimum, maximum et coefficient sont légitimement absents quand
l'établissement ne les publie pas.

**Exigence.** Une sentinelle de note inconnue donne `GradeStatus.UNKNOWN`, jamais
une exception. La table d'amont a huit entrées indexées par `int(string[1]) - 1` :
un futur `|9` y lèverait `IndexError` et ferait échouer **toutes** les notes.

**Exigence.** `Client.current_period` retombe sur `onglets[0]` quand l'onglet 198
est absent, donc peut désigner silencieusement la mauvaise période dans un
établissement qui ne publie pas les notes. La passerelle préfère `None` à une
retombée, et l'entité correspondante devient indisponible plutôt que fausse.

### 3.4 Pourquoi `pronotepy` et non un client propre

Le protocole est chiffré, ses clés tournent trois fois par session, et il change
sans préavis ni versionnement — chaque rupture se solde par un correctif
d'urgence, jusqu'au repli sur le défi brut quand le chiffrement bouge. Réécrire
cette couche, c'est reprendre à son compte une dette qu'un projet amont assume
déjà, et se priver de ses correctifs.

**Exigence.** `pronotepy` est épinglé à une version exacte. Ses types de
données ne sont lus que dans `gateway.py` et `hardened_client.py` — deux
fichiers à relire lors d'une montée de version, un seul point à doubler en
test. Seules ses classes d'exception traversent au-delà, sans état, pour
classer un échec (`session.py` doit distinguer `CryptoError` de
`PronoteAPIError`, §6.3).

**Exigence.** La passerelle utilise `client.post()` directement là où les
méthodes de haut niveau coûtent des appels superflus (§3.3), et les méthodes de
haut niveau (`lessons`, `homework`, `menus`, `information_and_surveys`,
`discussions`, `get_teaching_staff`) là où elles n'en coûtent qu'un.

### 3.5 Arborescence du dépôt

```
custom_components/carnet_scolaire/
├── __init__.py            # mise en place / retrait de l'entrée, migration
├── manifest.json
├── const.py               # domaine, clés d'options, valeurs par défaut
├── config_flow.py         # configuration initiale, options, ré-authentification
├── flow_login.py          # connexion du flow, exécutée dans l'exécuteur
├── account.py             # PronoteAccount : orchestration
├── tiers.py               # un palier : collecte, instantané, delta
├── login_guard.py         # état punitif qui survit à l'entrée
├── options.py             # lecture des options, estimation du budget
├── connectors/            # couture SchoolConnector : PRONOTE, EcoleDirecte
├── session.py             # SessionManager : connexion, expiration, rotation
├── ratelimit.py           # RateLimiter : les trois couches + repli
├── scheduler.py           # FetchScheduler : paliers, échéances, priorités
├── hardened_client.py     # client durci (§3.6)
├── gateway.py             # adaptateur pronotepy → DTO
├── models.py              # DTO figés
├── item_keys.py           # clés stables frappées (le N tourne par session)
├── delta.py               # DeltaDetector : instantané N-1 → évènements
├── coordinator.py         # un coordinateur par palier
├── entity.py              # base commune : appareil, disponibilité, obsolescence
├── sensor.py
├── binary_sensor.py
├── calendar.py
├── todo.py
├── button.py
├── event.py
├── image.py
├── device_trigger.py      # déclencheurs proposés dans l'interface
├── device_condition.py
├── device_action.py
├── diagnostics.py
├── services.py
├── services.yaml          # généré, structure seule, aucun texte
├── strings.json           # généré, textes en anglais
└── translations/          # générés par scripts/build_translations.py
    ├── en.json
    └── fr.json
blueprints/automation/carnet_scolaire/
├── fr/
└── en/
tests/
├── conftest.py
├── fixtures/              # réponses protocolaires écrites à la main
├── test_ratelimit.py
├── test_scheduler.py
├── test_gateway.py        # décodage des réponses → DTO
├── test_delta.py
├── test_config_flow.py
├── test_translations.py   # toute clé employée existe dans chaque langue
├── test_no_secret_in_state.py
└── test_<plateforme>.py
docs/
.github/workflows/         # validation, publication (§11)
```

### 3.6 Client durci — prérequis, pas option

`pronotepy` se réauthentifie dans le dos de l'appelant, et c'est incompatible
avec un limiteur de débit qui prétend être le seul chemin vers le réseau.

`ClientBase.post` intercepte **toute** `PronoteAPIError` sauf `ExpiredObject`,
appelle `self.refresh()` — soit `session.close()`, un GET HTML,
`FonctionParametres`, `Identification`, `Authentification`,
`ParametresUtilisateur` — puis rejoue l'appel. Un appel budgété peut donc valoir
six requêtes réseau. Trois aggravations :

- **`Erreur.G = 25` déclenche une authentification.** C'est une
  `PronoteAPIError` ordinaire, donc elle provoque exactement la classe de
  requête que `G = 25` sanctionne.
- **`ParentClient.post` duplique le gestionnaire sans la garde `_refreshing`.**
  `ClientBase.post` protège la récursion (`if self._refreshing: raise e`) ;
  `ParentClient.post` ne le fait pas. Un serveur qui répond en erreur pendant le
  `_login()` du `refresh()` produit une **récursion non bornée de connexions
  complètes** — sur un compte parent, c'est-à-dire le cas d'usage de ce projet.
- **`refresh()` perd la sélection de l'enfant.** Il appelle `_login()`, qui
  réécrit `parametres_utilisateur[...]["ressource"]` avec la ressource du
  parent ; `ParentClient` ne reconstruit `_selected_child` que dans
  `__init__`. Après un rafraîchissement automatique, `post()` estampille encore
  le bon `membre` mais `lessons()` envoie la ressource du parent.

**Exigence.** La passerelle emploie un client dont `post()` ne se réauthentifie
jamais. La reconnexion redevient une décision du `SessionManager`, comptée par le
limiteur. Bénéfice de bord indispensable au §6.5 : `Erreur.G = 10` devient un
évènement explicite et **mesurable**.

**Exigence — ne pas hériter de `ParentClient`.** La forme évidente serait une
sous-classe `_NoRefreshParentClient(pronotepy.ParentClient)` surchargeant
`post()`. **C'est la moins bonne des
deux formes** : elle hérite d'une classe dont deux des trois défauts ci-dessus
sont les siens, et elle les neutralise par surcharge — donc une méthode d'amont
ajoutée demain les réintroduit sans que rien ne le signale.

La forme retenue est un `HardenedClient(pronotepy.Client)` qui **réimplémente**
la surface parent — liste des enfants, `set_child()`, signature `membre` — sur
`Client`. Les deux défauts propres à `ParentClient` deviennent alors
structurellement inatteignables plutôt que corrigés, et le §7.1 n'a plus de
chemin fragile à surveiller. Un seul client couvre les deux types de compte,
avec `is_parent_account` pour la seule chose qui diffère vraiment.

Le confinement de `Period.instances` vit dans un objet et un gestionnaire de
contexte séparés de la classe, ce qui permet de le poser aussi sur le chemin
d'enrôlement QR du flux de configuration, où il n'y a pas de client durci.

**Exigence.** `refresh()` et `keep_alive()` **refusent** au lieu de se taire.
Les rendre inopérants sans le dire laisserait croire à un appelant qu'une
reconnexion a eu lieu ; refuser force le `SessionManager` à assumer la décision.

**Exigence.** Le même client confine `Period.instances` : après chaque
connexion, il retient les `Period` de sa propre session et retire du registre
global celles qui appartiennent à une session close — jamais en le vidant
(§3.3.2), toujours par différence.

**Exigence.** Le module porte en tête la description des trois défauts d'amont
qu'il existe pour contenir, avec leurs références. C'est la seule documentation
qui a une chance d'être lue par la personne qui se demandera, dans deux ans,
pourquoi ce client ne se contente pas d'hériter de `pronotepy`.

---

## 4. Modèle de données

### 4.1 DTO figés

**Exigence.** Tout ce qui sort de la passerelle est une
`@dataclass(frozen=True, slots=True)`. Aucun champ n'est un objet `pronotepy`.
Les listes sont des `tuple`.

La correspondance champ par champ avec les classes `pronotepy` est en annexe A.
Le principe :

```python
@dataclass(frozen=True, slots=True)
class Lesson:
    id: str  # clé frappée : matière, début, fin
    subject: str | None
    subject_id: str | None
    teachers: tuple[str, ...]
    classrooms: tuple[str, ...]
    groups: tuple[str, ...]
    start: datetime  # conscient du fuseau
    end: datetime  # conscient du fuseau
    canceled: bool
    status: str | None
    detention: bool
    outing: bool
    exempted: bool
    test: bool
    memo: str | None
    background_color: str | None
    virtual_classrooms: tuple[str, ...]
    num: int  # départage les entrées d'un même créneau : max gagne
    place: int  # index de créneau brut, clé de dédoublonnage
    duration: int  # en créneaux
    end_inferred: bool  # True si end a été calculé, non fourni
    ref: str | None  # le N de PRONOTE, valable dans sa seule session
```

Deux champs demandent une justification.

**`num`** est le champ `P` du protocole, et il porte le dédoublonnage exigé au
§2.2.1 : plusieurs entrées coexistent pour un même créneau, celle de `num`
maximal est celle que PRONOTE affiche.

**`end_inferred`** existe parce que `Lesson.end` n'est pas toujours transmis :
quand `DateDuCoursFin` est absent, `pronotepy` le calcule via `Util.place2time`,
dont le commentaire d'amont dit *« might be wrong... works with demo »*. Or tout
en dépend — réveil, `en_cours`, agenda, fin des cours. Le drapeau permet à une
entité de se taire plutôt que d'affirmer une heure douteuse, et
`test_gateway.py` doit couvrir le chemin inféré par une fixture dédiée.

### 4.2 Le temps

Les dates PRONOTE sont du texte local, naïf, sans décalage, et deux de leurs six
formes complètent la partie manquante avec *aujourd'hui*. Comparer ces valeurs à
l'horloge d'une machine réglée en UTC — le cas d'un conteneur par défaut —
produit des décalages silencieux, et donc des automatisations qui déclenchent à
la mauvaise heure.

**Exigence.** La conversion en `datetime` conscient a lieu **dans la
passerelle**, une seule fois, avec le fuseau de l'établissement.

**Exigence.** Le fuseau est une option de l'entrée,
`establishment_timezone`, dont la valeur par défaut est celle de Home
Assistant. Il ne se déduit pas du serveur : le protocole ne le transmet pas.

**Exigence.** Cette valeur par défaut est **dérivée à chaque démarrage**, pas
recopiée une fois. L'absence de l'option *est* la valeur « suivre Home
Assistant », et c'est la seule option de l'entrée dont le défaut soit la
lecture d'un réglage mutable d'un autre système plutôt qu'une constante. Deux
conséquences, tenues par `account._establishment_timezone` :

- la page d'options ne doit **jamais** enregistrer le fuseau de l'instance à la
  place de l'utilisateur. Home Assistant renvoie un formulaire tel qu'il est
  affiché, donc un champ pré-rempli transforme la simple visite de la page en
  décision : le fuseau se retrouve figé, et corriger celui de l'instance — le
  geste ordinaire après un déménagement, ou après une première installation
  restée en UTC — ne change plus rien ici. La case s'affiche donc **vide** tant
  que rien n'est choisi, et la vider est le chemin de retour ;
- une valeur enregistrée illisible **se rabat** sur le fuseau de l'instance,
  avec un avertissement au journal, au lieu de lever. `ZoneInfo` lève
  `ZoneInfoNotFoundError`, et cet appel est sur le chemin d'installation : lever
  y coûte toutes les entités du compte pour un unique caractère de trop dans
  une case de texte, avec « indisponible » pour seule explication. La page,
  elle, refuse la saisie sur le champ — c'est le seul endroit où la vérification
  peut être faite avant qu'elle ne coûte quelque chose, et ce n'est pas le
  chemin qu'emprunte une sauvegarde restaurée.

**Exigence.** Aucune comparaison temporelle n'utilise `datetime.now()` ni
`date.today()` sans fuseau, ni dans le code ni dans les tests.

### 4.3 Les notes ne sont pas toujours des nombres

Une note peut valoir `|1` à `|8` — absent, dispensé, non noté, inapte, non
rendu, absent compté zéro, non rendu compté zéro, félicitations. Les valeurs
numériques arrivent avec une **virgule** décimale.

**Exigence.** Le DTO `Grade` porte deux champs mutuellement exclusifs :

```python
value: float | None  # None dès que la note n'est pas numérique
status: GradeStatus | None  # énumération, None quand value est renseigné
```

Cette séparation est ce qui permet à un capteur `sensor.<élève>_derniere_note`
d'avoir un état **numérique** — donc utilisable par un `numeric_state` — et de
basculer sur `unknown` avec le motif en attribut quand la note est une
sentinelle. Un état qui vaudrait tantôt `14,5` tantôt `Absent` ne serait
exploitable ni par un seuil ni par un graphique.

**Exception : félicitations.** Elle arrive sous la forme `|8|<valeur>` et
n'est pas l'absence d'une note mais une note assortie d'une mention : le client
PRONOTE lit le champ qui suit le second `|` comme la valeur de la note et la
compte dans la moyenne. `Grade` porte alors **les deux** : `value` (virgule
décimale comme ailleurs) et `status = CONGRATULATIONS`. Sans cela, la meilleure
note de la période n'aurait pas de valeur et un `numeric_state` « note ≥ 18 »
ne se déclencherait pas pour elle. Un `|8` sans valeur reste sans valeur ;
tous les autres statuts restent exclusifs de `value`. Le code de la sentinelle
est le champ entier jusqu'au `|` suivant : un futur `|10` est `UNKNOWN`, pas
`|1`.

### 4.4 Instantanés

**Exigence.** Chaque palier produit un instantané immuable horodaté :

```python
@dataclass(frozen=True, slots=True)
class Snapshot[T]:
    data: T
    fetched_at: datetime
    tier: str
```

Le `SnapshotStore` conserve le dernier instantané réussi de chaque palier, et
c'est lui qui alimente le `DeltaDetector` du §2.2. Une collecte qui échoue **ne
remplace pas** l'instantané précédent : l'entité continue d'afficher la dernière
valeur connue et signale son âge (§5.4). C'est la différence entre « je ne sais
plus » et « je sais, mais c'est vieux » — sur des données scolaires, la seconde
réponse est presque toujours la bonne.

---

## 5. Collecte

### 5.1 Un battement maître, des paliers

Des minuteries indépendantes finissent par coïncider et produisent des rafales.
L'ordonnanceur n'en a donc qu'une.

**Exigence.** Un unique battement (`master_tick`, 5 min par défaut) demande à
l'ordonnanceur quels paliers sont échus, puis exécute leurs collectes dans une
seule session, espacées par le limiteur de débit. Les paliers ne portent pas de
minuterie propre ; ils portent une **échéance**.

### 5.2 Les paliers

Onze paliers de données, plus la session qui n'en est pas un. Le découpage
suit ce que le protocole facture : des *semaines*, pas des jours.

| Palier | Contenu | Appel | Intervalle par défaut | Priorité |
| --- | --- | --- | --- | --- |
| `session` | connexion, expiration | — | à la demande | critique |
| `timetable` | semaine courante **et** semaine suivante, bornées à l'année scolaire | `PageEmploiDuTemps` 16 | 15 min | haute |
| `homework` | devoirs jusqu'à la fin de l'année | `PageCahierDeTexte` 88 | 30 min | haute |
| `news` | actualités et sondages | `PageActualites` 8 | 1 h | normale |
| `discussions` | messagerie | `ListeMessagerie` 131 | 1 h | basse |
| `marks` | notes, moyennes, moyenne générale, bulletin de la période courante | `DernieresNotes` 198 + `PageBulletins` 13 | 3 h | normale |
| `attendance` | absences, retards, punitions | `PagePresence` 19 | 6 h | normale |
| `evaluations` | évaluations par compétences | `DernieresEvaluations` 201 | 12 h | basse |
| `menus` | menus de la cantine | `PageMenus` 10 | 24 h | basse |
| `agenda` | agenda de l'établissement, de ce jour à la fin de l'année (PRONOTE seulement) | `PageAgenda` 9 | 24 h | basse |
| `static` | équipe pédagogique | `PageEquipePedagogique` 37 | 24 h | basse |
| `history` | périodes closes : notes, présence, bulletins | 198 / 19 / 13 | 24 h | basse |

Trois regroupements, avec leur raison :

- **Un seul palier pour l'emploi du temps.** `Client.lessons()` boucle
  `for week in range(first_week, last_week + 1)` et poste une fois **par
  semaine**, puis filtre côté client. Demander « aujourd'hui et demain » coûte
  donc exactement le même appel que demander la semaine entière : un palier
  « semaine » séparé n'achèterait rien, et la vue semaine comme
  `binary_sensor.vacances` sont à jour au rythme de `timetable`.

  **Exigence.** L'horizon est la semaine courante **et** la semaine suivante,
  à chaque collecte. `binary_sensor.vacances` pose une question à **sept**
  jours ; sans la semaine suivante, aucun cours du lundi ne serait connu du
  samedi au lundi matin — carte vide, `sensor.prochain_cours` sans cible,
  `binary_sensor.vacances` à `on` chaque week-end. Coût : 2 appels par lot
  (annexe B §5.3).

  **Exigence.** Les semaines demandées sont bornées à `[PremierLundi,
  DerniereDate]`. Hors année scolaire — les deux mois de l'été, où aucun emploi
  du temps n'est publié avant la semaine de rentrée — le palier ne place
  **aucune** requête et publie un instantané vide : une semaine au-delà de
  `DerniereDate` n'est pas une semaine creuse, c'est une question que le
  serveur n'a jamais eu à traiter, et un palier qui échoue dessus échoue à
  chaque battement pendant deux mois.
- **Les bulletins voyagent avec `marks` et `history`.** Le bulletin de la
  période courante voyage avec les notes ; ceux des périodes closes avec
  l'historique, qui parcourt déjà les mêmes périodes une fois par jour.
- **L'URL iCal n'appartient à aucun palier.** Voir §8.2 : `static` la
  déposerait dans le magasin d'instantanés, c'est-à-dire dans la structure
  dont la fonction est d'être vidée dans un rapport de diagnostic.

**Exigence.** Chaque intervalle est configurable individuellement (§7.3), et un
palier peut être désactivé — ce qui supprime aussi ses entités.

**Exigence.** Le palier `history` ne parcourt que les périodes **closes**. Une
période close ne change plus : la relire toutes les trois heures dépense des
appels pour un résultat constant.

**Exigence.** `homework` charge l'année entière en un appel et filtre dans la
couche DTO. `Client.homework()` construit son domaine comme une **plage de
semaines** et fait retomber `date_to` sur la fin de l'année scolaire : l'étendue
demandée ne change pas le coût. `homework_horizon` est donc une option de
présentation, pas un levier de budget (§7.3), et `calendar.<élève>_devoirs`
comme `binary_sensor.<élève>_devoirs_en_retard` en profitent gratuitement.

**Note — bornes des intervalles.** Chaque intervalle est borné à
`[1, 1440]` minutes (`TIER_INTERVAL_RANGE`), la même borne pour tous les
paliers : l'interface d'options n'impose pas de plafond propre aux paliers qui
alimentent un évènement du §2.2. Porter `timetable` au-delà d'une heure retire
pourtant son intérêt à `event.<élève>_cours_modifie` — un cours annulé annoncé
quatre heures après ne sert plus à rien.

### 5.3 Un coordinateur par palier

**Exigence.** Un `DataUpdateCoordinator` par palier, avec
`update_interval=None` : c'est l'ordonnanceur qui déclenche
`async_set_updated_data()`, pas la minuterie du coordinateur. Une entité ne
s'abonne qu'au coordinateur de son palier, donc une collecte de menus ne
réécrit pas l'état de l'emploi du temps.

**Exigence — contrat d'échec.** Une collecte en échec lève `UpdateFailed` avec
un message court et sans secret. Home Assistant journalise alors **une** ligne
d'erreur à la transition succès → échec, et non une trace à chaque tentative.
Un `except Exception` générique est interdit ailleurs qu'au sommet d'un cycle,
où il doit lever `UpdateFailed`.

### 5.4 Obsolescence plutôt qu'indisponibilité

**Exigence.** Une entité ne devient `unavailable` que si elle n'a **jamais** eu
de donnée, ou si l'âge du dernier instantané dépasse `stale_after` × son
intervalle (`stale_after` = 6 par défaut). Entre les deux, elle garde sa valeur
et expose :

```yaml
attributes:
  fetched_at: 2026-09-08T07:45:12+02:00
  stale: false
```

Motivation directe du §2.5 : un plantage réseau de vingt minutes ne doit ni
vider un tableau de bord ni faire déclencher une automatisation de seuil.

### 5.5 Entités de calendrier

**Exigence.** `CalendarEntity` recalcule son évènement courant à **chaque**
écriture d'état. Sans cela, l'entité s'endort après la fin de l'évènement en
cours : `_async_write_ha_state` ne programme plus rien une fois
`now >= event.end`, et `CoordinatorEntity.async_added_to_hass` enregistre
l'écouteur sans l'appeler.

```python
@callback
def _async_write_ha_state(self) -> None:
    self._event = self._compute_event()
    super()._async_write_ha_state()
```

**Exigence.** Chaque `CalendarEvent` porte un `uid` issu de l'identifiant
PRONOTE. Sans `uid`, tous les `VEVENT` exportés portent `UID: none` et
l'agenda est inutilisable hors de Home Assistant.

---

## 6. Limiteur de débit

Le sujet a son annexe : **[annexe B](annexe-b-rate-limit.md)**. Résumé des
exigences structurantes.

### 6.1 Ce que le protocole punit

- Deux appels concurrents désynchronisent la session (§3.2).
- `Erreur.G = 25` — trop de demandes d'autorisation.
- Une répétition de connexions échouées fait **suspendre l'adresse IP** : la
  page d'accueil répond alors une chaîne contenant `IP`, et la levée n'est pas
  documentée.

Les deux dernières sanctions ne visent pas le même geste. Les *appels* coûtent
du débit ; les *connexions échouées* coûtent l'accès. Le limiteur les compte
donc séparément.

### 6.2 Trois couches pour les appels

| Couche | Option | Défaut | Rôle |
| --- | --- | --- | --- |
| Espacement | `min_request_interval` | 1,0 s | interdit toute rafale |
| Seau à jetons | `max_requests_per_hour` / `burst_size` | 240 / 20 | lisse la charge horaire |
| Plafond du jour | `max_requests_per_day` | 2 000 | filet contre une boucle |

**Exigence.** Aucun appel ne part sans jeton. Le limiteur est le seul chemin
vers la passerelle ; il n'existe pas de voie de contournement, pas même pour un
rafraîchissement manuel — un bouton demande un passage prioritaire, il n'obtient
pas une dérogation.

### 6.3 Deux compteurs pour les connexions

| Option | Défaut | Rôle |
| --- | --- | --- |
| `max_logins_per_day` | 24 | garde-fou sur les **tentatives** de connexion, réussies ou non |
| `max_failed_logins_per_hour` | 3 | garde-fou sur les connexions **échouées** |

**Exigence.** `max_logins_per_day` vaut **24**, pas 120. Une conception qui
attend une à trois connexions par jour (§6.5) doit alarmer bien avant 120 : un
plafond ne sert à rien s'il ne peut pas attraper le défaut pour lequel il
existe. Il compte les tentatives et non les succès : ce qui doit être borné est
le trafic d'authentification envoyé à l'établissement, et une tentative qui
échoue lui coûte autant qu'une réussie.

**Exigence — classer l'échec sur la bonne exception.** Un mot de passe faux ne
lève **pas** de `PronoteAPIError` : le déchiffrement du défi échoue et `_login`
lève `CryptoError`, ou bien `_login` renvoie `False` quand la clé `cle` est
absente de la réponse d'`Authentification`. Le compteur d'échecs s'accroche donc
à `CryptoError` **et** à `logged_in is False`, jamais à une erreur HTTP ou
protocolaire.

C'est le point le plus facile à rater de `ratelimit.py`, et le plus coûteux :
accroché à la mauvaise exception, le compteur censé protéger l'adresse IP ne
s'incrémenterait jamais.

**Exigence.** Au troisième échec dans l'heure, l'intégration **cesse
d'essayer**, ouvre une réparation invitant à vérifier les identifiants, et attend
`credentials_hold` (1 h par défaut). Réessayer un mot de passe faux en boucle est
exactement ce qui fait suspendre une adresse.

**Exigence.** L'enrôlement par QR code est **exclu** de ce compteur : il coûte
deux connexions par construction (§7.2), et le doublement volontaire ne doit pas
déclencher le garde-fou.

#### N'affirmez jamais la suspension d'adresse IP

`pronotepy` décide « adresse IP suspendue » avec `if "IP" in html` — deux
majuscules, n'importe où dans la page. Un établissement dont la page contient
« IP » dans un titre, un menu, une classe CSS, une bannière ENT, ou les lettres
de `SKIP`, `ZIP` ou `EQUIPE`, déclenche la détection.

Bâtir sur ce signal une attente d'une heure, l'arrêt de tout appel et une
réparation « au texte explicite » annoncerait à des parents que leur adresse
est bannie parce qu'un collège a écrit « Espace IP » dans un pied de page — et
le ferait en s'arrêtant vraiment.

**Exigence.** Un amorçage qui échoue est traité comme « connexion impossible,
cause indéterminée » : attente générique, et une réparation dont le texte dit que
la connexion n'a pas pu être établie et **énumère** les causes possibles sans en
choisir une. L'intégration n'affirme jamais une suspension d'adresse IP depuis ce
signal.

**Exigence.** Le seul test d'amorçage retenu est l'**absence du bloc
d'attributs `Start({…})`** — et même là, la réparation ne nomme pas la cause.

### 6.4 Heures creuses

**Exigence.** Hors de la fenêtre `[quiet_end, quiet_start[` (06:00 → 22:00 par
défaut), seuls les paliers de priorité critique s'exécutent. Ne pas interroger
un serveur scolaire la nuit retire un tiers du budget quotidien sans retirer une
seule information utile.

**Exigence.** L'option est désactivable et les bornes sont configurables. Une
famille en décalage horaire ou un internat n'ont pas la même fenêtre utile.

### 6.5 Stratégie de session — reconnexion paresseuse

Fermer la session entre les lots paraît économe : un maintien à 523 appels
contre 256 pour une reconnexion par lot. C'est un mauvais modèle de coût, et
deux corrections annulent l'écart :

- Le maintien ne coûte pas 523 appels. `_KeepAlive.alive()` ne ping que si la
  session est **inactive depuis 110 secondes**, et `last_ping` est remis à jour
  par *chaque* POST : le trafic de données déplace les pings. Coût réel
  ≈ 523 − 167 ≈ **356**.
- Une connexion ne coûte pas 4 appels mais **1 GET + 4 POST**, et 1 à 2
  `SecurisationCompteDoubleAuth` de plus en mode jeton : **5 à 7 requêtes**. Le
  GET d'amorçage n'est pas optionnel, c'est lui qui rapporte les attributs
  `Start({…})`.

La comparaison honnête est donc 356 contre 320 à 448 : un match nul. Et trois
coûts non chiffrés font basculer la décision dans l'autre sens.

**Il n'existe aucune déconnexion dans `pronotepy`.** « Fermer la session » se
réduit à `communication.session.close()`, qui jette le pool TCP côté client ; la
session serveur vit jusqu'à son propre délai d'inactivité. Une connexion par
lot ne crée donc pas 64 sessions successives mais **64 sessions qui se
chevauchent**, et 64 `derniereConnexion` par jour dans le journal que PRONOTE
présente à l'établissement.

**`Erreur.G = 25` sanctionne les demandes d'*autorisation*.** Si la grandeur
mesurée par le serveur est bien celle des autorisations, alors la variable de
risque est le nombre de connexions — et une connexion par lot la multiplie par
64 pour économiser des *appels*, une variable qui n'est probablement pas
comptée.

**La rotation du jeton et la 2FA.** En mode jeton, chaque `Authentification`
renvoie un nouveau `jetonConnexionAppliMobile` et le §7.2 exige de le
réenregistrer. Soit 64 réécritures de `.storage` par jour, et surtout **64
fenêtres par jour** pendant lesquelles un arrêt brutal entre la rotation
serveur et la persistance locale laisse un jeton mort et une intégration
verrouillée — récupérable seulement en rescannant un QR code. Et 64 occasions
par jour d'être interrogé en 2FA, alors que le §8.1 interdit de conserver le
PIN : une connexion par lot contredirait le §8.1.

#### La stratégie retenue

**Exigence.** Garder la session et se reconnecter **paresseusement**, sur un
code d'expiration de session — `Erreur.G = 10`, et `8` (« La page a
expiré ! ») qu'un établissement emploie pour dire la même chose
(`SESSION_EXPIRED_CODES`). Coût nul tant que la session vit, coût d'une
connexion quand elle meurt. Avec `timetable` à 15 minutes, si le délai
d'inactivité de l'établissement est supérieur à cet intervalle — le réglage
courant est 30 minutes — le trafic réel maintient la session et le nombre de
connexions tombe de 64 à **une à trois par jour**. Budget total ≈ **180 appels
par jour**.

**Exigence.** Cette stratégie repose entièrement sur le §3.6 : sans le client
durci, `pronotepy` se reconnecte de lui-même et l'expiration n'est jamais
observable.

**Exigence.** Une session sans aucun appel réussi depuis une heure
(`PRESUMED_DEAD_AFTER_SECONDS`) est abandonnée au prochain travail : c'est ce
qui rattrape le code d'expiration que personne n'a encore vu.

**Exigence — mesurer au lieu de supposer.** Le `SessionManager` enregistre
l'intervalle observé entre le dernier appel réussi et la première expiration,
et l'expose en `sensor.<compte>_duree_vie_session`.

C'est ce qui rend le choix défendable plutôt que parieur : **si le délai mesuré
s'avère inférieur à l'intervalle de `timetable`, la stratégie paresseuse
dégénère en une connexion par lot**. Elle n'est donc jamais significativement
pire, pour toute valeur du paramètre inconnu.

**Exigence — la dégénérescence est un mécanisme.** Trois expirations
consécutives font passer la stratégie effective (`effective_strategy`) à une
connexion par lot ; une sonde quotidienne garde la conclusion falsifiable, et
la lever exige une contre-preuve — une session qui survit à un intervalle
d'inactivité au moins aussi long que le plus court de ceux qui en ont tué une.
Le coût du mauvais cas est ainsi borné à trois requêtes perdues, puis une par
jour pour la sonde.

**Exigence.** La stratégie est une option de l'entrée (`SessionStrategy`) :
`LAZY` par défaut et recommandé, `PER_BATCH` offert comme échappatoire
explicite pour un établissement dont on préfère épingler la politique plutôt
que la découvrir.

**Exigence.** Avant la première mesure, le comportement par défaut est le
pessimiste — on ne présume pas d'un délai généreux. La mesure ne fait que
relâcher la contrainte, jamais la poser.

**Exigence.** Le maintien actif (`_KeepAlive`) n'est **pas** utilisé. Il n'a
d'intérêt que pour un intervalle de collecte inférieur à 110 secondes, et aucun
palier ne descend là.

### 6.6 Le limiteur est observable

**Exigence.** Le budget n'est pas un réglage aveugle. Il alimente des entités de
diagnostic — appels du jour, budget restant, âge de la dernière collecte,
indicateur de bridage, motif de la dernière attente (annexe A, §7). Un réglage
qu'on ne peut pas observer ne se règle pas ; il se subit.

---

## 7. Configuration

### 7.1 Entrées et appareils

**Exigence.** Une entrée de configuration par **compte**. Un compte parent à
plusieurs enfants crée un appareil par enfant, sous un appareil parent portant
l'établissement. Les entités d'un enfant sont rattachées à son appareil — c'est
ce qui rend les déclencheurs d'appareil du §2.3 lisibles.

Ce choix a une conséquence voulue : le budget d'appels est **partagé entre les
enfants d'un même compte**, parce que c'est la même session et la même adresse
IP que le serveur voit. Un compte parent n'est pas N comptes élève, c'est une
session multiplexée : les appels restent sérialisés *à travers* les enfants.

#### Ce qui sépare un compte parent d'un compte élève

Moins qu'on ne l'imagine, et c'est ce qui le rend piégeux. `ParentClient` hérite
de `Client` : même surface de données, mêmes méthodes. Il n'en surcharge qu'une,
`post()`, qui ajoute un champ à l'enveloppe —
`"membre": {"N": <id enfant>, "G": 4}`. Au niveau du fil, c'est toute la
différence. Le `genreEspace` lui-même n'est pas propre à la classe : il vaut
`int(attributes["a"])`, lu dans la page d'amorçage, donc c'est l'URL
(`eleve.html` ou `parent.html`) qui décide du type de compte.

Le reste est de la mécanique côté client, et elle porte trois pièges.

**Exigence — jamais de sélection implicite.** `ParentClient.__init__` positionne
`_selected_child = self.children[0]`. Un code qui omet `set_child()` ne lève
donc aucune erreur : il rapporte les données du **premier** enfant, silencieuse-
ment. La passerelle n'appelle jamais un onglet sans avoir explicitement
sélectionné l'enfant dans le même bloc atomique, et un test de contrat le vérifie
avec deux enfants.

**Exigence.** Chaque DTO porte le `student_id` de l'enfant auquel il appartient,
et le coordinateur refuse un instantané dont le `student_id` ne correspond pas à
l'enfant demandé. C'est la seule protection réelle contre l'inversion décrite au
§3.2 : sans elle, le défaut ne se voit qu'à l'œil nu, sur un tableau de bord, par
un parent qui connaît l'emploi du temps de ses enfants.

**Exigence.** Les deux défauts d'amont identifiés au §3.6 — `ParentClient.post`
sans garde `_refreshing`, et `refresh()` qui perd la sélection de l'enfant — sont
**exclusivement** sur le chemin parent. Un compte élève n'en rencontre aucun. Le
client durci les neutralise, mais la revue de code doit savoir que le chemin
parent est le chemin fragile.

**Exigence.** Les onglets accessibles viennent de `listeOnglets`, propre au
compte : un parent et son enfant peuvent avoir des surfaces de données
différentes sur le même établissement. Une écriture partie d'un compte parent
porte bien la signature `membre`, donc l'appel est formé — mais ce que PRONOTE
*enregistre* relève des droits de l'établissement et n'est pas déductible de
la source : un parent ne coche pas un devoir (§8.3).

### 7.2 Flux initial

```
user ──► choix de la méthode
          ├─ qr_code    ──► charge utile du QR + PIN ──► enrôlement
          ├─ credentials──► URL + identifiant + mot de passe
          └─ ent        ──► URL + identifiant + mot de passe + fournisseur ENT
                              │
                       validation (1 connexion)
                              │
                   sélection des enfants (compte parent)
                              │
                     réglages (§7.3, valeurs par défaut)
```

**Exigence.** La validation n'effectue **aucun réessai**. Un flux qui reboucle
sur une saisie fautive est le premier chemin vers une suspension d'adresse.

**Exigence.** Le chemin QR code coûte **deux** connexions, pas une :
`qrcode_login` construit un client — dont le constructeur se connecte —, poste
`PageInfosPerso` 49, puis appelle `token_login()` avec les identifiants exportés,
soit une seconde poignée de main complète. Ce doublement est structurel, il est
budgété comme tel, et il est exclu du compteur d'échecs (§6.3).

**Exigence.** `export_credentials()` est appelé après chaque connexion réussie
et le résultat remplace les identifiants stockés dans l'entrée. Le jeton
d'application mobile issu d'un QR code tourne à chaque usage : ne pas le
réenregistrer, c'est perdre l'accès au prochain démarrage.

**Exigence.** Un échec d'authentification ouvre un flux de ré-authentification
(`async_step_reauth`), pas une entrée en erreur muette.

### 7.3 Options

Trois sections, préremplies — sauf le fuseau, qui s'affiche vide tant que
rien n'est choisi (§4.2) :

**Général** — `master_tick`, `establishment_timezone`, `homework_horizon`
(jours de devoirs à **afficher**, 14 par défaut — filtre de présentation, sans
effet sur le budget, cf. §5.2), `wake_margin` (marge du capteur de réveil,
90 min par défaut), `stale_after` (§5.4), `session_strategy` (§6.5),
`write_operations_enabled` (vrai pour une nouvelle entrée PRONOTE, faux pour
une entrée qui ne porte pas la clé, §8.3).

**Paliers** — pour chaque palier (§5.2), son intervalle en minutes et son
activation.

**Limiteur** — `min_request_interval`, `max_requests_per_hour`,
`burst_size`, `max_requests_per_day`, `max_wait`, `max_logins_per_day`,
`max_failed_logins_per_hour`, `credentials_hold`, `bootstrap_hold`,
`backoff_base`, `backoff_max`, les délais HTTP de connexion et de lecture,
`quiet_hours_enabled`, `quiet_start`, `quiet_end`.

**Note.** L'option `history_periods` (nombre de périodes closes suivies,
0 = toutes) est lue par l'intégration mais n'est offerte dans aucune section du
formulaire.

**Exigence.** Un changement d'options recharge l'entrée sans redémarrage, et une
réduction d'intervalle ne déclenche pas de collecte immédiate : la prochaine
échéance est recalculée depuis la dernière collecte, pas depuis le changement.

---

## 8. Sécurité

### 8.1 Ce qui ne doit jamais atteindre un état ni un journal

Quatre secrets, de natures différentes :

| Secret | Pourquoi | Traitement |
| --- | --- | --- |
| Mot de passe | entre dans la dérivation des clés | jamais journalisé, jamais en attribut |
| Jeton d'application mobile | réutilisable, tourne à chaque connexion | stocké dans l'entrée, jamais exposé |
| PIN de compte | déverrouille l'enrôlement | jamais conservé après usage |
| **URL iCal** | porteur d'authentification autonome, sans expiration connue | voir §8.2 |

#### Le PIN

Le PIN n'est pas conservé, et PRONOTE décide seul quand le redemander —
typiquement sur une session neuve ou un appareil inconnu. `_do_2fa` lève alors
`MFAError` si aucun PIN n'est disponible. Combinée à une connexion par lot,
cette règle donnerait des dizaines d'occasions quotidiennes d'échouer durement
à trois heures du matin sans possibilité de se rétablir ; avec la reconnexion
paresseuse du §6.5 — une à trois connexions par jour — l'exposition est quasi
nulle.

**Exigence.** Le PIN n'est jamais persisté, et une demande de 2FA n'est pas un
échec silencieux : elle ouvre un flux de ré-authentification qui **demande le
PIN à l'utilisateur**, et l'intégration attend. C'est le seul comportement
honnête — un secret qu'on refuse de stocker est un secret qu'il faut savoir
redemander.

**Exigence.** Un test dédié (`test_no_secret_in_state.py`) exécute un cycle
complet au niveau `DEBUG` avec des secrets connus, puis affirme qu'aucun
n'apparaît ni dans les journaux, ni dans un état, ni dans un attribut, ni dans
la sortie de diagnostic.

**Exigence.** Un test vérifie que `manifest.json` ne contient **pas** de clé
`loggers`. L'exigence n'a de valeur que testée : elle protège contre un ajout
bien intentionné, et c'est exactement le genre de ligne qu'on ajoute pour
déboguer puis qu'on oublie de retirer.

**Exigence.** Le journaliseur `pronotepy` n'est jamais activé en `DEBUG` par
l'intégration, et `manifest.json` ne contient pas de clé `loggers`.
`pronotepy/pronoteAPI.py` journalise à ce niveau l'hexadécimal de la requête en
clair, identifiant compris.

### 8.2 L'URL iCal n'est pas une entité

Exposer cette URL comme état d'un capteur est commode, et dangereux : quiconque
la détient lit l'emploi du temps de l'élève sans identifiant ni mot de passe, et
les états partent dans l'enregistreur, les sauvegardes, les captures d'écran et
les rapports de bug.

**Exigence.** L'URL iCal est fournie par un service à réponse
(`SupportsResponse.ONLY`) et **n'existe dans aucun état**. Elle est absente de
la sortie de diagnostic. L'entité `calendar` locale couvre le besoin réel — voir
l'emploi du temps dans un agenda — sans exporter de porteur.

**Exigence.** Aucun palier ne collecte l'URL iCal. La collecter dans `static`
la déposerait dans le `SnapshotStore`, une structure mémoire longue durée
**dont la fonction est d'être vidée dans un rapport de diagnostic** — le danger
même que ce paragraphe existe pour prévenir. `get_ical_url` appelle
`export_ical()` à la demande, un appel budgété, et rend la valeur sans la
stocker.

**Exigence.** Même traitement pour `get_identity` : `ClientInfo._cache()`
court-circuite `ClientBase.post` (§3.1), donc l'identité est lue par une
fonction de la passerelle appelée à la demande, jamais par une lecture de
propriété depuis une entité.

**Réserve documentée.** La réponse d'un service appelée depuis une automatisation
ou un script est écrite dans la **trace**, et les traces sont persistées dans
`.storage` et visibles dans l'interface. Cela ne remet pas en cause le choix du
service à réponse — c'est toujours strictement mieux qu'un état — mais la
documentation doit dire que ces services sont à usage interactif.

### 8.3 Écritures

**Exigence.** Les opérations qui modifient PRONOTE (marquer un devoir fait,
marquer une actualité lue, envoyer un message) sont derrière l'option
`write_operations_enabled`, **vraie à la création** d'une entrée PRONOTE. Une
liste de devoirs qu'on ne peut pas cocher passe pour une panne.

**Exigence.** Une entrée qui ne porte pas l'option reste en lecture seule :
personne n'y a choisi d'écrire. La valeur vraie est inscrite dans les options à
la création, jamais lue comme repli. Le formulaire d'options suit la même
règle, puisqu'il est renvoyé tel qu'il est affiché. Un message envoyé ne se
rappelle pas.

**Exigence.** L'entité `todo` reste visible quand l'option est fausse, mais
n'annonce pas `UPDATE_ITEM` : mieux vaut une case non cochable qu'une case qui
échoue au clic.

**Exigence.** Quand l'option est vraie, l'entité annonce aussi
`SET_DUE_DATE_ON_ITEM` et `SET_DESCRIPTION_ON_ITEM`, sans pour autant les
écrire. La carte de liste de Home Assistant coche un élément en le renvoyant
entier, et `todo.update_item` refuse un champ dont la fonctionnalité n'est pas
annoncée. Un appel qui change la matière, l'énoncé ou l'échéance est refusé
avec une explication plutôt qu'ignoré : seule la coche part vers PRONOTE.

**Exigence.** Sur un compte **parent**, la liste de devoirs reste en lecture
seule même quand l'option est vraie, le service `mark_homework_done` est refusé
avant tout envoi, et l'action d'appareil « Cocher un devoir » n'est pas
proposée. PRONOTE répond normalement à `SaisieTAFFaitEleve` venant d'une
session parent et n'enregistre rien, y compris avec le `N` de la session qui
envoie. Dans l'Espace Parents, l'état d'un devoir est d'ailleurs un texte, pas
une case.

**Exigence.** Une coche n'est annoncée réussie qu'après relecture. La réponse du
serveur à une écriture ne prouve rien : elle est la même quand l'écriture est
ignorée. La liste est donc relue après l'envoi, dans la même session, et une
coche que la relecture ne montre pas appliquée lève une erreur, sur laquelle
la carte remet la case dans son état. Elle coûte trois requêtes : lecture,
envoi, relecture.

### 8.4 Diagnostic

**Exigence.** `async_get_config_entry_diagnostics` masque `password`,
`username`, `uuid`, `client_identifier`, les jetons, l'URL iCal, l'INE, les
adresses, les courriels et les numéros de téléphone. Le reste — options,
compteurs du limiteur, âges des instantanés, décomptes par palier — est rendu
tel quel : c'est ce qui rend un rapport de bug exploitable.

---

## 9. Enregistreur et volume d'attributs

Les listes de PRONOTE sont volumineuses : l'enregistreur de Home Assistant
refuse un jeu d'attributs de plus de 16 384 octets, et un emploi du temps de
période dépasse ce seuil.

**Exigence.** L'état d'une entité de liste est un **décompte** ; la liste vit
dans un attribut, et **tout attribut de liste est déclaré dans
`_unrecorded_attributes`**. L'historique conserve alors une courbe utile — le
nombre de devoirs, la moyenne — sans stocker le détail à chaque écriture.

**Exigence.** La déclaration est faite **une fois**, sur les classes de base, et
la liste des noms vit dans une constante partagée. Une sous-classe ne redéclare
jamais `_unrecorded_attributes` : Home Assistant ne réunit pas ces ensembles le
long d'une hiérarchie. `Entity.__init_subclass__` calcule
`_entity_component_unrecorded_attributes | cls._unrecorded_attributes`, où le
terme de droite est résolu par recherche d'attribut ordinaire — donc une
déclaration sur une sous-classe **remplace** celle du parent au lieu de
l'étendre, et retire au silence toutes ses entrées. Un nom nouveau s'ajoute à la
constante.

```python
# const.py -- le seul endroit où la liste des noms est écrite.
UNRECORDED_LIST_ATTRIBUTES: Final = frozenset({"items", "lessons", ...})


# entity.py -- déclaré sur les bases, et sur chaque *frère* d'une base.
class PronoteEntity(CoordinatorEntity["PronoteTierCoordinator"]):
    _unrecorded_attributes = frozenset({*UNRECORDED_LIST_ATTRIBUTES, "fetched_at"})


# Une sous-classe hérite, et ne déclare rien.
class PronoteListSensor(PronoteEntity, SensorEntity): ...
```

**Exigence.** Le filtre de l'enregistreur ne porte que sur les clés de **premier
rang** — `recorder.db_schema.shared_attrs_bytes_from_event` est une
compréhension sur `state.attributes.items()`. Une valeur imbriquée n'est donc
jamais exclue par son propre nom : elle l'est en excluant la clé qui la
contient. Nommer une clé imbriquée n'échoue pas, ne prévient pas, et n'exclut
rien.

Ces deux exigences sont des barrières et non des conventions, parce qu'une
erreur sur l'une ou l'autre ne se voit pas : la déclaration paraît correcte et
un attribut portant une adresse signée part quand même dans l'historique. Deux
tests les tiennent — l'un passe l'état publié dans le
filtre réel de l'enregistreur, l'autre refuse toute déclaration de sous-classe
qui ne couvre pas la constante partagée.

---

## 10. Internationalisation

L'intégration est destinée à un public francophone mais s'installe dans un Home
Assistant qui peut être dans n'importe quelle langue. L'i18n n'est donc pas une
finition : elle conditionne la lisibilité de l'éditeur d'automatisation (§2.3),
et une clé de traduction manquante y produit une entrée illisible plutôt qu'une
erreur.

### 10.1 Structure des fichiers

**Exigence.** La structure est celle que Home Assistant valide, et l'anglais y
est le fichier de référence même si le public est français :

```
custom_components/carnet_scolaire/
├── strings.json              # en anglais, vérifié par hassfest
└── translations/
    ├── en.json               # copie de strings.json
    └── fr.json               # traduction française
```

Les trois fichiers, et `services.yaml`, sont générés depuis une table unique
par `scripts/build_translations.py` ; un test vérifie qu'ils en sont la copie
exacte.

**Exigence.** Aucune chaîne visible par l'utilisateur n'est écrite dans le code
Python. Ni dans une entité, ni dans un service, ni dans une exception, ni dans
une réparation. Une chaîne en dur est un défaut de conformité, pas un détail de
présentation.

### 10.2 Noms d'entités

**Exigence.** Toute entité porte `_attr_has_entity_name = True` et un
`_attr_translation_key`. Aucun nom n'est construit par f-string.

Les noms variables passent par `translation_placeholders` :

```python
_attr_translation_key = "grades_period"
_attr_translation_placeholders = {"period": period.label}
```

```json
"grades_period": { "name": "Notes {period}" }
```

Le libellé de période vient de PRONOTE et n'est **pas** traduit (§10.6) ; seule
la structure de la phrase l'est.

### 10.3 États traduits, comparaisons non traduites

Le capteur `etat_limiteur` a six valeurs. Elles doivent s'afficher en français
et rester comparables dans une automatisation.

**Exigence.** Un capteur à valeurs fermées déclare
`device_class: SensorDeviceClass.ENUM` et son tuple `options`, puis traduit ses
états :

```json
"limiter_state": {
  "name": "État du limiteur",
  "state": {
    "nominal": "Nominal",
    "throttled": "Bridé",
    "backoff": "En repli",
    "quiet_hours": "Heures creuses",
    "credentials_hold": "Identifiants à vérifier",
    "bootstrap_failed": "Connexion impossible"
  }
}
```

**Exigence documentée.** Une automatisation compare **l'état brut**
(`throttled`), jamais le libellé affiché. C'est ce qui rend la traduction des
états sans danger pour l'automatisation, et cela doit être écrit dans la
documentation utilisateur : un utilisateur qui recopie « Bridé » dans un
déclencheur écrit une automatisation qui ne partira jamais.

### 10.4 Flux de configuration et d'options

**Exigence.** Sont traduits : les titres d'étape, les libellés de champ, les
`data_description` (le texte d'aide sous un champ), les messages d'erreur, les
motifs d'abandon et les titres de section.

Les `data_description` portent une charge particulière ici : c'est là
qu'un utilisateur apprend ce que fait `min_request_interval` sans lire l'annexe
B. Un champ de limiteur sans texte d'aide est un champ mal réglé.

### 10.5 Services, exceptions, réparations

**Exigence.** `services.yaml` ne contient aucun texte : noms, descriptions et
libellés de champ vivent dans la section `services` des fichiers de traduction.

**Exigence.** Toute exception remontée à l'utilisateur est levée avec ses clés :

```python
raise HomeAssistantError(
    translation_domain=DOMAIN,
    translation_key="write_disabled",
)
```

**Exigence.** Les réparations (`issue_registry`) du §6.3 ont leur titre et leur
description traduits, avec substituteurs pour les valeurs variables — nombre de
tentatives, heure de fin d'attente.

### 10.6 Ce qui ne doit pas être traduit

**Exigence.** Les données de PRONOTE traversent l'intégration **verbatim**.
Noms de matières, noms de professeurs, libellés de périodes, catégories
d'actualités, statuts de cours, motifs d'absence, intitulés de punitions,
descriptions de devoirs : tout cela vient de l'établissement, en français, et
n'est ni traduit, ni normalisé, ni mis en majuscules.

Une seule exception, et elle est délibérée : **les sentinelles de note** (`|1` à
`|8`) forment une énumération fermée du protocole, pas un texte
d'établissement. Le DTO conserve l'énumération (`GradeStatus.ABSENT`) et la
traduction a lieu à la couche entité, comme au §10.3. C'est la seule donnée
PRONOTE dont l'affichage est traduit, parce que c'est la seule dont les valeurs
sont connues d'avance.

### 10.7 Dates et nombres

**Exigence.** Aucun formatage de date ni de nombre dans le code. Un état est un
horodatage ISO 8601 conscient du fuseau, ou un nombre ; l'interface le met en
forme selon la locale du navigateur.

Un capteur dont l'état vaudrait `8h30` ou `14,5` est un défaut : il perd le
tri, le graphique, la comparaison de seuil et la locale d'un seul coup.

### 10.8 Le piège des identifiants d'entité

Home Assistant fabrique l'`entity_id` à partir du nom **traduit dans la langue
active au moment de la création**. Une installation en français produit
`sensor.emma_prochain_cours` ; la même en anglais produit
`sensor.emma_next_lesson`. Changer la langue ensuite ne renomme rien.

**Exigence.** La documentation le dit explicitement et donne la conséquence
pratique : les automatisations partagées entre utilisateurs passent par les
**déclencheurs d'appareil** du §2.3, qui sont indépendants de la langue
d'installation. Les blueprints livrés utilisent des sélecteurs d'entité, jamais
un `entity_id` codé en dur.

**Exigence.** Les exemples de la documentation portent une note d'un ligne
rappelant que les identifiants dépendent de la langue d'installation. Un
copier-coller silencieusement inopérant est le pire des accueils.

### 10.9 Blueprints

Home Assistant n'offre aucun mécanisme de traduction pour les blueprints : leur
`name` et leur `description` sont du texte figé.

**Exigence.** Les blueprints du §2.3 sont livrés dans les deux langues, en
sous-répertoires séparés :

```
blueprints/automation/carnet_scolaire/
├── fr/   # noms et descriptions en français
└── en/   # noms et descriptions en anglais
```

La duplication est assumée : c'est la seule voie disponible, et un blueprint est
un fichier court dont seuls les textes changent.

### 10.10 Traductions vérifiées, pas espérées

Une clé référencée par le code mais absente des fichiers de traduction ne fait
échouer ni la construction ni le démarrage. Le symptôme est silencieux :
l'interface affiche la clé brute.

**Exigence.** Un test (`test_translations.py`) parcourt le code, collecte tous
les `translation_key` employés — entités, états d'énumération, services, champs,
erreurs, réparations, automatisations d'appareil — et affirme que **chacun
existe dans chaque fichier de langue livré**. Le test échoue aussi sur l'inverse :
une clé présente dans `fr.json` et absente de `strings.json` signale une
traduction orpheline.

**Exigence.** `hassfest` valide la structure ; le test ci-dessus valide la
couverture. Les deux sont bloquants en intégration continue.

---

## 11. Qualité

**Exigence.** Chaîne de validation en intégration continue, obligatoire avant
fusion :

- `ruff check` et `ruff format --check`, jeu de règles large, aucune dette
  initiale — le projet démarre vierge, il n'y a pas de raison d'ouvrir un
  registre d'exemptions.
- `mypy --strict` sur `custom_components/carnet_scolaire`, avec
  `[[tool.mypy.overrides]] module = "pronotepy.*"` et
  `follow_imports = "skip"`. `pronotepy` livre bien `py.typed`, mais
  `dataClasses.py` importe `autoslot.Slots` sous `# type: ignore` et porte
  plusieurs annotations ignorées : sans cette dérogation, la première exécution
  de CI se battrait contre l'amont au lieu de notre code.
- `pytest` sur `pytest-homeassistant`, **95 % sur chaque
  module** sans exception, 80 % en global, et 100 % sur `ratelimit.py`,
  `scheduler.py`, `gateway.py`, `delta.py` et les trois
  `connectors/ecoledirecte/ed_*.py` : ce sont les sept modules où une erreur
  est invisible en fonctionnement.
- `hassfest` et validation HACS.
- `test_translations.py` (§10.10) : toute clé employée existe dans chaque
  langue livrée, et aucune traduction n'est orpheline.

### 11.1 Chaîne GitHub Actions

**Exigence.** Des fichiers de workflow séparés, parce qu'ils n'ont ni le même
déclencheur ni le même public. Ceux qui portent la qualité et la publication :

| Fichier | Déclencheur | Contenu |
| --- | --- | --- |
| `validate.yml` | `pull_request`, `push` sur la branche par défaut | `ruff`, `mypy --strict`, `pytest` avec seuil de couverture, matrice de versions Home Assistant |
| `hassfest.yml` | idem | action officielle `home-assistant/actions/hassfest` |
| `hacs.yml` | idem, plus `schedule` hebdomadaire | action `hacs/action` en mode `integration` |
| `release.yml` | `push` d'une étiquette `v*` | archive `zip` de `custom_components/carnet_scolaire`, note de version, publication GitHub |
| `pronotepy-watch.yml` | `schedule` hebdomadaire, `workflow_dispatch` | compare l'épingle `pronotepy` à PyPI et propose la montée de version en *pull request* (§11.1.1) |
| `ecoledirecte-watch.yml` | `schedule` hebdomadaire, `workflow_dispatch` | compare la version d'API EcoleDirecte à celle que publie PyPI |
| `docs.yml` | `pull_request`, `push` sur la branche par défaut | `mkdocs build --strict`, puis publication du site |

S'y ajoutent les workflows de sécurité et d'entretien du dépôt : `codeql.yml`,
`cve.yml`, `scorecard.yml` et `dependabot-auto-merge.yml`.

**Exigence.** `validate.yml` échoue si **un seul module** descend sous 95 %, si
la couverture globale descend sous 80 %, ou si l'un des sept modules critiques
descend sous 100 %. Un seuil qui n'échoue pas n'est pas un seuil, et une
moyenne globale seule n'en est pas un : elle absorbe la chute d'un petit module
derrière la masse des gros.

**Exigence.** L'étiquette de publication est la seule source de version : la
CI de publication l'écrit dans le `manifest.json` du zip publié et l'y
revérifie, le dépôt restant à `0.0.0`. Une intégration HACS dont le manifeste
diverge de l'étiquette s'installe et ne se met plus à jour.

**Exigence.** Le fichier `LICENSE` (MIT) vit sur la branche par défaut : GitHub
ne détecte une licence que là, et la validation HACS interroge l'API, pas les
fichiers du checkout. Un `LICENSE` présent sur une seule branche de travail
laisse le contrôle au rouge.

**Exigence.** Aucun secret de dépôt n'est nécessaire à aucun workflow.
`validate.yml`, `hassfest.yml` et `hacs.yml` n'utilisent aucun jeton ;
`release.yml` et `pronotepy-watch.yml` utilisent celui que GitHub Actions
fournit, avec les permissions minimales déclarées **par job** dans le workflow.

#### 11.1.1 La veille sur l'épingle `pronotepy`

**Exigence.** L'épingle `pronotepy` est comparée une fois par semaine à ce que
PyPI publie, et l'écart est signalé sans intervention humaine. Une mise à jour
du serveur PRONOTE d'un établissement peut faire échouer *toutes* les
connexions — sur un `CryptoError` que l'amont commente « probably the qr code
has expired » — alors que le correctif amont est déjà publié. Une version
épinglée sans veille est une dette dont l'échéance est une panne totale.

**Exigence.** Le signalement prend la forme d'une *pull request* qui relève
l'épingle dans ses **deux** déclarations — `requirements_test.txt` et
`manifest.json` — sur une branche par version amont. Une issue dirait qu'une
version existe ; une *pull request* fait tourner `validate.yml` et dit si elle
fonctionne. Elle porte la version épinglée, la nouvelle version, sa date et les
sujets de commit entre les deux étiquettes amont.

**Exigence.** Rien ne fusionne automatiquement, et aucune fusion automatique
n'est activée : relever l'épingle oblige à relire chaque divergence documentée
dans `hardened_client.py` (§3.6), ce qu'aucun portail ne sait faire.

**Exigence.** Une indisponibilité de PyPI ou de l'API GitHub produit un rapport
et une sortie au vert, jamais un échec. Un portail hebdomadaire qui rougit sur
l'incident d'un tiers est un portail qu'on apprend à ignorer.

**Exigence.** Aucun test ne touche le réseau. Les tests de la passerelle
travaillent sur des réponses protocolaires **écrites à la main**, versionnées
dans `tests/fixtures/` et décodées par le vrai `pronotepy`. Aucune réponse
réelle n'entre dans le dépôt : une capture va dans `tests/fixtures/_raw/`,
ignoré par git. Le revers est connu : les fixtures reflètent la compréhension
du protocole, pas ce qu'un établissement envoie, et une forme de réponse que
personne n'a imaginée n'a pas de test.

**Exigence.** Le 100 % sur `gateway.py` exige une fixture **par groupe de champs
optionnels**, pas une par onglet. `gateway.py` décode tout ce que le réseau
renvoie (§3.4) et le §3.3.3 y impose un décodage non strict : chaque branche
« champ absent » est une branche à couvrir. Dit autrement — une fixture par
onglet donnerait un 100 % de façade.
Les cas à couvrir nommément : moyenne de classe absente, coefficient absent,
salle absente, professeur absent, `DateDuCoursFin` absent (chemin inféré, §4.1),
sentinelle de note inconnue, `current_period` sans onglet 198.

**Exigence.** Un test de contrat par palier vérifie qu'une collecte consomme le
nombre d'appels prévu. C'est la seule protection contre la régression que ce
projet existe pour empêcher : un accès de propriété ajouté par inadvertance, et le coût
double sans que rien ne casse.

**Exigence.** Un test par blueprint livré vérifie qu'il se charge et que les
entités qu'il cible existent dans le catalogue. Un blueprint cassé est un
support client garanti.

---

## 12. Ajouter un palier ou une surface

Deux règles valent pour tout ajout, parce qu'on ne les rattrape pas après coup :

- **Une collecte nouvelle passe par le limiteur dès sa première ligne**, avec
  son test de contrat d'appels (§11). Un ordonnanceur ajouté après coup à des
  collectes déjà écrites ne les contient jamais complètement — il reste
  toujours un chemin qui appelle en direct.
- **Ses évènements (§2.2) et ses textes (§10) arrivent avec elle**, pas à la
  fin. Les évènements sont l'objectif du projet, pas une finition ; et une
  intégration qu'on traduit à la fin est truffée de chaînes en dur qu'il faut
  retrouver une par une.

---

## 13. Écarts assumés et leur coût

À lire comme une liste de ruptures volontaires avec la manière la plus simple
de faire : chacune coûte quelque chose, et il faut savoir ce qu'on achète.

| Sujet | Choix | Coût |
| --- | --- | --- |
| Automatisation | entités primitives + `event` + déclencheurs d'appareil, plutôt que des templates sur des attributs de liste | beaucoup plus d'entités |
| Cadence | onze paliers réglables, plutôt qu'un intervalle unique | plus de code d'ordonnancement |
| Appels | ~10 par cycle complet, ~180/jour, par déduplication | un appel brut par onglet à écrire |
| Session | client durci, reconnexion mesurée, plutôt que la gestion par défaut de `pronotepy` | une sous-classe à maintenir |
| Débit | trois couches configurables, plutôt qu'aucune limite | un module de plus |
| Objets | DTO figés, plutôt que des objets `pronotepy` jusque dans les entités | une couche de correspondance à maintenir |
| Fuseau | conscient, converti à la frontière, plutôt que naïf | une option de plus à expliquer |
| URL iCal | service à réponse, jamais un état | l'URL n'est pas lisible dans un template |
| Devoirs | entité `todo` avec écriture, plutôt qu'un capteur en lecture seule | surface d'écriture à sécuriser |
| i18n | `strings.json` de référence, couverture testée | un test de plus, aucune chaîne en dur tolérée |
| Historique | périodes closes relues une fois par jour, pas en boucle | aucun |

---

## Annexes

- **[Annexe A — catalogue des entités et services](annexe-a-entites.md)** :
  chaque entité, son état, ses attributs, son palier, et le champ PRONOTE
  d'origine.
- **[Annexe B — limiteur de débit](annexe-b-rate-limit.md)** : les trois
  couches en détail, les options, l'arithmétique du budget, le repli et les
  diagnostics.
