<p align="center"><img src="docs/assets/banner.jpg" alt="Carnet scolaire" width="100%"></p>

# Carnet scolaire — PRONOTE et EcoleDirecte pour Home Assistant
[![Version](https://img.shields.io/github/v/release/FiveElements/ha-carnet-scolaire?style=flat-square&label=version)](https://github.com/FiveElements/ha-carnet-scolaire/releases)
![Home Assistant 2026.10.0 minimum](https://img.shields.io/badge/Home%20Assistant-2026.10.0%2B-41BDF5?style=flat-square&logo=homeassistant&logoColor=white)
[![validate](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/validate.yml?branch=main&style=flat-square&label=validate)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/validate.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/codeql.yml?branch=main&style=flat-square&label=CodeQL)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/codeql.yml)
[![Licence](https://img.shields.io/github/license/FiveElements/ha-carnet-scolaire?style=flat-square&label=licence)](LICENSE)
[![Alertes CVE](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/cve.yml?branch=main&style=flat-square&label=alertes%20CVE)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/cve.yml)
[![Couverture des tests](https://img.shields.io/badge/couverture-%E2%89%A595%20%25%20par%20module-brightgreen?style=flat-square)](scripts/check_coverage.py)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/FiveElements/ha-carnet-scolaire/badge)](https://scorecard.dev/viewer/?uri=github.com/FiveElements/ha-carnet-scolaire)
[![HACS](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/hacs.yml?branch=main&style=flat-square&label=HACS)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/hacs.yml)
[![hassfest](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/hassfest.yml?branch=main&style=flat-square&label=hassfest)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/hassfest.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?style=flat-square)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Téléchargements](https://img.shields.io/github/downloads/FiveElements/ha-carnet-scolaire/total?style=flat-square&label=t%C3%A9l%C3%A9chargements)](https://github.com/FiveElements/ha-carnet-scolaire/releases)
[![Téléchargements de la dernière version](https://img.shields.io/github/downloads/FiveElements/ha-carnet-scolaire/latest/total?style=flat-square&label=derni%C3%A8re%20version)](https://github.com/FiveElements/ha-carnet-scolaire/releases/latest)
[![Dependabot](https://img.shields.io/badge/Dependabot-activ%C3%A9-025E8C?style=flat-square&logo=dependabot&logoColor=white)](.github/dependabot.yml)

 **Carnet scolaire** est une intégration [Home Assistant](<https://www.home-assistant.io/>) permettant d'intégrer **PRONOTE** et **EcoleDirecte** directement dans votre installation domotique.

 Retrouvez dans Home Assistant les informations scolaires de vos enfants : **emploi du temps, devoirs, notes, moyennes, absences, retards, évaluations, actualités, discussions, menus et bien plus encore.**

 L'intégration est conçue pour les élèves comme pour les parents utilisant un compte PRONOTE, EcoleDirecte ou un ENT.
 > 🏫 **Votre établissement utilise une autre plateforme ?** Carnet scolaire est conçu pour accueillir plusieurs sources : PRONOTE et EcoleDirecte sont les deux premières. Si l'ENT ou le logiciel de vie scolaire de vos enfants n'est pas encore pris en charge, [ouvrez un ticket](https://github.com/FiveElements/ha-carnet-scolaire/issues/new?template=feature_request.yml&title=%5Bnouvelle%20source%5D%20) en indiquant la plateforme, le type de compte (parent ou élève) et les données qui vous seraient utiles. Chaque demande aide à choisir la prochaine source à intégrer.


<p align="center">
  <a href="https://fiveelements.github.io/ha-carnet-scolaire/">📖 Documentation</a>
  ·
  <a href="https://fiveelements.github.io/ha-carnet-scolaire/GUIDE-UTILISATEUR/">📚 Guide de l'utilisateur</a>
  ·
  <a href="https://github.com/FiveElements/ha-carnet-scolaire/issues/new/choose">🐛 Signaler un problème</a>

</p>
 

 ## ✨ Fonctionnalités

 ### 📅 Vie scolaire

 - Emploi du temps
- Prochains cours
- Devoirs
- Notes et moyennes
- Évaluations par compétences
- Absences
- Retards
- Punitions
- Bulletins
- Actualités
- Discussions
- Menus de cantine
- Informations sur le personnel

 ### 🤖 Automatisations Home Assistant

 Carnet scolaire s'intègre nativement dans le moteur d'automatisation de Home Assistant.

 Vous pouvez notamment créer des automatisations lorsqu'une nouvelle note apparaît, lorsqu'un devoir est ajouté ou lorsqu'un événement scolaire change.

 L'intégration fournit :

 - 14 déclencheurs d'appareil
- 10 conditions
- 4 actions
- [8 blueprints prêts à l'emploi](https://fiveelements.github.io/ha-carnet-scolaire/BLUEPRINTS/)
- des blueprints disponibles en français et en anglais

 Aucune programmation en YAML n'est nécessaire pour les automatisations courantes.


 ### 👨‍👩‍👧‍👦 Comptes parents

 Les comptes parents avec plusieurs enfants sont pris en charge.

 Chaque enfant est représenté comme un appareil Home Assistant distinct, tout en partageant la même session PRONOTE et le même budget de requêtes.

 ### 🔐 Connexion sécurisée

 Trois méthodes de connexion sont disponibles :

 1. **QR code — recommandé**
2. Identifiant et mot de passe
3. Connexion via ENT

 L'enrôlement par QR code permet à Home Assistant de s'enregistrer comme appareil auprès de PRONOTE sans avoir à conserver votre mot de passe.

 ### 🛡️ Protection contre les requêtes excessives

 PRONOTE peut limiter les requêtes provenant d'une même adresse IP.

 Carnet scolaire intègre donc un limiteur de requêtes à plusieurs niveaux afin d'éviter les sollicitations excessives :

 - espacement minimal entre les requêtes
- seau à jetons horaire
- plafond quotidien
- compteurs de connexion séparés
- cadence configurable des différentes collectes

 Le budget par défaut est d'environ **180 requêtes par jour et par enfant**.

 L'intégration estime également le coût des réglages avant leur application afin de rendre leur impact visible.

---

 ## 🚀 Installation

 ### Avec HACS

 Carnet scolaire s'installe avec [HACS](https://www.hacs.xyz/) comme dépôt personnalisé.

**1. Ajouter le dépôt à HACS et télécharger l'intégration**

[![Ouvrir ce dépôt dans HACS sur votre Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=FiveElements&repository=ha-carnet-scolaire&category=integration)

 Ce bouton ouvre **votre** Home Assistant sur la fiche du dépôt dans HACS. Il ne
reste qu'à **Télécharger**, puis à redémarrer Home Assistant.

**2. Ajouter l'intégration**

[![Ouvrir votre Home Assistant et commencer la configuration d'une nouvelle intégration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=carnet_scolaire)

 Le formulaire demande d'abord **comment** vous vous connectez — QR code,
identifiants, ou ENT. Le [guide de l'utilisateur](https://fiveelements.github.io/ha-carnet-scolaire/GUIDE-UTILISATEUR/#2-se-connecter--les-trois-modes)
décrit les trois modes et dit lequel choisir.

<details>
<summary>Ajouter le dépôt à la main, si les boutons n'aboutissent pas</summary>

1. Ouvrez **HACS**
2. Ouvrez le menu **⋮** puis **Dépôts personnalisés**
3. Ajoutez `https://github.com/FiveElements/ha-carnet-scolaire`
4. Sélectionnez la catégorie **Intégration**, puis **Ajouter**
5. Installez **Carnet scolaire**, puis redémarrez Home Assistant
6. **Paramètres → Appareils et services → Ajouter une intégration**, cherchez **PRONOTE**

</details>

 > 💡 Une intégration HACS officielle pourra être utilisée lorsqu'elle sera disponible. En attendant, l'installation par dépôt personnalisé permet d'utiliser Carnet scolaire directement depuis HACS.

 ### Installation manuelle

 Copiez le dossier :

```
custom_components/carnet_scolaire/
```

 dans le dossier `custom_components/` de votre configuration Home Assistant, puis redémarrez Home Assistant.

 #### Version minimale

 Carnet scolaire nécessite actuellement :

```
Home Assistant 2026.10.0 ou supérieur
```

---

 ## 📊 Afficher les données PRONOTE

 Carnet scolaire fournit des entités Home Assistant natives :

 - `sensor`
- `binary_sensor`
- `calendar`
- `todo`
- `image`
- `event`
- `button`

 Vous pouvez donc utiliser les cartes natives de Home Assistant pour construire votre propre tableau de bord.

 La documentation explique également comment créer un tableau de bord complet à partir des données PRONOTE.

 👉 [Afficher les données PRONOTE dans Home Assistant](https://fiveelements.github.io/ha-carnet-scolaire/AFFICHER-LES-DONNEES/)

---

 ## 🎨 Cartes Lovelace

 Des cartes Lovelace spécialement conçues pour Carnet scolaire sont disponibles dans un dépôt séparé :

 **[FiveElements/ha-carnet-scolaire-cards](https://github.com/FiveElements/ha-carnet-scolaire-cards)** — [documentation](https://fiveelements.github.io/ha-carnet-scolaire-cards/), une page par carte.

 Elles permettent notamment d'afficher :

 - 👨‍🎓 [Informations sur l'élève](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/eleve/)
- 📅 [Prochain cours](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/prochain-cours/)
- 🌅 [Vue journée](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/journee/)
- 🗓️ [Emploi du temps](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/emploi-du-temps/)
- 📝 [Devoirs](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/devoirs/)
- 📊 [Notes](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/notes/)
- 🎯 [Évaluations](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/evaluations/)
- 🍽️ [Cantine](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/menu/)
- 🏫 [Vie scolaire](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/vie-scolaire/)
- 🚦 [État du limiteur](https://fiveelements.github.io/ha-carnet-scolaire-cards/cartes/limiteur/)

 Les cartes sont **optionnelles** : Carnet scolaire fonctionne avec les cartes natives de Home Assistant.

---

 ## 🔧 Services et actions

 Carnet scolaire fournit plusieurs services Home Assistant.

 Certains services permettent notamment d'accéder à :

 - l'URL iCal de l'emploi du temps
- l'identité de l'élève
- l'emploi du temps au format PDF
- l'état du limiteur de requêtes

 Le [catalogue des entités et des services](https://fiveelements.github.io/ha-carnet-scolaire/annexe-a-entites/) donne, pour chaque champ publié, d'où il vient.

 Des opérations d'écriture sont également disponibles, **activées à l'installation** et désactivables dans les options :

 - cocher un devoir
- marquer une actualité comme lue
- envoyer un message

---

 ## 🔒 Vie privée et sécurité

 La protection des données PRONOTE est une priorité du projet.

 Les informations sensibles ne sont pas stockées dans les états des entités ou dans leurs attributs.

 Cela concerne notamment :

 - l'URL iCal
- les informations d'identité
- le lien vers le PDF de l'emploi du temps
- les informations d'authentification

 Les données sensibles sont fournies uniquement lorsqu'elles sont explicitement demandées par un service.

 Le fichier de diagnostic ne contient pas le contenu des données scolaires. Les identifiants d'élèves y sont également protégés.

 Le détail de ce qui est conservé, de ce qui ne l'est pas et de ce que contient
le fichier de diagnostic est au [§ 11 du guide](https://fiveelements.github.io/ha-carnet-scolaire/GUIDE-UTILISATEUR/#11-vie-privée).

 > ⚠️ **Ne configurez jamais le journaliseur `pronotepy` en niveau DEBUG.**
>
>  Le mode DEBUG de `pronotepy` peut contenir des informations sensibles provenant des requêtes PRONOTE.

---

 ## 🧠 Pourquoi Carnet scolaire ?

 Carnet scolaire a été conçu autour de quelques principes importants.

 ### Ne jamais confondre une absence de données avec une donnée vide

 Une liste vide peut être parfaitement valide : par exemple, une semaine sans devoirs.

 À l'inverse, la disparition inattendue d'une donnée obligatoire peut indiquer un changement du protocole PRONOTE.

 Carnet scolaire distingue donc ces deux situations afin d'éviter de publier silencieusement des données incorrectes dans Home Assistant.

 ### Une session partagée pour plusieurs enfants

 Avec un compte parent, chaque enfant dispose de son propre appareil Home Assistant, tout en partageant la même session PRONOTE.

 Cela évite de multiplier inutilement les connexions.

 ### Des événements utilisables directement dans les automatisations

 Les événements permettent de réagir précisément aux changements.

 Par exemple :

 > « Une nouvelle note vient d'être publiée en mathématiques. »

 plutôt que simplement :

 > « Le nombre de notes a changé. »

 Cela permet de créer des automatisations Home Assistant plus fiables et plus simples.

---

 ## 📚 Documentation

 La documentation complète est disponible ici :

 **[📖 Documentation Carnet scolaire](https://fiveelements.github.io/ha-carnet-scolaire/)**

 Elle contient notamment :

 - [Guide utilisateur](https://fiveelements.github.io/ha-carnet-scolaire/GUIDE-UTILISATEUR/) — installation, entités, services, réglages, dépannage
- [Architecture de l'intégration](https://fiveelements.github.io/ha-carnet-scolaire/ARCHITECTURE/)
- [Catalogue des entités et services](https://fiveelements.github.io/ha-carnet-scolaire/annexe-a-entites/) — l'origine de chaque champ publié
- [Configuration du limiteur de requêtes](https://fiveelements.github.io/ha-carnet-scolaire/annexe-b-rate-limit/) — couches, options, arithmétique du budget
- [Création de tableaux de bord](https://fiveelements.github.io/ha-carnet-scolaire/AFFICHER-LES-DONNEES/)
- [Blueprints](https://fiveelements.github.io/ha-carnet-scolaire/BLUEPRINTS/) — chaque réglage des huit blueprints, et le piège que chacun évite
- [Exemples d'utilisation](https://fiveelements.github.io/ha-carnet-scolaire/EXEMPLES-BLUEPRINTS/) — une automatisation complète et deux variantes d'action par blueprint
- [Documentation technique pour les contributeurs](CONTRIBUTING.md) — et la [spécification](https://fiveelements.github.io/ha-carnet-scolaire/SPECIFICATION/)

---

 ## 🤝 Contribuer

 Les contributions, rapports de bugs et suggestions sont les bienvenus.

 Avant d'ouvrir une issue :

 - vérifiez [la documentation](https://fiveelements.github.io/ha-carnet-scolaire/)
- utilisez les [formulaires d'issue](https://github.com/FiveElements/ha-carnet-scolaire/issues/new/choose) : ils demandent ce qu'il faut, et rappellent ce qu'il ne faut **pas** coller dans une issue publique
- lisez [`CONTRIBUTING.md`](CONTRIBUTING.md) avant une *pull request*
- ne partagez jamais d'identifiants PRONOTE réels
- ne joignez jamais de données scolaires personnelles
- ne joignez jamais de logs `pronotepy` en mode DEBUG

 👉 [Créer une issue](https://github.com/FiveElements/ha-carnet-scolaire/issues/new/choose)

---

 ## ⚠️ Important

 PRONOTE est un service tiers.

 **Carnet scolaire n'est pas développé, maintenu ou officiellement supporté par Index Éducation / PRONOTE.**

 Le fonctionnement de l'intégration dépend de l'API et du comportement de PRONOTE. Une évolution de PRONOTE ou de l'ENT utilisé par votre établissement peut nécessiter une adaptation de l'intégration.

---


 ## ⭐ Vous utilisez Carnet scolaire ?

 Si cette intégration vous est utile :

 - ⭐ ajoutez une étoile au projet GitHub
- 🐛 signalez les problèmes rencontrés
- 💡 proposez des améliorations
- 📖 contribuez à la documentation
- 📣 partagez le projet avec d'autres utilisateurs de Home Assistant

 Chaque étoile et chaque contribution aide le projet à être découvert par les utilisateurs qui recherchent une intégration **PRONOTE ou EcoleDirecte pour Home Assistant**.


 ## 📄 Licence

 Carnet scolaire est distribué sous licence [MIT](LICENSE).

---


