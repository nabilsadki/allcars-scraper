# old-analysis

Scripts d'exploration garde pour memoire (ils ne font plus partie du projet
et ne sont pas maintenus) :

* `test_parity.py` / `test_parity_strict.py` — comparaison de l'ancien
  scraper Selenium (rendu navigateur + JSON-LD) avec la nouvelle extraction
  HTTP (`__NEXT_DATA__`)
* `test_live.py` — test rapide de l'extraction HTTP
* `run.py` — premier prototype de CLI

Ils referencent l'ancienne arborescence (`scrapper/sites/avito_http.py`) :
le code actuel est dans `services/app/src/avito/`.
