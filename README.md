# Scraper avito.ma — 100 % Docker

Nouvelle implémentation, sans navigateur, qui scrape les annonces de voitures
d'Avito.ma et les stocke dans MySQL, interrogeables via un outil en ligne de
commande.

> **Aucun service ne démarre tout seul.** Le compose est volontairement
> « à la demande » : `restart: "no"`, aucun cron, aucun scheduler.
> Les données (MySQL + dumps) vivent dans des volumes Docker sur `/mnt/4TB`.

---

## 1. Ce que fait la solution

| Étape | Méthode | Requêtes HTTP |
|---|---|---|
| Liste d'une catégorie | `curl_cffi` (impersonation TLS) + lecture du `__NEXT_DATA__` (Next.js SSR) | **1 requête ≈ 38 annonces** |
| URLs de détail | extraites du HTML de la page de liste (clé = `listId`) | 0 requête supplémentaire |
| Fiche complète (téléphone, description) | même `__NEXT_DATA__` sur l'URL de détail | 1 requête par annonce |
| Marque / modèle | API GraphQL `graphql.avito.ma` (143 marques, modèles) + matching sur le titre | ~145 requêtes, **une seule fois** (cache 7 j) |

**Contournement Cloudflare** : l'empreinte TLS de `curl` estclassée comme bot
(HTTP 403 « Just a moment »). `curl_cffi` usurpe une vraie signature TLS
(Chrome/Firefox/Safari) → **HTTP 200 direct**, sans navigateur, sans proxy,
sans `cf_clearance`.

**Gain** : l'ancien projet Selenium faisait ~1 navigation par annonce
(≈ 138 000 pour le catalogue complet). Ici : **≈ 3 650 requêtes** pour la
totalité du catalogue, sans navigateur.

---

## 1b. Où sont les données

**Tout est dans le dossier du projet, sur le disque 4 To** — aucun volume
Docker (donc rien sur le disque racine) :

```
/mnt/4TB/scraper-avito/
├── data/
│   ├── mysql/        base MySQL (annonces, versions, historique des runs)
│   └── scraped/       images, catalogue, progression, jobs par site
├── logs/             logs de scrape
├── services/         code (web, app, db)
└── docker-compose.yml
```

Le `docker-compose.yml` utilise des **bind mounts** (`./data/mysql`,
`./data/scraped`) et non des volumes nommés. Conséquence : un `docker
compose down` puis `up` ne perd rien, et le dossier `data/` peut être
sauvegardé ou déplacé tel quel.

> Vérification rapide : `docker volume ls | grep avito` ne doit rien
> renvoyer, et `df -h /mnt/4TB` montre la taille des données.

## 2. Démarrage (manuel)

```bash
cd /mnt/4TB/scraper-avito

# 1. base de données (à démarrer seulement quand on en a besoin)
docker compose up -d db
docker compose ps          # -> avito-db  healthy

# 2. scraper (one-shot : le conteneur fait son travail puis s'arrête)
docker compose run --rm app list --category "maroc/voitures-à_vendre" --limit 100 --db

# 3. consulter
docker compose run --rm tools ads --limit 20
docker compose run --rm tools stats

# 4. arrêter la base
docker compose stop db
```

`docker compose down` supprime les conteneurs **mais pas** les volumes
(les données sont conservées). `docker compose down -v` efface les données.

---

## 3. Le scraper

```bash
# lister + inserer en base
docker compose run --rm app list --category "maroc/voitures-à_vendre" --limit 500 --db

# re-scraping intelligent : seules les annonces modifiees sont detaillees
docker compose run --rm app rescan --category "maroc/voitures-à_vendre" --details
docker compose run --rm app rescan --category "maroc/voitures-à_vendre" --details --mark-missing

# completer les fiches detail manquantes
docker compose run --rm app details --limit 200

# recalculer les notes
docker compose run --rm app score

# avec les fiches detail (telephone, description complete)
docker compose run --rm app list --category "maroc/voitures-à_vendre" --limit 50 --details --db

# export JSONL dans le volume /data
docker compose run --rm app list --category "maroc/voitures-à_vendre" --limit 100 --out /data/ads.jsonl

# options
  --delay 1.5      pause entre requetes (s)
  --jitter 0.8     alea de la pause
  --no-brand       ne pas deduire marque/modele
  --quiet
```

