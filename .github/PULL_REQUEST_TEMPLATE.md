<!--
Merci pour la contribution. Le guide complet est dans CONTRIBUTING.md ;
ce qui suit en est la version courte, à remplir.
-->

## Ce que ça change

<!-- Et surtout *pourquoi*. Pour une correction, décrivez le symptôme qu'un
utilisateur voyait : c'est ce qui permet de juger si la correction est la
bonne, et pas seulement si elle fait passer les tests. -->

## Comment vous l'avez vérifié

<!-- Les tests ajoutés, ou ce que vous avez observé sur une instance réelle. -->

## Portails

- [ ] `ruff check .` et `ruff format --check .`
- [ ] `mypy --strict custom_components/carnet_scolaire`
- [ ] `pytest tests` — au complet, sous Linux, WSL ou Docker
      (`pytest -p no:homeassistant` ne couvre que la moitié de la suite)
- [ ] `python scripts/check_coverage.py coverage.xml` — 80 % global, et 100 %
      sur `ratelimit.py`, `scheduler.py`, `gateway.py`, `delta.py`
- [ ] `mkdocs build --strict`, si vous avez touché à `docs/`

## Vérifications propres à ce dépôt

- [ ] **Aucun identifiant réel** : ni mot de passe, ni code PIN, ni contenu de
      QR code, ni URL iCal, ni nom d'élève, ni nom d'établissement — dans le
      code, les tests, les *fixtures*, les messages de commit et les captures
      d'écran.
- [ ] `manifest.json` ne déclare **pas** de clé `loggers`, et rien n'active le
      journaliseur `pronotepy`.
- [ ] Si vous avez modifié un texte visible par l'utilisateur : la table de
      `scripts/build_translations.py` est la source, et
      `python scripts/build_translations.py` a été relancé. `strings.json`,
      `translations/*.json` et `services.yaml` ne s'éditent pas à la main.
- [ ] Si vous avez ajouté un appel réseau : il passe par le limiteur et déclare
      son coût réel en requêtes.
- [ ] La version de `manifest.json` est **inchangée** (`0.0.0`) — le tag est
      la version, la publication l'écrit dans le zip.

## Impact sur le budget de requêtes

<!-- Le budget par défaut est d'environ 180 requêtes par jour pour un enfant.
Si votre changement le modifie, dites de combien et mettez à jour
docs/annexe-b-rate-limit.md. Sinon, écrivez « aucun ». -->

## Issues liées

<!-- Closes #… -->
