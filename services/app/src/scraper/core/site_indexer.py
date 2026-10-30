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
from datetime import datetime, timedelta, timezone
from statistics import median

import pymysql
from pymysql.cursors import DictCursor


BODY_GROUPS = (
    ("suv", "SUV & Crossovers", ("SUV", "Crossover", "4x4", "4X4"), "bg-indigo-600"),
    ("berline", "Berlines Routières", ("Berline", "Break"), "bg-blue-600"),
    ("citadine", "Citadines polyvalentes", ("Citadine", "Compact"), "bg-emerald-500"),
    ("4x4", "4x4 & Pick-up Pro", ("Utilitaire", "Pick-up", "Pickup"), "bg-amber-500"),
)
BODY_GROUP_BY_TYPE = {
    variant.lower(): (key, display, color)
    for key, display, variants, color in BODY_GROUPS
    for variant in variants
}

CATEGORY_DELTA_YEAR = 2023
CATEGORY_DELTA_BASE_YEAR = 2022


def _body_group(value):
    if not value:
        return None
    return BODY_GROUP_BY_TYPE.get(str(value).strip().lower())


def _fuel_bucket(value):
    text = str(value or "").lower()
    if "diesel" in text:
        return "diesel"
    if "essence" in text:
        return "essence"
    if "hybr" in text or "elect" in text or "élect" in text:
        return "hybride"
    return "autre"


def _parse_dt(value):
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _boolish(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "oui", "yes", "vrai", "o")


def _deal_bucket(delta_pct):
    if delta_pct is None:
        return "unknown"
    if delta_pct <= -10:
        return "super"
    if delta_pct <= 5:
        return "fair"
    return "above"


def _seller_key(value):
    text = str(value or "").strip().lower()
    if not text:
        return "inconnu"
    if "particul" in text or "priv" in text or "individual" in text:
        return "particulier"
    return "professionnel"


def _warranty_in_text(text):
    value = str(text or "").lower()
    return "garantie" in value or "warranty" in value


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

    # toutes les images par annonce, en conservant l'ordre d'insertion
    cur.execute("""
        SELECT site, ad_id,
               JSON_ARRAYAGG(link) AS imgs,
               JSON_ARRAYAGG(COALESCE(link_full, link)) AS imgs_hd
        FROM (SELECT site, ad_id, link, link_full, id FROM ad_images ORDER BY site, ad_id, id) x
        GROUP BY site, ad_id
    """)
    images = {}
    images_hd = {}
    for r in cur.fetchall():
        links = r.get("imgs")
        links_hd = r.get("imgs_hd")
        if isinstance(links, str):
            try:
                links = json.loads(links)
            except Exception:
                links = None
        if isinstance(links_hd, str):
            try:
                links_hd = json.loads(links_hd)
            except Exception:
                links_hd = None
        images[(r["site"], str(r["ad_id"]))] = links if isinstance(links, list) else []
        images_hd[(r["site"], str(r["ad_id"]))] = links_hd if isinstance(links_hd, list) else []

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
            "image": (images.get((site, ad_id)) or [None])[0],
            "image_hd": (images_hd.get((site, ad_id)) or [None])[0],
            "images": images.get((site, ad_id)) or [],
            "images_hd": images_hd.get((site, ad_id)) or [],
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
        options = r.get("options_json")
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except Exception:
                options = None
        if isinstance(options, dict):
            doc["options"] = options
        batch_docs.append(doc)
        total += 1
    cur.close()
    conn.close()
    return total, batch_docs


