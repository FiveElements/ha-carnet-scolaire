# Migrer ses automatisations depuis l'intégration `pronote`

Ce document est pour vous si vous arrivez de l'intégration personnalisée qui
occupe le domaine `pronote` et que vous avez déjà des automatisations écrites
contre ses entités.

Carnet scolaire utilise délibérément le domaine **`carnet_scolaire`** et non `pronote`,
pour que les deux intégrations puissent être installées **en même temps**. C'est
ce qui rend une migration progressive possible : vous ajoutez Carnet scolaire, vous
réécrivez vos automatisations une par une en les vérifiant, et vous ne retirez
l'ancienne intégration qu'à la fin.

**Le piège de calendrier.** Vos anciennes automatisations ne cassent pas quand
vous installez Carnet scolaire — elles cassent quand vous **retirez** l'ancienne
intégration, parce que ses capteurs disparaissent alors du registre. Une
automatisation dont le déclencheur pointe une entité qui n'existe plus reste
affichée comme active : elle ne signale rien et ne se déclenche jamais. Si vous
avez déjà retiré l'ancienne intégration, commencez par le § 2 : l'inventaire est
la seule façon de savoir ce qui est silencieusement mort.

Si vous n'avez pas encore d'automatisations, ce document ne vous concerne pas :
lisez le § 9 du [guide de l'utilisateur](GUIDE-UTILISATEUR.md), qui explique
comment en écrire.

---

## Sommaire

- [1. Ce qui change, en une phrase](#1-ce-qui-change-en-une-phrase)
- [2. Faire l'inventaire avant de toucher à quoi que ce soit](#2-faire-linventaire-avant-de-toucher-à-quoi-que-ce-soit)
- [3. La correspondance des entités](#3-la-correspondance-des-entités)
- [4. Le schéma d'un cours a changé](#4-le-schéma-dun-cours-a-changé)
- [5. Ce qui n'a plus d'équivalent](#5-ce-qui-na-plus-déquivalent)
- [6. Garder ses conditions tout en utilisant un blueprint](#6-garder-ses-conditions-tout-en-utilisant-un-blueprint)
- [7. Cinq pièges rencontrés en vrai](#7-cinq-pièges-rencontrés-en-vrai)
- [8. Vérifier, puis nettoyer](#8-vérifier-puis-nettoyer)

---

## 1. Ce qui change, en une phrase

L'ancienne intégration publie des **listes** que votre automatisation parcourt ;
Carnet scolaire publie le **fait** que vous cherchiez, dans l'état d'une entité.

C'est tout l'objet du projet, et c'est ce qui rend la migration payante plutôt
que pénible. Une automatisation qui commençait par

```jinja
{{ state_attr('sensor.…_today_s_timetable','lessons')
   | rejectattr('canceled') | selectattr('is_afternoon') | … }}
```

devient un déclencheur horaire sur `sensor.enfant_un_fin_des_cours`. Une
automatisation qui comparait l'ancien et le nouvel état d'un attribut pour
repérer une annulation devient un déclencheur `Un cours a été annulé`.

En pratique, la plupart des migrations **suppriment du Jinja**. Gardez cette
question en tête à chaque ligne de template que vous transposez : *est-ce qu'une
entité ne porte pas déjà ce fait ?* Le § 4 du guide de l'utilisateur est le
catalogue à consulter, et l'[annexe A](annexe-a-entites.md) donne l'origine
exacte de chaque donnée.

---

## 2. Faire l'inventaire avant de toucher à quoi que ce soit

Une entité n'est presque jamais utilisée à un seul endroit. Avant de réécrire,
listez **tous** les consommateurs de chaque ancienne entité — et pas seulement
vos automatisations.

| Où chercher | Comment |
| --- | --- |
| Automatisations et scripts | **Paramètres → Automatisations et scènes**, puis la recherche. Cherchez l'identifiant d'entité, pas son nom affiché. |
| Tableaux de bord | Ouvrez chaque tableau de bord en mode YAML (**⋮ → Modifier en YAML**) et cherchez l'identifiant. Pensez aux **badges** en haut de vue et aux cartes Markdown : ce ne sont pas des cartes ordinaires et une lecture rapide les rate. |
| Capteurs template | **Paramètres → Appareils et services → Aides**. Un capteur template qui lit une entité disparue devient `indisponible`, ce qui casse aussi tout ce qui l'utilisait — la panne se propage d'un cran. |
| Groupes, seuils, min/max | Même page. Ces aides mémorisent l'identifiant dans leur configuration ; renommer l'entité ne les met **pas** à jour. |

**Notez la liste.** Elle devient votre liste de contrôle au § 8, et c'est le
seul moyen de savoir que vous avez fini.

**Le cas des chaînes d'aides.** Si une de vos automatisations se déclenche sur un
capteur template que vous avez écrit vous-même — « fin des cours », « prochain
contrôle », « nombre de devoirs pour demain » —, regardez d'abord si Carnet scolaire
ne publie pas déjà ce capteur. Souvent oui, et l'aide disparaît complètement au
lieu d'être réécrite.

---

## 3. La correspondance des entités

Ce tableau couvre les entités les plus utilisées dans des automatisations. Il ne
prétend pas décrire exhaustivement l'ancienne intégration, dont le catalogue a
changé au fil de ses versions : prenez-le comme un point de départ, et vérifiez
chez vous.

| Ce que vous lisiez | Chez Carnet scolaire |
| --- | --- |
| L'attribut `lessons` de l'emploi du temps du jour | `sensor.enfant_un_cours_du_jour` — l'**état** est le nombre de cours, l'attribut `lessons` reste disponible pour les cartes |
| L'attribut `lessons` de l'emploi du temps du lendemain | `sensor.enfant_un_emploi_du_temps_de_demain` |
| Un template qui cherchait le prochain cours | `sensor.enfant_un_prochain_cours` — état = horodatage du début |
| Un template qui cherchait la fin de la journée | `sensor.enfant_un_fin_des_cours` — cours annulés et dispensés déjà exclus |
| Un template « y a-t-il cours aujourd'hui » | `binary_sensor.enfant_un_jour_de_classe` |
| Un template « l'enfant est-il en cours en ce moment » | `binary_sensor.enfant_un_en_cours` |
| Un template « un cours est-il annulé aujourd'hui » | `binary_sensor.enfant_un_cours_annules` |
| Un template comptant les devoirs à faire | `sensor.enfant_un_devoirs_a_faire` |
| Un template cherchant la dernière note | `sensor.enfant_un_derniere_note` — état **numérique** |
| Un template cherchant le prochain contrôle | `sensor.enfant_un_prochain_controle` — état = horodatage |
| Une comparaison d'états pour détecter un changement | Une entité `event` et les déclencheurs d'appareil — voir § 5.2 |

**Les identifiants dépendent de la langue de votre installation.** `enfant_un`
est un préfixe fictif, et `fin_des_cours` n'existe sous cette forme que sur une
installation en français. Utilisez toujours le sélecteur d'entité de l'interface
plutôt que de recopier un identifiant de ce tableau.

---

## 4. Le schéma d'un cours a changé

Si vous gardez un template qui parcourt `lessons` — c'est parfois inévitable
pour **composer un texte** —, les clés de chaque élément ne sont plus les mêmes.

| Ancienne clé | Chez Carnet scolaire | Remarque |
| --- | --- | --- |
| `lesson` | `subject` | |
| `start_time` (`"08:30"`) | `start` (ISO complet) | `{{ c.start \| as_datetime \| as_local }}` pour en faire une date |
| `end_time` (`"09:30"`) | `end` (ISO complet) | idem |
| `start_at` (date-heure) | `start` | une seule clé désormais, plus deux |
| `num` | `id` | et il ne sert plus à dédoublonner : voir § 5.2 |
| `canceled` | `canceled` | inchangé |
| `is_morning`, `is_afternoon` | **rien** | voir § 5.1 |
| — | `exempted` | nouveau : cours dont l'enfant est dispensé |
| — | `status` | nouveau : le libellé PRONOTE (« Prof. absent », « Cours dépl. ») |
| — | `end_inferred` | nouveau, et important : voir § 7 |

**Mettre une heure en forme.** Les horodatages étant maintenant ISO, l'astuce
`start_time | replace(':','h')` ne fonctionne plus. L'équivalent :

```jinja
{%- set d = c.start | as_datetime | as_local -%}
{{ '%02dh%02d' | format(d.hour, d.minute) }}
```

**Comparer à maintenant.** `as_datetime` rend une date **avec fuseau**, donc
comparable directement à `now()` sans conversion.

---

## 5. Ce qui n'a plus d'équivalent

### 5.1 `is_morning` et `is_afternoon`

Carnet scolaire ne marque pas les cours « matin » ou « après-midi ». La raison est
qu'il n'existe pas de définition juste à heure fixe : un collège qui finit la
matinée à 13 h et un lycée qui la finit à 12 h ne se découpent pas au même
endroit, et une heure codée en dur se trompe la moitié du temps.

Ce qui remplace le découpage, selon ce que vous cherchiez :

| Vous cherchiez | Utilisez |
| --- | --- |
| La fin de la journée | `sensor.enfant_un_fin_des_cours` |
| Le début de la journée | `sensor.enfant_un_prochain_cours`, ou l'attribut `first_start` de `cours_du_jour` |
| L'heure du réveil | `sensor.enfant_un_prochain_reveil` |
| La fin de la matinée, pour un retour du midi | Voir ci-dessous |

**La fin de la matinée.** C'est le seul cas qui demande encore du travail. La
règle qui donne un résultat juste n'est pas une heure fixe mais **la coupure
repas** : parmi les cours du jour non annulés et non dispensés, le plus long trou
qui commence entre 11 h et 14 h 30 et qui dure au moins 45 minutes ; l'heure
cherchée est la fin du cours qui précède ce trou.

Le point important est ce qu'il faut faire **quand ce trou n'existe pas** :
publier `inconnu`, et non une valeur de repli. Une journée continue ne rend pas
le fait faux, elle le rend inexistant. En particulier, une journée qui s'arrête à
midi ne doit **rien** produire ici — c'est `fin_des_cours` qui répond, et deux
entités portant le même horodatage déclencheraient deux automatisations pour un
seul retour à la maison.

**Un capteur natif est prévu** pour porter cette règle, avec en plus l'heure de
reprise et la durée de la coupure. D'ici là, écrivez-la dans un capteur
template. Vous pourrez alors le supprimer **sans retoucher votre
automatisation**, à condition d'avoir gardé l'identifiant de l'aide — c'est
exactement ce que dit le point 5 du § 7.

### 5.2 La déduplication par `num`

Beaucoup d'automatisations de l'ancienne intégration détectaient un changement en
comparant l'état précédent et le nouvel état d'un attribut, puis
dédoublonnaient par numéro de créneau pour ne pas annoncer deux fois la même
annulation.

**Tout cela disparaît.** Carnet scolaire fait la détection lui-même et émet un
évènement par changement, avec son contexte. Vous n'écrivez plus la comparaison,
et il n'y a plus rien à dédoublonner. Six changements sont distingués :
annulation, annulation levée, déplacement, changement de salle, de professeur, de
statut — là où une comparaison maison les confond volontiers.

Le § 9.2 du guide de l'utilisateur liste les seize déclencheurs disponibles,
et le § 4.10 les attributs que chacun porte.

**Le piège de l'annulation levée.** `lesson_restored` (un cours annulé qui est
rétabli) est un type **distinct** de `lesson_canceled`. Une détection maison qui
les confond fait déclencher « pas de cours en première heure, dors » le matin
même où le cours est rétabli. Si vous ne cochez pas « Annulation levée », ce cas
ne passe jamais.

---

## 6. Garder ses conditions tout en utilisant un blueprint

Le réflexe naturel est de renoncer aux blueprints livrés dès qu'on a des
conditions à soi — un détecteur de présence, un drapeau, une plage horaire.
C'est inutile : le champ **« Action de notification »** des blueprints n'attend
pas une notification, il attend une **séquence d'actions**. Vous pouvez y mettre
des conditions, des variables, des `si/alors` — tout ce qu'une automatisation
sait faire.

Une condition placée dans cette séquence **arrête la séquence** quand elle
n'est pas remplie. C'est ce qui permet d'ajouter vos propres filtres au blueprint
sans le modifier :

```yaml
# Contenu du champ « Action de notification » du blueprint
# « PRONOTE - Cours annulé ou modifié »
- condition: time
  after: "07:00:00"
  before: "21:30:00"
  note: Hors de cette plage, on ne parle pas dans le salon.

- condition: template
  value_template: >-
    {{ (trigger.event.data.attributes.end | as_datetime) > now() }}
  note: >-
    Le cours ne doit pas être terminé. Le test porte sur `end` et non sur
    `start` : une annulation publiée pendant le créneau lui-même est encore
    une nouvelle utile.

- if:
    - condition: state
      entity_id: binary_sensor.salon_presence
      state: "on"
  then:
    - action: script.annonce_salon
      data:
        message: >-
          Attention, un cours est annulé :
          {{ trigger.event.data.attributes.subject | title }}.
  note: >-
    Le test de présence est ICI, sur la seule annonce, et non dans les
    conditions de l'automatisation : sinon une pièce vide ferait perdre
    l'annonce ET l'armement du rappel ci-dessous.

- if:
    - condition: not
      conditions:
        - condition: state
          entity_id: person.enfant_un
          state: home
  then:
    - action: input_boolean.turn_on
      target:
        entity_id: input_boolean.rappel_en_attente
```

**Ce que vous gardez du blueprint** en faisant ça : les six déclencheurs déjà
câblés, le tri natif entre les types de changement, le mode `queued` correctement
dimensionné, et le fait que les déclencheurs d'appareil sont indépendants de la
langue de votre installation.

**Où placer une condition.** Dans les *conditions* de l'automatisation, elle
bloque **toute** la séquence. Dans la séquence, vous choisissez ce qu'elle
bloque. La différence compte dès qu'une de vos actions doit rester
inconditionnelle — armer un drapeau, incrémenter un compteur, écrire dans un
journal.

---

## 7. Cinq pièges rencontrés en vrai

Cette liste vient d'une migration réelle de quatre automatisations. Chacun de ces
points a produit un défaut observable.

**1. « Fin des cours » est la fin de la journée, pas la fin de l'après-midi.**
Si votre ancien capteur ne retenait que les cours d'après-midi, il ne publiait
rien les jours qui s'arrêtent à midi, et c'était votre alerte du midi qui
annonçait le retour. `sensor.enfant_un_fin_des_cours` publie ces jours-là. Si
vous branchez les deux annonces sans y penser, elles tombent **ensemble** à midi.
Le § 5.1 explique comment le découpage se répare : c'est la fin de matinée qui
doit se taire, pas la fin de journée.

**2. Un évènement par changement, pas un évènement agrégé.** Deux annulations
détectées dans la même collecte donnent deux déclenchements successifs. Vos
messages du type « 2 cours sont annulés : … » n'ont plus de source : réécrivez-les
au singulier. C'est délibéré — une automatisation qui veut notifier par cours
doit pouvoir le faire, et regrouper est trivial à refaire alors que séparer ne
l'est pas. Pensez à `mode: queued` avec un `max` suffisant, sinon la deuxième
annulation de la journée est silencieusement jetée.

**3. Filtrez sur `end`, pas sur `start`.** Un cours annulé compte **tant qu'il
n'est pas terminé**. Le symptôme, sinon : l'enfant rentre à 14 h 15 pendant un
cours annulé de 14 h – 15 h, et le rappel ne dit rien — alors qu'il est là
précisément à cause de cette annulation.

**4. Les heures de fin peuvent être des estimations.** Quand PRONOTE ne publie
pas l'heure de fin d'un cours, elle est **déduite**, et l'attribut
`end_inferred` vaut alors `true`. Sur certains établissements c'est le cas de
**tous** les cours. Les heures de début, elles, sont publiées et fiables.
N'appuyez donc rien de critique à la minute sur une heure de fin — une serrure,
un départ en voiture — sans regarder ce drapeau. Une annonce « il a fini, il
rentre » ne pose aucun problème.

**5. Modifier une aide ne change pas son identifiant.** Si vous gardez un
capteur template en le réécrivant pour le nouveau schéma, modifiez-le
(**Paramètres → Appareils et services → Aides**) au lieu de le supprimer et de le
recréer. Son identifiant d'entité est conservé, donc vos déclencheurs continuent
de fonctionner sans être retouchés. Le supprimer vous oblige à reprendre tout ce
qui le référence.

---

## 8. Vérifier, puis nettoyer

Dans cet ordre. Nettoyer avant d'avoir vérifié vous prive du point de
comparaison.

**Vérifier.**

1. Reprenez la liste du § 2 et cherchez chaque **ancien** identifiant. Vous
   devez n'obtenir aucun résultat. Attention aux faux positifs : un identifiant
   cité dans une *description* est de la documentation, pas une référence — mais
   il fera mentir vos recherches futures, donc retirez-le ou reformulez.
2. Ouvrez chaque automatisation réécrite : elle doit être **active**, et non
   `indisponible`. Une automatisation indisponible a une erreur de configuration.
3. Testez chaque template de message avant d'attendre l'évènement réel :
   **Outils de développement → Modèles**, collez la variable et regardez ce
   qu'elle rend aujourd'hui.
4. Pour les automatisations à déclencheur horaire, vérifiez que le capteur cible
   a bien une valeur (**Outils de développement → États**). Un déclencheur
   horaire sur un capteur sans valeur ne s'arme pas : il ne se déclenchera jamais
   et rien ne vous le dira.
5. Après le premier déclenchement réel, lisez la **trace** (⋮ → Traces) : elle
   montre quelle condition a bloqué, ce qu'aucun journal ne dit.

**Nettoyer.**

Une fois vos automatisations vérifiées sur plusieurs jours — dont un jour avec
une annulation, si vous pouvez l'attendre —, retirez l'ancienne intégration
(**Paramètres → Appareils et services**), puis les aides devenues inutiles.
Vérifiez à ce moment-là qu'aucune entité `indisponible` ne subsiste dans vos
tableaux de bord.

Le § 12 du [guide de l'utilisateur](GUIDE-UTILISATEUR.md) décrit ce qui part
tout seul et ce qui ne part pas.
