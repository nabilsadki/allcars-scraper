"""Interface web du catalogue Avito (liste, filtres, detail, scoring, suivi).

Lise la base MySQL du compose (service `db`) et sert les images
telechargees si elles existent dans le volume /data.

Routes
------
GET  /                    liste + filtres (query string)
GET  /ad/<id>             fiche d'une annonce (donnees, photos, scoring, historique)
GET  /tracking            suivi du scrap en cours + historique + pilotage
GET  /images/<path>       image locale du volume
GET  /api/ads             meme liste en JSON
GET  /api/progress        etat live du scrap (JSON)
POST /api/scrape/start    demarrer le scrap
POST /api/scrape/stop     arreter le scrap
POST /api/scrape/restart  relancer le scrap
GET  /api/scrape/logs     dernieres lignes de log
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from urllib.parse import urlencode

import pymysql
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_from_directory)

import progress as progress_reader
import scrapectl

app = Flask(__name__)


def _asset_version(name: str) -> int:
    """Version d'un fichier statique (mtime) pour casser le cache.

    Sans cela, le navigateur (et le telephone) garde l'ancien CSS et les
    changements ne sont pas visibles.
    """
    try:
        return int(os.path.getmtime(os.path.join(app.static_folder, name)))
    except Exception:
        return 0


app.jinja_env.globals["asset_version"] = _asset_version

DB = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "avito"),
    "password": os.environ.get("DB_PASSWORD", "avito"),
    "database": os.environ.get("DB_NAME", "avito"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}
IMAGES_DIR = os.environ.get("IMAGES_DIR", "/data/images")

COLUMNS = [
    "site", "ad_id", "title", "price", "year", "km", "fuel", "gear", "brand", "model",
    "city", "area", "note", "note_etat", "note_achat", "note_global",
    "note_confiance", "version", "is_active", "seller", "phone",
    "publish_date", "last_seen_at", "nb_images", "details_fetched",
]

#: tris disponibles : cle -> clause ORDER BY
#: "colonne IS NULL" pour pushed les annonces sans valeur en fin de liste
SORTABLE = {
    # dates
    "date": "publish_date IS NULL, publish_date DESC",
    "date_asc": "publish_date IS NULL, publish_date ASC",
    "update": "last_seen_at IS NULL, last_seen_at DESC",
    "update_asc": "last_seen_at IS NULL, last_seen_at ASC",
    "recent": "scraped_at IS NULL, scraped_at DESC",
    # prix
    "price": "price IS NULL, price ASC",
    "price_desc": "price IS NULL, price DESC",
    # millesime / kilometrage
    "year": "year IS NULL, year DESC",
    "year_asc": "year IS NULL, year ASC",
    "km": "km IS NULL, km ASC",
    "km_desc": "km IS NULL, km DESC",
    # notes
    "note": "note_global IS NULL, note_global DESC, note DESC",
    "note_asc": "note_global IS NULL, note_global ASC",
    "note_etat": "note_etat IS NULL, note_etat DESC",
    "note_achat": "note_achat IS NULL, note_achat DESC",
    "confiance": "note_confiance IS NULL, note_confiance DESC",
}

#: tris proposes dans le selecteur (regroupes par famille)
SORT_GROUPS = [
    ("Dates", [
        ("date", "Publication : plus récentes"),
        ("date_asc", "Publication : plus anciennes"),
        ("update", "Mise à jour : plus récentes"),
        ("update_asc", "Mise à jour : plus anciennes"),
    ]),
    ("Prix", [
        ("price", "Prix croissant"),
        ("price_desc", "Prix décroissant"),
    ]),
    ("Véhicule", [
        ("year", "Année (plus récentes)"),
        ("year_asc", "Année (plus anciennes)"),
        ("km", "Kilométrage croissant"),
        ("km_desc", "Kilométrage décroissant"),
    ]),
    ("Notes", [
        ("note", "Note globale (meilleure)"),
        ("note_asc", "Note globale (moins bonne)"),
        ("note_etat", "Note état"),
        ("note_achat", "Note achat"),
        ("confiance", "Confiance"),
    ]),
]


def db():
    return pymysql.connect(**DB)


SITES_PATH = os.environ.get("SITES_FILE", "/data/sites.json")

#: fallback si le scraper n'a jamais tourne (pas de /data/sites.json)
DEFAULT_SITES = [
    {"name": "avito", "label": "Avito.ma",
     "default_category": "maroc/voitures-à_vendre",
     "categories": ["maroc/voitures-à_vendre", "maroc/immobilier",
                    "maroc/telephones", "maroc/informatique"],
     "modes": ["list", "rescan", "details"]},
    {"name": "tomobila", "label": "Tomobila.ma",
     "default_category": "achat-voiture-occasion",
     "categories": ["achat-voiture-occasion"],
     "modes": ["list", "rescan", "details"]},
]


def known_sites() -> list[dict]:
    """Sites disponibles (categories + modes), publies par le scraper."""
    try:
        with open(SITES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        sites = data.get("sites") if isinstance(data, dict) else data
        if sites:
            return sites
    except Exception:
        pass
    return DEFAULT_SITES


def query(sql, params=()):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def execute(sql, params=()) -> int:
    """Ecriture en base : la transaction est validee explicitement.

    (fermer la connexion sans commit annule l'ecriture)
    """
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
    finally:
        conn.close()


SITE_LABELS = {"avito": "Avito.ma", "tomobila": "Tomobila.ma"}


def _datefr(value, with_time: bool = False) -> str:
    """Formate une date : '21/10/2026' (ou '21/10 à 12h46')."""
    if not value:
        return "-"
    if isinstance(value, str):
        value = _parse_date(value)
        if value is None:
            return "-"
    fmt = "%d/%m/%Y à %Hh%M" if with_time else "%d/%m/%Y"
    try:
        return value.strftime(fmt)
    except Exception:
        return "-"


def _parse_date(value: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


app.jinja_env.filters["datefr"] = _datefr


def site_label(site: str) -> str:
    """Libelle lisible du site ('avito' -> 'Avito.ma')."""
    return SITE_LABELS.get((site or "").lower(), site or "?")


app.jinja_env.filters["site_label"] = site_label

#: teinte associee a chaque site (pour les badges colores)
SITE_HUES = {
    "avito": 214,        # bleu
    "tomobila": 145,     # vert
    "globaloccaz": 268,  # violet
    "kifal": 24,        # orange
    "renew": 340,       # rose
    "otoclic": 186,     # cyan
    "leguideauto": 45,  # jaune
    "moteur": 300,     # magenta
}


def site_hue(site: str) -> int:
    """Teinte (0-360) d'un site : fixe pour les sites connus, sinon calculee.

    Le calcul est deterministe : un nouveau site obtient toujours une
    couleur stable et differente de ses voisins.
    """
    name = (site or "").lower()
    if name in SITE_HUES:
        return SITE_HUES[name]
    total = sum((i + 1) * ord(c) for i, c in enumerate(name))
    hue = total % 360
    # on evite les teintes deja prises par les sites connus (et on recommence
    # si la rotation retombe sur une teinte proche)
    for _ in range(20):
        clash = [k for k, v in SITE_HUES.items()
                 if min(abs(hue - v), 360 - abs(hue - v)) < 18]
        if not clash:
            break
        hue = (hue + 40) % 360
    return hue


app.jinja_env.filters["site_hue"] = site_hue


def freshness(value) -> str:
    """Classe CSS selon l'anciennete du scraping.

    <= 2 jours : 'freshness-fresh' (vert) ; au-dela : 'freshness-stale' (orange).
    """
    if not value:
        return "freshness-stale"
    try:
        age = datetime.now() - value
    except Exception:
        return "freshness-stale"
    if age.days >= 2:
        return "freshness-stale"
    return "freshness-fresh"


app.jinja_env.filters["freshness"] = freshness
#: groupes de tris (utilises par la barre du haut, sur toutes les pages)
app.jinja_env.globals["sort_groups"] = SORT_GROUPS


def goto_pages(page: int, pages: int, window: int = 15) -> list:
    """Pages proposees dans le selecteur "aller a une page".

    Une fenetre autour de la page courante, plus la premiere et la
    derniere page (le catalogue peut depasser les 700 pages).
    """
    if pages <= 1:
        return []
    wanted = {1, pages}
    wanted |= set(range(max(1, page - window), min(pages, page + window) + 1))
    return sorted(wanted)


def pager_items(page: int, pages: int, window: int = 2) -> list:
    """Numeros de page a afficher dans le pager.

    Renvoie une liste de nombres et de "…" : au maximum une dizaine
    d'elements, quel que soit le nombre de pages (le catalogue avito
    dépasse les 600 pages).
    """
    if pages <= 1:
        return []
    shown = set(range(1, min(pages, 3) + 1))                     # 1, 2, 3
    shown |= set(range(max(1, page - window), min(pages, page + window) + 1))
    shown |= set(range(max(1, pages - 2), pages + 1))             # N-2, N-1, N
    out = []
    prev = 0
    for p in sorted(shown):
        if prev and p - prev > 1:
            out.append("…")
        out.append(p)
        prev = p
    return out


def model_options(brand: str = "", site: str = "") -> list[dict]:
    """Modeles disponibles, groupes par marque.

    Si une marque est donnee, seuls ses modeles sont renvoyes.
    """
    where = ["model IS NOT NULL", "model <> ''"]
    params: list = []
    if brand:
        where.append("brand = %s")
        params.append(brand)
    if site:
        where.append("site = %s")
        params.append(site)
    rows = query(
        "SELECT brand, model, COUNT(*) n FROM ads WHERE " + " AND ".join(where) +
        " GROUP BY brand, model ORDER BY brand, n DESC", tuple(params))
    groups: dict[str, list] = {}
    for r in rows:
        brand_name = r["brand"] or "(sans marque)"
        groups.setdefault(brand_name, []).append(r["model"])
    return [{"brand": b, "models": m} for b, m in sorted(groups.items())]


# ------------------------------------------------------------------ filtres
#: libelles des filtres (pour les puces "filtres actifs")
FILTER_LABELS = [
    ("search", "Recherche"), ("site", "Site"), ("city", "Ville"),
    ("brand", "Marque"), ("model", "Modèle"), ("fuel", "Carburant"),
    ("gear", "Boîte"), ("min_price", "Prix min"), ("max_price", "Prix max"),
    ("min_year", "Année min"), ("max_year", "Année max"), ("max_km", "Km max"),
    ("min_note_etat", "Note état"), ("min_note_achat", "Note achat"),
    ("min_note_global", "Note globale"), ("min_confiance", "Confiance"),
    ("with_phone", "Téléphone"), ("with_photos", "Photos"),
    ("with_price", "Prix"), ("with_details", "Fiche complète"),
    ("active_only", "Actives"),
]


def active_filters() -> list[dict]:
    """Filtres actuellement appliques (pour les puces)."""
    out = []
    for key, label in FILTER_LABELS:
        value = (request.args.get(key) or "").strip()
        if not value:
            continue
        rest = [(k, v) for k, v in request.args.items() if k != key]
        # les cases a cocher n'ont pas de valeur a afficher ("1")
        if key.startswith("with_") or key == "active_only":
            value = ""
        out.append({
            "key": key,
            "label": label,
            "value": value,
            "url": ("/?" + urlencode(rest)) if rest else "/",
        })
    return out


def build_filters() -> tuple[str, list]:
    where = ["1=1"]
    params: list = []

    def eq(col, val):
        if val:
            where.append(f"{col} = %s")
            params.append(val)

    def num(col, op, val):
        if val not in (None, ""):
            where.append(f"{col} {op} %s")
            params.append(float(val) if "." in str(val) else int(val))

    eq("site", request.args.get("site"))
    eq("city", request.args.get("city"))
    eq("brand", request.args.get("brand"))
    eq("model", request.args.get("model"))
    eq("fuel", request.args.get("fuel"))
    eq("gear", request.args.get("gear"))
    eq("category", request.args.get("category"))
    num("price", ">=", request.args.get("min_price"))
    num("price", "<=", request.args.get("max_price"))
    num("year", ">=", request.args.get("min_year"))
    num("year", "<=", request.args.get("max_year"))
    num("km", "<=", request.args.get("max_km"))
    num("note_etat", ">=", request.args.get("min_note_etat"))
    num("note_achat", ">=", request.args.get("min_note_achat"))
    num("note_global", ">=", request.args.get("min_note_global"))
    num("note_confiance", ">=", request.args.get("min_confiance"))

    search = (request.args.get("search") or "").strip()
    if search:
        where.append("(title LIKE %s OR description LIKE %s OR seller LIKE %s)")
        like = f"%{search}%"
        params += [like, like, like]
    if request.args.get("with_phone") == "1":
        where.append("phone IS NOT NULL")
    if request.args.get("active_only") == "1":
        where.append("is_active = 1")
    if request.args.get("with_photos") == "1":
        where.append("nb_images > 0")
    if request.args.get("with_price") == "1":
        where.append("price IS NOT NULL")
    if request.args.get("with_details") == "1":
        where.append("details_fetched = 1")
    return " AND ".join(where), params


# -------------------------------------------------------------------- pages
@app.route("/")
def index():
    where, params = build_filters()
    order = SORTABLE.get(request.args.get("sort", "date"), "publish_date DESC")
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 24)))

    total = query(f"SELECT COUNT(*) n FROM ads WHERE {where}", tuple(params))[0]["n"]
    pages = max(1, (total + per_page - 1) // per_page)
    # une page hors borne (page=9999) est ramenee dans les limites
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = query(
        f"SELECT {', '.join(COLUMNS)} FROM ads WHERE {where} "
        f"ORDER BY {order} LIMIT %s OFFSET %s",
        tuple(params) + (per_page, offset),
    )

    # options pour les filtres
    opts = {}
    for col, label in (("city", "Villes"), ("brand", "Marques"),
                       ("fuel", "Carburants"), ("gear", "Boites")):
        opts[col] = [r[col] for r in query(
            f"SELECT DISTINCT {col} FROM ads WHERE {col} IS NOT NULL "
            f"ORDER BY {col}")]
    opts["models"] = model_options(request.args.get("brand", ""),
                                   request.args.get("site", ""))
    opts["sites"] = known_sites()
    # annees presentes dans le catalogue (pour les selects)
    years = [r["year"] for r in query(
        "SELECT DISTINCT year FROM ads WHERE year IS NOT NULL "
        "AND year BETWEEN 1950 AND 2100 ORDER BY year DESC")]
    opts["years"] = years

    return render_template(
        "list.html", rows=rows, total=total, page=page, pages=pages,
        per_page=per_page, opts=opts, args=request.args,
        pager=pager_items(page, pages), goto=goto_pages(page, pages),
        base_qs=urlencode({k: v for k, v in request.args.to_dict().items()
                           if k != "page"}),
        active=active_filters(),
    )


@app.route("/ad/<site>/<ad_id>")
def detail_site(site: str, ad_id: str):
    """Fiche d'une annonce (cle composite : site + ad_id).

    ad_id est une chaine : certains sites utilisent un identifiant
    alphanumerique ("dITj3MBMiY") ou un slug.
    """
    return _detail(site, ad_id)


@app.route("/ad/<int:ad_id>")
def detail(ad_id: int):
    """Fiche d'une annonce, site non precise (on prend la plus recente)."""
    rows = query("SELECT site FROM ads WHERE ad_id = %s "
                 "ORDER BY scraped_at DESC LIMIT 1", (ad_id,))
    if not rows:
        abort(404)
    return _detail(rows[0]["site"], ad_id)


