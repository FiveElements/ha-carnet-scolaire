# Contribuer à Carnet scolaire

Merci de l'intérêt. Ce document dit comment travailler sur ce dépôt, et
surtout **pourquoi** certaines règles sont plus strictes qu'ailleurs : cette
intégration manipule les identifiants scolaires d'enfants et parle à un serveur
qui sanctionne une adresse IP. Deux contraintes qui façonnent tout le reste.

Le dépôt s'appelle `ha-carnet-scolaire`, l'intégration s'appelle **Carnet
scolaire**, et son domaine Home Assistant est `carnet_scolaire`. Elle collecte
auprès de PRONOTE et d'EcoleDirecte ; elle s'appelait *Pronote NG* (domaine
`pronote_ng`) jusqu'à la 0.0.32.

---

> **Un `.venv` local ne suffit plus pour les portails.** Home Assistant 2026.9
> exige Python **3.14.2**, et `pytest-homeassistant-custom-component` refuse de
> s'installer sous 3.13 à partir de `0.13.317`. Un environnement local en 3.13
> installe donc l'ancienne pile, et `mypy --strict` y vérifie le code contre une
> version de Home Assistant **antérieure au plancher déclaré** — ce qui a déjà
> laissé passer une clé d'API supprimée depuis. Lancez `ruff`, `mypy` et
> `pytest` dans le conteneur ou sous Python 3.14, comme la CI.

## 1. Les deux règles non négociables

### 1.1 Jamais un identifiant réel dans ce dépôt

Aucune *pull request* ne peut contenir un identifiant PRONOTE réel, un mot de
passe, un code PIN, un jeton `jetonConnexionAppliMobile`, le contenu d'un QR
code, une URL iCal (`icalsecurise=`), un nom d'élève, un nom d'établissement ou
un identifiant `N` réel — ni dans le code, ni dans un test, ni dans une
*fixture*, ni dans un message de commit, ni dans une capture d'écran.

Les *fixtures* de `tests/fixtures/` sont **écrites à la main** et non
enregistrées depuis un serveur réel. Toute valeur d'exemple doit être
manifestement fictive : `demo.example.invalid`, `Enfant Un`, `STUDENT-1`,
`not-a-real-password`, `0000000000A`. Si vous avez besoin d'une charge utile
capturée en vrai pour comprendre un bug, elle va dans `tests/fixtures/_raw/`,
que `.gitignore` exclut, et vous en dérivez une version anonymisée.

Une URL iCal mérite une mention à part : elle donne accès à l'emploi du temps
complet d'un élève **sans aucun identifiant**. Traitez-la comme un mot de
passe.

### 1.2 Jamais le journaliseur `pronotepy` en DEBUG

`pronotepy/pronoteAPI.py` écrit l'hexadécimal de chaque corps de requête au
niveau DEBUG, identifiants compris. Cette intégration ne configure donc aucun
journaliseur et ne déclare **aucune clé `loggers`** dans son manifeste — c'est
`scripts/check_manifest.py` qui l'empêche, et un test qui vérifie qu'un cycle
complet ne journalise aucun secret.

Pour déboguer, activez le journaliseur de l'intégration seulement :

```yaml
logger:
  logs:
    custom_components.carnet_scolaire: debug
```

N'ajoutez jamais `pronotepy` à cette liste, et ne le suggérez à personne dans
une issue.

---

## 2. Mettre en place l'environnement

