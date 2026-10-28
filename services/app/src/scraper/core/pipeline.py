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
from .progress import Progress
from .scoring import compute_notes, compute_notes_v2, load_config
from .site import SiteAdapter


def _apply_notes(ad: dict, market=None, config=None) -> dict:
    """Applique le scoring legacy ET le scoring v2 sur l'annonce."""
    # normalisation type : certains adapters seportent des year/km en chaine
    for _k in ("year", "km"):
        if _k in ad and isinstance(ad[_k], str):
            try:
                ad[_k] = int(float(ad[_k].strip()))
            except (ValueError, TypeError):
                pass
    if "mileage_km" in ad and isinstance(ad["mileage_km"], str):
        try:
            ad["mileage_km"] = int(float(ad["mileage_km"].strip()))
        except (ValueError, TypeError):
            pass
    if "price" in ad and isinstance(ad["price"], str):
        try:
            ad["price"] = int(float(ad["price"].strip()))
        except (ValueError, TypeError):
            pass
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
    ad["note_confidence"] = v2["note_confiance"]
    ad["notes_v2"] = v2
    v2 = compute_notes_v2(ad, market=market, config=config)
    ad["note_etat"] = v2["note_etat"]
    ad["note_achat"] = v2["note_achat"]
    ad["note_global"] = v2["note_global"]
    ad["note_confiance"] = v2["note_confiance"]
    ad["notes_v2"] = v2

    # --- price anomaly detection (expose via price_anomaly / price_anomaly_reason)
    _reason = None
    _price = ad.get("price")
    if _price is not None:
        try:
            price = float(_price)
        except (TypeError, ValueError):
            price = None
        if price is not None and price < 10000:
            _reason = "prix inférieur à 10 000 DH"
        elif price is not None and price > 3000000:
            _reason = "prix supérieur à 3 000 000 DH"
        elif price is not None and price < 0:
            _reason = "prix négatif"
        elif price is not None and market is not None:
            try:
                bounds = market.segment_bounds(ad)
            except Exception:
                bounds = None
            if bounds:
                mn, mx, avg, nseg = bounds
                ratio, _label, nseg2 = market.price_ratio(ad)
                if ratio and nseg >= getattr(market, "min_sample", 5):
                    if ratio > 4:
                        _reason = "prix trop élevé pour le segment"
                    elif ratio < 0.3:
                        _reason = "prix trop bas pour le segment"
        ad["price_anomaly"] = (1 if _reason else 0)
        ad["price_anomaly_reason"] = _reason
    else:
        ad["price_anomaly"] = 0
        ad["price_anomaly_reason"] = None

    if not ad.get("body_type"):
        try:
            from .body_types import infer_body_type
            _bt = infer_body_type(ad)
            if _bt:
                ad["body_type"] = _bt
        except Exception:
            pass
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
                 page_size: int = 38, verbose: bool = True,
                 progress: Optional[Progress] = None):
    """Parcourt les pages de liste d'une categorie.

    Rend des annonces normalisees. La categorie peut etre decoupee en
    tranches (`site.slices`) : c'est necessaire quand le site plafonne la
    pagination (Avito : ~3 966 annonces par tranche).
    """
    total_seen = 0
    if getattr(site, "use_slices", True) and hasattr(site, "slices"):
        slices = site.slices(category, client)
    else:
        slices = [(category, category)]
    if len(slices) > 1:
        print(f"  {len(slices)} tranches", flush=True)
    if progress is not None:
        progress.update(slices_total=len(slices),
                        total=getattr(site, "global_total", 0) or progress.state.get("total", 0))
    for idx, (label, path) in enumerate(slices):
        if limit and total_seen >= limit:
            return
        if progress is not None:
            progress.update(current_slice=label, slices_done=idx)
        for ad in _iter_slice(site, client, path, limit, total_seen, page_size,
                              verbose, progress):
            total_seen += 1
            yield ad
            if limit and total_seen >= limit:
                return


