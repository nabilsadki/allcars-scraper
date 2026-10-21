"""
CLI du scraper.

Modes
-----
list    : scrape une categorie et ecrit en base (avec versioning)
rescan  : re-passe sur la categorie SANS re-scraper les annonces inchangees.
          La page de liste suffit a detecter les changements : seule la fiche
          detail des annonces modifiees est recuperee.
details : recupere les fiches detail manquantes d'annonces deja connues
score   : recalcule les notes (et la version si le score a change)
stats   : statistiques de la base
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from .catalog import Catalog
from .client import AvitoClient
from .db import Database
from .images import VARIANTS, ImageFetcher
from .market import MarketStats
from .scoring import compute_notes, compute_notes_v2, load_config

DEFAULT_CATEGORY = "maroc/voitures-à_vendre"


def _apply_notes(ad: dict, market=None, config=None) -> dict:
    """Applique le scoring legacy ET le scoring v2 sur l'annonce."""
    notes = compute_notes(ad)
    ad["note"] = notes["global"]
    ad["note_year"] = notes["annee"]
    ad["note_km"] = notes["km"]
    ad["note_ratio"] = notes["ratio"]
    ad["note_cv"] = notes["cv"]
    ad["note_sunroof"] = notes["toit"]
    ad["note_fuel"] = notes["carburant"]
    ad["note_gear"] = notes["boite"]
    ad["note_specs"] = notes["specs"]
    ad["km_year"] = notes["km_year"]

    # --- v2 : etat / achat / global + confiance
    v2 = compute_notes_v2(ad, market=market, config=config)
    ad["note_etat"] = v2["note_etat"]
    ad["note_achat"] = v2["note_achat"]
    ad["note_global"] = v2["note_global"]
    ad["note_confiance"] = v2["note_confiance"]
    ad["notes_v2"] = v2
    return ad


def _market(db):
    """Statistiques de marche (prix median par segment)."""
    if db is None:
        return None
    try:
        m = MarketStats(db)
        return m
    except Exception:
        return None


def _catalog(client, args):
    if args.no_brand:
        return None
    cat = Catalog(client)
    try:
        cat.fetch()
    except Exception as e:
        if not args.quiet:
            print(f"  ! catalogue indisponible ({e})")
        return None
    return cat


def cmd_list(args) -> int:
    db = None
    if args.db:
        db = Database()
        db.ensure_schema()

    client = AvitoClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)
    catalog = _catalog(client, args)
    config = load_config()
    market = _market(db)
    started = datetime.now()
    t0 = time.time()
    n_listed = n_details = 0
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    err = None

    try:
        for ad in client.iter_listing(args.category, max_ads=args.limit):
            n_listed += 1
            if catalog:
                ad["brand"], ad["model"] = catalog.parse_brand_model(ad.get("title") or "")
            if args.details:
                ad = client.detail(ad)
                n_details += 1
            _apply_notes(ad, market=market, config=config)
            if out:
                out.write(json.dumps(ad, ensure_ascii=False, default=str) + "\n")
            if db:
                db.upsert_ad(ad)
                db.upsert_images(ad.get("ad_id"), ad.get("images"))
            if not args.quiet and n_listed % 25 == 0:
                print(f"  ... {n_listed} annonces", flush=True)
        if db:
            db.commit()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"  ERREUR : {err}", file=sys.stderr)
    finally:
        if out:
            out.close()
        if db:
            db.touch_run({
                "started_at": started, "finished_at": datetime.now(),
                "category": args.category, "mode": "details" if args.details else "list",
                "ads_listed": n_listed, "ads_details": n_details,
                "ads_inserted": db.counters["inserted"], "ads_updated": db.counters["updated"],
                "ads_unchanged": db.counters["unchanged"],
                "ads_disappeared": db.counters["disappeared"],
                "http_requests": client.stats["requests"], "blocked": client.stats["blocked"],
                "status": "error" if err else "success", "error": err,
            })
            db.close()

    dt = time.time() - t0
    if not args.quiet:
        print(f"\nTermine : {n_listed} annonces listees, {n_details} fiches detail, "
              f"{dt:.0f}s | {client.stats['requests']} requetes HTTP | "
              f"{client.stats['blocked']} blocages CF")
    return 1 if err else 0


