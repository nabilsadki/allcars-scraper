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
from .core.progress import install_signal_handlers
from .core.registry import available_sites, get_site, publish, site_info
from .core.scoring import compute_notes, compute_notes_v2, load_config
from .core.site_indexer import push as push_site_index
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
    if not (args.category or "").strip():
        args.category = getattr(site, "default_category", "") or ""
    db = _db(args) if args.db else None
    client = _client(args)
    started = datetime.now()
    run_id = None
    if db:
        run_id = db.start_run({"started_at": started, "site": site.name,
                               "category": args.category,
                               "mode": "list+detail" if args.details else "list"})
    try:
        res = run_list(site, client, db, category=args.category, limit=args.limit,
                       details=args.details, out=args.out, quiet=args.quiet,
                       start=started, run_id=run_id)
    finally:
        if db:
            db.finish_run(run_id, {
                "finished_at": datetime.now(),
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
    if not (args.category or "").strip():
        args.category = getattr(site, "default_category", "") or ""
    db = _db(args)
    client = _client(args)
    started = datetime.now()
    run_id = db.start_run({"started_at": started, "site": site.name,
                           "category": args.category, "mode": "rescan"})
    try:
        res = run_rescan(site, client, db, category=args.category, limit=args.limit,
                         details=args.details, mark_missing=args.mark_missing,
                         quiet=args.quiet, run_id=run_id)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.finish_run(run_id, {"finished_at": datetime.now(), "status": "error",
                               "error": err})
        db.close()
        print(f"  ERREUR : {err}", file=sys.stderr)
        return 1
    db.finish_run(run_id, {
        "finished_at": datetime.now(),
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
    if not (args.category or "").strip() and getattr(args, "category", None) is not None:
        args.category = getattr(site, "default_category", "") or ""
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
    sql = ("SELECT ad_id, brand, model, year, price, km, category, raw_json "
           "FROM ads WHERE site = %s"
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
            ad = {}
        ad.setdefault("ad_id", r.get("ad_id"))
        ad.setdefault("site", site.name)
        ad.setdefault("brand", r.get("brand"))
        ad.setdefault("model", r.get("model"))
        if ad.get("year") is None and r.get("year") is not None:
            ad["year"] = r["year"]
        if ad.get("price") is None and r.get("price") is not None:
            ad["price"] = r["price"]
        if ad.get("km") is None and r.get("km") is not None:
            ad["km"] = r["km"]
            ad.setdefault("mileage_km", r.get("km"))
        ad.setdefault("category", r.get("category"))
        # _apply_notes englobe scoring legacy + v2 + body_type + anomalie prix
        try:
            from .core.pipeline import _apply_notes
            ad = _apply_notes(ad, market=market, config=config)
        except Exception:
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


def cmd_reindex_site(args) -> int:
    """Pousse l'etat courant de la BDD vers le site via /api/reindex."""
    url = args.url or os.environ.get("SITE_REINDEX_URL", "http://host.docker.internal:8081/api/reindex")
    token = args.token or os.environ.get("SITE_REINDEX_TOKEN", "")
    if not url:
        print("URL manquante (--url ou SITE_REINDEX_URL)", file=sys.stderr)
        return 2
    if not token:
        print("token manquant (--token ou SITE_REINDEX_TOKEN)", file=sys.stderr)
        return 2
    print("export et push de la BDD vers le site...")
    started = datetime.now()
    try:
        res = push_site_index(url, token,
                              batch_size=max(100, int(getattr(args, "batch", 1000))),
                              limit=args.limit)
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"termine en {datetime.now() - started}: {res}")
    return 0


def cmd_clean(args) -> int:
    """Supprime les annonces qui n'appartiennent pas a la categorie attendue.

    Les pages de resultats d'Avito injectent des annonces sponsorisees
    d'autres categories : ce sont les annonces dont le nom de categorie
    ne commence pas par le prefixe attendu ("Voiture" par defaut).
    """
    db = _db(args)
    site = _site(args)
    prefix = args.keep or "Voiture"
    like = prefix + "%"
    junk = db.query(
        "SELECT ad_id, title, category FROM ads WHERE site = %s "
        "AND (category IS NULL OR category NOT LIKE %s)", (site.name, like))
    ids = [r["ad_id"] for r in junk]
    print(f"  {len(ids)} annonce(s) hors categorie '{like}'")
    for r in junk[:20]:
        print(f"    - {r['ad_id']} | {r['category'] or 'sans categorie'} | "
              f"{(r['title'] or '')[:50]}")
    if len(junk) > 20:
        print(f"    ... et {len(junk) - 20} autres")
    if not ids or args.dry_run:
        db.close()
        return 0
    marks = ",".join(["%s"] * len(ids))
    db.cur.execute(f"DELETE FROM ad_images WHERE site = %s AND ad_id IN ({marks})",
                   [site.name] + ids)
    db.cur.execute(f"DELETE FROM ad_versions WHERE site = %s AND ad_id IN ({marks})",
                   [site.name] + ids)
    db.cur.execute(f"DELETE FROM ads WHERE site = %s AND ad_id IN ({marks})",
                   [site.name] + ids)
    db.conn.commit()
    print(f"  {len(ids)} annonce(s) supprimee(s)")
    db.close()
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


def cmd_cleanup_stale(args) -> int:
    """Retire des annonces anciennes: test vie detail URL (404 => désactivée)."""
    setattr(args, "delay", getattr(args, "delay", 1.5))
    setattr(args, "jitter", getattr(args, "jitter", 0.8))
    site = _site(args)
    db = _db(args)
    client = _client(args)
    idle_days = int(getattr(args, "idle_days", 30) or 30)
    limit = int(getattr(args, "limit", 500) or 500)
    dry_run = bool(getattr(args, "dry_run", False))
    quiet = bool(getattr(args, "quiet", False))
    try:
        rows = db.query(
            "SELECT ad_id, link FROM ads "
            "WHERE site = %s AND is_active = 1 "
            "AND (last_seen_at IS NULL OR last_seen_at < (NOW() - INTERVAL %s DAY)) "
            "ORDER BY last_seen_at LIMIT %s",
            (site.name, idle_days, limit),
        )
        if not quiet:
            print(f"  {len(rows)} candidat(s) inactifs depuis >{idle_days}j", flush=True)
        if dry_run:
            for r in rows:
                print("  -", r.get("ad_id"), r.get("link"))
            return 0
        deactivated: list[str] = []
        touched = 0
        for r in rows:
            ad_id = r.get("ad_id")
            link = r.get("link")
            if not ad_id:
                continue
            try:
                html = client.get(link) if link else None
                status = 200
            except Exception as e:
                status = 404 if ("HTTP 404" in str(e) or "HTTP 410" in str(e)) else None
            if status == 404 or status == 410:
                db.cur.execute(
                    "UPDATE ads SET is_active = 0, details_fetched = 1, "
                    "last_seen_at = NOW() WHERE site = %s AND ad_id = %s",
                    (site.name, ad_id),
                )
                deactivated.append(str(ad_id))
            elif status == 200:
                db.cur.execute(
                    "UPDATE ads SET last_seen_at = NOW() WHERE site = %s AND ad_id = %s",
                    (site.name, ad_id),
                )
                touched += 1
            else:
                continue
            # Meilisearch partial update
            if len(deactivated) % 50 == 0:
                db.commit()
        db.commit()
        # push inactive flags to index (partial update by id)
        if deactivated:
            import json as _json, urllib.request as _ur
            url = os.environ.get("SITE_REINDEX_URL",
                                 "http://allcars-site-web:8080/api/reindex").replace("reindex", "reindex")
            # Meilisearch direct (more reliable than full site export)
            meili_url = os.environ.get("MEILISEARCH_URL")
            meili_key = os.environ.get("MEILISEARCH_KEY")
            if not meili_url:
                meili_url = os.environ.get("MEILISEARCH_URL", "http://meilisearch:7700")
            if not meili_key:
                meili_key = os.environ.get("MEILISEARCH_KEY", "allcars-site-master-key")
            payload = [{"id": f"{site.name}_{aid}", "is_active": False} for aid in deactivated]
            # batch 100
            pushed = 0
            for i in range(0, len(payload), 100):
                try:
                    data = _json.dumps(payload[i:i + 100]).encode("utf-8")
                    req = _ur.Request(f"{meili_url}/indexes/ads/documents",
                                      data=data, method="PUT")
                    req.add_header("Content-Type", "application/json")
                    req.add_header("Authorization", f"Bearer {meili_key}")
                    with _ur.urlopen(req, timeout=60) as r:
                        _ = r.read()
                    pushed += len(data and payload[i:i + 100])
                except Exception as e:
                    if not quiet:
                        print(f"  ! Meilisearch push echec: {e}", file=sys.stderr)
            if not quiet:
                print(f"  {len(deactivated)} annonce(s) desactivees dans la BDD "
                      f"(+{touched} encore vivantes), index Meilisearch mis a jour "
                      f"({pushed} docs)", flush=True)
        else:
            if not quiet:
                print("  aucune annonce a desactiver", flush=True)
    finally:
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

    sr = sub.add_parser("reindex-site",
                      help="pousse l'etat courant de la BDD vers le site web")
    sr.add_argument("--url", default="", help="URL cible (defaut SITE_REINDEX_URL)")
    sr.add_argument("--token", default="", help="cle d'auth (defaut SITE_REINDEX_TOKEN)")
    sr.add_argument("--batch", type=int, default=int(os.environ.get("SITE_REINDEX_BATCH", "1000")),
                    dest="batch", help="taille de lot")
    sr.add_argument("--limit", type=int, default=0, help="limite de docs (test)")
    sr.set_defaults(func=cmd_reindex_site)

    s7 = sub.add_parser("clean", help="supprimer les annonces hors categorie")
    s7.add_argument("--site", default="avito")
    s7.add_argument("--keep", default="Voiture",
                    help="prefixe du nom de categorie a conserver")
    s7.add_argument("--dry-run", action="store_true")
    s7.set_defaults(func=cmd_clean)

    sc = sub.add_parser("cleanup-stale",
                       help="desactive les annonces non vues depuis N jours (details 404)")
    sc.add_argument("--site", default="avito")
    sc.add_argument("--idle-days", type=int, default=30, dest="idle_days")
    sc.add_argument("--limit", type=int, default=500)
    sc.add_argument("--db", action="store_true")
    sc.add_argument("--dry-run", action="store_true", dest="dry_run")
    sc.add_argument("--quiet", action="store_true")
    sc.set_defaults(func=cmd_cleanup_stale)

    s6 = sub.add_parser("sites", help="liste des sites supportes")
    s6.set_defaults(func=cmd_sites)

    args = p.parse_args(argv)
    install_signal_handlers()
    publish()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