def _doc_listing_dt(doc):
    dt = _parse_dt(doc.get("publish_date")) or _parse_dt(doc.get("first_seen_at"))
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def enrich_docs(docs):
    """Ajoute les champs analytiques utilises par la page listing."""
    model_prices, brand_year_prices = {}, {}
    model_all_prices, brand_prices = {}, {}
    for doc in docs:
        price = _dec(doc.get("price"))
        if price is None or price < 10000 or price > 3000000 or _bool(doc.get("price_anomaly")):
            continue
        brand = doc.get("brand")
        model = doc.get("model")
        year = doc.get("year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        if brand:
            brand_prices.setdefault(brand, []).append(price)
        if brand and year is not None:
            brand_year_prices.setdefault((brand, year), []).append(price)
        if brand and model:
            model_all_prices.setdefault((brand, model), []).append(price)
        if brand and model and year is not None:
            model_prices.setdefault((brand, model, year), []).append(price)

    market_model = {
        key: median(values) for key, values in model_prices.items() if len(values) >= 3
    }
    market_brand_year = {
        key: median(values) for key, values in brand_year_prices.items() if len(values) >= 5
    }
    market_model_all = {
        key: median(values) for key, values in model_all_prices.items() if len(values) >= 5
    }
    market_brand = {
        key: median(values) for key, values in brand_prices.items() if len(values) >= 20
    }

    for doc in docs:
        price = _dec(doc.get("price"))
        brand = doc.get("brand")
        model = doc.get("model")
        year = doc.get("year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        market = None
        if price is not None and not _bool(doc.get("price_anomaly")):
            market = (market_model.get((brand, model, year))
                      or market_brand_year.get((brand, year))
                      or market_model_all.get((brand, model))
                      or market_brand.get(brand))
        delta = pct = None
        bucket = "unknown"
        market_anomaly = False
        if market and price is not None and market > 0:
            delta = price - market
            pct = delta / market * 100.0
            market_anomaly = pct < -50 or pct > 100
            if market_anomaly:
                market = None
                delta = None
                pct = None
            else:
                bucket = _deal_bucket(pct)
        doc["market_price"] = round(market, 2) if market else None
        doc["price_delta"] = round(delta, 2) if delta is not None else None
        doc["price_delta_pct"] = round(pct, 2) if pct is not None else None
        doc["deal_bucket"] = bucket
        doc["market_anomaly"] = market_anomaly
        doc["monthly_payment"] = round(price * 0.8 / 48) if price else None
        doc["photos_count"] = max(
            int(doc.get("nb_images") or 0),
            len(doc.get("images") or []),
        )
        title = str(doc.get("title") or "")
        description = str(doc.get("description") or "")
        first_hand = (_boolish(doc.get("first_hand"))
                      or "1ere main" in title.lower()
                      or "première main" in title.lower())
        warranty = (_warranty_in_text(title + " " + description)
                    or doc.get("site") in ("autocash", "kia", "renew"))
        doc["first_hand_key"] = "oui" if first_hand else "non"
        doc["warranty_key"] = "oui" if warranty else "non"
        doc["details_key"] = "oui" if _bool(doc.get("details_fetched")) else "non"
        doc["photos_key"] = "oui" if max(
            int(doc.get("nb_images") or 0), len(doc.get("images") or [])) > 0 else "non"
        doc["seller_key"] = _seller_key(doc.get("seller_type"))
        doc["verified"] = bool(doc.get("details_fetched")) or doc.get("site") in ("autocash", "kia", "renew")
    return docs


def build_stats(docs, key="global", label="Toutes les annonces", since=None,
                stats_year=None):
    """Construit un document `stats/<key>` a partir des documents exportes.

    Le site ne fait aucune agregation SQL : il lit ces documents dans
    Meilisearch pour alimenter l'observatoire, les sources et la couverture.
    """
    if since is not None:
        selected = []
        for doc in docs:
            dt = _doc_listing_dt(doc)
            if dt is not None and dt >= since:
                selected.append(doc)
        docs = selected
    total = len(docs)
    stats_year = int(stats_year or datetime.now(timezone.utc).year)
    active_count = 0
    price_sum = 0.0
    price_count = 0
    site_counts = {}
    fuel_counts = {}
    fuel_bucket_counts = {"diesel": 0, "essence": 0, "hybride": 0, "autre": 0}
    body_counts = {}
    brand_counts = {}
    brand_price_sum = {}
    brand_price_count = {}
    city_counts = {}
    body_price_sum = {}
    body_price_count = {}
    year_price_sum = {}
    year_price_count = {}
    group_price_sum = {}
    group_price_count = {}
    group_year_price_sum = {}
    group_year_price_count = {}
    model_year_price_sum = {}
    model_year_price_count = {}
    presence_sum = 0.0
    presence_count = 0
    inactive_presence_sum = 0.0
    inactive_presence_count = 0
    deal_bucket_counts = {"super": 0, "fair": 0, "above": 0, "unknown": 0}
    price_delta_sum = 0.0
    price_delta_count = 0

    for doc in docs:
        site = doc.get("site")
        active = bool(doc.get("is_active", True))
        active_count += int(active)
        site_counts[site] = site_counts.get(site, 0) + 1
        fuel = doc.get("fuel") or "Inconnu"
        fuel_counts[fuel] = fuel_counts.get(fuel, 0) + 1
        fuel_bucket_counts[_fuel_bucket(fuel)] += 1
        body = doc.get("body_type") or "Inconnu"
        body_counts[body] = body_counts.get(body, 0) + 1
        brand = doc.get("brand")
        if brand:
            brand_counts[brand] = brand_counts.get(brand, 0) + 1
        city = doc.get("city")
        if city:
            city_counts[city] = city_counts.get(city, 0) + 1

        first = _parse_dt(doc.get("first_seen_at"))
        last = _parse_dt(doc.get("last_seen_at"))
        if first and last and last > first:
            days = (last - first).total_seconds() / 86400.0
            presence_sum += days
            presence_count += 1
            if not active:
                inactive_presence_sum += days
                inactive_presence_count += 1

        price = doc.get("price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        price_sum += price
        price_count += 1
        if doc.get("price_anomaly") or not (10000 <= price <= 3000000):
            continue
        delta_pct = doc.get("price_delta_pct")
        if delta_pct is not None:
            try:
                delta_pct = float(delta_pct)
                deal_bucket_counts[_deal_bucket(delta_pct)] += 1
                price_delta_sum += delta_pct
                price_delta_count += 1
            except (TypeError, ValueError):
                pass
        body_price_sum[body] = body_price_sum.get(body, 0) + price
        body_price_count[body] = body_price_count.get(body, 0) + 1
        if brand:
            brand_price_sum[brand] = brand_price_sum.get(brand, 0) + price
            brand_price_count[brand] = brand_price_count.get(brand, 0) + 1
        year = doc.get("year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        if year is not None:
            year_price_sum[year] = year_price_sum.get(year, 0) + price
            year_price_count[year] = year_price_count.get(year, 0) + 1
            if brand and doc.get("model"):
                model_key = (brand, doc.get("model"), year)
                model_year_price_sum[model_key] = model_year_price_sum.get(model_key, 0) + price
                model_year_price_count[model_key] = model_year_price_count.get(model_key, 0) + 1
        group = _body_group(body)
        if group:
            gkey, glabel, gcolor = group
            group_price_sum[gkey] = group_price_sum.get(gkey, 0) + price
            group_price_count[gkey] = group_price_count.get(gkey, 0) + 1
            if year is not None:
                gy = (gkey, year)
                group_year_price_sum[gy] = group_year_price_sum.get(gy, 0) + price
                group_year_price_count[gy] = group_year_price_count.get(gy, 0) + 1

    ref_year = max(year_price_count) if year_price_count else datetime.now(timezone.utc).year

    def cohort_depreciation(year):
        ratios, weights = [], []
        for (brand, model, y), sum_model in model_year_price_sum.items():
            if y != year - 3:
                continue
            key0 = (brand, model, year)
            if key0 not in model_year_price_sum:
                continue
            c3 = model_year_price_count.get((brand, model, y), 0)
            c0 = model_year_price_count.get(key0, 0)
            if c3 < 3 or c0 < 3:
                continue
            avg3 = sum_model / c3
            avg0 = model_year_price_sum[key0] / c0
            if avg0 <= 0:
                continue
            ratios.append((avg3 - avg0) / avg0 * 100.0)
            weights.append(min(c3, c0))
        if not ratios:
            avg0 = (year_price_sum.get(year, 0) / year_price_count[year]
                    if year_price_count.get(year) else None)
            avg3 = (year_price_sum.get(year - 3, 0) / year_price_count[year - 3]
                    if year_price_count.get(year - 3) else None)
            if avg0 and avg3:
                return round((avg3 - avg0) / avg0 * 100.0, 1), 0
            return None, 0
        weighted = sum(r * w for r, w in zip(ratios, weights)) / sum(weights)
        return round(weighted, 1), len(ratios)

    depreciation, depreciation_cohorts = cohort_depreciation(ref_year)
    previous_depreciation, previous_cohorts = cohort_depreciation(ref_year - 1)
    depreciation_delta = None
    if depreciation is not None and previous_depreciation is not None:
        depreciation_delta = round(depreciation - previous_depreciation, 1)

    if inactive_presence_count:
        avg_sale_days = inactive_presence_sum / inactive_presence_count
        sale_basis = "inactive"
        sale_count = inactive_presence_count
    elif presence_count:
        avg_sale_days = presence_sum / presence_count
        sale_basis = "active_presence"
        sale_count = presence_count
    else:
        avg_sale_days = None
        sale_basis = "none"
        sale_count = 0

    year_stats = [
        {"year": y, "count": year_price_count[y],
         "avg_price": round(year_price_sum[y] / year_price_count[y], 2)}
        for y in sorted(year_price_count)
        if year_price_count.get(y)
    ]
    brand_value_stats = [
        {"brand": b, "count": brand_price_count[b],
         "avg_price": round(brand_price_sum[b] / brand_price_count[b], 2)}
        for b in brand_price_count
        if brand_price_count.get(b) >= 100
    ]
    brand_value_stats.sort(key=lambda x: -x["avg_price"])

    category_price_stats = []
    max_group_avg = 0.0
    for gkey, glabel, glabels, gcolor in BODY_GROUPS:
        win_sum = 0.0
        win_count = 0
        for y in range(2018, stats_year + 1):
            gy = (gkey, y)
            win_sum += group_year_price_sum.get(gy, 0)
            win_count += group_year_price_count.get(gy, 0)
        if win_count == 0:
            win_sum = group_price_sum.get(gkey, 0)
            win_count = group_price_count.get(gkey, 0)
        if win_count == 0:
            continue
        avg_price = win_sum / win_count
        max_group_avg = max(max_group_avg, avg_price)
        avg_current = None
        avg_previous = None
        if group_year_price_count.get((gkey, CATEGORY_DELTA_YEAR)):
            avg_current = (group_year_price_sum[(gkey, CATEGORY_DELTA_YEAR)]
                           / group_year_price_count[(gkey, CATEGORY_DELTA_YEAR)])
        if group_year_price_count.get((gkey, CATEGORY_DELTA_BASE_YEAR)):
            avg_previous = (group_year_price_sum[(gkey, CATEGORY_DELTA_BASE_YEAR)]
                            / group_year_price_count[(gkey, CATEGORY_DELTA_BASE_YEAR)])
        delta_pct = None
        if avg_current and avg_previous:
            delta_pct = round((avg_current - avg_previous) / avg_previous * 100.0, 1)
        category_price_stats.append({
            "key": gkey, "label": glabel, "avg_price": round(avg_price, 0),
            "count": win_count, "delta_pct": delta_pct, "color": gcolor,
        })
    for row in category_price_stats:
        row["width_pct"] = (round(row["avg_price"] / max_group_avg * 100, 0)
                            if max_group_avg else 0)

    total_fuel = (fuel_bucket_counts.get("diesel", 0)
                  + fuel_bucket_counts.get("essence", 0)
                  + fuel_bucket_counts.get("hybride", 0))
    fuel_base = total_fuel or 1
    fuel_breakdown = {
        "diesel": fuel_bucket_counts.get("diesel", 0),
        "essence": fuel_bucket_counts.get("essence", 0),
        "hybride": fuel_bucket_counts.get("hybride", 0),
        "autre": fuel_bucket_counts.get("autre", 0),
        "total": total_fuel,
        "diesel_pct": round(fuel_bucket_counts.get("diesel", 0) / fuel_base * 100),
        "essence_pct": round(fuel_bucket_counts.get("essence", 0) / fuel_base * 100),
        "hybride_pct": round(fuel_bucket_counts.get("hybride", 0) / fuel_base * 100),
    }
    classified = (deal_bucket_counts.get("super", 0)
                  + deal_bucket_counts.get("fair", 0)
                  + deal_bucket_counts.get("above", 0))
    deal_bucket_counts["unknown"] = max(0, price_count - classified)

    return {
        "id": key,
        "period": key,
        "period_label": label,
        "period_start": since.isoformat() if since else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats_year": stats_year,
        "total_documents": total,
        "active_count": active_count,
        "with_price_count": price_count,
        "avg_price": round(price_sum / price_count, 2) if price_count else None,
        "site_stats": [
            {"name": k, "count": v} for k, v in sorted(site_counts.items(), key=lambda x: -x[1])
        ],
        "fuel_distribution": fuel_counts,
        "fuel_breakdown": fuel_breakdown,
        "deal_bucket_distribution": deal_bucket_counts,
        "avg_price_delta_pct": (
            round(price_delta_sum / price_delta_count, 1)
            if price_delta_count else None
        ),
        "body_type_distribution": body_counts,
        "brand_stats": [
            {"brand": k, "count": v} for k, v in sorted(brand_counts.items(), key=lambda x: -x[1])[:30]
        ],
        "brand_value_stats": brand_value_stats[:30],
        "top_brands": [
            {"brand": k, "count": v} for k, v in sorted(brand_counts.items(), key=lambda x: -x[1])[:5]
        ],
        "top_value_brands": brand_value_stats[:5],
        "city_stats": [
            {"city": k, "count": v} for k, v in sorted(city_counts.items(), key=lambda x: -x[1])[:30]
        ],
        "price_by_year": year_stats,
        "price_by_body_type": [
            {"body_type": bt, "avg_price": round(body_price_sum[bt] / body_price_count[bt], 2),
             "count": body_price_count[bt]}
            for bt in body_price_count if body_price_count.get(bt)
        ],
        "category_price_stats": category_price_stats,
        "depreciation_3y": {
            "pct": depreciation,
            "previous_pct": previous_depreciation,
            "delta_pct": depreciation_delta,
            "cohorts": depreciation_cohorts,
            "previous_cohorts": previous_cohorts,
            "reference_year": ref_year,
            "previous_reference_year": ref_year - 1,
        },
        "sale_speed": {
            "days": round(avg_sale_days, 1) if avg_sale_days is not None else None,
            "delta_days": None,
            "basis": sale_basis,
            "count": sale_count,
        },
        "coverage": {
            "sites": len(site_counts),
            "cities": len(city_counts),
        },
    }


def build_stats_documents(docs):
    """Retourne les documents global, 30 jours, 6 mois et annee courante."""
    now = datetime.now(timezone.utc)
    year = now.year
    periods = [
        ("30d", "30 derniers jours", now - timedelta(days=30)),
        ("6m", "6 derniers mois", now - timedelta(days=183)),
        ("year", f"Année {year}", datetime(year, 1, 1, tzinfo=timezone.utc)),
    ]
    documents = [build_stats(docs, "global", "Toutes les annonces", None, year)]
    documents += [
        build_stats(docs, key, label, since, year)
        for key, label, since in periods
    ]
    return documents


def push(url, token, batch_size=1000, limit=0):
    total_docs, docs = load_docs(limit=limit)
    if total_docs == 0:
        return {"accepted": 0}
    docs = enrich_docs(docs)

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

    if limit:
        print("  statistiques non poussees (mode --limit)", flush=True)
        return {"total": total_docs, "accepted": sent, "stats": False}

    stats = build_stats_documents(docs)
    body = json.dumps({"stats": stats}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Reindex-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        resp = json.loads(e.read() or b"{}")
        raise RuntimeError(f"stats HTTP {e.code}: {resp}")
    print("  document de statistiques pousse", flush=True)
    return {"total": total_docs, "accepted": sent, "stats": True}
