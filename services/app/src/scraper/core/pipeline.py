"""Pipeline de scraping generique, independant du site.

Un site fournit un `SiteAdapter` ; le pipeline se charge de :
  * parcourir les pages de liste
  * detecter les changements (comparaison champ par champ)
  * recuperer les fiches detail uniquement pour les annonces concernees
  * ecrire en base (avec versioning) et calculer les notes

Modes
-----
list     : scrape et ecrit tout
rescan   : ne recupere le detail que des annonces nouvelles/modifiees
details  : complete les fiches detail manquantes
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Optional

from .catalog import Catalog
from .db import Database
from .history import compare
from .market import MarketStats
from .scoring import compute_notes, compute_notes_v2, load_config
from .site import SiteAdapter


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

    v2 = compute_notes_v2(ad, market=market, config=config)
    ad["note_etat"] = v2["note_etat"]
    ad["note_achat"] = v2["note_achat"]
    ad["note_global"] = v2["note_global"]
    ad["note_confiance"] = v2["note_confiance"]
    ad["notes_v2"] = v2
    return ad


def _catalog(site: SiteAdapter, client, verbose: bool = True):
    """Catalogue marques/modeles (optionnel, propre a un site)."""
    getter = getattr(site, "catalog_fetcher", None)
    if not getter:
        return None
    try:
        from .catalog import catalog_path_for
        cat = Catalog(client, path=catalog_path_for(site.name))
        cat.fetcher = getter(client)   # l'adaptateur renvoie la fonction de fetch
        cat.fetch()
        return cat
    except Exception as e:
        if verbose:
            print(f"  ! catalogue indisponible ({e})")
        return None


def iter_listing(site: SiteAdapter, client, category: str, limit: int = 0,
                 page_size: int = 38, verbose: bool = True):
    """Parcourt les pages de liste d'une categorie.

    Rend des annonces normalisees. Gere la pagination de facon generique :
    l'offset est incremente du nombre d'annonces vues (les sites n'ont pas
    tous la meme taille de page).
    """
    offset = 0
    seen = 0
    total = None
    while True:
        url = site.listing_url(category, offset)
        ads = []
        html = ""
        # certaines pages renvoient une variante incomplete : on reessaye
        for attempt in range(1, 4):
            html = client.get(url)
            if total is None:
                total = site.total_ads(html)
                if total and verbose:
                    print(f"  catalogue : {total} annonces", flush=True)
            ads = site.parse_listing(html)
            if ads:
                break
            if attempt < 3:
                client.log(f"  ! page vide ({attempt}/3) - nouvel essai")
        if not ads:
            break
        for ad in ads:
            yield ad
            seen += 1
            if limit and seen >= limit:
                if verbose:
                    print(f"  plafond atteint ({seen})", flush=True)
                return
        offset += len(ads)
        if total and offset >= total:
            break
        if len(ads) < page_size and not getattr(site, "page_size_fixed", False):
            break


def fetch_detail(site: SiteAdapter, client, ad: dict) -> dict:
    """Recupere la fiche detail d'une annonce (si le site le permet)."""
    url = site.detail_url(ad)
    if not url:
        return ad
    try:
        html = client.get(url)
        full = site.parse_detail(html, ad)
    except Exception as e:
        client.log(f"  ! detail en echec ({type(e).__name__}: {e})")
        return ad
    if not full:
        return ad
    merged = dict(ad)
    for k, v in full.items():
        if v is None or v == [] or v == {}:
            continue
        merged[k] = v
    merged["url"] = url
    merged["details_fetched"] = True
    if merged.get("images"):
        merged["nb_images"] = len(merged["images"])
    return merged


# ------------------------------------------------------------------ modes
def run_list(site: SiteAdapter, client, db: Optional[Database], *,
             category: str, limit: int = 0, details: bool = False,
             out: str = "", quiet: bool = False,
             start: Optional[datetime] = None) -> dict:
    """Scrape une categorie et ecrit en base."""
    started = start or datetime.now()
    t0 = time.time()
    config = load_config()
    market = None
    if db is not None:
        try:
            market = MarketStats(db)
        except Exception:
            market = None
    catalog = _catalog(site, client, not quiet)

    n_listed = n_details = 0
    out_fh = open(out, "w", encoding="utf-8") if out else None
    error = None
    try:
        for ad in iter_listing(site, client, category, limit, verbose=not quiet):
            n_listed += 1
            if catalog is not None and not ad.get("brand"):
                ad = site.enrich(ad, client)
            if details:
                ad = fetch_detail(site, client, ad)
                n_details += 1
            _apply_notes(ad, market=market, config=config)
            if out_fh:
                out_fh.write(json.dumps(ad, ensure_ascii=False, default=str) + "\n")
            if db is not None:
                db.upsert_ad(ad, site=site.name)
                db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name)
                # commit par lots : un run interrompu ne perd pas tout
                if n_listed % 50 == 0:
                    db.commit()
            if not quiet and n_listed % 25 == 0:
                print(f"  ... {n_listed} annonces", flush=True)
        if db is not None:
            db.commit()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"  ERREUR : {error}", file=sys.stderr)
        if os.environ.get("SCRAPER_DEBUG"):
            traceback.print_exc()
    finally:
        if out_fh:
            out_fh.close()

    dt = time.time() - t0
    if not quiet:
        print(f"\nTermine : {n_listed} annonces listees"
              + (f", {n_details} fiches detail" if details else "")
              + f" | {client.stats['requests']} requetes HTTP"
              + f" | {client.stats['blocked']} blocages"
              + f" | {dt:.0f}s", flush=True)
    return {"listed": n_listed, "details": n_details, "error": error,
            "seconds": dt, "started": started}


