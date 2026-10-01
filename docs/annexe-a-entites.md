# Annexe A — catalogue des entités et services

Référence de [`SPECIFICATION.md`](SPECIFICATION.md). Chaque ligne indique
l'entité, son état, ses attributs, le palier qui l'alimente et l'origine de la
donnée dans `pronotepy` 2.15.7.

Conventions de lecture :

- `<é>` remplace le préfixe de l'appareil de l'enfant. Toutes les entités
  portent `_attr_has_entity_name = True` et un `translation_key` ; le nom
  affiché est traduit, l'identifiant est stable (§2.4 de la spécification).
- **P** = palier de collecte, tel que défini au §5.2.
- Un attribut en **gras** est déclaré dans `_unrecorded_attributes`.
- « — » signifie que la donnée n'a pas d'équivalent direct dans `pronotepy` :
  elle est calculée par l'intégration.

---

## 1. Capteurs primitifs — la surface d'automatisation

Ce sont les entités que le §2.1 de la spécification rend obligatoires : un état
scalaire, directement utilisable par un déclencheur `state` ou
`numeric_state`, sans template.

| Entité | État | `device_class` | Attributs | P | Origine |
| --- | --- | --- | --- | --- | --- |
| `sensor.<é>_prochain_cours` | horodatage du début | `timestamp` | `subject`, `teachers`, `classroom`, `end`, `canceled` | `timetable` | `Lesson.start` |
| `sensor.<é>_fin_des_cours` | horodatage de la fin du dernier cours du jour | `timestamp` | `subject` | `timetable` | `Lesson.end` |
| `sensor.<é>_fin_de_matinee` | horodatage de la fin du dernier cours avant la pause de midi | `timestamp` | `subject`, `end_inferred`, `resumes_at`, `break_minutes` | `timetable` | `Lesson.end` |
| `sensor.<é>_prochaine_annulation` | horodatage du début du prochain cours annulé | `timestamp` | `subject`, `end`, `classroom`, `end_inferred`, **`items`** | `timetable` | `Lesson.canceled` |
| `sensor.<é>_prochain_reveil` | horodatage du réveil calculé | `timestamp` | `first_lesson`, `margin_minutes` | `timetable` | — (premier cours − `wake_margin`) |
| `sensor.<é>_cours_du_jour` | nombre de cours | — | **`lessons`**, `first_start`, `last_end`, `canceled_count` | `timetable` | `Client.lessons()` |
| `sensor.<é>_devoirs_a_faire` | nombre de devoirs non faits | — | **`items`**, `next_due` | `homework` | `Homework.done` |
| `sensor.<é>_devoirs_pour_demain` | nombre de devoirs pour le lendemain | — | **`items`** | `homework` | `Homework.date` |
| `sensor.<é>_derniere_note` | valeur numérique de la note la plus récente | — | `subject`, `out_of`, `coefficient`, `date`, `class_average`, `status` | `marks` | `Grade.grade` |
| `sensor.<é>_moyenne_generale` | moyenne générale de l'élève | — | `out_of` (**constante 20**), `period` | `marks` | `Period.overall_average` |
| `sensor.<é>_moyenne_de_la_classe` | moyenne générale de la classe | — | `out_of` (**constante 20**), `period` | `marks` | `Period.class_overall_average` |
| `sensor.<é>_prochain_controle` | horodatage du prochain cours marqué contrôle | `timestamp` | `subject`, `classroom` | `timetable` | `Lesson.test` |
| `sensor.<é>_prochaine_punition` | horodatage du prochain créneau de retenue | `timestamp` | `nature`, `duration`, `giver` | `attendance` | `Punishment.schedule` |
| `sensor.<é>_absences_non_justifiees` | nombre | — | **`items`** | `attendance` | `Absence.justified` |
| `sensor.<é>_actualites_non_lues` | nombre | — | **`items`** | `news` | `Information.read` |
| `sensor.<é>_messages_non_lus` | **somme** des non-lus | — | **`items`** | `discussions` | `sum(Discussion.unread)` |
| `sensor.<é>_periode_en_cours` | nom de la période | — | `start`, `end`, `index` | `session` | `Client.current_period` |

**Exigence.** `sensor.<é>_messages_non_lus` vaut `sum(d.unread for d in
discussions)`. `Discussion.unread` est un **entier** (`nbNonLus`), pas un
booléen : compter les discussions ayant au moins un message non lu donnerait un
nombre différent, et le capteur contredirait son propre attribut `items`.

**Exigence.** `sensor.<é>_prochain_controle` est ce qui rend possible
« réviser la veille au soir ». Un capteur binaire `controle_prevu` ne répond
qu'à « aujourd'hui » ; sans horodatage, l'automatisation la plus demandée
retombe sur un template parcourant `attributes.lessons`, c'est-à-dire l'échec
exact que le §1 de la spécification se donne pour critère.

**Exigence.** `sensor.<é>_periode_en_cours` devient **indisponible** plutôt que
faux quand `current_period` ne peut pas être déterminée : `pronotepy` retombe sur
`onglets[0]` si l'onglet 198 est absent, ce qui désigne silencieusement la
mauvaise période dans un établissement qui ne publie pas les notes.

**Exigence.** `sensor.<é>_derniere_note` a un état **numérique**. Quand la note
la plus récente est une sentinelle (`|1` à `|8`), l'état vaut `unknown` et
l'attribut `status` porte le motif. Un état qui vaudrait tantôt `14.5` tantôt
`Absent` ne serait exploitable ni par un seuil ni par un graphique (§4.3).

**Exigence.** `sensor.<é>_prochain_reveil` ignore les cours annulés et les jours
sans cours, et n'avance pas au lendemain avant la fin des cours du jour. Le
calcul se fait dans le fuseau de l'établissement.

