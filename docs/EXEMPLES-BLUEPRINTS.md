# Exemples d'utilisation des blueprints

Huit automatisations complètes, une par blueprint, à copier et à adapter.

Cette page ne remplace pas [Les huit blueprints, en détail](BLUEPRINTS.md),
qui explique chaque réglage et nomme le piège que chacun évite. Celle-ci sert à
autre chose : partir d'un cas concret et d'un bloc prêt à coller, plutôt que
d'un formulaire vide.

---

## Avant de copier quoi que ce soit

**Trois pièges d'appel**, qui ne se voient qu'une fois l'automatisation muette.

### Le chemin n'est pas celui du dépôt

Le dépôt range les blueprints dans `blueprints/automation/carnet_scolaire/fr/`. Ce
n'est **pas** le chemin à écrire. Quand Home Assistant importe un blueprint
depuis une URL GitHub, il le range sous le **nom du propriétaire du dépôt** :

```yaml
use_blueprint:
  path: FiveElements/wake_up_alarm.yaml     # ✅ ce que Home Assistant a réellement
  # path: carnet_scolaire/fr/wake_up_alarm.yaml  # ❌ ce que tout le monde suppose
```

Pour vérifier chez vous : **Paramètres → Automatisations et scènes → onglet
Blueprints**. Le chemin réel est celui affiché sous chaque modèle.

### Le français et l'anglais s'écrasent l'un l'autre

Les deux versions d'un même blueprint aboutissent au même chemin —
`FiveElements/absence_alert.yaml` — parce que seul le nom du fichier est
conservé, pas le dossier `fr/` ou `en/`. **Importer l'anglais après le français
remplace le français.** Les deux langues ne cohabitent pas sans renommer l'un
des deux fichiers à l'import.

### Les identifiants d'entité dépendent de la langue

Home Assistant fabrique l'identifiant d'une entité à partir de son nom
**traduit**. Les exemples ci-dessous sont écrits pour une installation en
**français** et un enfant nommé « Enfant Un » :

| Capteur | Sur une installation française | Sur une installation anglaise |
| --- | --- | --- |
| Prochain réveil | `sensor.enfant_un_prochain_reveil` | `sensor.enfant_un_next_wake_up` |
| Devoirs pour demain | `sensor.enfant_un_devoirs_pour_demain` | `sensor.enfant_un_homework_tomorrow` |
| Fin des cours | `sensor.enfant_un_fin_des_cours` | `sensor.enfant_un_end_of_lessons` |
| Menu du jour | `sensor.enfant_un_menu_du_jour` | `sensor.enfant_un_menu_today` |
| Cours du jour | `sensor.enfant_un_cours_du_jour` | `sensor.enfant_un_lessons_today` |
| Jour de classe | `binary_sensor.enfant_un_jour_de_classe` | `binary_sensor.enfant_un_school_day` |

**Remplacez `enfant_un` par le préfixe réel de votre enfant**, et n'inventez
pas le suffixe : lisez-le dans **Outils de développement → États**.

### L'identifiant d'appareil ne se devine pas

Quatre blueprints demandent l'**appareil** de l'élève et non un capteur, parce
qu'un déclencheur d'appareil est indépendant de la langue. Les exemples portent
donc un `child_device` factice :

```yaml
child_device: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4   # à remplacer
```

Pour trouver le vôtre : **Paramètres → Appareils et services → Carnet scolaire →**
l'appareil de l'enfant. L'identifiant est la dernière portion de l'URL de sa
page. Il est propre à votre installation : recopier celui d'un exemple ne
produit aucune erreur, juste une automatisation qui ne se déclenche jamais.

### Où coller ces blocs

**Paramètres → Automatisations et scènes → Créer une automatisation → Créer une
automatisation** puis, dans le menu **⋮** en haut à droite, **Modifier en
YAML**. Effacez le contenu et collez le bloc.

---

## Sommaire