Le catalogue d'annonces est d'environ **138 500 véhicules** ; la catégorie se
change avec `--category` (ex. `maroc/immobilier`, `maroc/telephones`...).

---

### Telecharger les images (separe du scraping)

Le scraping collecte les annonces **et leurs URLs d'images** ; le
telechargement des fichiers est une operation a part, avec sa propre cadence
et son propre choix de qualite.

```bash
# 20 annonces, qualite standard (~42 Ko/image)
docker compose run --rm app images --variant images --limit 20

# miniatures (~20 Ko) : 21 Go pour tout le catalogue
docker compose run --rm app images --variant card --limit 100

# pleine resolution (~205 Ko) : 216 Go pour tout le catalogue
docker compose run --rm app images --variant full_hd --limit 100

# 3 photos max par annonce, sur une categorie
docker compose run --rm app images --variant images --per-ad 3 --category "maroc/voitures-à_vendre"

# reprendre/re-telecharger
docker compose run --rm app images --variant images --limit 0 --force
```

| variante | Ko/image | catalogue complet (138 500 annonces x 8 photos) |
|---|---|---|
| `card` | 20 | 21 Go |
| `images` | 42 | 45 Go |
| `full_hd` | 205 | 216 Go |

* **Incremental** : seules les images non telechargees dans cette variante
  sont prises ; `--force` force le retelechargement
* **Stockage** : `/data/images/{ad_id}/{image_id}_{variant}.jpg` dans le volume
  Docker (chaque variante a son propre fichier, rien n'est ecrase)
* **Base** : `ad_images` est completee (`path_local`, `variant`, `bytes`,
  `sha1`, `downloaded_at`)
* **Photo remplacee sur le site** : le `sha1` change -> une version
  `image_change` est archivee dans l'historique de l'annonce
* **Rythme** : ~1 image / 1,2 s par defaut. 1,1 M images representent
  6 a 12 jours : a lancer en batch quand tu le veux, pas pendant le scraping

## 3b. Interface web (dans Docker)

```bash
docker compose up -d web          # http://127.0.0.1:8080
```

* **Liste** : photos, prix, marque/modele, ville et la **note globale**
  (+ sa confiance) ; tri par date, prix, annee, km, note globale, etat, achat,
  confiance. Les notes detaillees (etat / achat / legacy) apparaissent
  uniquement sur la fiche d'une annonce
* **Filtres** (tous dans le meme bloc) : recherche plein texte, site, ville,
  marque, **modele** (liste groupee par marque, mise a jour quand on change
  de marque), carburant, boite, prix min/max, **annee min/max (listes
  deroulantes)**, km max, notes (etat / achat / globale), confiance minimale,
  avec telephone, **avec photos**, **avec prix**, **fiche complete**,
  actives
* **Tri** : publication, mise a jour, prix, annee, kilometrage, note globale
  (etat, achat, confiance) — dans les deux sens. Le selecteur de tri est
  juste au-dessus de la liste des annonces (il conserve les filtres
  actifs), avec un selecteur "aller a une page" pour sauter directement a
  une page
* Chaque annonce porte un **badge d'etat du scrap** : "detail v" (page
  detail recuperee) ou "liste" (connue uniquement via la page de liste)
* **Fiche d'annonce** : toutes les caracteristiques, description, equipements,
  photos (locales si telechargees, sinon distantes) — un clic sur une photo
  ouvre une **lightbox** (navigation clavier, fermeture par Echap) au lieu
  d'ouvrir l'image dans un onglet vide ; **scoring v2 detaille**
  (etat / achat / global / confiance) + **scoring legacy**, et l'**historique
  des versions** avec le diff de chaque changement
* **API JSON** : `/api/ads?fuel=Diesel&min_price=100000&limit=50`

Exposition via le tailnet (chiffre, hors Internet) :
```bash
sudo tailscale serve --bg --https=8444 8080
# https://nabyl-ms-7d46.taild3ce02.ts.net:8444/
```

### Persistance de l'interface