def _detail(site: str, ad_id: str):
    rows = query("SELECT * FROM ads WHERE site = %s AND ad_id = %s", (site, ad_id))
    if not rows:
        abort(404)
    ad = rows[0]
    raw = ad.pop("raw_json", None)
    opt_json = ad.pop("options_json", None)
    options = {}
    for blob in (raw, opt_json):
        if not blob:
            continue
        try:
            data = json.loads(blob)
            options = data.get("options") or options
        except Exception:
            pass
    ad["options"] = options
    try:
        ad["notes_v2"] = json.loads(raw).get("notes_v2") if raw else None
    except Exception:
        ad["notes_v2"] = None

    images = query(
        "SELECT id, link, link_full, path_local, variant, bytes, is_main "
        "FROM ad_images WHERE site = %s AND ad_id = %s "
        "ORDER BY is_main DESC, id", (site, ad_id))
    for im in images:
        if im.get("path_local") and os.path.exists(im["path_local"]):
            im["local_url"] = "/images/" + im["path_local"].split("/images/", 1)[-1]
        else:
            im["local_url"] = None

    versions = query(
        "SELECT version, change_type, diff_json, snapshot_json, created "
        "FROM ad_versions WHERE site = %s AND ad_id = %s "
        "ORDER BY version DESC", (site, ad_id))
    for v in versions:
        try:
            v["diff"] = json.loads(v.pop("diff_json") or "{}")
        except Exception:
            v["diff"] = {}

    return render_template("detail.html", ad=ad, images=images, versions=versions)


