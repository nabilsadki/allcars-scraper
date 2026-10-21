"""Interface web du catalogue Avito (liste, filtres, detail, scoring).

Lise la base MySQL du compose (service `db`) et sert les images
telechargees si elles existent dans le volume /data.

Routes
------
GET  /                    liste + filtres (query string)
GET  /ad/<id>             fiche d'une annonce (donnees, photos, scoring, historique)
GET  /images/<path>       image locale du volume
GET  /api/ads             meme liste en JSON
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pymysql
from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_from_directory)

app = Flask(__name__)

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
    "ad_id", "title", "price", "year", "km", "fuel", "gear", "brand", "model",
    "city", "area", "note", "note_etat", "note_achat", "note_global",
    "note_confiance", "version", "is_active", "seller", "phone",
    "publish_date", "nb_images",
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


# ------------------------------------------------------------------ filtres
def build_filters() -> tuple[str, list]:
    where = ["site = 'avito'"]
    params: list = []

    def eq(col, val):
        if val:
            where.append(f"{col} = %s")
            params.append(val)

    def num(col, op, val):
        if val not in (None, ""):
            where.append(f"{col} {op} %s")
            params.append(float(val) if "." in str(val) else int(val))

    eq("city", request.args.get("city"))
    eq("brand", request.args.get("brand"))
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

    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "list.html", rows=rows, total=total, page=page, pages=pages,
        per_page=per_page, opts=opts, args=request.args,
    )


@app.route("/ad/<int:ad_id>")
def detail(ad_id: int):
    rows = query("SELECT * FROM ads WHERE ad_id = %s", (ad_id,))
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
        "FROM ad_images WHERE ad_id = %s ORDER BY is_main DESC, id", (ad_id,))
    for im in images:
        if im.get("path_local") and os.path.exists(im["path_local"]):
            im["local_url"] = "/images/" + im["path_local"].split("/images/", 1)[-1]
        else:
            im["local_url"] = None

    versions = query(
        "SELECT version, change_type, diff_json, snapshot_json, created "
        "FROM ad_versions WHERE ad_id = %s ORDER BY version DESC", (ad_id,))
    for v in versions:
        try:
            v["diff"] = json.loads(v.pop("diff_json") or "{}")
        except Exception:
            v["diff"] = {}

    return render_template("detail.html", ad=ad, images=images, versions=versions)


@app.route("/images/<path:filename>")
def images(filename: str):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/imgthumb/<int:ad_id>")
def imgthumb(ad_id: int):
    """Vignette de l'annonce : image locale si telechargee, sinon l'URL distante."""
    rows = query(
        "SELECT link, link_full, path_local FROM ad_images "
        "WHERE ad_id = %s ORDER BY is_main DESC, id LIMIT 1", (ad_id,))
    if not rows:
        abort(404)
    im = rows[0]
    if im.get("path_local") and os.path.exists(im["path_local"]):
        return send_from_directory(
            IMAGES_DIR, im["path_local"].split("/images/", 1)[-1])
    return redirect(im.get("link_full") or im["link"], code=302)


@app.route("/api/ads")
def api_ads():
    where, params = build_filters()
    order = SORTABLE.get(request.args.get("sort", "date"), "publish_date DESC")
    limit = min(500, int(request.args.get("limit", 50)))
    rows = query(f"SELECT {', '.join(COLUMNS)} FROM ads WHERE {where} "
                 f"ORDER BY {order} LIMIT %s", tuple(params) + (limit,))
    return jsonify(rows)


@app.route("/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
