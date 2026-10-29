"""Pousse l'etat actuel de la base MySQL du scrapper vers le site All Cars.

Le site expose une route POST /api/reindex qui relais en bloc vers Meilisearch.
Le scrapper est le seul acteur qui lit la BDD et la source de verite.

Usage :
    app reindex-site [--url URL] [--token TOKEN] [--batch N] [--limit N]
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime

import pymysql
from pymysql.cursors import DictCursor


def _date(v):
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    return str(v)


def _dec(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).lower() in ("1", "true", "on", "yes")


def make_doc_id(site, ad_id):
    raw = f"{site}_{ad_id}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)


def load_docs(limit=0):
    conn = pymysql.connect(
        host=os.environ.get("DB_HOST", "db"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "allcars"),
        password=os.environ.get("DB_PASSWORD", "allcars"),
        database=os.environ.get("DB_NAME", "allcars"),
        charset="utf8mb4",
        cursorclass=DictCursor,
    )
    cur = conn.cursor()

    # image principale par annonce
    cur.execute("SELECT site, ad_id, MIN(link) AS link FROM ad_images GROUP BY site, ad_id")
    images = {}
    for r in cur.fetchall():
        images[(r["site"], str(r["ad_id"]))] = r["link"]

    sql = "SELECT * FROM ads"
    params = []
    if limit and int(limit) > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    cur.execute(sql, tuple(params))

    total = 0
    batch_docs = []
    for r in cur:
        site = r.get("site")
        ad_id = str(r.get("ad_id"))
        doc = {
            "id": make_doc_id(site, ad_id),
            "site": site,
            "ad_id": ad_id,
            "category": r.get("category"),
            "title": r.get("title"),
            "price": r.get("price"),
            "publish_date": _date(r.get("publish_date")),
            "city": r.get("city"),
            "area": r.get("area"),
            "seller": r.get("seller"),
            "seller_type": r.get("seller_type"),
            "phone": r.get("phone"),
            "fuel": r.get("fuel"),
            "gear": r.get("gear"),
            "cv": r.get("cv"),
            "km": r.get("km"),
            "year": r.get("year"),
            "brand": r.get("brand"),
            "model": r.get("model"),
            "color": r.get("color"),
            "car_body": r.get("car_body"),
            "doors": r.get("doors"),
            "fiscal_power": r.get("fiscal_power"),
            "origin": r.get("origin"),
            "first_hand": r.get("first_hand"),
            "cond": r.get("cond"),
            "body_type": r.get("body_type"),
            "nb_images": r.get("nb_images") or 0,
            "details_fetched": _bool(r.get("details_fetched")),
            "is_active": _bool(r.get("is_active", 1)),
            "version": r.get("version"),
            "content_hash": r.get("content_hash"),
            "seen_count": r.get("seen_count"),
            "first_seen_at": _date(r.get("first_seen_at")),
            "last_seen_at": _date(r.get("last_seen_at")),
            "note": _dec(r.get("note")),
            "note_year": _dec(r.get("note_year")),
            "note_km": _dec(r.get("note_km")),
            "note_ratio": _dec(r.get("note_ratio")),
            "note_cv": _dec(r.get("note_cv")),
            "note_sunroof": _dec(r.get("note_sunroof")),
            "note_fuel": _dec(r.get("note_fuel")),
            "note_gear": _dec(r.get("note_gear")),
            "note_specs": _dec(r.get("note_specs")),
            "km_year": r.get("km_year"),
            "note_etat": _dec(r.get("note_etat")),
            "note_achat": _dec(r.get("note_achat")),
            "note_global": _dec(r.get("note_global")),
            "note_confiance": _dec(r.get("note_confiance")),
            "created": _date(r.get("created")),
            "updated": _date(r.get("updated")),
            "scraped_at": _date(r.get("scraped_at")),
            "link": r.get("link", r.get("url")),
            "image": images.get((site, ad_id)),
            "price_anomaly": _bool(r.get("price_anomaly")),
            "price_anomaly_reason": r.get("price_anomaly_reason"),
            "list_time": None,
        }
        raw = r.get("raw_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, dict):
            doc["description"] = raw.get("description")
        else:
            doc["description"] = None
        batch_docs.append(doc)
        total += 1
    cur.close()
    conn.close()
    return total, batch_docs


def push(url, token, batch_size=1000, limit=0):
    total_docs, docs = load_docs(limit=limit)
    if total_docs == 0:
        return {"accepted": 0}

    sent = 0
    for i in range(0, len(docs), batch_size):
        chunk = docs[i:i + batch_size]
        body = json.dumps({"documents": chunk}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Reindex-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            resp = json.loads(e.read() or b"{}")
            raise RuntimeError(f"reindex HTTP {e.code}: {resp}")
        sent += len(chunk)
        print(f"  {sent}/{total_docs} documents pousses", flush=True)
    return {"total": total_docs, "accepted": sent}