def run_rescan(site: SiteAdapter, client, db: Database, *,
               category: str, limit: int = 0, details: bool = True,
               mark_missing: bool = False, quiet: bool = False) -> dict:
    """Re-scraping intelligent : la liste detecte, la fiche detail confirme."""
    started = datetime.now()
    t0 = time.time()
    config = load_config()
    try:
        market = MarketStats(db)
    except Exception:
        market = None
    catalog = _catalog(site, client, not quiet)

    known_before = set(db.known_ad_ids(site.name))
    seen: set = set()
    n_new = n_changed = n_unchanged = n_details = n_candidates = 0

    for ad in iter_listing(site, client, category, limit, verbose=not quiet):
        ad_id = ad.get("ad_id")
        if ad_id is not None:
            seen.add(ad_id)
        if catalog is not None and not ad.get("brand"):
            ad = site.enrich(ad, client)

        # 1) detection SANS ecriture : la page de liste suffit
        state, _changes = db.check(ad, site=site.name)
        if state == "unchanged":
            n_unchanged += 1
            continue

        if state == "new":
            n_new += 1
        else:
            n_candidates += 1

        # 2) la fiche detail fait foi
        if details:
            ad = fetch_detail(site, client, ad)
            n_details += 1
        _apply_notes(ad, market=market, config=config)
        result = db.upsert_ad(ad, site=site.name)
        if result == "updated":
            n_changed += 1
        elif result == "unchanged":
            n_unchanged += 1
            n_candidates -= 1
        db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name)
        if len(seen) % 50 == 0:
            db.commit()          # commit par lots
        if not quiet and len(seen) % 25 == 0:
            print(f"  ... {len(seen)} annonces vues "
                  f"({n_new} nouvelles, {n_changed} modifiees)", flush=True)

    db.commit()

    # disparitions : uniquement si le run a couvert TOUTE la categorie
    total = getattr(client, "catalog_total", None)
    complete = bool(total) and len(seen) >= int(total)
    n_gone = 0
    if mark_missing and complete:
        n_gone = db.mark_disappeared(list(known_before - seen), site=site.name)
        if not quiet:
            print(f"  {n_gone} annonces disparues marquees inactives")
    elif mark_missing and not quiet:
        print(f"  run partiel ({len(seen)}/{total or '?'}) : pas de marquage")

    dt = time.time() - t0
    if not quiet:
        print(f"\nRescan termine : {len(seen)} annonces vues | {n_new} nouvelles | "
              f"{n_changed} modifiees (version creee) | {n_unchanged} inchangees | "
              f"{n_details} fiches detail | {client.stats['requests']} requetes | {dt:.0f}s",
              flush=True)
    return {"seen": len(seen), "new": n_new, "changed": n_changed,
            "unchanged": n_unchanged, "details": n_details, "disappeared": n_gone,
            "seconds": dt, "started": started, "error": None}


def run_details(site: SiteAdapter, client, db: Database, *,
                limit: int = 50, category: str = "", quiet: bool = False) -> dict:
    """Complete les fiches detail manquantes d'annonces deja connues."""
    config = load_config()
    try:
        market = MarketStats(db)
    except Exception:
        market = None
    catalog = _catalog(site, client, not quiet)
    where = [f"site = %s"]
    params: list[Any] = [site.name]
    if category:
        where.append("category = %s")
        params.append(category)
    where.append("details_fetched = 0")
    where.append("link IS NOT NULL")
    sql = (f"SELECT ad_id, link, brand, model FROM ads WHERE {' AND '.join(where)} "
           "ORDER BY scraped_at LIMIT %s")
    params.append(limit)
    rows = db.query(sql, tuple(params))
    if not quiet:
        print(f"  {len(rows)} annonces a completer", flush=True)
    n = 0
    for r in rows:
        ad = fetch_detail(site, client, {
            "ad_id": r["ad_id"], "url": r["link"],
            "brand": r.get("brand"), "model": r.get("model")})
        if not ad.get("details_fetched"):
            continue
        if catalog is not None and not ad.get("brand"):
            ad = site.enrich(ad, client)
        _apply_notes(ad, market=market, config=config)
        db.upsert_ad(ad, site=site.name)
        db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name)
        n += 1
    db.commit()
    if not quiet:
        print(f"  {n} fiches completees", flush=True)
    return {"details": n}
