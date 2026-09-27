---
hide:
  - navigation
---

# Carnet scolaire

![Carnet scolaire](assets/banner.jpg){ .carnet-banniere }

**Intégration Home Assistant pour PRONOTE et EcoleDirecte.**

[![Validate](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/validate.yml?branch=main&label=validate&logo=github)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/validate.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/hassfest.yml?branch=main&label=hassfest&logo=homeassistant&logoColor=white)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/hassfest.yml)
[![HACS](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-carnet-scolaire/hacs.yml?branch=main&label=HACS)](https://github.com/FiveElements/ha-carnet-scolaire/actions/workflows/hacs.yml)
![Home Assistant 2026.9.0 minimum](https://img.shields.io/badge/Home%20Assistant-2026.9.0%2B-41BDF5?logo=homeassistant&logoColor=white)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-green)](https://github.com/FiveElements/ha-carnet-scolaire/blob/main/LICENSE)

Ce qui la caractérise, en quatre points :

- **Le coût est affiché avant d'être dépensé.** Chaque page de réglages montre
  l'estimation du nombre de requêtes quotidiennes que les valeurs saisies vont
  coûter, calculée par la fonction même qui produit les chiffres de
  [l'annexe B](annexe-b-rate-limit.md) — un réglage dont on ne voit pas la
  conséquence se règle au hasard. En dessous : un limiteur à trois étages, deux
  compteurs de connexion tenus à part, dix paliers de collecte ayant chacun sa
  cadence, et un seul battement maître.
- **Aucun secret ne devient un état.** L'URL iCal, le bloc d'identité et le
  lien du PDF d'emploi du temps sont des réponses de service, que rien ne
  retient. Le code PIN à deux facteurs n'est pas persisté, et le fichier de
  diagnostic rapporte la *forme* de chaque collecte — son âge, son coût, son
  nombre d'éléments — jamais son contenu.
- **« Absent » n'est pas « vide ».** Quand une clé que le protocole garantit
  disparaît de la réponse, le palier échoue et garde son instantané précédent,
  au lieu de publier une collecte réussie et vide : une liste vide est une
  information plausible, une rupture de protocole doit se voir.
- **Un appareil par enfant, une seule session pour tous.** Sur un compte
  parent, la session et le budget de requêtes sont partagés, et la sélection de
  l'enfant est indissociable de l'appel qui suit — l'unité de travail atomique
  est le couple *(enfant, palier)*, jamais le palier seul.

L'enrôlement recommandé se fait par **QR code** : Home Assistant s'inscrit
auprès de PRONOTE comme un appareil, le jeton obtenu tourne à chaque
authentification, et aucun mot de passe n'est conservé.

!!! tip "Par où commencer"

    * Vous voulez **installer et utiliser** l'intégration :
      [le guide de l'utilisateur](GUIDE-UTILISATEUR.md).
    * Vous voulez **contribuer**, ou comprendre pourquoi c'est construit ainsi :
      [l'architecture](ARCHITECTURE.md).
    * Une tuile est bloquée sur « Temporisation » ou « Bridé » et vous voulez
      savoir pourquoi : [le limiteur et ses six états](LIMITEUR.md).
    * Vous voulez le **raisonnement d'origine**, y compris ce qui s'est révélé
      faux : [la spécification](SPECIFICATION.md) et
      [la revue contradictoire](revue-contradictoire-v1.md).

## Ce que ça fait

- **Sept plateformes d'entités** : `sensor`, `binary_sensor`, `calendar`,
  `todo`, `image`, `event`, `button` — emploi du temps, devoirs, notes et
  moyennes, absences et retards, punitions, évaluations par compétences,
  actualités, discussions, menus, personnel, bulletins.
- **Neuf services**, dont cinq renvoient une réponse plutôt que d'alimenter
  une entité : l'URL iCal, l'identité, le PDF d'emploi du temps, l'adresse
  d'une pièce jointe et l'état du limiteur.
- **Écritures optionnelles**, activées pour une nouvelle installation et
  coupées d'un interrupteur : cocher un devoir, marquer une actualité comme
  lue, envoyer un message.
- **Automatisations d'appareil** : 14 déclencheurs, 10 conditions, 4 actions,
  plus huit blueprints livrés en français et en anglais.
- **Trois modes de connexion** : QR code (recommandé), identifiants directs,
  ENT.
- **Comptes parents multi-enfants**, chaque enfant étant un appareil distinct.

Tout n'est pas disponible partout : EcoleDirecte n'offre qu'une partie de ces
fonctions, et PRONOTE lui-même pose quelques limites. Le guide les rassemble
en trois sections — [ce qui est pris en charge](GUIDE-UTILISATEUR.md#13-ce-qui-est-pris-en-charge),
[les fonctions par source](GUIDE-UTILISATEUR.md#14-fonctions-fournies-par-source)
et [les limites connues](GUIDE-UTILISATEUR.md#15-limites-connues).

## Ce que ça protège

!!! warning "PRONOTE sanctionne une adresse IP, pas seulement un compte"

    C'est la contrainte qui a dicté l'architecture, et la raison pour laquelle
    la page d'options affiche un budget de requêtes plutôt qu'un simple
    intervalle.

Le limiteur de débit est au centre du design : trois couches — espacement
minimal, seau à jetons, plafond journalier — plus deux compteurs de connexion,
tous **débités à l'admission** sous un verrou, jamais à la sortie. Une
connexion coûte cinq à sept requêtes et la poignée de main PRONOTE est lente ;
facturée au retour, elle laisse une fenêtre de plusieurs secondes pendant
laquelle un second appelant lit un budget intact — c'est ainsi qu'un plafond de
cinq connexions en laisse passer dix.

Le budget par défaut est d'environ **180 requêtes par jour** pour un enfant.

Aucun secret ne transite par un état d'entité, un attribut ou le fichier de
diagnostic : ni l'URL iCal — qui donne accès à l'emploi du temps complet d'un
élève sans aucun identifiant — ni le bloc d'identité, ni le lien du PDF. Ce
sont des réponses de service, et rien ne les stocke.

## Installation

L'intégration s'appelle **Carnet scolaire** et son domaine Home Assistant est
**`carnet_scolaire`** : c'est le nom du dossier sous `custom_components/`, et le
préfixe de toutes ses entités et de tous ses services.

=== "HACS (dépôt personnalisé)"

    1. HACS → Intégrations → menu ⋮ → *Dépôts personnalisés*.
    2. Ajouter `https://github.com/FiveElements/ha-carnet-scolaire`, catégorie
       *Intégration*.
    3. Installer **Carnet scolaire**, puis redémarrer Home Assistant.
    4. *Paramètres → Appareils et services → Ajouter une intégration →
       PRONOTE*.

=== "Manuellement"

    Copier `custom_components/carnet_scolaire/` dans le dossier
    `custom_components/` de votre configuration, puis redémarrer.

Home Assistant **2026.9.0** minimum.

Le [guide de l'utilisateur](GUIDE-UTILISATEUR.md) reprend chaque étape, y
compris les trois modes de connexion et ce qu'il faut avoir sous la main avant
de commencer.

## Qualité

`ruff`, `mypy --strict`, plus de mille tests, et un portail de
couverture qui exige **95 % sur chaque module**, 80 % globalement, et
**100 %** sur le limiteur, l'ordonnanceur, la passerelle, le détecteur de
changements et les trois modules Ecoledirecte — en prenant pour chacun le
minimum de la couverture de lignes et de branches.

La suite ne tourne pas sous Windows : `pytest-homeassistant-custom-component`
importe `fcntl`. La moitié sans dépendance à Home Assistant s'y exécute quand
même, et c'est précisément celle qui porte le portail à 100 %. Les détails sont
au chapitre *Qualité* de [l'architecture](ARCHITECTURE.md).