**Exigence.** `sensor.<é>_fin_de_matinee` retient le creux le plus long qui
**commence entre 11h00 et 14h30** dans le fuseau de l'établissement **et** dure
au moins **45 minutes** (`MIDDAY_BREAK_EARLIEST`, `MIDDAY_BREAK_LATEST`,
`MIDDAY_BREAK_MIN_MINUTES`). Ce n'est délibérément pas « le plus grand creux de
la journée » : un enfant qui a un cours le matin et un en fin d'après-midi a un
creux de cinq heures qui n'est pas un déjeuner, et répondre 10h00 mettrait un
parent sur la route au mauvais moment. La durée minimale est l'autre moitié de
la règle — trente minutes à midi est un changement de salle, pas un repas.
Un creux qui commence plus tôt compte quand même s'il **contient la pause
prévue** : un creux de la grille telle que planifiée, cours annulés compris,
qui satisfait lui-même la règle. C'est le jour où un professeur absent avance
la fin de matinée — 8h30 à 10h30, le cours de 10h30 annulé, reprise à 14h00 :
l'état vaut 10h30, alors que le creux réel, commençant avant 11h00, était
écarté et que l'annonce du midi ne partait pas. La grille planifiée le
distingue d'« un cours le matin puis une fin d'après-midi », dont le creux
prévu commence lui aussi trop tôt. L'attribut `scheduled_end` donne l'heure
prévue de fin de matinée, annulations ignorées — ce qu'attend un élève qui
reste en permanence — et vaut `None` quand la grille ne prévoyait aucune pause
et que seules des annulations en ont ouvert une ; `canceled_before_break`
compte les cours annulés entre la fin réelle et la reprise.
Comme `prochain_reveil`, le capteur ne compte que les cours qui placent
réellement l'enfant quelque part : les cours annulés et ceux dont il est
dispensé sont écartés (`_teaching_lessons`). Les chevauchements sont traités en
suivant la fin la plus lointaine atteinte et non par comparaison de paires
consécutives, parce que PRONOTE renvoie des cours qui se chevauchent — un
remplaçant arrive alors que l'original est encore là — et qu'un balayage par
paires inventerait un creux négatif.

**Exigence.** L'état de `sensor.<é>_fin_de_matinee` est **`unknown` quand la
journée n'a pas de pause de midi**, et c'est l'objet de l'entité, pas une
lacune. `unknown` et non `unavailable` : la collecte a réussi, la réponse est
« pas de pause de midi aujourd'hui ». Il n'y a **pas de valeur de repli** vers
la fin des cours : `fin_des_cours` répond déjà à l'enfant qui finit à midi sans
rien après, et deux entités d'horodatage portant le même instant feraient
déclencher deux automatisations pour un seul retour. L'attribut `end_inferred`
vaut ce qu'il vaut pour toute fin de cours — vrai partout où `DateDuCoursFin`
n'est pas envoyé, ce qui est le cas de tous les cours sur certains
établissements (§4.1).

**Exigence.** `sensor.<é>_prochaine_annulation` retient le prochain cours annulé
**dont la fin n'est pas passée**, et `unknown` s'il n'y en a pas. Le même filtre
`end > now` sert l'état **et** l'attribut `items` : deux filtres finiraient par
diverger, et une carte montrerait une liste dont la première entrée n'est pas
l'état de l'entité. La conséquence est assumée — *pendant* le créneau annulé,
l'état est légèrement dans le passé, ce qui se lit « cette annulation est encore
en cours » et ne demande pas de contournement.

**Exigence.** La fenêtre de `sensor.<é>_prochaine_annulation` est celle qui a été
collectée — la **semaine entière**, pas la journée — comme `prochain_cours` : une
annulation à deux jours est exactement celle dont un parent veut être prévenu. Une
**dispense n'est pas une annulation** et n'y figure jamais : le cours a lieu,
l'enfant n'est simplement pas tenu d'y être, et l'annoncer comme annulé dirait à un
parent que la classe est supprimée.

---

## 2. Capteurs de liste — la surface d'affichage

État = décompte, contenu = attribut non enregistré. Destinés aux cartes.

| Entité | État | Attributs | P | Origine |
| --- | --- | --- | --- | --- |
| `sensor.<é>_emploi_du_temps_de_demain` | nombre de cours | **`lessons`** | `timetable` | `Client.lessons()` |
| `sensor.<é>_emploi_du_temps_de_la_semaine` | nombre de cours | **`lessons`** | `timetable` | `Client.lessons()` |
| `sensor.<é>_devoirs` | nombre total sur l'horizon | **`items`** | `homework` | `Client.homework()` |
| `sensor.<é>_notes` | nombre de notes de la période | **`items`** | `marks` | `Period.grades` |
| `sensor.<é>_moyennes_par_matiere` | nombre de matières | **`items`** | `marks` | `Period.averages` |
| `sensor.<é>_absences` | nombre | **`items`** | `attendance` | `Period.absences` |
| `sensor.<é>_retards` | nombre | **`items`** | `attendance` | `Period.delays` |
| `sensor.<é>_punitions` | nombre | **`items`** | `attendance` | `Period.punishments` |
| `sensor.<é>_evaluations` | nombre | **`items`** | `evaluations` | `Period.evaluations` |
| `sensor.<é>_actualites` | nombre | **`items`** | `news` | `Client.information_and_surveys()` |
| `sensor.<é>_discussions` | nombre | **`items`** | `discussions` | `Client.discussions()` |
| `sensor.<é>_menu_du_jour` | nombre de plats | **`first_meal`**, **`main_meal`**, **`side_meal`**, **`other_meal`**, **`cheese`**, **`dessert`**, `is_lunch`, `published` | `menus` | `Menu` |
| `sensor.<é>_menu_de_demain` | nombre de plats | idem | `menus` | `Menu` |
| `sensor.<é>_bulletin` | nombre de matières | **`subjects`**, **`comments`**, `period` | `marks` | `Period.report` |
| `sensor.<é>_equipe_pedagogique` | nombre de membres | **`items`** | `static` | `Client.get_teaching_staff()` |
| `sensor.<é>_classe` | nom de la classe | `grade`, `establishment` | `session` | `ClientInfo.class_name` |
| `sensor.<é>_periodes` | nombre de périodes | **`items`**, `current_index` | `session` | `ClientBase.periods` |

**Exigence.** Les trois dernières lignes marquées `session` ne coûtent **aucun
appel** : `class_name`, `establishment`, `name` et `periods` se lisent dans
`func_options` et `parametres_utilisateur`, déjà en main après la connexion. La
v1 les rangeait sous `static` et leur budgétait des appels ; le palier `static`
ne vaut en réalité qu'un seul appel, celui de l'équipe pédagogique.

**Exigence.** Les éléments de `sensor.<é>_moyennes_par_matiere` sont clés par
`Subject.id` : `Average` n'a **pas** de champ `id`, et la règle de stabilité
(§2.4 de la spécification) interdit d'employer un rang dans la liste.

**Exigence.** Les deux capteurs de menu portent **toujours** leurs sept clés de
plats, plus `published`. Un établissement qui ne publie rien un jour donné n'est
pas une erreur : la requête aboutit et la semaine revient vide. Or un jeu
d'attributs qui apparaît et disparaît est illisible depuis une carte —
`state_attr(..., 'main_meal')` rend `None` aussi bien quand la cantine ne publie
rien que quand le palier n'a jamais tourné, et ces deux phrases ne se disent pas
pareil à un parent. Les listes sont donc vides et `published` porte la
distinction ; `is_lunch` vaut `None` plutôt que de deviner un service pour un
repas qui n'existe pas. L'état, lui, reste `unknown` : zéro plat serait une
affirmation sur un menu existant.