def _iter_slice(site: SiteAdapter, client, category: str, limit: int,
                 already: int, page_size: int, verbose: bool,
                 progress: Optional[Progress] = None):
    """Parcourt les pages d'une seule tranche."""
    offset = 0
    seen = 0
    total = None
    seen_ids: set = set()
    try:
        site.reset_pagination()
    except Exception:
        pass
    failures = 0
    while True:
        url = site.listing_url(category, offset)
        ads = []
        html = ""
        # certaines pages renvoient une variante incomplete : on reessaye
        try:
            # "pages" en flax (401/502) : on reessay souvent pour eviter d'abandonner
            # prematurerement quand le site ressent le referrer.
            for attempt in range(1, 8):
                html = client.get(url)
                if total is None:
                    total = site.total_ads(html)
                    if total and verbose:
                        print(f"  catalogue : {total} annonces", flush=True)
                    # le total alimente la barre de progression (ETA).
                    # on garde le plus grand connu : avec des tranches, le
                    # total par tranche est plus petit que le total global
                    if progress is not None and total:
                        known = int(progress.state.get("total") or 0)
                        progress.update(total=max(known, int(total)))
                ads = site.parse_listing(html, category)
                if ads:
                    break
                if attempt < 7:
                    client.log(f"  ! page vide ({attempt}/8) - nouvel essai")
                    time.sleep(min(60, 8 * attempt))
        except Exception as e:
            # erreur HTTP transitoire (502/503 du site) : on ne tue pas le run
            failures += 1
            client.log(f"  ! page en echec ({type(e).__name__}: {str(e)[:60]}) "
                       f"- {failures}/25")
            if failures >= 25:
                client.log("  ! trop d'echecs consecutifs - fin du balayage (retries_failed)")
                try:
                    site._retry_exhausted = True
                except Exception:
                    pass
                break
            time.sleep(min(60, 5 * failures))
            continue
        failures = 0
        if not ads:
            break
        # la taille de page varie (38 puis 35) : on ne s'arrete que si la page
        # ne fait plus Borneo de progrès (memes annonces renvoyees)
        page_ids = {a.get("ad_id") for a in ads}
        if page_ids and page_ids <= seen_ids:
            # retry une fois avec un delai : parfois la page est flapi.
            client.log("  ! page sans nouvelles annonces - essai de rechargement apres pause")
            time.sleep(10)
            html2 = None
            try:
                html2 = client.get(url)
                ads2 = site.parse_listing(html2, category)
                if ads2:
                    ads = ads2
                    page_ids = {a.get("ad_id") for a in ads}
                    if page_ids and page_ids <= seen_ids:
                        client.log("  ! page sans nouvelles annonces - fin du balayage")
                        break
                else:
                    client.log("  ! page sans nouvelles annonces - fin du balayage")
                    break
            except Exception:
                client.log("  ! page sans nouvelles annonces - fin du balayage")
                break
        if not ads:
            # relaxation finale : seconde pause + retry
            time.sleep(5)
            break
        seen_ids |= page_ids
        for ad in ads:
            yield ad
            seen += 1
            if limit and (already + seen) >= limit:
                if verbose:
                    print(f"  plafond atteint ({already + seen})", flush=True)
                return
        offset += len(ads)
        if total and offset >= total:
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
             start: Optional[datetime] = None, run_id: Optional[int] = None) -> dict:
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

    n_listed = n_details = n_db_errors = 0
    out_fh = open(out, "w", encoding="utf-8") if out else None
    error = None
    # un run garde la main sur sa propre possibilite de "restart fail" : on
    # le met a "retries_failed" si le balayage casse pour cause de reseau
    # (voir _iter_slice) plutot que d'annoncer un "success" derriere un trou.
    try:
        setattr(site, "_retry_exhausted", False)
    except Exception:
        pass
    progress = Progress(site.name, mode="list", category=category)
    try:
        for ad in iter_listing(site, client, category, limit, verbose=not quiet,
                               progress=progress):
            n_listed += 1
            if not ad.get("brand"):
                # l'adaptateur gere lui-meme son referentiel (cache sur le client)
                ad = site.enrich(ad, client)
            if details:
                ad = fetch_detail(site, client, ad)
                n_details += 1
            _apply_notes(ad, market=market, config=config)
            if out_fh:
                out_fh.write(json.dumps(ad, ensure_ascii=False, default=str) + "\n")
            if db is not None:
                try:
                    db.upsert_ad(ad, site=site.name)
                    db.upsert_images(ad.get("ad_id"), ad.get("images"),
                                      site=site.name)
                except Exception as e:
                    # une annonce en echec ne doit pas arreter le run
                    n_db_errors += 1
                    client.log(f"  ! ecriture en echec ({type(e).__name__}: {e})")
                    try:
                        db.conn.rollback()
                    except Exception:
                        pass
                # commit par lots : un run interrompu ne perd pas tout
                if n_listed % 20 == 0:
                    try:
                        db.commit()
                    except Exception:
                        db.reconnect()   # MySQL redemarre : on repart
            if n_listed % 10 == 0:
                progress.update(listed=n_listed, details=n_details,
                                db_errors=n_db_errors)
                progress.stats(client, db)
            if n_listed % 100 == 0 and db is not None:
                db.update_run(run_id, {
                    "ads_listed": n_listed, "ads_details": n_details,
                    "ads_inserted": db.counters["inserted"],
                    "ads_updated": db.counters["updated"],
                    "ads_unchanged": db.counters["unchanged"],
                    "http_requests": client.stats["requests"],
                    "blocked": client.stats["blocked"],
                })
            if not quiet and n_listed % 25 == 0:
                print(f"  ... {n_listed} annonces", flush=True)
        if db is not None:
            try:
                db.commit()
            except Exception:
                db.reconnect()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"  ERREUR : {error}", file=sys.stderr)
        if os.environ.get("SCRAPER_DEBUG"):
            traceback.print_exc()
    finally:
        if out_fh:
            out_fh.close()
        progress.update(listed=n_listed, details=n_details,
                        db_errors=n_db_errors)
        progress.stats(client, db)
        if error:
            final_status = "error"
        elif getattr(site, "_retry_exhausted", False):
            final_status = "retries_failed"
            err_msg = "retry budget exhausted"
            if not error:
                error = err_msg
        else:
            final_status = "success"
        progress.finish(final_status, error)
        try:
            site._retry_exhausted = False
        except Exception:
            pass

    dt = time.time() - t0
    dropped = int(getattr(site, "log_sponsored", 0) or 0)
    progress.update(dropped=dropped)
    if not quiet:
        if dropped:
            print(f"  {dropped} annonces hors categorie ecartees "
                  f"(publicites sponsorisees)", flush=True)
        print(f"\nTermine : {n_listed} annonces listees"
              + (f", {n_details} fiches detail" if details else "")
              + f" | {client.stats['requests']} requetes HTTP"
              + f" | {client.stats['blocked']} blocages"
              + f" | {dt:.0f}s", flush=True)
    return {"listed": n_listed, "details": n_details, "error": error,
            "seconds": dt, "started": started}


