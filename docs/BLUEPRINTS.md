# Les huit blueprints, en détail

Un blueprint est un modèle d'automatisation : vous choisissez l'élève et ce
qu'il faut faire, le modèle fournit le reste. Les huit livrés avec Carnet scolaire
existent parce que huit automatisations scolaires reviennent tout le temps, et
que chacune contient un piège qu'on ne voit qu'après s'être fait prendre.

Ce document explique chaque réglage et nomme chaque piège. Pour la vue
d'ensemble et **la procédure d'import**, voyez le
[§ 9.1 du guide de l'utilisateur](GUIDE-UTILISATEUR.md#91-les-huit-blueprints-livrés).
Pour des automatisations complètes, prêtes à coller, voyez
[Exemples d'utilisation des blueprints](EXEMPLES-BLUEPRINTS.md).
Si vous arrivez de l'intégration `pronote` avec des automatisations déjà
écrites, lisez d'abord
[Migrer ses automatisations](MIGRATION-AUTOMATISATIONS.md).

---

## Sommaire

- [Ce que les huit ont en commun](#ce-que-les-huit-ont-en-commun)
- [1. Réveil adaptatif](#1-réveil-adaptatif)
- [2. Rappel des devoirs du lendemain](#2-rappel-des-devoirs-du-lendemain)
- [3. Cours annulé ou modifié](#3-cours-annulé-ou-modifié)
- [4. Absence, retard ou punition](#4-absence-retard-ou-punition)
- [5. Nouvelle note](#5-nouvelle-note)
- [6. Nouveau message ou information](#6-nouveau-message-ou-information)
- [7. Menu de la cantine](#7-menu-de-la-cantine)
- [8. Retour de l'école](#8-retour-de-lécole)
- [Si rien n'arrive jamais](#si-rien-narrive-jamais)

---

## Ce que les huit ont en commun

**Rien n'est codé en dur, et c'est délibéré.** Home Assistant fabrique
l'identifiant d'une entité à partir de son nom **traduit** : « Prochain
réveil » devient `sensor.enfant_un_prochain_reveil` en français et
`sensor.enfant_un_next_wake_up` en anglais. Un blueprint qui écrirait
l'identifiant en dur ne fonctionnerait que sur les installations d'une seule
langue. D'où les sélecteurs.

**Certains demandent un appareil, d'autres un capteur.** Ce n'est pas une
incohérence :

| Le blueprint réagit à… | Il demande | Pourquoi |
| --- | --- | --- |
| un changement (note, absence, annulation) | l'**appareil** de l'élève | les déclencheurs d'appareil sont indépendants de la langue d'installation |
| une heure (réveil, rappel, menu) | le **capteur** | un déclencheur horaire a besoin de l'entité, dont l'identifiant dépend de la langue |

La liste des appareils **exclut l'appareil du compte** : « une note est
arrivée » n'arrive pas à un compte, ça arrive à un élève.

**Le champ « Action » accepte une séquence entière.** C'est le point le plus
sous-estimé. Ce champ ne veut pas dire « une notification » : il veut dire « ce
que vous voulez », conditions comprises. Vous pouvez y mettre un test de
présence, un drapeau, un `si/alors`, plusieurs actions. Une condition placée
dans cette séquence **arrête la séquence** quand elle n'est pas remplie, ce qui
permet d'ajouter vos propres filtres sans toucher au blueprint. L'exemple
complet est au
[§ 6 de « Migrer ses automatisations »](MIGRATION-AUTOMATISATIONS.md#6-garder-ses-conditions-tout-en-utilisant-un-blueprint).

**Deux variables sont toujours disponibles** dans votre action : `title` et
`message`. `message` est déjà rédigé — matière, heure, valeur selon le cas — et
chaque section ci-dessous donne la phrase exacte.

**Les seuils sont toujours strictement supérieurs.** « Alerter à partir de 0
devoir » signifie « dès qu'il y en a au moins un », pas « même s'il n'y en a
aucun ». Même convention pour le seuil de retard.

**Les capteurs optionnels se laissent vides.** Quand un réglage propose un
capteur « (optionnel) », sa valeur par défaut est une liste vide, et une
condition d'état portant sur une liste vide d'entités passe toujours. Laisser
vide ne désactive donc rien d'autre que ce réglage.

**Le mode est déclaré, jamais laissé par défaut.** Les blueprints horaires sont
en `single` — un rappel par soir. Ceux qui réagissent à un changement sont en
`queued`, avec une file dimensionnée pour une vraie journée : l'intégration émet
**un évènement par élément**, donc huit notes publiées d'un coup produisent huit
déclenchements. En `single`, les sept dernières seraient jetées sans un mot.

---

## 1. Réveil adaptatif

Exécute l'action de votre choix à l'heure calculée pour le premier cours du
prochain jour de classe.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Capteur « Prochain réveil »** | — | L'entité de réveil de l'élève. |
| **Écart supplémentaire** | 0 | S'**ajoute** à la marge de l'intégration. Négatif pour sonner plus tôt. |
| **Pas avant** | 05:00 | Garde-fou : un emploi du temps aberrant ne doit pas réveiller la maison à 3 h. |
| **Pas après** | 10:00 | Garde-fou symétrique : passée cette heure, le réveil n'a plus d'objet. |
| **Capteur « Jour de classe »** | vide | Ceinture et bretelles. Déjà couvert, voir ci-dessous. |
| **Action de réveil** | — | Lumière, volet, playlist, notification… |

**Le piège évité : vous n'avez aucun calendrier à écrire.** Les jours sans
cours, en vacances, ou quand toute la matinée est annulée, le capteur n'a pas de
valeur — et un déclencheur horaire sans valeur **ne s'arme pas**. Il n'y a donc
ni test « est-on en vacances ? », ni liste de jours fériés à maintenir. Le
capteur ignore déjà les cours annulés et ceux dont l'enfant est dispensé, et ne
passe pas au lendemain avant la fin des cours du jour.

**Attention au sélecteur.** Il propose **tous** les capteurs horodatés de
l'intégration. Prenez bien celui du réveil, et non « Prochain cours » ou « Fin
des cours » — l'automatisation fonctionnerait, mais sonnerait à l'heure du
premier cours ou du dernier.

**Où se règle la marge.** Les 90 minutes avant le premier cours sont une
**option de l'intégration** (« Marge de réveil »), pas un réglage du blueprint.
L'« écart supplémentaire » ci-dessus s'y ajoute au lieu de la remplacer : si vous
voulez changer la marge pour de bon, changez l'option.

**Le seul horaire fiable quand les heures de fin sont estimées.** Ce capteur se
calcule sur les heures de **début**, que PRONOTE publie. Les heures de fin, elles,
sont parfois déduites — voir `end_inferred` au § 4.1 du guide.

Message fourni : `Premier cours à 08:30 (MATHÉMATIQUES).`

---

## 2. Rappel des devoirs du lendemain

Rappelle le soir, à heure fixe, ce qu'il reste à faire pour demain.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Capteur « Devoirs demain »** | — | Son état est le **nombre** de devoirs dus le lendemain. |
| **Heure du rappel** | 19:30 | |
| **Nombre de devoirs à partir duquel alerter** | 0 | Strictement supérieur. 0 = dès le premier devoir. |
| **Seulement les devoirs non cochés** | activé | Recommandé, voir ci-dessous. |
| **Action de notification** | — | |

**Le piège évité : un rappel qui sonne pour rien.** Le capteur compte les
devoirs dus demain, **cochés ou non**. Un rappel qui part alors que tout est
terminé use la patience d'un adolescent plus vite qu'il ne l'aide — d'où
l'option, activée par défaut, qui ne retient que les devoirs non cochés. Elle
fonctionne aussi bien si vous cochez dans PRONOTE que dans la liste de tâches de
Home Assistant.

**Les vacances sont gratuites.** Aucun test à écrire : pas de devoirs pour
demain, le capteur vaut 0, la condition ne passe pas, rien n'est dit.

Message fourni : `Il reste des devoirs pour demain : MATHÉMATIQUES, ANGLAIS.`

---

## 3. Cours annulé ou modifié

Notifie à chaque changement d'emploi du temps. Six changements sont distingués,
et vous cochez ceux qui vous intéressent.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Appareil de l'élève** | — | |
| **Changements retenus** | annulé + déplacé | Six cases : annulé, annulation levée, déplacé, salle, professeur, statut. |
| **Seulement le premier et le dernier cours** | désactivé | Ne garder que ceux qui changent l'heure d'arrivée ou de sortie. |
| **Capteur « Cours du jour »** | vide | Nécessaire au réglage ci-dessus, inutile sinon. |
| **Action de notification** | — | |

**Le piège évité : « annulation levée » n'est pas « annulation ».**
`lesson_restored` — le cours a finalement lieu — est un type **distinct** de
`lesson_canceled`. Une détection maison qui les confond fait déclencher « pas de
cours en première heure, dors » le matin même où le cours est rétabli. Ici, si la
case « Annulation levée » n'est pas cochée, ce cas ne passe jamais.

**Ce que « statut » recouvre.** PRONOTE utilise un champ libre pour des choses
qu'il ne marque pas comme annulées : « Prof. absent », « Cours dépl. ». Le
changement est réel et vaut d'être signalé, mais ce n'est pas une annulation.
C'est une case séparée pour cette raison.

**La limite du filtre « premier et dernier cours ».** Il ne connaît que la
journée **en cours**. Une annulation annoncée la veille pour le lendemain ne
passera pas ce filtre. Laissez-le désactivé si vous voulez être prévenu à
l'avance.

Message fourni : `MATHÉMATIQUES de 08:30 : annulé.`

---

## 4. Absence, retard ou punition

Alerte dès que l'établissement saisit une de ces trois choses.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Appareil de l'élève** | — | |
| **Évènements retenus** | absence + retard | Trois cases. La punition est décochée par défaut. |
| **Ignorer les retards plus courts que** | 0 min | Strictement supérieur, et **ne s'applique qu'aux retards**. |
| **Action de notification** | — | |

**Le piège évité : trois phrases pour trois évènements.** Une absence porte des
heures et des jours, un retard porte des minutes, une punition porte une nature
et un donneur — ce ne sont pas les mêmes données. Une automatisation qui les
fusionne finit par annoncer « retard de 0 minute » sur une absence d'une journée
entière. Chaque cas a donc sa formulation.

**Le seuil de retard est une vraie économie.** Les retards de deux minutes
n'apprennent rien à personne et sont fréquents. Le régler à 5 ou 10 minutes
supprime le bruit sans rien perdre d'utile. Il n'affecte ni les absences ni les
punitions.

Messages fournis, selon le cas :

```
absence du 12/09 à 08:00 (2h), non justifiée. Motif : Maladie.
retard de 12 min le 12/09 à 08:00.
punition : Retenue (M. Dupont).
```

---

## 5. Nouvelle note

Notifie à la publication d'une note, avec un seuil facultatif.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Appareil de l'élève** | — | |
| **Seuil de notification** | 20/20 | Ne notifier que si la note, **ramenée sur 20**, est ≤ à cette valeur. 20 = toutes. |
| **Notifier aussi les notes sans valeur** | désactivé | Absent, Dispensé, Non rendu… |
| **Action de notification** | — | |

**Le premier piège évité : ne surveillez pas le capteur « Dernière note ».**
C'est le réflexe naturel, et il est faux. Deux 12/20 consécutifs ne font pas
bouger l'état du capteur, donc **la deuxième note passerait inaperçue**. C'est
exactement la raison d'être des entités « évènement » : un état répond à « où
en est-on », un évènement répond à « quelque chose vient d'arriver ». Le
blueprint utilise l'évènement.

**Le second piège évité : le seuil est remis à l'échelle.** Un 8/10 est une
bonne note, un 8/20 non. Comparer directement une note à « 10 » sans regarder
sur combien elle est notée donne n'importe quoi. Le blueprint utilise le
`out_of` du payload pour ramener la note sur 20 avant de comparer.

**Les notes sans valeur.** « Absent », « Dispensé », « Non noté », « Non
rendu » n'ont pas de valeur numérique : aucun seuil ne peut
s'y appliquer, et elles ont donc leur propre case. Décochée, elles ne notifient
pas ; cochée, elles notifient toujours, seuil ou pas. « Félicitations » n'en
fait pas partie : c'est une note qui garde sa valeur, et le seuil s'y applique.

Message fourni : `MATHÉMATIQUES : 14/20 (coef. 2).` — et `14`, pas `14.0`.

---

## 6. Nouveau message ou information

Notifie l'arrivée d'un message de la messagerie PRONOTE, ou la publication
d'une information par l'établissement.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Appareil de l'élève** | — | |
| **Sources retenues** | les deux | La messagerie est souvent bavarde, les actualités beaucoup moins. |
| **Seulement les informations avec un sondage** | désactivé | Ne garder que les actualités qui attendent une réponse. |
| **Action de notification** | — | |

**Le piège évité : deux sources, deux paliers de collecte.** La messagerie
dépend du palier `discussions`, les actualités du palier `news`, et **chacun peut
être désactivé ou ralenti** dans les options de l'intégration. Si rien n'arrive
jamais, ce n'est pas l'automatisation qu'il faut déboguer — voyez
[Si rien n'arrive jamais](#si-rien-narrive-jamais).

**L'option « sondage » est celle qui rend les actualités supportables.** Un
établissement publie beaucoup ; ce qui demande une action de votre part est une
petite partie. Cochée, l'option ne retient que ça. Elle n'affecte pas la
messagerie.

**Un auteur peut manquer.** Quand la passerelle n'a pas pu déplier le fil de
discussion, l'auteur est inconnu ; le texte affiche alors « Établissement »
plutôt que `None`.

Message fourni : `Mme Martin : Sortie scolaire du 14 octobre`

---

## 7. Menu de la cantine

Annonce le matin, à heure fixe, le menu du jour.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Capteur « Menu du jour »** | — | Son état est le **nombre de plats** du repas du jour. |
| **Capteur « Jour de classe »** | vide | Évite l'annonce les jours où l'enfant ne va pas au collège. |
| **Heure de l'annonce** | 07:15 | |
| **Action d'annonce** | — | Notification, synthèse vocale, affichage… |

**Le piège évité : ne rien dire plutôt que d'annoncer un menu vide.** Beaucoup
d'établissements ne publient pas de menu. Le capteur vaut alors `unknown`, la
condition ne passe pas, et l'automatisation se tait — au lieu d'annoncer « Au
menu aujourd'hui : » chaque matin.

**Le palier `menus` est collecté une fois par jour**, et c'est le plus basse
priorité. Si le menu n'apparaît pas, vérifiez d'abord que votre établissement en
publie un, puis que le palier est actif dans les options.

Message fourni : `Au menu aujourd'hui : Salade de tomates, Poulet rôti, Haricots verts, Yaourt.`

---

## 8. Retour de l'école

Exécute l'action de votre choix quand les cours de l'élève se terminent.

| Réglage | Défaut | À quoi ça sert |
| --- | --- | --- |
| **Capteur « Fin des cours »** | — | L'heure de sortie **réelle** : les cours annulés et les dispenses en sont déjà retirés. |
| **Écart** | 0 | S'**ajoute** à l'heure de sortie. Négatif pour partir avant la sonnerie, positif pour le temps du trajet. |
| **Seulement les journées écourtées par une annulation** | désactivé | Ne rien faire les jours normaux. Voir ci-dessous. |
| **Pas avant** | 10:00 | Garde-fou : un emploi du temps aberrant ne doit pas ouvrir le portail en pleine nuit. |
| **Pas après** | 22:00 | Garde-fou symétrique. |
| **Action à la fin des cours** | — | Notification, portail, chauffage de la chambre, message vocal… |

**Le piège évité : « la journée finit plus tôt » n'est pas « un cours a été
annulé ».** C'est la raison d'être de ce blueprint. Une **dispense** décale
l'heure de sortie exactement comme une annulation le ferait, et un test
`fin prévue ≠ fin réelle` annoncerait donc une annulation le jour où le cours a
bien lieu — l'élève n'est simplement pas tenu d'y être. L'option ci-dessus ne
compare pas deux horaires : elle lit l'attribut `canceled_after`, qui compte les
cours **annulés** finissant après l'heure de sortie retenue. Un test juste, et
non un test qui a l'air juste.

**Les jours sans cours sont gratuits.** Le capteur n'a alors pas de valeur, et
un déclencheur horaire sans valeur **ne s'arme pas**. Aucun calendrier à écrire,
aucun test « est-on en vacances ? ».

**C'est le blueprint le plus exposé aux heures de fin déduites.** Les heures de
début sont publiées par PRONOTE ; les heures de **fin** ne le sont pas toujours,
et sont alors calculées à partir de la durée du créneau. Sur certains
établissements, c'est le cas de **toutes** les fins de cours. L'attribut
`end_inferred` le dit, et le § 4.1 du guide l'explique. Tant que l'action est
« préviens-moi », l'approximation est sans conséquence ; avant d'y accrocher
l'ouverture d'un portail ou un départ en voiture, vérifiez cet attribut.

**Il ne remplace pas le suivi de présence.** Il dit quand les cours finissent,
pas quand l'enfant est parti ni quand il est arrivé.

Message fourni : `Sortie à 16:00 après ÉDUCATION MUSICALE. 1 cours annulé ensuite.`

---

## Si rien n'arrive jamais

Un blueprint qui ne se déclenche pas n'est presque jamais un blueprint cassé.
Trois causes, dans cet ordre de probabilité.

**1. L'établissement ne publie pas la donnée.** La vie scolaire, les menus, la
messagerie et les évaluations sont facultatifs côté PRONOTE. Vérifiez d'abord
l'entité correspondante : si elle est `indisponible` ou à `unknown` depuis
toujours, il n'y a rien à automatiser.

**2. Le palier de collecte est désactivé ou ralenti.** Chaque blueprint dépend
d'un palier, réglable dans les options de l'intégration :

| Blueprint | Palier | Entité à regarder |
| --- | --- | --- |
| Réveil adaptatif | `timetable` | « Prochain réveil » |
| Rappel des devoirs | `homework` | « Devoirs demain » |
| Cours annulé ou modifié | `timetable` | « Cours du jour » |
| Absence, retard, punition | `attendance` | « Absences », « Retards », « Punitions » |
| Nouvelle note | `marks` | « Notes », « Dernière note » |
| Message ou information | `discussions`, `news` | « Messages non lus », « Actualités non lues » |
| Menu de la cantine | `menus` | « Menu du jour » |
| Retour de l'école | `timetable` | « Fin des cours » |

**3. Les collectes sont arrêtées, et rien n'en a l'air.** Carnet scolaire applique
« périmé plutôt qu'indisponible » : quand une collecte échoue, les entités
**gardent leur dernière valeur**. Vos automatisations se taisent alors sans que
rien ne paraisse cassé — l'emploi du temps a juste l'air un peu vieux.

C'est le seul cas qui mérite sa propre automatisation, et ce n'est pas un
blueprint mais trois lignes :

```yaml
triggers:
  - trigger: state
    entity_id: sensor.mon_etablissement_etat_du_limiteur
    from: nominal
    for: "01:00:00"
actions:
  - action: notify.mobile_app_telephone
    data:
      message: >-
        Collectes PRONOTE interrompues :
        {{ states('sensor.mon_etablissement_etat_du_limiteur') }}
```

L'état `credentials_hold` mérite une alerte **immédiate**, sans le délai d'une
heure : il signale des connexions refusées à répétition, ce qui est exactement ce
qui expose à une suspension d'adresse IP par PRONOTE. Le § 6 du guide décrit les
autres entités de diagnostic, et le § 8 les réglages de cadence.

---

## Pour aller plus loin

| Document | Pour qui |
| --- | --- |
| [§ 9 du guide](GUIDE-UTILISATEUR.md#9-automatisations) | Écrire une automatisation sans blueprint : déclencheurs, conditions, actions disponibles |
| [Migrer ses automatisations](MIGRATION-AUTOMATISATIONS.md) | Vous arrivez de l'intégration `pronote` |
| [Exemples d'utilisation](EXEMPLES-BLUEPRINTS.md) | Une automatisation complète et deux variantes d'action par blueprint |
| [Annexe A](annexe-a-entites.md) | L'origine exacte de chaque donnée, et les attributs de chaque évènement |