@app.route("/favicon.ico")
def favicon():
    """Les navigateurs demandent /favicon.ico d'office."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static"), "favicon.svg",
        mimetype="image/svg+xml")


@app.route("/images/<path:filename>")
def images(filename: str):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/imgthumb/<site>/<ad_id>")
def imgthumb_site(site: str, ad_id: str):
    """Vignette d'une annonce (site + ad_id)."""
    return _imgthumb(site, ad_id)


@app.route("/imgthumb/<int:ad_id>")
def imgthumb(ad_id: int):
    """Vignette de l'annonce : image locale si telechargee, sinon l'URL distante."""
    rows = query(
        "SELECT site FROM ads WHERE ad_id = %s "
        "ORDER BY scraped_at DESC LIMIT 1", (ad_id,))
    if not rows:
        abort(404)
    return _imgthumb(rows[0]["site"], ad_id)


def _imgthumb(site: str, ad_id: str):
    rows = query(
        "SELECT link, link_full, path_local FROM ad_images "
        "WHERE site = %s AND ad_id = %s "
        "ORDER BY is_main DESC, id LIMIT 1", (site, ad_id))
    if not rows:
        abort(404)
    im = rows[0]
    if im.get("path_local") and os.path.exists(im["path_local"]):
        return send_from_directory(
            IMAGES_DIR, im["path_local"].split("/images/", 1)[-1])
    return redirect(im.get("link_full") or im["link"], code=302)


