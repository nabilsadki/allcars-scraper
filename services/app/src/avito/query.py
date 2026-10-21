"""Outil de consultation de la base, entierement parametrable.

Exemples
--------
  # annonces recentes
  docker compose run --rm tools ads --limit 20

  # voitures diesel a Rabat, 100k-200k DH, >= 2018
  docker compose run --rm tools ads --city Rabat --fuel Diesel \
        --min-price 100000 --max-price 200000 --min-year 2018

  # recherche plein texte dans le titre
  docker compose run --rm tools ads --search "golf" --order price --limit 10

  # fiche complete d'une annonce
  docker compose run --rm tools ad 76261364

  # statistiques
  docker compose run --rm tools stats

  # repartition par ville / marque / carburant
  docker compose run --rm tools group-by city
  docker compose run --rm tools group-by brand --limit 20

  # export
  docker compose run --rm tools ads --city Casablanca --format csv --out /data/casablanca.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from .db import Database

TABLE_COLS = [
    "ad_id", "title", "price", "year", "km", "fuel", "gear", "brand",
    "model", "city", "note", "note_etat", "note_achat", "note_global",
    "note_confiance", "version", "is_active", "seller", "phone",
    "publish_date", "link",
]

GROUPABLE = ["city", "brand", "fuel", "gear", "year", "seller", "area", "category"]


def _filters(args) -> tuple[str, list]:
    where, params = [], []
    if getattr(args, "city", None):
        where.append("city = %s")
        params.append(args.city)
    if getattr(args, "brand", None):
        where.append("brand = %s")
        params.append(args.brand)
    if getattr(args, "fuel", None):
        where.append("fuel = %s")
        params.append(args.fuel)
    if getattr(args, "gear", None):
        where.append("gear = %s")
        params.append(args.gear)
    if getattr(args, "min_price", None) is not None:
        where.append("price >= %s")
        params.append(args.min_price)
    if getattr(args, "max_price", None) is not None:
        where.append("price <= %s")
        params.append(args.max_price)
    if getattr(args, "min_year", None) is not None:
        where.append("year >= %s")
        params.append(args.min_year)
    if getattr(args, "max_year", None) is not None:
        where.append("year <= %s")
        params.append(args.max_year)
    if getattr(args, "max_km", None) is not None:
        where.append("km <= %s")
        params.append(args.max_km)
    if getattr(args, "with_phone", False):
        where.append("phone IS NOT NULL")
    if getattr(args, "min_note", None) is not None:
        where.append("note >= %s")
        params.append(args.min_note)
    if getattr(args, "min_note_etat", None) is not None:
        where.append("note_etat >= %s")
        params.append(args.min_note_etat)
    if getattr(args, "min_note_achat", None) is not None:
        where.append("note_achat >= %s")
        params.append(args.min_note_achat)
    if getattr(args, "min_note_global", None) is not None:
        where.append("note_global >= %s")
        params.append(args.min_note_global)
    if getattr(args, "min_confiance", None) is not None:
        where.append("note_confiance >= %s")
        params.append(args.min_confiance)
    if getattr(args, "max_note", None) is not None:
        where.append("note <= %s")
        params.append(args.max_note)
    if getattr(args, "active_only", False):
        where.append("is_active = 1")
    if getattr(args, "min_version", None) is not None:
        where.append("version >= %s")
        params.append(args.min_version)
    if getattr(args, "details_only", False):
        where.append("details_fetched = 1")
    if getattr(args, "search", None):
        where.append("(title LIKE %s OR description LIKE %s OR seller LIKE %s)")
        like = f"%{args.search}%"
        params += [like, like, like]
    return (" WHERE " + " AND ".join(where)) if where else "", params


def cmd_ads(args) -> int:
    db = Database()
    where, params = _filters(args)
    order = {
        "price": "price ASC", "price_desc": "price DESC", "date": "publish_date DESC",
        "year": "year DESC", "km": "km ASC", "relevance": "scraped_at DESC",
        "note": "note_global DESC, note_global IS NULL, scraped_at DESC",
        "note_global": "note_global DESC, note_global IS NULL, scraped_at DESC",
        "note_etat": "note_etat DESC, scraped_at DESC",
        "note_achat": "note_achat DESC, scraped_at DESC",
        "confiance": "note_confiance DESC, scraped_at DESC",
        "legacy": "note DESC, scraped_at DESC",
    }.get(args.order, "scraped_at DESC")
    cols = ", ".join(args.columns.split(",")) if args.columns else ", ".join(TABLE_COLS)
    sql = f"SELECT {cols} FROM ads{where} ORDER BY {order} LIMIT %s"
    rows = db.query(sql, tuple(params) + (args.limit,))
    _emit(rows, args)
    db.close()
    return 0


def cmd_ad(args) -> int:
    db = Database()
    row = db.query("SELECT * FROM ads WHERE ad_id = %s", (args.ad_id,))
    if not row:
        print(f"annonce {args.ad_id} introuvable", file=sys.stderr)
        db.close()
        return 1
    a = row[0]
    if a.get("options_json"):
        try:
            a["options"] = json.loads(a["options_json"])
        except Exception:
            a["options"] = a["options_json"]
    imgs = db.query(
        "SELECT link, link_full, is_main FROM ad_images WHERE ad_id = %s ORDER BY is_main DESC",
        (args.ad_id,),
    )
    a["images"] = imgs
    if args.explain:
        _explain(a)
    else:
        a.pop("raw_json", None)
        a.pop("options_json", None)
        print(json.dumps(a, ensure_ascii=False, default=str, indent=2))
    db.close()
    return 0


def _explain(a: dict) -> None:
    """Detail des notes : criteres, poids, valeurs, confiance."""
    raw = a.get("raw_json")
    v2 = None
    if raw:
        try:
            v2 = json.loads(raw).get("notes_v2")
        except Exception:
            v2 = None
    print(f"=== {a.get('title')} ===")
    print(f"  {a.get('brand')} {a.get('model')} | {a.get('year')} | {a.get('km')} km | "
          f"{a.get('price')} DH | {a.get('city')}")
    print()
    print(f"  NOTE LEGACY : {a.get('note')}   (annee={a.get('note_year')} "
          f"km={a.get('note_km')} ratio={a.get('note_ratio')} "
          f"cv={a.get('note_cv')} equip={a.get('note_specs')})")
    if not v2:
        print("  NOTE V2      : non calcule (lancer 'app score')")
        return
    print(f"  NOTE V2 etat  : {v2.get('note_etat')}  (confiance {v2.get('confiance_etat')}%)")
    print(f"  NOTE V2 achat : {v2.get('note_achat')}  (confiance {v2.get('confiance_achat')}%)")
    print(f"  NOTE V2 global: {v2.get('note_global')}  (confiance globale {v2.get('note_confiance')}%)")
    print()
    print("  detail etat :")
    for k, v in (v2.get("detail_etat") or {}).items():
        print(f"    {k:12s} = {v}")
    print("  detail achat:")
    for k, v in (v2.get("detail_achat") or {}).items():
        print(f"    {k:16s} = {v}")
    if v2.get("marche"):
        m = v2["marche"]
        print(f"  marche : prix {m.get('ratio')}x la moyenne du segment "
              f"({m.get('reference')}, {m.get('n')} annonces)")


def cmd_stats(args) -> int:
    db = Database()
    rows = db.query("""
        SELECT COUNT(*) total, COUNT(DISTINCT city) villes, COUNT(DISTINCT brand) marques,
               MIN(year) annee_min, MAX(year) annee_max,
               MIN(price) prix_min, MAX(price) prix_max, ROUND(AVG(price)) prix_moyen,
               SUM(details_fetched) avec_detail, SUM(phone IS NOT NULL) avec_tel,
               MAX(scraped_at) dernier_run
        FROM ads
    """)
    print(json.dumps(rows[0], ensure_ascii=False, default=str, indent=2))
    db.close()
    return 0


def cmd_group_by(args) -> int:
    if args.field not in GROUPABLE:
        print(f"champ non groupable: {args.field} (au choix: {', '.join(GROUPABLE)})",
              file=sys.stderr)
        return 1
    db = Database()
    rows = db.query(
        f"SELECT {args.field} AS valeur, COUNT(*) AS n, ROUND(AVG(price)) AS prix_moyen, "
        f"MIN(price) AS prix_min, MAX(price) AS prix_max "
        f"FROM ads WHERE {args.field} IS NOT NULL GROUP BY {args.field} "
        f"ORDER BY n DESC LIMIT %s",
        (args.limit,),
    )
    _emit(rows, args)
    db.close()
    return 0


def cmd_history(args) -> int:
    """Historique des versions d'une annonce (ou de toutes les annonces modifiees)."""
    db = Database()
    if args.ad_id:
        rows = db.query(
            "SELECT v.id, v.ad_id, v.version, v.change_type, v.diff_json, "
            "v.details_fetched, v.created FROM ad_versions v "
            "WHERE v.ad_id = %s ORDER BY v.version",
            (args.ad_id,),
        )
    else:
        rows = db.query(
            "SELECT v.id, v.ad_id, v.version, v.change_type, v.diff_json, "
            "v.details_fetched, v.created FROM ad_versions v "
            "ORDER BY v.created DESC LIMIT %s",
            (args.limit,),
        )
    out = []
    for r in rows:
        r = dict(r)
        try:
            r["diff"] = json.loads(r.pop("diff_json") or "{}")
        except Exception:
            r["diff"] = r.pop("diff_json")
        out.append(r)
    if args.verbose:
        for r in out:
            print(f"\n[v{r['version']}] {r['change_type']} — {r['created']}")
            for k, v in (r.get("diff") or {}).items():
                print(f"    {k}: {str(v.get('old'))[:60]} -> {str(v.get('new'))[:60]}")
    else:
        _emit(out, args)
    db.close()
    return 0


