"""
CLI du scraper, multi-sites.

    app list    --site avito --category "maroc/voitures-à_vendre" --db
    app rescan  --site avito --details
    app details --site avito --limit 200
    app score   --site avito
    app images  --variant images --limit 50
    app sites
    app stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from .core.db import Database
from .core.http import HttpClient
from .core.pipeline import run_details, run_list, run_rescan
from .core.registry import available_sites, get_site, site_info
from .core.scoring import compute_notes, compute_notes_v2, load_config
from . import sites as _sites  # noqa: F401  (enregistre les adaptateurs)


def _client(args) -> HttpClient:
    return HttpClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)


def _db(args) -> Database:
    db = Database()
    db.ensure_schema()
    return db


def cmd_sites(args) -> int:
    for s in site_info():
        print(f"  {s['name']:10s} {s['label']}")
        print(f"     defaut  : {s['default_category']}")
        if s["categories"]:
            print(f"     categories : {', '.join(s['categories'])}")
    return 0


def _site(args):
    """Recupere l'adaptateur et applique les options."""
    site = get_site(args.site)
    if getattr(args, "no_slices", False):
        site.use_slices = False
    return site


def cmd_list(args) -> int:
    site = _site(args)
    db = _db(args) if args.db else None
    client = _client(args)
    started = datetime.now()
    try:
        res = run_list(site, client, db, category=args.category, limit=args.limit,
                       details=args.details, out=args.out, quiet=args.quiet,
                       start=started)
    finally:
        if db:
            db.touch_run({
                "started_at": started, "finished_at": datetime.now(),
                "category": args.category, "mode": "details" if args.details else "list",
                "ads_listed": res.get("listed", 0), "ads_details": res.get("details", 0),
                "ads_inserted": db.counters["inserted"], "ads_updated": db.counters["updated"],
                "ads_unchanged": db.counters["unchanged"],
                "ads_disappeared": db.counters["disappeared"],
                "http_requests": client.stats["requests"], "blocked": client.stats["blocked"],
                "status": "error" if res.get("error") else "success",
                "error": res.get("error"),
            })
            db.close()
    return 1 if res.get("error") else 0


def cmd_rescan(args) -> int:
    site = _site(args)
    db = _db(args)
    client = _client(args)
    try:
        res = run_rescan(site, client, db, category=args.category, limit=args.limit,
                         details=args.details, mark_missing=args.mark_missing,
                         quiet=args.quiet)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.touch_run({"started_at": datetime.now(), "category": args.category,
                      "mode": "rescan", "status": "error", "error": err})
        db.close()
        print(f"  ERREUR : {err}", file=sys.stderr)
        return 1
    db.touch_run({
        "started_at": res.get("started"), "finished_at": datetime.now(),
        "category": args.category, "mode": "rescan",
        "ads_listed": res.get("seen", 0), "ads_details": res.get("details", 0),
        "ads_inserted": res.get("new", 0), "ads_updated": res.get("changed", 0),
        "ads_unchanged": res.get("unchanged", 0),
        "ads_disappeared": res.get("disappeared", 0),
        "http_requests": client.stats["requests"], "blocked": client.stats["blocked"],
        "status": "success",
    })
    db.close()
    return 0


def cmd_details(args) -> int:
    site = _site(args)
    db = _db(args)
    client = _client(args)
    try:
        run_details(site, client, db, limit=args.limit, category=args.category,
                    quiet=args.quiet)
    finally:
        db.close()
    return 0


def cmd_score(args) -> int:
    site = get_site(args.site)
    db = _db(args)
    config = load_config()
    from .core.market import MarketStats
    try:
        market = MarketStats(db)
    except Exception:
        market = None
    sql = ("SELECT raw_json FROM ads WHERE site = %s"
           + (" AND category = %s" if args.category else "")
           + " ORDER BY ad_id")
    params: list = [site.name]
    if args.category:
        params.append(args.category)
    if args.limit:
        sql += " LIMIT %s"
        params.append(args.limit)
    rows = db.query(sql, tuple(params))
    n = 0
    for r in rows:
        try:
            ad = json.loads(r["raw_json"])
        except Exception:
            continue
        v2 = compute_notes_v2(ad, market=market, config=config)
        ad["note_etat"] = v2["note_etat"]
        ad["note_achat"] = v2["note_achat"]
        ad["note_global"] = v2["note_global"]
        ad["note_confiance"] = v2["note_confiance"]
        ad["notes_v2"] = v2
        db.upsert_ad(ad, site=site.name)
        n += 1
    db.commit()
    db.close()
    print(f"{n} annonces rescoredes")
    return 0


def cmd_stats(args) -> int:
    db = Database()
    rows = db.query("""
        SELECT site, COUNT(*) total, SUM(is_active = 1) actives,
               SUM(details_fetched = 1) avec_detail, SUM(phone IS NOT NULL) avec_tel,
               COUNT(DISTINCT city) villes, COUNT(DISTINCT brand) marques,
               ROUND(AVG(note_global), 2) note_moyenne, MAX(note_global) note_max,
               MAX(scraped_at) dernier_run
        FROM ads GROUP BY site
    """)
    print(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
    db.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scrape", description="Scraper multi-sites (Docker)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_site=True, default_site="avito"):
        if with_site:
            sp.add_argument("--site", default=default_site,
                            help=f"site a scraper ({', '.join(available_sites())})")
        sp.add_argument("--category", default="")
        sp.add_argument("--limit", type=int, default=0)
        sp.add_argument("--db", action="store_true", help="ecrire dans MySQL")
        sp.add_argument("--out", default="", help="fichier JSONL de sortie")
        sp.add_argument("--delay", type=float, default=1.5)
        sp.add_argument("--jitter", type=float, default=0.8)
        sp.add_argument("--no-brand", action="store_true")
        sp.add_argument("--no-slices", action="store_true",
                        help="ne pas decouper la categorie en tranches")
        sp.add_argument("--quiet", action="store_true")
        return sp

    s = common(sub.add_parser("list", help="lister les annonces d'une categorie"))
    s.add_argument("--details", action="store_true", help="fetcher la fiche complete")
    s.set_defaults(func=cmd_list)

    s2 = common(sub.add_parser("rescan", help="re-scraping intelligent"))
    s2.add_argument("--details", action="store_true", default=True)
    s2.add_argument("--mark-missing", action="store_true",
                    help="marquer inactives les annonces disparues (run complet)")
    s2.set_defaults(func=cmd_rescan)

    s3 = common(sub.add_parser("details", help="completer les fiches detail manquantes"),
                with_site=True)
    s3.set_defaults(func=cmd_details)

    s4 = common(sub.add_parser("score", help="recalculer les notes"))
    s4.set_defaults(func=cmd_score)

    s5 = sub.add_parser("stats", help="statistiques par site")
    s5.set_defaults(func=cmd_stats)

    s6 = sub.add_parser("sites", help="liste des sites supportes")
    s6.set_defaults(func=cmd_sites)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