L'interface doit rester accessible : les services `db` et `web` ont la
politique de redemarrage `unless-stopped` (ils se relancent tout seuls si le
poste redemarre, Docker demarrant au boot).

En complement, une unite systemd utilisateur assure le demarrage de la pile :

```bash
systemctl --user enable --now nabyl.avito-web.service
systemctl --user status nabyl.avito-web.service
```

Le service `scraper` (lui) reste en `restart: "no"` : **aucun scrap n'est
relance automatiquement**, c'est l'interface qui le pilote.

## 3c. Page de suivi (`/tracking`)

Deuxieme page de l'interface, accessible depuis la barre de navigation
(lien dans les deux sens) et par URL directe : `http://127.0.0.1:8080/tracking`.

* **Progression en direct** : barre de progression (annonces vues / total du
  catalogue), pourcentage, **temps ecoule**, **temps restant (estimation)**,
  vitesse (annonces/min), tranche en cours, requetes HTTP, blocages, erreurs
  reseau. Rafraichissement automatique toutes les 3 s (le reste aussi quand
  l'onglet est ouvert).
* **Pilotage** : demarrer / arreter / relancer le scrap en cours, avec le
  choix du site, de la categorie, du mode (`list`, `rescan`, `details`),
  de la cadence (delay / jitter), du plafond et de la decoupe en tranches.
  Changer de site met a jour la categorie et les modes proposes ; les
  boutons passent en "desactive" selon l'etat du conteneur (evite le double
  clic).
* **Un bloc de progression par site** : barre, %, temps ecoule, temps
  restant, vitesse, tranche en cours, requetes, blocages, erreurs. La barre
  est **animee** (bandes defilantes) tant qu'un scrap tourne, et statique
  sinon ; le badge "en cours" est precede d'un petit loader.
* **Alertes** : erreur du run, **blocage serveur (Cloudflare)** detecte
  (403/429/503 ou challenge), erreurs reseau, arret manuel, conteneur arrete
  alors que le suivi annonce un run en cours.
* **Historique** : tous les scrapes passes (site, debut, duree, mode,
  annonces listees / detail / inserees / modifiees, requetes, blocages,
  statut). L''erreur d'un run en echec s'affiche en cliquant sur le statut.
  La colonne categorie est masquee quand chaque site n'a que peu de
  categories. Le tableau se **filtre** (site, statut, mode) et se **trie**
  en cliquant sur un en-tete. + les derniers logs du conteneur.

### Scraper plusieurs sites en parallele

Chaque site a **son propre conteneur ephemere**, lance par l'interface :

```bash
# depuis l'interface (page /tracking) : le site choisi demarre
# equivalent en ligne de commande :
docker compose run -d --rm --name avito-scraper-tomobila app list --site tomobila --db
```

* les conteneurs sont supprimes a la fin du run (`--rm`) : il n'y a jamais
  plus de conteneurs que de scrapes en cours
* arreter un site ne touche pas les autres
* l'interface appelle `docker compose run` (le CLI docker est embarque dans
  l'image web) : la configuration reste celle du `docker-compose.yml`
* chaque site a ses propres parametres (`/data/jobs/<site>.json`)

Fonctionnement :

* le scraper ecrit `/data/progress/<site>.json` (volume partage) a chaque
  lot d'annonces : c'est la source de la progression live ;
* les runs sont aussi enregistres en base (`scrape_runs`) : une ligne est
  ouverte au debut du run (statut `running`) et completee a la fin ;
* l'interface pilote un conteneur par site via le CLI docker
  (`docker compose run`, `docker stop`) ; le socket Docker est monte dans le
  conteneur web ;
* les parametres du job sont ecrits dans `/data/scrape_job.json` et lus au
  demarrage du conteneur : on peut changer de categorie sans reconstruire.

```bash
# lancer / arreter un scrape en ligne de commande
docker compose run --rm app list --site avito --category "maroc/voitures-à_vendre" --db
docker compose run -d --rm --name avito-scraper-avito app list --site avito --db
docker stop avito-scraper-avito
```

## 4. L'outil de consultation