- [1. Réveil adaptatif](#1-réveil-adaptatif)
- [2. Rappel des devoirs du lendemain](#2-rappel-des-devoirs-du-lendemain)
- [3. Cours annulé ou modifié](#3-cours-annulé-ou-modifié)
- [4. Absence, retard ou punition](#4-absence-retard-ou-punition)
- [5. Nouvelle note](#5-nouvelle-note)
- [6. Nouveau message ou information](#6-nouveau-message-ou-information)
- [7. Menu de la cantine](#7-menu-de-la-cantine)
- [8. Retour de l'école](#8-retour-de-lécole)
- [9. Retour du midi](#9-retour-du-midi)
- [10. Seulement les vraies heures de retour](#10-seulement-les-vraies-heures-de-retour)
- [Deux recettes qui ne sont pas des blueprints](#deux-recettes-qui-ne-sont-pas-des-blueprints)

---

## 1. Réveil adaptatif

**Le cas.** Le réveil doit sonner à la bonne heure sans que personne n'y pense
la veille — et se taire les jours sans cours, sans qu'on ait à maintenir un
calendrier de vacances.

```yaml
alias: Réveil scolaire d'Enfant Un
description: Sonne à l'heure calculée pour le premier cours du lendemain.
use_blueprint:
  path: FiveElements/wake_up_alarm.yaml
  input:
    wake_up_sensor: sensor.enfant_un_prochain_reveil
    extra_offset:
      hours: 0
      minutes: -10
      seconds: 0
    earliest_time: "06:00:00"
    latest_time: "09:00:00"
    wake_up_action:
      - action: light.turn_on
        target:
          entity_id: light.chambre_enfant_un
        data:
          brightness_pct: 30
          transition: 300
      - delay:
          minutes: 5
      - action: media_player.play_media
        target:
          entity_id: media_player.enceinte_chambre
        data:
          media_content_id: "https://icecast.example.invalid/radio.mp3"
          media_content_type: music
```

L'**écart supplémentaire** est ici négatif : il sonne dix minutes **avant**
l'heure calculée par l'intégration. Il ne remplace pas la « Marge de réveil »
des options, il s'y ajoute.

### Variante : réveil en douceur, puis annonce parlée

```yaml
    wake_up_action:
      - action: light.turn_on
        target:
          entity_id: light.chambre_enfant_un
        data:
          brightness_pct: 60
          transition: 600
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.enceinte_chambre
          message: "{{ message }}"
```

`message` vaut ici `Premier cours à 08:30 (MATHÉMATIQUES).`

### Variante : ne rien faire si personne n'est à la maison

Le champ « Action » accepte une **séquence entière**, conditions comprises. Une
condition qui échoue arrête la séquence — c'est ainsi qu'on ajoute un filtre
sans toucher au blueprint.

```yaml
    wake_up_action:
      - condition: state
        entity_id: person.enfant_un
        state: home
      - action: switch.turn_on
        target:
          entity_id: switch.radio_reveil
```

---

## 2. Rappel des devoirs du lendemain

**Le cas.** Un rappel le soir, mais uniquement s'il reste vraiment quelque
chose à faire — pas un message quotidien qu'on apprend à ignorer.

```yaml
alias: Devoirs d'Enfant Un pour demain
description: Rappelle à 19 h 30 les devoirs non cochés dus le lendemain.
use_blueprint:
  path: FiveElements/homework_reminder.yaml
  input:
    homework_tomorrow_sensor: sensor.enfant_un_devoirs_pour_demain
    reminder_time: "19:30:00"
    minimum_items: 0
    only_unfinished: true
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

`minimum_items: 0` veut dire « dès le premier devoir » : le seuil est
**strictement supérieur**, pas « ou égal ».

### Variante : sur l'enceinte de la cuisine, et seulement en semaine

```yaml
    notify_action:
      - condition: time
        weekday: [mon, tue, wed, thu, sun]
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.enceinte_cuisine
          message: "{{ message }}"
```

Le dimanche est inclus et le vendredi exclu : c'est la veille des jours de
classe qui compte, pas le jour de classe lui-même.

### Variante : rappeler deux fois, à deux endroits

```yaml
    notify_action:
      - action: notify.mobile_app_telephone_parent
        data:
          message: "{{ message }}"
      - action: notify.mobile_app_telephone_enfant
        data:
          message: "{{ message }}"
```

---

## 3. Cours annulé ou modifié

**Le cas.** Savoir tout de suite si le **premier ou le dernier** cours saute —
ce sont les seuls changements qui déplacent l'heure du trajet. Une salle
changée en milieu de journée n'intéresse personne.

```yaml
alias: Emploi du temps d'Enfant Un — bords de journée
description: Notifie une annulation ou un déplacement du premier ou du dernier cours.
use_blueprint:
  path: FiveElements/lesson_canceled.yaml
  input:
    child_device: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
    changes:
      - lesson_canceled
      - lesson_restored
      - lesson_moved
    only_day_edges: true
    lessons_today_sensor: sensor.enfant_un_cours_du_jour
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

**`lesson_restored` mérite d'être coché avec `lesson_canceled`.** Une
annulation levée est un évènement distinct, et l'oublier produit exactement la
panne qu'on redoute : « pas de premier cours, on dort plus tard » envoyé le
matin où le cours est finalement rétabli.

`only_day_edges` exige le capteur « Cours du jour » : c'est lui qui dit quels
créneaux sont les bords de la journée.

### Variante : tout suivre, mais discrètement

```yaml
    changes:
      - lesson_canceled
      - lesson_restored
      - lesson_moved
      - lesson_replaced
      - room_changed
      - teacher_changed
      - lesson_status_changed
    only_day_edges: false
    notify_action:
      - action: persistent_notification.create
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

Une notification persistante s'empile dans l'interface sans faire sonner un
téléphone — le bon compromis quand on veut tout voir sans être interrompu.

### Variante : être prévenu des remplacements et des changements de salle

Pour un remplacement, un « Changement de salle » ou un « Cours maintenu »,
PRONOTE envoie le cours d'origine marqué annulé **et** le cours qui le
remplace. L'intégration n'en fait **pas** une annulation — l'enfant a cours —
mais un `lesson_replaced`, qui ne passe que s'il est coché :

```yaml
    changes:
      - lesson_canceled
      - lesson_restored
      - lesson_replaced
      - room_changed
    only_day_edges: false
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

Le message devient « Mathématiques de 14:00 : remplacé par Eval maths en salle
informatique. » : le blueprint lit le cours de remplacement dans les attributs
`replacement_subject` et `replacement_classroom` de l'évènement.
`room_changed` couvre l'autre cas, celui d'un cours qui change de salle **sans**
être remplacé.

Sans `lesson_replaced` dans la liste, ces créneaux ne notifient rien du tout —
c'est le réglage par défaut, et c'est voulu : une annonce « cours annulé »
un matin où l'enfant a une évaluation dans une autre salle se lit comme un
professeur absent.

---

## 4. Absence, retard ou punition

**Le cas.** Être prévenu quand l'établissement saisit une absence, sans être
réveillé pour un retard de deux minutes.

```yaml
alias: Vie scolaire d'Enfant Un
description: Notifie une absence, ou un retard d'au moins dix minutes.
use_blueprint:
  path: FiveElements/absence_alert.yaml
  input:
    child_device: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
    events:
      - absence_added
      - delay_added
      - punishment_added
    minimum_delay_minutes: 10
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

Le seuil ne s'applique qu'aux **retards**. Une absence et une punition passent
toujours, quelle que soit sa valeur.

### Variante : les absences sur le téléphone, le reste dans le journal

```yaml
    notify_action:
      - if:
          - condition: template
            value_template: "{{ 'bsence' in message }}"
        then:
          - action: notify.mobile_app_telephone
            data:
              message: "{{ message }}"
        else:
          - action: logbook.log
            data:
              name: Vie scolaire
              message: "{{ message }}"
```

---

## 5. Nouvelle note

**Le cas.** Être prévenu d'une note en dessous de la moyenne, pas de toutes les
notes.

```yaml
alias: Notes d'Enfant Un sous la moyenne
description: Notifie à la publication d'une note strictement inférieure à 10/20.
use_blueprint:
  path: FiveElements/new_grade.yaml
  input:
    child_device: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
    threshold: 10
    notify_sentinels: false
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

**Le seuil est appliqué après remise à l'échelle sur 20.** Un 7/10 est comparé
comme un 14/20 et ne déclenche donc rien : c'est le comportement voulu, et
c'est aussi la raison pour laquelle il ne faut pas écrire ce filtre soi-même.

`notify_sentinels: false` écarte les notes sans valeur numérique — « Absent »,
« Non rendu », « Dispensé ». Passez-le à `true` pour être prévenu d'un devoir
non rendu, qui n'a pas de note mais mérite souvent plus d'attention qu'un 9.

### Variante : toutes les notes, avec la couleur de la matière

```yaml
    threshold: 20
    notify_sentinels: true
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          message: "{{ message }}"
```

`threshold: 20` laisse tout passer, le seuil étant strictement supérieur.

---

## 6. Nouveau message ou information

**Le cas.** Ne rater aucune information qui **demande une réponse** — un
sondage, une autorisation de sortie — sans lire les annonces générales.

```yaml
alias: Sondages et messages d'Enfant Un
description: Notifie les messages, et seulement les informations contenant un sondage.
use_blueprint:
  path: FiveElements/new_message.yaml
  input:
    child_device: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
    sources:
      - message_received
      - information_added
    surveys_only: true
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

`surveys_only` ne filtre que les **informations**. Les messages de la
messagerie passent tous : personne n'écrit à un parent pour ne rien demander.

### Variante : une alerte critique, parce qu'un sondage a une date limite

```yaml
    notify_action:
      - action: notify.mobile_app_telephone
        data:
          message: "{{ message }}"
          data:
            push:
              interruption-level: time-sensitive
```

---

## 7. Menu de la cantine

**Le cas.** Annoncer le menu à voix haute pendant le petit-déjeuner, et se
taire les jours où l'enfant ne déjeune pas au collège.

```yaml
alias: Menu de la cantine d'Enfant Un
description: Annonce le menu du jour à 7 h 15, les jours de classe seulement.
use_blueprint:
  path: FiveElements/canteen_menu.yaml
  input:
    menu_sensor: sensor.enfant_un_menu_du_jour
    school_day_sensor: binary_sensor.enfant_un_jour_de_classe
    announce_time: "07:15:00"
    announce_action:
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.enceinte_cuisine
          message: "{{ message }}"
```

Le capteur « Jour de classe » est **optionnel** et vaut une liste vide par
défaut ; une condition d'état sur une liste vide passe toujours. Le renseigner
ajoute un filtre, le laisser vide n'en retire aucun autre.

### Variante : l'afficher sur une tablette plutôt que le dire

```yaml
    announce_action:
      - action: persistent_notification.create
        data:
          notification_id: menu_du_jour
          title: "{{ title }}"
          message: "{{ message }}"
```

`notification_id` fixe fait que l'annonce du jour **remplace** celle de la
veille au lieu de s'empiler.

---

## 8. Retour de l'école

**Le cas.** Allumer le chauffage de la chambre et prévenir quand les cours
finissent — y compris, et surtout, les jours écourtés par une annulation.

```yaml
alias: Retour d'Enfant Un
description: Prévient et chauffe la chambre quinze minutes avant la fin des cours.
use_blueprint:
  path: FiveElements/end_of_day.yaml
  input:
    end_of_lessons_sensor: sensor.enfant_un_fin_des_cours
    offset:
      hours: 0
      minutes: -15
      seconds: 0
    only_when_shortened: false
    earliest_time: "11:00:00"
    latest_time: "20:00:00"
    arrival_action:
      - action: climate.set_temperature
        target:
          entity_id: climate.chambre_enfant_un
        data:
          temperature: 19
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

L'écart de −15 minutes fait agir **avant** la sonnerie, le temps que la chambre
chauffe.

### Variante : n'alerter que les jours écourtés

```yaml
    only_when_shortened: true
    offset:
      hours: 0
      minutes: 0
      seconds: 0
    arrival_action:
      - action: notify.mobile_app_telephone
        data:
          message: "{{ message }}"
```

C'est le seul réglage de cette page qui repose sur un test qu'on écrirait faux
soi-même. Il compte les cours **annulés** après l'heure de sortie
(`canceled_after`) au lieu de comparer la fin prévue à la fin réelle — une
**dispense** décale la seconde sans qu'aucun cours ne soit annulé, et la
comparaison annoncerait une annulation ce jour-là.

!!! warning "Avant d'y accrocher un portail"

    Les heures de **fin** ne sont pas toujours publiées par PRONOTE ; elles
    sont alors déduites de la durée du créneau, et sur certains établissements
    c'est le cas de **toutes**. L'attribut `end_inferred` le dit. Une
    notification supporte l'approximation ; l'ouverture d'un portail ou un
    départ en voiture, non.

---

## 9. Retour du midi

**Le cas.** Annoncer que l'enfant rentre déjeuner, à l'heure où sa matinée se
termine vraiment — y compris quand le dernier cours du matin saute parce que
le professeur est absent.

```yaml
alias: Retour du midi d'Enfant Un
description: Annonce dans le salon la fin de la matinée et l'heure de reprise.
use_blueprint:
  path: FiveElements/midday_return.yaml
  input:
    morning_end_sensor: sensor.enfant_un_fin_de_matinee
    include_cancellations: true
    midday_action:
      - action: notify.mobile_app_telephone
        data:
          title: "{{ title }}"
          message: "{{ message }}"
```

Le capteur « Fin de matinée » reconnaît une pause du midi avancée par une
annulation : l'action part à 10h30 le jour où le cours de 10h30 est annulé, et
le message dit combien de cours ont sauté et à quelle heure les cours
reprennent.

### Variante : l'enfant reste en permanence

```yaml
    include_cancellations: false
```

L'action part alors à l'heure **prévue** par la grille (`scheduled_end`),
annulations ignorées. Un jour où la grille ne prévoyait aucune pause du midi
et où seules des annulations en ont ouvert une, rien ne se passe : l'enfant
qui reste en permanence n'a pas de retour du midi ce jour-là. L'attente se
fait dans l'exécution de l'automatisation, à partir de la fin réelle ; un
redémarrage de Home Assistant pendant cette attente la perd.

Les jours sans pause du midi — journée continue, journée qui s'arrête à midi,
week-end, vacances — le capteur n'a pas de valeur et rien ne s'arme. Une
journée qui s'arrête à midi relève du blueprint « Retour de l'école ».

---

## 10. Seulement les vraies heures de retour

**Le cas.** Ne pas entendre parler de chaque annulation ou changement de salle,
mais être prévenu **une fois**, à l'heure réelle, des deux moments où l'enfant
sort : la fin de la matinée quand il rentre déjeuner, et la fin de la journée.
Une annulation qui ne change aucune de ces deux heures — un cours de 10h30
annulé entre deux autres — ne produit rien ; une annulation qui les avance —
le dernier cours du matin ou de l'après-midi qui saute — déplace l'annonce, et
le message le dit.

La recette tient en deux automatisations, sans le blueprint « Cours annulé ou
modifié » :

```yaml
# 1. Fin de la journée, à l'heure réelle.
alias: Fin des cours d'Enfant Un
description: Annonce la vraie fin de journée, annulations comprises.
use_blueprint:
  path: FiveElements/end_of_day.yaml
  input:
    end_of_lessons_sensor: sensor.enfant_un_fin_des_cours
    offset:
      hours: 0
      minutes: 0
      seconds: 0
    only_when_shortened: false
    earliest_time: "11:00:00"
    latest_time: "20:00:00"
    arrival_action:
      - condition: not
        conditions:
          - condition: state
            entity_id: person.enfant_un
            state: home
      - action: tts.speak
        target:
          entity_id: tts.google_translate_fr_fr
        data:
          media_player_entity_id: media_player.salon
          message: "{{ message }}"
```

```yaml
# 2. Retour du midi, à l'heure réelle.
alias: Retour du midi d'Enfant Un
description: Annonce la vraie fin de matinée, annulations comprises.
use_blueprint:
  path: FiveElements/midday_return.yaml
  input:
    morning_end_sensor: sensor.enfant_un_fin_de_matinee
    include_cancellations: true
    midday_action:
      - condition: not
        conditions:
          - condition: state
            entity_id: person.enfant_un
            state: home
      - action: tts.speak
        target:
          entity_id: tts.google_translate_fr_fr
        data:
          media_player_entity_id: media_player.salon
          message: "{{ message }}"
```

**Pourquoi c'est « une seule fois ».** Les deux blueprints se déclenchent sur
l'**heure** portée par un capteur (« Fin des cours », « Fin de matinée »), pas
sur un évènement de changement. Une annulation publiée dans la journée recule ou
avance cette heure, et le déclencheur se reprogramme seul ; il ne part qu'une
fois, quand l'heure arrive. Dix collectes qui revoient la même annulation ne
changent pas l'heure et ne produisent rien.

**Pourquoi c'est « la vraie heure ».** Les deux capteurs sont calculés sur ce
que l'enfant a réellement : cours annulés et dispenses exclus, et un créneau
remplacé — changement de salle, cours maintenu — compté comme un cours, puisque
l'enfant y est. Un jour où la dernière heure de l'après-midi est une évaluation
à la place d'un cours annulé, la fin de journée ne bouge pas.

**Ce qu'on retire.** Si une automatisation « Cours annulé ou modifié » existe
déjà, désactivez-la plutôt que de la supprimer : elle se réactive en un clic le
jour où l'on veut à nouveau le détail. Les jours sans pause du midi, le
capteur « Fin de matinée » n'a pas de valeur et seule l'annonce du soir part.

---

## Deux recettes qui ne sont pas des blueprints

Certaines choses ne méritent pas un modèle. Les voici en entier.

### Prévenir quand les collectes s'arrêtent

C'est la seule panne qui ne se voit pas : quand une collecte échoue, les
entités **gardent leur dernière valeur** et rien n'a l'air cassé.

```yaml
alias: Collectes PRONOTE interrompues
triggers:
  - trigger: state
    entity_id: sensor.mon_etablissement_etat_du_limiteur
    from: nominal
    for: "01:00:00"
conditions: []
actions:
  - action: notify.mobile_app_telephone
    data:
      message: >-
        Collectes PRONOTE interrompues :
        {{ states('sensor.mon_etablissement_etat_du_limiteur') }}
mode: single
```

**`credentials_hold` mérite une alerte immédiate**, sans le délai d'une heure :
il signale des identifiants refusés à répétition, ce qui expose à une
suspension d'adresse IP par PRONOTE.

```yaml
alias: PRONOTE — connexions suspendues
triggers:
  - trigger: state
    entity_id: sensor.mon_etablissement_etat_du_limiteur
    to: credentials_hold
conditions: []
actions:
  - action: notify.mobile_app_telephone
    data:
      title: PRONOTE
      message: >-
        Connexions suspendues : identifiants refusés à répétition.
        Ne ressaisissez pas le mot de passe avant d'avoir vérifié.
mode: single
```

**Comparez l'état brut, jamais le libellé traduit.** Le déclencheur ci-dessus
teste `credentials_hold` et non « Connexions suspendues » : un déclencheur écrit
sur le libellé ne se déclenche jamais, sans erreur ni avertissement.

### Ne rien faire pendant les vacances

Aucun blueprint n'en a besoin — c'est déjà gratuit dans les huit, parce qu'un
capteur sans valeur n'arme pas un déclencheur horaire. Mais pour vos propres
automatisations :

```yaml
conditions:
  - condition: state
    entity_id: binary_sensor.enfant_un_jour_de_classe
    state: "on"
```

---

## Pour aller plus loin

| Document | Pour qui |
| --- | --- |
| [Les huit blueprints, en détail](BLUEPRINTS.md) | Chaque réglage, et le piège que chacun évite |
| [§ 9 du guide](GUIDE-UTILISATEUR.md#9-automatisations) | Écrire une automatisation sans blueprint |
| [Migrer ses automatisations](MIGRATION-AUTOMATISATIONS.md) | Vous arrivez de l'intégration `pronote` |
| [Annexe A](annexe-a-entites.md) | L'origine exacte de chaque donnée, et les attributs de chaque évènement |