def cmd_rescan(args) -> int:
    """Re-scraping intelligent : seules les annonces modifiees sont detaillees."""
    db = Database()
    db.ensure_schema()
    client = AvitoClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)
    catalog = _catalog(client, args)
    config = load_config()
    market = _market(db)
    started = datetime.now()
    t0 = time.time()

    known_before = set(db.known_ad_ids())
    seen: set[int] = set()
    n_changed = n_new = n_unchanged = n_details = n_candidates = 0

    try:
        for ad in client.iter_listing(args.category, max_ads=args.limit):
            ad_id = ad.get("ad_id")
            seen.add(ad_id)
            if catalog:
                ad["brand"], ad["model"] = catalog.parse_brand_model(ad.get("title") or "")

            # 1) detection SANS ecriture : la page de liste suffit
            state, _changes = db.check(ad)
            if state == "unchanged":
                n_unchanged += 1
                continue

            # l'annonce est nouvelle ou candidate a un changement
            if state == "new":
                n_new += 1
            else:
                n_candidates += 1

            # 2) on recupere la fiche complete : elle fait foi (la liste peut
            #    servir un instantane legerement different)
            if args.details:
                ad = client.detail(ad)
                n_details += 1
            if catalog and not ad.get("brand"):
                ad["brand"], ad["model"] = catalog.parse_brand_model(ad.get("title") or "")
            _apply_notes(ad)
            result = db.upsert_ad(ad)
            if result == "updated":
                n_changed += 1
            elif result == "unchanged":
                # le listing annoncait un changement, la fiche detail dit non
                n_unchanged += 1
                n_candidates -= 1
            db.upsert_images(ad.get("ad_id"), ad.get("images"))
            if not args.quiet and len(seen) % 25 == 0:
                print(f"  ... {len(seen)} annonces vues "
                      f"({n_new} nouvelles, {n_changed} modifiees)", flush=True)
        db.commit()

        # disparitions : uniquement si le run a couvert TOUTE la categorie
        # (sinon on marquerait a tort les annonces non encore parcourues)
        total = getattr(client, "catalog_total", None)
        complete = bool(total) and len(seen) >= int(total)
        if args.mark_missing and complete:
            missing = list(known_before - seen)
            n_gone = db.mark_disappeared(missing)
            if not args.quiet:
                print(f"  {n_gone} annonces disparues marquees inactives")
        elif args.mark_missing and not args.quiet:
            print(f"  run partiel ({len(seen)}/{total or '?'}) : pas de marquage des disparitions")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"  ERREUR : {err}", file=sys.stderr)
        db.touch_run({
            "started_at": started, "finished_at": datetime.now(),
            "category": args.category, "mode": "rescan",
            "ads_listed": len(seen), "ads_details": n_details,
            "ads_inserted": n_new, "ads_updated": n_changed,
            "ads_unchanged": n_unchanged, "ads_disappeared": db.counters["disappeared"],
            "http_requests": client.stats["requests"], "blocked": client.stats["blocked"],
            "status": "error", "error": err,
        })
        db.close()
        return 1

    db.touch_run({
        "started_at": started, "finished_at": datetime.now(),
        "category": args.category, "mode": "rescan",
        "ads_listed": len(seen), "ads_details": n_details,
        "ads_inserted": n_new, "ads_updated": n_changed,
        "ads_unchanged": n_unchanged, "ads_disappeared": db.counters["disappeared"],
        "http_requests": client.stats["requests"], "blocked": client.stats["blocked"],
        "status": "success",
    })
    db.close()
    dt = time.time() - t0
    if not args.quiet:
        print(f"\nRescan termine : {len(seen)} annonces vues | {n_new} nouvelles | "
              f"{n_changed} modifiees (version creee) | {n_unchanged} inchangees | "
              f"{n_candidates} candidats confirmes par la fiche | "
              f"{n_details} fiches detail recuperees | "
              f"{client.stats['requests']} requetes | {dt:.0f}s")
    return 0


def cmd_details(args) -> int:
    """Recupere les fiches detail manquantes d'annonces deja connues."""
    db = Database()
    db.ensure_schema()
    client = AvitoClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)
    catalog = _catalog(client, args)
    config = load_config()
    market = _market(db)
    rows = db.query(
        "SELECT ad_id, link, brand, model FROM ads WHERE site='avito' "
        "AND details_fetched = 0 AND link IS NOT NULL "
        + ("AND category = %s " if args.category else "")
        + "ORDER BY scraped_at LIMIT %s",
        ((args.category,) if args.category else ()) + (args.limit,),
    )
    if not args.quiet:
        print(f"  {len(rows)} annonces a completer")
    n = 0
    for r in rows:
        ad = client.detail({"ad_id": r["ad_id"], "url": r["link"],
                            "brand": r.get("brand"), "model": r.get("model")})
        if not ad.get("details_fetched"):
            continue
        if catalog and not ad.get("brand"):
            ad["brand"], ad["model"] = catalog.parse_brand_model(ad.get("title") or "")
        _apply_notes(ad, market=market, config=config)
        db.upsert_ad(ad)
        db.upsert_images(ad.get("ad_id"), ad.get("images"))
        n += 1
    db.commit()
    db.close()
    if not args.quiet:
        print(f"  {n} fiches completees")
    return 0