@app.route("/api/job")
def api_job():
    """Parametres du job d'un site (pour recharger le formulaire de pilotage)."""
    site = (request.args.get("site") or "").strip() or "avito"
    return jsonify(progress_reader.read_job(site))


@app.route("/api/sites")
def api_sites():
    """Sites scrapables : categories et modes (pour le formulaire de pilotage)."""
    return jsonify({"sites": known_sites()})


@app.route("/api/models")
def api_models():
    """Modeles disponibles, groupes par marque (pour le selecteur dynamique)."""
    brand = (request.args.get("brand") or "").strip()
    site = (request.args.get("site") or "").strip()
    return jsonify({"brand": brand, "groups": model_options(brand, site)})


@app.route("/api/ads")
def api_ads():
    where, params = build_filters()
    order = SORTABLE.get(request.args.get("sort", "date"), "publish_date DESC")
    limit = min(500, int(request.args.get("limit", 50)))
    rows = query(f"SELECT {', '.join(COLUMNS)} FROM ads WHERE {where} "
                 f"ORDER BY {order} LIMIT %s", tuple(params) + (limit,))
    return jsonify(rows)


# ------------------------------------------------------------------ suivi
def _duration(seconds) -> str:
    """Formate une duree en secondes : '2h14' / '14min' / '45s'."""
    try:
        sec = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if sec <= 0:
        return "-"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}min"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}"