```bash
# 20 annonces recentes
docker compose run --rm tools ads --limit 20

# voitures diesel a Rabat, 100k-200k DH, >= 2018
docker compose run --rm tools ads --city Rabat --fuel Diesel \
      --min-price 100000 --max-price 200000 --min-year 2018

# recherche plein texte
docker compose run --rm tools ads --search "golf" --order price --limit 10

# fiche complete (images, options, telephone)
docker compose run --rm tools ad 78325310

# statistiques globales
docker compose run --rm tools stats

# repartitions
docker compose run --rm tools group-by city --limit 20
docker compose run --rm tools group-by brand
docker compose run --rm tools group-by fuel

# historique des executions
docker compose run --rm tools runs --limit 10

# historique des versions d'une annonce
docker compose run --rm tools history --ad-id 78455851 --verbose
# annonces ayant change (version > 1)
docker compose run --rm tools versions --limit 20

# filtrer par note
docker compose run --rm tools ads --min-note 15 --order note

# detail des notes d'une annonce
docker compose run --rm tools ad 78455941 --explain

# export
docker compose run --rm tools ads --city Casablanca --format csv --out /data/casablanca.csv
docker compose run --rm tools ads --format json --out /data/ads.json
```

Filtres disponibles : `--city --brand --fuel --gear --min-price --max-price
--min-year --max-year --max-km --search --with-phone --details-only --columns
--order --limit --format --out`.

---

## 5. Scoring (porté de l'ancien projet)

Chaque sous-note est sur 20 ; la note globale est la moyenne pondérée :
`(annee*3 + km*3 + ratio*2 + cv*3 + toit*1 + carburant*3 + boite*2 + specs*2) / 19`

| sous-note | règle | source |
|---|---|---|
| `note_year` | âge : 0-1 an = 20, 2 = 19, 3 = 18 … 16 = 3, 17 = 1 | année-modèle |
| `note_km` | <10k = 20, <50k = 16, <100k = 13, <160k = 10, <200k = 8, <250k = 5 | kilométrage |
| `note_ratio` | km/an : <8k = 20, <10k = 18, <12k = 17, <14k = 15 … <30k = 3 | km / âge |
| `note_cv` | <8 CV = 20, <11 = 12, <15 = 5 | puissance fiscale |
| `note_fuel` | Hybride 20, Diesel 15, Essence 10, Electrique 5 | carburant |
| `note_gear` | Automatique 20, Manuelle 10 | boîte |
| `note_sunroof` | 20 si « Toit ouvrant » | équipements |
| `note_specs` | confort (9 × coef 2) + sécurité (5 × coef 3) + look (1 × coef 1), /6 | équipements |

La divergence historique sur `note_cv` (12 dans le Crawler, 10 dans le
Calculator) est tranchée en faveur du Crawler, seule constante documentée.

```bash
docker compose run --rm app score            # recalcule toutes les notes
docker compose run --rm tools ads --min-note 15 --order note
```

### Scoring v2 — « aider les acheteurs »

Le legacy reste tel quel (colonne `note`). Le v2 ajoute trois axes :

| axe | contenu | poids par defaut |
|---|---|---|
| `note_etat` | etat, 1ere main, origine, 5 portes, age, km, coherence km/an, equipements | 65 % |
| `note_achat` | prix vs marche, **prix par annee de vie restante**, prix/km, valeur pour le prix | 35 % |
| `note_global` | **note globale** : etat x 65 % + achat x 35 % | — |
| `note_confiance` | % des criteres reellement connus (0-100) | — |

Differences voulues avec le legacy :
* **une donnee manquante n'est plus notee 0** : elle est exclue du calcul et
  compte dans la confiance. Le legacy penalisait l'info manquante, ce qui
  biaisait le classement contre les fiches incompletes.