def cmd_versions(args) -> int:
    """Annonces multi-versions (les plus scrappees/changees)."""
    db = Database()
    rows = db.query(
        "SELECT ad_id, title, version, seen_count, price, last_seen_at, note "
        "FROM ads WHERE version > 1 ORDER BY version DESC, last_seen_at DESC LIMIT %s",
        (args.limit,),
    )
    _emit(rows, args)
    db.close()
    return 0


def cmd_runs(args) -> int:
    db = Database()
    rows = db.query(
        "SELECT id, started_at, finished_at, category, mode, ads_listed, ads_details, "
        "ads_inserted, http_requests, blocked, status, error FROM scrape_runs "
        "ORDER BY id DESC LIMIT %s",
        (args.limit,),
    )
    _emit(rows, args)
    db.close()
    return 0


def _emit(rows: list[dict], args) -> None:
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
        return
    if args.out:
        if args.format == "csv":
            cols = list(rows[0].keys()) if rows else []
            with open(args.out, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        else:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, default=str, indent=2)
        print(f"{len(rows)} lignes -> {args.out}", file=sys.stderr)
        return
    if not rows:
        print("(aucun resultat)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(_s(r.get(c))) for r in rows)) for c in cols}
    widths = {c: min(w, 45) for c, w in widths.items()}
    print(" | ".join(c.ljust(widths[c])[:widths[c]] for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(_s(r.get(c)).ljust(widths[c])[:widths[c]] for c in cols))


def _s(v) -> str:
    return "" if v is None else str(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="query", description="Consultation de la base avito")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--limit", type=int, default=50)
    common.add_argument("--format", choices=["table", "json", "csv"], default="table")
    common.add_argument("--out", default="")

    s = sub.add_parser("ads", parents=[common], help="lister les annonces")
    s.add_argument("--city")
    s.add_argument("--brand")
    s.add_argument("--fuel")
    s.add_argument("--gear")
    s.add_argument("--min-price", type=int, dest="min_price")
    s.add_argument("--max-price", type=int, dest="max_price")
    s.add_argument("--min-year", type=int, dest="min_year")
    s.add_argument("--max-year", type=int, dest="max_year")
    s.add_argument("--max-km", type=int, dest="max_km")
    s.add_argument("--search", help="recherche dans titre/description/vendeur")
    s.add_argument("--with-phone", action="store_true")
    s.add_argument("--details-only", action="store_true")
    s.add_argument("--min-note", type=float, dest="min_note")
    s.add_argument("--min-note-etat", type=float, dest="min_note_etat")
    s.add_argument("--min-note-achat", type=float, dest="min_note_achat")
    s.add_argument("--min-note-global", type=float, dest="min_note_global")
    s.add_argument("--min-confiance", type=float, dest="min_confiance")
    s.add_argument("--max-note", type=float, dest="max_note")
    s.add_argument("--active-only", action="store_true", dest="active_only")
    s.add_argument("--min-version", type=int, dest="min_version")
    s.add_argument("--columns", default="", help="colonnes personnalisees (defaut: standard)")
    s.add_argument("--order", default="date",
                   choices=["price", "price_desc", "date", "year", "km", "relevance",
                            "note", "note_global", "note_etat", "note_achat",
                            "confiance", "legacy"])
    s.set_defaults(func=cmd_ads)

    s2 = sub.add_parser("ad", parents=[common], help="fiche d'une annonce")
    s2.add_argument("ad_id", type=int)
    s2.add_argument("--explain", action="store_true", help="detail des notes")
    s2.set_defaults(func=cmd_ad)

    s3 = sub.add_parser("stats", parents=[common], help="statistiques globales")
    s3.set_defaults(func=cmd_stats)

    s4 = sub.add_parser("group-by", parents=[common], help="repartition par champ")
    s4.add_argument("field", choices=GROUPABLE)
    s4.set_defaults(func=cmd_group_by)

    s5 = sub.add_parser("runs", parents=[common], help="historique des executions")
    s5.set_defaults(func=cmd_runs)

    s6 = sub.add_parser("history", parents=[common], help="historique des versions")
    s6.add_argument("--ad-id", type=int, dest="ad_id",
                    help="annonce precise (sinon : dernieres versions)")
    s6.add_argument("--verbose", action="store_true", help="afficher le detail des changements")
    s6.set_defaults(func=cmd_history)

    s7 = sub.add_parser("versions", parents=[common],
                        help="annonces ayant change (version > 1)")
    s7.set_defaults(func=cmd_versions)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