**Exigence.** Chaque élément de `sensor.<é>_devoirs` (et des deux capteurs
primitifs de devoirs) porte `description` **et** `description_text`. `descriptif`
arrive de PRONOTE en HTML — les professeurs saisissent dans un éditeur riche —
et une carte ne peut en faire ni l'un ni l'autre : l'injecter ferait de chaque
champ de texte professoral une voie d'entrée dans le tableau de bord, l'afficher
tel quel fait lire les balises au parent. La conversion appartient donc au seul
module qui *sait* que le champ est du HTML, une fois, et non à chaque carte — le
HTML reste à côté, car il porte l'emphase et les liens.

### 2.1 Forme des éléments

Les tableaux ci-dessus nomment l'attribut ; ils ne disaient pas ce qu'un élément
contient, et c'est un manque qui a coûté. Une carte a lu `grade` au lieu de
`value` : **toutes** les notes s'affichaient « — », matière et coefficient
corrects, note absente, sans un signe d'erreur — et ses propres tests ne l'ont
pas vu, parce que leurs fixtures reprenaient le nom inventé. Un nom de champ
deviné se vérifie contre lui-même. Il est donc écrit ici.

Ces noms sont ceux des DTO de `models.py` et **ils sont stables** : un champ
peut s'ajouter, aucun ne sera renommé sans annonce.

**`lessons[]`** — `id`, `subject`, **`subject_id`**, `teachers[]`,
`classroom`, `start`, `end` (ISO 8601), `canceled`, `status`, `test`, `outing`,
`detention`, `exempted`, `memo`, **`background_color`**, `end_inferred`.