### 2.1 Sous Linux, macOS ou WSL

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
```

### 2.2 Sous Windows

**La suite de tests complète ne tourne pas sous Windows.**
`pytest-homeassistant-custom-component` s'enregistre comme *entry point* pytest
et importe `fcntl`, qui n'existe pas. Ce n'est pas contournable : le plugin est
chargé avant toute lecture de `conftest.py`.

Deux options.

La moitié de la suite qui ne dépend pas de Home Assistant s'exécute quand même
en désactivant le plugin — et c'est précisément cette moitié qui porte le
portail à 100 % : le limiteur, l'ordonnanceur, la passerelle, le détecteur de
changements, les DTO et l'estimateur de budget.

```powershell
pytest tests -p no:homeassistant
```

Attention : cette invocation désactive aussi le garde-fou réseau du plugin.
N'écrivez jamais un test qui sortirait sur le réseau, et vérifiez toujours vos
changements avec la suite complète avant d'ouvrir une PR.

Cette moitié est fragile d'une façon qu'il faut connaître, parce qu'une seule
erreur de collecte avorte **tout** le lot — y compris les modules qui n'ont
aucun besoin de Home Assistant. `REQUIRES_HASS` suffit dans le cas courant : un
marqueur de saut est consulté avant qu'aucune fixture ne soit construite. Il ne
suffit pas si le module paramètre en `indirect=True`, car pytest résout un
paramètre indirect contre la fermeture de fixtures **à la collecte**, avant tout
marqueur ; un tel module doit se sauter au niveau du module
(`pytest.skip(..., allow_module_level=True)`), comme `test_todo.py`. Et
`importorskip("homeassistant")` ne remplace ni l'un ni l'autre : sous Windows ce
paquet s'importe parfaitement, c'est le *harnais* qui manque.

Pour la suite complète, passez par Docker :

```bash
docker run --rm -v "$PWD:/work" -w /work python:3.14 bash -c \
  "pip install -q -r requirements_test.txt && python -m pytest tests"
```

Sous Git Bash pour Windows, préfixez par `MSYS_NO_PATHCONV=1` et donnez le
chemin en absolu, sinon MSYS réécrit `/work` :

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:/chemin/vers/ha-carnet-scolaire:/work" -w /work \
  python:3.14 bash -c "pip install -q -r requirements_test.txt && python -m pytest tests"
```

Construire une image une fois pour toutes évite de réinstaller les dépendances
à chaque exécution — la pile de test de Home Assistant est volumineuse.

### 2.3 `graft`, l'index de code (facultatif)

Le dépôt est indexé par [graft](https://github.com/trailhq/Graft) (licence MIT,
publié sur npm sous `@nanonets/graft`) : un graphe du code en fiches markdown,
une par fichier, qui nomment le `fichier:ligne` de chaque symbole et les arêtes
« qui appelle quoi ». Un agent de code y lit en une requête ce que plusieurs
lectures de fichiers coûteraient en milliers de jetons — et, plus utile ici, les
arêtes répondent à « qui appelle ce symbole » sans qu'on ait à faire confiance à
un `grep` sur un nom court.

Aucun portail n'en dépend, et rien dans `custom_components/` ne le connaît. Qui
ne l'installe pas travaille exactement comme avant : ce que le dépôt versionne,
c'est la configuration, jamais l'index.

```bash
npm install -g @nanonets/graft
graft build          # construit graft/ à partir des sources, ~11 Mo
```

Quatre points sur ce que le dépôt porte réellement.

**`graft/` n'est pas versionné.** L'index se régénère intégralement à partir du
code. Le committer reviendrait à verser onze mégaoctets dérivés, à les
reconstruire dans chaque PR et à arbitrer des conflits sur un artefact ;
`.gitignore` l'exclut donc, et les commandes `graft` rafraîchissent le graphe
d'elles-mêmes avant de répondre.

**`.ignore` le réadmet à la recherche.** `ripgrep` lit `.ignore` avant
`.gitignore` : sans ce fichier, un `rg` dans le dépôt ne verrait aucune fiche,
alors qu'elles sont précisément faites pour être lues. Il ne réadmet que la
recherche, jamais le suivi de version.

**`.claude/` configure Claude Code, et rien d'autre.** `settings.json` y
enregistre les hooks et la ligne de statut de graft, `skills/graft/SKILL.md` dit
quand employer laquelle des six commandes, et `.mcp.json` déclare le serveur MCP
— que le client demande d'activer explicitement au premier lancement. Les
préférences propres à un poste vont dans `.claude/settings.local.json`, qui
n'est pas versionné. Les deux fichiers de `.claude/helpers/` ne sont que des
amorces : ils cherchent le paquet installé et n'ont aucun effet en son absence.
L'installeur de graft y grave le chemin absolu de la machine qui l'a lancé, ce
que ce dépôt neutralise à chaque fois — un chemin d'un autre poste n'aide
personne, et porte un nom de compte.