def cmd_score(args) -> int:
    """Recalcule les notes des annonces (et cree une version si la note change)."""
    db = Database()
    db.ensure_schema()
    config = load_config()
    market = _market(db)
    sql = ("SELECT raw_json FROM ads WHERE site='avito'"
           + (" AND category = %s" if args.category else "")
           + " ORDER BY ad_id")
    params = (args.category,) if args.category else ()
    if args.limit:
        sql += " LIMIT %s"
        params = params + (args.limit,)
    rows = db.query(sql, params)
    n = 0
    for r in rows:
        try:
            ad = json.loads(r["raw_json"])
        except Exception:
            continue
        _apply_notes(ad, market=market, config=config)
        db.upsert_ad(ad)
        n += 1
    db.commit()
    db.close()
    print(f"{n} annonces rescoredes")
    return 0


def cmd_images(args) -> int:
    """Telechargement des images (separe du scraping)."""
    db = Database()
    db.ensure_schema()
    client = AvitoClient(delay=args.delay, jitter=args.jitter, verbose=not args.quiet)
    fetcher = ImageFetcher(
        client, db,
        variant=args.variant,
        per_ad=args.per_ad,
        delay=args.delay,
        jitter=args.jitter,
        out_dir=args.out_dir,
        force=args.force,
        verbose=not args.quiet,
    )
    try:
        fetcher.run(limit=args.limit, category=args.category)
    finally:
        db.close()
    return 0


def cmd_stats(args) -> int:
    db = Database()
    rows = db.query("""
        SELECT COUNT(*) total,
               SUM(is_active = 1) actives,
               SUM(details_fetched = 1) avec_detail,
               SUM(phone IS NOT NULL) avec_tel,
               COUNT(DISTINCT city) villes, COUNT(DISTINCT brand) marques,
               ROUND(AVG(note), 2) note_moyenne, MAX(note) note_max,
               MIN(year) annee_min, MAX(year) annee_max,
               MIN(price) prix_min, MAX(price) prix_max,
               MAX(scraped_at) dernier_run
        FROM ads
    """)
    v = db.query("SELECT COUNT(*) n FROM ad_versions")
    out = dict(rows[0])
    out["versions"] = v[0]["n"]
    print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
    db.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scrape", description="Scraper avito.ma (HTTP, sans navigateur)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_category=True, default_category=DEFAULT_CATEGORY):
        if with_category:
            sp.add_argument("--category", default=default_category)
        sp.add_argument("--limit", type=int, default=50)
        sp.add_argument("--db", action="store_true", help="ecrire dans MySQL")
        sp.add_argument("--out", default="", help="fichier JSONL de sortie")
        sp.add_argument("--delay", type=float, default=1.5)
        sp.add_argument("--jitter", type=float, default=0.8)
        sp.add_argument("--no-brand", action="store_true")
        sp.add_argument("--quiet", action="store_true")
        return sp

    s = common(sub.add_parser("list", help="lister les annonces d'une categorie"))
    s.add_argument("--details", action="store_true", help="fetcher la fiche complete")
    s.set_defaults(func=cmd_list)

    s2 = common(sub.add_parser("rescan", help="re-scraping intelligent (seulement les changements)"))
    s2.add_argument("--details", action="store_true", default=True)
    s2.add_argument("--mark-missing", action="store_true",
                    help="marquer inactives les annonces disparues (run complet)")
    s2.set_defaults(func=cmd_rescan)

    s3 = common(sub.add_parser("details", help="completer les fiches detail manquantes"),
                with_category=True, default_category="")
    s3.set_defaults(func=cmd_details)

    s4 = common(sub.add_parser("score", help="recalculer les notes"),
                with_category=True, default_category="")
    s4.set_defaults(func=cmd_score)

    s5 = sub.add_parser("images", help="telecharger les images (separe du scraping)")
    s5.add_argument("--variant", default="images", choices=list(VARIANTS),
                    help="qualite : card (~20 Ko) | images (~42 Ko) | full_hd (~205 Ko)")
    s5.add_argument("--category", default="", help="restreindre a une categorie")
    s5.add_argument("--limit", type=int, default=20,
                    help="nb d'annonces a traiter (0 = toutes)")
    s5.add_argument("--per-ad", type=int, default=0, dest="per_ad",
                    help="nb max d'images par annonce (0 = toutes)")
    s5.add_argument("--out-dir", default="/data/images", dest="out_dir")
    s5.add_argument("--delay", type=float, default=1.0)
    s5.add_argument("--jitter", type=float, default=0.5)
    s5.add_argument("--force", action="store_true", help="retelecharger meme si deja fait")
    s5.add_argument("--quiet", action="store_true")
    s5.set_defaults(func=cmd_images)

    s6 = sub.add_parser("stats", help="statistiques sur la base")
    s6.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