def run_rescan(site: SiteAdapter, client, db: Database, *,
               category: str, limit: int = 0, details: bool = True,
               mark_missing: bool = False, quiet: bool = False,
               run_id: Optional[int] = None) -> dict:
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
    n_new = n_changed = n_unchanged = n_details = n_candidates = n_db_errors = 0
    progress = Progress(site.name, mode="rescan", category=category)

    for ad in iter_listing(site, client, category, limit, verbose=not quiet,
                           progress=progress):
        ad_id = ad.get("ad_id")
        if ad_id is not None:
            seen.add(ad_id)
        if not ad.get("brand"):
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
        try:
            result = db.upsert_ad(ad, site=site.name)
        except Exception as e:
            n_db_errors += 1
            client.log(f"  ! ecriture en echec ({type(e).__name__}: {e})")
            try:
                db.conn.rollback()
            except Exception:
                pass
            continue
        if result == "updated":
            n_changed += 1
        elif result == "unchanged":
            n_unchanged += 1
            n_candidates -= 1
        try:
            db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name)
        except Exception as e:
            n_db_errors += 1
            client.log(f"  ! images en echec ({type(e).__name__}: {e})")
        if len(seen) % 20 == 0:
            try:
                db.commit()      # commit par lots (transactions courtes)
            except Exception:
                db.reconnect()       # MySQL redemarre : on repart
        if len(seen) % 10 == 0:
            progress.update(listed=len(seen), details=n_details,
                            unchanged=n_unchanged)
            progress.stats(client, db)
        if len(seen) % 100 == 0:
            db.update_run(run_id, {
                "ads_listed": len(seen), "ads_details": n_details,
                "ads_inserted": db.counters["inserted"],
                "ads_updated": db.counters["updated"],
                "ads_unchanged": db.counters["unchanged"],
                "http_requests": client.stats["requests"],
                "blocked": client.stats["blocked"],
            })
        if not quiet and len(seen) % 25 == 0:
            print(f"  ... {len(seen)} annonces vues "
                  f"({n_new} nouvelles, {n_changed} modifiees)", flush=True)

    try:
        db.commit()
    except Exception:
        db.reconnect()
    progress.update(listed=len(seen), details=n_details, unchanged=n_unchanged)
    progress.stats(client, db)

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
    progress.finish("success")
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
    progress = Progress(site.name, mode="details", category=category)
    progress.update(total=len(rows))
    for r in rows:
        ad = fetch_detail(site, client, {
            "ad_id": r["ad_id"], "url": r["link"],
            "brand": r.get("brand"), "model": r.get("model")})
        if not ad.get("details_fetched"):
            continue
        if not ad.get("brand"):
            ad = site.enrich(ad, client)
        _apply_notes(ad, market=market, config=config)
        db.upsert_ad(ad, site=site.name)
        db.upsert_images(ad.get("ad_id"), ad.get("images"), site=site.name)
        n += 1
        if n % 5 == 0:
            progress.update(details=n)
            progress.stats(client, db)
    db.commit()
    progress.update(details=n)
    progress.stats(client, db)
    progress.finish("success")
    if not quiet:
        print(f"  {n} fiches completees", flush=True)
    return {"details": n}