**La règle §1.1 tient sans aménagement.** graft n'indexe que ce qui est déjà
suivi en version, où aucun identifiant réel ne figure ; il suit `.gitignore`,
donc l'index couvre `custom_components/`, `scripts/` et `tests/`, et jamais
`tests/fixtures/_raw/`. Une fiche reste néanmoins du contenu dérivé du dépôt :
elle se relit avant d'être collée dans un ticket, comme n'importe quel extrait.

---

## 3. Les portails

Ce sont exactement ceux que la CI applique
(`.github/workflows/validate.yml`). Faites-les passer avant d'ouvrir une PR :
ils échouent plus vite chez vous que dans un *runner*.

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/carnet_scolaire
python scripts/check_manifest.py
pytest tests --cov=custom_components/carnet_scolaire --cov-branch --cov-report=xml:coverage.xml
python scripts/check_coverage.py coverage.xml
```

### 3.1 La couverture

Le portail exige **95 % sur chaque module**, sans liste d'exceptions, **80 %**
globalement en garde-fou plus faible, et **100 %** sur sept modules.

Les 95 % par module sont la règle `test-coverage` de l'échelle de qualité Home
Assistant (palier argent). Ce n'est pas une moyenne déguisée : quatre modules
de ce paquet dépassent les cinq cents instructions, donc un module de quarante
peut tomber à zéro sans faire bouger le chiffre global d'un point. La liste des
modules est **lue dans le rapport de couverture**, jamais écrite dans le
script — un module ajouté demain est tenu au seuil sans que personne ait à
penser à l'inscrire.

Les sept modules tenus à 100 % :

| Module | Pourquoi 100 % |
| --- | --- |
| `ratelimit.py` | un limiteur qui laisse passer un appel ne produit aucun symptôme visible — jusqu'à la sanction |
| `scheduler.py` | un palier sauté ressemble à des données un peu vieilles |
| `gateway.py` | un décodage faux produit une valeur plausible |
| `delta.py` | un changement manqué, c'est un évènement qui n'arrive jamais |
| `ed_client.py` | un code métier Ecoledirecte mal classé relogue hors du limiteur |
| `ed_limiter.py` | même raison que `ratelimit.py`, sur l'autre source |
| `ed_mapping.py` | même raison que `gateway.py` : une valeur plausible et fausse |

Pour chacun, la métrique retenue est le **minimum de la couverture de lignes et
de branches** : 100 % de lignes avec une branche manquante ne passe pas.

L'inventaire des règles [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)
vit dans `custom_components/carnet_scolaire/quality_scale.yaml`. Une intégration
HACS n'affiche pas de palier bronze/argent/or/platine ; le fichier sert à
suivre ce qui est fait, exempté, ou encore ouvert. Ne marquez pas une règle
`done` sans que le code (ou une exemption commentée) le tienne.

### 3.2 La documentation

```bash
pip install -r requirements_docs.txt
mkdocs serve           # aperçu local
mkdocs build --strict  # ce que la CI exige
```

`--strict` échoue sur un lien interne ou une ancre qui ne résout plus. Les
documents utilisent les *slugs* de GitHub — accents compris — pour se lire
identiquement sur GitHub et sur le site.

**Citez un symbole, jamais un numéro de ligne.** La documentation renvoie au
code par le nom de ce qu'elle décrit et le fichier qui le porte —
« `async_request_tick`, `account.py` » — et non par une plage de lignes. La
règle vient d'un dégât mesuré : sur cent cinquante-deux plages citées, soixante
et onze pointaient sur du code qui avait bougé, et les quatorze premières
ouvertes étaient toutes fausses. Une plage fausse est plus coûteuse qu'une
citation absente, parce qu'elle atterrit sur du code réel : le lecteur essaie de
le réconcilier avec une affirmation qui, elle, est restée juste, et conclut
contre l'affirmation. Un nom de symbole survit à un déplacement de fonction, se
retrouve par recherche, et `scripts/check_doc_citations.py` reste là pour les
plages qui subsistent.

L'exception est `docs/revue-contradictoire-v1.md`, qui cite `pronotepy` 2.15.6
par plages : c'est une archive de revue, ses citations portent sur du code
extérieur au dépôt, et un avertissement en tête du document le dit.

---

## 4. Ce qui se génère et ne s'édite pas

Cinq fichiers sont produits par un script. Les modifier à la main casse un
test qui compare le fichier à ce que le générateur produit.

| Fichier | Source |
| --- | --- |
| `custom_components/carnet_scolaire/strings.json` | `scripts/build_translations.py` |
| `custom_components/carnet_scolaire/translations/en.json` | idem |
| `custom_components/carnet_scolaire/translations/fr.json` | idem |
| `custom_components/carnet_scolaire/services.yaml` | idem |
| `custom_components/carnet_scolaire/icons.json` | idem |

Modifiez la table dans le script, puis :

```bash
python scripts/build_translations.py
```

**Rien de visible par l'utilisateur n'est écrit en dur sous
`custom_components/`.** C'est ce qui rend réel le test qui vérifie que les deux
langues ont les mêmes clés et les mêmes espaces réservés : trois fichiers JSON
maintenus à la main gardent ce test rouge pour des raisons sans intérêt.

Deux détails qui font échouer hassfest si on les ignore : une chaîne de
traduction **ne peut pas contenir d'URL** (passez-la en
`description_placeholders`), et les clés de `manifest.json` doivent être
ordonnées `domain`, `name`, puis alphabétiquement.

---

## 5. La frontière `pronotepy`

Deux modules seulement voient la bibliothèque amont : `gateway.py` et
`hardened_client.py`. Tout le reste manipule des DTO gelés (`frozen`, `slots`)
définis dans `models.py`.

Ce n'est pas une préférence esthétique. `pronotepy` expose des propriétés
paresseuses — `Information.content`, `Period.grades`, `Discussion.messages` —
dont chaque lecture **place une requête HTTP**. Un objet amont qui s'échappe
vers un capteur, c'est un appel réseau depuis la boucle d'évènements, hors de
tout comptage du limiteur. La frontière DTO l'exclut par construction.

Corollaires, si vous touchez à ces deux modules :

- une `GatewayResult` déclare son coût réel en requêtes (`calls`), et la
  session réconcilie ce coût avec le limiteur ; une déclaration fausse est un
  bug, pas une approximation ;
- une entrée illisible coûte **cette entrée**, jamais la collecte entière —
  mais une **clé absente** fait échouer le palier, parce que « absent » et
  « vide » ne sont pas la même information et qu'un changement de protocole
  doit se voir ;
- `hardened_client.py` existe pour corriger des bugs de l'amont, avec la
  version épinglée `pronotepy==2.15.7`. Si vous relevez cette épingle, relisez
  chaque divergence documentée dans ce fichier.

### 5.1 L'épingle est surveillée

Un matin, toutes les connexions à un établissement ont échoué sur un
`CryptoError` que l'amont commente « *probably the qr code has expired* ». Le QR
code était parfaitement valide : le serveur PRONOTE de l'établissement venait
de passer en 26.2.5, et `pronotepy` avait publié six jours plus tôt une version
dont un commit s'appelle littéralement *fix compatibility with PRONOTE
2026.2.5.6* et réécrit trois lignes de l'échange de défi dans `_login`. Le
correctif existait, il était public, et rien dans ce dépôt ne le disait — c'est
la seule raison pour laquelle la panne a duré une matinée.

`.github/workflows/pronotepy-watch.yml` le dit désormais. Une fois par semaine,
il compare l'épingle à ce que PyPI publie et, si l'amont est devant, **ouvre une
pull request qui relève l'épingle** sur la branche
`chore/pronotepy-<version>`. Pas une issue : une issue annonce qu'une version
existe, une pull request dit si elle *fonctionne*, parce que `Validate` y fait
tourner la suite entière et `mypy --strict` contre la nouvelle bibliothèque sur
les deux socles. Ce matin-là, cette réponse aurait été disponible quelques
minutes après la publication amont au lieu d'exiger de refaire l'installation à
la main.

Une branche par version amont, ce qui rend le job idempotent : une deuxième
exécution retrouve la branche et met la pull request à jour au lieu d'en
empiler une par semaine, et une version *plus récente* obtient sa propre
branche plutôt que d'écraser une proposition en cours de lecture. Si la branche
existe déjà, ses commits ne sont pas réécrits — quelqu'un a pu y ajouter le
correctif que la montée de version demandait, et le perdre serait exactement le
contraire du but.

La pull request porte la version épinglée, la nouvelle version, sa date de
publication et **les sujets de commit entre les deux tags**. Ce dernier point
est l'essentiel : ce qui a permis de conclure ce matin-là n'était pas un numéro
de version, c'était une ligne de commit. Un rapport qui obligerait à aller la
chercher n'aurait fait gagner la matinée à personne. Si la convention de
nommage des tags amont change, la comparaison ne se résout plus et la pull
request le dit, avec les seules informations de PyPI ; si PyPI est
inaccessible, le job le note et sort au vert. Un portail hebdomadaire qui
rougit parce qu'un tiers a hoqueté est un portail qu'on apprend à ignorer.

**Rien de tout cela ne se fusionne tout seul**, et aucune fusion automatique
n'est activée. La règle ci-dessus s'applique sans exception : relever l'épingle
oblige à relire **chaque** divergence documentée dans `hardened_client.py`,
parce qu'un correctif amont peut en avoir rendu une inutile, ou fausse. La CI
voit qu'un contournement ne casse rien, pas qu'il est devenu superflu — c'est la
seule chose qui distingue une montée de version d'une régression silencieuse, et
elle ne se délègue pas. La pull request le rappelle avec une case à cocher, et
la relecture est le travail réel que la proposition demande.

Deux détails de fonctionnement, pour ne pas les redécouvrir en lisant les
journaux. GitHub ne déclenche pas de workflow à partir d'un évènement produit
par `GITHUB_TOKEN` — sinon un job pourrait s'auto-relancer sans fin — donc la
pull request naît sans vérifications, et le job demande `Validate`
explicitement sur la branche (c'est la raison du `workflow_dispatch` ajouté à
`validate.yml`). Et si le dépôt interdit à Actions d'ouvrir une pull request,
le rapport arrive quand même sous forme d'issue préfixée `[pronotepy-upstream]`
en disant pourquoi : le silence est le défaut qu'on corrige ici, pas la forme
du rapport.

Enfin, la version est épinglée à deux endroits — `requirements_test.txt`, ce
que la suite teste, et `manifest.json`, ce que Home Assistant installe chez
l'utilisateur. Les deux bougent ensemble, dans la proposition comme à la main,
et `scripts/check_manifest.py` échoue si elles divergent : une divergence
signifie que les tests n'exercent pas la bibliothèque que les utilisateurs font
tourner, ce qui rend précisément invérifiables les corrections de
`hardened_client.py`.

---

## 6. Le limiteur

Toute modification qui ajoute un appel réseau doit passer par le limiteur, et
déclarer son coût. Les trois couches — espacement minimal, seau à jetons,
plafond journalier — plus les deux compteurs de connexion sont **débités à
l'admission**, sous un verrou, jamais à la sortie.

La raison est mesurable : une connexion coûte cinq à sept requêtes et la
poignée de main PRONOTE est lente. Facturée au retour, elle laisse une fenêtre
de plusieurs secondes pendant laquelle un second appelant lit un budget intact
— c'est ainsi qu'un plafond de cinq connexions en laisse passer dix.

Si votre changement modifie le budget par défaut (≈ 180 requêtes par jour pour
un enfant), dites-le explicitement dans la PR et mettez à jour
`docs/annexe-b-rate-limit.md`.

---

## 7. Écrire des tests

Les tests de ce dépôt suivent trois conventions.

**Un nom de test est une phrase.** `test_a_login_is_charged_before_the_handshake_returns`,
pas `test_login_charge`. Le lecteur suivant doit comprendre ce qui est protégé
sans lire le corps.

**Une docstring dit pourquoi le test existe.** Idéalement quel défaut il
empêche de revenir. Un test dont personne ne sait à quoi il sert est un test
que quelqu'un supprimera pour faire passer la CI.

**Une seule couture est bouchonnée.** La *fixture* `account` de
`tests/conftest.py` remplace `session.build_client` et fait tourner tout le
reste pour de vrai — limiteur, ordonnanceur, session, passerelle,
coordinateurs et les sept plateformes ensemble. Les *fixtures* de
`tests/fixtures/protocol.py` rendent les dictionnaires bruts du protocole, que
les vraies classes `pronotepy.dataClasses` décodent : la fidélité est
structurelle, pas déclarative.

Si vous ajoutez une méthode à `FakeClient`, faites-lui compter ses requêtes
comme la vraie bibliothèque les place. Un double qui sous-compte rend le
contrat de coût indémontrable.

---

## 8. Ouvrir une pull request

`main` n'est jamais poussée en direct. Tout changement arrive par une *pull
request* depuis une branche de sujet. Un `git push origin main` est une erreur,
pas un raccourci.

1. Une branche par sujet, depuis `main`.
2. Les portails de la section 3 au vert **en local**.
3. Un message de commit qui dit ce qui change **et pourquoi**. Si vous
   corrigez un bug, décrivez le symptôme qu'un utilisateur voyait ; c'est ce
   qui permet à quelqu'un de juger si votre correction est la bonne.
4. Pousser **la branche**, ouvrir la PR.
5. Les quatre workflows (`Validate`, `Hassfest`, `HACS`, `Docs`) au vert sur la
   PR.

N'incrémentez **pas** la version dans `manifest.json` : c'est fait au moment de
la publication, et `release.yml` refuse un tag dont le manifeste diverge.

---

## 9. Publier une version

Réservé aux mainteneurs.

1. Porter `manifest.json` à la version voulue, sans le `v`.
2. Fusionner la *pull request* sur `main`, workflows au vert.
3. `git tag -a vX.Y.Z && git push origin vX.Y.Z`.

`release.yml` vérifie que le manifeste et le tag concordent — une intégration
HACS dont le manifeste diverge du tag s'installe une fois et ne se met plus
jamais à jour — puis construit `carnet_scolaire.zip` et publie la *release*.

---

## 10. Signaler un problème

Utilisez les formulaires d'issue : ils demandent exactement ce qu'il faut et,
surtout, ils rappellent ce qu'il ne faut **pas** coller.

Le fichier de diagnostic (*Paramètres → Appareils et services → Carnet scolaire →
⋯ → Télécharger les diagnostics*) est conçu pour être joint à une issue
publique : les identifiants y sont rédigés, l'adresse retaillée, et les
identifiants d'élève remplacés par une empreinte tronquée. C'est presque
toujours la pièce jointe la plus utile.

En revanche, **relisez toujours** ce que vous collez. Un extrait de journal, un
message d'erreur brut ou une capture d'écran ne passent par aucune rédaction.

---

## 11. Où lire la suite

| Document | Contenu |
| --- | --- |
| [`docs/GUIDE-UTILISATEUR.md`](docs/GUIDE-UTILISATEUR.md) | ce que voit l'utilisateur : entités, services, réglages, dépannage |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | les couches, le limiteur, l'ordonnanceur, la session, et pourquoi c'est construit ainsi |
| [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) | le raisonnement d'origine — à lire avec le §12 de l'architecture, qui liste où le code s'en écarte |
| [`docs/annexe-b-rate-limit.md`](docs/annexe-b-rate-limit.md) | l'arithmétique du budget de requêtes |
| [`docs/revue-contradictoire-v1.md`](docs/revue-contradictoire-v1.md) | la revue qui a produit la v2, conservée parce que ses raisons valent mieux que ses conclusions |

Le tout est publié sur <https://fiveelements.github.io/ha-carnet-scolaire/>.

Sous licence [MIT](LICENSE).