* **criteres ajoutes** : etat, 1ere main, origine, nombre de portes, et
  surtout les criteres de prix (prix/an, prix/km, et **prix compare au
  median du segment** : meme marque + modele + annee, sinon meme marque +
  annee — un segment de moins de 5 annonces n'est pas exploitable).
* **les criteres sont explicites** : `tools ad <id> --explain` affiche le
  detail, les poids appliques et la confiance.
* **deux criteres corriges pour un acheteur** :
  - *prix par annee de vie restante* (et non plus prix/anciennete) : on paie
    moins par annee qui reste a rouler. Un critere « prix / anciennete »
    favorisait les voitures deja depreciees — a l'inverse de l'interet de
    l'acheteur. Duree de vie de reference : 15 ans (configurable).
  - *coherence km/annee* : au-dela de 30 000 km/an la note ne tombe plus a 0
    mais a un plancher, et l'annonce est signalee (`km/an incoherent`).
  Resultat : a conditions egales, la voiture la plus recente sort devant,
  de facon monotone.
* **la note globale ne peut pas Mentir** : les poids sont fixes (65/35), donc
  une annonce sans prix (pas d'axe « achat » evaluable) ne peut pas depasser
  13/20 au lieu de sortir a 20/20. Mesure : les annonces avec prix ont une
  note globale moyenne de 15,6, celles sans prix 10,7.

```bash
docker compose run --rm tools ad 78455941 --explain
docker compose run --rm tools ads --min-note-global 16 --order note_global
docker compose run --rm tools ads --min-confiance 80
```

Les poids et seuils sont dans `services/app/config/scoring.json`, monte en
lecture seule dans le conteneur : tu les ajustes et tu relances, **sans
rebuild**.

> Le score `prix_vs_marche` n'a de sens qu'avec un corpus suffisant
> (quelques milliers d'annonces, donc apres un scraping complet de la
> categorie). Sur une base de 40 annonces, le most often n'est pas
> exploitable et la confiance affichee baisse.

## 6. Versionnement : un historique, sans re-scraper à l'aveugle

Le re-scraping est **optimisé** : la page de liste suffit à détecter qu'une
annonce a changé, la fiche détail n'est récupérée **que** pour les annonces
candidates.

```
rescan
  └─ page de liste (1 requête ≈ 38 annonces)
       ├─ annonce inchangée  -> rien (juste last_seen_at / seen_count)
       ├─ annonce nouvelle   -> fiche detail -> version 1
       └─ annonce modifiée   -> fiche detail -> version N+1 + diff
```

* La comparaison se fait **champ par champ, à la profondeur de l'observation** :
  un champ absent de la passe courante n'est pas comparé (une passe « liste »
  ne peut pas conclure sur le km, la marque, l'état…).
* Les drapeaux marketing (`premium`, `urgent`, `hot deal`) et le nombre
  d'images sont **exclus du déclenchement** : le site les expose de façon
  fluctuante entre les deux vues, ce qui créait des faux positifs à chaque
  passage. Ils restent stockés et filtrables.
* La fiche detail fait foi : si la liste annonce un changement mais que la
  fiche est identique, **aucune version** n'est créée.
* Une annonce disparue du catalogue est marquée `is_active = 0` avec une
  version de type `disappeared` (uniquement sur un run couvrant toute la
  catégorie).

Tables :
* `ads` — état courant + `version`, `content_hash`, `seen_count`,
  `first_seen_at`, `last_seen_at`, `is_active`
* `ad_versions` — une ligne par version : `version`, `change_type`
  (`new`, `price_change`, `content_change`, `flag_change`, `updated`,
  `disappeared`), `diff_json` (ancien → nouveau par champ), `snapshot_json`

```bash
# l'historique d'une annonce
docker compose run --rm tools history --ad-id 78455851 --verbose
# les annonces qui ont change
docker compose run --rm tools versions --limit 20
# toutes les dernieres versions
docker compose run --rm tools history --limit 20
```

Exemple réel :
```
[v1] new          — 2026-10-21 01:05:16
[v2] price_change — 2026-10-21 01:09:54
    price: 99999 -> 280000
```

## 7. Le modèle de données

`ads` (1 ligne par annonce) — colonnes alignées sur l'ancienne table `ad`
pour faciliter une migration, enrichies :

| colonne | contenu |
|---|---|
| `ad_id`, `list_id` | identifiants Avito |
| `title`, `description` | titre, description complète |
| `price`, `publish_date` | prix en DH, date de publication |
| `year`, `km`, `fuel`, `gear` | caractéristiques (km = fiche détail) |
| `doors`, `origin`, `first_hand`, `cond`, `fiscal_power`, `sector` | specs de la page détail |
| `brand`, `model` | déduits du titre (référentiel GraphQL) |
| `city`, `area` | ville, quartier |
| `seller`, `seller_type`, `seller_id`, `phone` | vendeur + téléphone |
| `color`, `car_body` | attributs lorsqu'ils sont disponibles |
| `options_json` | équipements (ABS, airbags, clim, GPS,|ESP...) |
| `link` | URL canonique de l'annonce |
| `is_premium`, `is_urgent`, `is_hot_deal` | drapeaux |
| `details_fetched` | 1 si la fiche complète a été récupérée |
| `raw_json` | annonce brute (pour rejouer/auditer) |
| `note`, `note_year`… | scoring **legacy** (inchangé) |
| `note_etat`, `note_achat`, `note_global`, `note_confiance` | scoring **v2** (aide aux acheteurs) |

`ad_images` — images (lien standard + lien `?t=full_hd`, qui évite l'OCR des
photos floutées).

`scrape_runs` — historique : dates, catégorie, nombre d'annonces, requêtes
HTTP, blocages Cloudflare, statut, erreur.

---

## 8. Anti-détection

* `curl_cffi` avec rotation de profil TLS (chrome / firefox / safari)
* pauses entre requêtes (`--delay` / `--jitter`) — à augmenter si
  Cloudflare escalade (compteur `blocked` visible dans `runs` et `stats`)
* backoff exponentiel sur 403/429/503
* rotation du User-Agent implicite via l'impersonation
* en cas de blocage durable : passer par un proxy résidentiel (le
  `cf_clearance` est lié à l'IP, il faut donc un cookie par IP)

---

## 8b. Sites scrapes

| site | adaptateur | source | annonces | requetes |
|------|------------|--------|----------|----------|
| avito | `sites/avito.py` | Next.js (`__NEXT_DATA__`) + API GraphQL marques | 138 836 | 2 556 pour le catalogue |
| tomobila | `sites/tomobila.py` | WordPress (tableau d'attributs) | ~4 600 | 1/page |
| globaloccaz | `sites/globaloccaz.py` | **API REST** `/wp-json/vehica/v1/cars` (plugin Vehica) | 268 | **1 pour tout le catalogue** |
| kifal | `sites/kifal.py` | HTML + donnees codees dans l'URL | 396 | 1/page (35) |
| renew | `sites/renew.py` | SPA **Nuxt** : etat `window.__NUXT__` (format devalue) | 412 | **1 pour tout le catalogue** |
| otoclic | `sites/otoclic.py` | WordPress + plugin Apus (taxonomies dans les classes CSS) | ~150 | 1/page (12) |
| leguideauto | `sites/leguideauto.py` | HTML (tableau d'attributs) | 2 934 | 1/page (10) |

Points notables :

* **globaloccaz** expose une API REST qui renvoie toutes les voitures avec
  leurs attributs types (marque, modele, annee, prix, km, carburant, boite,
  VIN, fiche expertise...) : **une seule requete** pour tout le catalogue
* **renew** est une application Vue/Nuxt : le catalogue est serialise dans le
  payload `window.__NUXT__` (format "devalue"). `sites/renew.py` embarque un
  lecteur de ce format, ce qui donne les 412 voitures (avec leurs photos et
  leurs equipements) en une requete
* **kifal** encode tout dans le nom de fichier de l'URL :
  `MARQUE_MODELE_ANNEE_CARBURANT_BOITE_VILLE_ID_REF.htm`
* **otoclic** encode les taxonomies dans les classes CSS de la carte
  (`listing_make-peugeot listing_fuel_type-diesel listing_feature-abs ...`),
  et la galerie complete dans un attribut `data-images`
* le `ad_id` est une **chaine** (VARCHAR 64) : certains sites utilisent un
  identifiant alphanumerique (`dITj3MBMiY`) ou un slug

## 9. Architecture multi-sites

Le scraper est **modulaire** : le coeur (pipeline, base, versionnement,
scoring, images, CLI) ignore completely le site ; chaque site est un
adaptateur.

```
services/app/src/scraper/
├── core/                  # genereque, aucun site
│   ├── http.py            # client curl_cffi : rate limiting, retry, anti-Cloudflare
│   ├── site.py            # contrat SiteAdapter + contrat de l'annonce normalisee
│   ├── registry.py        # enregistrement des adaptateurs
│   ├── pipeline.py        # list / rescan / details (detection de changement, versions)
│   ├── db.py              # MySQL (upsert versionne, multi-sites)
│   ├── history.py         # comparaison champ par champ + diff
│   ├── scoring.py         # legacy + v2
│   ├── market.py          # prix median par segment
│   ├── catalog.py         # referentiel marques/modeles
│   └── schema.py          # DDL
├── sites/                 # un fichier par site
│   ├── avito.py           # Next.js (__NEXT_DATA__) + API GraphQL
│   └── tomobila.py        # WordPress (tableau d'attributs)
├── cli.py                 # list | rescan | details | score | images | stats | sites
├── query.py               # CLI de consultation
└── images.py              # telechargeur d'images
```

**Ajouter un site** : ecrire `sites/mon_site.py` avec une classe qui herite de
`SiteAdapter` (5 methodes : `listing_url`, `parse_listing`, `detail_url`,
`parse_detail`, et en option `total_ads` / `enrich` / `image_url`), puis
l'enregistrer dans `sites/__init__.py`. Rien d'autre a changer : pipeline,
versionnement, scoring, images et CLI sont reutilisables tels quels.

```bash
docker compose run --rm app sites
docker compose run --rm app list --site tomobila --category "achat-voiture-occasion" --details --db
```

En base, la cle primaire est composite `(site, ad_id)` : plusieurs sites
coexistent dans la meme table, filtrable par `site`. `ad_id` est un
VARCHAR(64) (les identifiants de site ne sont pas toujours numeriques).

## 10. Structure

```
/mnt/4TB/scraper-avito/
├── docker-compose.yml      # db + app + tools, aucun auto-demarrage
├── .env                    # identifiants (copie de .env.example)
├── services/
│   ├── app/                # image Python (scraper + outils)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── bin/{scrape,query}.py
│   │   └── src/avito/
│   │       ├── client.py   # HTTP + __NEXT_DATA__ + URLs
│   │       ├── catalog.py  # marques/modeles via GraphQL
│   │       ├── db.py       # MySQL (upsert, lecture)
│   │       ├── schema.py   # DDL
│   │       ├── cli.py      # sous-commande `list`
│   │       └── query.py    # sous-commande `ads`/`ad`/`stats`/`group-by`/`runs`
│   └── db/                 # image MySQL + schema initial
└── old-analysis/           # scripts d'analyse (hors Docker)
```

Volumes Docker (persistants, sur `/mnt/4TB`) :
* `avito_db_data` — données MySQL
* `avito_scraped_data` — dumps JSONL/CSV + cache du catalogue

---

## 11. Développement

* **Chaque modification est commitée et poussée** sur `main`
  (`https://github.com/nabilsadki/avito-scraper`). Un hook `post-commit`
  local pousse automatiquement apres chaque commit.
* Pour pulls : `git pull --rebase`
* Secrets : ne jamais commiter le `.env` (déjà ignoré) ; utiliser
  `.env.example` comme modèle

```bash
git add -A && git commit -m "description courte" && git push
```

## 12. Reprise de l'ancien projet

L'ancien système (à `/var/www/html/selenium`, et le Symfony
`/mnt/Ubundata/html/voitures-maroc-sy`) faisait :

* scraping Selenium headless Firefox + XPath absolus, 1 page détail par annonce
* écriture dans la table `ad` d'un MySQL **distant** `sql.feeling-group.com:6033`
  — dont le **DNS ne résout plus** (mort depuis le 20/10/2026)
* règles de filtrage/notation (`note_year`, `note_km`, `note_ratio`...) non
  reprises ici : le nouveau schéma garde les colonnes d'attributs mais pas le
  scoring

Les crons de l'ancien projet sont **commentés** dans le crontab utilisateur.
Rien n'est planifié dans la nouvelle solution : le scraping se déclenche
manuellement, quand on le décide.
