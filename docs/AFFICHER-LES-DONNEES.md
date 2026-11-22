# Afficher les données

Le § 4 du [guide de l'utilisateur](GUIDE-UTILISATEUR.md#4-catalogue-des-entités)
catalogue ce que l'intégration publie. Ce document explique comment le
**montrer** dans un tableau de bord.

Il y a deux chemins, et ils ne s'excluent pas.

---

## Sommaire

- [Deux chemins](#deux-chemins)
- [La bibliothèque de cartes Carnet scolaire](#la-bibliothèque-de-cartes-carnet-scolaire)
- [Avec les cartes intégrées de Home Assistant](#avec-les-cartes-intégrées-de-home-assistant)
- [Une valeur périmée a l'air actuelle](#une-valeur-périmée-a-lair-actuelle)
- [Ce qu'aucune carte ne montrera](#ce-quaucune-carte-ne-montrera)

---

## Deux chemins

| | Bibliothèque de cartes | Cartes intégrées |
| --- | --- | --- |
| Installation | HACS, dépôt personnalisé | rien à installer |
| Configuration | l'appareil de l'enfant, et c'est tout | l'entité, carte par carte |
| Emploi du temps lisible | oui, créneau en cours surligné | non, une liste de texte au mieux |
| Suit un renommage de l'enfant | oui | non, les identifiants changent |
| Dépendance externe | une, à maintenir | aucune |

Si vous voulez un tableau de bord scolaire présentable en dix minutes, prenez
la bibliothèque. Si vous ne voulez aucune dépendance tierce, ou si vous
n'affichez que deux ou trois faits au milieu d'un tableau de bord de maison,
les cartes intégrées suffisent.

---

## La bibliothèque de cartes Carnet scolaire

Dix cartes Lovelace faites pour cette intégration, dans un dépôt séparé :
**[FiveElements/ha-carnet-scolaire-cards](https://github.com/FiveElements/ha-carnet-scolaire-cards)**
— [documentation](https://fiveelements.github.io/ha-carnet-scolaire-cards/), une
page par carte.

Élève · Prochain cours · Vue journée · Emploi du temps · Devoirs · Notes ·
Évaluations · Cantine · Vie scolaire · Limiteur.

**Ce qu'il faut savoir avant d'installer.** Elles se configurent avec
**l'appareil de l'enfant**, jamais avec un identifiant d'entité — y compris la
carte « limiteur », qui remonte toute seule jusqu'à l'appareil du compte. C'est
délibéré, et c'est ce qui les rend insensibles à un renommage de l'enfant : elles
retrouvent chaque entité par sa clé technique stable, pas par son identifiant.
Le dépôt est un dépôt personnalisé HACS de catégorie **Lovelace**, et il demande
Home Assistant 2026.9.0 comme l'intégration.

L'installation, l'ajout d'une carte et le cas d'un tableau de bord en mode YAML
sont décrits chez eux ; il n'y a pas de raison de le redire ici.

---

## Avec les cartes intégrées de Home Assistant

Tout ce qui suit fonctionne sans rien installer. Les identifiants sont fictifs :
`enfant_un` remplace le préfixe de votre enfant, et il **dépend de la langue**
de votre installation — utilisez le sélecteur de l'éditeur plutôt que de
recopier.

### Un fait, une tuile

Les capteurs dont l'état est un horodatage ou un nombre sont faits pour la carte
**Tuile**. Un horodatage s'y affiche en temps relatif — « dans 25 minutes » —
ce qui est exactement ce qu'on veut lire.

```yaml
type: tile
entity: sensor.enfant_un_prochain_cours
```

Les meilleures candidates : « Prochain cours », « Fin des cours », « Prochain
réveil », « Prochain contrôle », « Devoirs à faire », « Moyenne générale ».

C'est le § 2.1 de la spécification qui rend cela possible : chaque fait qu'une
automatisation peut vouloir a **son état à lui**, un horodatage ou un nombre, et
jamais du texte. Une tuile n'a donc rien à mettre en forme.

### Les trois agendas

Sous-utilisés, et c'est dommage : l'intégration publie trois entités `calendar`
que la carte **Agenda** intégrée affiche telles quelles.

```yaml
type: calendar
entities:
  - calendar.enfant_un_emploi_du_temps
  - calendar.enfant_un_devoirs
  - calendar.enfant_un_punitions
```

C'est le moyen le plus court d'obtenir une semaine lisible sans aucune carte
tierce. Le détail de cette carte — chaque option, ce que contient chacun des
trois agendas, et **jusqu'où il sait** — est sur
[la page qui lui est consacrée](https://fiveelements.github.io/ha-carnet-scolaire-cards/ha-calendar/). Un cours **annulé** y reste, avec son statut en description et le mot
« annulé » dans le résumé : le retirer donnerait l'illusion qu'il n'a jamais
existé.

### Les devoirs comme liste de tâches

`todo.enfant_un_devoirs` est une vraie liste de tâches, donc la carte **Liste de
tâches** intégrée la montre, un devoir par ligne, avec l'échéance.

```yaml
type: todo-list
entity: todo.enfant_un_devoirs
```

**Cocher une case écrit dans PRONOTE.** La carte n'est donc modifiable que si
vous avez activé l'écriture (§ 7 du guide). Sinon, elle est en lecture seule —
par ses fonctions déclarées, pas en échouant quand on tape dessus.

**Et sans l'écriture, rien n'indique qu'un devoir est fait.** L'attribut `done`
existe sur chaque élément, mais une liste de tâches en lecture seule ne dessine
pas de case cochée : l'information est là et n'est pas montrée. Si vous voulez la
voir sans activer l'écriture, l'agenda **Devoirs** préfixe d'une coche les
devoirs faits, et une carte Markdown peut lire `done` directement.

### Les actualités et la messagerie : aucune carte, et c'est structurel

**Aucune carte, ni intégrée ni de la bibliothèque, ne montre les actualités ou
les discussions.** Ce n'est pas un oubli de configuration : les quatre entités
concernées — `sensor.<é>_actualites`, `_actualites_non_lues`,
`sensor.<é>_discussions`, `_messages_non_lus` — ne sont lues par aucune des neuf
cartes, et les cartes intégrées ne savent pas parcourir un attribut. C'est donc
la famille où un modèle est le plus nécessaire, et la seule où il n'y a aucune
solution de repli.

Ce que vous pouvez faire, du plus simple au plus complet :

**Deux badges**, qui répondent à « y a-t-il du nouveau » sans rien encombrer :

```yaml
type: entities
entities:
  - entity: sensor.enfant_un_actualites_non_lues
  - entity: sensor.enfant_un_messages_non_lus
```

`_messages_non_lus` est la **somme** des messages non lus de toutes les
discussions, et non le nombre de discussions concernées — un 7 peut venir d'un
seul fil.

**La liste, en Markdown**, puisque c'est le seul chemin :

```yaml
type: markdown
content: >-
  {% for a in state_attr('sensor.enfant_un_actualites', 'items')
       | rejectattr('read') %}
  - **{{ a.title }}** — {{ a.author }}
  {% endfor %}
```

**Ce qu'aucun affichage ne vous donnera : le texte.** Le corps d'une actualité et
celui d'un message ne sont pas récupérés — les lire coûterait une requête par
élément et par collecte. Les entités disent qu'il y a du nouveau, de qui et sur
quel sujet ; le contenu se lit dans PRONOTE. Une carte qui promettrait de
l'afficher promettrait une donnée qui n'existe pas côté Home Assistant.

Pour être **prévenu** plutôt que d'aller voir, ces deux familles ont leurs
déclencheurs d'appareil — « nouvelle actualité » et « nouveau message » — et
c'est le bon outil : voyez [Les neuf blueprints](BLUEPRINTS.md).

### Les listes en Markdown

Les listes — cours du jour, devoirs, notes, plats du menu — vivent dans des
**attributs**, pas dans des états. Aucune carte intégrée ne sait parcourir un
attribut : c'est le seul endroit où un template est inévitable, et la carte
**Markdown** est faite pour ça.

```yaml
type: markdown
content: |
  {% for c in state_attr('sensor.enfant_un_cours_du_jour', 'lessons') %}
  - **{{ (c.start | as_datetime | as_local).strftime('%H:%M') }}**
    {{ c.subject }}{{ ' — ANNULÉ' if c.canceled else '' }}
  {%- endfor %}
```

Le § 4 du guide donne les attributs de chaque capteur. Deux remarques :

- Les horodatages sont ISO et **avec fuseau** : `| as_datetime | as_local` avant
  d'en faire quoi que ce soit.
- Un cours porte `canceled`, `exempted`, `status`, `test`, `outing` — de quoi
  distinguer visuellement bien plus qu'un cours normal d'un cours annulé.

### Les badges du jour

Les capteurs binaires font de bons badges en haut de vue : d'un coup d'œil, sans
lire une ligne.

```yaml
badges:
  - type: entity
    entity: binary_sensor.enfant_un_jour_de_classe
  - type: entity
    entity: binary_sensor.enfant_un_cours_annules
  - type: entity
    entity: binary_sensor.enfant_un_devoirs_en_retard
  - type: entity
    entity: binary_sensor.enfant_un_controle_prevu
```

« Cours annulés », « Devoirs en retard », « Absence en cours » et « Punition à
venir » portent la classe `problem` : Home Assistant les colore tout seul quand
ils s'allument.

### Masquer le scolaire pendant les vacances

Un tableau de bord scolaire affiché en plein mois d'août n'informe personne. La
carte **Conditionnelle** intégrée le règle sans template :

```yaml
type: conditional
conditions:
  - condition: state
    entity: binary_sensor.enfant_un_vacances
    state: "off"
card:
  type: entities
  entities:
    - sensor.enfant_un_prochain_cours
    - sensor.enfant_un_devoirs_a_faire
```

### La photo, si elle existe

`image.enfant_un_photo` n'est créée **que si PRONOTE détient une photo** pour
cet enfant. Beaucoup d'établissements n'en publient pas, et l'entité est alors
tout simplement absente — ce n'est pas une panne. Quand elle existe, la carte
**Image** l'affiche.

**Une carte Image vide n'est pas non plus une panne.** L'entité peut exister et
ne rien rendre : la photo est cherchée **une seule fois** par rechargement de
l'intégration, et si l'adresse fournie par le serveur est injoignable — cela
arrive — l'entité reste vide jusqu'au prochain rechargement. Une seule tentative
est délibéré : réessayer à chaque affichage du tableau de bord dépenserait le
budget quotidien sur une image qui n'existe pas. Si la photo vous manque,
rechargez l'intégration (§ 10.4 du guide) ; ne cherchez pas un réglage, il n'y en
a pas. C'est aussi pourquoi cette entité reste *disponible* dès que PRONOTE
annonce une photo, qu'elle finisse par en rendre une ou non.

---

## Une valeur périmée a l'air actuelle

C'est le piège d'affichage propre à cette intégration, et il vient d'un choix
délibéré : quand une collecte échoue ou est reportée, les entités **gardent leur
dernière valeur** au lieu de devenir indisponibles (§ 4 du guide). Une entité qui
clignote en « indisponible » déclencherait des automatisations à tort — mais sur
un tableau de bord, un emploi du temps de mardi affiché le jeudi a l'air d'être
celui de jeudi.

Chaque entité porte donc deux attributs pour le dire :

| Attribut | Contenu |
| --- | --- |
| `fetched_at` | l'heure de la dernière collecte réussie |
| `stale` | vrai quand la valeur affichée est jugée trop vieille |

Un bandeau qui n'apparaît que dans ce cas, sans rien encombrer le reste du
temps :

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.enfant_un_cours_du_jour
    attribute: stale
    state: true
card:
  type: markdown
  content: >-
    ⚠️ Données PRONOTE figées depuis
    {{ state_attr('sensor.enfant_un_cours_du_jour', 'fetched_at')
       | as_datetime | as_local | relative_time }}.
```

Si ce bandeau reste allumé, ce n'est pas un problème d'affichage : voyez
[Si rien n'arrive jamais](BLUEPRINTS.md#si-rien-narrive-jamais), et notamment
l'automatisation de surveillance du limiteur.

### Vieux et mort ne se lisent pas de la même façon

Le bandeau ci-dessus ne couvre **qu'un** des deux cas, et l'autre est celui où
il ne fonctionne pas. Une entité **périmée** appartient à une intégration
vivante dont la dernière collecte a échoué. Une entité **restaurée** n'appartient
plus à rien : elle survit dans le registre après la disparition de ce qui
l'alimentait — un enfant qui n'est plus suivi, un appareil dédoublé (§ 10.5 du
guide), une catégorie désactivée dans les options.

| | Collecte échouée | Entité restaurée |
| --- | --- | --- |
| L'intégration l'alimente encore | oui | **non** |
| État | la dernière valeur, conservée | **`unavailable`** |
| `stale` | `true` | **absent** |
| `fetched_at` | présent, ancien | **absent** |
| `restored` | absent | **présent** |
| Durée | passager | **définitif** |

**Une entité restaurée perd `stale` et `fetched_at`**, parce que le code qui les
calculait n'est plus là. Le bandeau ci-dessus ne s'allumera donc **jamais**
dessus : `state_attr(..., 'stale')` rend `None`, la condition est fausse, et il
ne reste que `restored`, `state_class`, `friendly_name` et les capacités. Le
mécanisme d'observation s'éteint exactement dans le cas où l'on en aurait le plus
besoin.

Le piège n'est pas qu'une entité morte ait l'air saine — elle est
`unavailable`, donc visiblement dégradée. Le piège est qu'elle ait l'air
**passagère** : sur un tableau de bord, « indisponible » se lit comme un creux
de collecte qui va se résorber, et une entité morte ne se résorbera jamais.

Retenez une ligne : **`stale` répond à « est-ce vieux ? », `restored` à « est-ce
mort ? », et la seconde question ne se pose jamais à la première.** Pour
distinguer les deux sur un tableau de bord :

```yaml
type: conditional
conditions:
  - condition: template
    value_template: >-
      {{ state_attr('sensor.enfant_un_cours_du_jour', 'restored') is not none }}
card:
  type: markdown
  content: >-
    ⛔ Cette entité n'est plus alimentée par l'intégration. Elle ne reviendra
    pas d'elle-même — voyez le § 10.5 du guide.
```

---

## Ce qu'aucune carte ne montrera

Trois catégories, pour trois raisons différentes.

**Ce qui n'est dans aucun état, délibérément.** L'URL iCal, le bloc d'identité
et le lien du PDF d'emploi du temps ne sont **pas** des attributs : ce sont des
réponses de services, et rien ne les conserve. Une URL iCal donne accès à
l'emploi du temps complet d'un enfant sans aucun identifiant — c'est un mot de
passe, et un mot de passe n'a rien à faire dans un état d'entité que n'importe
quel utilisateur de Home Assistant peut lire. Ces trois données s'obtiennent par
un appel de service, décrit au § 5.1 du guide.

**La couleur de matière est publiée.** `background_color` est
un attribut des créneaux de la liste `lessons`, des devoirs, des éléments de
« Notes » et des moyennes par matière : la couleur que l'établissement associe à la matière, **telle que le
serveur l'envoie**, ou `null`. La clé est toujours présente, seule sa valeur peut
manquer, et aucune couleur n'est inventée — ni défaut, ni dérivation depuis le
nom de la matière. L'annexe A en donne la règle complète.

Deux réserves, parce qu'elles décident de ce qu'une carte peut afficher :

- **Quatre endroits ne la portent pas.** Les capteurs « Prochain cours » et
  « Dernière note » composent leurs attributs un à un et ne la reprennent pas ;
  les matières du bulletin non plus,
  bien que le DTO la porte ; et les évaluations n'ont pas de couleur du tout,
  le champ étant absent en amont. Sur ceux-là, une table de correspondance
  écrite côté carte reste le seul moyen d'obtenir un accent — et elle doit
  prendre ses valeurs dans les variables de thème plutôt que de les écrire en
  dur, sinon elle casse en thème sombre.
- **Sur le bulletin, le champ change d'orthographe en amont.** Le serveur
  l'envoie sous `couleur` en minuscules et non `CouleurFond` — même sémantique,
  orthographe différente.

!!! warning "Une table de compensation muette ne le dit pas"

    Une table de couleurs par matière écrite côté carte ne sert que là où le
    serveur ne fournit pas de couleur. Une carte qui consulte d'abord
    `background_color` ne retombe sur votre table que pour les matières que
    l'établissement ne colore pas — souvent, en pratique, pour aucune. **Le bloc
    reste alors dans votre YAML sans rien faire**, sans erreur et sans
    avertissement.

    C'est le même mode de panne qu'un `entity_id` inconnu dans un déclencheur,
    que Home Assistant accepte sans broncher et qui ne se déclenche jamais : un
    YAML silencieusement inopérant. Gardez la table pour les quatre endroits
    ci-dessus, où elle sert.

**Ce que PRONOTE envoie et que l'intégration ne republie pas.** Deux données
sont décodées puis retenues, et il est plus utile de le savoir que de les
chercher :

- **Les libellés d'un plat de cantine**, et c'est le cas le plus lourd des
  deux. PRONOTE envoie pour chaque plat une liste de libellés portant un nom et
  une couleur — c'est là qu'apparaîtraient un régime ou un allergène. Ils sont
  décodés par la bibliothèque, puis **abandonnés** : l'intégration ne garde que
  le nom du plat, donc les six listes de plats sont des listes de chaînes. Rien
  ne permet aujourd'hui d'afficher un régime à côté d'un plat, et l'absence a
  des conséquences hors écran.
- **Le volume horaire d'une absence.** `sensor.<é>_absences` porte un **nombre
  d'absences**, sans unité, et non un nombre d'heures. Le volume n'existe que
  dans les éléments de `items`, sous la forme d'une **chaîne** écrite par
  l'établissement (« 2h00 »), donc ni sommable ni comparable. « Combien d'heures
  de cours manquées ce trimestre » n'est pas affichable en l'état ; la seule
  durée calculable est l'écart entre `from_date` et `to_date`.

**Ce qui n'a pas d'historique.** Les attributs qui portent des listes ne sont
pas enregistrés en base : les listes que renvoie PRONOTE dépassent la taille
qu'un attribut peut avoir dans l'historique. Vous pouvez donc grapher le
**nombre** de devoirs sur un mois, mais pas retrouver « la liste des devoirs de
mardi dernier ». L'état — un décompte, une date, une note — est historisé
normalement.

---

## Pour aller plus loin

| Document | Pour qui |
| --- | --- |
| [Documentation des cartes](https://fiveelements.github.io/ha-carnet-scolaire-cards/) | Les neuf cartes, réglage par réglage |
| [§ 4 du guide](GUIDE-UTILISATEUR.md#4-catalogue-des-entités) | Le catalogue des entités et de leurs attributs |
| [§ 5.1 du guide](GUIDE-UTILISATEUR.md#51-les-services-qui-renvoient-une-réponse) | Les services à réponse, pour l'iCal et le PDF |
| [Les neuf blueprints](BLUEPRINTS.md) | Automatiser, plutôt qu'afficher |
| [Exemples d'utilisation](EXEMPLES-BLUEPRINTS.md) | Une automatisation complète par blueprint |