**`items[]` de devoirs** — `id`, `subject`, `description` (HTML tel que PRONOTE
l'envoie), `description_text` (le même énoncé en texte simple), `due`, `done`,
**`background_color`**, `attachments[]`, `attachment_refs[]`,
**`attachment_links[]`** (déprécié).

**`attachments[]`, `attachment_refs[]` et `attachment_links[]`** — trois
projections d'un seul champ.

`attachments[]` porte les **noms** de toutes les pièces jointes, ouvrables ou
non, sous forme de chaînes. Un gabarit qui fait `| join(', ')` dessus continue
de fonctionner, et c'est la façon normale d'écrire « Pièces jointes : a.pdf,
b.pdf » dans une notification. Sa forme ne change pas.

`attachment_refs[]` dit lesquelles on peut **ouvrir**, et comment. Deux formes,
que distingue `kind` :

| `kind` | Champs | Comment l'ouvrir |
| --- | --- | --- |
| `external` | `name`, `url` | `url` est absolue, en `http` ou `https`, et va dans un `href` |
| `local` | `name`, `key` | `key` se passe au service `carnet_scolaire.get_attachment_url`, qui rend l'adresse |

Un consommateur traite tout autre `kind` comme **non ouvrable**, ce qui fait
échouer fermé une valeur que cette intégration n'aurait pas encore inventée.
Aucune entrée n'y porte d'adresse qui autorise quoi que ce soit : celle d'un
lien appartient à un tiers et l'élève la voit déjà dans PRONOTE, et `key`
désigne un document sans rien ouvrir. `key` est la sortie de
`attachment.fingerprint` — seize caractères hexadécimaux **en minuscules**, et
une carte qui filtre sur cette forme exacte est dans son droit.

`attachment_links[]` est **déprécié** et disparaît à la version suivante. Il ne
porte plus que les liens, sous leur ancienne forme `{ name, url }` : une carte
qui n'a pas encore été mise à jour continue d'ouvrir les liens et perd les
fichiers. L'inverse aurait été plus confortable et c'est un choix : ce champ
donnait à chaque fichier une adresse signée, et le conserver « le temps d'une
version » aurait gardé ouverte exactement la faille que cette version ferme.

**Le discriminant est déclaré, pas déduit.** `kind` est décidé une fois, là où
la charge utile est lue (`gateway._attachment`), et non inféré par le
consommateur de la présence d'une adresse. Cette inférence était fausse sur le
seul cas qui compte : un lien dont l'adresse est inutilisable n'a pas
d'adresse non plus, et il a été pris pour un fichier — une adresse signée lui
était frappée, et le relais allait le chercher avec la session de
l'établissement comme s'il s'agissait d'un document. C'est la doctrine que ce
projet applique déjà aux coûts : déclarés, jamais inférés.

Le rapprochement entre `attachments[]` et `attachment_refs[]` se fait par le
nom, et c'est `key` qui désigne un fichier sans ambiguïté — y compris quand
deux pièces d'un même devoir portent le même nom, cas où l'ancien
rapprochement par le nom rendait les deux ouvrables.

**Pourquoi PRONOTE impose deux sortes.** PRONOTE range deux choses différentes
sous une même clé, et la différence décide de ce qui peut sortir de ce
processus.

Un **lien** est une adresse qu'un professeur a collée. Elle est stable, elle
n'authentifie personne, et elle est publiée telle quelle : la relayer
reviendrait à aller chercher le site d'un tiers avec la session de
l'établissement, ce qui n'est pas l'affaire de cette intégration.

Un **fichier** n'a aucune adresse qui existe indépendamment de la session.
`pronotepy` en construit une de la forme
`FichiersExternes/<hexadécimal>/<nom>?Session=<n>`, où le segment hexadécimal
n'identifie pas le document : c'est le couple `{"N": id, "Actif": true}`
chiffré avec la clé **et** le vecteur d'initialisation de la session, tous deux
tirés à chaque connexion. Deux liens vers le même document depuis deux sessions
n'ont donc aucun octet commun, et celle-ci ouvre le document sans aucun
identifiant tant que la session vit.

**Exigence.** L'adresse d'un fichier n'est dans **aucun** attribut. Le service
`carnet_scolaire.get_attachment_url` la frappe à l'instant du clic : un chemin signé
vers le point d'entrée `/api/carnet_scolaire/attachment/` de l'intégration, qui va
chercher les octets par le chemin unique vers le réseau — une session, un
verrou, une requête facturée — et les **relaie**.

La raison est qu'une adresse signée est un **jeton porteur**, et qu'un attribut
est la surface la plus largement lisible de Home Assistant. Tout compte,
administrateur ou non, lit `/api/states` en entier ; une trace d'automatisation
conserve l'état déclencheur avec ses attributs dans `.storage`, donc dans les
sauvegardes ; la boîte de dialogue « plus d'infos » les affiche, donc une
capture d'écran publie un jeton vivant. Jusqu'à la version 0.0.25 l'adresse
signée était publiée dans l'attribut, et sa protection reposait sur son
exclusion de l'enregistreur : il a fallu trois versions pour que cette
exclusion porte réellement — § 9 de la spécification en garde les deux pièges
— et même juste, elle ne retirait que le vecteur *durable*.

**Exigence.** L'adresse frappée expire en **cinq minutes**. Elle a expiré en
douze heures, et ce chiffre était dérivé d'une contrainte qui n'existe plus :
publiée dans un attribut, elle devait survivre à l'intervalle du palier des
devoirs sans quoi un parent rencontrait un 401 sur un lien que la page affichait
encore. Frappée au clic et ouverte aussitôt, elle n'a qu'à survivre à
l'aller-retour et à la seconde plage d'octets qu'une visionneuse PDF réclame —
et chaque minute de plus est une minute pendant laquelle une adresse recopiée
ouvre encore le document d'un enfant.

**Exigence.** Le service ne place **aucune requête** vers PRONOTE. Il résout
`key` contre l'instantané déjà en mémoire et signe avec une clé que Home
Assistant détient déjà. La requête a lieu à l'ouverture de l'adresse, quand le
relais va chercher les octets ; ce relais est en `Priority.HIGH`, donc refusé
entre 22 h et 6 h avec un 503 et un `Retry-After` qui dit quand revenir. Une
carte qui appelle le service ne déclenche donc toujours aucune collecte.

**Exigence.** Le service ne résout une clé que pour l'enfant désigné par
`device_id`. Sur un compte parent une carte détient les clés des deux enfants,
et la clé de l'un présentée avec l'appareil de l'autre ne doit pas devenir une
adresse. Elle rend `attachment_unknown` — la même réponse qu'une clé périmée,
pour que le refus ne confirme même pas que la clé existe ailleurs.

**Exigence.** Deux refus distincts, et leurs `translation_key` sont un
**contrat** : une carte les lit sur l'erreur pour choisir sa phrase, et les
renommer la ferait retomber, sans bruit, sur un message générique.

| `translation_key` | Ce qu'il signifie | Ce que la carte en dit |
| --- | --- | --- |
| `attachment_not_collected` | le palier des devoirs n'a pas encore d'instantané pour cet enfant — typiquement dans les secondes qui suivent un redémarrage | patienter |
| `attachment_unknown` | l'instantané existe et la clé n'y désigne aucun fichier : le devoir a quitté l'horizon, il a changé, ou la clé appartient à un autre enfant | rafraîchir |

Une clé mal formée — mauvaise longueur, majuscules, chemin — est refusée par le
schéma avant toute recherche, parce qu'une faute de frappe n'est pas une
rotation et ne doit pas se dire comme telle.

Quatre propriétés du chemin frappé portent le raisonnement, et aucune n'est
cosmétique :

- **Il relaie, il ne redirige pas.** Une redirection 302 mettrait l'adresse
  PRONOTE dans la barre d'adresse, l'historique du navigateur, l'onglet réseau
  et le journal de tout mandataire sur le trajet — donc exactement le droit
  d'accès qu'il s'agit de ne pas publier.
- **Il est enraciné, pas absolu.** Une URL absolue obligerait l'intégration à
  deviner par quel hôte le navigateur est arrivé, et rendrait l'adresse interne
  à quelqu'un connecté de l'extérieur. Le consommateur le résout contre l'URL de
  l'instance, jamais contre celle de la page qui l'héberge : un tableau de bord
  servi depuis une autre origine enverrait sinon le jeton chez un tiers.
- **Il nomme une empreinte, pas un identifiant.** Un identifiant PRONOTE réel
  contient un `#`, délimiteur de fragment qui tronquerait le chemin, et l'écrire
  dans un attribut le rendrait durable. L'empreinte est l'idiome que
  `diagnostics.py` emploie déjà pour les identifiants d'enfant. Elle désigne un
  **document** et non un rang, donc un réordonnancement entre deux collectes ne
  peut pas servir le mauvais fichier.
- **Il expire** en cinq minutes, vu plus haut.

**Exigence.** Le relais ne sert qu'un **fichier**. `attachment._locate` ne rend
que des pièces de sorte `FILE`, et le relais refuse encore toute autre sorte
qu'on lui tendrait : un lien est la page d'un tiers, et le relayer ferait de
Home Assistant un mandataire ouvert pour tout ce qu'un professeur colle.

**Exigence.** Un lien n'est publié que s'il n'est pas une adresse que PRONOTE
authentifierait. Trois signatures suffisent chacune à le refuser : l'hôte de
l'établissement, lu sur `communication.root_site` sans requête ; un paramètre
`Session`, quelle que soit sa casse ; un segment `FichiersExternes`, route de
PRONOTE pour un fichier quel que soit l'hôte qu'un mandataire met devant. Sur
une instance réelle, onze liens mesurés, tous chez des tiers — ce qui est un
fait sur une quinzaine de devoirs, pas une garantie. La carte ne peut pas faire
ce contrôle elle-même : elle ne connaît pas l'hôte du serveur, et elle ne doit
pas le connaître.

**Exigence.** Le type de contenu annoncé au navigateur est une **liste
blanche** — PDF, images matricielles, texte simple — et tout le reste est servi
en `application/octet-stream`, donc téléchargé plutôt qu'affiché. La réponse
sort de l'origine de Home Assistant : un document renvoyé en `text/html`
exécuterait son propre script avec la session du lecteur, et `image/svg+xml`
est le même danger sous un nom d'image. `X-Content-Type-Options: nosniff`
empêche le navigateur de deviner un autre type que celui annoncé, et
`Referrer-Policy: no-referrer` que l'adresse signée du relais parte chez un
tiers quand un document contient un lien.

Une pièce absente de `attachment_refs[]` reste nommée dans `attachments[]`, et
ce n'est pas une lacune : sa sorte est `OPAQUE`. C'est un lien dont l'adresse
est inutilisable — pas de schéma `http`/`https`, ou pas d'adresse du tout dans
la charge utile, cas où amont retombe sur le **nom**, ce qui est le piège —, un
lien que PRONOTE authentifierait, ou un fichier publié sans identifiant, qu'on
ne saurait pas demander.

**Exigence.** L'adresse publiée est en `http` ou `https`, jamais autre chose.
C'est une liste blanche et non un filtre, parce qu'un consommateur met cette
valeur dans un `href` : `javascript:` y serait l'injection contre laquelle le
champ `description` est déjà protégé (§ 3.3). Une adresse relative est refusée
par le même contrôle, et c'est aussi la bonne réponse — le consommateur ne
sait pas de quel hôte elle viendrait, et la résoudre contre celui de Home
Assistant fabriquerait un lien mort.

**`background_color`** — la couleur que l'établissement associe à la matière,
**telle que le serveur l'envoie**, ou `null`. Elle apparaît sur les créneaux,
sur les devoirs et sur les moyennes par matière : trois étages, une seule
sémantique.

Trois précisions, parce que les trois se devinent mal :

- **la clé est toujours là, seule sa valeur peut manquer.** `null` dit « on a
  demandé, il n'y en a pas » — ce qui n'est pas la même phrase que « cette
  intégration ne publie pas de couleur ». Une clé qui apparaît et disparaît
  obligerait chaque gabarit à tester l'appartenance avant la valeur, et celui
  qui l'oublie écrit `None` dans un tableau de bord ;
- **aucune valeur n'est inventée.** Pas de couleur par défaut, pas de couleur
  dérivée du nom de la matière, pas de substitut. Une couleur fabriquée est
  indiscernable d'une vraie : dès qu'il en existe une, plus personne ne peut
  savoir ce que le serveur envoie, et le consommateur perd le seul signal qui
  lui permet de décider s'il doit se replier sur une table à lui ;
- **le serveur ne la remplit pas partout.** PRONOTE l'envoie sous
  `CouleurFond` sur les devoirs et les créneaux, sous `couleur` sur les
  moyennes par matière — et `pronotepy` résout celle des devoirs en mode
  *strict* là où celle des créneaux est tolérante, ce qui est la bibliothèque
  affirmant qu'un devoir en porte toujours une. Un créneau sans couleur n'est
  donc pas un défaut de collecte.

**`subject_id`** — l'identifiant de la matière, à côté de son nom. Une carte
qui associe une matière à quelque chose qui lui appartient (une couleur, une
icône, un filtre) doit poser cette association sur une clé, et le nom en est
une mauvaise : PRONOTE l'écrit en capitales, avec des accents, et un
établissement peut le renommer en cours d'année.

**`grades[]`** — `id`, `subject`, `subject_id`, `subject_in_groups`,
**`value`** (et non `grade` — mais la charge de `event.<é>_nouvelle_note` nomme
cette même valeur `grade` : les deux noms coexistent, `value` dans les
attributs, `grade` dans l'événement), `status`, `out_of`, `default_out_of`,
`is_out_of_20`, `coefficient`, `date`, `class_average`, `min`, `max`,
`comment`, `remark`, `is_bonus`, `is_optional`, `attachment_refs[]`.
`comment` est l'intitulé du devoir, le même pour toute la classe ; `remark` est
l'appréciation du professeur sur **cette** note (`commentaireSurNote`), nulle
le plus souvent. `subject_in_groups` dit que la matière est enseignée en
groupes : `class_average`, `min` et `max` sont alors ceux du groupe.
`default_out_of` est le barème habituel de la matière et `is_out_of_20` dit
que PRONOTE ramène la note sur 20 dans la moyenne.

`attachment_refs[]` porte le **sujet** et le **corrigé** que le professeur a
joints au devoir — au plus un de chaque, et une liste vide est le cas
ordinaire. Même forme que sur les devoirs, plus le rôle :
`{"name", "kind": "local", "key", "role": "subject" | "correction"}`. Un
document de devoir noté est toujours un fichier, d'où `kind` toujours `local`,
et il s'ouvre comme un fichier de devoir : `carnet_scolaire.get_attachment_url`
signe la `key` au clic, puis le relais relit les notes de la période et
télécharge le document — deux requêtes, imputées au palier des notes. Aucune
adresse de PRONOTE n'est publiée. Le sujet et le corrigé n'entrent pas dans la
clé de la note : un corrigé déposé une semaine après la note ne la fait pas
annoncer une seconde fois.

`value` et `status` sont **exclusifs** (§4.3) :
l'un des deux est nul. C'est ce qui permet à l'état de
`sensor.<é>_derniere_note` d'être numérique — donc utilisable par un
`numeric_state` et par un graphe — au lieu d'être tantôt `14.5` tantôt
`Absent`.

**`averages[]`** — `subject_id`, `subject`, **`student`** (et non `average`),
`class_average`, `min`, `max`, `out_of`, **`background_color`** (décodée ici
sous `couleur` et non `CouleurFond` — même sémantique, orthographe amont
différente, absorbée par la passerelle). Clés par `subject_id` : `Average` n'a
pas d'identifiant propre et §2.4 interdit d'employer un rang.

Ici `out_of` est le barème décodé (`baremeMoyEleve`). Sur
`sensor.<é>_moyenne_generale` et `sensor.<é>_moyenne_de_la_classe`, l'attribut du même
nom est une **constante 20** écrite dans le producteur d'attributs, pas une
lecture : le même nom de clé porte une mesure sur une entité et une hypothèse
sur l'autre.

**`absences[]`** — `id`, `from_date`, `to_date`, `justified`, **`hours`** (une
**chaîne**, `"2h00"`, telle qu'upstream la donne), `days` (un entier),
`reasons[]`. Il n'y a **pas** de `minutes` ici.

**`delays[]`** — `id`, `date`, **`minutes`** (un entier), `justified`,
`justification`, `reasons[]`. `minutes` vit ici et nulle part ailleurs : la
spécification v1 le listait sur les deux, ce qui est la raison pour laquelle les
deux ont aujourd'hui des entités `event` distinctes (§4).

**`punishments[]`** — `id`, `nature`, `reasons[]`, `giver`, `exclusion`,
`schedule[]`. Chaque créneau de `schedule[]` porte `start` et
**`duration_minutes`**. La durée n'existe **qu'**au niveau du créneau :
`Punishment` n'en a pas au premier niveau, et ce n'est pas un oubli
d'exposition — PRONOTE ne la donne pas autrement.

**`evaluations[]`** — `id`, `name`, `subject`, `date`, `acquisitions[]`. Chaque
acquisition porte `name`, `level`, `abbreviation`, `domain`.

**`items[]` d'actualités** — `id`, `title`, `author`, `category`, `read`,
`survey`, `created`. Le **contenu est délibérément absent** :
`Information.content` est un attribut paresseux qui place une requête à la
lecture, et rien de tel ne franchit la passerelle (§3.1).

**`items[]` de discussions** — `id`, `subject`, `creator`, `unread`, `closed`,
`messages[]`, chaque message portant `id`, `author`, `created`.

**`items[]` d'équipe pédagogique** — `name`, `role` (`teacher` ou `staff`,
l'interprétation restant celle d'upstream), `subjects[]`.

**`subjects[]` du bulletin** — `id`, `name`, `student_average`, `class_average`,
`coefficient`, `comments[]`, `teachers[]`. Le capteur porte en plus `comments`
au premier niveau et `period`.

**Exigence.** Ces formes sont un **contrat**. Une carte tierce qui les lit doit
pouvoir être écrite sans lire le code source de l'intégration, et un champ
renommé en silence casse un tableau de bord sans aucun message d'erreur — le
défaut se présente comme une donnée manquante, jamais comme une panne.

### 2.2 Historique des périodes closes

**Exigence.** Pour chaque période close suivie (option `history_periods`), les
entités suivantes sont créées, suffixées par l'**index** de période et non par
son nom (§2.4 de la spécification) :

`sensor.<é>_notes_p<n>` · `sensor.<é>_moyennes_p<n>` ·
`sensor.<é>_moyenne_generale_p<n>` · `sensor.<é>_absences_p<n>` ·
`sensor.<é>_retards_p<n>` · `sensor.<é>_punitions_p<n>` ·
`sensor.<é>_evaluations_p<n>` · `sensor.<é>_bulletin_p<n>`

Le nom **affiché** contient le libellé de la période, traduit avec un
substituteur (`{period}`), donc lisible ; l'identifiant reste stable si
l'établissement renomme « Trimestre 1 » en « Semestre 1 ».

**Désactivées par défaut.** Ces huit entités par période close sont les
**seules** de l'intégration créées désactivées (règle
`entity-disabled-by-default` de l'échelle de qualité) : leur nombre grandit au
fil de l'année, elles s'écrivent dans l'historique à chaque relecture pour des
chiffres qui ne changent plus, et aucun blueprint, déclencheur ou condition
d'appareil ni aucune carte ne les lit. Trois précisions :

- cela ne vaut que pour une entité **nouvellement créée** : une entité déjà
  présente dans le registre garde son état, activée ou non ;
- une entité désactivée reste **collectée** — le palier `history` ignore
  quelles entités sont activées. C'est la désactivation du palier « Périodes
  closes » dans les options qui économise ses requêtes ;
- les activer se fait entité par entité, voir le
  [guide, §4.4](GUIDE-UTILISATEUR.md#44-les-périodes-closes).

---

## 3. Capteurs binaires

| Entité | `on` quand | `device_class` | P | Origine |
| --- | --- | --- | --- | --- |
| `binary_sensor.<é>_jour_de_classe` | au moins un cours non annulé aujourd'hui | — | `timetable` | `Lesson` |
| `binary_sensor.<é>_en_cours` | l'heure courante est dans un cours | — | `timetable` | `Lesson.start`/`end` |
| `binary_sensor.<é>_cours_annules` | au moins un cours annulé aujourd'hui | `problem` | `timetable` | `Lesson.canceled` |
| `binary_sensor.<é>_sortie_pedagogique` | une sortie est prévue aujourd'hui | — | `timetable` | `Lesson.outing` |
| `binary_sensor.<é>_controle_prevu` | un contrôle est prévu aujourd'hui | — | `timetable` | `Lesson.test` |
| `binary_sensor.<é>_devoirs_en_retard` | un devoir non fait a une échéance passée | `problem` | `homework` | `Homework.done`/`date` |
| `binary_sensor.<é>_absence_en_cours` | une absence couvre l'heure courante | `problem` | `attendance` | `Absence.from_date`/`to_date` |
| `binary_sensor.<é>_punition_a_venir` | une punition est programmée dans le futur | `problem` | `attendance` | `Punishment.schedule` |
| `binary_sensor.<é>_vacances` | aucun cours dans les 7 jours **ou** aucun cours de toute la semaine courante | — | `timetable` | — |
| `binary_sensor.<compte>_collectes_bridees` | le limiteur retarde des collectes | `problem` | — | — |

### 3.1 Trois jumeaux retirés en v2

`binary_sensor.devoirs_a_faire`, `_actualites_non_lues` et `_messages_non_lus`
existaient en v1 à côté des capteurs de décompte du §1. Ils tombent sous le test
du jumeau (§2.1 de la spécification) : chacun n'était qu'un
`numeric_state above: 0` sur un nombre déjà exposé, donc une ligne de YAML sans
template — le niveau que fixe l'objectif du projet. Trois entités par enfant qui
ne faisaient disparaître aucun template.

Les capteurs binaires conservés encodent tous un prédicat qu'il faudrait sinon
écrire à la main : une intersection de créneaux, une comparaison d'échéance, une
absence de cours sur une fenêtre.

**Exigence.** Les entités qui dépendent de l'heure et non des seules données —
`en_cours`, `absence_en_cours`, et côté capteurs `prochain_cours`,
`fin_des_cours`, `fin_de_matinee`, `prochaine_annulation`, `prochain_reveil`,
`prochain_controle`, `prochaine_punition` —
sont réévaluées par `async_track_point_in_time` positionné sur la prochaine
bascule connue, jamais par sondage. Sans cela elles changent d'état au rythme de
leur palier, avec jusqu'à quinze minutes de retard : sur un capteur de réveil,
c'est tout son objet qui disparaît.

---

## 4. Entités `event` — les déclencheurs

Alimentées par `delta.py` (§2.2 de la spécification). Le `event_type` distingue
la nature du changement ; les attributs portent le contexte.

| Entité | `event_types` | Attributs du contexte | P |
| --- | --- | --- | --- |
| `event.<é>_nouvelle_note` | `grade_added` | `subject`, `grade`, `out_of`, `coefficient`, `date`, `class_average`, `status`, `grade_id` | `marks` |
| `event.<é>_nouveau_devoir` | `homework_added` | `subject`, `description`, `due`, `id` | `homework` |
| `event.<é>_cours_modifie` | `lesson_canceled`, `lesson_restored`, `lesson_replaced`, `lesson_moved`, `room_changed`, `teacher_changed`, `lesson_status_changed` | `subject`, `start`, `end`, `previous_start`, `previous_end`, `classroom`, `previous_classroom`, `teachers`, `previous_teachers`, `status`, `canceled`, `lesson_id` ; sur `lesson_replaced` seulement : `replacement_subject`, `replacement_classroom`, `replacement_teachers`, `replacement_status` | `timetable` |
| `event.<é>_nouvelle_actualite` | `information_added` | `author`, `title`, `category`, `survey`, `information_id` | `news` |
| `event.<é>_nouvelle_absence` | `absence_added` | `from_date`, `to_date`, `justified`, `reasons`, `hours`, `days`, `absence_id` | `attendance` |
| `event.<é>_nouveau_retard` | `delay_added` | `date`, `justified`, `justification`, `reasons`, `minutes`, `delay_id` | `attendance` |
| `event.<é>_nouvelle_punition` | `punishment_added` | `nature`, `reasons`, `giver`, `exclusion`, `schedule`, `punishment_id` | `attendance` |
| `event.<é>_nouveau_message` | `message_received` | `discussion`, `author`, `created`, `discussion_id`, `unread` | `discussions` |
| `event.<é>_nouvelle_evaluation` | `evaluation_added` | `subject`, `name`, `acquisitions`, `date`, `evaluation_id` | `evaluations` |

**Deux attributs ne sont pas toujours renseignés.** Sur
`event.<é>_nouveau_message`, `author` et `created` sont `null` quand la passerelle
n'a pas déplié le fil — plus de fils sont passés en non-lu que le plafond
d'expansion n'en autorise. L'évènement part quand même, avec l'identité du fil :
perdre « un message est arrivé » serait pire que perdre le nom de l'auteur.

**Exigence.** Deux règles de détection, selon la collection (§2.2.1 de la
spécification) :

- **Toutes les lignes de ce tableau sauf `cours_modifie`** sont des collections
  en ajout seul : delta sur l'identifiant `N`, jamais sur le contenu. Un libellé
  corrigé par un professeur ne produit pas de faux évènement.
- **`cours_modifie`** ne peut pas fonctionner sur `N` : un changement de salle
  conserve le même identifiant, et un remplacement en crée un nouveau sans
  retirer l'ancien. Sa détection est donc : dédoublonnage par créneau en gardant
  le `num` maximal, puis comparaison du tuple
  `(canceled, status, classroom, teachers, start, end)` sur la clé
  `(date, place, subject_id)`. Les quatre attributs `previous_*` viennent de ce
  tuple précédent — ils n'avaient aucune source dans la v1.

**Exigence.** Aucun évènement n'est émis lors du premier instantané d'un palier
après démarrage ou rechargement, sinon chaque redémarrage rejouerait le
trimestre.

**Exigence.** Un même cycle qui découvre huit nouvelles notes émet **huit**
évènements successifs, pas un évènement agrégé : une automatisation qui notifie
par note doit pouvoir le faire, et l'agrégation est triviale à refaire côté
utilisateur alors que la séparation ne l'est pas.

---

## 5. Agendas, tâches, boutons, image

### 5.1 `calendar`

| Entité | Contenu | P | Origine |
| --- | --- | --- | --- |
| `calendar.<é>_emploi_du_temps` | un évènement par cours **dédoublonné**, `uid` = `Lesson.id` | `timetable` | `Lesson` |
| `calendar.<é>_devoirs` | un évènement d'une journée par échéance | `homework` | `Homework.date` |
| `calendar.<é>_punitions` | les créneaux programmés (retenues) | `attendance` | `Punishment.schedule` |

**Exigence.** Un cours annulé reste dans l'agenda, avec son statut en
description et le préfixe traduit `annulé` dans le résumé. Le supprimer
donnerait l'illusion qu'il n'a jamais existé.

### 5.2 `todo`

| Entité | Éléments | Écriture | P |
| --- | --- | --- | --- |
| `todo.<é>_devoirs` | un élément par devoir, `due` = échéance, `summary` = matière, `description` = énoncé **en texte simple** | cocher → `SaisieTAFFaitEleve`, relu ensuite ; compte élève seulement | `homework` |

**Exigence.** `UPDATE_ITEM` n'est annoncé que si `write_operations_enabled` est
vrai (§8.3 de la spécification).

### 5.3 `button`

| Entité | Effet |
| --- | --- |
| `button.<é>_rafraichir` | demande un passage prioritaire à l'ordonnanceur, sans contourner le limiteur |
| `button.<é>_rafraichir_les_notes` | idem, palier `marks` seulement |

### 5.4 `image`

| Entité | Contenu | P | Origine |
| --- | --- | --- | --- |
| `image.<é>_photo` | photo de profil | `static` | `ClientInfo.profile_picture` |

**Exigence.** `ClientInfo.profile_picture` passe par `ClientInfo._cache()`, qui
court-circuite `ClientBase.post` et la signature `membre` du parent. La photo est
donc lue par une fonction de la passerelle, sous verrou et après `set_child`,
jamais par une lecture de propriété depuis l'entité — sans quoi un compte parent
peut afficher la photo du mauvais enfant.

**Exigence.** Aucune entité n'expose l'identité détaillée (`Identity`,
`Guardian`) : date de naissance, adresse, INE, téléphones et courriels sont
lus par le palier `static` uniquement si un service les demande, et n'entrent
dans aucun état (§8.1 de la spécification).

---

## 6. Services

| Service | Cible | Effet | Réponse |
| --- | --- | --- | --- |
| `carnet_scolaire.refresh` | entrée, palier optionnel | force une échéance | — |
| `carnet_scolaire.get_ical_url` | entrée | rend l'URL iCal | `SupportsResponse.ONLY` |
| `carnet_scolaire.get_identity` | entrée | rend l'identité et les responsables légaux | `SupportsResponse.ONLY` |
| `carnet_scolaire.mark_homework_done` | identifiant de devoir | `SaisieTAFFaitEleve`, relu ensuite ; refusé sur un compte parent | — |
| `carnet_scolaire.mark_information_read` | identifiant d'actualité | `Information.mark_as_read()` | — |
| `carnet_scolaire.send_message` | discussion ou destinataires | `Discussion.reply()` / `Client.new_discussion()` | — |
| `carnet_scolaire.generate_timetable_pdf` | entrée, jour, orientation | rend une URL de PDF | `SupportsResponse.ONLY` |
| `carnet_scolaire.get_rate_limit_status` | entrée | rend l'état complet du limiteur | `SupportsResponse.ONLY` |
| `carnet_scolaire.get_attachment_url` | enfant, `key` | rend une adresse signée de cinq minutes vers un fichier de devoir | `SupportsResponse.ONLY` |

**Exigence.** Les services à réponse ne créent aucun état et ne journalisent
pas leur réponse. `get_ical_url`, `get_identity` et `get_attachment_url`
rendent des données que le §8 de la spécification interdit d'exposer
autrement : les deux premiers des données personnelles ou un accès à l'emploi
du temps, le troisième un jeton porteur.

**Exigence.** Les services d'écriture lèvent `HomeAssistantError` avec
`translation_domain` et `translation_key` — jamais un message construit en
français dans le code (§10 de la spécification).

---

## 7. Diagnostic du limiteur

Rend le réglage du §6 observable. Toutes ces entités portent
`entity_category: diagnostic`.

| Entité | État | Attributs |
| --- | --- | --- |
| `sensor.<compte>_appels_du_jour` | nombre d'appels depuis minuit | `by_tier`, `logins`, `failed_logins` |
| `sensor.<compte>_budget_restant` | appels restants sur le plafond du jour | `daily_cap`, `hourly_remaining`, `tokens` |
| `sensor.<compte>_derniere_collecte` | horodatage (`timestamp`) | `tier`, `duration_ms`, `calls` |
| `sensor.<compte>_prochaine_collecte` | horodatage (`timestamp`) | `tiers_due`, `overdue_by`, `failing`, `boosted`, `boost_served_at` |
| `sensor.<compte>_age_de_la_session` | âge de la session en secondes | `session_id_hash`, `opened_at` |
| `sensor.<compte>_duree_de_vie_de_la_session` | durée de vie **mesurée** de la session, en minutes | `samples`, `last_expiry`, `strategy` |
| `sensor.<compte>_connexions_du_jour` | nombre de connexions réussies | `failed`, `cap` |
| `sensor.<compte>_etat_du_limiteur` | `nominal`, `throttled`, `backoff`, `quiet_hours`, `credentials_hold`, `bootstrap_failed` | `until`, `reason`, `consecutive_failures` |
| `binary_sensor.<compte>_collectes_bridees` | `on` si des collectes sont retardées | `since` |

Ces entités sont attachées à l'appareil **compte**, pas à un enfant : le budget
est partagé (§7.1 de la spécification).

**Exigence.** `session_id_hash` est une empreinte tronquée, pas l'identifiant de
session. Un diagnostic ne doit pas donner de quoi rejouer une session.

**Exigence.** Ces neuf entités se **rafraîchissent d'elles-mêmes**, toutes les
trente secondes, sans placer aucune requête : leurs valeurs se lisent dans le
limiteur, l'ordonnanceur et la session, déjà en mémoire. Ce n'est pas un détail
d'implémentation. Rafraîchies seulement par une collecte — ce qu'elles étaient,
`_attr_should_poll` étant inopérant sur une entité de coordinateur —, elles se
taisaient précisément dans la circonstance où on les consulte : un palier dû et
en échec laissait « prochaine collecte » figée quarante minutes dans le passé,
et le témoin « bridé », dont tout le rôle est d'expliquer une intégration
silencieuse (§6.6), pouvait rester allumé après le retour à la normale. La
contrepartie est explicite dans `LocallyPolledMixin` : le scrutin ne doit
**jamais** appeler `async_request_refresh`, sans quoi une lecture gratuite
deviendrait une requête toutes les trente secondes.

**Exigence.** `prochaine_collecte` répond aussi à « mon appui sur *Rafraîchir*
a-t-il porté ? », et il faut deux attributs pour ça parce qu'il y a trois
réponses. `boosted` liste les catégories dont la demande est **armée et pas
encore servie** ; `boost_served_at` donne, par catégorie, l'heure à laquelle la
dernière demande a effectivement été suivie d'une collecte.

L'absence des deux ne signifie **rien** en soi, et la formulation courte — « pas
dans `boosted`, pas dans `boost_served_at`, donc refusé » — est fausse. Ces
attributs portent un **état**, pas une issue : sur une instance qui vient de
démarrer les deux sont vides parce que personne n'a rien pressé, et non parce
qu'un plafond a refusé quoi que ce soit. La lecture juste demande une troisième
information que l'intégration n'a pas et que l'appelant a : *l'instant de
l'appui*. Si un appui de moins d'un intervalle existe et que la catégorie
n'apparaît dans aucun des deux, alors le plafond a refusé — une demande ayant
déjà été servie dans l'intervalle, c'est-à-dire « c'est déjà demandé, ça
viendra ». Sans cette condition, une interface fraîchement ouverte annonce
« refusé » à quelqu'un qui vient d'arriver.

Ni l'un ni l'autre ne redit `failing`, ni les reports du limiteur : ceux-là
répondent à « le limiteur a-t-il refusé », qui est une autre question. Un boost
sur une catégorie que le limiteur maintient fait monter les reports sans placer
une seule requête. La distinction est ici parce que la confondre a un coût
concret : un bouton qui ne peut dire que « la demande est partie » est un bouton
qu'on presse deux fois, et un second appui n'est pas gratuit sur une catégorie
en échec. L'ordonnanceur connaissait déjà les deux faits ; ils n'atteignaient
que le vidage de diagnostics et un service à réponse seule, donc aucun tableau
de bord.

**Exigence.** `prochaine_collecte` porte une **échéance**, et non un décompte
converti à l'instant de la lecture. Un horodatage passé y est une lecture
légitime — un palier est en retard — et `overdue_by` en donne l'ampleur en
secondes tandis que `failing` nomme les paliers dont la dernière tentative a
échoué. Sans eux, « échéance dépassée et rien n'a tourné » et « il tourne et
échoue à chaque fois » se lisent pareil sur la tuile.

---

## 8. Ce que PRONOTE expose et qui n'est délibérément pas exposé

Traçabilité du périmètre : ces données existent dans `pronotepy` et ne
deviennent pas des entités.

| Donnée | Classe | Raison |
| --- | --- | --- |
| Identité complète | `Identity` | données personnelles ; service à réponse uniquement |
| Responsables légaux | `Guardian` | idem |
| Numéro INE | `Identity.ine_number` | identifiant national ; jamais exposé |
| URL iCal | `Client.export_ical()` | porteur d'authentification (§8.2) |
| Jeton mobile | `export_credentials()` | secret de connexion |
| Pièces jointes | `Attachment.data` | téléchargement à la demande, pas un état ; URL en attribut |
| Contenu de cours | `Lesson.content` | attribut paresseux coûteux ; résolu seulement pour la semaine courante |
| Destinataires possibles | `Recipient` | utile au seul service `send_message`, pas un état |
| Élèves de la classe | `StudentClass.students()` | données d'autres élèves |
| Corbeille et brouillons | `Discussion.labels` | filtrés à l'entrée |

**Exigence.** Cette table est maintenue. Toute donnée `pronotepy` non exposée y
figure avec sa raison — sans quoi l'écart entre ce que le protocole rend et ce
que l'intégration montre redevient invisible, ce qui est précisément le défaut
qu'on corrige.
