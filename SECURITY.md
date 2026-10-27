# Politique de sécurité

## Versions suivies

Seule la dernière version publiée reçoit des correctifs. Une faille corrigée
l'est dans une nouvelle version, publiée comme les autres via HACS.

## Signaler une vulnérabilité

**N'ouvrez pas d'issue publique.** Utilisez le signalement privé de GitHub :
onglet *Security* du dépôt, puis *Report a vulnerability*. Le rapport n'est
visible que des mainteneurs.

Ce qui compte particulièrement ici, parce que l'intégration manipule les
identifiants scolaires d'enfants :

- un identifiant, un jeton, une URL iCal ou une donnée d'élève qui atteint un
  état, un attribut, les diagnostics ou un journal ;
- un chemin qui contourne le limiteur de débit et peut exposer l'adresse IP de
  l'utilisateur à une sanction du serveur de l'établissement ;
- un accès à une pièce jointe ou à un service sans l'authentification de Home
  Assistant.

**Ne joignez jamais d'identifiant réel** à un rapport — ni mot de passe, ni
jeton, ni URL iCal, ni nom d'élève ou d'établissement. Des valeurs visiblement
fictives suffisent à décrire le problème (voir `CONTRIBUTING.md` §1.1).

Un accusé de réception suit sous sept jours. La correction est publiée dès
qu'elle est prête, et l'avis de sécurité ensuite, une fois la version
disponible.
