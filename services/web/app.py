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

import pymysql
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_from_directory)

import dockerctl
import progress as progress_reader

app = Flask(__name__)
docker = dockerctl.Docker()

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
    "publish_date", "last_seen_at", "nb_images",
]

SORTABLE = {
    "date": "publish_date DESC",
    "price": "price ASC",
    "price_desc": "price DESC",
    "year": "year DESC",
    "km": "km ASC",
    "note": "note_global DESC, note DESC",
    "note_etat": "note_etat DESC",
    "note_achat": "note_achat DESC",
    "confiance": "note_confiance DESC",
    "recent": "scraped_at DESC",
}


def db():
    return pymysql.connect(**DB)


def query(sql, params=()):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
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
    return " AND ".join(where), params


# -------------------------------------------------------------------- pages
@app.route("/")
def index():
    where, params = build_filters()
    order = SORTABLE.get(request.args.get("sort", "date"), "publish_date DESC")
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, int(request.args.get("per_page", 24)))

    total = query(f"SELECT COUNT(*) n FROM ads WHERE {where}", tuple(params))[0]["n"]
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

    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "list.html", rows=rows, total=total, page=page, pages=pages,
        per_page=per_page, opts=opts, args=request.args,
        pager=pager_items(page, pages),
    )


@app.route("/ad/<site>/<int:ad_id>")
def detail_site(site: str, ad_id: int):
    """Fiche d'une annonce (cle composite : site + ad_id)."""
    return _detail(site, ad_id)


@app.route("/ad/<int:ad_id>")
def detail(ad_id: int):
    """Fiche d'une annonce, site non precise (on prend la plus recente)."""
    rows = query("SELECT site FROM ads WHERE ad_id = %s "
                 "ORDER BY scraped_at DESC LIMIT 1", (ad_id,))
    if not rows:
        abort(404)
    return _detail(rows[0]["site"], ad_id)


def _detail(site: str, ad_id: int):
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


@app.route("/images/<path:filename>")
def images(filename: str):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/imgthumb/<site>/<int:ad_id>")
def imgthumb_site(site: str, ad_id: int):
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


def _imgthumb(site: str, ad_id: int):
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


def _container_state() -> str:
    """Etat du conteneur scraper ('running', 'exited', 'absent'...)."""
    try:
        return docker.state()
    except Exception:
        return "unknown"


def _runs_history(limit: int = 50) -> list[dict]:
    """Historique des runs (table scrape_runs)."""
    rows = query(
        "SELECT id, started_at, finished_at, site, category, mode, ads_listed, "
        "ads_details, ads_inserted, ads_updated, ads_unchanged, ads_disappeared, "
        "http_requests, blocked, status, error FROM scrape_runs "
        "ORDER BY id DESC LIMIT %s", (limit,))
    for r in rows:
        start = r.get("started_at")
        end = r.get("finished_at")
        if start and end:
            r["duration"] = int((end - start).total_seconds())
        else:
            r["duration"] = None
    return rows


def _sync_stale_runs(container_state: str) -> None:
    """Marque 'stopped' les runs 'running' dont le conteneur n'est plus actif."""
    if container_state == "running":
        return
    try:
        query("UPDATE scrape_runs SET status = 'stopped', "
              "error = COALESCE(error, 'interrompu') "
              "WHERE status = 'running'")
    except Exception:
        pass


@app.route("/tracking")
def tracking():
    """Page de suivi : live, historique, pilotage, alertes."""
    container_state = _container_state()
    _sync_stale_runs(container_state)
    states = progress_reader.read_all()
    state = progress_reader.pick_primary(states, container_state)
    alerts = progress_reader.alerts(state, container_state, states)
    job = progress_reader.read_job()
    runs = _runs_history(50)
    counts = query("SELECT site, COUNT(*) n, SUM(is_active = 1) actives, "
                   "MAX(scraped_at) dernier FROM ads GROUP BY site")
    return render_template(
        "tracking.html", state=state, states=states, alerts=alerts, runs=runs,
        job=job, container_state=container_state, counts=counts,
        container_name=dockerctl.CONTAINER_NAME)


@app.route("/api/progress")
def api_progress():
    """Etat live du scrap (JSON), appele en boucle par la page."""
    container_state = _container_state()
    _sync_stale_runs(container_state)
    states = progress_reader.read_all()
    state = progress_reader.pick_primary(states, container_state)
    return jsonify({
        "state": state,
        "states": states,
        "container": container_state,
        "alerts": progress_reader.alerts(state, container_state, states),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    })


def _job_from_request() -> dict:
    """Parametres du job depuis le formulaire (ou le JSON)."""
    job = progress_reader.read_job()
    payload = request.get_json(silent=True) or request.form or {}
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
    """Demarre (ou relance) le conteneur scraper avec les parametres demandes."""
    job = _job_from_request()
    progress_reader.write_job(job)
    try:
        state = docker.state()
        if state == "running":
            docker.restart()
            msg = "Scrap relance"
        elif state == "absent":
            return jsonify({"ok": False,
                            "error": f"Conteneur {dockerctl.CONTAINER_NAME} absent. "
                                     "Creez-le avec : docker compose create scraper"}), 409
        else:
            docker.start()
            msg = "Scrap demarre"
    except dockerctl.DockerError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    return jsonify({"ok": True, "message": msg, "job": job,
                    "container": dockerctl.CONTAINER_NAME})


@app.route("/api/scrape/stop", methods=["POST"])
def api_scrape_stop():
    """Arrete le scrap en cours."""
    try:
        state = docker.state()
        if state == "absent":
            return jsonify({"ok": False, "error": "Aucun conteneur scraper"}), 409
        docker.stop(timeout=10)
    except dockerctl.DockerError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    return jsonify({"ok": True, "message": "Scrap arrete"})


@app.route("/api/scrape/restart", methods=["POST"])
def api_scrape_restart():
    """Relance le scrap en cours (meme parametres)."""
    job = _job_from_request()
    progress_reader.write_job(job)
    try:
        state = docker.state()
        if state == "absent":
            return jsonify({"ok": False,
                            "error": f"Conteneur {dockerctl.CONTAINER_NAME} absent"}), 409
        if state == "running":
            docker.restart()
        else:
            docker.start()
    except dockerctl.DockerError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    return jsonify({"ok": True, "message": "Scrap relance", "job": job})


@app.route("/api/scrape/logs")
def api_scrape_logs():
    """Dernieres lignes de log du conteneur scraper."""
    try:
        tail = max(10, min(500, int(request.args.get("tail", 120))))
    except (TypeError, ValueError):
        tail = 120
    try:
        text = docker.logs(tail=tail)
    except dockerctl.DockerError as e:
        text = f"(!) {e}"
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return jsonify({"lines": lines[-tail:], "container": dockerctl.CONTAINER_NAME})


@app.route("/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