app.jinja_env.filters["duree"] = _duration


def _container_state(site: str = "") -> str:
    """Etat du conteneur de scrape d'un site.

    'running' | 'exited' | 'created' | 'absent' (run termine, conteneur
    supprime) | 'unknown'
    """
    if not site:
        site = "avito"
    try:
        return scrapectl.state(site)
    except Exception:
        return "unknown"


def _container_states() -> dict:
    """Etat des conteneurs de scrape, par site (etat + libelle)."""
    out = {}
    try:
        raw = scrapectl.states([s["name"] for s in known_sites()])
    except Exception:
        raw = {}
    for site, st in raw.items():
        site_state = next((s for s in progress_reader.read_all()
                           if s.get("site") == site), None)
        label, css = progress_reader.container_status(st, site_state)
        out[site] = {"state": st, "label": label, "css": css}
    return out


def _runs_history(limit: int = 50, site: str = "", status: str = "",
                  mode: str = "") -> list[dict]:
    """Historique des runs (table scrape_runs), filtre par site/statut/mode."""
    where = []
    params: list = []
    if site:
        where.append("site = %s")
        params.append(site)
    if status:
        where.append("status = %s")
        params.append(status)
    if mode:
        where.append("mode = %s")
        params.append(mode)
    sql = (
        "SELECT id, started_at, finished_at, site, category, mode, ads_listed, "
        "ads_details, ads_inserted, ads_updated, ads_unchanged, ads_disappeared, "
        "http_requests, blocked, status, error FROM scrape_runs"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY id DESC LIMIT %s")
    params.append(limit)
    rows = query(sql, tuple(params))
    for r in rows:
        start = r.get("started_at")
        end = r.get("finished_at")
        if start and end:
            r["duration"] = int((end - start).total_seconds())
        else:
            r["duration"] = None
        # timestamp pour le tri (Jinja n'appelle pas les methodes)
        r["started_ts"] = int(start.timestamp()) if start else 0
    return rows


def _hide_category_column(runs: list[dict]) -> bool:
    """La colonne categorie est inutile si chaque site n'a que peu de categories."""
    per_site: dict = {}
    for r in runs:
        site = r.get("site") or "?"
        per_site.setdefault(site, set()).add(r.get("category") or "")
    if not per_site:
        return True
    return all(len(cats) <= 2 for cats in per_site.values())


def _sync_stale_runs(container_states: dict) -> None:
    """Marque 'stopped' les runs 'running' dont plus aucun scrap ne tourne.

    Si aucun conteneur de scrape n'est actif, les lignes restees a
    'running' ont ete interrompues (machine redemarree, conteneur tue).
    """
    if any(c.get("state") in ("running", "restarting")
           for c in container_states.values()):
        return
    try:
        execute("UPDATE scrape_runs SET status = 'stopped', "
                "error = COALESCE(error, 'interrompu') "
                "WHERE status = 'running'")
    except Exception:
        pass


@app.route("/tracking")
def tracking():
    """Page de suivi : live, historique, pilotage, alertes."""
    all_sites = known_sites()
    containers = _container_states()
    site = (request.args.get("job_site") or "").strip() or \
        (all_sites[0]["name"] if all_sites else "avito")
    job = progress_reader.read_job(site)
    container_state = containers.get(site, {}).get("state", "unknown")
    _sync_stale_runs(containers)
    states = progress_reader.read_all()
    state = progress_reader.pick_primary(states, container_state)
    alerts = progress_reader.alerts(state, container_state, states)
    f_site = (request.args.get("site") or "").strip()
    f_status = (request.args.get("status") or "").strip()
    f_mode = (request.args.get("mode") or "").strip()
    runs = _runs_history(50, f_site, f_status, f_mode)
    counts = query("SELECT site, COUNT(*) n, SUM(is_active = 1) actives, "
                   "SUM(details_fetched = 1) details, MAX(scraped_at) dernier, "
                   "SUM(nb_images > 0) avec_photos FROM ads GROUP BY site ORDER BY COUNT(*) DESC")
    c_label = containers.get(site, {}).get("label", "inconnu")
    c_css = containers.get(site, {}).get("css", "s-unknown")
    site_meta = next((s for s in all_sites if s.get("name") == site), None)
    job_categories = (site_meta or {}).get("categories", [])
    job_modes = (site_meta or {}).get("modes", ["list", "rescan", "details"])
    return render_template(
        "tracking.html", state=state, states=states, alerts=alerts, runs=runs,
        job=job, container_state=container_state, counts=counts,
        container_label=c_label, container_css=c_css,
        hide_category=_hide_category_column(runs),
        sites=all_sites, job_categories=job_categories, job_modes=job_modes,
        f_site=f_site, f_status=f_status, f_mode=f_mode,
        containers=containers, site=site,
        container_name=scrapectl.container_name(site))


@app.route("/api/containers")
def api_containers():
    """Etat de tous les conteneurs du projet."""
    try:
        return jsonify({"containers": scrapectl.list_containers()})
    except Exception as e:
        return jsonify({"containers": [], "error": str(e)})


@app.route("/api/progress")
def api_progress():
    """Etat live du scrap (JSON), appele en boucle par la page."""
    containers = _container_states()
    _sync_stale_runs(containers)
    site = (request.args.get("site") or "").strip() or "avito"
    container_state = containers.get(site, {}).get("state", "unknown")
    states = progress_reader.read_all()
    state = progress_reader.pick_primary(states, container_state)
    c_label = containers.get(site, {}).get("label", "inconnu")
    c_css = containers.get(site, {}).get("css", "s-unknown")
    return jsonify({
        "state": state,
        "states": states,
        "containers": containers,
        "site": site,
        "container": container_state,
        "container_label": c_label,
        "container_css": c_css,
        "alerts": progress_reader.alerts(state, container_state, states),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    })


def _job_from_request() -> dict:
    """Parametres du job depuis le formulaire (ou le JSON).

    Chaque site a ses propres parametres : on repart du job du site demande.
    """
    payload = request.get_json(silent=True) or request.form or {}
    site = str(payload.get("site") or "").strip() or "avito"
    job = progress_reader.read_job(site)
    for key in ("site", "category", "mode"):
        val = payload.get(key)
        if val:
            job[key] = str(val).strip()
    if "details" in payload:
        job["details"] = str(payload.get("details")).lower() in ("1", "true", "on", "yes")
    if "slices" in payload:
        job["slices"] = str(payload.get("slices")).lower() in ("1", "true", "on", "yes")
    for key in ("limit",):
        try:
            job[key] = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            pass
    for key in ("delay", "jitter"):
        try:
            job[key] = max(0.0, float(payload.get(key) or job.get(key, 1.5)))
        except (TypeError, ValueError):
            pass
    return job


@app.route("/api/scrape/start", methods=["POST"])
def api_scrape_start():
    """Demarre un scrape pour le site demande (conteneur ephemere).

    Les sites sont independants : avito et tomobila peuvent tourner en
    meme temps, chacun dans son conteneur.
    """
    job = _job_from_request()
    site = job.get("site") or "avito"
    progress_reader.write_job(job)
    try:
        msg = scrapectl.start(site, job)
    except scrapectl.DockerCliError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    return jsonify({"ok": True, "message": msg, "job": job,
                    "container": scrapectl.container_name(site)})


@app.route("/api/scrape/start-all", methods=["POST"])
def api_scrape_start_all():
    """Lance un scrape pour tous les sites, avec les parametres partages.

    Le parametre site est ignore : on reutilise la maniere specifique a chaque
    site (categorie par defaut). Le mode, le delay, le jitter, le plafond,
    les fiches detail et la decoupe en tranches sont partages.
    """
    payload = request.get_json(silent=True) or request.form or {}

    def _flag(v):
        return str(v).lower() in ("1", "true", "on", "yes")

    mode = str(payload.get("mode") or "").strip()
    limit_val = None
    if "limit" in payload and str(payload.get("limit") or "").strip():
        try:
            limit_val = max(0, int(payload.get("limit")))
        except (TypeError, ValueError):
            limit_val = None

    results = []
    for site_meta in known_sites():
        name = site_meta.get("name")
        if not name:
            continue
        # parametres specifiques au site (categorie en particulier)
        job = progress_reader.read_job(name)
        job["site"] = name
        if mode:
            job["mode"] = mode
        if limit_val is not None:
            job["limit"] = limit_val
        if "details" in payload:
            job["details"] = _flag(payload.get("details"))
        if "slices" in payload:
            job["slices"] = _flag(payload.get("slices"))
        for key in ("delay", "jitter"):
            if key in payload:
                try:
                    job[key] = max(0.0, float(payload.get(key)))
                except (TypeError, ValueError):
                    pass
        progress_reader.write_job(job)
        try:
            msg = scrapectl.start(name, job)
            results.append({"site": name, "ok": True, "message": msg,
                            "container": scrapectl.container_name(name)})
        except Exception as e:
            results.append({"site": name, "ok": False, "error": str(e)})

    return jsonify({"ok": all(r.get("ok") for r in results) if results else True,
                    "launched": sum(1 for r in results if r.get("ok")),
                    "total": len(results),
                    "results": results})


@app.route("/api/scrape/stop-all", methods=["POST"])
def api_scrape_stop_all():
    """Arrete tous les scraps (un par site)."""
    results = []
    for site_meta in known_sites():
        name = site_meta.get("name")
        if not name:
            continue
        try:
            msg = scrapectl.stop(name)
            results.append({"site": name, "ok": True, "message": msg})
        except scrapectl.DockerCliError as e:
            # pas de conteneur pour ce site : rien a faire
            if "Aucun scrape" in str(e):
                results.append({"site": name, "ok": True, "message": "aucun scrape en cours"})
            else:
                results.append({"site": name, "ok": False, "error": str(e)})
    return jsonify({"ok": True,
                    "stopped": sum(1 for r in results if r.get("ok")),
                    "total": len(results),
                    "results": results})


@app.route("/api/scrape/stop", methods=["POST"])
def api_scrape_stop():
    """Arrete le scrape d'un site (les autres sites continuent)."""
    job = _job_from_request()
    site = job.get("site") or "avito"
    try:
        msg = scrapectl.stop(site)
    except scrapectl.DockerCliError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "message": msg})


@app.route("/api/scrape/restart", methods=["POST"])
def api_scrape_restart():
    """Relance le scrape d'un site (meme parametres)."""
    job = _job_from_request()
    site = job.get("site") or "avito"
    progress_reader.write_job(job)
    try:
        msg = scrapectl.restart(site, job)
    except scrapectl.DockerCliError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "message": msg, "job": job})


@app.route("/api/scrape/logs")
def api_scrape_logs():
    """Dernieres lignes de log du scrape d'un site."""
    try:
        tail = max(10, min(500, int(request.args.get("tail", 120))))
    except (TypeError, ValueError):
        tail = 120
    site = (request.args.get("site") or "").strip() or "avito"
    try:
        lines = scrapectl.logs(site, tail=tail)
    except scrapectl.DockerCliError as e:
        lines = [f"(!) {e}"]
    return jsonify({"lines": lines[-tail:], "site": site,
                    "container": scrapectl.container_name(site)})


@app.route("/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
